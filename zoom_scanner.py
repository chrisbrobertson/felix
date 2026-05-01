import asyncio
import base64
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

from llm_routes import resolve
from usage_tracker import record_usage
from secrets import get_secret_or_env
from utils import load_config
from heartbeat import record_beat

log = logging.getLogger("zoom-scanner")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "zoom-scanner-state.json"

ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE = "https://api.zoom.us/v2"

MAX_MEETINGS_PER_CYCLE = 20
MAX_TRANSCRIPT_LINES = 50


class ZoomScanner:
    def __init__(self, role: str = "full"):
        self.role = role
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self.notification_callback = None  # Set by daemon.py for watchlist notifications
        self._ai_companion_disabled = False
        self._ai_companion_403_logged = False
        self._local_dir_warned = False

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        return load_config(CONFIG_PATH)

    def _scanner_config(self) -> dict:
        return self._load_config().get("zoom_scanner", {})

    # ── State ─────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {"processed_uuids": [], "processed_summaries": [], "processed_local": [], "last_poll": None}

    def _save_state(self, state: dict):
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(STATE_FILE))
        except Exception as e:
            log.warning("Failed to save zoom scanner state: %s", e)

    def _add_processed_uuid(self, state: dict, uuid: str):
        """Append uuid to processed list and immediately persist state."""
        uuids = state.setdefault("processed_uuids", [])
        if uuid not in uuids:
            uuids.append(uuid)
        # Cap at 10,000 entries — trim oldest
        if len(uuids) > 10_000:
            state["processed_uuids"] = uuids[-10_000:]
        self._save_state(state)

    def _add_processed_summary(self, state: dict, meeting_id: str):
        """Append meeting_id to processed summaries list and immediately persist state."""
        summaries = state.setdefault("processed_summaries", [])
        if meeting_id not in summaries:
            summaries.append(meeting_id)
        # Cap at 10,000 entries — trim oldest
        if len(summaries) > 10_000:
            state["processed_summaries"] = summaries[-10_000:]
        self._save_state(state)

    def _add_processed_local(self, state: dict, folder_hash: str):
        """Append folder_hash to processed local list and immediately persist state."""
        local = state.setdefault("processed_local", [])
        if folder_hash not in local:
            local.append(folder_hash)
        # Cap at 10,000 entries — trim oldest
        if len(local) > 10_000:
            state["processed_local"] = local[-10_000:]
        self._save_state(state)

    # ── OAuth ─────────────────────────────────────────────────────────────────

    def _get_credentials(self) -> tuple:
        account_id = get_secret_or_env("zoom_account_id", "ZOOM_ACCOUNT_ID") or ""
        client_id = get_secret_or_env("zoom_client_id", "ZOOM_CLIENT_ID") or ""
        client_secret = get_secret_or_env("zoom_client_secret", "ZOOM_CLIENT_SECRET") or ""
        return account_id, client_id, client_secret

    async def _acquire_token(self) -> Optional[str]:
        account_id, client_id, client_secret = self._get_credentials()
        if not all([account_id, client_id, client_secret]):
            log.warning(
                "Missing Zoom credentials (ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, "
                "ZOOM_CLIENT_SECRET). Zoom scanner will be skipped."
            )
            return None

        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    ZOOM_TOKEN_URL,
                    headers={"Authorization": f"Basic {creds}"},
                    data={
                        "grant_type": "account_credentials",
                        "account_id": account_id,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                self._token = token
                # Refresh 5 minutes before expiry
                self._token_expiry = time.monotonic() + expires_in - 300
                return token
        except Exception as e:
            log.warning("Failed to acquire Zoom token: %s", e)
            return None

    async def _get_token(self) -> Optional[str]:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        return await self._acquire_token()

    # ── API helpers ───────────────────────────────────────────────────────────

    async def _api_get(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict = None,
        _retry: int = 0,
    ) -> Optional[dict]:
        token = await self._get_token()
        if not token:
            return None
        try:
            resp = await client.get(
                f"{ZOOM_API_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                log.warning("Zoom API rate limited — waiting %ds (path=%s)", retry_after, path)
                if _retry < 1:
                    await asyncio.sleep(retry_after)
                    return await self._api_get(client, path, params, _retry + 1)
                log.warning("Persistent rate limit on %s — skipping", path)
                return None
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            log.warning("Zoom API HTTP error %s: %s", e.response.status_code, path)
            return None
        except Exception as e:
            log.warning("Zoom API request failed (%s): %s", path, e)
            return None

    # ── AI Companion ──────────────────────────────────────────────────────────────

    async def _list_meeting_summaries(self, client: httpx.AsyncClient, since: datetime) -> Optional[list]:
        """Returns list of summary metadata dicts, None on 403 (permanent), or [] on transient error."""
        token = await self._get_token()
        if not token:
            return []

        results = []
        from_date = since.strftime("%Y-%m-%d")
        to_date = datetime.now().strftime("%Y-%m-%d")
        next_page_token = None

        while True:
            params = {"from": from_date, "to": to_date, "page_size": 100}
            if next_page_token:
                params["next_page_token"] = next_page_token

            try:
                resp = await client.get(
                    f"{ZOOM_API_BASE}/meetings/meeting_summaries",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                if resp.status_code == 403:
                    return None
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    log.warning("Zoom API rate limited on summaries — waiting %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code == 404:
                    break
                if resp.status_code == 400:
                    body = resp.text[:200]
                    log.warning("Zoom AI Companion API returned 400 — %s (from=%s to=%s)",
                                body, from_date, to_date)
                    return []
                resp.raise_for_status()
                data = resp.json()
                results.extend(data.get("summaries", []))
                next_page_token = data.get("next_page_token")
                if not next_page_token:
                    break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 403:
                    return None
                log.warning("Zoom API HTTP error %s: /meetings/meeting_summaries", e.response.status_code)
                return []
            except Exception as e:
                log.warning("Zoom API request failed (/meetings/meeting_summaries): %s", e)
                return []

        return results

    async def _get_meeting_summary(self, client: httpx.AsyncClient, meeting_id: int) -> Optional[dict]:
        """Fetch individual summary using numeric meeting_id."""
        return await self._api_get(client, f"/meetings/{meeting_id}/meeting_summary")

    def _parse_summary_content(self, content: str) -> dict:
        """Parse HTML or plain-text into {overview, action_items, next_steps}."""
        if not content or not content.strip():
            return {}

        # Normalize block HTML tags to newlines
        import html
        text = content
        # Decode HTML entities
        text = html.unescape(text)
        # Convert <li> to bullet points before stripping tags
        text = re.sub(r'<li[^>]*>', '\n- ', text)
        # Normalize block tags to newlines
        for tag in ['<br>', '<br/>', '<br />', '</p>', '</div>', '</li>', '</h1>',
                    '</h2>', '</h3>', '</h4>', '</h5>', '</h6>']:
            text = text.replace(tag, '\n')
        # Strip all remaining HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Clean up whitespace
        text = re.sub(r'\n\s*\n', '\n', text)
        text = text.strip()

        if not text:
            return {}

        # Split on section markers (case-insensitive, multiline)
        overview = ""
        action_items = []
        next_steps = []

        # Look for section markers
        overview_match = re.search(r'(?i)overview\s*[:\n]', text)
        action_match = re.search(r'(?i)action\s+items?\s*[:\n]', text)
        next_match = re.search(r'(?i)next\s+steps?\s*[:\n]', text)

        if overview_match:
            start = overview_match.end()
            end = action_match.start() if action_match else (next_match.start() if next_match else len(text))
            overview = text[start:end].strip()
        elif action_match or next_match:
            # No overview marker but other sections exist — treat everything before first section as overview
            end = action_match.start() if action_match else next_match.start()
            overview = text[:end].strip()
        else:
            # No markers at all — entire content is overview
            overview = text

        if action_match:
            start = action_match.end()
            end = next_match.start() if next_match else len(text)
            action_text = text[start:end].strip()
            # Extract bullet items
            for line in action_text.splitlines():
                line = line.strip()
                # Match lines starting with -, •, *, or digits followed by . or ), or just non-empty lines
                if re.match(r'^[-•*]|^\d+[\.)]\s+', line):
                    # Strip bullet/number prefix
                    item = re.sub(r'^[-•*]|^\d+[\.)]\s+', '', line).strip()
                    if item:
                        action_items.append(item)
                elif line and not re.match(r'(?i)^(overview|action|next)', line):
                    # Non-bullet line that's not a section header
                    action_items.append(line)

        if next_match:
            start = next_match.end()
            next_text = text[start:].strip()
            # Extract bullet items
            for line in next_text.splitlines():
                line = line.strip()
                if re.match(r'^[-•*]|^\d+[\.)]\s+', line):
                    item = re.sub(r'^[-•*]|^\d+[\.)]\s+', '', line).strip()
                    if item:
                        next_steps.append(item)
                elif line and not re.match(r'(?i)^(overview|action|next)', line):
                    next_steps.append(line)

        return {
            "overview": overview,
            "action_items": action_items,
            "next_steps": next_steps,
        }

    async def _generate_tags(self, overview: str, topic: str) -> list:
        """LLM call to generate tags from AI Companion overview text."""
        prompt = (
            f"Generate 2-4 concise tags for this meeting.\n\n"
            f"Meeting: {topic}\n"
            f"Summary: {overview[:500]}\n\n"
            f"Return JSON only:\n"
            '{"tags": ["tag1", "tag2"]}'
        )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("summarize"),
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            if hasattr(resp, "usage") and resp.usage:
                record_usage(resolve("summarize"), resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            data = json.loads(text)
            tags = data.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            return [re.sub(r'[^a-z0-9-]', '-', t.lower()).strip('-') for t in tags if t]
        except Exception:
            log.warning("LLM tags generation failed for meeting: %s — using fallback", topic)
            return []

    def _write_ai_companion_memory(self, summary_data: dict, ai_parsed: dict, tags: list):
        """Write memory file for AI-Companion-only meeting (no VTT)."""
        meeting_id = str(summary_data.get("meeting_id", ""))
        topic = summary_data.get("meeting_topic", "Meeting")
        start_time = summary_data.get("meeting_start_time", "")
        end_time = summary_data.get("meeting_end_time", "")

        # Calculate duration in minutes
        duration = 0
        if start_time and end_time:
            try:
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                duration = int((end_dt - start_dt).total_seconds() / 60)
            except Exception:
                pass

        # Generate filename
        date = start_time[:10] if start_time else "0000-00-00"
        slug = self._slugify(topic)
        id_hash = hashlib.sha1(meeting_id.encode()).hexdigest()[:6]
        filename = f"meeting-{date}-{slug}-{id_hash}.md"
        memory_path = MEMORIES_DIR / filename
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        fm = {
            "source_title": topic,
            "summary": ai_parsed.get("overview", "")[:300],
            "tags": tags,
            "last_scanned": now,
            "source_url": f"zoom:{meeting_id}",
            "type": "meeting_transcript",
            "participants": [],
            "speakers": [],
            "duration_minutes": duration,
            "meeting_date": start_time,
            "zoom_meeting_id": meeting_id,
            "summary_source": "ai_companion",
        }
        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

        # Body sections
        overview_section = f"## Summary\n{ai_parsed.get('overview', '')}\n\n"

        action_items = ai_parsed.get("action_items", [])
        action_section = ""
        if action_items:
            action_section = "## Action Items\n" + "\n".join(f"- {item}" for item in action_items) + "\n\n"

        next_steps = ai_parsed.get("next_steps", [])
        next_section = ""
        if next_steps:
            next_section = "## Next Steps\n" + "\n".join(f"- {item}" for item in next_steps) + "\n"

        content = (
            f"---\n{frontmatter}---\n\n"
            f"{overview_section}"
            f"{action_section}"
            f"{next_section}"
        )

        tmp_path = memory_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(memory_path))
            log.debug("Wrote AI Companion memory %s", memory_path.name)

            # Check watchlists after successful write
            if self.notification_callback:
                from watchlist_checker import check_watchlists
                check_watchlists(memory_path, MEMORIES_DIR, self.notification_callback)

        except Exception:
            log.exception("Failed to write %s", memory_path)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Recordings poll ───────────────────────────────────────────────────────

    async def _poll_recordings(self, client: httpx.AsyncClient, since: datetime) -> list:
        """Return list of (uuid, meeting_dict, transcript_download_url) for new meetings."""
        results = []
        from_date = since.strftime("%Y-%m-%d")
        next_page_token = None

        while True:
            params = {"from": from_date, "page_size": 100}
            if next_page_token:
                params["next_page_token"] = next_page_token

            data = await self._api_get(client, "/users/me/recordings", params)
            if not data:
                break

            for meeting in data.get("meetings", []):
                uuid = meeting.get("uuid", "")
                for f in meeting.get("recording_files", []):
                    if f.get("file_type") == "TRANSCRIPT" and f.get("status") == "completed":
                        results.append((uuid, meeting, f.get("download_url", "")))
                        break  # one transcript per meeting

            next_page_token = data.get("next_page_token")
            if not next_page_token:
                break

        return results

    # ── VTT parsing ───────────────────────────────────────────────────────────

    def _parse_vtt(self, vtt_text: str) -> dict:
        """Parse a Zoom VTT transcript into structured segments."""
        segments = []
        speakers = []
        seen_speakers: set = set()

        lines = vtt_text.splitlines()
        i = 0
        # Skip to (and past) WEBVTT header
        while i < len(lines) and "WEBVTT" not in lines[i]:
            i += 1
        if i < len(lines):
            i += 1  # skip WEBVTT line itself

        current: Optional[dict] = None

        while i < len(lines):
            line = lines[i].strip()
            i += 1

            if not line:
                # Empty line = end of segment
                if current and current.get("text"):
                    segments.append(current)
                current = None
                continue

            # Numeric index line (e.g. "1", "2")
            if re.match(r'^\d+$', line):
                continue

            # Timestamp line: HH:MM:SS.mmm --> HH:MM:SS.mmm
            ts_match = re.match(
                r'(\d{2}:\d{2}:\d{2})[.,]\d+ --> (\d{2}:\d{2}:\d{2})[.,]\d+',
                line
            )
            if ts_match:
                if current and current.get("text"):
                    segments.append(current)
                current = {
                    "index": len(segments) + 1,
                    "start_time": ts_match.group(1),
                    "speaker": None,
                    "text": "",
                }
                continue

            # Content line
            if current is not None:
                speaker_match = re.match(r'^(.+?):\s+(.+)$', line)
                if speaker_match and current["text"] == "":
                    # First content line with speaker prefix
                    speaker = speaker_match.group(1).strip()
                    text = speaker_match.group(2).strip()
                    current["speaker"] = speaker
                    current["text"] = text
                    if speaker not in seen_speakers:
                        seen_speakers.add(speaker)
                        speakers.append(speaker)
                else:
                    # Continuation line or speakerless content
                    if current["text"]:
                        current["text"] += " " + line
                    else:
                        current["text"] = line

        # Flush last segment
        if current and current.get("text"):
            segments.append(current)

        # Duration from last segment start time
        duration_ms = 0
        if segments:
            ts = segments[-1]["start_time"]
            parts = ts.split(":")
            if len(parts) == 3:
                h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                duration_ms = (h * 3600 + m * 60 + s) * 1000

        return {
            "segments": segments,
            "speakers": speakers,
            "raw_text": " ".join(seg["text"] for seg in segments if seg.get("text")),
            "duration_ms": duration_ms,
        }

    def _parse_timestamp_ms(self, ts: str) -> int:
        """Parse HH:MM:SS.mmm (or HH:MM:SS,mmm) → total milliseconds."""
        ts = ts.replace(",", ".")
        parts = ts.split(":")
        if len(parts) != 3:
            return 0
        try:
            h = int(parts[0])
            m = int(parts[1])
            s_parts = parts[2].split(".")
            s = int(s_parts[0])
            ms = int(s_parts[1]) if len(s_parts) > 1 else 0
            return (h * 3600 + m * 60 + s) * 1000 + ms
        except (ValueError, IndexError):
            return 0

    # ── Participant matching ───────────────────────────────────────────────────

    async def _get_participants(self, client: httpx.AsyncClient, meeting_uuid: str) -> list:
        import urllib.parse
        # Double-encode if UUID contains //
        if "//" in meeting_uuid:
            enc = urllib.parse.quote(urllib.parse.quote(meeting_uuid, safe=""), safe="")
        else:
            enc = urllib.parse.quote(meeting_uuid, safe="")
        data = await self._api_get(client, f"/past_meetings/{enc}/participants")
        if not data:
            return []
        return data.get("participants", [])

    def _match_speakers(self, speakers: list, participants: list) -> list:
        """Match VTT speaker names to participant email addresses."""
        exact: dict = {}   # name.lower() → email
        first: dict = {}   # first_name.lower() → email

        for p in participants:
            name = (p.get("name") or "").strip()
            email = p.get("user_email") or None
            if not name:
                continue
            exact[name.lower()] = email
            fname = name.split()[0].lower()
            if fname not in first:
                first[fname] = email

        result = []
        for speaker in speakers:
            lower = speaker.lower()
            if lower in exact:
                result.append({"name": speaker, "email": exact[lower], "confidence": 1.0})
            elif lower.split()[0] in first:
                fname = lower.split()[0]
                result.append({"name": speaker, "email": first[fname], "confidence": 0.7})
            else:
                result.append({"name": speaker, "email": None, "confidence": 0.0})
        return result

    # ── LLM summary ───────────────────────────────────────────────────────────

    async def _generate_summary(self, meeting: dict, parsed: dict, matched_speakers: list) -> dict:
        topic = meeting.get("topic", "Meeting")
        start_time = meeting.get("start_time", "")
        duration = meeting.get("duration", 0)

        speaker_list = ", ".join(
            f"{s['name']} ({s['email']})" if s.get("email") else s["name"]
            for s in matched_speakers
        ) or "unknown"

        # Sample up to 20 lines evenly
        segments = parsed.get("segments", [])
        if len(segments) > 20:
            step = max(1, len(segments) // 20)
            sample = segments[::step][:20]
        else:
            sample = segments

        excerpt_lines = []
        for seg in sample:
            if seg.get("speaker"):
                excerpt_lines.append(f"{seg['start_time']} {seg['speaker']}: {seg['text']}")
            else:
                excerpt_lines.append(f"{seg['start_time']} {seg['text']}")

        # First 100 words of raw text
        first_words = " ".join(parsed.get("raw_text", "").split()[:100])

        prompt = (
            f"Summarize this Zoom meeting for a personal knowledge base.\n\n"
            f"Meeting: {topic}\n"
            f"Date: {start_time[:10] if start_time else 'unknown'}\n"
            f"Duration: {duration} minutes\n"
            f"Speakers: {speaker_list}\n\n"
            f"Transcript excerpt:\n{first_words}\n\n"
            + "\n".join(excerpt_lines)
            + "\n\nReturn JSON only:\n"
            '{\n'
            '  "summary": "2-3 sentence summary of key topics and outcomes",\n'
            '  "tags": ["tag1", "tag2"],\n'
            '  "key_decisions": ["decision 1"]\n'
            '}'
        )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("summarize"),
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            if hasattr(resp, "usage") and resp.usage:
                record_usage(resolve("summarize"), resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("LLM returned non-JSON summary for meeting: %s — using fallback", topic)
            return {"summary": f"Meeting: {topic}", "tags": [], "key_decisions": []}
        except Exception:
            log.warning("LLM summary call failed for meeting: %s — using fallback", topic)
            return {"summary": f"Meeting: {topic}", "tags": [], "key_decisions": []}

    # ── Memory file write ─────────────────────────────────────────────────────

    def _slugify(self, text: str, max_len: int = 40) -> str:
        s = text.lower()
        s = re.sub(r'[^a-z0-9]+', '-', s)
        s = s.strip('-')
        return s[:max_len].rstrip('-')

    def _meeting_filename(self, meeting: dict) -> str:
        date = (meeting.get("start_time") or "")[:10] or "0000-00-00"
        topic = meeting.get("topic", "meeting")
        slug = self._slugify(topic)
        meeting_id = str(meeting.get("id", ""))
        id_hash = hashlib.sha1(meeting_id.encode()).hexdigest()[:6]
        return f"meeting-{date}-{slug}-{id_hash}.md"

    def _write_memory(
        self,
        meeting: dict,
        parsed: dict,
        matched_speakers: list,
        llm_result: dict,
        summary_source: str = "llm",
        action_items: Optional[list] = None,
        source_url_override: Optional[str] = None,
        filename_override: Optional[str] = None,
    ):
        filename = filename_override or self._meeting_filename(meeting)
        memory_path = MEMORIES_DIR / filename
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        uuid = meeting.get("uuid", "")
        start_time = meeting.get("start_time", "")
        duration = meeting.get("duration", 0)
        meeting_id = str(meeting.get("id", ""))

        participants = [s["email"] for s in matched_speakers if s.get("email")]
        speakers = [s["name"] for s in matched_speakers]

        tags = llm_result.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        tags = [re.sub(r'[^a-z0-9-]', '-', t.lower()).strip('-') for t in tags if t]

        source_url = source_url_override or f"zoom:{uuid}"

        fm = {
            "source_title": meeting.get("topic", "Meeting"),
            "summary": llm_result.get("summary", ""),
            "tags": tags,
            "last_scanned": now,
            "source_url": source_url,
            "type": "meeting_transcript",
            "participants": participants,
            "speakers": speakers,
            "duration_minutes": duration,
            "meeting_date": start_time,
            "summary_source": summary_source,
        }
        # Only add zoom_meeting_id if not a local recording
        if not source_url.startswith("local:"):
            fm["zoom_meeting_id"] = meeting_id

        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

        # Transcript section — cap at MAX_TRANSCRIPT_LINES
        segments = parsed.get("segments", [])
        transcript_lines = []
        for seg in segments[:MAX_TRANSCRIPT_LINES]:
            if seg.get("speaker"):
                transcript_lines.append(
                    f"- {seg['start_time']} {seg['speaker']}: {seg['text']}"
                )
            else:
                transcript_lines.append(f"- {seg['start_time']} {seg['text']}")
        if len(segments) > MAX_TRANSCRIPT_LINES:
            extra = len(segments) - MAX_TRANSCRIPT_LINES
            transcript_lines.append(f"(... {extra} more lines)")

        transcript_section = "\n".join(transcript_lines) or "(no transcript)"

        # Build body sections based on summary source
        if summary_source == "ai_companion" and action_items:
            # AI Companion merged format: Transcript + Summary + Action Items
            action_section = "\n## Action Items\n" + "\n".join(f"- {item}" for item in action_items) + "\n"
            content = (
                f"---\n{frontmatter}---\n\n"
                f"## Transcript\n{transcript_section}\n\n"
                f"## Summary\n{llm_result.get('summary', '')}\n"
                f"{action_section}"
            )
        else:
            # LLM format: Transcript + Summary + Key Decisions
            decisions = llm_result.get("key_decisions", [])
            decisions_section = ""
            if decisions:
                decisions_section = (
                    "\n## Key Decisions\n"
                    + "\n".join(f"- {d}" for d in decisions)
                )
            content = (
                f"---\n{frontmatter}---\n\n"
                f"## Transcript\n{transcript_section}\n\n"
                f"## Summary\n{llm_result.get('summary', '')}\n"
                f"{decisions_section}\n"
            )

        tmp_path = memory_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(memory_path))
            log.debug("Wrote %s", memory_path.name)

            # Check watchlists after successful write
            if self.notification_callback:
                from watchlist_checker import check_watchlists
                check_watchlists(memory_path, MEMORIES_DIR, self.notification_callback)

        except Exception:
            log.exception("Failed to write %s", memory_path)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Transcript download ───────────────────────────────────────────────────

    async def _download_transcript(self, download_url: str) -> Optional[str]:
        token = await self._get_token()
        if not token:
            return None
        try:
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(
                    download_url,
                    headers=headers,
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    log.warning("Rate limited on transcript download — waiting %ds", retry_after)
                    await asyncio.sleep(retry_after)
                    resp = await client.get(
                        download_url,
                        headers=headers,
                    )
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            log.warning("Failed to download transcript from %s: %s", download_url, e)
            return None

    # ── Local recordings ──────────────────────────────────────────────────────────

    def _folder_hash(self, folder_path: Path) -> str:
        """8-char SHA1 of folder absolute path."""
        return hashlib.sha1(str(folder_path.absolute()).encode()).hexdigest()[:8]

    def _parse_folder_name(self, folder_name: str) -> Optional[tuple]:
        """Parse 'YYYY-MM-DD HH.MM.SS Topic' -> (iso_datetime, topic) or None."""
        # Pattern: YYYY-MM-DD HH.MM.SS <optional topic>
        match = re.match(r'^(\d{4}-\d{2}-\d{2})\s+(\d{2})\.(\d{2})\.(\d{2})(?:\s+(.+))?$', folder_name)
        if not match:
            return None

        date_str = match.group(1)
        hour = match.group(2)
        minute = match.group(3)
        second = match.group(4)
        topic = match.group(5) or folder_name  # fallback to full folder name if no topic

        # Build ISO datetime
        iso_datetime = f"{date_str}T{hour}:{minute}:{second}"
        return (iso_datetime, topic.strip())

    async def _scan_local_recordings(self, state: dict):
        """Scan ~/Documents/Zoom/ for local recording folders. All roles."""
        sc = self._scanner_config()
        if not sc.get("local_recordings_enabled", False):
            return

        local_path_str = sc.get("local_recordings_path", "~/Documents/Zoom")
        local_path = Path(local_path_str).expanduser()

        if not local_path.exists():
            if not self._local_dir_warned:
                log.warning("Local recordings path does not exist: %s", local_path)
                self._local_dir_warned = True
            return

        if not local_path.is_dir():
            if not self._local_dir_warned:
                log.warning("Local recordings path is not a directory: %s", local_path)
                self._local_dir_warned = True
            return

        processed_local = set(state.setdefault("processed_local", []))

        for folder in local_path.iterdir():
            if not folder.is_dir():
                continue

            # Parse folder name
            parsed = self._parse_folder_name(folder.name)
            if not parsed:
                continue

            meeting_date, source_title = parsed

            # Check for VTT file
            vtt_path = folder / "closed_caption.vtt"
            if not vtt_path.exists():
                log.debug("Local folder %s has no closed_caption.vtt — skipping", folder.name)
                continue

            # Check dedup
            folder_hash = self._folder_hash(folder)
            if folder_hash in processed_local:
                continue

            log.info("Processing local recording: %s", folder.name)

            try:
                # Parse VTT
                vtt_text = vtt_path.read_text(encoding="utf-8")
                parsed_vtt = self._parse_vtt(vtt_text)
                if not parsed_vtt.get("segments"):
                    log.debug("Empty VTT for local folder %s — marking processed", folder.name)
                    self._add_processed_local(state, folder_hash)
                    continue

                # Speakers from VTT, no participants
                matched_speakers = self._match_speakers(parsed_vtt.get("speakers", []), [])

                # Generate summary (LLM)
                duration_minutes = parsed_vtt.get("duration_ms", 0) // 60000
                meeting_dict = {
                    "topic": source_title,
                    "start_time": meeting_date,
                    "duration": duration_minutes,
                    "id": folder_hash,  # Use folder hash as pseudo-ID
                }
                llm_result = await self._generate_summary(meeting_dict, parsed_vtt, matched_speakers)

                # Write memory file
                date = meeting_date[:10]
                slug = self._slugify(source_title)
                filename = f"meeting-{date}-{slug}-{folder_hash[:6]}.md"
                source_url = f"local:{folder_hash}"

                self._write_memory(
                    meeting_dict,
                    parsed_vtt,
                    matched_speakers,
                    llm_result,
                    summary_source="llm",
                    source_url_override=source_url,
                    filename_override=filename,
                )

                self._add_processed_local(state, folder_hash)
                log.info("Local recording processed: %s", source_title)

            except Exception:
                log.exception("Error processing local folder %s", folder.name)

    # ── Run loop ──────────────────────────────────────────────────────────────

    async def run_loop(self, stop_event: asyncio.Event):
        sc = self._scanner_config()
        local_enabled = sc.get("local_recordings_enabled", False)

        if self.role != "full" and not local_enabled:
            log.debug("Zoom scanner skipped (role=%s, local_recordings_enabled=false)", self.role)
            return

        interval = sc.get("interval_seconds", 300)

        if self.role != "full":
            # watcher: only local recordings
            log.info("Zoom scanner started (watcher — local recordings only)")
            while not stop_event.is_set():
                beat_status, beat_error = "ok", None
                try:
                    state = self._load_state()
                    state.setdefault("processed_summaries", [])
                    state.setdefault("processed_local", [])
                    await self._scan_local_recordings(state)
                except Exception as exc:
                    log.exception("Uncaught error in zoom scanner (local) cycle")
                    beat_status, beat_error = "error", str(exc)
                record_beat("zoom_scanner", beat_status, beat_error)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
            return

        # full role: cloud + local
        log.info("Zoom scanner started — polling every %ds", interval)

        # Check credentials once at startup — exit gracefully if missing
        _, client_id, _ = self._get_credentials()
        if not client_id:
            if not local_enabled:
                log.warning("ZOOM_CLIENT_ID not set — Zoom scanner disabled.")
                return
            log.warning("ZOOM_CLIENT_ID not set — cloud scanning disabled, local only.")

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await self._run_scan()
            except Exception as exc:
                log.exception("Uncaught error in zoom scanner cycle")
                beat_status, beat_error = "error", str(exc)
            record_beat("zoom_scanner", beat_status, beat_error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def backfill(self, days: int) -> dict:
        """Reprocess Zoom meetings from the last N days (max 180). Returns dict with counts."""
        days = min(days, 180)
        state = self._load_state()

        # Clear processed_uuids in memory to force reprocessing
        state["processed_uuids"] = []
        state["last_poll"] = None

        since = datetime.now() - timedelta(days=days)

        processed = 0
        skipped = 0
        errors = 0

        async with httpx.AsyncClient(timeout=30) as client:
            recordings = await self._poll_recordings(client, since)

            if not recordings:
                self._save_state(state)
                return {
                    "processed": 0,
                    "skipped": 0,
                    "errors": 0,
                    "notes": f"No Zoom meetings found in last {days} days"
                }

            count = min(len(recordings), MAX_MEETINGS_PER_CYCLE * 3)  # Higher limit for backfill
            log.info("Backfill: processing %d Zoom meeting(s)", count)

            for uuid, meeting, download_url in recordings[:count]:
                try:
                    vtt_text = await self._download_transcript(download_url)
                    if vtt_text is None:
                        errors += 1
                        continue

                    parsed = self._parse_vtt(vtt_text)
                    if not parsed.get("segments"):
                        skipped += 1
                        self._add_processed_uuid(state, uuid)
                        continue

                    participants = await self._get_participants(client, uuid)
                    matched_speakers = self._match_speakers(
                        parsed.get("speakers", []), participants
                    )
                    llm_result = await self._generate_summary(
                        meeting, parsed, matched_speakers
                    )
                    self._write_memory(meeting, parsed, matched_speakers, llm_result)
                    self._add_processed_uuid(state, uuid)
                    processed += 1

                except Exception as e:
                    log.error(f"Backfill error processing Zoom meeting {uuid}: {e}")
                    errors += 1

        state["last_poll"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._save_state(state)

        return {
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "notes": f"Scanned {days} days of Zoom recordings"
        }

    async def _run_scan(self):
        sc = self._scanner_config()
        lookback_days = int(sc.get("initial_lookback_days", 30))
        interval = sc.get("interval_seconds", 300)
        ai_companion_enabled = sc.get("ai_companion_enabled", True)
        prefer_ai_summary = sc.get("prefer_ai_summary", True)

        state = self._load_state()
        state.setdefault("processed_summaries", [])
        state.setdefault("processed_local", [])
        last_poll = state.get("last_poll")

        if last_poll:
            # Subsequent run: look back 2x interval to avoid gaps
            since = datetime.now() - timedelta(seconds=interval * 2)
        else:
            # First run: full lookback
            since = datetime.now() - timedelta(days=lookback_days)

        state["last_poll"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        processed_uuids = set(state.get("processed_uuids", []))
        processed_summaries = set(state.get("processed_summaries", []))
        rate_limit_hits = 0

        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Poll cloud recordings
            recordings = await self._poll_recordings(client, since)

            # 2. Poll AI Companion summaries if enabled
            summaries_by_id = {}
            if ai_companion_enabled and not self._ai_companion_disabled:
                summaries = await self._list_meeting_summaries(client, since)
                if summaries is None:
                    # None means 403 — permanently disable for this session
                    if not self._ai_companion_403_logged:
                        log.warning(
                            "AI Companion API returned 403 — missing scopes or feature disabled. "
                            "Falling back to VTT-only processing."
                        )
                        self._ai_companion_403_logged = True
                    self._ai_companion_disabled = True
                elif summaries:
                    summaries_by_id = {str(s.get("meeting_id")): s for s in summaries}
                    log.debug("Found %d AI Companion summaries", len(summaries_by_id))

            new_recordings = [
                (uuid, meeting, url)
                for uuid, meeting, url in recordings
                if uuid not in processed_uuids
            ]

            if not new_recordings and not summaries_by_id:
                log.debug("No new Zoom meetings to process")
                self._save_state(state)
                await self._scan_local_recordings(state)
                return

            # 3. Pass 1 - Cloud recordings (merged or VTT-only)
            count = min(len(new_recordings), MAX_MEETINGS_PER_CYCLE)
            if new_recordings:
                log.info("Processing %d new Zoom recording(s)", count)

            for uuid, meeting, download_url in new_recordings[:MAX_MEETINGS_PER_CYCLE]:
                if rate_limit_hits >= 3:
                    log.warning(
                        "3+ rate limit hits this cycle — skipping remaining meetings"
                    )
                    break

                try:
                    meeting_id = str(meeting.get("id", ""))
                    ai_parsed = None
                    action_items = None
                    summary_source = "llm"

                    # Check for AI Companion summary
                    if prefer_ai_summary and meeting_id in summaries_by_id:
                        summary_data = await self._get_meeting_summary(client, int(meeting_id))
                        if summary_data and summary_data.get("summary_content"):
                            ai_parsed = self._parse_summary_content(summary_data["summary_content"])
                            if ai_parsed.get("overview"):
                                summary_source = "ai_companion"
                                action_items = ai_parsed.get("action_items", [])
                                log.debug("Using AI Companion summary for meeting %s", meeting_id)
                        # Mark as processed regardless of success
                        if meeting_id not in processed_summaries:
                            self._add_processed_summary(state, meeting_id)
                            processed_summaries.add(meeting_id)

                    vtt_text = await self._download_transcript(download_url)
                    if vtt_text is None:
                        rate_limit_hits += 1
                        log.warning(
                            "Could not download transcript for %s — skipping (not added to state)",
                            uuid,
                        )
                        continue

                    parsed = self._parse_vtt(vtt_text)
                    if not parsed.get("segments"):
                        log.debug("Empty transcript for %s — marking processed", uuid)
                        self._add_processed_uuid(state, uuid)
                        continue

                    participants = await self._get_participants(client, uuid)
                    matched_speakers = self._match_speakers(
                        parsed.get("speakers", []), participants
                    )

                    # Generate summary: use AI Companion or LLM
                    if summary_source == "ai_companion" and ai_parsed:
                        # Use AI Companion overview as summary, skip LLM call
                        llm_result = {
                            "summary": ai_parsed.get("overview", ""),
                            "tags": [],  # Will be empty for now; could generate tags if needed
                            "key_decisions": [],
                        }
                    else:
                        llm_result = await self._generate_summary(
                            meeting, parsed, matched_speakers
                        )

                    self._write_memory(
                        meeting, parsed, matched_speakers, llm_result,
                        summary_source=summary_source,
                        action_items=action_items,
                    )
                    self._add_processed_uuid(state, uuid)
                    log.info("Zoom meeting processed: %s", meeting.get("topic", uuid))

                except Exception:
                    log.exception("Error processing Zoom meeting %s", uuid)

            # 4. Pass 2 - AI Companion only (no VTT)
            if summaries_by_id:
                ai_only = [
                    (mid, s)
                    for mid, s in summaries_by_id.items()
                    if mid not in processed_summaries
                ]
                if ai_only:
                    log.info("Processing %d AI-Companion-only meeting(s)", len(ai_only))
                for meeting_id, summary_meta in ai_only[:MAX_MEETINGS_PER_CYCLE]:
                    try:
                        summary_data = await self._get_meeting_summary(client, int(meeting_id))
                        if not summary_data or not summary_data.get("summary_content"):
                            log.debug("No summary content for meeting %s — skipping", meeting_id)
                            self._add_processed_summary(state, meeting_id)
                            continue

                        ai_parsed = self._parse_summary_content(summary_data["summary_content"])
                        if not ai_parsed.get("overview"):
                            log.debug("Empty overview for meeting %s — skipping", meeting_id)
                            self._add_processed_summary(state, meeting_id)
                            continue

                        # Generate tags
                        topic = summary_data.get("meeting_topic", "Meeting")
                        tags = await self._generate_tags(ai_parsed["overview"], topic)

                        self._write_ai_companion_memory(summary_data, ai_parsed, tags)
                        self._add_processed_summary(state, meeting_id)
                        log.info("AI Companion meeting processed: %s", topic)

                    except Exception:
                        log.exception("Error processing AI Companion meeting %s", meeting_id)

        self._save_state(state)
        await self._scan_local_recordings(state)
