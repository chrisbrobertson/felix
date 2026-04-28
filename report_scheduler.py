import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

import yaml

from llm_routes import resolve
from usage_tracker import record_usage
from heartbeat import record_beat

if TYPE_CHECKING:
    from telegram import Bot

log = logging.getLogger("report-scheduler")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))

VALID_SOURCES = {"commitments", "calendar", "meetings", "contacts", "memories", "projects", "comms"}
VALID_TYPES = {"digest", "analysis"}
VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun", "daily", "weekday", "weekend"}

# Maps source name → memory type values to match in frontmatter
SOURCE_TO_TYPES = {
    "commitments": {"commitment"},
    "calendar": {"calendar_event"},
    "meetings": {"meeting_transcript"},
    "contacts": {"contact"},
    "memories": {"web_memory"},
    "projects": {"project"},
    "comms": {"email_thread", "slack_thread"},
}


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


def parse_schedule(schedule_str: str) -> dict:
    """
    Parse a schedule string into {"days": list[str], "time": str}.

    Formats:
    - "daily HH:MM" → days=["mon","tue","wed","thu","fri","sat","sun"]
    - "weekday HH:MM" → days=["mon","tue","wed","thu","fri"]
    - "weekend HH:MM" → days=["sat","sun"]
    - "mon HH:MM" through "sun HH:MM" → days=[single day]
    - "mon,wed,fri HH:MM" → days=["mon","wed","fri"]

    Raises ValueError on invalid input.
    """
    parts = schedule_str.strip().lower().split()
    if len(parts) != 2:
        raise ValueError(f"Schedule must be '<days> HH:MM', got: {schedule_str}")

    days_part, time_part = parts

    # Validate time format
    if not re.match(r"^\d{2}:\d{2}$", time_part):
        raise ValueError(f"Time must be HH:MM format, got: {time_part}")

    hour, minute = map(int, time_part.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time must be 00:00-23:59, got: {time_part}")

    # Parse days
    if days_part == "daily":
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    elif days_part == "weekday":
        days = ["mon", "tue", "wed", "thu", "fri"]
    elif days_part == "weekend":
        days = ["sat", "sun"]
    elif "," in days_part:
        days = [d.strip() for d in days_part.split(",")]
        for d in days:
            if d not in VALID_DAYS or d in ("daily", "weekday", "weekend"):
                raise ValueError(f"Invalid day abbreviation: {d}")
    else:
        if days_part not in VALID_DAYS or days_part in ("daily", "weekday", "weekend"):
            raise ValueError(f"Invalid day specification: {days_part}")
        days = [days_part]

    return {"days": days, "time": time_part}


def is_due(report: dict, last_sent_date: Optional[str], now: datetime) -> bool:
    """
    Check if a report should be sent now.

    Returns True if:
    1. report is not paused
    2. today's weekday is in the schedule
    3. current time >= scheduled time
    4. hasn't been sent today yet
    """
    if report.get("paused", False):
        return False

    try:
        schedule = parse_schedule(report["schedule"])
    except Exception as e:
        log.warning(f"Failed to parse schedule '{report.get('schedule')}': {e}")
        return False

    # Check if today is in the schedule
    weekday_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_abbr = weekday_names[now.weekday()]
    if today_abbr not in schedule["days"]:
        return False

    # Check if current time >= scheduled time
    current_time = now.strftime("%H:%M")
    if current_time < schedule["time"]:
        return False

    # Check if already sent today
    today_str = now.strftime("%Y-%m-%d")
    if last_sent_date == today_str:
        return False

    return True


def _load_memories_for_sources(sources: list[str], window_days: int) -> list[dict]:
    """
    Load memories matching the given sources within the time window.

    Returns list of dicts with keys: path, fm (frontmatter), body_snippet.
    Sorted by created descending, capped at 100 files.
    """
    if not MEMORIES_DIR.exists():
        log.warning(f"Memories directory does not exist: {MEMORIES_DIR}")
        return []

    # Collect all type values we need to match
    target_types = set()
    for source in sources:
        target_types.update(SOURCE_TO_TYPES.get(source, set()))

    if not target_types:
        return []

    cutoff = datetime.now() - timedelta(days=window_days)
    memories = []

    for path in MEMORIES_DIR.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            fm = _parse_frontmatter(text)

            # Check type match
            mem_type = fm.get("type")
            if mem_type not in target_types:
                continue

            # Check created date
            created_str = fm.get("created")
            if not created_str:
                continue

            try:
                # Handle ISO datetime parsing
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                # Make timezone-naive for comparison
                if created.tzinfo:
                    created = created.replace(tzinfo=None)
            except Exception:
                continue

            if created < cutoff:
                continue

            # Extract body (everything after second ---)
            parts = text.split("---", 2)
            body = parts[2].strip() if len(parts) >= 3 else ""
            body_snippet = body[:500]

            memories.append({
                "path": path,
                "fm": fm,
                "body_snippet": body_snippet,
                "created": created,
            })
        except Exception as e:
            log.debug(f"Error loading memory {path}: {e}")
            continue

    # Sort by created descending, cap at 100
    memories.sort(key=lambda m: m["created"], reverse=True)
    return memories[:100]


# ── Generators ────────────────────────────────────────────────────────────────

class DigestGenerator:
    """Generate structured digest text from loaded memories."""

    def generate(self, report: dict, memories: list[dict]) -> str:
        """Generate structured digest text from loaded memories."""
        now = datetime.now()
        title = report.get("title", "Report")
        header = f"# {title} — {now.strftime('%A, %B %-d')}\n\n"

        sources = report.get("sources", ["memories"])
        sections = []

        for source in sources:
            # Filter memories to this source's types
            source_types = SOURCE_TO_TYPES.get(source, set())
            items = [m for m in memories if m["fm"].get("type") in source_types]

            if not items:
                continue

            section_header = f"**{source.title()} ({len(items)})**\n"
            lines = []

            for i, item in enumerate(items[:10]):  # Cap at 10 per source
                fm = item["fm"]
                line = self._format_item(source, fm)
                lines.append(f"• {line}")

            if len(items) > 10:
                lines.append(f"…and {len(items) - 10} more")

            sections.append(section_header + "\n".join(lines))

        if not sections:
            return header + "No items to report."

        return header + "\n\n".join(sections)

    def _format_item(self, source: str, fm: dict) -> str:
        """Format a single item based on its source type."""
        if source == "commitments":
            title = fm.get("title", "Untitled commitment")
            status = fm.get("status", "unknown")
            due_date = fm.get("due_date", "no date")
            return f"{title} [{status}] — due: {due_date}"

        elif source == "calendar":
            title = fm.get("title", "Untitled event")
            start_time = fm.get("start_time", "unknown time")
            location = fm.get("location", "no location")
            return f"{title} — {start_time} ({location})"

        elif source == "meetings":
            title = fm.get("title", "Untitled meeting")
            date = fm.get("date", "unknown date")
            participants = fm.get("participants", [])
            count = len(participants) if participants else "?"
            return f"{title} — {date} ({count} attendees)"

        elif source == "contacts":
            name = fm.get("name", "Unknown")
            email = fm.get("email", "no email")
            last_seen = fm.get("last_seen", "?")
            return f"{name} — {email} (last seen: {last_seen})"

        elif source == "memories":
            title = fm.get("source_title", "Untitled page")
            url = fm.get("source_url", "")
            domain = urlparse(url).netloc if url else "unknown"
            return f"{title} — {domain}"

        elif source == "projects":
            title = fm.get("title", "Untitled project")
            last_commit = fm.get("last_commit_date", "unknown")
            return f"{title} — last commit: {last_commit}"

        elif source == "comms":
            title = fm.get("title", "Untitled thread")
            mem_type = fm.get("type", "unknown")
            type_label = "email" if "email" in mem_type else "slack"

            if "email" in mem_type:
                sender = fm.get("participants", [{}])[0].get("name", "unknown") if fm.get("participants") else "unknown"
                last_msg = fm.get("last_message_date", fm.get("date", "unknown"))
                return f"[{type_label}] {title} — {sender} ({last_msg})"
            else:
                channel = fm.get("channel", "unknown")
                last_msg = fm.get("last_message_ts", fm.get("date", "unknown"))
                return f"[{type_label}] {title} — {channel} ({last_msg})"

        return fm.get("title", "Untitled")


class AnalysisGenerator:
    """Generate LLM narrative analysis from loaded memories."""

    async def generate(self, report: dict, memories: list[dict], model_route: str) -> str:
        """Generate LLM narrative analysis from loaded memories."""
        from litellm import acompletion

        # Build context string
        context_parts = []
        total_chars = 0

        for item in memories[:20]:  # Cap at 20 items
            fm = item["fm"]
            mem_type = fm.get("type", "unknown")
            title = fm.get("title", "Untitled")
            snippet = item["body_snippet"]

            part = f"[{mem_type}] {title}: {snippet}"
            context_parts.append(part)
            total_chars += len(part) + 5  # +5 for separator

            if total_chars >= 8000:
                break

        context = "\n---\n".join(context_parts)

        # Build prompt
        prompt = report.get("prompt", "Summarize the key themes and insights from the context below.")

        messages = [
            {
                "role": "system",
                "content": "You are synthesizing a personal knowledge report. Be concise and actionable. 300 words max."
            },
            {
                "role": "user",
                "content": f"{prompt}\n\nContext:\n{context}"
            }
        ]

        try:
            response = await acompletion(
                model=resolve(model_route),
                messages=messages,
                max_tokens=500
            )
            if hasattr(response, "usage") and response.usage:
                record_usage(resolve(model_route), response.usage.prompt_tokens or 0, response.usage.completion_tokens or 0)
            return response.choices[0].message.content.strip()
        except Exception as e:
            log.error(f"Analysis generation failed: {e}")
            return f"Analysis failed: {str(e)}"


# ── ReportScheduler ───────────────────────────────────────────────────────────

class ReportScheduler:
    """
    Configurable report scheduler that delivers digest or LLM-analysis reports
    via Telegram on user-defined schedules.
    """

    def __init__(self, config: dict, bot: "Bot", chat_id_getter, deploy_dir: Path):
        """
        config: full daemon config dict
        bot: python-telegram-bot Bot instance
        chat_id_getter: callable() -> Optional[int] (reads from notification state)
        deploy_dir: Path to DEPLOY_DIR
        """
        self.config = config
        self.bot = bot
        self.chat_id_getter = chat_id_getter
        self.state_file = deploy_dir / "reports-state.json"
        self.digest_gen = DigestGenerator()
        self.analysis_gen = AnalysisGenerator()
        self._last_report_set: list[dict] = []  # For /report N command

    async def run_loop(self, stop_event: asyncio.Event):
        """Main polling loop — checks reports every 60 seconds."""
        log.info("Report scheduler started")

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await self._check_reports()
            except Exception as exc:
                log.error(f"Report scheduler error: {exc}", exc_info=True)
                beat_status, beat_error = "error", str(exc)
            record_beat("report_scheduler", beat_status, beat_error)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

        log.info("Report scheduler stopped")

    async def _check_reports(self):
        """Check all reports and send any that are due."""
        chat_id = self.chat_id_getter()
        if chat_id is None:
            return  # No Telegram configured yet

        state = self._load_state()
        config_reports = self.config.get("reports", {})
        runtime_reports = state.get("runtime_reports", [])

        # Build list of (name, definition) tuples
        all_reports = []

        # Config reports
        for name, defn in config_reports.items():
            all_reports.append((name, defn, True))  # True = is_config_report

        # Runtime reports
        for defn in runtime_reports:
            name = defn.get("id", "unknown")
            all_reports.append((name, defn, False))  # False = is_runtime_report

        now = datetime.now()

        for name, defn, is_config in all_reports:
            # Check if paused
            if is_config:
                # Config reports can have paused override in state
                if state.get("paused_config_reports", {}).get(name, False):
                    continue

            last_sent = state.get("last_sent", {}).get(name)

            if not is_due(defn, last_sent, now):
                continue

            try:
                text = await self._run_report(name, defn)
                for chunk in self._chunk(text, 4000):
                    await self.bot.send_message(chat_id=chat_id, text=chunk)

                # Update state
                if "last_sent" not in state:
                    state["last_sent"] = {}
                state["last_sent"][name] = now.strftime("%Y-%m-%d")
                self._save_state(state)

                log.info(f"Report sent: {name}")
            except Exception as e:
                log.error(f"Report {name} failed: {e}", exc_info=True)

    async def _run_report(self, name: str, defn: dict) -> str:
        """Execute a single report and return the formatted text."""
        sources = defn.get("sources", ["memories"])
        window_days = defn.get("window_days", 7)

        memories = _load_memories_for_sources(sources, window_days)

        report_type = defn.get("type", "digest")

        if report_type == "digest":
            return self.digest_gen.generate(defn, memories)
        elif report_type == "analysis":
            model_route = defn.get("model_route", "chat")
            return await self.analysis_gen.generate(defn, memories, model_route)
        else:
            raise ValueError(f"Unknown report type: {report_type}")

    async def trigger_report(self, name: str, defn: dict, chat_id: int):
        """
        Run a report immediately (for /report-run command).
        Does not update last_sent state.
        """
        text = await self._run_report(name, defn)
        for chunk in self._chunk(text, 4000):
            await self.bot.send_message(chat_id=chat_id, text=chunk)

    def _chunk(self, text: str, max_len: int) -> list[str]:
        """Split text into chunks ≤ max_len, preferring newlines as split points."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        remaining = text

        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            # Try to split at newline
            chunk = remaining[:max_len]
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

    def _load_state(self) -> dict:
        """Load state from JSON file."""
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception as e:
                log.warning(f"Failed to load state: {e}")

        return {
            "runtime_reports": [],
            "last_sent": {},
            "paused_config_reports": {},
        }

    def _save_state(self, state: dict):
        """Atomically save state to JSON file."""
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(self.state_file))
        except Exception as e:
            log.warning(f"Failed to save state: {e}")

    def get_all_reports(self) -> list[dict]:
        """
        Get merged list of all reports (config + runtime).

        Returns list of dicts with keys:
        - name: report name/id
        - schedule: schedule string
        - type: "digest" or "analysis"
        - sources: list of source names
        - window_days: int
        - paused: bool
        - last_sent: YYYY-MM-DD or None
        - is_config_report: bool
        """
        state = self._load_state()
        config_reports = self.config.get("reports", {})
        runtime_reports = state.get("runtime_reports", [])
        paused_config = state.get("paused_config_reports", {})
        last_sent = state.get("last_sent", {})

        all_reports = []

        # Config reports
        for name, defn in config_reports.items():
            all_reports.append({
                "name": name,
                "schedule": defn.get("schedule", "unknown"),
                "type": defn.get("type", "digest"),
                "sources": defn.get("sources", ["memories"]),
                "window_days": defn.get("window_days", 7),
                "paused": paused_config.get(name, defn.get("paused", False)),
                "last_sent": last_sent.get(name),
                "is_config_report": True,
            })

        # Runtime reports
        for defn in runtime_reports:
            report_id = defn.get("id", "unknown")
            all_reports.append({
                "name": report_id,
                "schedule": defn.get("schedule", "unknown"),
                "type": defn.get("type", "digest"),
                "sources": defn.get("sources", ["memories"]),
                "window_days": defn.get("window_days", 7),
                "paused": defn.get("paused", False),
                "last_sent": last_sent.get(report_id),
                "is_config_report": False,
            })

        return all_reports

    def add_runtime_report(self, defn: dict) -> str:
        """
        Add a new runtime report.

        Validates schedule, type, and sources. Raises ValueError on invalid input.
        Returns the generated report id.
        """
        # Validate schedule
        try:
            parse_schedule(defn["schedule"])
        except Exception as e:
            raise ValueError(f"Invalid schedule: {e}")

        # Validate type
        report_type = defn.get("type", "digest")
        if report_type not in VALID_TYPES:
            raise ValueError(f"Invalid type '{report_type}'. Must be one of: {VALID_TYPES}")

        # Validate sources
        sources = defn.get("sources", ["memories"])
        for source in sources:
            if source not in VALID_SOURCES:
                raise ValueError(f"Invalid source '{source}'. Must be one of: {VALID_SOURCES}")

        # Generate unique id
        import hashlib
        import time
        key = f"{time.time()}:{defn.get('title', 'report')}"
        report_id = "r-" + hashlib.sha1(key.encode()).hexdigest()[:6]

        # Add to state
        state = self._load_state()
        if "runtime_reports" not in state:
            state["runtime_reports"] = []

        defn["id"] = report_id
        state["runtime_reports"].append(defn)
        self._save_state(state)

        log.info(f"Added runtime report: {report_id}")
        return report_id

    def remove_runtime_report(self, report_id: str) -> bool:
        """Remove runtime report by id. Returns False if not found."""
        state = self._load_state()
        runtime_reports = state.get("runtime_reports", [])

        initial_len = len(runtime_reports)
        state["runtime_reports"] = [r for r in runtime_reports if r.get("id") != report_id]

        if len(state["runtime_reports"]) < initial_len:
            self._save_state(state)
            log.info(f"Removed runtime report: {report_id}")
            return True

        return False

    def set_paused(self, name: str, paused: bool, is_runtime: bool) -> bool:
        """
        Set paused state for a report.

        For runtime reports: update in runtime_reports list.
        For config reports: store override in state["paused_config_reports"].

        Returns True if found, False otherwise.
        """
        state = self._load_state()

        if is_runtime:
            # Update runtime report
            runtime_reports = state.get("runtime_reports", [])
            for report in runtime_reports:
                if report.get("id") == name:
                    report["paused"] = paused
                    self._save_state(state)
                    log.info(f"Set runtime report {name} paused={paused}")
                    return True
            return False
        else:
            # Config report — store override
            if "paused_config_reports" not in state:
                state["paused_config_reports"] = {}
            state["paused_config_reports"][name] = paused
            self._save_state(state)
            log.info(f"Set config report {name} paused={paused}")
            return True
