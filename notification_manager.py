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

log = logging.getLogger("notification-manager")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "notification-state.json"

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
    }


def _save_state(state: dict):
    """Atomically save notification state."""
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        os.rename(str(tmp), str(STATE_FILE))
    except Exception as e:
        log.warning("Failed to save notification state: %s", e)


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
    def __init__(self, bot=None, deploy_dir: Path = DEPLOY_DIR):
        self.bot = bot
        self.deploy_dir = deploy_dir
        self._state_cache = None

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

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
        """Send message to Telegram, chunking if needed."""
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

    def _prune_sent_alerts(self, state: dict):
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

            fm = _parse_frontmatter(commitment_files[0].read_text())
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

        # Prune pre-meeting alerts: remove if start_time has passed
        pruned_meetings = []
        for event_id in state.get("sent_pre_meeting", []):
            # Find the calendar event file
            event_files = list(MEMORIES_DIR.glob(f"calendar-event-*-{event_id}.md"))
            if not event_files:
                continue  # File deleted — discard

            fm = _parse_frontmatter(event_files[0].read_text())
            start_time_str = fm.get("start_time")
            if not start_time_str:
                continue  # No start time — discard

            try:
                start_time = datetime.fromisoformat(start_time_str)
                if start_time > now:
                    pruned_meetings.append(event_id)
            except Exception:
                pass  # Invalid timestamp — discard

        state["sent_pre_meeting"] = pruned_meetings

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

        # Send briefing
        briefing_text = self._assemble_briefing()
        await self.send_message(briefing_text)

        # Update state
        state["last_briefing_date"] = today_str
        _save_state(state)
        log.info("Daily briefing sent")

    def _assemble_briefing(self) -> str:
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
        for f in MEMORIES_DIR.glob("calendar-event-*.md"):
            fm = _parse_frontmatter(f.read_text())
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
                title = fm.get("title") or "(no title)"
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
        for f in MEMORIES_DIR.glob("commitment-*.md"):
            fm = _parse_frontmatter(f.read_text())
            if fm.get("type") != "commitment":
                continue
            if fm.get("status") != "active":
                continue
            due_date_str = fm.get("due_date")
            if not due_date_str:
                continue
            try:
                due_date = datetime.fromisoformat(due_date_str).date()
                if due_date == today:
                    due_today.append(fm)
                elif due_date < today:
                    overdue.append(fm)
            except Exception:
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
                target = recipient if ct == "outbound" else owner
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
        for f in MEMORIES_DIR.glob("commitment-*.md"):
            fm = _parse_frontmatter(f.read_text())
            if fm.get("type") != "commitment":
                continue
            if fm.get("status") != "active":
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

        # New memories since yesterday
        new_count = 0
        yesterday_midnight = datetime.combine(yesterday, datetime.min.time())
        yesterday_midnight = yesterday_midnight.replace(tzinfo=now.tzinfo)
        for f in MEMORIES_DIR.glob("*.md"):
            if f.name.startswith("commitment-") or f.name.startswith("calendar-event-"):
                continue
            try:
                mtime = f.stat().st_mtime
                mtime_dt = datetime.fromtimestamp(mtime, tz=now.tzinfo)
                if mtime_dt > yesterday_midnight:
                    new_count += 1
            except Exception:
                continue

        if new_count > 0:
            lines.append(f"\n{new_count} new memories captured since yesterday.")

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

        for f in MEMORIES_DIR.glob("commitment-*.md"):
            fm = _parse_frontmatter(f.read_text())
            if fm.get("type") != "commitment":
                continue
            if fm.get("status") != "active":
                continue

            due_date_str = fm.get("due_date")
            if not due_date_str:
                continue

            try:
                due_date = datetime.fromisoformat(due_date_str).date()
            except Exception:
                continue

            # Extract commitment ID from filename
            match = re.search(r'-([a-f0-9]{12})\.md$', f.name)
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

                target = recipient if ct == "outbound" else owner
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

                target = recipient if ct == "outbound" else owner
                alert = f"Reminder: commitment due tomorrow:\n[{ct}] {desc}"
                if target:
                    alert += f" → {target}"
                alert += f"\nSource: {source_title}"

                await self.send_message(alert)
                sent_alerts.add(commitment_id)
                log.info("Sent due-tomorrow alert for commitment %s", commitment_id)

        state["sent_commitment_alerts"] = list(sent_alerts)
        _save_state(state)

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

        for f in MEMORIES_DIR.glob("calendar-event-*.md"):
            fm = _parse_frontmatter(f.read_text())
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

            # Extract event ID from filename or use stable hash
            event_id = fm.get("event_id") or f.name.replace("calendar-event-", "").replace(".md", "")

            if event_id in sent_meetings:
                continue  # Already sent

            # Assemble pre-meeting context
            context_text = self._assemble_pre_meeting_context(fm, start_time)
            await self.send_message(context_text)
            sent_meetings.add(event_id)
            log.info("Sent pre-meeting context for event %s", event_id)

        state["sent_pre_meeting"] = list(sent_meetings)
        _save_state(state)

    def _assemble_pre_meeting_context(self, event_fm: dict, start_time: datetime) -> str:
        """Assemble pre-meeting context from event and related files."""
        title = event_fm.get("title") or "(no title)"
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
                contact_files = list(MEMORIES_DIR.glob(f"contact-*.md"))
                contact_fm = None
                for cf in contact_files:
                    cfm = _parse_frontmatter(cf.read_text())
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
        for f in MEMORIES_DIR.glob("commitment-*.md"):
            fm = _parse_frontmatter(f.read_text())
            if fm.get("type") != "commitment":
                continue
            if fm.get("status") != "active":
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
                target = recipient if ct == "outbound" else owner
                if target:
                    lines.append(f"• [{ct}] {desc} → {target}{due_str}")
                else:
                    lines.append(f"• [{ct}] {desc}{due_str}")

        # Recent threads mentioning attendees (would use _last_results cache in real impl)
        # For now, skip — requires access to chat_handler's cache

        return "\n".join(lines)

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
        self._prune_sent_alerts(state)

        # Run checks
        await self._check_daily_briefing(state)
        await self._check_commitment_alerts(state)
        await self._check_pre_meeting_alerts(state)

    async def run_loop(self, stop_event: asyncio.Event):
        """Main notification scheduling loop."""
        log.info("Notification manager started — checking every 60s")

        while not stop_event.is_set():
            try:
                await self._check_and_send()
            except Exception:
                log.exception("Uncaught error in notification loop")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
