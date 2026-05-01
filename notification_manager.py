import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
from zoneinfo import ZoneInfo

from utils import read_text_with_retry_async
from heartbeat import record_beat

log = logging.getLogger("notification-manager")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "notification-state.json"
QUOTA_STATE_FILE = DEPLOY_DIR / "quota-scanner-state.json"

TG_MAX_CHARS = 4000  # Chunk at 4000 to leave margin


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter from a memory file. Returns {} on any failure."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _safe_frontmatter(raw: str) -> dict:
    """Parse a JSON frontmatter string from the memory cache. Returns {} on any failure
    or if the parsed value is not a dict (guards against JSON null, arrays, strings)."""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _load_state() -> dict:
    """Load notification state from JSON file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "chat_id": None,
        "muted": False,
        "last_briefing_date": None,
        "sent_commitment_alerts": [],
        "sent_pre_meeting": [],
        "sent_goal_alerts": [],
        "sent_project_alerts": [],
        "sent_calendar_staleness_alerts": [],
        "sent_commitment_checkpoints": [],
    }


def _save_state(state: dict):
    """Atomically save notification state. Raises on failure — callers must handle."""
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        os.rename(str(tmp), str(STATE_FILE))
    except Exception as e:
        log.error("Failed to save notification state: %s", e)
        raise


def _read_quota_state() -> dict:
    """Read quota-scanner-state.json directly (no scanner instance coupling)."""
    if QUOTA_STATE_FILE.exists():
        try:
            return json.loads(QUOTA_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _chunk_message(text: str, max_len: int = TG_MAX_CHARS) -> list:
    """Split long message into chunks at paragraph or line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to split at paragraph boundary
        chunk = remaining[:max_len]
        split_pos = chunk.rfind("\n\n")
        if split_pos == -1:
            # Try single newline
            split_pos = chunk.rfind("\n")
        if split_pos == -1:
            # Try space
            split_pos = chunk.rfind(" ")
        if split_pos == -1:
            # Hard cut
            split_pos = max_len

        chunks.append(remaining[:split_pos].rstrip())
        remaining = remaining[split_pos:].lstrip()

    return chunks


# ── NotificationManager ───────────────────────────────────────────────────────

class NotificationManager:
    def __init__(self, bot=None, deploy_dir: Path = DEPLOY_DIR, transports: Optional[list] = None, cache=None):
        self.bot = bot
        self.deploy_dir = deploy_dir
        self._state_cache = None
        # Optional list of TransportAdapter instances for multi-transport dispatch.
        # When set, send_message delivers to each transport instead of (or in addition to)
        # the legacy self.bot. Phase 4 will make this the primary path.
        self._transports: list = transports or []
        self._cache = cache  # MemoryCache instance for queries

    def _load_config(self) -> dict:
        """Read config.yaml via the shared iCloud-resilient loader in utils."""
        from utils import load_config
        return load_config(CONFIG_PATH)

    @property
    def _config(self) -> dict:
        """Cached config property for internal use."""
        return self._load_config()

    def _notification_config(self) -> dict:
        return self._load_config().get("notifications", {})

    def _user_config(self) -> dict:
        return self._load_config().get("user", {})

    def get_chat_id(self) -> Optional[int]:
        """Get chat_id from config override or persisted state."""
        config = self._notification_config()
        override = config.get("telegram_chat_id")
        if override is not None:
            return int(override)

        state = _load_state()
        chat_id = state.get("chat_id")
        return int(chat_id) if chat_id is not None else None

    def set_chat_id(self, chat_id: int):
        """Persist chat_id to state file."""
        state = _load_state()
        state["chat_id"] = chat_id
        _save_state(state)

    async def send_message(self, text: str, chat_id: Optional[int] = None):
        """Send message to all active transports, chunking if needed.

        When self._transports is populated (Phase 4+), each TransportAdapter's
        send_text() is called.  Falls back to the legacy self.bot path so
        existing callers continue to work unchanged.
        """
        if self._transports:
            any_sent = False
            last_error: Optional[Exception] = None
            for adapter in self._transports:
                try:
                    adapter_chat_id = getattr(adapter, "get_chat_id", lambda: None)()
                    if adapter_chat_id is None:
                        log.debug("send_message: adapter %s has no chat_id — skipping", adapter)
                        continue
                    max_len = adapter.max_message_length()
                    chunks = _chunk_message(text, max_len)
                    for chunk in chunks:
                        await adapter.send_text(adapter_chat_id, chunk)
                    any_sent = True
                except Exception as e:
                    log.warning("send_message: error sending via %s: %s", adapter, e)
                    last_error = e
            if any_sent:
                return
            if last_error is not None:
                raise last_error
            # else: fall through to legacy self.bot path (no transports had a chat_id)

        # Legacy path — direct telegram.Bot
        if self.bot is None:
            log.debug("Bot is None — skipping send")
            return

        if chat_id is None:
            chat_id = self.get_chat_id()

        if chat_id is None:
            log.debug("No chat_id available — skipping send")
            return

        chunks = _chunk_message(text)
        for chunk in chunks:
            await self.bot.send_message(chat_id=chat_id, text=chunk)

    def _get_local_now(self) -> datetime:
        """Get current datetime in user's configured timezone."""
        tz_name = self._user_config().get("timezone", "America/Los_Angeles")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            log.warning("Invalid timezone %s, falling back to UTC", tz_name)
            tz = ZoneInfo("UTC")
        return datetime.now(tz)

    async def _prune_sent_alerts(self, state: dict):
        """Remove stale entries from sent_commitment_alerts and sent_pre_meeting."""
        now = self._get_local_now()
        today = now.date()

        # Prune commitment alerts: remove if due_date is > 1 day in the past
        pruned_commitments = []
        for commitment_id in state.get("sent_commitment_alerts", []):
            # Find the commitment file
            commitment_files = list(MEMORIES_DIR.glob(f"commitment-*-{commitment_id}.md"))
            if not commitment_files:
                continue  # File deleted — discard

            fm = _parse_frontmatter(await read_text_with_retry_async(commitment_files[0]))
            due_date_str = fm.get("due_date")
            if not due_date_str:
                pruned_commitments.append(commitment_id)
                continue

            try:
                due_date = datetime.fromisoformat(due_date_str).date()
                if (today - due_date).days <= 1:
                    pruned_commitments.append(commitment_id)
            except Exception:
                pruned_commitments.append(commitment_id)

        state["sent_commitment_alerts"] = pruned_commitments

        # Prune pre-meeting alerts: remove entries whose event is now in the past.
        # Keys may be source_url ("calendar:…", new format) or filename stem
        # ("calendar-event-…", legacy). Build a start_time lookup covering both
        # key styles from the cache so we avoid one iCloud read per entry.
        cal_entries = await self._cache.query_by_prefix("calendar-event-")
        event_times: dict[str, datetime] = {}
        for e in cal_entries:
            try:
                fm_e = json.loads(e["frontmatter"])
                start_str = fm_e.get("start_time")
                if not start_str:
                    continue
                t = datetime.fromisoformat(start_str)
                if fm_e.get("source_url"):
                    event_times.setdefault(fm_e["source_url"], t)
                event_times.setdefault(Path(e["filename"]).stem, t)
            except Exception:
                continue

        pruned_meetings = []
        for event_key in state.get("sent_pre_meeting", []):
            t = event_times.get(event_key)
            if t is not None and t > now:
                pruned_meetings.append(event_key)

        state["sent_pre_meeting"] = pruned_meetings

        # Prune calendar staleness alerts older than 7 days — keep the list
        # bounded without losing same-day dedup.
        pruned_cal = []
        cutoff = today - timedelta(days=7)
        for date_str in state.get("sent_calendar_staleness_alerts", []):
            try:
                d = datetime.fromisoformat(date_str).date()
            except Exception:
                continue
            if d >= cutoff:
                pruned_cal.append(date_str)
        state["sent_calendar_staleness_alerts"] = pruned_cal

        # Prune commitment day-checkpoint keys older than 1 day (format: YYYY-MM-DD:slot).
        pruned_ckpt = []
        for key in state.get("sent_commitment_checkpoints", []):
            try:
                date_part = key.split(":")[0]
                d = datetime.fromisoformat(date_part).date()
                if d >= today - timedelta(days=1):
                    pruned_ckpt.append(key)
            except Exception:
                continue
        state["sent_commitment_checkpoints"] = pruned_ckpt

    async def _check_daily_briefing(self, state: dict):
        """Check if daily briefing should be sent."""
        config = self._notification_config()
        if not config.get("enabled", True):
            return

        now = self._get_local_now()
        today_str = now.strftime("%Y-%m-%d")
        briefing_time_str = config.get("briefing_time", "07:30")

        # Parse briefing time
        try:
            hour, minute = map(int, briefing_time_str.split(":"))
            briefing_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except Exception:
            log.warning("Invalid briefing_time %s, using 07:30", briefing_time_str)
            briefing_time = now.replace(hour=7, minute=30, second=0, microsecond=0)

        # Check if we should send
        last_date = state.get("last_briefing_date")
        if last_date == today_str:
            return  # Already sent today

        if now < briefing_time:
            return  # Too early

        # Commit the attempt before the network call so a crash mid-send
        # doesn't re-send the briefing on the next restart (send-before-save
        # was the root cause of duplicate briefings from daemon crash loops).
        briefing_text = await self._assemble_briefing()
        prev_date = state.get("last_briefing_date")
        state["last_briefing_date"] = today_str
        _save_state(state)
        try:
            await self.send_message(briefing_text)
            log.info("Daily briefing sent")
        except Exception:
            log.exception("Briefing send failed — rolling back last_briefing_date")
            state["last_briefing_date"] = prev_date
            _save_state(state)
            raise

    async def _assemble_briefing(self) -> str:
        """Assemble daily briefing content from memory files."""
        now = self._get_local_now()
        today = now.date()
        today_str = today.strftime("%Y-%m-%d")
        yesterday = today - timedelta(days=1)

        # Format header
        weekday = now.strftime("%A")
        date_str = now.strftime("%B %d")
        lines = [f"Good morning. Here's your briefing for {weekday}, {date_str}:"]

        # Calendar events for today
        calendar_events = []
        entries = await self._cache.query_by_prefix("calendar-event-")
        for entry in entries:
            try:
                fm = _safe_frontmatter(entry["frontmatter"])
            except (json.JSONDecodeError, TypeError):
                log.warning("briefing: malformed frontmatter in %s — skipping", entry["filename"])
                continue
            if fm.get("type") != "calendar_event":
                continue
            start_time_str = fm.get("start_time")
            if not start_time_str:
                continue
            try:
                start_time = datetime.fromisoformat(start_time_str)
                if start_time.date() == today:
                    calendar_events.append((start_time, fm))
            except Exception:
                continue

        if calendar_events:
            calendar_events.sort(key=lambda x: x[0])
            lines.append(f"\nCalendar ({len(calendar_events)} events):")
            for start_time, fm in calendar_events:
                title = fm.get("source_title") or fm.get("title") or "(no title)"
                time_str = start_time.strftime("%I:%M %p").lstrip("0")
                participants = fm.get("participants") or []
                participant_str = ", ".join(participants[:3]) if participants else ""
                if participant_str:
                    lines.append(f"• {time_str} — {title} ({participant_str})")
                else:
                    lines.append(f"• {time_str} — {title}")

        # Commitments due today and overdue
        due_today = []
        overdue = []
        entries = await self._cache.query_by_type("commitment", status="active")
        for entry in entries:
            try:
                fm = _safe_frontmatter(entry["frontmatter"])
            except (json.JSONDecodeError, TypeError):
                log.warning("briefing: malformed frontmatter in %s — skipping", entry["filename"])
                continue
            due_date_str = fm.get("due_date")
            if not due_date_str:
                continue
            try:
                due_date = datetime.fromisoformat(str(due_date_str)).date()
                if due_date == today:
                    due_today.append(fm)
                elif due_date < today:
                    overdue.append(fm)
            except Exception:
                log.warning(
                    "briefing: unparseable due_date %r in %s — skipping",
                    due_date_str,
                    fm.get("source_title", "?"),
                )
                continue

        if due_today:
            lines.append(f"\nCommitments due today ({len(due_today)}):")
            for fm in due_today:
                ct = fm.get("commitment_type", "outbound")
                desc = (
                    fm.get("source_title")
                    or fm.get("summary")
                    or "(untitled commitment)"
                )[:60]
                owner = fm.get("owner", "")
                recipient = fm.get("recipient", "")
                target = recipient if ct == "outbound" else (owner if ct != "personal" else "")
                if target:
                    lines.append(f"• [{ct}] {desc} → {target}")
                else:
                    lines.append(f"• [{ct}] {desc}")

        if overdue:
            lines.append(f"\nOverdue ({len(overdue)}):")
            for fm in overdue:
                ct = fm.get("commitment_type", "outbound")
                desc = (
                    fm.get("source_title")
                    or fm.get("summary")
                    or "(untitled commitment)"
                )[:60]
                owner = fm.get("owner", "")
                due_date_str = fm.get("due_date")
                lines.append(f"• [{ct}] {desc} — was due {due_date_str}")

        # Stale waiting-ons
        config = self._notification_config()
        stale_days = config.get("stale_waiting_on_days", 7)
        cutoff = now - timedelta(days=stale_days)
        stale_waiting = []
        entries = await self._cache.query_by_type("commitment", status="active")
        for entry in entries:
            try:
                fm = _safe_frontmatter(entry["frontmatter"])
            except (json.JSONDecodeError, TypeError):
                log.warning("briefing: malformed frontmatter in %s — skipping", entry["filename"])
                continue
            if fm.get("commitment_type") != "waiting_on":
                continue
            last_scanned_str = fm.get("last_scanned")
            if not last_scanned_str:
                continue
            try:
                last_scanned = datetime.fromisoformat(last_scanned_str)
                if last_scanned < cutoff:
                    stale_waiting.append(fm)
            except Exception:
                continue

        if stale_waiting:
            lines.append(f"\nStale waiting-ons ({len(stale_waiting)}):")
            for fm in stale_waiting[:5]:  # Limit to 5
                desc = (
                    fm.get("source_title")
                    or fm.get("summary")
                    or "(untitled commitment)"
                )[:60]
                owner = fm.get("owner", "")
                lines.append(f"• {desc} — from {owner}")

        # Agent-proposed actions (pending, last 24h)
        pending_actions = []
        cutoff_24h = now - timedelta(hours=24)
        entries = await self._cache.query_by_type("agent_action", status="pending")
        # Sort by mtime descending
        entries_sorted = sorted(entries, key=lambda e: e["mtime"], reverse=True)
        for entry in entries_sorted:
            try:
                fm = _safe_frontmatter(entry["frontmatter"])
                proposed_at_str = fm.get("proposed_at", "")
                if proposed_at_str:
                    proposed_at = datetime.fromisoformat(str(proposed_at_str))
                    # Make tz-aware if needed
                    if now.tzinfo is not None and proposed_at.tzinfo is None:
                        proposed_at = proposed_at.replace(tzinfo=now.tzinfo)
                    elif now.tzinfo is None and proposed_at.tzinfo is not None:
                        proposed_at = proposed_at.replace(tzinfo=None)
                    if proposed_at >= cutoff_24h:
                        pending_actions.append(fm)
            except Exception:
                pass

        if pending_actions:
            lines.append(f"\nAgent proposals ({len(pending_actions)}):")
            for fm in pending_actions[:10]:
                lines.append(f"• [{fm['action_type']}] {str(fm.get('rationale',''))[:80]}")
            lines.append("  → /actions to review, /run N to approve")

        # Goal/project agent updates (recent reports)
        goal_agent_state_file = DEPLOY_DIR / "goal-agent-state.json"
        if goal_agent_state_file.exists():
            try:
                agent_state = json.loads(goal_agent_state_file.read_text())
                updates = []
                for state_key in ["goals", "projects"]:
                    for item_name, item_state in agent_state.get(state_key, {}).items():
                        last_checked_str = item_state.get("last_checked")
                        last_report = item_state.get("last_report", "")
                        if not last_checked_str or not last_report:
                            continue
                        try:
                            last_checked = datetime.fromisoformat(last_checked_str)
                            # Make tz-aware
                            if now.tzinfo is not None and last_checked.tzinfo is None:
                                last_checked = last_checked.replace(tzinfo=now.tzinfo)
                            elif now.tzinfo is None and last_checked.tzinfo is not None:
                                last_checked = last_checked.replace(tzinfo=None)
                            if last_checked >= cutoff_24h:
                                updates.append((item_name, last_report))
                        except Exception:
                            continue

                if updates:
                    lines.append(f"\nGoal/project updates ({len(updates)}):")
                    for item_name, report in updates[:5]:
                        lines.append(f"• {item_name}: {report[:80]}")
            except Exception:
                pass

        # Active projects snapshot
        try:
            project_entries = await self._cache.query_by_type("project", status="active")
            active_projects = []
            new_threshold = now - timedelta(days=7)
            for entry in project_entries:
                if "project-candidate-" in entry["filename"]:
                    continue
                try:
                    fm = _safe_frontmatter(entry["frontmatter"])
                    if fm.get("type") != "project":
                        continue
                    title = fm.get("source_title") or Path(entry["filename"]).stem
                    due_raw = fm.get("due_date")
                    due_str = str(due_raw) if due_raw else None
                    created_str = fm.get("created", "")
                    is_new = False
                    if created_str:
                        try:
                            created_dt = datetime.fromisoformat(created_str)
                            if now.tzinfo is not None and created_dt.tzinfo is None:
                                created_dt = created_dt.replace(tzinfo=now.tzinfo)
                            elif now.tzinfo is None and created_dt.tzinfo is not None:
                                created_dt = created_dt.replace(tzinfo=None)
                            is_new = created_dt >= new_threshold
                        except Exception:
                            pass
                    milestones = fm.get("milestones") or []
                    total_ms = len(milestones)
                    done_ms = sum(1 for m in milestones if m.get("done"))
                    active_projects.append({
                        "title": title,
                        "due_str": due_str,
                        "is_new": is_new,
                        "total_ms": total_ms,
                        "done_ms": done_ms,
                    })
                except Exception:
                    continue

            # Sort by due_date ascending, nulls last
            active_projects.sort(key=lambda p: p["due_str"] or "9999-12-31")

            if active_projects:
                lines.append(f"\nActive projects ({len(active_projects)}):")
                for proj in active_projects[:10]:
                    label = "[new] " if proj["is_new"] else ""
                    due_part = f" — due {proj['due_str']}" if proj["due_str"] else ""
                    ms_part = (
                        f" ({proj['done_ms']}/{proj['total_ms']} done)"
                        if proj["total_ms"] > 0
                        else ""
                    )
                    lines.append(f"• {label}{proj['title']}{due_part}{ms_part}")
        except Exception:
            log.exception("briefing: error building active projects section")

        # New memories since yesterday
        new_count = 0
        yesterday_midnight = datetime.combine(yesterday, datetime.min.time())
        yesterday_midnight = yesterday_midnight.replace(tzinfo=now.tzinfo)
        # Query cache for all memories, exclude transient types
        all_entries = []
        for type_ in ["webpage", "email_thread", "meeting_transcript", "slack_thread", "code", "goal", "project", "contact"]:
            all_entries.extend(await self._cache.query_by_type(type_))
        for entry in all_entries:
            try:
                mtime_dt = datetime.fromtimestamp(entry["mtime"], tz=now.tzinfo)
                if mtime_dt > yesterday_midnight:
                    new_count += 1
            except Exception:
                continue

        if new_count > 0:
            lines.append(f"\n{new_count} new memories captured since yesterday.")

        # Quota status (if enabled and recent data)
        quota_cfg = self._config.get("quota", {})
        if quota_cfg.get("briefing_enabled", True):
            quota_state = _read_quota_state()
            recent_quota = {}
            cutoff_24h = now - timedelta(hours=24)
            for platform, state_data in quota_state.items():
                last_seen_str = state_data.get("last_seen_at", "")
                if last_seen_str:
                    try:
                        last_seen = datetime.fromisoformat(last_seen_str)
                        # Make tz-aware
                        if now.tzinfo is not None and last_seen.tzinfo is None:
                            last_seen = last_seen.replace(tzinfo=now.tzinfo)
                        elif now.tzinfo is None and last_seen.tzinfo is not None:
                            last_seen = last_seen.replace(tzinfo=None)
                        if last_seen >= cutoff_24h:
                            recent_quota[platform] = state_data
                    except Exception:
                        pass

            if recent_quota:
                from quota_scanner import render_one
                lines.append("\nQuotas:")
                for platform in ["claude", "chatgpt"]:
                    if platform in recent_quota:
                        lines.append(f"• {render_one(platform, quota_state)}")

        return "\n".join(lines)

    async def _check_commitment_alerts(self, state: dict):
        """Check for commitment deadline alerts."""
        config = self._notification_config()
        if not config.get("enabled", True):
            return

        now = self._get_local_now()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        sent_alerts = set(state.get("sent_commitment_alerts", []))

        entries = await self._cache.query_by_type("commitment", status="active")
        for entry in entries:
            try:
                fm = _safe_frontmatter(entry["frontmatter"])
            except (json.JSONDecodeError, TypeError):
                log.warning("commitment_alerts: malformed frontmatter in %s — skipping", entry["filename"])
                continue

            due_date_str = fm.get("due_date")
            if not due_date_str:
                continue

            try:
                due_date = datetime.fromisoformat(due_date_str).date()
            except Exception:
                continue

            # Extract commitment ID from filename
            match = re.search(r'-([a-f0-9]{12})\.md$', entry["filename"])
            if not match:
                continue
            commitment_id = match.group(1)

            if commitment_id in sent_alerts:
                continue  # Already sent

            if due_date == today:
                # Due today alert
                ct = fm.get("commitment_type", "outbound")
                desc = (
                    fm.get("source_title")
                    or fm.get("summary")
                    or "(untitled commitment)"
                )
                owner = fm.get("owner", "")
                recipient = fm.get("recipient", "")
                source_title = fm.get("source_memory", "").split(":", 1)[-1]

                target = recipient if ct == "outbound" else (owner if ct != "personal" else "")
                alert = f"Commitment due today:\n[{ct}] {desc}"
                if target:
                    alert += f" → {target}"
                alert += f"\nSource: {source_title}"

                await self.send_message(alert)
                sent_alerts.add(commitment_id)
                log.info("Sent due-today alert for commitment %s", commitment_id)

            elif due_date == tomorrow:
                # Due tomorrow alert
                ct = fm.get("commitment_type", "outbound")
                desc = (
                    fm.get("source_title")
                    or fm.get("summary")
                    or "(untitled commitment)"
                )
                owner = fm.get("owner", "")
                recipient = fm.get("recipient", "")
                source_title = fm.get("source_memory", "").split(":", 1)[-1]

                target = recipient if ct == "outbound" else (owner if ct != "personal" else "")
                alert = f"Reminder: commitment due tomorrow:\n[{ct}] {desc}"
                if target:
                    alert += f" → {target}"
                alert += f"\nSource: {source_title}"

                await self.send_message(alert)
                sent_alerts.add(commitment_id)
                log.info("Sent due-tomorrow alert for commitment %s", commitment_id)

        state["sent_commitment_alerts"] = list(sent_alerts)
        _save_state(state)

    async def _check_commitment_day_checkpoints(self, state: dict):
        """Send midday and end-of-day summary alerts for commitments still due today.

        Each slot fires at most once per calendar day. If the daemon was offline
        when the slot time passed, the alert fires on restart provided we are still
        within 2 hours of the slot time; beyond that the slot is silently skipped
        so stale reminders don't fire at midnight.
        """
        config = self._notification_config()
        if not config.get("enabled", True):
            return

        now = self._get_local_now()
        today = now.date()
        today_str = today.isoformat()

        midday_str = config.get("midday_alert_time", "12:00")
        eod_str = config.get("eod_alert_time", "17:00")

        checkpoints = []
        for slot, time_str in [("midday", midday_str), ("eod", eod_str)]:
            try:
                h, m = map(int, time_str.split(":"))
                slot_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            except Exception:
                log.warning("Invalid %s_alert_time %r — skipping", slot, time_str)
                continue
            checkpoints.append((slot, slot_time))

        sent = set(state.get("sent_commitment_checkpoints", []))

        for slot, slot_time in checkpoints:
            key = f"{today_str}:{slot}"
            if key in sent:
                continue  # Already sent today

            if now < slot_time:
                continue  # Not time yet

            # If the daemon was down through the slot window, skip the alert
            # rather than sending a stale reminder hours later.
            if now > slot_time + timedelta(hours=2):
                sent.add(key)
                continue

            # Collect active commitments due today
            due_today = []
            entries = await self._cache.query_by_type("commitment", status="active")
            for entry in entries:
                try:
                    fm = _safe_frontmatter(entry["frontmatter"])
                except (json.JSONDecodeError, TypeError):
                    continue
                due_date_str = fm.get("due_date")
                if not due_date_str:
                    continue
                try:
                    due_date = datetime.fromisoformat(str(due_date_str)).date()
                    if due_date == today:
                        due_today.append(fm)
                except Exception:
                    continue

            # Mark sent even when list is empty so we don't re-check each minute.
            sent.add(key)

            if not due_today:
                log.debug("Commitment %s checkpoint: nothing due today — skipping send", slot)
                continue

            label = "Midday check-in" if slot == "midday" else "End-of-day reminder"
            n = len(due_today)
            lines = [f"{label}: {n} commitment{'s' if n != 1 else ''} due today:"]
            for fm in due_today:
                ct = fm.get("commitment_type", "outbound")
                desc = (
                    fm.get("source_title")
                    or fm.get("summary")
                    or "(untitled commitment)"
                )[:60]
                recipient = fm.get("recipient", "")
                owner = fm.get("owner", "")
                target = recipient if ct == "outbound" else (owner if ct != "personal" else "")
                if target:
                    lines.append(f"• [{ct}] {desc} → {target}")
                else:
                    lines.append(f"• [{ct}] {desc}")

            if slot == "eod":
                lines.append("\nRun /commitments for current item numbers, then /complete N to mark any done.")

            # State-before-send dedup: persist the key before delivery so a
            # crash between send and the outer _check_and_send save cannot
            # replay an already-delivered checkpoint on restart.
            state["sent_commitment_checkpoints"] = list(sent)
            try:
                _save_state(state)
            except Exception:
                log.exception(
                    "Failed to save commitment checkpoint dedup; skipping %s send", slot
                )
                sent.discard(key)
                state["sent_commitment_checkpoints"] = list(sent)
                continue
            try:
                await self.send_message("\n".join(lines))
            except Exception:
                log.exception(
                    "Failed to send %s commitment checkpoint; rolling back dedup", slot
                )
                sent.discard(key)
                state["sent_commitment_checkpoints"] = list(sent)
                try:
                    _save_state(state)
                except Exception:
                    log.exception(
                        "Failed to roll back commitment checkpoint dedup for %s", slot
                    )
                continue
            log.info("Sent %s commitment checkpoint (%d due today)", slot, n)

        state["sent_commitment_checkpoints"] = list(sent)

    async def _check_goal_alerts(self, state: dict):
        """Fire 7-day and 1-day deadline alerts for active goals."""
        config = self._notification_config()
        if not config.get("enabled", True):
            return

        sent_alerts = set(state.get("sent_goal_alerts", []))
        horizons = self._config.get("goals", {}).get("deadline_horizons", [7, 1])

        now = self._get_local_now()
        today = now.date()

        entries = await self._cache.query_by_type("goal", status="active")
        for entry in entries:
            try:
                fm = _safe_frontmatter(entry["frontmatter"])

                due_raw = fm.get("due_date")
                if not due_raw:
                    continue

                try:
                    due = datetime.fromisoformat(str(due_raw)).date()
                except Exception:
                    continue

                # Stable ID from filename: extract the 6-char hex suffix
                goal_id = Path(entry["filename"]).stem.rsplit("-", 1)[-1]

                days_until = (due - today).days

                # Find the smallest horizon that matches and hasn't fired yet
                for horizon in sorted(horizons):
                    alert_key = f"goal:{goal_id}:{horizon}d"
                    if alert_key in sent_alerts:
                        continue

                    if 0 <= days_until <= horizon:
                        # Haven't fired yet, within horizon - fire and stop
                        msg = (
                            f"⏰ Goal deadline approaching: \"{fm.get('source_title', Path(entry['filename']).stem)}\" "
                            f"— due in {days_until} day{'s' if days_until != 1 else ''} ({due})"
                        )
                        await self.send_message(msg)
                        sent_alerts.add(alert_key)
                        log.info("Sent %d-day goal alert for %s", horizon, goal_id)
                        break  # Only fire one alert per goal per check
            except Exception:
                log.exception("Error checking goal alerts for %s", entry["filename"])

        state["sent_goal_alerts"] = list(sent_alerts)

    async def _check_project_alerts(self, state: dict):
        """Fire 7-day and 1-day deadline alerts for active and on-hold projects."""
        config = self._notification_config()
        if not config.get("enabled", True):
            return

        sent_alerts = set(state.get("sent_project_alerts", []))
        horizons = self._config.get("goals", {}).get("deadline_horizons", [7, 1])

        now = self._get_local_now()
        today = now.date()

        entries = await self._cache.query_by_type("project")
        for entry in entries:
            # Skip candidates
            if "project-candidate-" in entry["filename"]:
                continue

            try:
                fm = _safe_frontmatter(entry["frontmatter"])

                # Status filter: active or on-hold
                status = fm.get("status")
                if status not in ("active", "on-hold"):
                    continue

                due_raw = fm.get("due_date")
                if not due_raw:
                    continue

                try:
                    due = datetime.fromisoformat(str(due_raw)).date()
                except Exception:
                    continue

                # Stable ID from filename: extract the 6-char hex suffix
                project_id = Path(entry["filename"]).stem.rsplit("-", 1)[-1]

                days_until = (due - today).days

                # Find the smallest horizon that matches and hasn't fired yet
                for horizon in sorted(horizons):
                    alert_key = f"project:{project_id}:{horizon}d"
                    if alert_key in sent_alerts:
                        continue

                    if 0 <= days_until <= horizon:
                        # Haven't fired yet, within horizon - fire and stop
                        msg = (
                            f"⏰ Project deadline approaching: \"{fm.get('source_title', Path(entry['filename']).stem)}\" "
                            f"— due in {days_until} day{'s' if days_until != 1 else ''} ({due})"
                        )
                        await self.send_message(msg)
                        sent_alerts.add(alert_key)
                        log.info("Sent %d-day project alert for %s", horizon, project_id)
                        break  # Only fire one alert per project per check
            except Exception:
                log.exception("Error checking project alerts for %s", entry["filename"])

        state["sent_project_alerts"] = list(sent_alerts)

    async def _check_pre_meeting_alerts(self, state: dict):
        """Check for pre-meeting context pushes."""
        config = self._notification_config()
        if not config.get("enabled", True):
            return

        pre_meeting_minutes = config.get("pre_meeting_minutes", 10)
        now = self._get_local_now()
        window_start = now + timedelta(minutes=8)
        window_end = now + timedelta(minutes=12)

        sent_meetings = set(state.get("sent_pre_meeting", []))

        entries = await self._cache.query_by_prefix("calendar-event-")
        for entry in entries:
            try:
                fm = _safe_frontmatter(entry["frontmatter"])
            except (json.JSONDecodeError, TypeError):
                log.warning("pre_meeting_alerts: malformed frontmatter in %s — skipping", entry["filename"])
                continue
            if fm.get("type") != "calendar_event":
                continue

            if fm.get("all_day", False):
                continue  # Skip all-day events

            start_time_str = fm.get("start_time")
            if not start_time_str:
                continue

            try:
                start_time = datetime.fromisoformat(start_time_str)
                # Normalize timezone awareness so comparison doesn't raise TypeError.
                # Calendar files written without tz → assume same tz as `now`.
                if now.tzinfo is not None and start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=now.tzinfo)
                elif now.tzinfo is None and start_time.tzinfo is not None:
                    start_time = start_time.replace(tzinfo=None)
            except Exception:
                continue

            if not (window_start <= start_time <= window_end):
                continue  # Outside window

            # Deduplicate by source_url (machine-independent, derived from
            # title+start_time) so the same event written by two machines only
            # fires one alert. Fall back to filename stem for files without
            # source_url (pre-scanner versions) and check both keys so in-flight
            # entries from old state survive migration without a spurious re-send.
            source_url_key = fm.get("source_url")
            stem_key = Path(entry["filename"]).stem
            if (source_url_key and source_url_key in sent_meetings) or stem_key in sent_meetings:
                continue  # Already sent

            event_key = source_url_key or stem_key

            # Assemble pre-meeting context
            context_text = await self._assemble_pre_meeting_context(fm, start_time)
            await self.send_message(context_text)
            sent_meetings.add(event_key)
            log.info("Sent pre-meeting context for event %s", event_key)

        state["sent_pre_meeting"] = list(sent_meetings)
        _save_state(state)

    async def _check_calendar_staleness(self, state: dict):
        """Warn via Telegram when no calendar-event file has been written in >24h.

        Silent 10-day outages shouldn't be possible — if the scanner loop is
        running but producing no files (EventKit grant revoked, SQLite schema
        mismatch, predicate bug, etc.), this surfaces it. Dedup key is the local
        date so at most one alert fires per day.
        """
        config = self._notification_config()
        if not config.get("enabled", True):
            return

        entries = await self._cache.query_by_prefix("calendar-event-")
        if not entries:
            # No files ever written — could be a fresh install; stay silent.
            return

        try:
            most_recent = max(e["mtime"] for e in entries)
        except Exception:
            return

        now = self._get_local_now()
        hours_stale = (now.timestamp() - most_recent) / 3600.0
        if hours_stale < 24:
            return

        today_str = now.date().isoformat()
        sent = state.get("sent_calendar_staleness_alerts", []) or []
        if today_str in sent:
            return

        last_seen = datetime.fromtimestamp(most_recent).strftime("%Y-%m-%d %H:%M")
        msg = (
            "⚠️ Calendar ingestion is stale\n\n"
            f"No calendar-event files have been written in {hours_stale:.0f} hours "
            f"(last seen {last_seen}).\n\n"
            "Possible causes:\n"
            "• EventKit Calendar grant revoked or partial\n"
            "• SQLite Calendar.sqlitedb schema mismatch\n"
            "• Predicate filtering all calendars\n"
            "• Genuinely no events in ±7-day window\n\n"
            "Check ~/secondbrain/logs/out.log for zero-event warnings."
        )

        # State-before-send dedup: record today's date, save, then send. Roll
        # back on send failure so a transient Telegram outage doesn't eat the
        # next day's alert too.
        sent.append(today_str)
        state["sent_calendar_staleness_alerts"] = sent
        try:
            _save_state(state)
        except Exception:
            # If we can't persist the dedup, skip the send to avoid an
            # unbounded alert storm.
            log.exception("Failed to save calendar-staleness dedup; skipping send")
            sent.remove(today_str)
            state["sent_calendar_staleness_alerts"] = sent
            return
        try:
            await self.send_message(msg)
        except Exception:
            log.exception("Failed to send calendar staleness alert; rolling back dedup")
            sent.remove(today_str)
            state["sent_calendar_staleness_alerts"] = sent
            try:
                _save_state(state)
            except Exception:
                log.exception("Failed to roll back staleness dedup")

    async def _assemble_pre_meeting_context(self, event_fm: dict, start_time: datetime) -> str:
        """Assemble pre-meeting context from event and related files."""
        title = event_fm.get("source_title") or event_fm.get("title") or "(no title)"
        time_str = start_time.strftime("%I:%M %p").lstrip("0")
        location = event_fm.get("location", "")
        location_str = f", {location}" if location else ""

        lines = [f"{title} starts in 10 minutes ({time_str}{location_str})"]

        # Attendees with contact info
        participants = event_fm.get("participants") or []
        if participants:
            lines.append("\nAttendees:")
            for participant in participants[:10]:  # Limit to 10
                # Try to find contact file
                contact_entries = await self._cache.query_by_prefix("contact-")
                contact_fm = None
                for entry in contact_entries:
                    try:
                        cfm = _safe_frontmatter(entry["frontmatter"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    name = cfm.get("name", "")
                    email = cfm.get("email", "")
                    if participant.lower() in name.lower() or participant.lower() in email.lower():
                        contact_fm = cfm
                        break

                if contact_fm:
                    last_interaction = contact_fm.get("last_interaction", "")
                    relationship_score = contact_fm.get("relationship_score", 0)
                    lines.append(f"• {participant} — last interaction {last_interaction}, relationship score {relationship_score:.2f}")
                else:
                    lines.append(f"• {participant}")

        # Open commitments involving attendees
        open_commitments = []
        commitment_entries = await self._cache.query_by_type("commitment", status="active")
        for entry in commitment_entries:
            try:
                fm = _safe_frontmatter(entry["frontmatter"])
            except (json.JSONDecodeError, TypeError):
                continue

            owner = fm.get("owner", "").lower()
            recipient = fm.get("recipient", "").lower()
            owner_email = (fm.get("owner_email") or "").lower()

            # Check if any participant is involved
            involved = False
            for participant in participants:
                p_lower = participant.lower()
                if p_lower in owner or p_lower in recipient or p_lower in owner_email:
                    involved = True
                    break

            if involved:
                open_commitments.append(fm)

        if open_commitments:
            lines.append(f"\nOpen commitments with attendees:")
            for fm in open_commitments[:5]:  # Limit to 5
                ct = fm.get("commitment_type", "outbound")
                desc = (
                    fm.get("source_title")
                    or fm.get("summary")
                    or "(untitled commitment)"
                )[:60]
                owner = fm.get("owner", "")
                recipient = fm.get("recipient", "")
                due_date = fm.get("due_date")
                due_str = f" (due {due_date})" if due_date else " (no due date)"
                target = recipient if ct == "outbound" else (owner if ct != "personal" else "")
                if target:
                    lines.append(f"• [{ct}] {desc} → {target}{due_str}")
                else:
                    lines.append(f"• [{ct}] {desc}{due_str}")

        # Recent threads mentioning attendees (would use _last_results cache in real impl)
        # For now, skip — requires access to chat_handler's cache

        return "\n".join(lines)

    def _newest_llm_chat_mtime_by_platform(self) -> dict[str, datetime]:
        """Return newest mtime per platform for llm-chat memories.

        Parses platform from filename (llm-chat-{platform}-*.md) to avoid
        opening every file. Returns dict[platform, datetime] (timezone-aware).
        """
        by_platform = {}
        tz_name = self._user_config().get("timezone", "America/Los_Angeles")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        for f in MEMORIES_DIR.glob("llm-chat-*.md"):
            # Filename pattern: llm-chat-{platform}-{date}-{slug}-{id}.md
            # Extract platform from second segment
            parts = f.stem.split("-")
            if len(parts) < 3:
                continue
            platform = parts[2]  # 0=llm, 1=chat, 2=platform
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=tz)
            if platform not in by_platform or mtime > by_platform[platform]:
                by_platform[platform] = mtime
        return by_platform

    async def _check_llm_chat_refresh(self, state: dict) -> None:
        """Check if llm_chat memories are stale and nudge user to re-import."""
        cfg = self._config.get("llm_chat", {})
        if not cfg.get("nudge_enabled", True):
            return
        if state.get("muted", False):
            return

        interval_days = cfg.get("refresh_interval_days", 14)
        cooldown_days = cfg.get("nudge_cooldown_days", 7)

        last_nudge = state.get("last_llm_chat_nudge")
        if last_nudge:
            try:
                last_nudge_dt = datetime.fromisoformat(last_nudge)
                if (self._get_local_now() - last_nudge_dt).days < cooldown_days:
                    return
            except Exception:
                pass

        newest = self._newest_llm_chat_mtime_by_platform()
        now = self._get_local_now()
        stale = [p for p, t in newest.items() if (now - t).days > interval_days]
        missing = [p for p in ("claude", "chatgpt") if p not in newest]
        targets = stale + missing

        if not targets:
            return

        msg = (
            f"📥 LLM chat history is going stale ({', '.join(targets)}). "
            f"Re-export and run `/import_chats {targets[0]}` when you have a moment."
        )
        await self.send_message(msg)
        state["last_llm_chat_nudge"] = now.isoformat()

    async def _check_quota_thresholds(self, state: dict):
        """Check quota utilization thresholds and send alerts."""
        if state.get("muted", False):
            return

        cfg = self._config.get("quota", {})
        warn = cfg.get("warning_threshold", 0.75)
        crit = cfg.get("critical_threshold", 0.90)

        quota_state = _read_quota_state()
        if not quota_state:
            return

        # Import pure helpers from quota_scanner
        from quota_scanner import detect_threshold_crossings, render_one

        sent_alerts = state.setdefault("sent_quota_alerts", {})
        # Pass a snapshot so detect_threshold_crossings' pre-send mutation of
        # sent_alerts doesn't persist if send_message raises — only commit
        # each timestamp after successful delivery.
        sent_alerts_staging = dict(sent_alerts)
        crossings = detect_threshold_crossings(
            quota_state, sent_alerts_staging,
            warn=warn,
            crit=crit,
            cooldown_min=60,
        )

        for platform, level in crossings.items():
            emoji = "⚠️" if level == "warning" else "🔴"
            msg = f"{emoji} {platform} quota {level} — {render_one(platform, quota_state)}"
            await self.send_message(msg)
            # Commit the sent timestamp only after successful delivery
            key = f"{platform}:{level}"
            sent_alerts[key] = sent_alerts_staging[key]

    async def _check_and_send(self):
        """Main check-and-send logic run every 60 seconds."""
        state = _load_state()

        # Check mute state
        if state.get("muted", False):
            return

        # Check chat_id
        if self.get_chat_id() is None:
            return

        # Prune stale entries
        await self._prune_sent_alerts(state)

        # Run checks — each isolated so one failure doesn't block the rest
        for check_name, coro in [
            ("daily_briefing", self._check_daily_briefing(state)),
            ("llm_chat_refresh", self._check_llm_chat_refresh(state)),
            ("commitment_alerts", self._check_commitment_alerts(state)),
            ("commitment_day_checkpoints", self._check_commitment_day_checkpoints(state)),
            ("goal_alerts", self._check_goal_alerts(state)),
            ("project_alerts", self._check_project_alerts(state)),
            ("quota_thresholds", self._check_quota_thresholds(state)),
        ]:
            try:
                await coro
            except Exception:
                log.exception("_check_and_send: %s raised — continuing", check_name)
        _save_state(state)
        for check_name, coro in [
            ("pre_meeting_alerts", self._check_pre_meeting_alerts(state)),
            ("calendar_staleness", self._check_calendar_staleness(state)),
        ]:
            try:
                await coro
            except Exception:
                log.exception("_check_and_send: %s raised — continuing", check_name)

    async def run_loop(self, stop_event: asyncio.Event):
        """Main notification scheduling loop."""
        log.info("Notification manager started — checking every 60s")

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await self._check_and_send()
            except Exception as exc:
                log.exception("Uncaught error in notification loop")
                beat_status, beat_error = "error", str(exc)
            record_beat("notification_manager", beat_status, beat_error)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
