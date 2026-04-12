import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
import yaml

log = logging.getLogger("slack-scanner")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "slack-scanner-state.json"

SLACK_API_BASE = "https://slack.com/api"

MAX_CHANNELS_PER_CYCLE = 20
MAX_THREADS_PER_CHANNEL = 30
MAX_TRANSCRIPT_LINES = 50


class SlackScanner:
    def __init__(self, role: str = "full"):
        self.role = role
        self._user_cache: dict = {}  # user_id -> display_name

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

    def _scanner_config(self) -> dict:
        return self._load_config().get("slack_scanner", {})

    # ── State ─────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {"channels": {}}

    def _save_state(self, state: dict):
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(STATE_FILE))
        except Exception as e:
            log.warning("Failed to save slack scanner state: %s", e)

    def _prune_threads(self, state: dict, lookback_days: int):
        """Prune thread entries older than lookback_days, cap at 1000 entries."""
        cutoff = time.time() - (lookback_days * 86400)
        for channel_id, channel_state in list(state.get("channels", {}).items()):
            threads = channel_state.get("threads", {})
            pruned = {}
            for thread_ts, thread_data in threads.items():
                try:
                    last_ts_float = float(thread_data.get("last_ts", "0"))
                    if last_ts_float >= cutoff:
                        pruned[thread_ts] = thread_data
                except (ValueError, TypeError):
                    pass
            # Cap at 1000
            if len(pruned) > 1000:
                sorted_threads = sorted(
                    pruned.items(), key=lambda x: float(x[1].get("last_ts", "0")), reverse=True
                )
                pruned = dict(sorted_threads[:1000])
            channel_state["threads"] = pruned

    # ── API helpers ───────────────────────────────────────────────────────────

    async def _api_call(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: dict = None,
        _retry: int = 0,
    ) -> Optional[dict]:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token:
            return None
        try:
            resp = await client.get(
                f"{SLACK_API_BASE}/{method}",
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                log.warning("Slack API rate limited — waiting %ds (method=%s)", retry_after, method)
                if _retry < 1:
                    await asyncio.sleep(retry_after)
                    return await self._api_call(client, method, params, _retry + 1)
                log.error("Persistent rate limit on %s — skipping", method)
                return None
            if resp.status_code == 401:
                log.error("Slack API auth failed (401) — check SLACK_BOT_TOKEN")
                return None
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                log.warning("Slack API error on %s: %s", method, data.get("error", "unknown"))
                return None
            return data
        except httpx.HTTPStatusError as e:
            log.warning("Slack API HTTP error %s: %s", e.response.status_code, method)
            return None
        except Exception as e:
            log.warning("Slack API request failed (%s): %s", method, e)
            return None

    # ── Channel discovery ─────────────────────────────────────────────────────

    async def _list_channels(self, client: httpx.AsyncClient) -> list:
        """Return list of (channel_id, channel_name) tuples for channels bot can access."""
        channels = []
        cursor = None
        while True:
            params = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self._api_call(client, "conversations.list", params)
            if not data:
                break
            for ch in data.get("channels", []):
                if not ch.get("is_archived", False):
                    channels.append((ch["id"], ch.get("name", "unknown")))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            await asyncio.sleep(1)  # rate limit compliance
        return channels

    def _filter_channels(self, channels: list, config: dict) -> list:
        """Apply whitelist/blacklist filtering."""
        include = config.get("channel_include", [])
        exclude = config.get("channel_exclude", [])

        if include:
            # Whitelist mode
            result = [(cid, cname) for cid, cname in channels if cname in include]
            missing = set(include) - {cname for _, cname in result}
            for m in missing:
                log.warning("Channel '%s' in channel_include not found or bot not added", m)
            return result
        else:
            # Blacklist mode
            return [(cid, cname) for cid, cname in channels if cname not in exclude]

    # ── Message polling ───────────────────────────────────────────────────────

    async def _fetch_channel_messages(
        self, client: httpx.AsyncClient, channel_id: str, high_water: Optional[str], lookback_days: int
    ) -> list:
        """Return list of message dicts newer than high_water."""
        if high_water:
            oldest = high_water
            inclusive = False
        else:
            # First run: lookback_days
            oldest = str(time.time() - lookback_days * 86400)
            inclusive = True

        messages = []
        cursor = None
        while True:
            params = {"channel": channel_id, "oldest": oldest, "limit": 100, "inclusive": str(inclusive).lower()}
            if cursor:
                params["cursor"] = cursor
            data = await self._api_call(client, "conversations.history", params)
            if not data:
                break
            messages.extend(data.get("messages", []))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            await asyncio.sleep(1)
        return messages

    # ── Thread retrieval ──────────────────────────────────────────────────────

    async def _fetch_thread_replies(self, client: httpx.AsyncClient, channel_id: str, thread_ts: str) -> list:
        """Return all messages in a thread (including root message)."""
        replies = []
        cursor = None
        while True:
            params = {"channel": channel_id, "ts": thread_ts, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = await self._api_call(client, "conversations.replies", params)
            if not data:
                break
            replies.extend(data.get("messages", []))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            await asyncio.sleep(1)
        return replies

    # ── User identity resolution ──────────────────────────────────────────────

    async def _resolve_user(self, client: httpx.AsyncClient, user_id: str) -> str:
        """Resolve user ID to display name, with in-memory cache."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        data = await self._api_call(client, "users.info", {"user": user_id})
        if data and data.get("user"):
            name = data["user"].get("real_name") or data["user"].get("name") or "Unknown User"
        else:
            name = "Unknown User"

        self._user_cache[user_id] = name
        await asyncio.sleep(1)  # rate limit compliance
        return name

    # ── LLM summary ───────────────────────────────────────────────────────────

    async def _generate_summary(self, channel_name: str, messages: list, participants: list) -> dict:
        """Generate summary and tags from thread messages."""
        # Build message excerpt (cap at 3000 chars)
        message_lines = []
        total_len = 0
        for msg in messages:
            ts = msg.get("ts", "")
            user_name = msg.get("_resolved_name", "unknown")
            text = msg.get("text", "")
            # Format as [HH:MM] Name: text
            try:
                dt = datetime.fromtimestamp(float(ts))
                time_str = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                time_str = "??:??"
            line = f"[{time_str}] {user_name}: {text}"
            if total_len + len(line) > 3000:
                break
            message_lines.append(line)
            total_len += len(line) + 1

        messages_text = "\n".join(message_lines)
        participants_text = ", ".join(participants)

        prompt = (
            f"Summarize this Slack thread in 1-2 sentences. Then provide 3-5 tags.\n\n"
            f"Channel: #{channel_name}\n"
            f"Participants: {participants_text}\n\n"
            f"Messages:\n{messages_text}\n\n"
            "Return JSON only:\n"
            '{\n'
            '  "summary": "...",\n'
            '  "tags": ["tag1", "tag2"]\n'
            '}'
        )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model="summarize",
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            result = json.loads(text)
            # Normalize tags
            tags = result.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            tags = [re.sub(r'[^a-z0-9-]', '-', t.lower()).strip('-') for t in tags if t]
            result["tags"] = tags
            # Cap summary at 280 chars
            summary = result.get("summary", "")
            if len(summary) > 280:
                summary = summary[:277] + "..."
            result["summary"] = summary
            return result
        except json.JSONDecodeError:
            log.warning("LLM returned non-JSON summary for channel %s — using fallback", channel_name)
            # Fallback: use first message text
            first_text = messages[0].get("text", "") if messages else ""
            return {"summary": first_text[:280], "tags": []}
        except Exception as e:
            log.warning("LLM summary call failed for channel %s: %s — using fallback", channel_name, e)
            first_text = messages[0].get("text", "") if messages else ""
            return {"summary": first_text[:280], "tags": []}

    # ── Memory file write ─────────────────────────────────────────────────────

    def _slugify(self, text: str, max_len: int = 40) -> str:
        s = text.lower()
        s = re.sub(r'[^a-z0-9]+', '-', s)
        s = s.strip('-')
        return s[:max_len].rstrip('-')

    def _thread_filename(self, channel_name: str, thread_ts: str) -> str:
        channel_slug = self._slugify(channel_name)
        # Replace . with - in thread_ts
        ts_numeric = thread_ts.replace(".", "-")
        return f"slack-thread-{channel_slug}-{ts_numeric}.md"

    def _write_memory(
        self,
        channel_id: str,
        channel_name: str,
        thread_ts: str,
        messages: list,
        llm_result: dict,
    ):
        filename = self._thread_filename(channel_name, thread_ts)
        memory_path = MEMORIES_DIR / filename
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Extract participants
        slack_user_id = os.environ.get("SLACK_USER_ID", "")
        participants = []
        seen_users = set()
        for msg in messages:
            user_id = msg.get("user", "")
            if user_id and user_id not in seen_users:
                seen_users.add(user_id)
                name = msg.get("_resolved_name", "Unknown User")
                participants.append({"name": name, "slack_id": user_id})

        # Derive source_title from root message
        root_msg = messages[0] if messages else {}
        root_text = root_msg.get("text", "Untitled thread")
        source_title = f"Thread: {root_text[:60]}"

        # last_message timestamp
        last_msg_ts = messages[-1].get("ts", "") if messages else ""
        try:
            last_message = datetime.fromtimestamp(float(last_msg_ts)).strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            last_message = now

        # Build frontmatter
        fm = {
            "source_title": source_title,
            "summary": llm_result.get("summary", ""),
            "tags": llm_result.get("tags", []),
            "last_scanned": now,
            "source_url": f"slack:{channel_id}/{thread_ts}",
            "type": "slack_thread",
            "channel": channel_name,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "participants": participants,
            "message_count": len(messages),
            "last_message": last_message,
        }
        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

        # Messages section (cap at MAX_TRANSCRIPT_LINES)
        message_lines = []
        for msg in messages[:MAX_TRANSCRIPT_LINES]:
            ts = msg.get("ts", "")
            user_name = msg.get("_resolved_name", "unknown")
            text = msg.get("text", "")
            try:
                dt = datetime.fromtimestamp(float(ts))
                time_str = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                time_str = "??:??"
            message_lines.append(f"[{time_str}] {user_name}: {text}")

        if len(messages) > MAX_TRANSCRIPT_LINES:
            extra = len(messages) - MAX_TRANSCRIPT_LINES
            message_lines.append(f"(... {extra} more messages)")

        messages_section = "\n".join(message_lines) if message_lines else "(no messages)"

        content = (
            f"---\n{frontmatter}---\n\n"
            f"## Messages\n\n{messages_section}\n\n"
            f"## Context\n\n{llm_result.get('summary', '')}\n"
        )

        tmp_path = memory_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(memory_path))
            log.debug("Wrote %s", memory_path.name)
        except Exception:
            log.exception("Failed to write %s", memory_path)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Run loop ──────────────────────────────────────────────────────────────

    async def run_loop(self, stop_event: asyncio.Event):
        if self.role != "full":
            log.debug("Slack scanner skipped (role=%s)", self.role)
            return

        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            log.warning("SLACK_BOT_TOKEN not set — Slack scanner disabled")
            return

        sc = self._scanner_config()
        interval = sc.get("interval_seconds", 300)
        log.info("Slack scanner started — polling every %ds", interval)

        while not stop_event.is_set():
            try:
                await self._run_scan()
            except Exception:
                log.exception("Uncaught error in slack scanner cycle")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _run_scan(self):
        sc = self._scanner_config()
        lookback_days = int(sc.get("lookback_days", 7))
        min_thread_messages = int(sc.get("min_thread_messages", 2))
        max_channels = int(sc.get("max_channels_per_cycle", MAX_CHANNELS_PER_CYCLE))
        max_threads = int(sc.get("max_threads_per_channel", MAX_THREADS_PER_CHANNEL))

        state = self._load_state()
        self._prune_threads(state, lookback_days)

        # Clear user cache each cycle
        self._user_cache = {}

        rate_limit_hits = 0

        async with httpx.AsyncClient(timeout=30) as client:
            # Discover channels
            all_channels = await self._list_channels(client)
            if not all_channels:
                log.debug("No channels found or API error")
                return

            channels = self._filter_channels(all_channels, sc)
            if not channels:
                log.debug("No channels after filtering")
                return

            # Process up to max_channels
            for channel_id, channel_name in channels[:max_channels]:
                if rate_limit_hits >= 3:
                    log.warning("3+ rate limit hits this cycle — skipping remaining channels")
                    break

                log.debug("Scanning channel: %s", channel_name)

                channel_state = state["channels"].setdefault(channel_id, {"name": channel_name, "threads": {}})
                high_water = channel_state.get("high_water")

                messages = await self._fetch_channel_messages(client, channel_id, high_water, lookback_days)
                if messages is None:
                    rate_limit_hits += 1
                    continue

                # Find thread roots
                thread_roots = []
                for msg in messages:
                    reply_count = msg.get("reply_count", 0)
                    thread_ts = msg.get("thread_ts")
                    ts = msg.get("ts")
                    if reply_count >= min_thread_messages and thread_ts == ts:
                        thread_roots.append(msg)

                # Process threads
                for thread_msg in thread_roots[:max_threads]:
                    thread_ts = thread_msg["ts"]
                    thread_state = channel_state["threads"].setdefault(thread_ts, {})

                    # Fetch full thread to check for changes
                    full_thread = await self._fetch_thread_replies(client, channel_id, thread_ts)
                    if full_thread is None:
                        rate_limit_hits += 1
                        continue

                    current_count = len(full_thread)
                    current_last_ts = full_thread[-1].get("ts", "") if full_thread else ""

                    # Change detection
                    prev_count = thread_state.get("message_count", 0)
                    prev_last_ts = thread_state.get("last_ts", "")

                    if current_count == prev_count and current_last_ts == prev_last_ts:
                        log.debug("Thread %s unchanged — skipping", thread_ts)
                        continue

                    # Resolve user names
                    for msg in full_thread:
                        user_id = msg.get("user", "")
                        if user_id:
                            name = await self._resolve_user(client, user_id)
                            msg["_resolved_name"] = name
                        else:
                            msg["_resolved_name"] = "Unknown User"

                    # Extract unique participants
                    participants = list({msg.get("_resolved_name", "Unknown User") for msg in full_thread if msg.get("user")})

                    # Generate summary
                    llm_result = await self._generate_summary(channel_name, full_thread, participants)

                    # Write memory file
                    self._write_memory(channel_id, channel_name, thread_ts, full_thread, llm_result)

                    # Update thread state
                    thread_state["message_count"] = current_count
                    thread_state["last_ts"] = current_last_ts

                    log.info("Slack thread processed: %s in #%s", thread_ts, channel_name)

                # Update high-water mark
                if messages:
                    newest_ts = max(msg.get("ts", "0") for msg in messages)
                    channel_state["high_water"] = newest_ts

                # Save state after each channel
                self._save_state(state)

                await asyncio.sleep(1)  # inter-channel delay

        self._save_state(state)
