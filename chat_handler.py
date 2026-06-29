import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import socket
import time
import yaml
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TimedOut, NetworkError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from skill_executor import SkillExecutor, SkillAuthError
from content_fetcher import fetch_url_content
from github_client import GitHubClient, _STANDARD_LABELS
from goals_tracker import GoalManager
import heartbeat as hb

log = logging.getLogger("chat-handler")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
DEFAULT_ICLOUD_ROOT = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
MAX_CONTEXT_CHARS = 80_000  # Deprecated; kept for fallback only
MAX_CONTEXT_TOKENS = 150_000  # Security (M9): token-aware budget (75% of 200k context window)
TG_MAX_CHARS = 4096  # Telegram hard limit per message

# COMMAND_REGISTRY is the single source of truth for all commands.
# Defined in command_core.py and imported here so cmd_help, tests, and
# _format_commands_text all reference the same object.
from command_core import COMMAND_REGISTRY  # noqa: E402 (import after stdlib)
from utils import read_text_with_retry, read_text_with_retry_async

# Backfill configuration: default and max days per scanner type
BACKFILL_CONFIG = {
    "readings": {"default_days": 30, "max_days": 90},
    "email":    {"default_days": 30, "max_days": 90},
    "zoom":     {"default_days": 30, "max_days": 180},
    "calendar": {"default_days": 30, "max_days": 180},
    "slack":    {"default_days": 30, "max_days": 90},
    "code":     {"default_days": 0,  "max_days": 0},
}


def _mutation_succeeded(name: str, result: str) -> bool:
    """Return True only when a mutating tool call actually wrote state.

    Errors are returned as strings (e.g. "Error: invalid category") rather than
    raised, so we must inspect the result before recording the mutation as applied.
    close_issue has additional non-error, non-mutating returns ("No issue found …",
    "Multiple matches …") that would otherwise slip through an "Error:" prefix check.
    deliver_pending_replies returns "No pending replies …" when the queue is empty —
    that is a no-op, not a mutation.
    """
    if result.startswith("Error"):
        return False
    if name == "close_issue":
        return result.startswith("Closed [")
    if name == "deliver_pending_replies":
        return result.startswith("Delivered")
    return True


def _safe_read_text(path: Path) -> Optional[str]:
    """Read iCloud file with retry, returning None on persistent EDEADLK/EAGAIN."""
    return read_text_with_retry(path, default=None)


def _safe_error(e: Exception) -> str:
    """Return a sanitized error string safe to send over Telegram.

    Strips filesystem paths and caps length so internal details don't
    leak through Telegram's servers to external logs.
    """
    msg = f"{type(e).__name__}: {str(e)[:100]}"
    msg = re.sub(r'/\S+/\S+', '[path]', msg)
    return msg


class TelegramChatHandler:
    PENDING_FILE = DEPLOY_DIR / "pending-replies.json"
    HISTORY_FILE = DEPLOY_DIR / "chat-history.json"

    def __init__(self, scanners: dict = None, cache=None):
        # Use the shared iCloud-resilient loader (retries EDEADLK with backoff).
        try:
            from utils import load_config
            config = load_config(BRAIN_DIR / "config.yaml")
        except Exception as e:
            log.warning("Could not read config.yaml at startup: %s — using defaults", e)
            config = {}
        self._config = config  # Store for access by tools and helpers
        self._cache = cache  # MemoryCache instance for reading memories
        self.token = config["telegram"]["bot_token"]
        self.allowed_user_id = int(config["user"]["telegram_user_id"])
        self.executor = SkillExecutor("chat")
        self.app = ApplicationBuilder().token(self.token).concurrent_updates(True).build()
        self.scanners = scanners or {}

        # Goal manager for FR-7 context injection and FR-8 LLM tools
        from goals_tracker import GoalManager
        self._goal_manager = GoalManager(BRAIN_DIR / "memories", config)
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )
        self.app.add_handler(CommandHandler("skip", self.cmd_skip))
        self.app.add_handler(CommandHandler("unskip", self.cmd_unskip))
        self.app.add_handler(CommandHandler("skiplist", self.cmd_skiplist))
        self.app.add_handler(CommandHandler("dupes", self.cmd_dupes))
        self.app.add_handler(CommandHandler("merge", self.cmd_merge))
        self.app.add_handler(CommandHandler("keep", self.cmd_keep))
        self.app.add_handler(CommandHandler("watch", self.cmd_watch))
        self.app.add_handler(CommandHandler("watches", self.cmd_watches))
        self.app.add_handler(CommandHandler("unwatch", self.cmd_unwatch))
        self.app.add_handler(CommandHandler("import_chats", self.cmd_import_chats))
        self.app.add_handler(CommandHandler("readings", self.cmd_readings))
        self.app.add_handler(CommandHandler("search", self.cmd_search))
        self.app.add_handler(CommandHandler("reading", self.cmd_reading))
        self.app.add_handler(CommandHandler("forget", self.cmd_forget))
        self.app.add_handler(CommandHandler("commitments", self.cmd_commitments))
        self.app.add_handler(CommandHandler("todos", self.cmd_todos))
        self.app.add_handler(CommandHandler("complete", self.cmd_complete))
        self.app.add_handler(CommandHandler("dismiss", self.cmd_dismiss))
        self.app.add_handler(CommandHandler("wrong", self.cmd_wrong))
        self.app.add_handler(CommandHandler("missed", self.cmd_missed))
        self.app.add_handler(CommandHandler("todo", self.cmd_todo))
        self.app.add_handler(CommandHandler("accuracy", self.cmd_accuracy))
        self.app.add_handler(CommandHandler("quota", self.cmd_quota))
        self.app.add_handler(CommandHandler("contacts", self.cmd_contacts))
        self.app.add_handler(CommandHandler("contact", self.cmd_contact))
        self.app.add_handler(CommandHandler("people", self.cmd_contacts))
        self.app.add_handler(CommandHandler("code", self.cmd_code))
        self.app.add_handler(CommandHandler("events", self.cmd_events))
        self.app.add_handler(CommandHandler("event", self.cmd_event))
        self.app.add_handler(CommandHandler("notes", self.cmd_notes))
        self.app.add_handler(CommandHandler("meetings", self.cmd_meetings))
        self.app.add_handler(CommandHandler("meeting", self.cmd_meeting))
        self.app.add_handler(CommandHandler("comms", self.cmd_comms))
        self.app.add_handler(CommandHandler("messages", self.cmd_comms))
        self.app.add_handler(CommandHandler("communications", self.cmd_comms))
        self.app.add_handler(CommandHandler("comm", self.cmd_comm))
        self.app.add_handler(CommandHandler("message", self.cmd_comm))
        self.app.add_handler(CommandHandler("communication", self.cmd_comm))
        self.app.add_handler(CommandHandler("aichat", self.cmd_aichat))
        self.app.add_handler(CommandHandler("insights", self.cmd_insights))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("commands", self.cmd_help))
        self.app.add_handler(CommandHandler("version", self.cmd_version))
        self.app.add_handler(CommandHandler("usage", self.cmd_usage))
        self.app.add_handler(CommandHandler("settings", self.cmd_settings))
        self.app.add_handler(CommandHandler("reset", self.cmd_reset))
        self.app.add_handler(CommandHandler("deliver", self.cmd_deliver))
        self.app.add_handler(CommandHandler("discard", self.cmd_discard))
        self.app.add_handler(CommandHandler("briefing", self.cmd_briefing))
        self.app.add_handler(CommandHandler("mute", self.cmd_mute))
        self.app.add_handler(CommandHandler("unmute", self.cmd_unmute))
        # Feature tracker
        self.app.add_handler(CommandHandler("feature", self.cmd_feature))
        self.app.add_handler(CommandHandler("feature_new", self.cmd_feature))
        self.app.add_handler(CommandHandler("bug", self.cmd_bug))
        self.app.add_handler(CommandHandler("bugs", self.cmd_bugs))
        self.app.add_handler(CommandHandler("features", self.cmd_features))
        self.app.add_handler(CommandHandler("feature_detail", self.cmd_feature_detail))
        self.app.add_handler(CommandHandler("fdetail", self.cmd_feature_detail))
        self.app.add_handler(CommandHandler("feature_priority", self.cmd_feature_priority))
        self.app.add_handler(CommandHandler("feature_plan", self.cmd_feature_plan))
        self.app.add_handler(CommandHandler("feature_start", self.cmd_feature_start))
        self.app.add_handler(CommandHandler("feature_done", self.cmd_feature_done))
        self.app.add_handler(CommandHandler("feature_wont_do", self.cmd_feature_wont_do))
        self.app.add_handler(CommandHandler("feature_note", self.cmd_feature_note))
        self.app.add_handler(CommandHandler("feature_import", self.cmd_feature_import))
        # Goals
        self.app.add_handler(CommandHandler("addgoal", self.cmd_addgoal))
        self.app.add_handler(CommandHandler("goals", self.cmd_goals))
        self.app.add_handler(CommandHandler("goal", self.cmd_goal))
        self.app.add_handler(CommandHandler("completegoal", self.cmd_completegoal))
        self.app.add_handler(CommandHandler("abandongoal", self.cmd_abandongoal))
        self.app.add_handler(CommandHandler("goal_note", self.cmd_goal_note))
        self.app.add_handler(CommandHandler("goal_due", self.cmd_goal_due))
        # Projects
        self.app.add_handler(CommandHandler("addproject", self.cmd_addproject))
        self.app.add_handler(CommandHandler("projects", self.cmd_projects))
        self.app.add_handler(CommandHandler("project", self.cmd_project))
        self.app.add_handler(CommandHandler("completeproject", self.cmd_completeproject))
        self.app.add_handler(CommandHandler("abandonproject", self.cmd_abandonproject))
        self.app.add_handler(CommandHandler("holdproject", self.cmd_holdproject))
        self.app.add_handler(CommandHandler("addmilestone", self.cmd_addmilestone))
        self.app.add_handler(CommandHandler("milestone", self.cmd_milestone))
        self.app.add_handler(CommandHandler("project_note", self.cmd_project_note))
        self.app.add_handler(CommandHandler("project_due", self.cmd_project_due))
        self.app.add_handler(CommandHandler("linkgoal", self.cmd_linkgoal))
        self.app.add_handler(CommandHandler("unlinkgoal", self.cmd_unlinkgoal))
        self.app.add_handler(CommandHandler("changes", self.cmd_changes))
        # Skill management
        self.app.add_handler(CommandHandler("skill_drafts", self.cmd_skill_drafts))
        self.app.add_handler(CommandHandler("skill_draft", self.cmd_skill_draft))
        self.app.add_handler(CommandHandler("approve_skill", self.cmd_approve_skill))
        self.app.add_handler(CommandHandler("reject_skill", self.cmd_reject_skill))
        self.app.add_handler(CommandHandler("skill_approval", self.cmd_skill_approval))
        self.app.add_handler(CommandHandler("skill_health", self.cmd_skill_health))
        # Reports
        self.app.add_handler(CommandHandler("reports", self.cmd_reports))
        self.app.add_handler(CommandHandler("report", self.cmd_report))
        self.app.add_handler(CommandHandler("report_add", self.cmd_report_add))
        self.app.add_handler(CommandHandler("report_remove", self.cmd_report_remove))
        self.app.add_handler(CommandHandler("report_pause", self.cmd_report_pause))
        self.app.add_handler(CommandHandler("report_resume", self.cmd_report_resume))
        self.app.add_handler(CommandHandler("report_run", self.cmd_report_run))
        # Review
        self.app.add_handler(CommandHandler("pending", self.cmd_pending))
        self.app.add_handler(CommandHandler("review", self.cmd_review))
        self.app.add_handler(CommandHandler("confirm", self.cmd_confirm))
        self.app.add_handler(CommandHandler("reject", self.cmd_reject))
        self.app.add_handler(CommandHandler("review_purge", self.cmd_review_purge))
        self.app.add_handler(CommandHandler("edit", self.cmd_edit))
        # Agent actions
        self.app.add_handler(CommandHandler("actions", self.cmd_actions))
        self.app.add_handler(CommandHandler("action", self.cmd_action))
        self.app.add_handler(CommandHandler("run", self.cmd_run))
        self.app.add_handler(CommandHandler("drop", self.cmd_drop))
        self.app.add_handler(CommandHandler("defer", self.cmd_defer))
        # System
        self.app.add_handler(CommandHandler("backfill", self.cmd_backfill))
        self.app.add_handler(CommandHandler("remember", self.cmd_remember))
        self.app.add_handler(CommandHandler("deepen", self.cmd_deepen))
        self.app.add_handler(CommandHandler("note", self.cmd_note))
        self.app.add_handler(CommandHandler("rebuild_cache", self.cmd_rebuild_cache))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        # Circles
        self.app.add_handler(CommandHandler("circles", self.cmd_circles))
        self.app.add_handler(CommandHandler("circle", self.cmd_circle))
        self.app.add_handler(CommandHandler("circle_status", self.cmd_circle_status))
        self.app.add_handler(CommandHandler("circle_rule", self.cmd_circle_rule))
        self.app.add_handler(CommandHandler("circle_invite", self.cmd_circle_invite))
        self.app.add_error_handler(self._on_telegram_error)
        # Cache: path -> (mtime, header_text). Invalidated when mtime changes.
        # Avoids reading every file on every chat message.
        self._header_cache: dict = {}
        # Last /readings or /search result set — used by /reading <N>.
        self._last_results: list = []
        # Set by each list command; /forget N indexes this
        self._active_list: list = []
        # Last /commitments result set — used by /complete <N> and /dismiss <N>.
        self._last_commitment_set: list = []
        # Last /todos result set — used by /todos done|dismiss <N>.
        self._last_todos_set: list = []
        # Last /contacts result set — used by /contact <N>.
        self._last_contact_set: list = []
        # Last /code result set — used by /code <N> detail view.
        self._last_code_set: list = []
        # Last /goals result set — used by /goal <N>, /completegoal <N>, /abandongoal <N>.
        self._last_goal_set: list = []
        # Last /projects result set — used by /project <N> and project actions.
        self._last_project_set: list = []
        # Last /events result set — used by /event <N>.
        self._last_event_set: list = []
        # Last /notes result set — used by /note <N>.
        self._last_note_set: list = []
        # Last /meetings result set — used by /meeting <N>.
        self._last_meeting_set: list = []
        # Last /comms result set — used by /comm <N>.
        self._last_comms_set: list = []
        # Last /actions result set — used by /action <N>, /run <N>, /drop <N>, /defer <N>.
        self._last_action_set: list = []
        # Last /features result set
        self._last_feature_set: list = []
        # Last /skill-drafts result set
        self._last_skill_draft_set: list = []
        # Conversation history per chat_id — {role, content} pairs, last N turns
        self._chat_history: dict = self._load_history()  # chat_id → list of {role, content}
        self._chat_history_locks: dict = {} # chat_id → asyncio.Lock
        # Ring buffer of recent slash-command outputs per chat_id — lets the LLM reference
        # previous listing results in follow-up questions ("which of those is urgent?")
        self._recent_commands: dict = {}  # chat_id → deque[(label, text), maxlen=5]
        self.HISTORY_WINDOW_TURNS = 15      # keep last 15 user+assistant pairs (30 messages)
        # Token budget for history injected into each request. Memory context already
        # consumes up to MAX_CONTEXT_TOKENS (150K); reserving 20K for history leaves
        # ~30K for system prompt, tools, new user message, and response inside 200K window.
        self.HISTORY_TOKEN_BUDGET = 20_000
        # Last /review result set — used by /review <N>, /confirm <N>, /reject <N>, /edit <N>.
        self._last_candidate_set: list = []
        # Last /dupes result set — used by /merge <N>, /keep <N>.
        self._last_dupes_set: list = []
        # Last /watches result set — used by /unwatch <N>.
        self._last_watchlist_set: list = []
        # GitHub backing for feature/bug commands
        gh_cfg = config.get("github", {}) or {}
        repo = os.environ.get("GITHUB_REPO") or gh_cfg.get("repo", "")
        self.github = GitHubClient(repo=repo)
        self._labels_bootstrapped = False
        if self.github.enabled:
            log.info(f"GitHub backing enabled for feature/bug → {self.github.repo}")
        else:
            log.info("GitHub backing disabled — feature/bug using local files")
        # Last /reports result set
        self._last_report_set: list = []
        # Last /circles result set — used by /circle <N>.
        self._last_circle_set: list = []
        # Goal manager for goals and projects CRUD
        self._goal_manager = GoalManager(BRAIN_DIR / "memories", config)
        # Notification manager reference (set by daemon.py)
        self.notification_manager = None
        # Skill creator and report scheduler (set by daemon.py)
        self.skill_creator = None
        self.report_scheduler = None

    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        self._stop_event = asyncio.Event()
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(self._stop_event))
        log.info("Telegram bot polling started")

    async def stop(self):
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        if hasattr(self, "_reconnect_task"):
            try:
                await asyncio.wait_for(self._reconnect_task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        log.info("Telegram bot stopped")

    async def poll_loop(self, stop_event: asyncio.Event):
        await stop_event.wait()

    def _get_header(self, path: Path) -> str:
        """Return cached first-500-chars header, refreshing only when mtime changes."""
        mtime = path.stat().st_mtime
        cached = self._header_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            header = path.read_text()[:500]
        except OSError:
            # iCloud may temporarily lock files during sync (EAGAIN).
            # Return cached header if we have one, otherwise empty string.
            return cached[1] if cached else ""
        self._header_cache[path] = (mtime, header)
        return header

    def _score_relevance(self, path: Path, query: str) -> int:
        """
        Cheap keyword intersection against cached file header.
        Score = query tokens (3+ chars) found in title/tags frontmatter block.
        One file read per new/modified file — not per message.
        """
        header = self._get_header(path).lower()
        tokens = {w for w in re.findall(r'\b\w{3,}\b', query.lower())}
        return sum(1 for t in tokens if t in header)

    def _fmt_command_list(self) -> str:
        """Format COMMAND_REGISTRY as plain text for injection into LLM context."""
        lines = ["# Available Telegram Commands",
                 "(You know these commands and can reference or suggest them to the user.)"]
        for group, cmds in COMMAND_REGISTRY.items():
            lines.append(f"\n## {group}")
            for cmd, desc in cmds:
                lines.append(f"/{cmd} — {desc}")
        return "\n".join(lines)

    def _list_commands_text(self) -> str:
        return self._fmt_command_list()

    def _get_display_config(self) -> dict:
        """Read display config from config.yaml (live, so /settings changes take effect)."""
        try:
            from utils import load_config
            return load_config(BRAIN_DIR / "config.yaml").get("display", {})
        except Exception:
            return {}

    def _fmt_datetime(self, iso_str) -> str:
        """Format an ISO datetime string per display.date_format and display.timezone config."""
        if not iso_str:
            return ""
        from datetime import datetime, timedelta
        display = self._get_display_config()
        fmt = display.get("date_format", "MM/DD/YYYY, HH:MM")
        tz_name = display.get("timezone", "")

        dt = None
        dt_str = str(iso_str).strip()
        for pattern in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M%z",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(dt_str, pattern)
                break
            except ValueError:
                continue
        if dt is None:
            return dt_str[:16]

        if tz_name:
            try:
                import zoneinfo
                tz = zoneinfo.ZoneInfo(tz_name)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz)
                else:
                    dt = dt.astimezone(tz)
            except Exception:
                pass

        if fmt == "DD/MM/YYYY, HH:MM":
            return dt.strftime("%d/%m/%Y, %H:%M")
        elif fmt == "YYYY-MM-DD HH:MM":
            return dt.strftime("%Y-%m-%d %H:%M")
        else:
            return dt.strftime("%m/%d/%Y, %H:%M")

    def _fmt_duration(self, start_str, end_str) -> str:
        """Return a human-friendly duration string like '1h 30m'."""
        if not start_str or not end_str:
            return ""
        from datetime import datetime, timedelta
        s = e = None
        for pattern in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M%z",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                s = datetime.strptime(str(start_str).strip(), pattern)
                e = datetime.strptime(str(end_str).strip(), pattern)
                break
            except ValueError:
                continue
        if s is None or e is None:
            return ""
        # Strip tz for subtraction if mixed
        if s.tzinfo is not None and e.tzinfo is None:
            e = e.replace(tzinfo=s.tzinfo)
        elif s.tzinfo is None and e.tzinfo is not None:
            s = s.replace(tzinfo=e.tzinfo)
        total_minutes = int((e - s).total_seconds() / 60)
        if total_minutes <= 0:
            return ""
        hours, mins = divmod(total_minutes, 60)
        if hours and mins:
            return f"{hours}h {mins}m"
        elif hours:
            return f"{hours}h"
        return f"{mins}m"

    async def _build_goal_project_context_async(self) -> str:
        """Build context block for active goals and projects (FR-7).

        Cache-backed: queries SQLite instead of globbing iCloud to avoid
        the project-candidate fan-out (584 files read only to be discarded).
        Always injected into chat LLM context, bypassing keyword relevance.
        Returns empty string if no active goals or projects exist.
        """
        import json as _json

        max_items = self._config.get("goals", {}).get("max_context_items", 5)
        lines = []

        # Active goals — one SQL query, no iCloud reads
        try:
            goal_rows = await self._cache.query_by_type("goal", status="active")
            goal_rows = goal_rows[:max_items]
            if goal_rows:
                lines.append("## Active Goals")
                for row in goal_rows:
                    try:
                        fm = _json.loads(row["frontmatter"]) if row.get("frontmatter") else {}
                        title = fm.get("source_title", row["filename"])
                        category = fm.get("category", "")
                        due = fm.get("due_date", "")
                        due_str = f" — due {due}" if due else ""
                        lines.append(f"- {title} [{category}]{due_str}")
                    except Exception:
                        continue
        except Exception:
            pass

        # Active + on-hold projects — one SQL query, no iCloud reads
        try:
            project_rows = await self._cache.query_by_type("project")
            active_projects = []
            for row in project_rows:
                try:
                    fm = _json.loads(row["frontmatter"]) if row.get("frontmatter") else {}
                    status = fm.get("status", "")
                    if status in ("active", "on-hold"):
                        active_projects.append(fm)
                except Exception:
                    continue

            active_projects = active_projects[:max_items]

            if active_projects:
                if lines:
                    lines.append("")
                lines.append("## Active Projects")
                for fm in active_projects:
                    title = fm.get("source_title", "")
                    category = fm.get("category", "")
                    due = fm.get("due_date", "")
                    milestones = fm.get("milestones", [])
                    done_count = sum(1 for m in milestones if m.get("done", False))
                    total_count = len(milestones)

                    due_str = f" — due {due}" if due else " — no due date"
                    milestone_str = f" (milestones: {done_count}/{total_count} done)" if milestones else ""
                    lines.append(f"- {title} [{category}]{due_str}{milestone_str}")
        except Exception:
            pass

        return "\n".join(lines) if lines else ""

    async def _load_context(self, query: str, history: list = None) -> str:
        """Load memory files into context with relevance sorting and token-aware budget."""
        parts = []
        budget_tokens = MAX_CONTEXT_TOKENS
        total_tokens = 0

        index_path = BRAIN_DIR / "index.md"
        if index_path.exists():
            try:
                text = await read_text_with_retry_async(index_path, default=None)
                if text is not None:
                    chunk = f"# Memory Index\n{text}"
                    chunk_tokens = self._count_tokens(chunk)
                    parts.append(chunk)
                    budget_tokens -= chunk_tokens
                    total_tokens += chunk_tokens
            except OSError:
                pass

        # FR-7: Inject active goals and projects before keyword-matched memories
        goal_context = await self._build_goal_project_context_async()
        if goal_context:
            goal_tokens = self._count_tokens(goal_context)
            parts.append(goal_context)
            budget_tokens -= goal_tokens
            total_tokens += goal_tokens

        # Augment short queries with recent user messages for better memory scoring
        score_query = query
        if history:
            recent_tokens = {w for w in re.findall(r'\b\w{3,}\b', query.lower())}
            if len(recent_tokens) < 3:
                recent_text = " ".join(
                    turn["content"] for turn in history[-10:]
                    if turn.get("role") == "user"
                )
                score_query = query + " " + recent_text

        # Score using cache keyword intersection (same algorithm as old _score_relevance)
        scored = await self._cache.score_keywords(score_query, top_n=50)

        for filename, score in scored:
            if budget_tokens <= 0:
                log.info(f"Chat context assembled: {len(parts)} files, ~{total_tokens} tokens")
                break

            entry = await self._cache.get(filename)
            if entry is None:
                continue

            text = entry["body"]
            text_tokens = self._count_tokens(text)
            if text_tokens > budget_tokens:
                # Truncate proportionally to fit remaining budget
                truncate_ratio = budget_tokens / text_tokens
                text = text[:int(len(text) * truncate_ratio)] + "\n[truncated]"
                text_tokens = budget_tokens
            parts.append(text)
            budget_tokens -= text_tokens
            total_tokens += text_tokens

        log.info(f"Chat context assembled: {len(parts)} files, ~{total_tokens} tokens")
        if not parts:
            return ""
        inner = "\n\n---\n\n".join(parts)
        return f"<memory-context>\n{inner}\n</memory-context>"

    def _count_tokens(self, text: str) -> int:
        """Count tokens using litellm.token_counter, fallback to char/4 heuristic."""
        try:
            import litellm
            return litellm.token_counter(model=self.model, text=text)
        except Exception as e:
            # Fallback: rough heuristic of 1 token ≈ 4 chars
            log.debug(f"Token counter failed, using char/4 fallback: {e}")
            return len(text) // 4

    def _trim_history_tokens(self, history: list) -> list:
        """Return a copy of history trimmed to HISTORY_TOKEN_BUDGET by dropping oldest turns.

        Drops one turn at a time from the front (not assumed pairs) to correctly handle
        standalone assistant notification turns inserted by the reconnect flow. Always
        keeps at least the last user turn and everything after it. After budget trimming,
        strips any remaining leading assistant turns so the API never receives history
        that starts with an assistant message.

        If there are no user turns at all (e.g. history contains only a reconnect
        notification), returns an empty list — the API must not receive assistant-only
        history.
        """
        trimmed = list(history)
        if not trimmed:
            return trimmed

        total = sum(self._count_tokens(m.get("content", "") or "") for m in trimmed)

        # Determine the minimum slice we must keep: from the last user turn onward.
        last_user_idx = next(
            (i for i in range(len(trimmed) - 1, -1, -1) if trimmed[i]["role"] == "user"),
            None,
        )
        if last_user_idx is None:
            # No user turn exists — entire history is assistant-only (e.g. a standalone
            # reconnect notification). Return empty so the API never sees it.
            return []
        min_keep = len(trimmed) - last_user_idx

        while total > self.HISTORY_TOKEN_BUDGET and len(trimmed) > min_keep:
            total -= self._count_tokens(trimmed[0].get("content", "") or "")
            trimmed = trimmed[1:]

        # Strip leading assistant turns (e.g. reconnect notifications) so the API
        # never sees a history that begins with an assistant message.
        while len(trimmed) > min_keep and trimmed[0]["role"] == "assistant":
            trimmed = trimmed[1:]

        return trimmed

    def _edit_skip_domains(self, action: str, domain: str):
        """Add or remove a domain from browser_watcher.skip_domains in config.yaml.

        Returns an error/info string if no change was made, or None on success.
        Writes atomically with exclusive flock to serialize concurrent writes.
        """
        config_path = BRAIN_DIR / "config.yaml"
        # Security (M4): acquire exclusive lock before read-modify-write
        with open(config_path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            config = yaml.safe_load(f.read())
            domains = config.setdefault("browser_watcher", {}).setdefault("skip_domains", [])
            if action == "add":
                if domain in domains:
                    return f"{domain} is already on the skip list."
                domains.append(domain)
            elif action == "remove":
                if domain not in domains:
                    return f"{domain} was not on the skip list."
                domains.remove(domain)
            # Atomic tmp→rename inside the lock
            tmp = config_path.with_suffix(".tmp")
            tmp.write_text(yaml.dump(config, default_flow_style=False))
            os.rename(tmp, config_path)
            # flock released on close (end of with block)
        return None

    def _url_matches_domain(self, url: str, domain: str) -> bool:
        """Check if a URL's hostname matches the given domain."""
        from urllib.parse import urlparse
        try:
            host = urlparse(url).hostname or ""
            return host == domain or host.endswith("." + domain)
        except Exception:
            return False

    async def _purge_domain(self, domain: str) -> int:
        """Delete all memory files whose source_url frontmatter contains domain.

        Returns the count of deleted files.
        """
        deleted = 0
        rows = await self._cache.query_all()
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                continue
            url = fm.get("source_url", "")
            if url and self._url_matches_domain(url, domain):
                filename = row.get("filename")
                if not filename:
                    continue
                target = (BRAIN_DIR / "memories") / filename
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                if self._cache:
                    await self._cache.invalidate(filename)
                deleted += 1
        return deleted

    # ── Telegram slash commands ───────────────────────────────────────────────

    def _check_auth(self, update: Update) -> bool:
        if update.effective_user.id != self.allowed_user_id:
            log.warning(f"Ignored command from unauthorised user_id={update.effective_user.id}")
            return False

        # Persist chat_id on first allowed message
        if self.notification_manager is not None:
            chat_id = update.effective_chat.id
            if self.notification_manager.get_chat_id() is None:
                self.notification_manager.set_chat_id(chat_id)
                log.info(f"Persisted chat_id {chat_id} for proactive notifications")

        return True

    def _load_history(self) -> dict:
        """Load persisted chat history from disk. Returns {int(chat_id): [...]}."""
        if self.HISTORY_FILE.exists():
            try:
                raw = json.loads(self.HISTORY_FILE.read_text())
                # Keys are strings in JSON; convert to int for chat_id lookup
                return {int(k): v for k, v in raw.items()}
            except Exception:
                log.warning("Failed to load chat history — starting fresh")
        return {}

    def _save_history(self):
        """Persist chat history to disk (atomic write)."""
        tmp = self.HISTORY_FILE.with_suffix(".tmp")
        try:
            # Convert int keys to strings for JSON
            data = {str(k): v for k, v in self._chat_history.items()}
            tmp.write_text(json.dumps(data, indent=2))
            os.rename(str(tmp), str(self.HISTORY_FILE))
        except Exception as e:
            log.warning("Failed to save chat history: %s", e)

    def _record_command_reply(self, chat_id: int, command: str, text: str) -> None:
        """Push a slash-command output into this chat's ring buffer (max 5 entries)."""
        buf = self._recent_commands.setdefault(chat_id, deque(maxlen=5))
        buf.append((command, text[:2000]))

    def _recent_commands_text(self, chat_id: int, limit: int = 5) -> str:
        """Return the last `limit` slash-command outputs formatted for LLM context."""
        buf = self._recent_commands.get(chat_id)
        if not buf:
            return "No recent slash commands in this session."
        entries = list(buf)[-limit:]
        parts = [f"/{cmd}\n{text}" for cmd, text in entries]
        return "\n\n---\n\n".join(parts)

    def _load_pending(self) -> dict:
        if self.PENDING_FILE.exists():
            try:
                return json.loads(self.PENDING_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_pending(self, state: dict):
        tmp = self.PENDING_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(self.PENDING_FILE))
        except Exception as e:
            log.warning("Failed to save pending-replies state: %s", e)

    def _queue_pending_reply(self, chat_id: int, query: str, response: str):
        state = self._load_pending()
        key = str(chat_id)
        entry = state.get(key) or {"pending": [], "summary_sent": False}
        entry["pending"].append({
            "query": query,
            "response": response[:8192],
            "queued_at": datetime.now().isoformat(timespec="seconds"),
        })
        entry["summary_sent"] = False
        state[key] = entry
        self._save_pending(state)
        log.warning("Queued undelivered reply for chat %s (now %d pending)", key, len(entry["pending"]))

    async def _is_telegram_reachable(self) -> bool:
        """Lightweight connectivity check — returns True if Telegram API is reachable."""
        try:
            await asyncio.wait_for(self.app.bot.get_me(), timeout=5.0)
            return True
        except Exception:
            return False

    async def _reconnect_loop(self, stop_event: asyncio.Event):
        """Poll for Telegram reachability every 30s; notify user when queue has pending replies."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30.0)
                return  # stop_event fired
            except asyncio.TimeoutError:
                pass  # normal 30s tick

            beat_status, beat_error = "ok", None
            try:
                state = self._load_pending()
                if not state:
                    hb.record_beat("reconnect_worker", beat_status, beat_error)
                    continue
                if not await self._is_telegram_reachable():
                    hb.record_beat("reconnect_worker", beat_status, beat_error)
                    continue

                for chat_id_str, entry in list(state.items()):
                    pending = entry.get("pending", [])
                    if not pending or entry.get("summary_sent"):
                        continue
                    count = len(pending)
                    notification_text = (
                        f"📬 Network is back. I have {count} response"
                        f"{'s' if count != 1 else ''} I couldn't deliver earlier.\n\n"
                        f"• /deliver — send them now\n"
                        f"• /discard — drop them"
                    )
                    try:
                        await self.app.bot.send_message(
                            chat_id=int(chat_id_str),
                            text=notification_text,
                        )
                        # Add to chat history so LLM has context when user responds
                        turns = self._chat_history.setdefault(int(chat_id_str), [])
                        turns.append({"role": "assistant", "content": notification_text})
                        max_msgs = self.HISTORY_WINDOW_TURNS * 2
                        if len(turns) > max_msgs:
                            self._chat_history[int(chat_id_str)] = turns[-max_msgs:]
                        self._save_history()

                        entry["summary_sent"] = True
                        state[chat_id_str] = entry
                        self._save_pending(state)
                        log.info("Notified chat %s of %d queued reply/replies", chat_id_str, count)
                    except Exception as e:
                        log.warning("Reconnect summary send failed for %s: %s", chat_id_str, e)
            except Exception as exc:
                log.exception("Reconnect worker iteration failed: %s", exc)
                beat_status, beat_error = "error", str(exc)

            hb.record_beat("reconnect_worker", beat_status, beat_error)

    async def _on_telegram_error(self, update, context: ContextTypes.DEFAULT_TYPE):
        """Catch all unhandled exceptions from handlers.

        Prevents python-telegram-bot from logging "No error handlers are registered"
        spam and gives a single actionable log line instead. Also tries to notify
        the user via effective_chat when possible.
        """
        log.error("Telegram handler error: %s", context.error, exc_info=context.error)
        chat = getattr(update, "effective_chat", None) if update else None
        if chat is not None:
            try:
                await context.bot.send_message(chat_id=chat.id, text="Internal error — check logs.")
            except Exception:
                pass

    async def cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /skip <domain>")
            return
        domain = context.args[0].lower()
        msg = self._edit_skip_domains("add", domain)
        if msg:
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(
                f"Added {domain} to skip list. Browser watcher will ignore it within 5 minutes."
            )

    async def cmd_unskip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /unskip <domain>")
            return
        domain = context.args[0].lower()
        msg = self._edit_skip_domains("remove", domain)
        if msg:
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(f"Removed {domain} from skip list.")

    async def cmd_skiplist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        from utils import load_config
        config = load_config(BRAIN_DIR / "config.yaml")
        domains = config.get("browser_watcher", {}).get("skip_domains", [])
        if not domains:
            await update.message.reply_text("Skip list is empty.")
            return
        lines = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(domains))
        await update.message.reply_text(f"Skipped domains:\n{lines}")

    # ── Deduplication commands ────────────────────────────────────────────────

    async def cmd_dupes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List candidate duplicate memories."""
        if not self._check_auth(update):
            return

        state_file = DEPLOY_DIR / "dedup-state.json"
        if not state_file.exists():
            await update.message.reply_text("No duplicate candidates found.")
            self._last_dupes_set = []
            return

        try:
            state = json.loads(state_file.read_text())
            candidates = state.get("candidates", [])
        except Exception as e:
            await update.message.reply_text(f"Error reading dedup state: {_safe_error(e)}")
            self._last_dupes_set = []
            return

        if not candidates:
            await update.message.reply_text("No duplicate candidates found.")
            self._last_dupes_set = []
            return

        self._last_dupes_set = candidates

        lines = [f"Found {len(candidates)} potential duplicate pairs:\n"]
        for i, cand in enumerate(candidates, 1):
            a = cand["a"]
            b = cand["b"]
            sim = cand["similarity"]
            lines.append(f"{i}. {a} ~ {b} (similarity: {sim:.2f})")

        lines.append("\nUse /merge N to merge pair N, or /keep N to dismiss as distinct.")
        await update.message.reply_text("\n".join(lines))

    async def cmd_merge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Merge duplicate pair N into one memory."""
        if not self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /merge N")
            return

        try:
            idx = int(context.args[0]) - 1
        except (ValueError, TypeError):
            await update.message.reply_text("Invalid index. Use /dupes to see the list.")
            return

        if not (0 <= idx < len(self._last_dupes_set)):
            await update.message.reply_text("Index out of range. Use /dupes to see the list.")
            return

        candidate = self._last_dupes_set[idx]
        filename_a = candidate["a"]
        filename_b = candidate["b"]

        memories_dir = BRAIN_DIR / "memories"
        path_a = memories_dir / filename_a
        path_b = memories_dir / filename_b

        if not path_a.exists() or not path_b.exists():
            await update.message.reply_text("One or both files no longer exist.")
            return

        try:
            len_a = len(path_a.read_text())
            len_b = len(path_b.read_text())
            if len_a >= len_b:
                keeper, deleter = path_a, path_b
                keeper_name, deleter_name = filename_a, filename_b
            else:
                keeper, deleter = path_b, path_a
                keeper_name, deleter_name = filename_b, filename_a

            fm_keeper = self._parse_frontmatter(keeper)
            fm_deleter = self._parse_frontmatter(deleter)

            tags_keeper = set(fm_keeper.get("tags", []))
            tags_deleter = set(fm_deleter.get("tags", []))
            union_tags = sorted(tags_keeper | tags_deleter)

            if union_tags != sorted(tags_keeper):
                keeper_text = keeper.read_text()
                m = re.match(r"^(---\n)(.*?)(\n---)", keeper_text, re.DOTALL)
                if m:
                    fm_keeper["tags"] = union_tags
                    new_frontmatter = yaml.dump(fm_keeper, default_flow_style=False, allow_unicode=True)
                    updated_text = f"---\n{new_frontmatter}---{keeper_text[m.end():]}"
                    keeper.write_text(updated_text)

            deleter.unlink()

            state_file = DEPLOY_DIR / "dedup-state.json"
            state = json.loads(state_file.read_text())
            state["candidates"] = [c for c in state["candidates"] if c != candidate]
            state_file.write_text(json.dumps(state, indent=2))

            await update.message.reply_text(
                f"Merged: kept {keeper_name}, deleted {deleter_name}.\n"
                f"Tags merged: {', '.join(union_tags)}"
            )
        except Exception as e:
            log.error("Error merging duplicates: %s", e)
            await update.message.reply_text(f"Error merging files: {_safe_error(e)}")

    async def cmd_keep(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dismiss duplicate pair N as intentionally distinct."""
        if not self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /keep N")
            return

        try:
            idx = int(context.args[0]) - 1
        except (ValueError, TypeError):
            await update.message.reply_text("Invalid index. Use /dupes to see the list.")
            return

        if not (0 <= idx < len(self._last_dupes_set)):
            await update.message.reply_text("Index out of range. Use /dupes to see the list.")
            return

        candidate = self._last_dupes_set[idx]

        state_file = DEPLOY_DIR / "dedup-state.json"
        try:
            state = json.loads(state_file.read_text())
            state["candidates"] = [c for c in state["candidates"] if c != candidate]
            state.setdefault("dismissed", []).append(candidate)
            state_file.write_text(json.dumps(state, indent=2))

            await update.message.reply_text(
                f"Dismissed pair as distinct: {candidate['a']} and {candidate['b']}"
            )
        except Exception as e:
            log.error("Error dismissing duplicate: %s", e)
            await update.message.reply_text(f"Error dismissing pair: {_safe_error(e)}")

    # ── Watchlist commands ────────────────────────────────────────────────────

    async def cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create a watchlist: /watch "topic" [from:person] [type:email|slack|meeting]"""
        if not self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text(
                'Usage: /watch "topic" [from:person] [type:email|slack|meeting]\n'
                'Examples:\n'
                '  /watch "API redesign"\n'
                '  /watch "budget approval" from:Sarah\n'
                '  /watch "deployment" type:email'
            )
            return

        full_text = " ".join(context.args)

        topic_match = re.search(r'"([^"]+)"', full_text)
        if not topic_match:
            await update.message.reply_text(
                'Topic must be quoted. Example: /watch "API redesign" from:Sarah'
            )
            return

        topic = topic_match.group(1).strip()

        person = None
        person_match = re.search(r'from:(\S+)', full_text, re.IGNORECASE)
        if person_match:
            person = person_match.group(1).strip()

        watch_type = "any"
        type_match = re.search(r'type:(email|slack|meeting)', full_text, re.IGNORECASE)
        if type_match:
            watch_type = type_match.group(1).lower()

        import hashlib
        watchlist_id = hashlib.sha1(
            f"{topic}{person or ''}{watch_type}{datetime.now(timezone.utc).isoformat()}".encode()
        ).hexdigest()[:6]

        slug = re.sub(r'[^a-z0-9]+', '-', topic.lower())[:40].strip('-')

        fm = {
            "type": "watchlist",
            "id": watchlist_id,
            "title": f"Watch for: {topic}",
            "status": "active",
            "topic": topic,
            "watch_type": watch_type,
            "created": datetime.now(timezone.utc).isoformat(),
            "expires": None,
        }
        if person:
            fm["person"] = person

        person_part = f" from {person}" if person else ""
        type_part = f" ({watch_type})" if watch_type != "any" else ""
        body = f"Watching for {topic}{person_part}{type_part}"

        filename = f"watchlist-{slug}-{watchlist_id}.md"
        memory_path = BRAIN_DIR / "memories" / filename

        frontmatter_str = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        content = f"---\n{frontmatter_str}---\n\n{body}\n"

        tmp_path = memory_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(memory_path))

            person_msg = f" from {person}" if person else ""
            type_msg = f" (type: {watch_type})" if watch_type != "any" else ""
            await update.message.reply_text(
                f"Watching for: {topic}{person_msg}{type_msg}"
            )
        except Exception as e:
            log.exception("Failed to create watchlist")
            await update.message.reply_text(f"Failed to create watchlist: {_safe_error(e)}")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    async def cmd_watches(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List active watchlists."""
        if not self._check_auth(update):
            return

        watchlist_rows = await self._cache.query_by_prefix("watchlist-")
        if not watchlist_rows:
            await update.message.reply_text("No watchlists found.")
            return

        watchlists = []
        memories_dir = BRAIN_DIR / "memories"
        for row in watchlist_rows:
            filename = row.get("filename")
            if not filename:
                continue
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
                status = fm.get("status", "")
                if status in ("active", "triggered"):
                    watchlists.append((memories_dir / filename, fm))
            except Exception:
                log.warning("Failed to parse watchlist %s", filename)

        if not watchlists:
            await update.message.reply_text("No active watchlists.")
            return

        watchlists.sort(key=lambda x: x[1].get("created", ""), reverse=True)
        self._last_watchlist_set = [wl[0] for wl in watchlists]

        lines = [f"Active watchlists ({len(watchlists)}):"]
        for i, (wl_path, fm) in enumerate(watchlists, 1):
            topic = fm.get("topic", "")
            person = fm.get("person")
            watch_type = fm.get("watch_type", "any")
            status = fm.get("status", "")

            person_part = f" from {person}" if person else ""
            type_part = f" [{watch_type}]" if watch_type != "any" else ""
            status_mark = " (triggered)" if status == "triggered" else ""

            lines.append(f"{i}. {topic}{person_part}{type_part}{status_mark}")

        await self._send_reply(update, "\n".join(lines))

    async def cmd_unwatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Deactivate watchlist N."""
        if not self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /unwatch N")
            return

        try:
            n = int(context.args[0])
        except (ValueError, TypeError):
            await update.message.reply_text("N must be a number. Use /watches to see the list.")
            return

        if not self._last_watchlist_set:
            await update.message.reply_text("Use /watches first to see the list.")
            return

        if not (1 <= n <= len(self._last_watchlist_set)):
            await update.message.reply_text(
                f"Index {n} out of range. You have {len(self._last_watchlist_set)} watchlist(s)."
            )
            return

        watchlist_path = self._last_watchlist_set[n - 1]
        try:
            fm = self._parse_frontmatter(watchlist_path)
            fm["status"] = "expired"

            text = watchlist_path.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            body = parts[2].strip() if len(parts) >= 3 else ""

            frontmatter_str = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
            content = f"---\n{frontmatter_str}---\n\n{body}\n"

            tmp_path = watchlist_path.with_suffix(".tmp")
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(watchlist_path))

            topic = fm.get("topic", "")
            await update.message.reply_text(f"Deactivated watchlist: {topic}")
        except Exception as e:
            log.exception("Failed to deactivate watchlist")
            await update.message.reply_text(f"Failed to deactivate watchlist: {_safe_error(e)}")

    # ── Import commands ───────────────────────────────────────────────────────

    async def cmd_import_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Import ChatGPT or Claude conversation export: /import_chats (with file attached)."""
        if not self._check_auth(update):
            return

        if not update.message.document:
            await update.message.reply_text(
                "Usage: /import_chats (attach a ChatGPT or Claude export file)\n\n"
                "Supported formats:\n"
                "• ChatGPT: ZIP containing conversations.json (Settings → Data Controls → Export)\n"
                "• Claude: ZIP or JSON export from claude.ai account settings\n\n"
                "Send this command with a file attached."
            )
            return

        try:
            await update.message.reply_text("Downloading file...")
            file = await context.bot.get_file(update.message.document.file_id)
            file_bytes = bytes(await file.download_as_bytearray())
            filename = update.message.document.file_name or "import.json"

            await update.message.reply_text(f"Processing {filename}...")

            from llm_chat_importer import import_file
            written = import_file(file_bytes, filename, BRAIN_DIR / "memories")

            if not written:
                await update.message.reply_text(
                    "No conversations found in the file. "
                    "Make sure it's a valid ChatGPT or Claude export."
                )
                return

            platform = "unknown"
            if written[0].startswith("llm-chat-chatgpt-"):
                platform = "ChatGPT"
            elif written[0].startswith("llm-chat-claude-"):
                platform = "Claude"

            suffix = "s" if len(written) != 1 else ""
            extra = f"\n... and {len(written) - 5} more" if len(written) > 5 else ""
            await update.message.reply_text(
                f"Imported {len(written)} conversation{suffix} from {platform}\n\n"
                f"Files written:\n" +
                "\n".join(f"• {f}" for f in written[:5]) + extra
            )
        except ValueError as e:
            await update.message.reply_text(f"Import failed: {_safe_error(e)}")
        except Exception as e:
            log.exception("cmd_import_chats failed")
            await update.message.reply_text(f"Import failed: {_safe_error(e)}")

    # ── Memory management helpers ─────────────────────────────────────────────

    def _parse_frontmatter(self, path: Path) -> dict:
        """Parse YAML frontmatter from a memory file. Returns {} on any failure."""
        try:
            text = path.read_text()
        except Exception:
            return {}
        m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not m:
            return {}
        try:
            return yaml.safe_load(m.group(1)) or {}
        except Exception:
            return {}

    def _resolve_index(self, n: str) -> Path:
        """Convert 1-based index string to a Path from _active_list, or None."""
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._active_list):
            return self._active_list[idx]
        return None

    def _fmt_memory_line(self, i: int, fm: dict) -> str:
        title = (fm.get("source_title") or "(no title)")[:60]
        date = (fm.get("created") or "")[:10]
        return f"{i}. {title}  ({date})"

    # ── /readings command ─────────────────────────────────────────────────────

    async def _list_readings_text(self, limit: int = 10) -> str:
        """Return formatted readings list text (called by cmd_readings and tool dispatch)."""
        limit = max(1, min(limit, 50))
        rows = await self._cache.query_all()
        if not rows:
            return "No memories found."

        # Sort by mtime descending — cache rows already carry mtime
        rows.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
        rows = rows[:limit]
        memories_dir = BRAIN_DIR / "memories"
        paths = [memories_dir / r["filename"] for r in rows if r.get("filename")]
        self._last_results = paths
        self._active_list = paths

        lines = [f"Your {len(rows)} most recent memories:"]
        for i, row in enumerate(rows, 1):
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                fm = {}
            lines.append(self._fmt_memory_line(i, fm))
        return "\n".join(lines)

    async def cmd_readings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        try:
            limit = int(context.args[0]) if context.args else 10
        except (ValueError, IndexError):
            limit = 10
        text = await self._list_readings_text(limit)
        await update.message.reply_text(text)

    # ── /search command ───────────────────────────────────────────────────────

    # Maps type-filter keyword → set of frontmatter `type` values (None = no type field)
    _SEARCH_TYPE_FILTERS: dict = {
        "email":      {"email_thread"},
        "slack":      {"slack_thread"},
        "meeting":    {"meeting_transcript"},
        "project":    {"project"},
        "commitment": {"commitment"},
        "event":      {"calendar_event"},
        "contact":    {"contact"},
        "web":        {None},
    }

    # Display order for grouped search results
    _SEARCH_GROUP_ORDER: list = [
        ("contact",        "Contacts"),
        ("commitment",     "Commitments"),
        ("project",        "Projects"),
        ("meeting_transcript", "Meetings"),
        ("email_thread",   "Email threads"),
        ("slack_thread",   "Slack threads"),
        ("calendar_event", "Calendar events"),
        (None,             "Web memories"),
    ]

    _SEARCH_TYPE_TO_KEYWORD: dict = {
        "email_thread":      "email",
        "slack_thread":      "slack",
        "meeting_transcript":"meeting",
        "project":           "project",
        "commitment":        "commitment",
        "calendar_event":    "event",
        "contact":           "contact",
    }

    def _fmt_search_line(self, i: int, fm: dict, mem_type) -> str:
        """Format a single search result line based on memory type."""
        if mem_type == "contact":
            name = (fm.get("name") or fm.get("source_title") or "Unknown")[:50]
            last = fm.get("last_interaction", "")
            last_str = f" · last seen {str(last)[:10]}" if last else ""
            return f"  {i}. {name}{last_str}"
        if mem_type == "commitment":
            title = (fm.get("title") or fm.get("source_title") or "Untitled")[:55]
            ctype = fm.get("commitment_type", "")
            tag = f"[{ctype}] " if ctype else ""
            return f"  {i}. {tag}{title}"
        if mem_type == "project":
            name = (fm.get("name") or fm.get("source_title") or "Untitled")[:40]
            cat = fm.get("category", "")
            last_commit = str(fm.get("last_commit", ""))[:10]
            cat_str = f" [{cat}]" if cat else ""
            date_str = f" · last commit {last_commit}" if last_commit else ""
            return f"  {i}. {name}{cat_str}{date_str}"
        if mem_type == "meeting_transcript":
            title = (fm.get("source_title") or "Untitled")[:45]
            date = str(fm.get("meeting_date") or fm.get("created") or "")[:10]
            return f"  {i}. {title} · {date}"
        if mem_type in ("email_thread", "slack_thread"):
            title = (fm.get("source_title") or fm.get("subject") or "Untitled")[:45]
            date = str(fm.get("last_message") or fm.get("last_reply") or fm.get("created") or "")[:10]
            return f"  {i}. {title} · {date}"
        if mem_type == "calendar_event":
            title = (fm.get("source_title") or "Untitled")[:45]
            start = str(fm.get("start_time") or fm.get("created") or "")[:10]
            return f"  {i}. {title} · {start}"
        # Web memory / default
        title = (fm.get("source_title") or "(no title)")[:55]
        date = str(fm.get("created") or "")[:10]
        return f"  {i}. {title}  ({date})"

    async def _search_memories_text(self, query: str, type_filter: Optional[str] = None) -> str:
        """Return formatted search results text (called by cmd_search and tool dispatch).
        type_filter is the keyword string like "email", "meeting", not the set."""
        # Resolve type_filter keyword to set
        filter_set: Optional[set] = None
        if type_filter:
            filter_set = self._SEARCH_TYPE_FILTERS.get(type_filter.lower())
            if filter_set is None:
                return f"Unknown type filter: {type_filter!r}. Valid types: email, slack, meeting, project, commitment, event, contact, web"

        memories_dir = BRAIN_DIR / "memories"
        # cache.score_keywords mirrors _score_relevance against the cached
        # header500 column — one SQL scan instead of N file reads.
        scored_pairs = await self._cache.score_keywords(query, top_n=50)
        rows_by_name: dict[str, dict] = {}
        for filename, _score in scored_pairs:
            row = await self._cache.get(filename)
            if row is not None:
                rows_by_name[filename] = row

        matches = []
        for filename, score in scored_pairs:
            row = rows_by_name.get(filename)
            if row is None:
                continue
            mtime = row.get("mtime") or 0
            matches.append((int(score), mtime, memories_dir / filename, row))

        # Cap at top 50 (score_keywords already limits, but keep tuple shape)
        matches.sort(key=lambda t: (t[0], t[1]), reverse=True)
        matches = matches[:50]

        if not matches:
            return f"No memories match '{query}'."

        def _row_fm(row: dict) -> dict:
            try:
                return json.loads(row.get("frontmatter") or "{}")
            except Exception:
                return {}

        # Apply type filter if specified
        if filter_set is not None:
            matches = [(s, mt, f, row) for s, mt, f, row in matches if _row_fm(row).get("type") in filter_set]
            if not matches:
                return f"No {type_filter} memories match '{query}'."

        if filter_set is not None:
            # Flat list for filtered mode
            self._last_results = [f for _, _, f, _ in matches]
            self._active_list = self._last_results
            lines = [f"Search results for \"{query}\" ({type_filter}) — {len(matches)} match{'es' if len(matches) != 1 else ''}:"]
            for i, (_, _, f, row) in enumerate(matches, 1):
                fm = _row_fm(row)
                mem_type = fm.get("type") or None
                lines.append(self._fmt_search_line(i, fm, mem_type))
            lines.append("\nUse /reading N for detail on any item.")
            return "\n".join(lines)

        # Grouped mode: assign global indices in group-display order
        # Build lookup: path → (score, mtime, fm, type)
        path_data: dict = {}
        for s, mt, f, row in matches:
            fm = _row_fm(row)
            path_data[f] = (s, mt, fm, fm.get("type") or None)

        # Bucket by type
        buckets: dict = {key: [] for key, _ in self._SEARCH_GROUP_ORDER}
        for f, (s, mt, fm, mem_type) in path_data.items():
            if mem_type in buckets:
                buckets[mem_type].append((s, mt, f, fm))
            else:
                buckets[None].append((s, mt, f, fm))

        # Build ordered result list and reply
        ordered_paths: list = []
        lines = [f"Search results for \"{query}\" — {len(matches)} match{'es' if len(matches) != 1 else ''}:"]

        MAX_PER_GROUP = 5
        for type_key, group_label in self._SEARCH_GROUP_ORDER:
            items = buckets.get(type_key, [])
            if not items:
                continue
            items.sort(key=lambda t: (t[0], t[1]), reverse=True)
            lines.append(f"\n{group_label} ({len(items)})")
            shown = items[:MAX_PER_GROUP]
            for s, mt, f, fm in shown:
                idx = len(ordered_paths) + 1
                ordered_paths.append(f)
                lines.append(self._fmt_search_line(idx, fm, type_key))
            if len(items) > MAX_PER_GROUP:
                overflow = len(items) - MAX_PER_GROUP
                kw = self._SEARCH_TYPE_TO_KEYWORD.get(type_key, "web") if type_key else "web"
                lines.append(f"  … and {overflow} more — /search {kw} {query}")
            # Add overflow items to _last_results even though not shown
            for _, _, f, _ in items[MAX_PER_GROUP:]:
                ordered_paths.append(f)

        self._last_results = ordered_paths
        self._active_list = ordered_paths
        lines.append("\nUse /reading N for detail on any item.")

        return "\n".join(lines)

    async def cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: /search <query>\n"
                "       /search <type> <query>  (types: email, slack, meeting, project, commitment, event, contact, web)"
            )
            return

        # Detect optional type-filter prefix
        filter_keyword: Optional[str] = None
        if context.args[0].lower() in self._SEARCH_TYPE_FILTERS:
            if len(context.args) < 2:
                await update.message.reply_text(
                    f"Usage: /search {context.args[0].lower()} <query>"
                )
                return
            filter_keyword = context.args[0].lower()
            query = " ".join(context.args[1:])
        else:
            query = " ".join(context.args)

        text = await self._search_memories_text(query, filter_keyword)
        await self._send_reply(update, text)

    # ── /reading command ──────────────────────────────────────────────────────

    async def cmd_reading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /reading <N>")
            return

        path = self._resolve_index(context.args[0])
        if path is None:
            await update.message.reply_text(self._format_group_help("Knowledge listings", "reading"))
            return

        fm = self._parse_frontmatter(path)
        title = fm.get("source_title") or "(no title)"
        url = fm.get("source_url") or ""
        date = (fm.get("created") or "")[:10]
        summary = fm.get("summary") or ""
        tags = fm.get("tags") or []
        tag_str = f"\nTags: {', '.join(tags)}" if tags else ""

        lines = [f"{title}", f"{url}", f"Date: {date}{tag_str}", "", summary]
        await update.message.reply_text("\n".join(lines))

    # ── /deepen command ───────────────────────────────────────────────────────

    async def cmd_deepen(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Re-process an existing reading memory with the deep analysis skill."""
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /deepen N")
            return

        path = self._resolve_index(context.args[0])
        if path is None:
            await update.message.reply_text("Invalid index. Run /readings first.")
            return

        fm = self._parse_frontmatter(path)
        source_url = fm.get("source_url", "")
        if not source_url or source_url.startswith("file://"):
            await update.message.reply_text("Can only deepen URL-sourced memories.")
            return

        await update.message.reply_text("🔍 Re-processing with deep analysis…")
        try:
            from content_fetcher import fetch_url_content
            from memory_writer import MemoryWriter
            title, text = await fetch_url_content(source_url)
            if not text or len(text.strip()) < 100:
                await update.message.reply_text("Couldn't fetch content from the source URL.")
                return

            deep_executor = SkillExecutor("summarize-deep")
            response = await deep_executor.run({"content": text, "url": source_url})
            if not response:
                await update.message.reply_text("Deep analysis failed — check error.log.")
                return

            await MemoryWriter().write(
                {"url": source_url, "title": title or fm.get("source_title", ""), "browser": "telegram"},
                response,
                depth="deep",
            )
            await update.message.reply_text(
                f"📚 Deepened: {title or fm.get('source_title', '')}"
            )
        except SkillAuthError as e:
            log.error("cmd_deepen: %s", e)
            await update.message.reply_text(f"Deep analysis failed — invalid API credentials. Check your API keys.")
        except Exception as e:
            log.exception("cmd_deepen failed")
            await update.message.reply_text(f"Error: {_safe_error(e)}")

    # ── /forget command ───────────────────────────────────────────────────────

    async def _forget_indices(self, update: Update, index_args) -> None:
        """Delete items at the given 1-based index strings from _active_list."""
        snapshot = list(self._active_list)

        indices_to_remove = []
        for arg in index_args:
            if not str(arg).isdigit():
                await update.message.reply_text(
                    f"Invalid argument '{arg}' — all arguments must be numbers when using index mode."
                )
                return
            try:
                idx = int(arg) - 1
            except (ValueError, TypeError):
                await update.message.reply_text(f"Invalid index '{arg}'.")
                return
            if 0 <= idx < len(snapshot):
                indices_to_remove.append(idx)
            else:
                await update.message.reply_text(
                    f"Index {arg} out of range (1-{len(snapshot)}). Run a list command first."
                )
                return

        indices_to_remove = sorted(set(indices_to_remove))

        successes = []
        failures = []
        for idx in indices_to_remove:
            path = snapshot[idx]
            try:
                path.unlink()
                successes.append((idx + 1, path))
            except FileNotFoundError:
                successes.append((idx + 1, path))
            except Exception:
                failures.append(idx + 1)

        for _, path in successes:
            try:
                self._active_list.remove(path)
            except ValueError:
                pass

        if not failures:
            if len(successes) == 1:
                fm = self._parse_frontmatter(successes[0][1])
                title = fm.get("source_title") or fm.get("title") or successes[0][1].name
                await update.message.reply_text(f"Forgotten: {title}")
            else:
                await update.message.reply_text(f"Forgot {len(successes)} items.")
        else:
            failed_str = ", ".join(f"#{n}" for n in failures)
            await update.message.reply_text(
                f"Forgot {len(successes)} of {len(successes) + len(failures)} (failed: {failed_str})."
            )

    async def cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text(
                "Usage:\n"
                "  /forget <N> [N...] — forget item(s) N from your last list\n"
                "  /forget <domain>   — forget all web captures from a domain"
            )
            return

        if context.args[0].isdigit():
            await self._forget_indices(update, context.args)
            return

        # Non-numeric → domain purge
        domain = context.args[0].lower()
        count = await self._purge_domain(domain)
        if count:
            await update.message.reply_text(f"Forgotten {count} captures from {domain}.")
        else:
            await update.message.reply_text(f"No captures found for {domain}.")

    # ── /commitments command ──────────────────────────────────────────────────

    async def _load_active_commitments(self, type_filter: str = None) -> list:
        """Return (path, frontmatter) pairs for active commitment files."""
        results = []
        rows = await self._cache.query_by_prefix("commitment-")
        rows = sorted(rows, key=lambda r: r["filename"])
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                fm = {}
            if fm.get("type") != "commitment":
                continue
            if fm.get("status") != "active":
                continue
            if type_filter:
                ct = fm.get("commitment_type", "")
                wanted = "waiting_on" if type_filter.lower() == "waiting" else type_filter.lower()
                if ct != wanted:
                    continue
            f = BRAIN_DIR / "memories" / row["filename"]
            results.append((f, fm))

        def _sort_key(item):
            _, fm = item
            due = fm.get("due_date")
            # Nulls last: (0, date_str) for known dates, (1, "") for unknown
            if due:
                return (0, str(due))
            return (1, "")

        results.sort(key=_sort_key)
        return results

    async def _list_commitments_text(self, limit: int = 20) -> str:
        """Return formatted commitments list text (called by cmd_commitments and tool dispatch)."""
        limit = max(1, min(limit, 100))
        items = await self._load_active_commitments(type_filter=None)

        if not items:
            return "No active commitments."

        self._last_commitment_set = [f for f, _ in items]
        self._active_list = self._last_commitment_set
        total = len(items)
        lines = [f"Active commitments ({total} total):"]

        today = datetime.now().date()
        for i, (_, fm) in enumerate(items[:limit], 1):
            ct = fm.get("commitment_type", "outbound")
            desc = (fm.get("source_title") or fm.get("summary") or "")[:50]
            owner = fm.get("owner", "")
            due = fm.get("due_date")
            if due:
                try:
                    overdue = datetime.strptime(str(due), "%Y-%m-%d").date() < today
                except ValueError:
                    overdue = False
                due_str = f" — was due {due} ⚠️" if overdue else f" — due {due}"
            else:
                due_str = " — due unknown"
            needs_review = "needs-review" in (fm.get("tags") or [])
            flag = " ⚠️" if needs_review and not due_str.endswith("⚠️") else ""
            owner_str = f" — {owner}" if owner and ct != "personal" else ""
            lines.append(f"{i}. [{ct}] {desc}{owner_str}{due_str}{flag}")

        if total > limit:
            lines.append(f"... and {total - limit} more.")

        lines.append("\nUse /complete N or /dismiss N to update status.")
        return "\n".join(lines)

    async def _close_commitment_text(self, index: int = None, title: str = None, status: str = "completed") -> str:
        """Mark a commitment completed or dismissed. Used by close_commitment tool."""
        if status not in ("completed", "dismissed"):
            return f"Invalid status {status!r}. Use 'completed' or 'dismissed'."

        from commitment_tracker import CommitmentTracker

        # Resolve by 1-based index into _last_commitment_set
        if index is not None:
            path = self._resolve_commitment_index(str(index))
            if path is None:
                return f"No commitment at index {index} (run list_commitments first to refresh)."
        elif title:
            # Search active commitments by title substring
            items = await self._load_active_commitments()
            hits = [
                f for f, fm in items
                if title.lower() in (fm.get("source_title") or fm.get("summary") or "").lower()
            ]
            if not hits:
                return f"No active commitment matching '{title}'."
            if len(hits) > 1:
                lines = ["Multiple matches — be more specific:"]
                for h in hits[:5]:
                    fm = self._parse_frontmatter(h)
                    lines.append(f"• {(fm.get('source_title') or fm.get('summary') or '')[:60]}")
                return "\n".join(lines)
            path = hits[0]
        else:
            return "Provide either index or title."

        fm = self._parse_frontmatter(path)
        label = (fm.get("source_title") or fm.get("summary") or str(path.name))[:60]
        try:
            await CommitmentTracker(cache=self._cache).update_commitment_status(path, status)
            mark = "✓" if status == "completed" else "✕"
            return f"{mark} {label} → {status}"
        except Exception as e:
            return f"Error updating commitment: {e}"

    async def _close_issue_text(self, short_id=None, title=None, status="done") -> str:
        """Close or update a bug/feature request. Used by close_issue tool."""
        # Tool enum uses underscores; internals (GH labels, local frontmatter) use hyphens.
        status = {"wont_do": "wont-do", "in_progress": "in-progress"}.get(status, status)
        rows = await self._cache.query_by_prefix("feature-request-")
        match_row = None
        match_fm = None

        if short_id:
            for row in rows:
                try:
                    fm = json.loads(row.get("frontmatter") or "{}")
                except Exception:
                    fm = {}
                if fm.get("short_id") == short_id:
                    match_row = row
                    match_fm = fm
                    break
            if match_row is None:
                return f"No issue found with ID '{short_id}'."

        elif title:
            hits = []
            for row in rows:
                try:
                    fm = json.loads(row.get("frontmatter") or "{}")
                except Exception:
                    fm = {}
                if title.lower() in (fm.get("title") or "").lower():
                    hits.append((row, fm))
            if not hits:
                return f"No issue found matching '{title}'."
            if len(hits) > 1:
                lines = ["Multiple matches — be more specific:"]
                for _, fm in hits[:5]:
                    lines.append(f"• [{fm.get('short_id')}] {(fm.get('title') or '')[:60]}")
                return "\n".join(lines)
            match_row, match_fm = hits[0]
        else:
            return "Provide either short_id or title."

        match = BRAIN_DIR / "memories" / match_row["filename"]
        fm = match_fm

        # Sync to GitHub first (before touching local) so the two stores stay consistent.
        # If GitHub rejects the update, return an error without mutating the local file.
        gh_number = fm.get("github_issue_number")
        if gh_number:
            if not self.github.enabled:
                return (
                    f"Cannot update GitHub issue #{gh_number}: "
                    "GitHub integration not configured (GITHUB_PAT / GITHUB_REPO missing)"
                )
            try:
                await self._gh_set_status(gh_number, status)
            except Exception as gh_e:
                return f"GitHub sync failed for #{gh_number}: {gh_e}"

        try:
            text = match_row.get("body") or ""
            if not text:
                text = match.read_text()
            updated = re.sub(r'^status:\s*\S+', f'status: {status}', text, flags=re.MULTILINE)
            tmp = match.with_suffix(".tmp")
            tmp.write_text(updated)
            os.rename(str(tmp), str(match))
            await self._cache.invalidate(match.name)
            return f"Closed [{fm.get('short_id')}] {(fm.get('title') or '')[:60]} → {status}"
        except Exception as e:
            return f"Error updating issue: {e}"

    async def _update_issue_priority_text(self, short_id=None, title=None, priority="medium") -> str:
        """Update the priority of a bug/feature request. Used by update_issue_priority tool."""
        if priority not in ("low", "medium", "high", "critical"):
            return f"Invalid priority '{priority}'. Must be: low, medium, high, or critical."
        rows = await self._cache.query_by_prefix("feature-request-")
        match_row = None
        match_fm = None

        if short_id:
            for row in rows:
                try:
                    fm = json.loads(row.get("frontmatter") or "{}")
                except Exception:
                    fm = {}
                if fm.get("short_id") == short_id:
                    match_row = row
                    match_fm = fm
                    break
            if match_row is None:
                return f"No issue found with ID '{short_id}'."

        elif title:
            hits = []
            for row in rows:
                try:
                    fm = json.loads(row.get("frontmatter") or "{}")
                except Exception:
                    fm = {}
                if title.lower() in (fm.get("title") or "").lower():
                    hits.append((row, fm))
            if not hits:
                return f"No issue found matching '{title}'."
            if len(hits) > 1:
                lines = ["Multiple matches — be more specific:"]
                for _, fm in hits[:5]:
                    lines.append(f"• [{fm.get('short_id')}] {(fm.get('title') or '')[:60]}")
                return "\n".join(lines)
            match_row, match_fm = hits[0]
        else:
            return "Provide either short_id or title."

        match = BRAIN_DIR / "memories" / match_row["filename"]
        fm = match_fm
        old_priority = fm.get("priority", "medium")

        gh_number = fm.get("github_issue_number")
        if gh_number:
            if not self.github.enabled:
                return (
                    f"Cannot update GitHub issue #{gh_number}: "
                    "GitHub integration not configured (GITHUB_PAT / GITHUB_REPO missing)"
                )
            try:
                await self._gh_set_priority(gh_number, priority)
                await self._rewrite_features_index_snapshot()
            except Exception as gh_e:
                return f"GitHub sync failed for #{gh_number}: {gh_e}"

        try:
            self._rewrite_feature_frontmatter(match, {"priority": priority})
            await self._cache.invalidate(match.name)
            return (
                f"Priority updated: [{fm.get('short_id')}] "
                f"{(fm.get('title') or '')[:60]} → {old_priority} → {priority}"
            )
        except Exception as e:
            return f"Error updating issue: {e}"

    def _close_goal_text(self, title: str, status: str = "completed") -> str:
        """Complete or abandon a goal by title substring. Used by close_goal tool."""
        if not title or not title.strip():
            return "Please specify a goal title to close."
        valid = {"completed", "abandoned"}
        if status not in valid:
            return f"Invalid status '{status}'. Use: completed, abandoned"
        goals = self._goal_manager.list_goals(status=None)
        hits = [
            p for p in goals
            if title.lower() in (self._parse_frontmatter(p).get("source_title") or "").lower()
        ]
        if not hits:
            return f"No goal found matching '{title}'."
        if len(hits) > 1:
            lines = ["Multiple goals match — be more specific:"]
            for h in hits[:5]:
                fm = self._parse_frontmatter(h)
                lines.append(f"• [{fm.get('status','?')}] {fm.get('source_title','?')[:60]}")
            return "\n".join(lines)
        path = hits[0]
        fm = self._parse_frontmatter(path)
        goal_title = fm.get("source_title", path.stem)
        current_status = fm.get("status")
        if current_status == status:
            verb = "completed" if status == "completed" else "abandoned"
            return f"Goal was already {verb}: \"{goal_title}\""
        try:
            self._goal_manager.update_goal_status(path, status)
            verb = "completed" if status == "completed" else "abandoned"
            return f"Goal {verb}: \"{goal_title}\""
        except ValueError as e:
            return f"Error: {e}"

    def _close_project_text(self, title: str, status: str = "completed") -> str:
        """Complete, abandon, or put a project on hold by title substring. Used by close_project tool."""
        if not title or not title.strip():
            return "Please specify a project title to close."
        status = {"on_hold": "on-hold"}.get(status, status)
        valid = {"completed", "abandoned", "on-hold"}
        if status not in valid:
            return f"Invalid status '{status}'. Use: completed, abandoned, on_hold"
        projects = self._goal_manager.list_projects(status=None)
        hits = [
            p for p in projects
            if title.lower() in (self._parse_frontmatter(p).get("source_title") or "").lower()
        ]
        if not hits:
            return f"No project found matching '{title}'."
        if len(hits) > 1:
            lines = ["Multiple projects match — be more specific:"]
            for h in hits[:5]:
                fm = self._parse_frontmatter(h)
                lines.append(f"• [{fm.get('status','?')}] {fm.get('source_title','?')[:60]}")
            return "\n".join(lines)
        path = hits[0]
        fm = self._parse_frontmatter(path)
        project_title = fm.get("source_title", path.stem)
        current_status = fm.get("status")
        if current_status == status:
            verb = {"completed": "completed", "abandoned": "abandoned", "on-hold": "put on hold"}[status]
            return f"Project was already {verb}: \"{project_title}\""
        try:
            self._goal_manager.update_project_status(path, status)
            verb = {"completed": "completed", "abandoned": "abandoned", "on-hold": "put on hold"}[status]
            return f"Project {verb}: \"{project_title}\""
        except ValueError as e:
            return f"Error: {e}"

    def _add_todo_text(self, description: str, due_date: str = None, todo_type: str = None) -> str:
        """Add a personal todo via CommitmentTracker. Used by add_todo tool."""
        from commitment_tracker import CommitmentTracker
        tracker = CommitmentTracker(
            MEMORIES_DIR,
            self._cache,
            SECOND_BRAIN_DIR / "commitment-scanner-state.json",
        )
        try:
            path = tracker.create_todo(description, due_date=due_date, todo_type=todo_type)
            due_str = f" — due {due_date}" if due_date else ""
            type_str = f" [{todo_type}]" if todo_type else " [personal]"
            return f"✓ Todo added{type_str}: {description}{due_str}"
        except Exception as e:
            return f"Error creating todo: {e}"

    def _get_goal_text(self, index: int) -> str:
        """Get full detail for a goal by list index. Used by get_goal tool."""
        if not self._last_goal_set:
            return "No goals listed yet. Call list_goals first."
        if index < 1 or index > len(self._last_goal_set):
            return f"Index {index} out of range (1-{len(self._last_goal_set)})."
        path = self._last_goal_set[index - 1]
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title", path.stem)
        category = fm.get("category", "?")
        status = fm.get("status", "?")
        due = fm.get("due_date", "none")
        priority = fm.get("priority", "medium")
        linked_projects = fm.get("linked_projects", [])
        lines = [
            f"**{title}** [{category}] — {status}",
            f"Priority: {priority}",
            f"Due: {due}",
        ]
        if linked_projects:
            lines.append(f"Linked projects: {', '.join(linked_projects)}")
        content = path.read_text()
        notes_match = re.search(r"## Notes\n(.*)", content, re.DOTALL)
        if notes_match and notes_match.group(1).strip():
            lines.append(f"\n{notes_match.group(1).strip()[:500]}")
        return "\n".join(lines)

    def _get_project_text(self, index: int) -> str:
        """Get full detail for a project by list index. Used by get_project tool."""
        if not self._last_project_set:
            return "No projects listed yet. Call list_projects first."
        if index < 1 or index > len(self._last_project_set):
            return f"Index {index} out of range (1-{len(self._last_project_set)})."
        path = self._last_project_set[index - 1]
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title", path.stem)
        category = fm.get("category", "?")
        status = fm.get("status", "?")
        due = fm.get("due_date", "none")
        priority = fm.get("priority", "medium")
        linked_goal = fm.get("linked_goal")
        milestones = fm.get("milestones", [])
        lines = [
            f"**{title}** [{category}] — {status}",
            f"Priority: {priority}",
            f"Due: {due}",
        ]
        if linked_goal:
            lines.append(f"Linked goal: {linked_goal}")
        if milestones:
            done_count = sum(1 for m in milestones if m.get("completed"))
            lines.append(f"Milestones: {done_count}/{len(milestones)} completed")
        content = path.read_text()
        notes_match = re.search(r"## Notes\n(.*)", content, re.DOTALL)
        if notes_match and notes_match.group(1).strip():
            lines.append(f"\n{notes_match.group(1).strip()[:500]}")
        return "\n".join(lines)

    async def _get_feature_text(self, index_or_id: str) -> str:
        """Get full detail for a feature/bug by index, short_id, or GH issue number. Used by get_feature tool."""
        # Try to parse as integer index first
        try:
            index = int(index_or_id)
            if not self._last_feature_set:
                return "No features/bugs listed yet. Call list_features first."
            if index < 1 or index > len(self._last_feature_set):
                return f"Index {index} out of range (1-{len(self._last_feature_set)})."
            path = self._last_feature_set[index - 1]
        except ValueError:
            # Try short_id or GitHub issue number via cache
            id_str = index_or_id.lstrip("#")
            memories_dir = BRAIN_DIR / "memories"
            rows = await self._cache.query_by_prefix("feature-request-")
            hits = []
            for row in rows:
                try:
                    fm = json.loads(row.get("frontmatter") or "{}")
                except Exception:
                    fm = {}
                if fm.get("short_id") == id_str or str(fm.get("github_issue_number")) == id_str:
                    hits.append(memories_dir / row["filename"])
            if not hits:
                return f"No feature/bug found with ID '{index_or_id}'."
            path = hits[0]

        fm = self._parse_frontmatter(path)
        title = fm.get("title", path.stem)
        kind = fm.get("kind", "feature")
        status = fm.get("status", "new")
        priority = fm.get("priority", "medium")
        short_id = fm.get("short_id", "?")
        gh_issue = fm.get("github_issue_number")
        lines = [
            f"**{title}** — [{short_id}]",
            f"Kind: {kind}  |  Status: {status}  |  Priority: {priority}",
        ]
        if gh_issue:
            lines.append(f"GitHub: #{gh_issue}")
        content = path.read_text()
        body_start = content.find("---\n\n") + 5 if "---\n\n" in content else 0
        lines.append(f"\n{content[body_start:body_start + 500]}")
        return "\n".join(lines)

    async def _update_feature_text(
        self, index_or_id: str, action: str, note_or_priority: str = None
    ) -> str:
        """Update a feature/bug status, priority, or add a note. Called by update_feature tool."""
        target = await self._resolve_feature_index([index_or_id], None)
        if target is None:
            return f"Feature '{index_or_id}' not found. Use list_features to browse."

        action = action.lower().replace("-", "_")
        valid_actions = ("plan", "start", "done", "wont_do", "priority", "note")
        if action not in valid_actions:
            return f"Invalid action '{action}'. Use one of: {', '.join(valid_actions)}"

        if isinstance(target, int):
            # GitHub-backed issue
            if action == "plan":
                await self._gh_set_status(target, "planned")
            elif action == "start":
                await self._gh_set_status(target, "in-progress")
            elif action == "done":
                await self._gh_set_status(target, "done")
                if note_or_priority:
                    await self.github.add_comment(target, note_or_priority)
            elif action == "wont_do":
                await self._gh_set_status(target, "wont-do")
                if note_or_priority:
                    await self.github.add_comment(target, f"Won't do: {note_or_priority}")
            elif action == "priority":
                if not note_or_priority or note_or_priority not in ("low", "medium", "high", "critical"):
                    return "Priority must be one of: low, medium, high, critical"
                await self._gh_set_priority(target, note_or_priority)
            elif action == "note":
                if not note_or_priority:
                    return "Note text is required."
                await self.github.add_comment(target, note_or_priority)
            await self._rewrite_features_index_snapshot()
            title = await self._gh_title(target)
            return f"Feature '{title[:60]}' updated: {action}."
        else:
            # Local file
            fm = self._parse_frontmatter(target)
            title = fm.get("title", "?")[:60]
            if action == "plan":
                self._rewrite_feature_frontmatter(target, {"status": "planned"})
            elif action == "start":
                self._rewrite_feature_frontmatter(target, {"status": "in-progress"})
            elif action == "done":
                self._rewrite_feature_frontmatter(target, {"status": "done"})
                if note_or_priority:
                    self._append_feature_note(target, note_or_priority)
            elif action == "wont_do":
                self._rewrite_feature_frontmatter(target, {"status": "wont-do"})
                if note_or_priority:
                    self._append_feature_note(target, f"Won't do: {note_or_priority}")
            elif action == "priority":
                if not note_or_priority or note_or_priority not in ("low", "medium", "high", "critical"):
                    return "Priority must be one of: low, medium, high, critical"
                self._rewrite_feature_frontmatter(target, {"priority": note_or_priority})
            elif action == "note":
                if not note_or_priority:
                    return "Note text is required."
                self._append_feature_note(target, note_or_priority)
            return f"Feature '{title}' updated: {action}."

    def _get_event_text(self, index: int) -> str:
        """Get full detail for a calendar event by list index. Used by get_event tool."""
        if not self._last_event_set:
            return "No events listed yet. Call list_events first."
        if index < 1 or index > len(self._last_event_set):
            return f"Index {index} out of range (1-{len(self._last_event_set)})."
        path = self._last_event_set[index - 1]
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title", path.stem)
        start = fm.get("start_time", "?")
        location = fm.get("location", "none")
        participants = fm.get("participants", [])
        summary = fm.get("summary", "")
        lines = [
            f"**{title}**",
            f"Start: {start}",
            f"Location: {location}",
        ]
        if participants:
            lines.append(f"Participants: {', '.join(participants[:5])}")
        if summary:
            lines.append(f"\n{summary}")
        return "\n".join(lines)

    def _get_meeting_text(self, index: int) -> str:
        """Get full detail for a meeting transcript by list index. Used by get_meeting tool."""
        if not self._last_meeting_set:
            return "No meetings listed yet. Call list_meetings first."
        if index < 1 or index > len(self._last_meeting_set):
            return f"Index {index} out of range (1-{len(self._last_meeting_set)})."
        path = self._last_meeting_set[index - 1]
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title", path.stem)
        date = fm.get("date", "?")
        participants = fm.get("participants", [])
        summary = fm.get("summary", "")
        lines = [
            f"**{title}** — {date}",
        ]
        if participants:
            lines.append(f"Participants: {', '.join(participants[:5])}")
        if summary:
            lines.append(f"\n{summary[:500]}")
        return "\n".join(lines)

    async def _get_contact_text(self, name_or_n: str) -> str:
        """Return formatted contact detail by index or name. Called by get_contact tool."""
        path = self._resolve_contact_index(name_or_n)
        if path:
            fm = self._parse_frontmatter(path)
        else:
            if not self._last_contact_set:
                self._list_contacts_text()
            path, fm = self._find_contact_by_name(name_or_n)
            if not path:
                return f"No contact found for '{name_or_n}'. Use list_contacts to browse."

        name = fm.get("name", "(no name)")
        emails = fm.get("emails", [])
        email_str = ", ".join(emails) if emails else "no email"
        score = fm.get("relationship_score", 0.0)
        interaction_count = fm.get("interaction_count", 0)
        lines = [
            f"{name} ({email_str})",
            f"Relationship score: {score} | {interaction_count} interactions",
            "",
        ]

        open_commitments = []
        commit_rows = await self._cache.query_by_prefix("commitment-")
        for row in commit_rows:
            try:
                cfm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                cfm = {}
            if cfm.get("status") != "active":
                continue
            owner = cfm.get("owner", "")
            recipient = cfm.get("recipient", "")
            owner_email = cfm.get("owner_email", "")
            if name in (owner, recipient) or (emails and owner_email in emails):
                open_commitments.append(cfm)

        if open_commitments:
            lines.append("Open commitments:")
            for cfm in open_commitments[:5]:
                ct = cfm.get("commitment_type", "outbound")
                desc = (cfm.get("source_title") or "")[:60]
                due = cfm.get("due_date")
                due_str = f"due {due}" if due else "due unknown"
                direction = "outbound" if ct == "outbound" else "waiting_on"
                lines.append(f"• [{direction}] {desc} — {due_str}")
            lines.append("")

        try:
            content = path.read_text()
            m = re.search(r'## Recent Interactions\n\n(.*?)(?=\n\n##|\Z)', content, re.DOTALL)
            if m:
                summary = m.group(1).strip()[:400]
                lines.append("Summary:")
                lines.append(summary)
        except Exception:
            pass

        return "\n".join(lines)

    def _get_comm_text(self, index: int) -> str:
        """Get full detail for an email/Slack thread by list index. Used by get_comm tool."""
        if not self._last_comms_set:
            return "No comms listed yet. Call list_comms first."
        if index < 1 or index > len(self._last_comms_set):
            return f"Index {index} out of range (1-{len(self._last_comms_set)})."
        path = self._last_comms_set[index - 1]
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title", path.stem)
        thread_type = fm.get("type", "?")
        participants = fm.get("participants", [])
        message_count = fm.get("message_count", 0)
        summary = fm.get("summary", "")
        lines = [
            f"**{title}** — {thread_type}",
            f"Participants: {', '.join(participants[:5])}",
            f"Messages: {message_count}",
        ]
        if summary:
            lines.append(f"\n{summary[:500]}")
        return "\n".join(lines)

    def _get_reading_text(self, index: int) -> str:
        """Get full detail for a web page capture by list index. Used by get_reading tool."""
        if not self._last_readings_set:
            return "No readings listed yet. Call list_readings first."
        if index < 1 or index > len(self._last_readings_set):
            return f"Index {index} out of range (1-{len(self._last_readings_set)})."
        path = self._last_readings_set[index - 1]
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title", path.stem)
        url = fm.get("source_url", "?")
        summary = fm.get("summary", "")
        key_points = fm.get("key_points", [])
        tags = fm.get("tags", [])
        lines = [
            f"**{title}**",
            f"URL: {url}",
        ]
        if summary:
            lines.append(f"\n{summary}")
        if key_points:
            lines.append("\nKey points:")
            lines.extend(f"• {p}" for p in key_points[:5])
        if tags:
            lines.append(f"\nTags: {', '.join(tags[:10])}")
        return "\n".join(lines)

    def _get_action_text(self, index: int) -> str:
        """Get full detail for an agent-proposed action by list index. Used by get_action tool."""
        if not self._last_actions_set:
            return "No actions listed yet. Call list_actions first."
        if index < 1 or index > len(self._last_actions_set):
            return f"Index {index} out of range (1-{len(self._last_actions_set)})."
        path = self._last_actions_set[index - 1]
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title", path.stem)
        source = fm.get("source_goal_or_project", "?")
        status = fm.get("status", "pending")
        rationale = fm.get("rationale", "")
        steps = fm.get("proposed_steps", [])
        lines = [
            f"**{title}** — {status}",
            f"Source: {source}",
        ]
        if rationale:
            lines.append(f"\nRationale: {rationale[:300]}")
        if steps:
            lines.append("\nProposed steps:")
            lines.extend(f"{i}. {s}" for i, s in enumerate(steps[:5], 1))
        return "\n".join(lines)

    async def cmd_commitments(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        type_filter = context.args[0] if context.args else None
        # For cmd, we allow type_filter but the tool doesn't expose it — the tool always passes None
        if type_filter:
            # Custom logic for filtered cmd
            items = await self._load_active_commitments(type_filter)
            if not items:
                await update.message.reply_text(f"No active {type_filter} commitments.")
                self._last_commitment_set = []
                return
            self._last_commitment_set = [f for f, _ in items]
            self._active_list = self._last_commitment_set
            total = len(items)
            lines = [f"Active {type_filter} commitments ({total} total):"]
            today = datetime.now().date()
            for i, (_, fm) in enumerate(items[:20], 1):
                ct = fm.get("commitment_type", "outbound")
                desc = (fm.get("source_title") or fm.get("summary") or "")[:50]
                owner = fm.get("owner", "")
                due = fm.get("due_date")
                if due:
                    try:
                        overdue = datetime.strptime(str(due), "%Y-%m-%d").date() < today
                    except ValueError:
                        overdue = False
                    due_str = f" — was due {due} ⚠️" if overdue else f" — due {due}"
                else:
                    due_str = " — due unknown"
                needs_review = "needs-review" in (fm.get("tags") or [])
                flag = " ⚠️" if needs_review and not due_str.endswith("⚠️") else ""
                owner_str = f" — {owner}" if owner and ct != "personal" else ""
                lines.append(f"{i}. [{ct}] {desc}{owner_str}{due_str}{flag}")
            if total > 20:
                lines.append(f"... and {total - 20} more.")
            lines.append("\nUse /complete N or /dismiss N to update status.")
            reply_text = "\n".join(lines)
            await update.message.reply_text(reply_text)
            self._record_command_reply(update.effective_chat.id, "commitments", reply_text)
        else:
            text = await self._list_commitments_text(limit=20)
            await update.message.reply_text(text)
            self._record_command_reply(update.effective_chat.id, "commitments", text)

    async def _list_todos_text(self) -> str:
        """Format all active commitments as a checklist (todo-list style)."""
        items = await self._load_active_commitments(type_filter=None)

        if not items:
            self._last_todos_set = []
            self._last_commitment_set = []
            self._active_list = []
            return "No active todos."

        all_paths = [f for f, _ in items]
        self._last_todos_set = all_paths
        self._last_commitment_set = all_paths
        self._active_list = all_paths
        total = len(items)
        lines = [f"Todos ({total}):"]

        today = datetime.now().date()
        for i, (_, fm) in enumerate(items, 1):
            desc = (fm.get("source_title") or fm.get("summary") or "")[:55]
            due = fm.get("due_date")
            if due:
                try:
                    overdue = datetime.strptime(str(due), "%Y-%m-%d").date() < today
                except ValueError:
                    overdue = False
                due_str = f" — was due {due} ⚠️" if overdue else f" — due {due}"
            else:
                due_str = ""
            owner = fm.get("owner", "")
            owner_part = f" — {owner}" if owner else ""
            ct = fm.get("commitment_type", "outbound")
            type_hint = f" [{ct}]" if ct != "personal" else ""
            needs_review = "needs-review" in (fm.get("tags") or [])
            flag = " ⚠️" if needs_review and not due_str.endswith("⚠️") else ""
            lines.append(f"{i}. [ ] {desc}{owner_part}{due_str}{type_hint}{flag}")

        lines.append("\n/todos done N  — complete  |  /todos dismiss N  — dismiss")
        return "\n".join(lines)

    async def cmd_todos(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List todos as a checklist, or complete/dismiss by index."""
        if not self._check_auth(update):
            return

        args = list(context.args) if context.args else []

        if not args:
            todos_text = await self._list_todos_text()
            await self._send_reply(update, todos_text)
            self._record_command_reply(update.effective_chat.id, "todos", todos_text)
            return

        verb = args[0].lower()
        indices = args[1:]

        if verb not in ("done", "dismiss") or not indices:
            await update.message.reply_text(
                "Usage: /todos | /todos done N [M…] | /todos dismiss N [M…]"
            )
            return

        new_status = "completed" if verb == "done" else "dismissed"
        from commitment_tracker import CommitmentTracker
        lines = []
        seen = set()
        for arg in indices:
            if arg in seen:
                continue
            seen.add(arg)
            try:
                idx = int(arg) - 1
            except (ValueError, TypeError):
                lines.append(f"✗ #{arg}: not found (run /todos to refresh)")
                continue
            if 0 <= idx < len(self._last_todos_set):
                path = self._last_todos_set[idx]
            else:
                lines.append(f"✗ #{arg}: not found (run /todos to refresh)")
                continue
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title") or "todo"
            try:
                await CommitmentTracker(cache=self._cache).update_commitment_status(path, new_status)
                mark = "✓" if new_status == "completed" else "✕"
                lines.append(f"{mark} {title}")
            except Exception as e:
                lines.append(f"✗ {title}: {e}")

        await update.message.reply_text("\n".join(lines))

    def _resolve_commitment_index(self, n: str):
        """Convert 1-based index string to a Path from _last_commitment_set, or None."""
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_commitment_set):
            return self._last_commitment_set[idx]
        return None

    async def cmd_complete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /complete N [M P ...]")
            return

        from commitment_tracker import CommitmentTracker
        lines = []
        seen = set()
        for arg in context.args:
            if arg in seen:
                continue
            seen.add(arg)
            path = self._resolve_commitment_index(arg)
            if path is None:
                lines.append(f"\u2717 #{arg}: not found (run /commitments to refresh)")
                continue
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title") or "commitment"
            owner = fm.get("owner", "")
            label = f'"{title}"' + (f" ({owner})" if owner else "")
            try:
                await CommitmentTracker(cache=self._cache).update_commitment_status(path, "completed")
                lines.append(f"\u2713 {label}")
            except Exception as e:
                lines.append(f"\u2717 {label}: {e}")

        await update.message.reply_text("\n".join(lines))

    async def cmd_dismiss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /dismiss N [M P ...]")
            return

        from commitment_tracker import CommitmentTracker
        lines = []
        seen = set()
        for arg in context.args:
            if arg in seen:
                continue
            seen.add(arg)
            path = self._resolve_commitment_index(arg)
            if path is None:
                lines.append(f"\u2717 #{arg}: not found (run /commitments to refresh)")
                continue
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title") or "commitment"
            owner = fm.get("owner", "")
            label = f'"{title}"' + (f" ({owner})" if owner else "")
            try:
                await CommitmentTracker(cache=self._cache).update_commitment_status(path, "dismissed")
                lines.append(f"\u2717 {label}")
            except Exception as e:
                lines.append(f"\u2717 {label}: {e}")

        await update.message.reply_text("\n".join(lines))

    # ── /wrong command (FR-11) ────────────────────────────────────────────────

    async def cmd_wrong(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /wrong N")
            return

        path = self._resolve_commitment_index(context.args[0])
        if path is None:
            await update.message.reply_text(self._format_group_help("Commitments"))
            return

        from commitment_tracker import (
            CommitmentTracker,
            CORRECTIONS_FILE,
            _record_false_positive,
        )
        import json
        from datetime import datetime, timedelta

        fm = self._parse_frontmatter(path)
        title = fm.get("source_title") or "commitment"
        owner = fm.get("owner", "")
        label = f'"{title}"' + (f" ({owner})" if owner else "")
        source_memory = fm.get("source_memory", "")
        source_type = ""

        # Infer source_type from source_memory URL scheme
        if source_memory.startswith("zoom:"):
            source_type = "meeting_transcript"
        elif source_memory.startswith("email:"):
            source_type = "email_thread"
        elif ":" in source_memory:
            source_type = source_memory.split(":")[0]

        # Extract commitment_id from source_url
        source_url = fm.get("source_url", "")
        commitment_id = source_url.split(":")[-1] if ":" in source_url else ""

        try:
            # Set status to dismissed
            await CommitmentTracker(cache=self._cache).update_commitment_status(path, "dismissed")

            # Append to corrections JSONL
            correction = {
                "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "correction_type": "false_positive",
                "commitment_id": commitment_id,
                "description": title,
                "owner": owner,
                "source_memory": source_memory,
                "source_type": source_type,
            }
            CORRECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CORRECTIONS_FILE.open("a") as f:
                f.write(json.dumps(correction) + "\n")

            # Update accuracy stats
            if source_type:
                _record_false_positive(source_type)

            await update.message.reply_text(
                f"\u2717 Marked as incorrect extraction: {label}\n"
                "This will help avoid similar false positives in future scans."
            )
        except Exception as e:
            await update.message.reply_text(f"Error marking as wrong: {_safe_error(e)}")

    # ── /missed command (FR-12) ───────────────────────────────────────────────

    async def cmd_missed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        # Store user ID in context for the reply handler
        context.user_data["awaiting_missed_reply"] = True
        context.user_data["missed_start_time"] = asyncio.get_event_loop().time()

        await update.message.reply_text(
            "Add a missed commitment. Reply with:\n"
            "type: outbound|inbound|waiting_on\n"
            "description: what the commitment is\n"
            "owner: who made it (name)\n"
            "due: YYYY-MM-DD or \"unknown\"\n"
            "source: brief note on where it came from"
        )

    async def _handle_missed_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle reply to /missed command."""
        text = update.message.text.strip()
        from commitment_tracker import CommitmentTracker, CORRECTIONS_FILE, _record_missed
        import json
        from datetime import datetime, timedelta

        # Parse structured reply
        parsed = {}
        for line in text.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                parsed[key.strip().lower()] = val.strip()

        # Validate required fields
        required = {"type", "description", "owner", "due", "source"}
        missing = required - set(parsed.keys())
        if missing:
            await update.message.reply_text(
                f"Missing required fields: {', '.join(missing)}. Please try /missed again."
            )
            context.user_data["awaiting_missed_reply"] = False
            return

        commitment_type = parsed["type"]
        if commitment_type not in ("outbound", "inbound", "waiting_on"):
            await update.message.reply_text(
                "Invalid type. Must be: outbound, inbound, or waiting_on. Try /missed again."
            )
            context.user_data["awaiting_missed_reply"] = False
            return

        description = parsed["description"]
        owner = parsed["owner"]
        due_date = parsed["due"] if parsed["due"].lower() != "unknown" else None
        source_note = parsed["source"]

        # Infer source_type from source note (best effort)
        source_type = "manual"
        if "meeting" in source_note.lower() or "zoom" in source_note.lower():
            source_type = "meeting_transcript"
        elif "email" in source_note.lower():
            source_type = "email_thread"

        try:
            # Create commitment file
            tracker = CommitmentTracker()
            commitment_path = tracker.create_manual_commitment(
                commitment_type, description, owner, due_date, source_note
            )

            # Append to corrections JSONL
            correction = {
                "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "correction_type": "missed",
                "description": description,
                "owner": owner,
                "source_type": source_type,
                "source_note": source_note,
            }
            CORRECTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CORRECTIONS_FILE.open("a") as f:
                f.write(json.dumps(correction) + "\n")

            # Update accuracy stats
            if source_type:
                _record_missed(source_type)

            await update.message.reply_text(
                f'\u2713 Created commitment: "{description}" ({owner})'
            )
        except Exception as e:
            await update.message.reply_text(f"Error creating commitment: {_safe_error(e)}")
        finally:
            context.user_data["awaiting_missed_reply"] = False

    # ── /todo command (#12) ──────────────────────────────────────────────────

    @staticmethod
    def _classify_todo(text: str) -> str:
        """Classify a todo description as waiting_on/outbound/personal using keyword heuristics."""
        lower = text.lower()
        outbound_markers = [
            "send to", "send ", "deliver ", "share with", "provide to",
            "give to", "submit to", "report to", "email to", "forward to",
        ]
        waiting_on_markers = [
            "follow up with", "follow-up with", "check in with", "check on ",
            "following up", "ask ", "remind ", "hear from ", "waiting for ",
            "schedule with", "reach out to", "connect with",
        ]
        for marker in outbound_markers:
            if marker in lower:
                return "outbound"
        for marker in waiting_on_markers:
            if marker in lower:
                return "waiting_on"
        return "personal"

    @staticmethod
    def _extract_todo_recipient(text: str) -> Optional[str]:
        """Extract a person name from patterns like 'follow up with John' or 'send to Jane Doe'."""
        import re
        # Match "with <Name>" or "to <Name>" where Name starts with a capital letter.
        # Capture 1-3 capitalised words; stops naturally at lowercase continuation words.
        m = re.search(
            r"\b(?:with|to)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})\b",
            text,
        )
        if m:
            name = m.group(1).strip()
            _NON_NAMES = {"Me", "Us", "Them", "Him", "Her", "You", "It", "The", "My", "Our"}
            if name not in _NON_NAMES:
                return name
        return None

    async def cmd_todo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create a personal todo item. /todo <desc> [due:YYYY-MM-DD] [type:personal|waiting_on|outbound]"""
        if not self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: /todo <description> [due:YYYY-MM-DD] [type:personal|waiting_on|outbound]\n"
                "Examples:\n"
                "  /todo Clean my desk\n"
                "  /todo Get the report to Jane Doe due:2026-05-01\n"
                "  /todo Follow up with John on the design doc"
            )
            return

        from commitment_tracker import CommitmentTracker

        raw_args = list(context.args)
        due_date: Optional[str] = None
        forced_type: Optional[str] = None

        # Extract due: and type: tokens from args
        remaining = []
        for token in raw_args:
            if token.lower().startswith("due:"):
                raw_due = token[4:].strip()
                try:
                    datetime.strptime(raw_due, "%Y-%m-%d")
                    due_date = raw_due
                except ValueError:
                    await update.message.reply_text(
                        f"Invalid due date {raw_due!r}. Use format: due:YYYY-MM-DD"
                    )
                    return
            elif token.lower().startswith("type:"):
                value = token[5:].strip().lower()
                if value in ("personal", "waiting_on", "outbound"):
                    forced_type = value
                else:
                    await update.message.reply_text(
                        f"Invalid type {value!r}. Must be: personal, waiting_on, or outbound."
                    )
                    return
            else:
                remaining.append(token)

        description = " ".join(remaining).strip()
        if not description:
            await update.message.reply_text("Please provide a description after /todo.")
            return

        commitment_type = forced_type or self._classify_todo(description)
        recipient = self._extract_todo_recipient(description)

        # For waiting_on, the owner is the external party being waited on; the
        # user is the recipient. For personal/outbound, the user is the owner.
        if commitment_type == "waiting_on":
            owner = recipient or "unknown"
            todo_recipient = "self"
        else:
            owner = "self"
            todo_recipient = recipient

        try:
            tracker = CommitmentTracker()
            tracker.create_manual_commitment(
                commitment_type=commitment_type,
                description=description,
                owner=owner,
                due_date=due_date,
                source_note="Created via /todo command",
                force_unique=True,
                recipient=todo_recipient,
            )
            due_str = f" — due {due_date}" if due_date else ""
            await update.message.reply_text(
                f"✓ Todo added [{commitment_type}]: {description}{due_str}"
            )
        except Exception as e:
            await update.message.reply_text(f"Error creating todo: {_safe_error(e)}")

    # ── /accuracy command (FR-14) ─────────────────────────────────────────────

    async def cmd_accuracy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        from commitment_tracker import _load_accuracy

        data = _load_accuracy()
        by_type = data.get("by_source_type", {})

        if not by_type:
            await update.message.reply_text(
                "No accuracy data yet. Use /wrong and /missed to provide feedback."
            )
            return

        lines = ["Commitment extraction accuracy:", ""]
        for source_type, stats in sorted(by_type.items()):
            extracted = stats.get("extracted", 0)
            false_positives = stats.get("false_positives", 0)
            missed = stats.get("missed", 0)

            if extracted > 0:
                precision = ((extracted - false_positives) / extracted) * 100
                precision_str = f"{precision:.0f}% precision"
            else:
                precision_str = "N/A"

            lines.append(
                f"{source_type}: {extracted} extracted, {false_positives} false positives "
                f"({precision_str}), {missed} missed"
            )

        lines.append("")
        lines.append("Use /wrong N to flag false positives. Use /missed to add skipped commitments.")
        await update.message.reply_text("\n".join(lines))

    # ── Quota tracking ────────────────────────────────────────────────────────

    async def cmd_quota(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Track Claude.ai Pro / ChatGPT Plus message quotas."""
        if not self._check_auth(update):
            return

        args = context.args or []

        # Get quota scanner from scanners dict
        quota_scanner = self.scanners.get("quota_scanner")
        if quota_scanner is None:
            await update.message.reply_text(
                "Quota scanner not available (requires role=full)."
            )
            return

        # No args: show status
        if not args:
            status = quota_scanner.render_status()
            await update.message.reply_text(status)
            return

        # report subcommand
        if args[0] == "report":
            if len(args) < 3:
                await update.message.reply_text(
                    "Usage: /quota report <platform> <used>/<cap> [reset <minutes>]"
                )
                return

            platform = args[1]
            used_cap = args[2]

            # Parse used/cap
            if "/" not in used_cap:
                await update.message.reply_text("Format: <used>/<cap>, e.g. 23/40")
                return

            try:
                used, cap = map(int, used_cap.split("/"))
            except ValueError:
                await update.message.reply_text("Invalid format. Use integers: <used>/<cap>")
                return

            # Parse optional reset minutes
            reset_min = None
            if len(args) >= 5 and args[3] == "reset":
                try:
                    reset_min = int(args[4])
                except ValueError:
                    await update.message.reply_text("Invalid reset minutes. Use an integer.")
                    return

            quota_scanner.report(platform, used, cap, reset_min)
            await update.message.reply_text(f"OK — {platform} at {used}/{cap}.")
            return

        # reset subcommand
        if args[0] == "reset":
            if len(args) < 2:
                await update.message.reply_text("Usage: /quota reset <platform>")
                return

            platform = args[1]
            quota_scanner.clear(platform)
            await update.message.reply_text(f"Cleared {platform} state.")
            return

        # Unknown subcommand
        await update.message.reply_text(
            "Usage: /quota | /quota report <platform> <used>/<cap> [reset <min>] | /quota reset <platform>"
        )

    # ── Agent actions commands ────────────────────────────────────────────────

    async def _list_actions_text(self, filter_status: Optional[str] = None) -> str:
        """Return formatted action list text. Called by cmd_actions and tool dispatch."""
        actions = await self._load_action_set(filter_status=filter_status)
        if not actions:
            msg = "No pending agent actions."
            if filter_status:
                msg += f" (filter: {filter_status})"
            msg += " Use /actions all to see all."
            return msg
        lines = [f"Agent-proposed actions ({len(actions)}):"]
        for i, (path, fm) in enumerate(actions, 1):
            action_type = fm.get("action_type", "")
            target = fm.get("target") or fm.get("source_goal", "")
            rationale = fm.get("rationale", "")[:60]
            lines.append(f"{i}. [{action_type}] {target} — {rationale}")
        lines.append("")
        lines.append("Use /action N for details, /run N to approve and execute.")
        return "\n".join(lines)

    async def _load_action_set(self, filter_status: Optional[str] = None, update_last_set: bool = True) -> list:
        """Load action-*.md files and filter by status. Returns list of (path, fm) tuples."""
        actions = []
        now = datetime.now()
        memories_dir = BRAIN_DIR / "memories"

        rows = await self._cache.query_by_prefix("action-")
        for row in rows:
            try:
                try:
                    fm = json.loads(row.get("frontmatter") or "{}")
                except Exception:
                    fm = {}
                path = memories_dir / row["filename"]
                if fm.get("type") != "agent_action":
                    continue

                status = fm.get("status")
                defer_until_str = fm.get("defer_until")

                # Default filter: pending and not deferred
                if filter_status is None:
                    if status != "pending":
                        continue
                    if defer_until_str:
                        try:
                            defer_until = datetime.fromisoformat(defer_until_str)
                            if defer_until > now:
                                continue  # Still deferred
                        except Exception:
                            pass
                elif filter_status == "approved":
                    if status != "approved":
                        continue
                elif filter_status == "all":
                    pass  # Include all
                else:
                    if status != filter_status:
                        continue

                actions.append((path, fm))
            except Exception:
                continue

        # Sort by proposed_at descending
        actions.sort(key=lambda x: x[1].get("proposed_at", ""), reverse=True)
        if update_last_set:
            self._last_action_set = actions
        return actions

    async def cmd_actions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List pending agent-proposed actions."""
        if not self._check_auth(update):
            return

        filter_arg = context.args[0].lower() if context.args else None
        actions_text = await self._list_actions_text(filter_status=filter_arg)
        await update.message.reply_text(actions_text)
        self._record_command_reply(update.effective_chat.id, "actions", actions_text)

    async def cmd_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show full detail for action N."""
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /action N")
            return

        try:
            idx = int(context.args[0]) - 1
        except ValueError:
            await update.message.reply_text("Usage: /action N (where N is a number)")
            return

        if not self._last_action_set:
            await update.message.reply_text("No action list loaded. Use /actions first.")
            return

        if idx < 0 or idx >= len(self._last_action_set):
            await update.message.reply_text(f"Index {idx+1} out of range. Use /actions to see the list.")
            return

        path, fm = self._last_action_set[idx]
        action_type = fm.get("action_type", "")
        target = fm.get("target") or "none"
        args = fm.get("args") or {}
        confidence = fm.get("confidence", 0)
        rationale = fm.get("rationale", "")
        evidence = fm.get("evidence") or []
        proposed_at = fm.get("proposed_at", "")
        source_goal = fm.get("source_goal", "")
        defer_until = fm.get("defer_until")

        lines = [
            f"Action {idx+1}:",
            f"Type: {action_type}",
            f"Target: {target}",
            f"Args: {args}",
            f"Confidence: {confidence:.2f}",
            f"Rationale: {rationale}",
            f"Evidence: {', '.join(evidence) if evidence else 'none'}",
            f"Proposed: {proposed_at}",
            f"Source: {source_goal}",
        ]
        if defer_until:
            lines.append(f"Deferred until: {defer_until}")

        lines.append("")
        lines.append("Use /run N to approve and execute, /drop N to reject, /defer N [hours] to snooze.")
        await update.message.reply_text("\n".join(lines))

    async def cmd_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Approve and execute action N."""
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /run N")
            return

        try:
            idx = int(context.args[0]) - 1
        except ValueError:
            await update.message.reply_text("Usage: /run N (where N is a number)")
            return

        if not self._last_action_set:
            await update.message.reply_text("No action list loaded. Use /actions first.")
            return

        if idx < 0 or idx >= len(self._last_action_set):
            await update.message.reply_text(f"Index {idx+1} out of range. Use /actions to see the list.")
            return

        path, fm = self._last_action_set[idx]

        # Re-read to get fresh status
        try:
            fresh_fm = self._parse_frontmatter(path)
            fresh_text = path.read_text(encoding="utf-8")
        except Exception as e:
            await update.message.reply_text(f"Error reading action file: {_safe_error(e)}")
            return

        if fresh_fm.get("status") != "pending":
            await update.message.reply_text(f"Action {idx+1} is no longer pending (status: {fresh_fm.get('status')})")
            return

        # Execute
        from goal_project_agent import GoalProjectAgent
        agent = GoalProjectAgent(role="full", cache=self._cache)
        try:
            msg = await agent._execute_action(path, fresh_fm)
            # Mark as executed
            fresh_fm["status"] = "executed"
            fresh_fm["executed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            parts = fresh_text.split("---", 2)
            new_fm = yaml.dump(fresh_fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
            new_content = f"---\n{new_fm}---{parts[2] if len(parts) >= 3 else ''}"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            os.rename(str(tmp), str(path))
            await update.message.reply_text(f"\u2713 Action {idx+1} executed: {msg}")
        except Exception as e:
            # Check if it's a precondition failure — mark as superseded
            error_str = str(e)
            if "not found" in error_str.lower() or "already" in error_str.lower():
                fresh_fm["status"] = "superseded"
                fresh_fm["superseded_reason"] = error_str
                fresh_fm["superseded_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                parts = fresh_text.split("---", 2)
                new_fm = yaml.dump(fresh_fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
                new_content = f"---\n{new_fm}---{parts[2] if len(parts) >= 3 else ''}"
                tmp = path.with_suffix(".tmp")
                tmp.write_text(new_content, encoding="utf-8")
                os.rename(str(tmp), str(path))
                await update.message.reply_text(f"Action {idx+1} superseded: {error_str}")
            else:
                await update.message.reply_text(f"Error executing action {idx+1}: {_safe_error(e)}")

    async def cmd_drop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reject action N."""
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /drop N")
            return

        try:
            idx = int(context.args[0]) - 1
        except ValueError:
            await update.message.reply_text("Usage: /drop N (where N is a number)")
            return

        if not self._last_action_set:
            await update.message.reply_text("No action list loaded. Use /actions first.")
            return

        if idx < 0 or idx >= len(self._last_action_set):
            await update.message.reply_text(f"Index {idx+1} out of range. Use /actions to see the list.")
            return

        path, fm = self._last_action_set[idx]

        # Re-read to get fresh state
        try:
            fresh_fm = self._parse_frontmatter(path)
            fresh_text = path.read_text(encoding="utf-8")
        except Exception as e:
            await update.message.reply_text(f"Error reading action file: {_safe_error(e)}")
            return

        # Update status to rejected
        fresh_fm["status"] = "rejected"
        fresh_fm["rejected_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Append to rejected actions file
        rejected_file = DEPLOY_DIR / "rejected-actions.json"
        if rejected_file.exists():
            try:
                rejected_data = json.loads(rejected_file.read_text())
            except Exception:
                rejected_data = {"rejected": []}
        else:
            rejected_data = {"rejected": []}

        rejected_data["rejected"].append({
            "action_id": fresh_fm.get("action_id"),
            "action_type": fresh_fm.get("action_type"),
            "rationale": fresh_fm.get("rationale"),
            "rejected_at": fresh_fm["rejected_at"],
        })

        # Atomic write of rejected file
        tmp_rej = rejected_file.with_suffix(".tmp")
        tmp_rej.write_text(json.dumps(rejected_data, indent=2))
        os.rename(str(tmp_rej), str(rejected_file))

        # Write updated action file
        parts = fresh_text.split("---", 2)
        new_fm = yaml.dump(fresh_fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        new_content = f"---\n{new_fm}---{parts[2] if len(parts) >= 3 else ''}"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.rename(str(tmp), str(path))

        await update.message.reply_text(f"\u2717 Action {idx+1} rejected.")

    async def cmd_defer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Snooze action N for N hours (default 24)."""
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /defer N [hours]")
            return

        try:
            idx = int(context.args[0]) - 1
        except ValueError:
            await update.message.reply_text("Usage: /defer N [hours] (where N is a number)")
            return

        hours = 24
        if len(context.args) > 1:
            try:
                hours = int(context.args[1])
            except ValueError:
                await update.message.reply_text("Hours must be a number")
                return
            if hours <= 0:
                await update.message.reply_text("Hours must be a positive number")
                return

        if not self._last_action_set:
            await update.message.reply_text("No action list loaded. Use /actions first.")
            return

        if idx < 0 or idx >= len(self._last_action_set):
            await update.message.reply_text(f"Index {idx+1} out of range. Use /actions to see the list.")
            return

        path, fm = self._last_action_set[idx]

        # Re-read
        try:
            fresh_fm = self._parse_frontmatter(path)
            fresh_text = path.read_text(encoding="utf-8")
        except Exception as e:
            await update.message.reply_text(f"Error reading action file: {_safe_error(e)}")
            return

        # Set defer_until
        defer_until = datetime.now() + timedelta(hours=hours)
        fresh_fm["defer_until"] = defer_until.strftime("%Y-%m-%dT%H:%M:%S")

        # Write back
        parts = fresh_text.split("---", 2)
        new_fm = yaml.dump(fresh_fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        new_content = f"---\n{new_fm}---{parts[2] if len(parts) >= 3 else ''}"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.rename(str(tmp), str(path))

        await update.message.reply_text(f"Action {idx+1} snoozed for {hours}h (until {defer_until.strftime('%Y-%m-%d %H:%M')})")

    # ── Goals commands ────────────────────────────────────────────────────────

    def _match_verb_in_group(
        self,
        group_name: str,
        base_command: str,
        verb: str,
    ) -> Optional[str]:
        """Return the single best-matching command in COMMAND_REGISTRY[group_name]
        for verb, or None if zero/multiple candidates. The base_command entry itself
        is excluded to prevent self-dispatch loops.

        Tiered matching (first tier with exactly one hit wins):
          1. Compound-prefix: verb + base_command  (e.g. "add"+"goal" → "addgoal")
          2. Compound-suffix: base_command + "_" + verb  (e.g. "feature"+"_"+"done" → "feature_done")
          3. Exact: verb itself is a command in the group
          4. Prefix: exactly one command starts with verb
          5. Substring: exactly one command contains verb
        """
        commands = [cmd for cmd, _ in COMMAND_REGISTRY.get(group_name, [])]
        candidates = [c for c in commands if c != base_command]
        verb_lower = verb.lower()

        for tier_candidates in [
            [c for c in candidates if c == verb_lower + base_command.replace("-", "_")],
            [c for c in candidates if c == base_command.replace("-", "_") + "_" + verb_lower],
            [c for c in candidates if c == verb_lower],
            [c for c in candidates if c.startswith(verb_lower)],
            [c for c in candidates if verb_lower in c],
        ]:
            if len(tier_candidates) == 1:
                return tier_candidates[0]
        return None

    def _format_group_help(
        self,
        group_name: str,
        base_command: Optional[str] = None,
    ) -> str:
        """Build a help string from COMMAND_REGISTRY[group_name].
        Used as the fallback when verb dispatch fails or a resolver gets a non-integer.
        """
        entries = COMMAND_REGISTRY.get(group_name, [])
        lines = []
        if base_command:
            lines.append(f"/{base_command} expects a number (e.g. /{base_command} 1). {group_name} commands:")
        else:
            lines.append(f"{group_name} commands:")
        for cmd, desc in entries:
            lines.append(f"/{cmd} — {desc}")
        return "\n".join(lines)

    def _resolve_goal_index(self, n_str: str):
        """Return (path, None) on success or (None, error_message) on failure.

        Lazy-populates _last_goal_set from active goals when the cache is empty,
        so /goal N works immediately after /addgoal without needing /goals first.
        """
        if not self._last_goal_set:
            self._last_goal_set = self._goal_manager.list_goals(status="active")
        try:
            n = int(n_str)
        except (ValueError, TypeError):
            return (None, self._format_group_help("Goals", "goal"))
        if not self._last_goal_set:
            return (None, "You don't have any active goals yet. Use /addgoal to create one.")
        if 1 <= n <= len(self._last_goal_set):
            return (self._last_goal_set[n - 1], None)
        return (None, f"Index {n} out of range. You have {len(self._last_goal_set)} active goal(s).")

    def _resolve_project_index(self, n_str: str):
        """Return (path, None) on success or (None, error_message) on failure.

        Lazy-populates _last_project_set from active projects when the cache is empty,
        so /project N works immediately after /addproject without needing /projects first.
        """
        if not self._last_project_set:
            self._last_project_set = self._goal_manager.list_projects(status="active")
        try:
            n = int(n_str)
        except (ValueError, TypeError):
            return (None, self._format_group_help("Projects", "project"))
        if not self._last_project_set:
            return (None, "You don't have any active projects yet. Use /addproject to create one.")
        if 1 <= n <= len(self._last_project_set):
            return (self._last_project_set[n - 1], None)
        return (None, f"Index {n} out of range. You have {len(self._last_project_set)} active project(s).")

    async def cmd_addgoal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        # Store user ID in context for the reply handler
        context.user_data["awaiting_addgoal_reply"] = True
        context.user_data["addgoal_start_time"] = asyncio.get_event_loop().time()

        await update.message.reply_text(
            "Add a new goal. Reply with:\n"
            "title: what you want to achieve\n"
            "category: personal | work | family | learning | other\n"
            "due: YYYY-MM-DD or \"none\"\n"
            "priority: low | medium | high | critical (optional, default: medium)"
        )

    async def _handle_addgoal_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle reply to /addgoal command."""
        text = update.message.text.strip()

        # Parse structured reply
        parsed = {}
        for line in text.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                parsed[key.strip().lower()] = val.strip()

        # Validate required fields
        required = {"title", "category"}
        missing = required - set(parsed.keys())
        if missing:
            await update.message.reply_text(
                f"Missing required fields: {', '.join(missing)}. Please try /addgoal again."
            )
            context.user_data["awaiting_addgoal_reply"] = False
            return

        title = parsed["title"]
        category = parsed["category"]
        due_date = parsed.get("due")
        if due_date and due_date.lower() == "none":
            due_date = None
        priority = parsed.get("priority", "medium")

        try:
            path = self._goal_manager.create_goal(title, category, due_date, priority)
            due_str = f" — due {due_date}" if due_date else ""
            await update.message.reply_text(f"Goal created: {title} [{category}]{due_str}")
        except ValueError as e:
            await update.message.reply_text(f"Error creating goal: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in _handle_addgoal_reply")
            await update.message.reply_text(f"Error creating goal: {_safe_error(e)}")
        finally:
            context.user_data["awaiting_addgoal_reply"] = False

    def _list_goals_text(self, category: Optional[str] = None, status: Optional[str] = "active") -> str:
        """Return formatted goals list text. Called by cmd_goals and tool dispatch."""
        goals = self._goal_manager.list_goals(category=category, status=status)
        self._last_goal_set = goals
        self._active_list = self._last_goal_set

        if not goals:
            return "No goals found."

        lines = []
        header = f"{status.capitalize() if status else 'All'} goals ({len(goals)} total):"
        lines.append(header)

        for i, path in enumerate(goals, 1):
            fm = self._parse_frontmatter(path)
            cat = fm.get("category", "")
            title = fm.get("source_title", "")
            due = fm.get("due_date")
            if due:
                try:
                    due_dt = datetime.strptime(due, "%Y-%m-%d")
                    days_until = (due_dt - datetime.now()).days
                    if days_until < 0:
                        due_str = f"was due {due} ⚠️ OVERDUE"
                    elif days_until <= 7:
                        due_str = f"due {due} ⚠️ {days_until} days"
                    else:
                        due_str = f"due {due}"
                except ValueError:
                    due_str = f"due {due}"
            else:
                due_str = "no due date"
            lines.append(f"{i}. [{cat}] {title} — {due_str}")

        return "\n".join(lines)

    async def cmd_goals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        # Parse optional filter argument
        category = None
        status: Optional[str] = "active"
        if context.args:
            arg = context.args[0]
            if arg in ["active", "completed", "abandoned"]:
                status = arg
            else:
                category = arg
                status = None

        try:
            goals_text = self._list_goals_text(category=category, status=status)
            await update.message.reply_text(goals_text)
            self._record_command_reply(update.effective_chat.id, "goals", goals_text)
        except Exception as e:
            log.exception("Error in cmd_goals")
            await update.message.reply_text(f"Error listing goals: {_safe_error(e)}")

    async def cmd_goal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /goal N")
            return

        # Verb-dispatch: first arg is not a number → look it up in the Goals group
        first = context.args[0]
        try:
            int(first)
        except ValueError:
            matched = self._match_verb_in_group("Goals", "goal", first.lstrip("/"))
            if matched:
                handler = getattr(self, f"cmd_{matched}", None)
                if handler is not None:
                    context.args = context.args[1:]
                    return await handler(update, context)
            # Fall through to resolver (which renders dynamic help for non-integer)

        path, err = self._resolve_goal_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            category = fm.get("category", "")
            status = fm.get("status", "")
            due_date = fm.get("due_date") or "none"
            priority = fm.get("priority", "medium")
            linked_projects = fm.get("linked_projects", [])
            notes = fm.get("notes", "")

            # Format linked projects
            if linked_projects:
                linked_str = ", ".join(linked_projects)
            else:
                linked_str = "none"

            lines = [
                f"{title} [{category}] — {status}",
                f"Due: {due_date} · Priority: {priority}",
                f"Linked projects: {linked_str}",
                "",
                f"Notes: {notes or '—'}",
            ]

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("Error in cmd_goal")
            await update.message.reply_text(f"Error showing goal: {_safe_error(e)}")

    async def cmd_completegoal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /completegoal N")
            return

        path, err = self._resolve_goal_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.update_goal_status(path, "completed")
            await update.message.reply_text(f"✓ Goal completed: \"{title}\"")
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_completegoal")
            await update.message.reply_text(f"Error completing goal: {_safe_error(e)}")

    async def cmd_abandongoal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /abandongoal N")
            return

        path, err = self._resolve_goal_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.update_goal_status(path, "abandoned")
            await update.message.reply_text(f"✗ Goal abandoned: \"{title}\"")
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_abandongoal")
            await update.message.reply_text(f"Error abandoning goal: {_safe_error(e)}")

    async def cmd_goal_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /goal_note <N> <text>")
            return

        path, err = self._resolve_goal_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        note = " ".join(context.args[1:])
        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.append_goal_note(path, note)
            await update.message.reply_text(f"Note added to \"{title}\".")
        except Exception as e:
            log.exception("Error in cmd_goal_note")
            await update.message.reply_text(f"Error adding note: {_safe_error(e)}")

    async def cmd_goal_due(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /goal_due <N> <YYYY-MM-DD|none>")
            return

        path, err = self._resolve_goal_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        due_date = context.args[1]
        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.update_goal_due(path, due_date)
            cleared = due_date.lower() == "none"
            msg = f"Due date cleared for \"{title}\"." if cleared else f"Due date set to {due_date} for \"{title}\"."
            await update.message.reply_text(msg)
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_goal_due")
            await update.message.reply_text(f"Error updating due date: {_safe_error(e)}")

    # ── Projects commands ─────────────────────────────────────────────────────

    async def cmd_addproject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        # Store user ID in context for the reply handler
        context.user_data["awaiting_addproject_reply"] = True
        context.user_data["addproject_start_time"] = asyncio.get_event_loop().time()

        await update.message.reply_text(
            "Add a new project. Reply with:\n"
            "title: what you're working on\n"
            "category: personal | work | family | learning | other\n"
            "due: YYYY-MM-DD or \"none\"\n"
            "goal: N (optional, link to a goal from last /goals list)"
        )

    async def _handle_addproject_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle reply to /addproject command."""
        text = update.message.text.strip()

        # Parse structured reply
        parsed = {}
        for line in text.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                parsed[key.strip().lower()] = val.strip()

        # Validate required fields
        required = {"title", "category"}
        missing = required - set(parsed.keys())
        if missing:
            await update.message.reply_text(
                f"Missing required fields: {', '.join(missing)}. Please try /addproject again."
            )
            context.user_data["awaiting_addproject_reply"] = False
            return

        title = parsed["title"]
        category = parsed["category"]
        due_date = parsed.get("due")
        if due_date and due_date.lower() == "none":
            due_date = None

        # Resolve linked goal if provided
        linked_goal = None
        if "goal" in parsed:
            goal_idx = parsed["goal"]
            goal_path, goal_err = self._resolve_goal_index(goal_idx)
            if goal_path:
                linked_goal = goal_path.name
            else:
                await update.message.reply_text(
                    f"Invalid goal index: {goal_idx}. {goal_err}"
                )
                context.user_data["awaiting_addproject_reply"] = False
                return

        try:
            path = self._goal_manager.create_project(title, category, due_date, linked_goal)
            due_str = f" — due {due_date}" if due_date else ""
            goal_str = f" (linked to {linked_goal})" if linked_goal else ""
            await update.message.reply_text(f"Project created: {title} [{category}]{due_str}{goal_str}")
        except ValueError as e:
            await update.message.reply_text(f"Error creating project: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in _handle_addproject_reply")
            await update.message.reply_text(f"Error creating project: {_safe_error(e)}")
        finally:
            context.user_data["awaiting_addproject_reply"] = False

    async def _list_projects_text(self, category: str = None, limit: int = 50) -> str:
        """Return formatted projects list text (called by cmd_projects and tool dispatch)."""
        limit = max(1, min(limit, 100))
        # "code" maps to code-repo memory files with hostname grouping, not GoalManager
        if category == "code":
            return await self._list_code_text(limit=limit)
        projects = self._goal_manager.list_projects(category=category, status="active")
        self._last_project_set = projects
        self._active_list = self._last_project_set

        if not projects:
            return "No active projects found."

        lines = [f"Active projects ({len(projects)} total):"]
        today = datetime.now().date()

        for i, path in enumerate(projects[:limit], 1):
            fm = self._parse_frontmatter(path)
            cat = fm.get("category", "")
            title = fm.get("source_title", "")
            proj_status = fm.get("status", "")
            due = fm.get("due_date")
            if due:
                try:
                    overdue = datetime.strptime(str(due), "%Y-%m-%d").date() < today
                except ValueError:
                    overdue = False
                due_str = f"was due {due} ⚠️ OVERDUE" if overdue else f"due {due}"
            else:
                due_str = "no due date"

            milestones = fm.get("milestones", [])
            if milestones:
                done_count = sum(1 for m in milestones if m.get("done"))
                milestone_str = f" (milestones: {done_count}/{len(milestones)} done)"
            else:
                milestone_str = ""

            lines.append(f"{i}. [{cat}] {title} — {proj_status} — {due_str}{milestone_str}")

        if len(projects) > limit:
            lines.append(f"... and {len(projects) - limit} more.")

        return "\n".join(lines)

    async def cmd_projects(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        # Parse optional filter argument
        category = None
        status = "active"  # default
        if context.args:
            arg = context.args[0]
            # Check if it's a status
            if arg in ["active", "completed", "abandoned", "on-hold"]:
                status = arg
            # Otherwise treat as category
            else:
                category = arg
                status = None

        try:
            if status != "active":
                # Non-active status requests: list directly without _list_projects_text
                projects = self._goal_manager.list_projects(category=category, status=status)
                self._last_project_set = projects
                self._active_list = self._last_project_set
                if not projects:
                    await update.message.reply_text("No projects found.")
                    return
                lines = [f"{status.capitalize() if status else 'All'} projects ({len(projects)} total):"]
                today = datetime.now().date()
                for i, path in enumerate(projects, 1):
                    fm = self._parse_frontmatter(path)
                    cat = fm.get("category", "")
                    title = fm.get("source_title", "")
                    proj_status = fm.get("status", "")
                    due = fm.get("due_date")
                    if due:
                        try:
                            overdue = datetime.strptime(str(due), "%Y-%m-%d").date() < today
                        except ValueError:
                            overdue = False
                        due_str = f"was due {due} ⚠️ OVERDUE" if overdue else f"due {due}"
                    else:
                        due_str = "no due date"
                    milestones = fm.get("milestones", [])
                    if milestones:
                        done_count = sum(1 for m in milestones if m.get("done"))
                        milestone_str = f" (milestones: {done_count}/{len(milestones)} done)"
                    else:
                        milestone_str = ""
                    lines.append(f"{i}. [{cat}] {title} — {proj_status} — {due_str}{milestone_str}")
                projects_text = "\n".join(lines)
                await update.message.reply_text(projects_text)
                self._record_command_reply(update.effective_chat.id, "projects", projects_text)
            else:
                text = await self._list_projects_text(category=category, limit=100)
                await update.message.reply_text(text)
                self._record_command_reply(update.effective_chat.id, "projects", text)
        except Exception as e:
            log.exception("Error in cmd_projects")
            await update.message.reply_text(f"Error listing projects: {_safe_error(e)}")

    async def cmd_project(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /project N")
            return

        # Verb-dispatch: first arg is not a number → look it up in the Projects group
        first = context.args[0]
        try:
            int(first)
        except ValueError:
            matched = self._match_verb_in_group("Projects", "project", first.lstrip("/"))
            if matched:
                handler = getattr(self, f"cmd_{matched}", None)
                if handler is not None:
                    context.args = context.args[1:]
                    return await handler(update, context)
            # Fall through to resolver (which renders dynamic help for non-integer)

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            category = fm.get("category", "")
            status = fm.get("status", "")
            due_date = fm.get("due_date") or "none"
            priority = fm.get("priority", "medium")
            linked_goal = fm.get("linked_goal") or "none"
            milestones = fm.get("milestones", [])
            notes = fm.get("notes", "")

            lines = [
                f"{title} [{category}] — {status}",
                f"Due: {due_date} · Priority: {priority}",
                f"Linked goal: {linked_goal}",
                "",
            ]

            # Format milestones
            if milestones:
                lines.append("Milestones:")
                for i, m in enumerate(milestones, 1):
                    check = "✓" if m.get("done") else "○"
                    text = m.get("text", "")
                    lines.append(f"  {check} {text}")
                lines.append("")
                lines.append("Use /milestone N M to toggle a milestone.")
            else:
                lines.append("No milestones. Use /addmilestone N text to add one.")

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("Error in cmd_project")
            await update.message.reply_text(f"Error showing project: {_safe_error(e)}")

    async def cmd_completeproject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /completeproject N")
            return

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.update_project_status(path, "completed")
            await update.message.reply_text(f"✓ Project completed: \"{title}\"")
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_completeproject")
            await update.message.reply_text(f"Error completing project: {_safe_error(e)}")

    async def cmd_abandonproject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /abandonproject N")
            return

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.update_project_status(path, "abandoned")
            await update.message.reply_text(f"✗ Project abandoned: \"{title}\"")
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_abandonproject")
            await update.message.reply_text(f"Error abandoning project: {_safe_error(e)}")

    async def cmd_holdproject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /holdproject N")
            return

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.update_project_status(path, "on-hold")
            await update.message.reply_text(f"⏸ Project on hold: \"{title}\"")
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_holdproject")
            await update.message.reply_text(f"Error putting project on hold: {_safe_error(e)}")

    async def cmd_project_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /project_note <N> <text>")
            return

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        note = " ".join(context.args[1:])
        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.append_project_note(path, note)
            await update.message.reply_text(f"Note added to \"{title}\".")
        except Exception as e:
            log.exception("Error in cmd_project_note")
            await update.message.reply_text(f"Error adding note: {_safe_error(e)}")

    async def cmd_project_due(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /project_due <N> <YYYY-MM-DD|none>")
            return

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        due_date = context.args[1]
        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.update_project_due(path, due_date)
            cleared = due_date.lower() == "none"
            msg = f"Due date cleared for \"{title}\"." if cleared else f"Due date set to {due_date} for \"{title}\"."
            await update.message.reply_text(msg)
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_project_due")
            await update.message.reply_text(f"Error updating due date: {_safe_error(e)}")

    async def cmd_addmilestone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /addmilestone N text")
            return

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        # Join the rest of args as milestone text
        text = " ".join(context.args[1:])

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.add_milestone(path, text)
            await update.message.reply_text(f"Milestone added to \"{title}\": {text}")
        except Exception as e:
            log.exception("Error in cmd_addmilestone")
            await update.message.reply_text(f"Error adding milestone: {_safe_error(e)}")

    async def cmd_milestone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /milestone N M")
            return

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            milestone_idx = int(context.args[1])
        except ValueError:
            await update.message.reply_text("Invalid milestone index.")
            return

        try:
            self._goal_manager.toggle_milestone(path, milestone_idx)
            # Read updated frontmatter to get new state
            fm = self._parse_frontmatter(path)
            milestones = fm.get("milestones", [])
            if 1 <= milestone_idx <= len(milestones):
                done = milestones[milestone_idx - 1].get("done")
                status = "✓ Milestone marked done" if done else "○ Milestone marked undone"
                await update.message.reply_text(status)
            else:
                await update.message.reply_text("Milestone toggled.")
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_milestone")
            await update.message.reply_text(f"Error toggling milestone: {_safe_error(e)}")

    async def cmd_linkgoal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /linkgoal <project_N> <goal_M>")
            return

        project_path, proj_err = self._resolve_project_index(context.args[0])
        if project_path is None:
            await update.message.reply_text(proj_err)
            return

        goal_path, goal_err = self._resolve_goal_index(context.args[1])
        if goal_path is None:
            await update.message.reply_text(goal_err)
            return

        try:
            project_fm = self._parse_frontmatter(project_path)
            goal_fm = self._parse_frontmatter(goal_path)
            project_title = project_fm.get("source_title", "")
            goal_title = goal_fm.get("source_title", "")

            self._goal_manager.link_goal_to_project(project_path, goal_path)
            await update.message.reply_text(f"Linked \"{project_title}\" to goal \"{goal_title}\"")
        except ValueError as e:
            await update.message.reply_text(f"Error: {_safe_error(e)}")
        except Exception as e:
            log.exception("Error in cmd_linkgoal")
            await update.message.reply_text(f"Error linking goal: {_safe_error(e)}")

    async def cmd_unlinkgoal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /unlinkgoal N")
            return

        path, err = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text(err)
            return

        try:
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title", "")
            self._goal_manager.unlink_goal_from_project(path)
            await update.message.reply_text(f"Unlinked \"{title}\" from its goal.")
        except Exception as e:
            log.exception("Error in cmd_unlinkgoal")
            await update.message.reply_text(f"Error unlinking goal: {_safe_error(e)}")

    # ── /changes command ──────────────────────────────────────────────────────

    async def cmd_changes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show project/goal activity digest for the last N hours (default 24)."""
        if not self._check_auth(update):
            return

        hours = 24
        if context.args:
            try:
                hours = int(context.args[0])
                if hours < 1 or hours > 168:
                    await update.message.reply_text("Hours must be between 1 and 168.")
                    return
            except ValueError:
                await update.message.reply_text("Usage: /changes [hours]  e.g. /changes 48")
                return

        await update.message.reply_text(f"Scanning activity for the last {hours}h…")

        from goal_project_agent import GoalProjectAgent
        agent = GoalProjectAgent(role="full", cache=self._cache)

        try:
            results = await agent.generate_change_digest(hours=hours)
        except Exception as e:
            log.exception("Error in cmd_changes")
            await update.message.reply_text(f"Error generating digest: {_safe_error(e)}")
            return

        if not results:
            await update.message.reply_text(
                f"No project/goal activity in the last {hours}h."
            )
            return

        header = f"Project/goal activity — last {hours}h ({len(results)} item{'s' if len(results) != 1 else ''})\n"
        parts = [header]
        for i, item in enumerate(results, 1):
            icon = "\U0001f3af" if item["type"] == "goal" else "\U0001f4cb"
            count = item["memory_count"]
            count_str = f"{count} update{'s' if count != 1 else ''}"
            parts.append(
                f"{i}. {icon} {item['title']}  ({count_str})\n{item['summary']}"
            )

        msg = "\n\n".join(parts)
        await self._send_reply(update, msg)

    # ── /contacts command ─────────────────────────────────────────────────────

    async def _list_contacts_text(self, limit: int = 30) -> str:
        """Return formatted contacts list text (called by cmd_contacts and tool dispatch)."""
        limit = max(1, min(limit, 200))
        rows = await self._cache.query_by_type("contact")
        if not rows:
            return "No contacts found."

        memories_dir = BRAIN_DIR / "memories"

        # Load frontmatter and sort by last_interaction descending
        contacts = []
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                continue
            if fm.get("type") != "contact":
                continue
            f = memories_dir / row["filename"]
            contacts.append((f, fm))

        total = len(contacts)
        contacts.sort(
            key=lambda x: x[1].get("last_interaction") or "",
            reverse=True
        )
        contacts = contacts[:limit]
        self._last_contact_set = [f for f, _ in contacts]

        lines = [f"Contacts ({total} total):"]

        for i, (f, fm) in enumerate(contacts, 1):
            name = fm.get("name", "(no name)")
            last_interaction = (fm.get("last_interaction") or "")[:10]
            score = fm.get("relationship_score", 0.0)
            lines.append(f"{i}. {name} — last: {last_interaction} — score: {score}")

        lines.append("\nUse /contact <name> or /contact <N> for details.")
        return "\n".join(lines)

    async def cmd_contacts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        try:
            limit = int(context.args[0]) if context.args else 20
        except (ValueError, IndexError):
            limit = 20
        text = await self._list_contacts_text(limit)
        await update.message.reply_text(text)
        self._record_command_reply(update.effective_chat.id, "contacts", text)

    # ── /contact command ──────────────────────────────────────────────────────

    def _resolve_contact_index(self, n: str):
        """Convert 1-based index string to a Path from _last_contact_set, or None."""
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_contact_set):
            return self._last_contact_set[idx]
        return None

    async def _find_contact_by_name(self, query: str):
        """Find contact file by case-insensitive substring match on name field."""
        query_lower = query.lower()
        rows = await self._cache.query_by_type("contact")
        memories_dir = BRAIN_DIR / "memories"

        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                continue
            if fm.get("type") != "contact":
                continue
            name = fm.get("name", "")
            if query_lower in name.lower():
                return memories_dir / row["filename"], fm

        return None, None

    async def cmd_contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /contact <name or N>")
            return

        arg = " ".join(context.args)

        # Try index resolution first
        path = self._resolve_contact_index(arg)
        if path:
            row = await self._cache.get(path.name)
            try:
                fm = json.loads((row or {}).get("frontmatter") or "{}")
            except Exception:
                fm = {}
        else:
            # Try name match
            path, fm = await self._find_contact_by_name(arg)
            if not path:
                await update.message.reply_text(
                    f"No contact found for '{arg}'. Try /contacts to browse."
                )
                return

        # Build response
        name = fm.get("name", "(no name)")
        emails = fm.get("emails", [])
        email_str = ", ".join(emails) if emails else "no email"
        score = fm.get("relationship_score", 0.0)
        interaction_count = fm.get("interaction_count", 0)

        lines = [
            f"{name} ({email_str})",
            f"Relationship score: {score} | {interaction_count} interactions",
            "",
        ]

        # Find open commitments involving this contact
        commitment_rows = await self._cache.query_by_prefix("commitment-")
        open_commitments = []
        for row in commitment_rows:
            try:
                cfm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                continue
            if cfm.get("status") != "active":
                continue
            # Match by name or email
            owner = cfm.get("owner", "")
            recipient = cfm.get("recipient", "")
            owner_email = cfm.get("owner_email", "")
            if name in (owner, recipient) or (emails and owner_email in emails):
                open_commitments.append(cfm)

        if open_commitments:
            lines.append("Open commitments:")
            for cfm in open_commitments[:5]:
                ct = cfm.get("commitment_type", "outbound")
                desc = (cfm.get("source_title") or "")[:60]
                due = cfm.get("due_date")
                due_str = f"due {due}" if due else "due unknown"
                direction = "outbound" if ct == "outbound" else "waiting_on"
                lines.append(f"• [{direction}] {desc} — {due_str}")
            lines.append("")

        # Add summary from file body
        try:
            row = await self._cache.get(path.name)
            content = (row or {}).get("body") or ""
            # Extract summary from Recent Interactions section
            m = re.search(r'## Recent Interactions\n\n(.*?)(?=\n\n##|\Z)', content, re.DOTALL)
            if m:
                summary = m.group(1).strip()[:400]
                lines.append("Summary:")
                lines.append(summary)
        except Exception:
            pass

        await update.message.reply_text("\n".join(lines))

    # ── Review commands ───────────────────────────────────────────────────────

    def _resolve_candidate_index(self, n_str: str) -> Optional[Path]:
        """Convert 1-based index string to a Path from _last_candidate_set, or None."""
        try:
            idx = int(n_str) - 1
            if 0 <= idx < len(self._last_candidate_set):
                return self._last_candidate_set[idx]
        except (ValueError, TypeError):
            pass
        return None

    def _show_candidate_detail(self, path: Path) -> str:
        """Format a detail block for one candidate."""
        try:
            content = path.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) < 3:
                return "Invalid candidate file format."
            fm = yaml.safe_load(parts[1]) or {}
        except Exception as e:
            return f"Error reading candidate: {e}"

        candidate_type = fm.get("candidate_type", "")
        extracted = fm.get("extracted_fields", {})
        source_title = fm.get("source_title", "Untitled")

        if candidate_type == "project":
            category_guess = fm.get("category_guess", "other")
            confidence = fm.get("confidence", 0.0)
            summary = fm.get("summary", "")
            evidence = fm.get("evidence", [])
            due_date_guess = extracted.get("due_date", "")

            lines = [
                f"{source_title} [{category_guess}] — confidence {int(confidence * 100)}%",
                f"Summary: {summary}",
                f"Evidence: {', '.join(evidence)}",
            ]
            if due_date_guess:
                lines.append(f"Due date guess: {due_date_guess}")
            lines.append("")
            lines.append("Use /confirm N [category] to confirm as a project.")
            lines.append("Use /reject N to reject.")
            lines.append("Use /edit N field=value to update a field before confirming.")
            return "\n".join(lines)

        elif candidate_type == "code_repo":
            local_path = extracted.get("local_path", "")
            branch = extracted.get("default_branch", "main")
            languages = extracted.get("languages", [])
            lang_str = ", ".join(languages) if languages else ""

            lines = [
                f"{source_title} (code repository)",
                f"Path: {local_path}",
                f"Branch: {branch}",
            ]
            if lang_str:
                lines.append(f"Languages: {lang_str}")
            lines.append("")
            lines.append("Use /confirm N to add to code index.")
            lines.append("Use /reject N to ignore this repo.")
            return "\n".join(lines)

        else:
            return f"Unknown candidate type: {candidate_type}"

    async def cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unified inbox: count all pending review items across candidates, actions, and skill drafts."""
        if not self._check_auth(update):
            return

        # Project candidates
        rows = await self._cache.query_by_prefix("project-candidate")
        candidate_count = 0
        for row in rows:
            try:
                fm = json.loads(row["frontmatter"]) if isinstance(row["frontmatter"], str) else row["frontmatter"]
            except Exception:
                continue
            if fm.get("type") == "project_candidate" and fm.get("status") == "pending_confirmation":
                candidate_count += 1

        # Agent actions — count only; must not clobber _last_action_set
        actions = await self._load_action_set(update_last_set=False)
        action_count = len(actions)

        # Skill drafts
        draft_count = 0
        if self.skill_creator is not None:
            try:
                draft_count = len(self.skill_creator.list_pending_drafts())
            except Exception:
                pass

        total = candidate_count + action_count + draft_count

        if total == 0:
            await update.message.reply_text("Nothing pending review.")
            return

        lines = [f"Pending review ({total} total):", ""]
        if candidate_count:
            noun = "candidate" if candidate_count == 1 else "candidates"
            lines.append(f"• {candidate_count} project {noun} — /review")
        if action_count:
            noun = "action" if action_count == 1 else "actions"
            lines.append(f"• {action_count} agent {noun} — /actions")
        if draft_count:
            noun = "draft" if draft_count == 1 else "drafts"
            lines.append(f"• {draft_count} skill {noun} — /skill_drafts")
        await update.message.reply_text("\n".join(lines))

    async def cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List pending project/repo candidates, or show detail of candidate N."""
        if not self._check_auth(update):
            return

        # Use cache to avoid reading every candidate file (can be 500+ on active installs).
        # cmd_review_purge uses the same pattern — keep them consistent.
        rows = await self._cache.query_by_prefix("project-candidate")

        project_candidates = []  # list of (Path, fm_dict)
        code_candidates = []     # list of (Path, fm_dict)

        for row in rows:
            try:
                fm = json.loads(row["frontmatter"]) if isinstance(row["frontmatter"], str) else row["frontmatter"]
            except Exception:
                continue
            if fm.get("type") != "project_candidate" or fm.get("status") != "pending_confirmation":
                continue
            path = BRAIN_DIR / "memories" / row["filename"]
            candidate_type = fm.get("candidate_type", "")
            if candidate_type == "project":
                project_candidates.append((path, fm))
            elif candidate_type == "code_repo":
                code_candidates.append((path, fm))

        # Sort by created descending (newest first) using cached frontmatter — no extra reads
        project_candidates.sort(key=lambda x: x[1].get("created", ""), reverse=True)
        code_candidates.sort(key=lambda x: x[1].get("created", ""), reverse=True)

        # Populate session result set (paths only, for confirm/reject/edit resolution)
        all_candidates = project_candidates + code_candidates
        self._last_candidate_set = [item[0] for item in all_candidates]
        self._active_list = self._last_candidate_set

        # If args[0] is a number, show detail
        if context.args:
            # Verb-dispatch: first arg is not a number → look it up in the Review group
            first = context.args[0]
            try:
                int(first)
            except ValueError:
                matched = self._match_verb_in_group("Review", "review", first.lstrip("/"))
                if matched:
                    handler = getattr(self, f"cmd_{matched}", None)
                    if handler is not None:
                        context.args = context.args[1:]
                        return await handler(update, context)
                # Fall through to list view for unknown verbs
                pass
            else:
                # Was a number — show detail
                n = int(first)
                path = self._resolve_candidate_index(str(n))
                if path is None:
                    await update.message.reply_text(self._format_group_help("Review"))
                    return
                detail = self._show_candidate_detail(path)
                await update.message.reply_text(detail)
                return

        # Show list view
        if not self._last_candidate_set:
            await update.message.reply_text("No pending candidates.")
            return

        total = len(self._last_candidate_set)
        lines = [f"Pending candidates ({total} total):"]

        if project_candidates:
            lines.append(f"\nProjects ({len(project_candidates)}):")
            for i, (f, fm) in enumerate(project_candidates, 1):
                title = fm.get("source_title", "Untitled").replace(" (candidate)", "")
                category = fm.get("category_guess", "other")
                confidence = fm.get("confidence", 0.0)
                evidence = fm.get("evidence", [])
                evidence_str = f"from {evidence[0]}" if evidence else ""
                lines.append(
                    f"{i}. {title} — {category} — confidence {int(confidence * 100)}% ({evidence_str})"
                )

        if code_candidates:
            start_idx = len(project_candidates) + 1
            lines.append(f"\nCode repos ({len(code_candidates)}):")
            for i, (f, fm) in enumerate(code_candidates, start_idx):
                title = fm.get("source_title", "Untitled").replace(" (candidate)", "")
                lines.append(f"{i}. {title} — awaiting confirmation")

        lines.append("\nUse /review N to see details, /confirm N [category] to confirm, /reject N to reject.")
        await update.message.reply_text("\n".join(lines))

    async def cmd_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm candidate N, optionally overriding category."""
        if not self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /confirm N [category]")
            return

        # Resolve candidate path
        path = self._resolve_candidate_index(context.args[0])
        if path is None:
            await update.message.reply_text(self._format_group_help("Review"))
            return

        # Read candidate frontmatter
        try:
            content = path.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) < 3:
                await update.message.reply_text("Invalid candidate file format.")
                return
            fm = yaml.safe_load(parts[1]) or {}
        except Exception as e:
            await update.message.reply_text(f"Error reading candidate: {_safe_error(e)}")
            return

        candidate_type = fm.get("candidate_type", "")
        extracted = fm.get("extracted_fields", {})
        source_title = fm.get("source_title", "Untitled")

        if candidate_type == "project":
            # Get category: from args[1] (override), else category_guess
            category = context.args[1] if len(context.args) > 1 else fm.get("category_guess")
            if not category or category == "code":
                await update.message.reply_text(
                    "Invalid category. Must specify a non-code category or candidate must have a valid category_guess."
                )
                return

            # Load config to validate category
            try:
                from utils import load_config
                config = load_config(BRAIN_DIR / "config.yaml")
                valid_categories = config.get("goals", {}).get(
                    "categories",
                    ["personal", "work", "family", "learning", "other"]
                )
                if category not in valid_categories:
                    await update.message.reply_text(
                        f"Invalid category '{category}'. Must be one of: {valid_categories}"
                    )
                    return
            except Exception:
                pass  # Continue if config read fails

            # Confirm via GoalManager
            try:
                from goals_tracker import GoalManager
                manager = GoalManager(BRAIN_DIR / "memories", config)
                created_path = manager.confirm_candidate(path, category_override=category)
                await self._cache.invalidate(path.name)
                if created_path is not None:
                    await self._cache.invalidate(created_path.name)
                title = extracted.get("title", source_title.replace(" (candidate)", ""))
                await update.message.reply_text(f"Project confirmed: \"{title}\" [{category}]")
            except ValueError as e:
                await update.message.reply_text(f"Error: {_safe_error(e)}")
            except Exception as e:
                log.exception("Error confirming project candidate")
                await update.message.reply_text(f"Error confirming candidate: {_safe_error(e)}")

        elif candidate_type == "code_repo":
            # Write code file directly
            try:
                import socket as _socket
                from datetime import datetime, timedelta

                hostname = _socket.gethostname().split(".")[0]
                name = extracted.get("name", "")
                if not name:
                    await update.message.reply_text("Code candidate missing name field.")
                    return

                now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                memory_path = BRAIN_DIR / "memories" / f"code-{hostname}-{name}.md"

                # Build frontmatter from extracted_fields
                code_fm = {
                    "source_title": name,
                    "summary": extracted.get("summary", ""),
                    "tags": extracted.get("tags", []),
                    "last_scanned": now,
                    "source_url": extracted.get("remote_url", ""),
                    "type": "code",
                    "hostname": hostname,
                    "local_path": extracted.get("local_path", ""),
                    "default_branch": extracted.get("default_branch", "main"),
                    "languages": extracted.get("languages", []),
                    "head_sha": extracted.get("head_sha", ""),
                }
                frontmatter = yaml.dump(code_fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

                # Simple body
                body = "## Recent Activity\n- (awaiting first scan)\n\n## Related Projects\n- (none detected)\n\n## Active Branches\n- main\n"
                content_out = f"---\n{frontmatter}---\n\n{body}"

                # Atomic write
                tmp_path = memory_path.with_suffix(".tmp")
                tmp_path.write_text(content_out, encoding="utf-8")
                os.rename(str(tmp_path), str(memory_path))

                # Delete candidate and index the new code file
                path.unlink()
                await self._cache.invalidate(path.name)
                await self._cache.invalidate(memory_path.name)

                await update.message.reply_text(f"Repo confirmed: \"{name}\" added to code index")

            except Exception as e:
                log.exception("Error confirming code_repo candidate")
                await update.message.reply_text(f"Error confirming repo: {_safe_error(e)}")

        else:
            await update.message.reply_text(f"Unknown candidate type: {candidate_type}")

    async def cmd_reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reject candidate N."""
        if not self._check_auth(update):
            return

        if not context.args:
            await update.message.reply_text("Usage: /reject N")
            return

        # Resolve candidate path
        path = self._resolve_candidate_index(context.args[0])
        if path is None:
            await update.message.reply_text(self._format_group_help("Review"))
            return

        # Read candidate frontmatter
        try:
            content = path.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) < 3:
                await update.message.reply_text("Invalid candidate file format.")
                return
            fm = yaml.safe_load(parts[1]) or {}
        except Exception as e:
            await update.message.reply_text(f"Error reading candidate: {_safe_error(e)}")
            return

        candidate_type = fm.get("candidate_type", "")
        extracted = fm.get("extracted_fields", {})
        source_title = fm.get("source_title", "Untitled")

        # Load rejected list
        rejected_json_path = DEPLOY_DIR / "rejected-candidates.json"
        if rejected_json_path.exists():
            try:
                rejected_data = yaml.safe_load(rejected_json_path.read_text()) or {}
            except Exception:
                rejected_data = {}
        else:
            rejected_data = {}

        # Append entry
        if candidate_type == "project":
            if "rejected" not in rejected_data:
                rejected_data["rejected"] = []
            rejected_data["rejected"].append({
                "source_title": source_title,
                "evidence": fm.get("evidence", []),
                "rejected_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            })
        elif candidate_type == "code_repo":
            if "rejected_repos" not in rejected_data:
                rejected_data["rejected_repos"] = []
            rejected_data["rejected_repos"].append({
                "source_title": source_title,
                "local_path": extracted.get("local_path", ""),
                "rejected_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            })

        # Atomic write of rejected JSON
        try:
            tmp_path = rejected_json_path.with_suffix(".tmp")
            tmp_path.write_text(yaml.dump(rejected_data, sort_keys=False, allow_unicode=True))
            os.rename(str(tmp_path), str(rejected_json_path))
        except Exception as e:
            await update.message.reply_text(f"Error saving rejected list: {_safe_error(e)}")
            return

        # Delete candidate
        try:
            path.unlink()
            await self._cache.invalidate(path.name)
            await update.message.reply_text(f"Rejected: \"{source_title}\"")
        except Exception as e:
            await update.message.reply_text(f"Error deleting candidate: {_safe_error(e)}")

    async def cmd_review_purge(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Bulk-delete pending candidates older than N days (/review purge [N])."""
        if not self._check_auth(update):
            return

        args = context.args or []
        try:
            days = int(args[0]) if args else 30
        except (ValueError, IndexError):
            days = 30

        if days < 1:
            await update.message.reply_text("Days must be >= 1.")
            return

        cutoff = datetime.now() - timedelta(days=days)

        rows = await self._cache.query_by_prefix("project-candidate")
        deleted = 0
        for row in rows:
            try:
                fm = json.loads(row["frontmatter"]) if isinstance(row["frontmatter"], str) else row["frontmatter"]
            except Exception:
                continue
            if fm.get("status") != "pending_confirmation":
                continue
            created_str = str(fm.get("created", ""))
            try:
                created_dt = datetime.fromisoformat(created_str)
            except Exception:
                created_dt = datetime.fromtimestamp(row["mtime"])
            if created_dt < cutoff:
                try:
                    (BRAIN_DIR / "memories" / row["filename"]).unlink()
                    await self._cache.invalidate(row["filename"])
                    deleted += 1
                except OSError:
                    pass

        await update.message.reply_text(
            f"Purged {deleted} pending candidate(s) older than {days} day(s)."
        )

    async def cmd_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Edit a candidate field: /edit N field=value"""
        if not self._check_auth(update):
            return

        if len(context.args) < 2:
            await update.message.reply_text("Usage: /edit N field=value")
            return

        # Resolve candidate path
        path = self._resolve_candidate_index(context.args[0])
        if path is None:
            await update.message.reply_text(self._format_group_help("Review"))
            return

        # Parse field=value
        assignment = context.args[1]
        if "=" not in assignment:
            await update.message.reply_text("Usage: /edit N field=value")
            return

        key, value = assignment.split("=", 1)
        key = key.strip()
        value = value.strip()

        # Read candidate frontmatter
        try:
            content = path.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) < 3:
                await update.message.reply_text("Invalid candidate file format.")
                return
            fm = yaml.safe_load(parts[1]) or {}
            body = parts[2]
        except Exception as e:
            await update.message.reply_text(f"Error reading candidate: {_safe_error(e)}")
            return

        source_title = fm.get("source_title", "Untitled")

        # Update field (either top-level or in extracted_fields)
        if "extracted_fields" not in fm:
            fm["extracted_fields"] = {}

        # Common fields that should go in extracted_fields
        if key in ["title", "due_date", "tags", "summary"]:
            fm["extracted_fields"][key] = value
        elif key == "category_guess":
            fm["category_guess"] = value
        else:
            # Default: put in extracted_fields
            fm["extracted_fields"][key] = value

        # Write updated frontmatter atomically
        try:
            new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
            new_content = f"---\n{new_fm}---{body}"
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_text(new_content, encoding="utf-8")
            os.rename(str(tmp_path), str(path))
            await self._cache.invalidate(path.name)
            await update.message.reply_text(f"Updated {key} → {value} on candidate \"{source_title}\"")
        except Exception as e:
            log.exception("Error editing candidate")
            await update.message.reply_text(f"Error editing candidate: {_safe_error(e)}")

    # ── /help command ─────────────────────────────────────────────────────────

    async def cmd_backfill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        args = context.args or []
        if not args:
            await self._send_reply(
                update,
                "Usage: /backfill <type> [days] [hostname]\n"
                "Types: readings, email, zoom, calendar, slack, projects"
            )
            return

        memory_type = args[0].lower()
        if memory_type not in BACKFILL_CONFIG:
            await self._send_reply(
                update,
                f"Unknown type '{memory_type}'. Valid types: {', '.join(BACKFILL_CONFIG)}"
            )
            return

        if memory_type not in self.scanners:
            await self._send_reply(
                update,
                f"Scanner for '{memory_type}' not available on this node."
            )
            return

        cfg = BACKFILL_CONFIG[memory_type]
        days = cfg["default_days"]
        hostname_arg = None

        # Parse args: could be [type, days] or [type, hostname] or [type, days, hostname]
        if len(args) >= 2:
            try:
                days = int(args[1])
                if len(args) >= 3:
                    hostname_arg = args[2]
            except ValueError:
                # args[1] is not an int, treat as hostname
                hostname_arg = args[1]

        # Check hostname match
        local_hostname = socket.gethostname()
        if hostname_arg and hostname_arg != local_hostname:
            await self._send_reply(
                update,
                f"Cross-node dispatch not yet implemented. This node is `{local_hostname}`. "
                f"Rerun on `{hostname_arg}` directly."
            )
            return

        # Clamp days
        if cfg["max_days"] > 0:
            days = min(days, cfg["max_days"])

        days_str = f"{days} days" if memory_type != "projects" else "all"
        await self._send_reply(
            update,
            f"Starting backfill of {memory_type} ({days_str}) on {local_hostname}..."
        )

        try:
            result = await self.scanners[memory_type].backfill(days)
            msg = (
                f"Backfill complete: {result['processed']} processed, "
                f"{result['skipped']} skipped, {result['errors']} errors."
            )
            if result.get('notes'):
                msg += f"\n{result['notes']}"
            await self._send_reply(update, msg)
        except Exception as e:
            log.exception("Backfill failed")
            await self._send_reply(update, f"Backfill failed: {e}")

    # Map depth aliases → canonical depth names used by _skill_for_depth
    _DEPTH_ALIASES: dict[str, str] = {
        "1": "quick", "q": "quick", "quick": "quick",
        "2": "standard", "s": "standard", "standard": "standard", "auto": "standard",
        "3": "deep", "d": "deep", "deep": "deep", "detailed": "deep", "note": "deep",
    }

    def _skill_for_depth(self, url: str, content: str, depth: str) -> tuple[str, str]:
        """Return (skill_name, content_type) for the requested depth level.

        The skill name is chosen based on depth; content_type is always auto-detected
        so frontmatter accurately reflects the page type regardless of capture depth.

        quick    → summarize-webpage-quick (concise 3-point capture)
        standard → auto-detect skill via skill_router (default)
        deep     → summarize-webpage-detailed (rich notes, same as /note)
        """
        from skill_router import detect_content_type, SKILL_REGISTRY
        content_type = detect_content_type(url=url, content=content[:3000])
        if depth == "quick":
            return "summarize-webpage-quick", content_type
        if depth == "deep":
            return "summarize-webpage-detailed", content_type
        # standard: auto-detect skill too
        skill_name = SKILL_REGISTRY.get(content_type, "summarize-webpage")
        return skill_name, content_type

    async def cmd_remember(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fetch a URL and save a reading memory: /remember <url> [quick|standard|deep]"""
        if not self._check_auth(update):
            return
        if not context.args or not context.args[0].startswith("http"):
            await update.message.reply_text(
                "Usage: /remember <url> [depth]\n"
                "  quick    — concise 3-point capture (fast)\n"
                "  standard — auto-detect and summarise (default)\n"
                "  deep     — detailed notes, same as /note\n\n"
                "Example: /remember https://example.com/article deep"
            )
            return

        url = context.args[0]
        raw_depth = context.args[1].lower() if len(context.args) > 1 else "standard"
        depth = self._DEPTH_ALIASES.get(raw_depth, "standard")

        await update.message.reply_text(f"📥 Fetching {url[:60]}...")

        try:
            from memory_writer import MemoryWriter

            title, content = await fetch_url_content(url)
            if not content:
                await update.message.reply_text(
                    "Could not fetch content from that URL. "
                    "The page may require JavaScript or block bots."
                )
                return

            skill_name, content_type = self._skill_for_depth(url, content, depth)
            executor = SkillExecutor(skill_name)
            memory_body = await executor.run({"url": url, "title": title or url, "content": content})

            if not memory_body:
                await update.message.reply_text("Summary failed — the LLM returned no content.")
                return

            entry = {
                "url": url,
                "title": title or url,
                "visit_count": 1,
                "browser": "telegram",
                "content_type": content_type,
            }
            filename = await MemoryWriter().write(entry, memory_body, depth=depth)
            preview = memory_body[:300].replace("\n", " ")
            await self._send_reply(
                update,
                f"✅ Saved: {title or url}\n→ {filename}\n\n{preview}…"
            )
        except SkillAuthError as e:
            log.error("cmd_remember: %s", e)
            await update.message.reply_text("Remember failed — invalid API credentials. Check your API keys.")
        except Exception as e:
            log.exception("cmd_remember failed for %s", url)
            await update.message.reply_text(f"Remember failed: {_safe_error(e)}")

    async def cmd_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fetch a URL and save detailed study notes: /note <url>"""
        if not self._check_auth(update):
            return
        if not context.args or not context.args[0].startswith("http"):
            await update.message.reply_text(
                "Usage: /note <url>\nExample: /note https://example.com/paper\n\n"
                "Like /remember but produces richer notes (longer summary, more key points, quotes)."
            )
            return

        url = context.args[0]
        await update.message.reply_text(f"📥 Fetching detailed notes for {url[:60]}...")

        try:
            from memory_writer import MemoryWriter

            title, content = await fetch_url_content(url)
            if not content:
                await update.message.reply_text(
                    "Could not fetch content from that URL. "
                    "The page may require JavaScript or block bots."
                )
                return

            executor = SkillExecutor("summarize-webpage-detailed")
            memory_body = await executor.run({"url": url, "title": title or url, "content": content})

            if not memory_body:
                await update.message.reply_text("Note-taking failed — the LLM returned no content.")
                return

            entry = {
                "url": url,
                "title": title or url,
                "visit_count": 1,
                "browser": "telegram",
                "content_type": "detailed",
            }
            filename = await MemoryWriter().write(entry, memory_body)
            preview = memory_body[:300].replace("\n", " ")
            await self._send_reply(
                update,
                f"✅ Saved detailed notes: {title or url}\n→ {filename}\n\n{preview}…"
            )
        except SkillAuthError as e:
            log.error("cmd_note: %s", e)
            await update.message.reply_text("Note-taking failed — invalid API credentials. Check your API keys.")
        except Exception as e:
            log.exception("cmd_note failed for %s", url)
            await update.message.reply_text(f"Note failed: {_safe_error(e)}")

    async def _handle_document_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle a document file sent directly to the bot — creates a memory from it."""
        if not self._check_auth(update):
            return

        doc = update.message.document
        fname = doc.file_name or "upload"
        mime = (doc.mime_type or "").lower()

        SUPPORTED_MIMES = {"application/pdf", "text/plain", "text/markdown"}
        SUPPORTED_EXTS = (".pdf", ".txt", ".md")
        is_pdf = "pdf" in mime or fname.lower().endswith(".pdf")
        is_text = "text" in mime or any(fname.lower().endswith(e) for e in (".txt", ".md"))

        if not (is_pdf or is_text):
            await update.message.reply_text(
                "Unsupported file type. Send a PDF, .txt, or .md file to create a memory."
            )
            return

        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("File too large (max 20 MB).")
            return

        await update.message.reply_text(f"Processing {fname}…")

        try:
            tg_file = await context.bot.get_file(doc.file_id)
            data = bytes(await tg_file.download_as_bytearray())
        except Exception as e:
            await update.message.reply_text(f"Download failed: {_safe_error(e)}")
            return

        if is_pdf:
            from content_fetcher import _extract_pdf
            title, text = _extract_pdf(data, fname)
        else:
            from pathlib import Path as _Path
            title = _Path(fname).stem.replace("-", " ").replace("_", " ").title()
            text = data.decode("utf-8", errors="replace")[:8000]

        if not text.strip():
            await update.message.reply_text("Couldn't extract any text from that file.")
            return

        source_url = f"file://{fname}"

        try:
            from skill_router import detect_content_type, SKILL_REGISTRY

            content_type = detect_content_type(url=source_url, content=text[:3000])
            skill_name = SKILL_REGISTRY.get(content_type, "summarize-webpage")
            executor = SkillExecutor(skill_name)
            response = await executor.run(
                {"content": text, "url": source_url, "title": title or fname}
            )
        except SkillAuthError as e:
            log.error("Document upload: %s", e)
            await update.message.reply_text("Summarization failed — invalid API credentials. Check your API keys.")
            return
        except Exception as e:
            log.exception("Document upload summarization failed for %s", fname)
            await update.message.reply_text(f"Summarization failed: {_safe_error(e)}")
            return

        if not response:
            await update.message.reply_text("Summarization returned empty — check error.log.")
            return

        try:
            from memory_writer import MemoryWriter

            entry = {
                "url": source_url,
                "title": title or fname,
                "visit_count": 1,
                "browser": "telegram",
                "content_type": content_type,
            }
            mem_path = await MemoryWriter().write(entry, response)
            preview = response[:300].replace("\n", " ")
            await self._send_reply(
                update,
                f"📄 Saved: {title or fname}\n→ {mem_path}\n\n{preview}…"
            )
        except Exception as e:
            log.exception("Document upload memory write failed for %s", fname)
            await update.message.reply_text(f"Failed to write memory: {_safe_error(e)}")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        lines = []
        for group, commands in COMMAND_REGISTRY.items():
            lines.append(f"*{group}*")
            for cmd, desc in commands:
                lines.append(f"  /{cmd} — {desc}")
            lines.append("")
        text = "\n".join(lines).rstrip()
        await self._send_reply(update, text)

    async def cmd_version(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        version_file = Path(__file__).parent / "VERSION"
        version = version_file.read_text().strip() if version_file.exists() else "unknown"
        await update.message.reply_text(f"second-brain v{version}")

    async def cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show LLM token usage summary. /usage [days] or /usage daily."""
        if not self._check_auth(update):
            return
        from usage_tracker import render_usage, render_daily_breakdown
        args = context.args or []
        if args and args[0] == "daily":
            text = render_daily_breakdown()
        else:
            days = 7
            if args:
                try:
                    days = max(1, min(int(args[0]), 90))
                except ValueError:
                    await update.message.reply_text(
                        "Usage: /usage [days] or /usage daily"
                    )
                    return
            text = render_usage(days)
        await self._send_reply(update, text)

    async def cmd_rebuild_cache(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Rebuild the memory cache from scratch."""
        if not self._check_auth(update):
            return

        if self._cache is None:
            await update.message.reply_text("Cache not available (pass-through mode or watcher role).")
            return

        await update.message.reply_text("Rebuilding cache...")
        try:
            count = await self._cache.rebuild()
            await update.message.reply_text(f"Cache rebuilt: {count} files indexed.")
        except Exception as e:
            log.exception("Cache rebuild failed")
            await update.message.reply_text(f"Cache rebuild failed: {_safe_error(e)}")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show last heartbeat time and loop status for all connected instances."""
        if not self._check_auth(update):
            return

        instances = hb.read_all(BRAIN_DIR)
        if not instances:
            await update.message.reply_text(
                "No heartbeat data found.\n"
                "Instances write heartbeat-{hostname}.json to the brain dir "
                "after each scan iteration — data will appear once the first iteration completes."
            )
            return

        now = datetime.now(timezone.utc)
        lines = []
        for inst in instances:
            hostname = inst.get("hostname", "unknown")
            role = inst.get("role", "?")
            version = inst.get("version", "?")
            last_hb = inst.get("last_heartbeat", "")
            try:
                hb_dt = datetime.fromisoformat(last_hb)
                age_s = int((now - hb_dt).total_seconds())
                if age_s < 60:
                    age_str = f"{age_s}s ago"
                elif age_s < 3600:
                    age_str = f"{age_s // 60}m ago"
                else:
                    age_str = f"{age_s // 3600}h ago"
                stale = age_s > 600  # no heartbeat in 10+ minutes
            except Exception:
                age_str = "unknown"
                stale = True

            header = f"[{hostname}] {role} v{version}"
            if stale:
                header += "  [STALE]"
            header += f"  (last seen {age_str})"
            lines.append(header)

            loops = inst.get("loops", {})
            if loops:
                for loop_name, info in sorted(loops.items()):
                    status = info.get("status", "?")
                    last_run = info.get("last_run", "")
                    error = info.get("error")
                    try:
                        run_dt = datetime.fromisoformat(last_run)
                        run_age_s = int((now - run_dt).total_seconds())
                        if run_age_s < 60:
                            run_str = f"{run_age_s}s ago"
                        elif run_age_s < 3600:
                            run_str = f"{run_age_s // 60}m ago"
                        else:
                            run_str = f"{run_age_s // 3600}h ago"
                    except Exception:
                        run_str = "unknown"

                    flag = "OK " if status == "ok" else "ERR"
                    line = f"  {flag}  {loop_name:<32} {run_str}"
                    if status != "ok" and error:
                        scrubbed = re.sub(r'/\S+/\S+', '[path]', error)[:80]
                        line += f"\n       {scrubbed}"
                    lines.append(line)
            else:
                lines.append("  (no loop data yet)")

            lines.append("")

        await self._send_reply(update, "\n".join(lines).rstrip())

    # ── /code command ─────────────────────────────────────────────────────────

    def _resolve_code_index(self, n: str):
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_code_set):
            return self._last_code_set[idx]
        return None

    async def _list_code_text(self, limit: int = 50) -> str:
        """Return formatted code repos list text (called by cmd_code and tool dispatch)."""
        limit = max(1, min(limit, 100))
        memories_dir = BRAIN_DIR / "memories"
        code_rows = await self._cache.query_by_prefix("code-")
        project_rows = await self._cache.query_by_prefix("project-")

        candidates = []  # list of (path, fm)
        for row in code_rows + project_rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                continue
            candidates.append((memories_dir / row["filename"], fm))

        code_repos = []
        for f, fm in candidates:
            if fm.get("type") not in ("code", "project", "code_project"):
                continue
            # Skip if type:project but category is not code
            if fm.get("type") == "project" and fm.get("category") != "code":
                continue
            code_repos.append((f, fm))

        if not code_repos:
            return "No code repos found."

        # Group by base name (hostname-scoped files share same base name)
        # Format: code-{hostname}-{base_name}.md or legacy project-{base_name}.md
        from collections import defaultdict
        groups = defaultdict(list)  # base_name -> [(file, fm, hostname), ...]

        for f, fm in code_repos:
            stem = f.stem
            hostname = fm.get("hostname", "")
            # Extract base name from filename
            if stem.startswith("code-"):
                rest = stem[len("code-"):]
                # Check if hostname-scoped: if hostname field exists, strip hostname- prefix
                if hostname and rest.startswith(f"{hostname}-"):
                    base_name = rest[len(f"{hostname}-"):]
                else:
                    # Legacy file (no hostname in filename)
                    base_name = rest
                groups[base_name].append((f, fm, hostname or "legacy"))
            elif stem.startswith("project-"):
                rest = stem[len("project-"):]
                # Check if hostname-scoped: if hostname field exists, strip hostname- prefix
                if hostname and rest.startswith(f"{hostname}-"):
                    base_name = rest[len(f"{hostname}-"):]
                else:
                    # Legacy file (no hostname in filename)
                    base_name = rest
                groups[base_name].append((f, fm, hostname or "legacy"))

        # Sort groups by most recent scan time across all hosts
        def group_mtime(items):
            return max((fm.get("last_scanned") or "" for _, fm, _ in items), default="")

        sorted_groups = sorted(groups.items(), key=lambda x: group_mtime(x[1]), reverse=True)
        sorted_groups = sorted_groups[:limit]

        # Build flat list for _last_code_set (all files in display order)
        self._last_code_set = []
        for _, items in sorted_groups:
            # Sort by hostname within group for consistent ordering
            items.sort(key=lambda x: x[2])
            for f, _, _ in items:
                self._last_code_set.append(f)

        # Display grouped results
        lines = [f"Code repos ({len(sorted_groups)} shown):"]
        idx = 1
        for base_name, items in sorted_groups:
            # Pick most recent entry for display metadata
            items_sorted = sorted(items, key=lambda x: x[1].get("last_scanned") or "", reverse=True)
            _, fm_latest, _ = items_sorted[0]

            name = fm_latest.get("source_title") or "(no name)"
            last = (fm_latest.get("last_scanned") or "")[:10]

            # Always show host(s) so the LLM can group by laptop.
            # Exclude the "legacy" sentinel written for files that pre-date
            # hostname-scoped naming (hostname or "legacy" in the grouping step).
            hostnames = sorted(set(h for _, _, h in items) - {"legacy"})
            if len(hostnames) > 1:
                host_str = f" · hosts: {', '.join(hostnames)}"
            elif len(hostnames) == 1:
                host_str = f" · host: {hostnames[0]}"
            else:
                host_str = ""

            lines.append(f"{idx}. {name} ({last}){host_str}")
            idx += 1

        return "\n".join(lines)

    async def cmd_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /code command: list repos or show detail if N provided."""
        if not self._check_auth(update):
            return

        args = list(context.args) if context.args else []

        # If arg is a digit, decide: detail index or list limit?
        # Strategy: if we have a populated list and idx is in range → detail.
        # If we have a populated list and idx is out of range but > 50 → error.
        # Otherwise → treat as list limit.
        if args and args[0].isdigit():
            try:
                idx = int(args[0])
                list_size = len(self._last_code_set)
                # If we have a non-empty list, treat numbers as indices
                if list_size > 0:
                    if 1 <= idx <= list_size:
                        # Valid index → show detail
                        await self._cmd_code_detail(update, args[0])
                        return
                    elif idx > 50:
                        # Out of range and too large to be list limit → error
                        await update.message.reply_text(self._format_group_help("Knowledge listings", "code"))
                        return
                    # else: treat as list limit (falls through)
            except ValueError:
                pass

        # Show list with optional limit (when no existing list, or non-digit arg)
        limit = 10
        if args:
            try:
                limit = max(1, min(int(args[0]), 50))
            except ValueError:
                pass

        text = await self._list_code_text(limit)
        await update.message.reply_text(text)

    async def _cmd_code_detail(self, update: Update, index_str: str):
        """Show detail for code repo N."""
        path = self._resolve_code_index(index_str)
        if path is None:
            await update.message.reply_text(self._format_group_help("Knowledge listings", "code"))
            return

        # Find all hosts that have the same base repo
        # Extract base name from the selected file
        memories_dir = BRAIN_DIR / "memories"
        row = await self._cache.get(path.name)
        try:
            fm = json.loads((row or {}).get("frontmatter") or "{}")
        except Exception:
            fm = {}
        stem = path.stem
        hostname = fm.get("hostname", "")

        # Handle both code- and project- prefixes (migration transition)
        if stem.startswith("code-"):
            rest = stem[len("code-"):]
            if hostname and rest.startswith(f"{hostname}-"):
                base_name = rest[len(f"{hostname}-"):]
            else:
                base_name = rest
        elif stem.startswith("project-"):
            rest = stem[len("project-"):]
            if hostname and rest.startswith(f"{hostname}-"):
                base_name = rest[len(f"{hostname}-"):]
            else:
                base_name = rest
        else:
            base_name = stem

        # Find all files with this base name (check both code- and project- prefixes)
        all_files = []
        code_rows = await self._cache.query_by_prefix("code-")
        for f_row in code_rows:
            try:
                f_fm = json.loads(f_row.get("frontmatter") or "{}")
            except Exception:
                continue
            f = memories_dir / f_row["filename"]
            f_stem = f.stem
            f_hostname = f_fm.get("hostname", "")
            if f_stem.startswith("code-"):
                f_rest = f_stem[len("code-"):]
                if f_hostname and f_rest.startswith(f"{f_hostname}-"):
                    f_base = f_rest[len(f"{f_hostname}-"):]
                else:
                    f_base = f_rest
                if f_base == base_name:
                    all_files.append((f, f_fm, f_hostname or "legacy"))

        # Also check legacy project- files
        project_rows = await self._cache.query_by_prefix("project-")
        for f_row in project_rows:
            try:
                f_fm = json.loads(f_row.get("frontmatter") or "{}")
            except Exception:
                continue
            # Only include if type:code or type:project+category:code
            if f_fm.get("type") not in ("code", "project", "code_project"):
                continue
            if f_fm.get("type") == "project" and f_fm.get("category") != "code":
                continue
            f = memories_dir / f_row["filename"]
            f_stem = f.stem
            f_hostname = f_fm.get("hostname", "")
            if f_stem.startswith("project-"):
                f_rest = f_stem[len("project-"):]
                if f_hostname and f_rest.startswith(f"{f_hostname}-"):
                    f_base = f_rest[len(f"{f_hostname}-"):]
                else:
                    f_base = f_rest
                if f_base == base_name:
                    all_files.append((f, f_fm, f_hostname or "legacy"))

        if not all_files:
            await update.message.reply_text("Code repo not found.")
            return

        # Sort by hostname for consistent display
        all_files.sort(key=lambda x: x[2])

        # If only one host, show normal detail view
        if len(all_files) == 1:
            f, fm, _ = all_files[0]
            name = fm.get("source_title") or "(no name)"
            url = fm.get("source_url") or ""
            local = fm.get("local_path") or ""
            langs = ", ".join(fm.get("languages") or []) or "unknown"
            last = (fm.get("last_scanned") or "")[:10]
            summary = fm.get("summary") or ""
            tags = fm.get("tags") or []
            tag_str = f"\nTags: {', '.join(tags)}" if tags else ""

            lines = [
                f"{name}",
                url,
                f"Local: {local}",
                f"Languages: {langs}",
                f"Last scanned: {last}{tag_str}",
                "",
                summary,
            ]
            await update.message.reply_text("\n".join(lines))
        else:
            # Multiple hosts — show stacked entries
            lines = []
            for i, (f, fm, h) in enumerate(all_files):
                if i > 0:
                    lines.append("\n---\n")
                name = fm.get("source_title") or "(no name)"
                url = fm.get("source_url") or ""
                local = fm.get("local_path") or ""
                last = (fm.get("last_scanned") or "")[:10]
                summary = fm.get("summary") or ""

                lines.append(f"{name} @ {h}")
                lines.append(url)
                lines.append(f"Local: {local}")
                lines.append(f"Last scanned: {last}")
                if summary:
                    lines.append("")
                    lines.append(summary)

            await self._send_reply(update, "\n".join(lines))

    # ── /events and /event commands ───────────────────────────────────────────

    def _resolve_event_index(self, n: str):
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_event_set):
            return self._last_event_set[idx]
        return None

    async def _list_events_text(self, limit: int = 20, calendar_filter=None) -> str:
        """Return formatted events list text (called by cmd_events and tool dispatch)."""
        limit = max(1, min(limit, 100))
        memories_dir = BRAIN_DIR / "memories"
        rows = await self._cache.query_by_type("calendar_event")
        all_events = []
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                continue
            if fm.get("type") != "calendar_event":
                continue
            all_events.append((memories_dir / row["filename"], fm))

        if not all_events:
            return "No calendar events found."

        all_events.sort(key=lambda x: x[1].get("start_time") or "", reverse=False)

        if calendar_filter:
            cf = calendar_filter.lower()
            events = [
                (f, fm) for f, fm in all_events
                if any(cf in name.lower() for name in fm.get("calendar_names", []))
            ]
            if not events:
                # Collect distinct calendar names for the hint
                all_cal_names: set[str] = set()
                for _, fm in all_events:
                    for name in fm.get("calendar_names", []):
                        all_cal_names.add(name)
                hint = ", ".join(sorted(all_cal_names)) if all_cal_names else "(none found)"
                return (
                    f"No events found matching calendar '{calendar_filter}'.\n"
                    f"Available calendars: {hint}"
                )
        else:
            events = all_events

        events = events[:limit]
        self._last_event_set = [f for f, _ in events]
        self._active_list = self._last_event_set

        filter_note = f" in '{calendar_filter}'" if calendar_filter else ""
        lines = [f"Calendar events{filter_note} ({len(events)} shown):"]
        for i, (_, fm) in enumerate(events, 1):
            title = (fm.get("source_title") or "(no title)")[:50]
            start = self._fmt_datetime(fm.get("start_time") or "")
            duration = self._fmt_duration(fm.get("start_time"), fm.get("end_time"))
            dur_str = f" ({duration})" if duration else ""
            location = fm.get("location") or ""
            loc_str = f" — {location[:30]}" if location else ""
            lines.append(f"{i}. {start}{dur_str} {title}{loc_str}")
        lines.append("\nUse /event <N> for details.")
        return "\n".join(lines)

    async def cmd_events(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        limit = 10
        calendar_filter = None
        for arg in (context.args or []):
            if arg.isdigit():
                limit = int(arg)
            else:
                calendar_filter = arg
        text = await self._list_events_text(limit, calendar_filter=calendar_filter)
        await update.message.reply_text(text)
        self._record_command_reply(update.effective_chat.id, "events", text)

    async def cmd_event(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /event <N>")
            return

        path = self._resolve_event_index(context.args[0])
        if path is None:
            await update.message.reply_text(self._format_group_help("Knowledge listings", "event"))
            return

        row = await self._cache.get(path.name)
        try:
            fm = json.loads((row or {}).get("frontmatter") or "{}")
        except Exception:
            fm = {}
        title = fm.get("source_title") or "(no title)"
        start = fm.get("start_time") or ""
        end = fm.get("end_time") or ""
        all_day = fm.get("all_day", False)
        location = fm.get("location") or ""
        cal_raw = fm.get("calendar_names", fm.get("calendar_name", ""))
        cal = ", ".join(cal_raw) if isinstance(cal_raw, list) else (cal_raw or "")
        participants = fm.get("participants") or []
        summary = fm.get("summary") or ""

        if all_day:
            time_str = "All day"
        else:
            start_fmt = self._fmt_datetime(start)
            end_fmt = self._fmt_datetime(end)
            duration = self._fmt_duration(start, end)
            dur_str = f" ({duration})" if duration else ""
            time_str = f"{start_fmt} – {end_fmt}{dur_str}"
        parts_str = ", ".join(participants[:10]) if participants else "none listed"
        lines = [
            title,
            f"When: {time_str}",
        ]
        if location:
            lines.append(f"Where: {location}")
        if cal:
            lines.append(f"Calendar: {cal}")
        lines += [f"Attendees: {parts_str}", "", summary]
        await update.message.reply_text("\n".join(lines))

    # ── /notes and /note commands ─────────────────────────────────────────────

    async def _list_notes_text(self, limit: int = 20, folder_filter: Optional[str] = None, todos_only: bool = False) -> str:
        """Return formatted list of Apple Notes memory files."""
        limit = max(1, min(limit, 100))
        memories_dir = BRAIN_DIR / "memories"
        rows = await self._cache.query_by_type("apple_notes")
        all_notes = []
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                continue
            if fm.get("type") != "apple_notes":
                continue
            all_notes.append((memories_dir / row["filename"], fm))

        if not all_notes:
            return "No Apple Notes found. The notes scanner may not have run yet."

        all_notes.sort(key=lambda x: x[1].get("modified") or "", reverse=True)

        if todos_only:
            all_notes = [(f, fm) for f, fm in all_notes if fm.get("has_todos")]
        elif folder_filter:
            ff = folder_filter.lower()
            all_notes = [(f, fm) for f, fm in all_notes if ff in (fm.get("folder") or "").lower()]

        if not all_notes:
            hint = " with todos" if todos_only else f" in folder '{folder_filter}'"
            return f"No Apple Notes found{hint}."

        notes = all_notes[:limit]
        self._last_note_set = [f for f, _ in notes]
        self._active_list = self._last_note_set

        filter_note = " (todos only)" if todos_only else (f" in '{folder_filter}'" if folder_filter else "")
        lines = [f"Apple Notes{filter_note} ({len(notes)} shown of {len(all_notes)}):"]
        for i, (_, fm) in enumerate(notes, 1):
            title = (fm.get("source_title") or "(no title)")[:50]
            folder = fm.get("folder") or ""
            modified = (fm.get("modified") or "")[:10]
            has_todos = " [todo]" if fm.get("has_todos") else ""
            lines.append(f"{i}. [{folder}] {title}{has_todos} — {modified}")
        lines.append("\nUse /notes <N> for full content.")
        return "\n".join(lines)

    def _resolve_note_index(self, n: str) -> Optional[Path]:
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_note_set):
            return self._last_note_set[idx]
        return None

    async def cmd_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List Apple Notes or show detail for note N.

        /notes              — list all notes
        /notes <N>          — show full content of note N from last list
        /notes todos        — list notes flagged as containing todos
        /notes <folder>     — list notes in a specific folder
        """
        if not self._check_auth(update):
            return

        args = list(context.args or [])

        # If first arg is an integer, show detail for that note
        if args and args[0].isdigit():
            path = self._resolve_note_index(args[0])
            if path is None:
                await update.message.reply_text("Run /notes first to build the list.")
                return
            row = await self._cache.get(path.name)
            try:
                fm = json.loads((row or {}).get("frontmatter") or "{}")
            except Exception:
                fm = {}
            title = fm.get("source_title") or "(no title)"
            folder = fm.get("folder") or ""
            modified = (fm.get("modified") or "")[:10]
            has_todos = fm.get("has_todos", False)
            try:
                content_parts = ((row or {}).get("body") or "").split("---", 2)
                body = content_parts[2].strip() if len(content_parts) >= 3 else ""
            except Exception:
                body = ""
            lines = [f"{title}", f"Folder: {folder} | Modified: {modified}"]
            if has_todos:
                lines.append("[Contains checklist/todo items]")
            lines += ["", body[:3500]]
            await self._send_reply(update, "\n".join(lines))
            return

        limit = 20
        folder_filter = None
        todos_only = False
        for arg in args:
            if arg.lower() == "todos":
                todos_only = True
            else:
                folder_filter = arg
        text = await self._list_notes_text(limit, folder_filter=folder_filter, todos_only=todos_only)
        await self._send_reply(update, text)
        self._record_command_reply(update.effective_chat.id, "notes", text)

    # ── /meetings and /meeting commands ──────────────────────────────────────

    def _resolve_meeting_index(self, n: str):
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_meeting_set):
            return self._last_meeting_set[idx]
        return None

    async def _list_meetings_text(self, limit: int = 20) -> str:
        """Return formatted meetings list text (called by cmd_meetings and tool dispatch)."""
        limit = max(1, min(limit, 100))
        memories_dir = BRAIN_DIR / "memories"
        rows = await self._cache.query_by_type("meeting_transcript")
        meetings = []
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                continue
            if fm.get("type") != "meeting_transcript":
                continue
            meetings.append((memories_dir / row["filename"], fm))

        if not meetings:
            return "No meeting transcripts found."

        meetings.sort(key=lambda x: x[1].get("start_time") or x[1].get("created") or "", reverse=True)
        meetings = meetings[:limit]
        self._last_meeting_set = [f for f, _ in meetings]
        self._active_list = self._last_meeting_set

        lines = [f"Meeting transcripts ({len(meetings)} shown):"]
        for i, (_, fm) in enumerate(meetings, 1):
            title = (fm.get("source_title") or "(no title)")[:50]
            date = (str(fm.get("start_time") or fm.get("created") or ""))[:10]
            participants = fm.get("participants") or []
            n_parts = len(participants)
            lines.append(f"{i}. [{date}] {title} — {n_parts} participant{'s' if n_parts != 1 else ''}")
        lines.append("\nUse /meeting <N> for details.")
        return "\n".join(lines)

    async def cmd_meetings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        try:
            limit = int(context.args[0]) if context.args else 10
        except (ValueError, IndexError):
            limit = 10
        text = await self._list_meetings_text(limit)
        await update.message.reply_text(text)

    async def cmd_meeting(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /meeting <N>")
            return

        path = self._resolve_meeting_index(context.args[0])
        if path is None:
            await update.message.reply_text(self._format_group_help("Knowledge listings", "meeting"))
            return

        row = await self._cache.get(path.name)
        try:
            fm = json.loads((row or {}).get("frontmatter") or "{}")
        except Exception:
            fm = {}
        title = fm.get("source_title") or "(no title)"
        date = (str(fm.get("start_time") or fm.get("created") or ""))[:10]
        participants = fm.get("participants") or []
        summary = fm.get("summary") or ""

        parts_str = ", ".join(str(p) for p in participants[:10]) if participants else "none listed"
        lines = [
            title,
            f"Date: {date}",
            f"Attendees: {parts_str}",
            "",
            summary,
        ]
        await update.message.reply_text("\n".join(lines))

    # ── /comms, /comm commands ────────────────────────────────────────────────

    def _resolve_comm_index(self, n: str):
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_comms_set):
            return self._last_comms_set[idx]
        return None

    async def _resolve_feature_index(self, args, update: Update):
        """Convert 1-based index, #N issue-number, or short_id hash to a feature, or None."""
        if not args:
            return None
        arg = str(args[0])
        # Direct GitHub issue reference: #42
        if arg.startswith("#"):
            try:
                return int(arg[1:])
            except ValueError:
                return None
        try:
            n = int(arg)
            idx = n - 1
        except (ValueError, TypeError):
            # Not an integer — try matching by short_id hash in feature files
            memories_dir = BRAIN_DIR / "memories"
            rows = await self._cache.query_by_prefix("feature-request-")
            for row in sorted(rows, key=lambda r: r["filename"]):
                try:
                    fm = json.loads(row.get("frontmatter") or "{}")
                except Exception:
                    fm = {}
                if fm.get("short_id") == arg:
                    return memories_dir / row["filename"]
            return None
        if 0 <= idx < len(self._last_feature_set):
            return self._last_feature_set[idx]
        return None

    def _rewrite_feature_frontmatter(self, path: Path, updates: dict):
        """Update specific frontmatter keys in a feature request file. Preserves body."""
        import os
        text = _safe_read_text(path)
        if text is None:
            return False
        # Split into frontmatter and body
        if not text.startswith("---"):
            return
        parts = text.split("---", 2)
        if len(parts) < 3:
            return
        _, fm_text, body = parts
        fm = yaml.safe_load(fm_text) or {}
        fm.update(updates)
        new_text = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---{body}"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new_text)
        os.rename(tmp, path)

    def _append_feature_note(self, path: Path, note: str):
        """Append a timestamped note to the ## Notes section."""
        import os
        from datetime import datetime, timedelta
        text = _safe_read_text(path)
        if text is None:
            return False
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        note_line = f"- {timestamp}: {note}\n"
        if "## Notes" in text:
            text = text.replace("## Notes\n", f"## Notes\n{note_line}", 1)
        else:
            text += f"\n## Notes\n{note_line}"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.rename(tmp, path)

    # ── GitHub Issues helpers ─────────────────────────────────────────────────────

    def _status_from_labels(self, issue: dict) -> str:
        if issue.get("state") == "closed":
            return "done" if issue.get("state_reason") == "completed" else "wont-do"
        for lb in issue.get("labels", []):
            if lb["name"].startswith("status:"):
                return lb["name"].split(":", 1)[1]
        return "new"

    def _priority_from_labels(self, issue: dict) -> str:
        for lb in issue.get("labels", []):
            if lb["name"].startswith("priority:"):
                return lb["name"].split(":", 1)[1]
        return "medium"

    def _kind_from_labels(self, issue: dict) -> str:
        for lb in issue.get("labels", []):
            if lb["name"].startswith("kind:"):
                return lb["name"].split(":", 1)[1]
        return "feature"

    def _project_from_labels(self, issue: dict) -> str:
        for lb in issue.get("labels", []):
            if lb["name"].startswith("project:"):
                return lb["name"].split(":", 1)[1]
        return ""

    def _tags_from_labels(self, issue: dict) -> list[str]:
        reserved_prefixes = ("kind:", "status:", "priority:", "project:")
        return [lb["name"] for lb in issue.get("labels", [])
                if not any(lb["name"].startswith(p) for p in reserved_prefixes)]

    async def _gh_ensure_labels(self) -> None:
        if not self._labels_bootstrapped:
            try:
                await self.github.ensure_labels(_STANDARD_LABELS)
                self._labels_bootstrapped = True
            except Exception as e:
                log.warning(f"Label bootstrap failed (non-fatal): {e}")

    async def _gh_set_status(self, number: int, status: str) -> None:
        issue = await self.github.get_issue(number)
        existing = [lb["name"] for lb in issue.get("labels", [])]
        non_status = [l for l in existing if not l.startswith("status:")]
        if status == "done":
            await self.github.update_issue(number, state="closed", state_reason="completed")
            await self.github.replace_labels(number, non_status)
        elif status == "wont-do":
            await self.github.update_issue(number, state="closed", state_reason="not_planned")
            await self.github.replace_labels(number, non_status)
        else:
            new_labels = non_status if status == "new" else non_status + [f"status:{status}"]
            await self.github.replace_labels(number, new_labels)
            if issue.get("state") == "closed":
                await self.github.update_issue(number, state="open")

    async def _gh_set_priority(self, number: int, priority: str) -> None:
        issue = await self.github.get_issue(number)
        existing = [lb["name"] for lb in issue.get("labels", [])]
        non_priority = [l for l in existing if not l.startswith("priority:")]
        await self.github.replace_labels(number, non_priority + [f"priority:{priority}"])

    async def _gh_title(self, number: int) -> str:
        issue = await self.github.get_issue(number)
        return issue.get("title", f"#{number}")

    async def _rewrite_features_index_snapshot(self) -> None:
        """Rewrite memories/features-index.md with a compact summary. Keeps index_builder fed."""
        import os as _os
        if not self.github.enabled:
            return
        try:
            open_issues = await self.github.list_issues(state="open", per_page=50)
            closed = await self.github.list_issues(state="closed", per_page=15)
        except Exception as e:
            log.warning(f"features-index snapshot refresh failed: {e}")
            return
        from datetime import datetime as _dt, timedelta
        fm = {
            "type": "feature_request_index",
            "title": "Feature and bug backlog",
            "source": f"github:{self.github.repo}",
            "last_updated": _dt.now().isoformat(timespec="seconds"),
            "open_count": len(open_issues),
            "recently_closed_count": len(closed),
        }
        lines = ["## Open", ""]
        for i in open_issues:
            kind = self._kind_from_labels(i)
            status = self._status_from_labels(i)
            priority = self._priority_from_labels(i)
            lines.append(f"- [{kind}][{status}][{priority}] #{i['number']} {i['title']}")
        lines += ["", "## Recently closed", ""]
        for i in closed:
            kind = self._kind_from_labels(i)
            status = self._status_from_labels(i)
            lines.append(f"- [{kind}][{status}] #{i['number']} {i['title']}")
        body = "\n".join(lines)
        content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}\n"
        target = BRAIN_DIR / "memories" / "features-index.md"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content)
        _os.rename(tmp, target)

    async def _list_features_from_github(self, update, kind_filter, status_filter, show_all,
                                          project_filter: Optional[str] = None) -> None:
        state = "all" if show_all or status_filter in ("done", "wont-do") else "open"
        labels = []
        if kind_filter:
            labels.append(f"kind:{kind_filter}")
        if status_filter in ("planned", "in-progress"):
            labels.append(f"status:{status_filter}")
        if project_filter:
            labels.append(f"project:{project_filter}")
        try:
            issues = await self.github.list_issues(state=state, labels=labels or None)
        except Exception as e:
            await self._send_reply(update, f"GitHub error: {e}")
            return
        if status_filter == "done":
            issues = [i for i in issues if i.get("state_reason") == "completed"]
        elif status_filter == "wont-do":
            issues = [i for i in issues if i.get("state_reason") == "not_planned"]
        elif not show_all and not status_filter:
            issues = [i for i in issues if i.get("state") == "open"]
        self._last_feature_set = [i["number"] for i in issues]
        if not issues:
            await self._send_reply(update, "No feature requests found.")
            return
        lines = [f"Feature requests ({len(issues)}):"]
        for idx, issue in enumerate(issues, 1):
            status = self._status_from_labels(issue)
            priority = self._priority_from_labels(issue)
            kind = self._kind_from_labels(issue)
            proj = self._project_from_labels(issue)
            created = issue.get("created_at", "")[:10]
            title = issue.get("title", "")[:60]
            kind_tag = f"[{kind[:4]}] " if not kind_filter else ""
            proj_tag = f" [{proj}]" if proj and not project_filter else ""
            lines.append(f"{idx}. {kind_tag}[{status}] [{priority}] {title}{proj_tag} ({created}) #{issue['number']}")
        await self._send_reply(update, "\n".join(lines))

    async def _list_features_text(self, kind: Optional[str] = None, show_all: bool = False) -> str:
        """Return formatted features/bugs list text. Called by cmd_features and tool dispatch.
        kind: 'feature', 'bug', or None for both. show_all includes done/wont-do."""
        if self.github.enabled:
            labels = [f"kind:{kind}"] if kind else None
            state = "all" if show_all else "open"
            try:
                issues = await self.github.list_issues(state=state, labels=labels)
            except Exception as e:
                return f"GitHub error: {e}"
            if not show_all:
                issues = [i for i in issues if i.get("state") == "open"]
            self._last_feature_set = [i["number"] for i in issues]
            if not issues:
                return "No feature requests found."
            lines = [f"Feature requests ({len(issues)}):"]
            for idx, issue in enumerate(issues, 1):
                status = self._status_from_labels(issue)
                priority = self._priority_from_labels(issue)
                issue_kind = self._kind_from_labels(issue)
                created = issue.get("created_at", "")[:10]
                title = issue.get("title", "")[:60]
                kind_tag = f"[{issue_kind[:4]}] " if not kind else ""
                lines.append(f"{idx}. {kind_tag}[{status}] [{priority}] {title} ({created}) #{issue['number']}")
            return "\n".join(lines)

        # Local fallback
        memories_dir = BRAIN_DIR / "memories"
        rows = await self._cache.query_by_prefix("feature-request-")
        rows_sorted = sorted(rows, key=lambda r: r.get("mtime") or 0, reverse=True)
        results = []
        for row in rows_sorted:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                fm = {}
            if fm.get("type") != "feature_request":
                continue
            item_status = fm.get("status", "new")
            item_kind = fm.get("kind", "feature")
            if kind and item_kind != kind:
                continue
            if not show_all and item_status in ("done", "wont-do"):
                continue
            f = memories_dir / row["filename"]
            results.append((f, fm))
        self._last_feature_set = [f for f, _ in results]
        if not results:
            return "No feature requests found."
        lines = [f"Feature requests ({len(results)}):"]
        for i, (f, fm) in enumerate(results, 1):
            item_status = fm.get("status", "new")
            priority = fm.get("priority", "medium")
            title = fm.get("title", f.stem)[:60]
            created = fm.get("created", "")[:10]
            item_kind = fm.get("kind", "feature")
            kind_tag = f"[{item_kind[:4]}] " if not kind else ""
            short_id = fm.get("short_id", "")
            id_suffix = f" [{short_id}]" if short_id else ""
            lines.append(f"{i}. {kind_tag}[{item_status}] [{priority}] {title} ({created}){id_suffix}")
        return "\n".join(lines)

    async def _get_memory_text(self, name: str) -> str:
        """Search memories by title or filename and return full file contents.
        Called by tool dispatch for get_memory tool."""
        name_lower = name.lower()
        rows = await self._cache.query_all()

        # First pass: filename match
        for row in rows:
            stem = row["filename"][:-3] if row["filename"].endswith(".md") else row["filename"]
            if name_lower in stem.lower():
                row_full = await self._cache.get(row["filename"])
                return (row_full or {}).get("body") or ""

        # Second pass: frontmatter source_title match
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                fm = {}
            source_title = (fm.get("source_title") or "").lower()
            if name_lower in source_title:
                row_full = await self._cache.get(row["filename"])
                return (row_full or {}).get("body") or ""

        return f"Memory not found: {name}"

    async def _list_comms_text(self, kind: Optional[str] = None, limit: int = 20, show_all: bool = False) -> str:
        """Return formatted comms list text (called by cmd_comms and tool dispatch).
        kind: 'email', 'slack', 'llm', or None for all.
        show_all: if True, show all email threads including marketing/automated."""
        limit = max(1, min(limit, 100))
        type_map = {"email": "email_thread", "slack": "slack_thread", "llm": "llm_chat"}
        wanted_types = {type_map[kind]} if kind else {"email_thread", "slack_thread", "llm_chat"}

        memories_dir = BRAIN_DIR / "memories"
        comms = []
        prefix_map = [
            ("email-thread-", "email_thread"),
            ("slack-thread-", "slack_thread"),
            ("llm-chat-", "llm_chat"),
        ]
        for prefix, mem_type in prefix_map:
            if mem_type not in wanted_types:
                continue
            rows = await self._cache.query_by_prefix(prefix)
            for row in rows:
                try:
                    fm = json.loads(row.get("frontmatter") or "{}")
                except Exception:
                    continue
                if fm.get("type") != mem_type:
                    continue

                # Filter email threads by classification unless show_all is True
                if mem_type == "email_thread" and not show_all:
                    classification = fm.get("classification", "human")
                    if classification in {"marketing", "automated"}:
                        continue

                comms.append((memories_dir / row["filename"], fm, mem_type))

        if not comms:
            msg = (f"No {kind} threads found." if kind
                   else "No communications found.")
            return msg

        def _sort_key(item):
            _, fm, mem_type = item
            if mem_type == "email_thread":
                return fm.get("last_message") or ""
            elif mem_type == "slack_thread":
                return fm.get("last_reply") or fm.get("last_message") or ""
            else:  # llm_chat
                return fm.get("created") or ""

        comms.sort(key=_sort_key, reverse=True)
        comms = comms[:limit]
        self._last_comms_set = [f for f, _, _ in comms]
        self._active_list = self._last_comms_set

        lines = [f"Communications ({len(comms)} shown):"]
        for i, (_, fm, mem_type) in enumerate(comms, 1):
            if mem_type == "email_thread":
                source_tag = "[email]"
                subject = (fm.get("source_title") or "(no subject)")[:45]
                sender = (fm.get("participants") or [""])[0]
                sender_str = f" — {str(sender)[:25]}" if sender else ""
                date = (fm.get("last_message") or "")[:10]

                # Add classification suffix for non-human emails when showing all
                classification_suffix = ""
                if show_all:
                    classification = fm.get("classification", "human")
                    if classification == "transactional":
                        classification_suffix = " [tx]"
                    elif classification == "marketing":
                        classification_suffix = " [mkt]"
                    elif classification == "automated":
                        classification_suffix = " [auto]"

                lines.append(f"{i}. {source_tag} {subject}{sender_str} ({date}){classification_suffix}")
            elif mem_type == "slack_thread":
                source_tag = "[slack]"
                channel = fm.get("channel") or fm.get("source_title") or "(no channel)"
                opener = (fm.get("participants") or [""])[0]
                opener_str = f" — {str(opener)[:25]}" if opener else ""
                date = (fm.get("last_reply") or fm.get("created") or "")[:10]
                lines.append(f"{i}. {source_tag} #{channel[:30]}{opener_str} ({date})")
            else:  # llm_chat
                platform = fm.get("platform", "llm")
                source_tag = f"[{platform}]"
                title = (fm.get("source_title") or "(no title)")[:45]
                date = (fm.get("created") or "")[:10]
                lines.append(f"{i}. {source_tag} {title} ({date})")
        lines.append("\nUse /comm <N> for details.")
        return "\n".join(lines)

    async def cmd_comms(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        args = list(context.args) if context.args else []
        type_filter = None
        limit = 10
        show_all = False

        # Parse: /comms [email|slack|llm] [forget N...] [all] [N]
        if args and args[0].lower() in ("email", "slack", "llm"):
            type_filter = args[0].lower()
            args = args[1:]
        elif args and not args[0].isdigit() and args[0].lower() not in ("all", "forget"):
            await update.message.reply_text(
                "Usage: /comms [email|slack|llm] [all] [N]\n"
                "Filter must be 'email', 'slack', or 'llm'. Add 'all' to show marketing/automated emails."
            )
            return

        # forget subcommand: /comms [email|slack] forget <N> [N ...]
        if args and args[0].lower() == "forget":
            forget_args = args[1:]
            if not forget_args or not all(str(a).isdigit() for a in forget_args):
                await update.message.reply_text(
                    "Usage: /comms [email|slack] forget <N> [N ...]\n"
                    "Run /comms [email|slack] first to see numbered items."
                )
                return
            # Rebuild the active list for this filter so indices are fresh
            await self._list_comms_text(type_filter, limit=50, show_all=True)
            await self._forget_indices(update, forget_args)
            return

        # Check for 'all' flag
        if args and args[0].lower() == "all":
            show_all = True
            args = args[1:]

        if args:
            try:
                limit = max(1, min(int(args[0]), 50))
            except ValueError:
                pass

        text = await self._list_comms_text(type_filter, limit, show_all)
        await update.message.reply_text(text)

    async def cmd_comm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /comm <N>")
            return

        path = self._resolve_comm_index(context.args[0])
        if path is None:
            await update.message.reply_text(self._format_group_help("Knowledge listings", "comm"))
            return

        row = await self._cache.get(path.name)
        try:
            fm = json.loads((row or {}).get("frontmatter") or "{}")
        except Exception:
            fm = {}
        mem_type = fm.get("type")
        title = fm.get("source_title") or "(no title)"
        participants = fm.get("participants") or []
        parts_str = ", ".join(str(p) for p in participants[:8]) if participants else "none listed"
        summary = fm.get("summary") or ""

        if mem_type == "email_thread":
            date = (fm.get("last_message") or "")[:10]
            lines = [
                f"[email] {title}",
                f"Participants: {parts_str}",
                f"Last message: {date}",
                "",
                summary,
            ]
        elif mem_type == "slack_thread":
            channel = fm.get("channel") or title
            date = (fm.get("last_reply") or fm.get("created") or "")[:10]
            lines = [
                f"[slack] #{channel}",
                f"Participants: {parts_str}",
                f"Last reply: {date}",
                "",
                summary,
            ]
        else:  # llm_chat
            platform = fm.get("platform", "llm")
            date = (fm.get("created") or "")[:10]
            topics = fm.get("topics", [])
            topics_str = ", ".join(topics[:5]) if topics else "none"
            lines = [
                f"[{platform}] {title}",
                f"Date: {date}",
                f"Topics: {topics_str}",
                "",
                summary,
            ]
        await update.message.reply_text("\n".join(lines))

    async def cmd_insights(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List recent synthesis insights."""
        if not self._check_auth(update):
            return

        rows = await self._cache.query_by_type("synthesis")
        synthesis = []
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                fm = {}
            if fm.get("type") != "synthesis":
                continue
            synthesis.append((fm, fm.get("created", "")))

        if not synthesis:
            await update.message.reply_text("No synthesis insights yet.")
            return

        synthesis.sort(key=lambda x: x[1], reverse=True)
        lines = ["Recent synthesis insights:\n"]
        for i, (fm, created) in enumerate(synthesis[:10], 1):
            title = fm.get("source_title", "(no title)")
            date = created[:10] if created else "unknown"
            lines.append(f"{i}. {title} — {date}")

        await update.message.reply_text("\n".join(lines))

    async def _llm_chat_memories(self) -> list[dict]:
        """Load all llm-chat memories, parse frontmatter, return sorted by most-recent first.

        Returns list of dicts with keys: path, platform, title, created, summary, topics, tags, header.
        """
        memories_dir = BRAIN_DIR / "memories"
        rows = await self._cache.query_by_prefix("llm-chat-")
        chats = []
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                fm = {}
            if fm.get("type") != "llm_chat":
                continue
            chats.append({
                "path": memories_dir / row["filename"],
                "platform": fm.get("platform", "unknown"),
                "title": fm.get("source_title", "(no title)"),
                "created": fm.get("created", ""),
                "summary": fm.get("summary", ""),
                "topics": fm.get("topics", []),
                "tags": fm.get("tags", []),
                "header": row.get("header500") or "",
            })

        # Sort by created timestamp, most recent first
        chats.sort(key=lambda x: x["created"], reverse=True)
        return chats

    def _format_aichat_list(self, memories: list[dict]) -> str:
        """Format llm_chat memories as a simple list (for search results)."""
        if not memories:
            return "No matching conversations found."

        lines = [f"LLM chat history ({len(memories)} shown):\n"]
        for i, m in enumerate(memories, 1):
            platform = m["platform"]
            title = m["title"][:50]
            date = m["created"][:10] if m["created"] else "unknown"
            lines.append(f"{i}. [{platform}] {title} ({date})")
        lines.append("\nUse /aichat <N> for details.")
        return "\n".join(lines)

    def _format_aichat_list_grouped(self, memories: list[dict]) -> str:
        """Format llm_chat memories grouped by platform."""
        if not memories:
            return "No imported LLM chats yet. Use /import_chats to get started."

        # Group by platform
        by_platform = {}
        for m in memories:
            platform = m["platform"]
            by_platform.setdefault(platform, []).append(m)

        lines = [f"LLM chat history ({len(memories)} shown):\n"]
        idx = 1
        for platform in sorted(by_platform.keys()):
            lines.append(f"\n{platform.title()}:")
            for m in by_platform[platform]:
                title = m["title"][:50]
                date = m["created"][:10] if m["created"] else "unknown"
                # Calculate days ago
                from datetime import datetime
                try:
                    created_dt = datetime.fromisoformat(m["created"])
                    now_dt = datetime.now(created_dt.tzinfo) if created_dt.tzinfo else datetime.now()
                    days_ago = (now_dt - created_dt).days
                    if days_ago == 0:
                        ago_str = "today"
                    elif days_ago == 1:
                        ago_str = "1 day ago"
                    else:
                        ago_str = f"{days_ago} days ago"
                except Exception:
                    ago_str = date

                lines.append(f"{idx}. {title} ({ago_str})")
                idx += 1

        lines.append("\nUse /aichat <N> for details or /aichat search <query> to filter.")
        return "\n".join(lines)

    def _format_aichat_detail(self, memory: dict) -> str:
        """Format a single llm_chat memory with full detail."""
        title = memory["title"]
        platform = memory["platform"]
        created = memory["created"][:10] if memory["created"] else "unknown"
        summary = memory["summary"] or "(no summary)"
        topics = memory.get("topics", [])
        tags = memory.get("tags", [])

        lines = [
            f"[{platform}] {title}",
            f"Date: {created}",
            "",
            "Summary:",
            summary,
        ]

        if topics:
            lines.append("")
            lines.append(f"Key topics: {', '.join(topics[:10])}")

        if tags:
            lines.append(f"Tags: {', '.join(tags[:10])}")

        return "\n".join(lines)

    async def cmd_aichat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Browse imported Claude/ChatGPT conversation history."""
        if not self._check_auth(update):
            return

        args = list(context.args) if context.args else []
        memories = await self._llm_chat_memories()

        # Search mode: /aichat search <query>
        if args and args[0].lower() == "search":
            query = " ".join(args[1:]).lower()
            if not query:
                await update.message.reply_text("Usage: /aichat search <query>")
                return
            filtered = [m for m in memories if query in m["header"].lower()]
            await update.message.reply_text(self._format_aichat_list(filtered[:20]))
            return

        # Detail mode: /aichat <N>
        if args and args[0].isdigit():
            idx = int(args[0]) - 1
            if not (0 <= idx < len(memories)):
                await update.message.reply_text("No such entry.")
                return
            await update.message.reply_text(self._format_aichat_detail(memories[idx]))
            return

        # List mode (default): /aichat
        await update.message.reply_text(self._format_aichat_list_grouped(memories[:20]))

    # ── Notification commands ─────────────────────────────────────────────────

    async def cmd_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        if self.notification_manager is None:
            await update.message.reply_text("Notification manager not available.")
            return

        # Assemble and send briefing without updating last_briefing_date
        briefing_text = await self.notification_manager._assemble_briefing()
        await update.message.reply_text(briefing_text)

    async def cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        from notification_manager import _load_state, _save_state
        state = _load_state()
        state["muted"] = True
        _save_state(state)
        await update.message.reply_text("Proactive notifications muted.")

    async def cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        from notification_manager import _load_state, _save_state
        state = _load_state()
        state["muted"] = False
        _save_state(state)
        await update.message.reply_text("Proactive notifications resumed.")

    # ── Feature Tracker commands ──────────────────────────────────────────────

    async def cmd_feature(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /feature <description>")
            return

        description = " ".join(context.args)
        # Extract #hashtags as tags and for:<project> specifier
        tags = [t[1:].lower() for t in re.findall(r'#\w+', description)]
        project = next((m[4:] for m in re.findall(r'\bfor:[\w-]+', description, re.IGNORECASE)), "")
        clean_desc = re.sub(r'#\w+', '', description).strip()
        clean_desc = re.sub(r'\bfor:[\w-]+\s*', '', clean_desc, flags=re.IGNORECASE).strip()
        title = " ".join(clean_desc.split()[:8])  # first 8 words as title

        import hashlib, os
        from datetime import datetime
        memories_dir = BRAIN_DIR / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:40].strip('-')
        id_hash = hashlib.sha1(f"{description}{datetime.now().isoformat()}".encode()).hexdigest()[:6]
        filename = f"feature-request-{slug}-{id_hash}.md"

        if self.github.enabled:
            await self._gh_ensure_labels()
            body = (
                f"## Request\n\n{clean_desc}\n\n"
                f"## Context\n\nCaptured via /feature command at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\n"
                f"## Notes\n\n"
            )
            labels = ["kind:feature", "priority:medium"] + tags
            if project:
                labels.append(f"project:{project.lower()}")
            try:
                issue = await self.github.create_issue(title, body, labels)
            except Exception as e:
                await self._send_reply(update, f"GitHub error: {e}")
                return
            fm = {
                "title": clean_desc[:100],
                "type": "feature_request",
                "kind": "feature",
                "status": "new",
                "priority": "medium",
                "created": datetime.now().isoformat(),
                "tags": tags,
                "short_id": id_hash,
                "github_issue_number": issue["number"],
            }
            if project:
                fm["project"] = project.lower()
            content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}"
            target = memories_dir / filename
            tmp = target.with_suffix(".tmp")
            tmp.write_text(content)
            os.rename(tmp, target)
            await self._rewrite_features_index_snapshot()
            proj_note = f" [{project}]" if project else ""
            await self._send_reply(update,
                f"Feature captured{proj_note}: '{clean_desc[:60]}' (#{issue['number']})\n{issue['html_url']}")
            return

        # --- local fallback ---
        fm = {
            "title": clean_desc[:100],
            "type": "feature_request",
            "kind": "feature",
            "status": "new",
            "priority": "medium",
            "created": datetime.now().isoformat(),
            "tags": tags,
            "short_id": id_hash,
        }
        if project:
            fm["project"] = project.lower()
        body = f"## Request\n\n{clean_desc}\n\n## Context\n\nCaptured via /feature command at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\n## Notes\n\n"
        content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}"

        target = memories_dir / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content)
        os.rename(tmp, target)

        proj_note = f" [{project}]" if project else ""
        await self._send_reply(update, f"Feature captured{proj_note}: '{clean_desc[:60]}' (ID: {id_hash})\nUse /features to view all.")

    async def cmd_bug(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /bug <description>")
            return

        description = " ".join(context.args)
        tags = [t[1:].lower() for t in re.findall(r'#\w+', description)]
        project = next((m[4:] for m in re.findall(r'\bfor:[\w-]+', description, re.IGNORECASE)), "")
        clean_desc = re.sub(r'#\w+', '', description).strip()
        clean_desc = re.sub(r'\bfor:[\w-]+\s*', '', clean_desc, flags=re.IGNORECASE).strip()
        title = " ".join(clean_desc.split()[:8])

        import hashlib, os
        from datetime import datetime
        memories_dir = BRAIN_DIR / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:40].strip('-')
        id_hash = hashlib.sha1(f"{description}{datetime.now().isoformat()}".encode()).hexdigest()[:6]
        filename = f"feature-request-{slug}-{id_hash}.md"
        body = (
            f"## Bug\n\n{clean_desc}\n\n"
            f"## Expected\n\n\n\n"
            f"## Actual\n\n\n\n"
            f"## Steps to reproduce\n\n\n\n"
            f"## Notes\n\nCaptured via /bug at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n"
        )

        if self.github.enabled:
            await self._gh_ensure_labels()
            labels = ["kind:bug", "priority:medium"] + tags
            if project:
                labels.append(f"project:{project.lower()}")
            try:
                issue = await self.github.create_issue(title, body, labels)
            except Exception as e:
                await self._send_reply(update, f"GitHub error: {e}")
                return
            fm = {
                "title":    clean_desc[:100],
                "type":     "feature_request",
                "kind":     "bug",
                "status":   "new",
                "priority": "medium",
                "created":  datetime.now().isoformat(),
                "tags":     tags,
                "short_id": id_hash,
                "github_issue_number": issue["number"],
            }
            if project:
                fm["project"] = project.lower()
            content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}"
            target = memories_dir / filename
            tmp = target.with_suffix(".tmp")
            tmp.write_text(content)
            os.rename(tmp, target)
            await self._rewrite_features_index_snapshot()
            proj_note = f" [{project}]" if project else ""
            await self._send_reply(update,
                f"Bug captured{proj_note}: '{clean_desc[:60]}' (#{issue['number']})\n{issue['html_url']}")
            return

        # --- local fallback ---
        fm = {
            "title":    clean_desc[:100],
            "type":     "feature_request",
            "kind":     "bug",
            "status":   "new",
            "priority": "medium",
            "created":  datetime.now().isoformat(),
            "tags":     tags,
            "short_id": id_hash,
        }
        if project:
            fm["project"] = project.lower()
        content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}"
        target = memories_dir / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content)
        os.rename(tmp, target)
        proj_note = f" [{project}]" if project else ""
        await self._send_reply(update, f"Bug captured{proj_note}: '{clean_desc[:60]}' (ID: {id_hash})\nUse /features bug to view all.")

    async def cmd_bugs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Alias for /features bug."""
        context.args = ["bug"]
        await self.cmd_features(update, context)

    async def cmd_features(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        args = [a.lower() for a in (context.args or [])]
        kind_filter: Optional[str] = None
        status_filter: Optional[str] = None
        project_filter: Optional[str] = None
        show_all = False
        for arg in args:
            if arg.startswith("project:"):
                project_filter = arg[len("project:"):]
            elif arg in ("bug", "bugs"):
                kind_filter = "bug"
            elif arg in ("feature", "features"):
                kind_filter = "feature"
            elif arg == "all":
                show_all = True
            elif arg:
                status_filter = arg

        if self.github.enabled:
            await self._list_features_from_github(update, kind_filter, status_filter, show_all,
                                                   project_filter=project_filter)
            return

        # --- local fallback ---
        memories_dir = BRAIN_DIR / "memories"
        rows = await self._cache.query_by_prefix("feature-request-")
        rows_sorted = sorted(rows, key=lambda r: r.get("mtime") or 0, reverse=True)

        results = []
        for row in rows_sorted:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                fm = {}
            if fm.get("type") != "feature_request":
                continue
            status = fm.get("status", "new")
            item_kind = fm.get("kind", "feature")
            item_project = fm.get("project", "")
            # Kind filter
            if kind_filter and item_kind != kind_filter:
                continue
            # Project filter
            if project_filter and item_project.lower() != project_filter.lower():
                continue
            # Status filter
            if not show_all:
                if status_filter:
                    if status != status_filter:
                        continue
                else:
                    # Default: show new and planned only
                    if status in ("done", "wont-do"):
                        continue
            f = memories_dir / row["filename"]
            results.append((f, fm))

        self._last_feature_set = [f for f, _ in results]

        if not results:
            await self._send_reply(update, "No feature requests found.")
            return

        lines = [f"Feature requests ({len(results)}):"]
        for i, (f, fm) in enumerate(results, 1):
            status = fm.get("status", "new")
            priority = fm.get("priority", "medium")
            title = fm.get("title", f.stem)[:60]
            created = fm.get("created", "")[:10]
            item_kind = fm.get("kind", "feature")
            item_project = fm.get("project", "")
            kind_tag = f"[{item_kind[:4]}] " if not kind_filter else ""
            proj_tag = f" [{item_project}]" if item_project and not project_filter else ""
            short_id = fm.get("short_id", "")
            id_suffix = f" [{short_id}]" if short_id else ""
            lines.append(f"{i}. {kind_tag}[{status}] [{priority}] {title}{proj_tag} ({created}){id_suffix}")

        await self._send_reply(update, "\n".join(lines))

    async def cmd_feature_detail(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        # Verb-dispatch: first arg is not a number and not #<issue> → look it up in Feature Requests group
        if context.args:
            first = context.args[0]
            if not first.startswith("#"):
                try:
                    int(first)
                except ValueError:
                    # Not a number, not a #<issue> — try verb dispatch
                    matched = self._match_verb_in_group("Feature Requests", "feature", first.lstrip("/"))
                    if matched:
                        handler = getattr(self, f"cmd_{matched}", None)
                        if handler is not None:
                            context.args = context.args[1:]
                            return await handler(update, context)
                    # Fall through to _resolve_feature_index which will return None

        target = await self._resolve_feature_index(context.args, update)
        if target is None:
            await self._send_reply(update, self._format_group_help("Feature Requests") + "\nOr use #<issue>.")
            return

        if isinstance(target, int):
            # GitHub path
            try:
                issue = await self.github.get_issue(target)
                comments = await self.github.get_comments(target)
            except Exception as e:
                await self._send_reply(update, f"GitHub error: {e}")
                return
            status = self._status_from_labels(issue)
            priority = self._priority_from_labels(issue)
            kind = self._kind_from_labels(issue)
            tags = self._tags_from_labels(issue)
            created = issue.get("created_at", "")[:10]
            body = issue.get("body", "")[:1500]
            lines = [
                f"**{issue.get('title', '?')}** — #{target} · {issue.get('html_url', '')}",
                f"Status: {status} | Priority: {priority} | Kind: {kind}",
                f"Created: {created}",
                f"Tags: {', '.join(tags) if tags else 'none'}",
                "",
                body,
            ]
            if comments:
                lines.append("")
                lines.append(f"## Notes ({len(comments)} comments)")
                for c in comments[-5:]:  # last 5
                    created_at = c.get("created_at", "")[:10]
                    comment_body = c.get("body", "")[:150]
                    lines.append(f"- {created_at}: {comment_body}")
            await self._send_reply(update, "\n".join(lines))
            return

        # Local path
        fm = self._parse_frontmatter(target)
        text = _safe_read_text(target)
        if text is None:
            await update.message.reply_text("Feature file temporarily unavailable — try again in a moment.")
            return
        # Strip frontmatter for body
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else text

        lines = [
            f"**{fm.get('title', target.stem)}**",
            f"Status: {fm.get('status', '?')} | Priority: {fm.get('priority', '?')}",
            f"Created: {fm.get('created', '?')[:16]}",
            f"Tags: {', '.join(fm.get('tags', []))}",
            "",
            body[:1500],
        ]
        await self._send_reply(update, "\n".join(lines))

    async def cmd_feature_plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        target = await self._resolve_feature_index(context.args, update)
        if target is None:
            await self._send_reply(update, self._format_group_help("Feature Requests") + "\nOr use #<issue>.")
            return
        if isinstance(target, int):
            await self._gh_set_status(target, "planned")
            await self._rewrite_features_index_snapshot()
            title = await self._gh_title(target)
            issue_url = f"https://github.com/{self.github.repo}/issues/{target}"
            await self._send_reply(update, f"Feature '{title[:50]}' marked as planned. {issue_url}")
        else:
            fm = self._parse_frontmatter(target)
            self._rewrite_feature_frontmatter(target, {"status": "planned"})
            await self._send_reply(update, f"Feature '{fm.get('title','?')[:50]}' marked as planned.")

    async def cmd_feature_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        target = await self._resolve_feature_index(context.args, update)
        if target is None:
            await self._send_reply(update, self._format_group_help("Feature Requests") + "\nOr use #<issue>.")
            return
        if isinstance(target, int):
            await self._gh_set_status(target, "in-progress")
            await self._rewrite_features_index_snapshot()
            title = await self._gh_title(target)
            issue_url = f"https://github.com/{self.github.repo}/issues/{target}"
            await self._send_reply(update, f"Feature '{title[:50]}' is now in progress. {issue_url}")
        else:
            fm = self._parse_frontmatter(target)
            self._rewrite_feature_frontmatter(target, {"status": "in-progress"})
            await self._send_reply(update, f"Feature '{fm.get('title','?')[:50]}' is now in progress.")

    async def cmd_feature_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        target = await self._resolve_feature_index(context.args[:1], update)
        if target is None:
            await self._send_reply(update, self._format_group_help("Feature Requests") + "\nOr use #<issue>.")
            return
        note = " ".join(context.args[1:]) if len(context.args) > 1 else None
        if isinstance(target, int):
            await self._gh_set_status(target, "done")
            if note:
                await self.github.add_comment(target, note)
            await self._rewrite_features_index_snapshot()
            title = await self._gh_title(target)
            issue_url = f"https://github.com/{self.github.repo}/issues/{target}"
            await self._send_reply(update, f"Feature '{title[:50]}' marked as done. {issue_url}")
        else:
            fm = self._parse_frontmatter(target)
            self._rewrite_feature_frontmatter(target, {"status": "done"})
            if note:
                self._append_feature_note(target, note)
            await self._send_reply(update, f"Feature '{fm.get('title','?')[:50]}' marked as done.")

    async def cmd_feature_wont_do(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        target = await self._resolve_feature_index(context.args[:1], update)
        if target is None:
            await self._send_reply(update, self._format_group_help("Feature Requests") + "\nOr use #<issue>.")
            return
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
        if isinstance(target, int):
            await self._gh_set_status(target, "wont-do")
            if reason:
                await self.github.add_comment(target, f"Won't do: {reason}")
            await self._rewrite_features_index_snapshot()
            title = await self._gh_title(target)
            issue_url = f"https://github.com/{self.github.repo}/issues/{target}"
            await self._send_reply(update, f"Feature '{title[:50]}' marked as won't do. {issue_url}")
        else:
            fm = self._parse_frontmatter(target)
            self._rewrite_feature_frontmatter(target, {"status": "wont-do"})
            if reason:
                self._append_feature_note(target, f"Won't do: {reason}")
            await self._send_reply(update, f"Feature '{fm.get('title','?')[:50]}' marked as won't do.")

    async def cmd_feature_priority(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await self._send_reply(update, "Usage: /feature-priority <N> <low|medium|high|critical>")
            return
        target = await self._resolve_feature_index(context.args[:1], update)
        if target is None:
            await self._send_reply(update, self._format_group_help("Feature Requests") + "\nOr use #<issue>.")
            return
        priority = context.args[1].lower()
        if priority not in ("low", "medium", "high", "critical"):
            await self._send_reply(update, "Priority must be: low, medium, high, or critical")
            return
        if isinstance(target, int):
            await self._gh_set_priority(target, priority)
            await self._rewrite_features_index_snapshot()
            title = await self._gh_title(target)
            issue_url = f"https://github.com/{self.github.repo}/issues/{target}"
            await self._send_reply(update, f"Priority updated: '{title[:50]}' is now {priority}. {issue_url}")
        else:
            fm = self._parse_frontmatter(target)
            self._rewrite_feature_frontmatter(target, {"priority": priority})
            await self._send_reply(update, f"Priority updated: '{fm.get('title','?')[:50]}' is now {priority}.")

    async def cmd_feature_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await self._send_reply(update, "Usage: /feature-note <N> <text>")
            return
        target = await self._resolve_feature_index(context.args[:1], update)
        if target is None:
            await self._send_reply(update, self._format_group_help("Feature Requests") + "\nOr use #<issue>.")
            return
        note = " ".join(context.args[1:])
        if isinstance(target, int):
            await self.github.add_comment(target, note)
            title = await self._gh_title(target)
            await self._send_reply(update, f"Note added to '{title[:50]}'.")
        else:
            fm = self._parse_frontmatter(target)
            self._append_feature_note(target, note)
            await self._send_reply(update, f"Note added to '{fm.get('title','?')[:50]}'.")

    async def cmd_feature_import(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """One-time migration: import local feature-request-*.md files into GitHub Issues."""
        if not self._check_auth(update):
            return
        if not self.github.enabled:
            await self._send_reply(update, "GitHub not configured. Set GITHUB_PAT and github.repo first.")
            return
        confirm = bool(context.args and context.args[0].lower() == "confirm")
        memories_dir = BRAIN_DIR / "memories"
        rows = await self._cache.query_by_prefix("feature-request-")
        to_import = []
        for row in rows:
            try:
                fm = json.loads(row.get("frontmatter") or "{}")
            except Exception:
                fm = {}
            if fm.get("type") != "feature_request":
                continue
            if fm.get("github_issue_number"):
                continue  # already imported
            f = memories_dir / row["filename"]
            to_import.append((f, fm))
        if not to_import:
            await self._send_reply(update, "No local feature files to import.")
            return
        if not confirm:
            titles = [f"• {fm.get('title', '?')[:60]}" for _, fm in to_import[:10]]
            extra = f"\n...and {len(to_import)-10} more" if len(to_import) > 10 else ""
            await self._send_reply(update,
                f"{len(to_import)} file(s) to import:\n" + "\n".join(titles) + extra +
                "\n\nRun /feature_import confirm to proceed.")
            return
        await self._gh_ensure_labels()
        imported = 0
        for f, fm in to_import:
            title = fm.get("title", f.stem)[:100]
            text = _safe_read_text(f)
            if text is None:
                continue
            parts = text.split("---", 2)
            body = parts[2].strip() if len(parts) >= 3 else text
            kind = fm.get("kind", "feature")
            tags = fm.get("tags") or []
            status = fm.get("status", "new")
            priority = fm.get("priority", "medium")
            labels = [f"kind:{kind}", f"priority:{priority}"] + tags
            if status == "planned":
                labels.append("status:planned")
            elif status == "in-progress":
                labels.append("status:in-progress")
            try:
                issue = await self.github.create_issue(title, body, labels)
            except Exception as e:
                await self._send_reply(update, f"Error importing '{title[:40]}': {e}")
                continue
            # Keep the file in memories/ — just stamp it with the GH issue number so
            # future imports skip it and memory context can still reference it.
            self._rewrite_feature_frontmatter(f, {"github_issue_number": issue["number"]})
            if status == "done":
                await self.github.update_issue(issue["number"], state="closed", state_reason="completed")
            elif status == "wont-do":
                await self.github.update_issue(issue["number"], state="closed", state_reason="not_planned")
            imported += 1
        await self._rewrite_features_index_snapshot()
        await self._send_reply(update, f"Imported {imported} issue(s) to {self.github.repo}.")

    # ── Skill Management commands ─────────────────────────────────────────────

    async def cmd_skill_drafts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if self.skill_creator is None:
            await self._send_reply(update, "Skill creator not available.")
            return
        drafts = self.skill_creator.list_pending_drafts()
        if not drafts:
            await self._send_reply(update, "No pending skill drafts.")
            return
        self._last_skill_draft_set = drafts
        lines = ["Pending skill drafts:"]
        for i, d in enumerate(drafts, 1):
            types = ", ".join(d.get("content_types", []))
            created = d.get("created", "")[:10]
            lines.append(f"{i}. {d['skill_name']} — type: {types} ({created})")
        await self._send_reply(update, "\n".join(lines))

    async def cmd_skill_draft(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /skill-draft <N>")
            return

        # Verb-dispatch: first arg is not a number → look it up in the Skill Management group
        first = context.args[0]
        try:
            int(first)
        except ValueError:
            matched = self._match_verb_in_group("Skill Management", "skill_draft", first.lstrip("/"))
            if matched:
                handler = getattr(self, f"cmd_{matched}", None)
                if handler is not None:
                    context.args = context.args[1:]
                    return await handler(update, context)
            # Fall through to error message for unknown verbs

        try:
            n = int(context.args[0])
            draft = self._last_skill_draft_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, self._format_group_help("Skill Management"))
            return
        path = Path(draft.get("draft_path", ""))
        if not path.exists():
            await self._send_reply(update, f"Draft file not found: {path}")
            return
        content = path.read_text()
        await self._send_reply(update, f"```\n{content[:3800]}\n```")

    async def cmd_approve_skill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /approve-skill <N>")
            return
        try:
            n = int(context.args[0])
            draft = self._last_skill_draft_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, self._format_group_help("Skill Management"))
            return
        skill_name = draft["skill_name"]
        if self.skill_creator and self.skill_creator.approve_draft(skill_name):
            await self._send_reply(update, f"Skill '{skill_name}' approved and entering probation.")
        else:
            await self._send_reply(update, f"Could not approve '{skill_name}'. Draft may have been removed.")

    async def cmd_reject_skill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /reject-skill <N>")
            return
        try:
            n = int(context.args[0])
            draft = self._last_skill_draft_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, self._format_group_help("Skill Management"))
            return
        skill_name = draft["skill_name"]
        ct = ", ".join(draft.get("content_types", ["unknown"]))
        if self.skill_creator and self.skill_creator.reject_draft(skill_name):
            await self._send_reply(update, f"Skill '{skill_name}' rejected. Content type '{ct}' on 24h cooldown.")
        else:
            await self._send_reply(update, f"Could not reject '{skill_name}'.")

    async def cmd_skill_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if self.skill_creator is None:
            await self._send_reply(update, "Skill creator not available.")
            return
        arg = context.args[0].lower() if context.args else "status"
        if arg == "on":
            self.skill_creator.set_approval_override(True)
            await self._send_reply(update, "Skill approval mode ON. New skill drafts require /approve-skill before running.")
        elif arg == "off":
            self.skill_creator.set_approval_override(False)
            await self._send_reply(update, "Skill approval mode OFF. New skills enter probation automatically.")
        else:
            mode = self.skill_creator.get_effective_approval_mode()
            override = self.skill_creator._load_registry().get("require_approval_runtime_override")
            source = "runtime override" if override is not None else "config"
            await self._send_reply(update, f"Skill approval: {'on' if mode else 'off'} (from {source})")

    async def cmd_skill_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        skills_dir = BRAIN_DIR / "skills"
        if not skills_dir.exists():
            await self._send_reply(update, "Skills directory not found.")
            return
        lines = ["Skill health:"]
        for path in sorted(skills_dir.glob("*.md")):
            fm = self._parse_frontmatter(path)
            name = path.stem
            score = fm.get("utility_score", fm.get("success_rate", "?"))
            trend = fm.get("score_trend", "")
            trend_sym = {"improving": "▲", "declining": "▼", "stable": "◆", "insufficient-data": "—"}.get(trend, "—")
            status = fm.get("status", "active")
            status_tag = f" [{status}]" if status != "active" else ""
            lines.append(f"• {name}{status_tag}: {score} {trend_sym}")
        await self._send_reply(update, "\n".join(lines))

    # ── Report commands ───────────────────────────────────────────────────────

    async def cmd_reports(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if self.report_scheduler is None:
            await self._send_reply(update, "Report scheduler not available.")
            return
        reports = self.report_scheduler.get_all_reports()
        if not reports:
            await self._send_reply(update, "No reports configured. Use /report-add to create one.")
            return
        self._last_report_set = reports
        lines = [f"Reports ({len(reports)}):"]
        for i, r in enumerate(reports, 1):
            name = r["name"]
            rtype = r.get("type", "digest")
            schedule = r.get("schedule", "?")
            last = r.get("last_sent", "never")
            paused = " [paused]" if r.get("paused") else ""
            is_config = " [config]" if r.get("is_config_report") else ""
            lines.append(f"{i}. [{rtype}] {name} — {schedule} (last: {last}){paused}{is_config}")
        await self._send_reply(update, "\n".join(lines))

    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /report <N>")
            return

        # Verb-dispatch: first arg is not a number → look it up in the Reports group
        first = context.args[0]
        try:
            int(first)
        except ValueError:
            matched = self._match_verb_in_group("Reports", "report", first.lstrip("/"))
            if matched:
                handler = getattr(self, f"cmd_{matched}", None)
                if handler is not None:
                    context.args = context.args[1:]
                    return await handler(update, context)
            # Fall through to error message for unknown verbs

        try:
            n = int(context.args[0])
            r = self._last_report_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, self._format_group_help("Reports"))
            return
        lines = [
            f"**{r['name']}**",
            f"Type: {r.get('type','?')} | Schedule: {r.get('schedule','?')}",
            f"Sources: {', '.join(r.get('sources',[]))}",
            f"Window: {r.get('window_days','?')} days",
            f"Paused: {r.get('paused', False)}",
            f"Last sent: {r.get('last_sent','never')}",
        ]
        if r.get("prompt"):
            lines.append(f"Prompt: {r['prompt'][:200]}")
        await self._send_reply(update, "\n".join(lines))

    async def cmd_report_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not self.report_scheduler:
            await self._send_reply(update, "Report scheduler not available.")
            return
        if len(context.args) < 3:
            await self._send_reply(update,
                "Usage: /report-add \"<schedule>\" <digest|analysis> <source1> [source2...]\n"
                "Example: /report-add \"mon 07:00\" digest commitments meetings")
            return
        try:
            # First arg is schedule (may be quoted), second is type, rest are sources
            args = context.args
            schedule = args[0].strip('"\'')
            rtype = args[1].lower()
            sources = [s.lower() for s in args[2:]]
            defn = {
                "schedule": schedule,
                "type": rtype,
                "sources": sources,
                "window_days": 7,
                "paused": False,
            }
            report_id = self.report_scheduler.add_runtime_report(defn)
            await self._send_reply(update, f"Report created (ID: {report_id}). Run /reports to view.")
        except ValueError as e:
            await self._send_reply(update, f"Error: {e}")

    async def cmd_report_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /report-remove <N>")
            return
        try:
            n = int(context.args[0])
            r = self._last_report_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, self._format_group_help("Reports"))
            return
        if r.get("is_config_report"):
            await self._send_reply(update, f"Config reports cannot be removed. Use /report-pause {n} to disable.")
            return
        report_id = r.get("id", r.get("name"))
        if self.report_scheduler.remove_runtime_report(report_id):
            await self._send_reply(update, f"Report '{r['name']}' removed.")
        else:
            await self._send_reply(update, f"Could not remove report '{r['name']}'.")

    async def cmd_report_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /report-pause <N>")
            return
        try:
            n = int(context.args[0])
            r = self._last_report_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, self._format_group_help("Reports"))
            return
        is_runtime = not r.get("is_config_report", False)
        if self.report_scheduler.set_paused(r["name"], True, is_runtime):
            await self._send_reply(update, f"Report '{r['name']}' paused.")
        else:
            await self._send_reply(update, f"Could not pause report '{r['name']}'.")

    async def cmd_report_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /report-resume <N>")
            return
        try:
            n = int(context.args[0])
            r = self._last_report_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, self._format_group_help("Reports"))
            return
        is_runtime = not r.get("is_config_report", False)
        if self.report_scheduler.set_paused(r["name"], False, is_runtime):
            await self._send_reply(update, f"Report '{r['name']}' resumed.")
        else:
            await self._send_reply(update, f"Could not resume report '{r['name']}'.")

    async def cmd_report_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /report-run <N>")
            return
        if self.report_scheduler is None:
            await self._send_reply(update, "Report scheduler not available.")
            return
        try:
            n = int(context.args[0])
            r = self._last_report_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, self._format_group_help("Reports"))
            return
        chat_id = update.effective_chat.id
        await self._send_reply(update, f"Running report '{r['name']}'...")
        try:
            await self.report_scheduler.trigger_report(r["name"], r, chat_id)
        except Exception as e:
            await self._send_reply(update, f"Report failed: {e}")

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear the conversation history for this chat session."""
        if not self._check_auth(update):
            return
        chat_id = update.effective_chat.id
        cleared = len(self._chat_history.pop(chat_id, []))
        # Also clear any pending queued replies
        state = self._load_pending()
        pending_cleared = len(state.pop(str(chat_id), {}).get("pending", []))
        if pending_cleared:
            self._save_pending(state)
        msg = (
            f"Conversation history cleared ({cleared // 2} turn(s) removed"
            + (f", {pending_cleared} pending reply/replies discarded" if pending_cleared else "")
            + ")."
            if cleared or pending_cleared else
            "No conversation history to clear."
        )
        await update.message.reply_text(msg)

    async def cmd_deliver(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send all pending replies queued while the network was down."""
        if not self._check_auth(update):
            return
        chat_id = update.effective_chat.id
        state = self._load_pending()
        entry = state.get(str(chat_id))
        if not entry or not entry.get("pending"):
            await update.message.reply_text("No pending replies to deliver.")
            return

        pending = entry["pending"]
        await update.message.reply_text(f"Delivering {len(pending)} queued response(s)…")

        delivered_count = 0
        remaining = []
        turns = self._chat_history.setdefault(chat_id, [])
        for item in pending:
            ok = await self._send_reply(update, item["response"])
            if ok:
                turns.append({"role": "user", "content": item["query"]})
                turns.append({"role": "assistant", "content": item["response"][:4096]})
                delivered_count += 1
            else:
                remaining.append(item)

        max_msgs = self.HISTORY_WINDOW_TURNS * 2
        if len(turns) > max_msgs:
            self._chat_history[chat_id] = turns[-max_msgs:]
        if delivered_count:
            self._save_history()

        if remaining:
            entry["pending"] = remaining
            entry["summary_sent"] = False
            state[str(chat_id)] = entry
        else:
            state.pop(str(chat_id), None)
        self._save_pending(state)

        try:
            await update.message.reply_text(
                f"✅ Delivered {delivered_count}."
                + (f" {len(remaining)} still queued — network is flaky again." if remaining else "")
            )
        except Exception:
            pass

    async def cmd_discard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Drop all pending replies queued while the network was down."""
        if not self._check_auth(update):
            return
        chat_id = update.effective_chat.id
        state = self._load_pending()
        entry = state.pop(str(chat_id), None)
        count = len(entry.get("pending", [])) if entry else 0
        self._save_pending(state)
        if count:
            await update.message.reply_text(f"Discarded {count} pending reply/replies.")
        else:
            await update.message.reply_text("No pending replies to discard.")

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View or change display settings: date_format, timezone.

        Usage:
          /settings                           — show current settings
          /settings date_format MM/DD/YYYY, HH:MM
          /settings date_format DD/MM/YYYY, HH:MM
          /settings date_format YYYY-MM-DD HH:MM
          /settings timezone America/Los_Angeles
          /settings timezone UTC
        """
        if not self._check_auth(update):
            return

        config_path = BRAIN_DIR / "config.yaml"
        try:
            config = yaml.safe_load(config_path.read_text())
        except Exception as e:
            await update.message.reply_text(f"Could not read config: {_safe_error(e)}")
            return

        display = config.setdefault("display", {})

        VALID_DATE_FORMATS = {"MM/DD/YYYY, HH:MM", "DD/MM/YYYY, HH:MM", "YYYY-MM-DD HH:MM"}

        if not context.args:
            # Show current settings
            fmt = display.get("date_format", "MM/DD/YYYY, HH:MM")
            tz = display.get("timezone", "(system default)")
            lines = [
                "Display settings:",
                f"  date_format: {fmt}",
                f"  timezone: {tz}",
                "",
                "Change with:",
                "  /settings date_format MM/DD/YYYY, HH:MM",
                "  /settings date_format DD/MM/YYYY, HH:MM",
                "  /settings date_format YYYY-MM-DD HH:MM",
                "  /settings timezone America/Los_Angeles",
                "  /settings timezone UTC",
            ]
            await update.message.reply_text("\n".join(lines))
            return

        key = context.args[0].lower()
        # Rejoin remaining args to support values with spaces
        value = " ".join(context.args[1:]).strip()

        if not value:
            await update.message.reply_text(f"Usage: /settings {key} <value>")
            return

        if key == "date_format":
            if value not in VALID_DATE_FORMATS:
                await update.message.reply_text(
                    f"Unknown format '{value}'. Valid options:\n" +
                    "\n".join(f"  {f}" for f in sorted(VALID_DATE_FORMATS))
                )
                return
            display["date_format"] = value
        elif key == "timezone":
            try:
                import zoneinfo
                zoneinfo.ZoneInfo(value)  # validate
            except Exception:
                await update.message.reply_text(
                    f"Unknown timezone '{value}'. Use IANA timezone names like "
                    "America/Los_Angeles, UTC, Europe/London."
                )
                return
            display["timezone"] = value
        else:
            await update.message.reply_text(
                f"Unknown setting '{key}'. Available: date_format, timezone"
            )
            return

        config["display"] = display
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(yaml.dump(config, default_flow_style=False))
        os.rename(tmp, config_path)
        await update.message.reply_text(f"Updated {key} to: {value}")

    async def _send_reply(self, update: Update, text: str) -> bool:
        """Chunk response into ≤4096-char messages. Retries on transient network errors."""
        if not text:
            text = "No response generated."
        chunks = [text[i:i + TG_MAX_CHARS] for i in range(0, len(text), TG_MAX_CHARS)] or [text]
        for chunk in chunks:
            delivered = False
            for attempt, delay in enumerate((1, 2, 4)):
                try:
                    await update.message.reply_text(chunk)
                    delivered = True
                    break
                except (TimedOut, NetworkError) as e:
                    if attempt == 2:
                        log.error("Reply failed after 3 attempts: %s", e)
                        return False
                    log.warning("Reply attempt %d failed (%s), retrying in %ds", attempt + 1, e, delay)
                    await asyncio.sleep(delay)
            if not delivered:
                return False
        return True

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        log.info(f"Message received from user_id={user_id}")

        if user_id != self.allowed_user_id:
            log.warning(f"Ignored message from unauthorised user_id={user_id} (allowed={self.allowed_user_id})")
            return

        # Best-effort ack — react with eyes to confirm receipt
        try:
            await update.message.set_reaction("👀")
        except Exception as e:
            log.debug("Reaction not supported, falling back to typing indicator: %s", e)
            try:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action=ChatAction.TYPING,
                )
            except Exception:
                pass  # best-effort only

        # Check if we're awaiting a /missed reply (FR-12)
        if (hasattr(context, "user_data") and
            isinstance(context.user_data, dict) and
            context.user_data.get("awaiting_missed_reply") is True):
            # Check timeout (60 seconds)
            start_time = context.user_data.get("missed_start_time", 0)
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > 60:
                await update.message.reply_text("Cancelled (timeout).")
                context.user_data["awaiting_missed_reply"] = False
                try:
                    await update.message.set_reaction("❌")
                except Exception:
                    pass
                return
            # Handle the structured reply
            await self._handle_missed_reply(update, context)
            return

        # Check if we're awaiting a /addgoal reply
        if (hasattr(context, "user_data") and
            isinstance(context.user_data, dict) and
            context.user_data.get("awaiting_addgoal_reply") is True):
            # Check timeout (60 seconds)
            start_time = context.user_data.get("addgoal_start_time", 0)
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > 60:
                await update.message.reply_text("Cancelled (timeout).")
                context.user_data["awaiting_addgoal_reply"] = False
                try:
                    await update.message.set_reaction("❌")
                except Exception:
                    pass
                return
            # Handle the structured reply
            await self._handle_addgoal_reply(update, context)
            return

        # Check if we're awaiting a /addproject reply
        if (hasattr(context, "user_data") and
            isinstance(context.user_data, dict) and
            context.user_data.get("awaiting_addproject_reply") is True):
            # Check timeout (60 seconds)
            start_time = context.user_data.get("addproject_start_time", 0)
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > 60:
                await update.message.reply_text("Cancelled (timeout).")
                context.user_data["awaiting_addproject_reply"] = False
                try:
                    await update.message.set_reaction("❌")
                except Exception:
                    pass
                return
            # Handle the structured reply
            await self._handle_addproject_reply(update, context)
            return

        # Handle document uploads (PDF, txt, md)
        # Check for document - in real Telegram messages, document is None for text messages
        # In test mocks with AsyncMock, document might be auto-created, so check file_id specifically
        doc = getattr(update.message, "document", None)
        if doc is not None:
            # Verify it's a real document object with file_id (not just a mock attribute)
            file_id = getattr(doc, "file_id", None)
            if file_id is not None and not callable(file_id):
                await self._handle_document_upload(update, context)
                return

        query = update.message.text
        chat_id = update.effective_chat.id
        # Security (M2): log hash+length at INFO, full query at DEBUG to avoid leaking sensitive content
        query_hash = hashlib.sha256(query.encode()).hexdigest()[:8]
        log.info(f"Processing query: hash={query_hash} len={len(query)}")
        log.debug(f"Query content: {query[:200]!r}")

        # Serialise per-chat to preserve turn ordering
        lock = self._chat_history_locks.setdefault(chat_id, asyncio.Lock())

        async with lock:
          try:
            history = self._chat_history.get(chat_id, [])

            memory_context = await self._load_context(query, history)
            log.info(f"Context loaded: {len(memory_context)} chars")

            from chat_tools import TOOLS, MUTATING_TOOLS, dispatch as _tool_dispatch

            # Track mutating tool calls so a timeout can warn rather than suggest a
            # blind retry that would duplicate state.
            # _completed_mutations: tools that returned successfully before the timeout.
            # _inflight_mutations: tools whose dispatch was in-flight when the timeout
            #   fired (await cancelled before result arrived — partial state is possible).
            _completed_mutations: list[tuple[str, str]] = []  # (name, result)
            _inflight_mutations: list[str] = []

            async def tool_dispatch(name: str, args: dict) -> str:
                if name == "get_recent_commands":
                    limit = min(int(args.get("limit", 5)), 10)
                    return self._recent_commands_text(chat_id, limit)
                is_mutating = name in MUTATING_TOOLS
                if is_mutating:
                    _inflight_mutations.append(name)
                result = await _tool_dispatch(name, args, self)
                if is_mutating:
                    _inflight_mutations.remove(name)
                    if _mutation_succeeded(name, result):
                        _completed_mutations.append((name, result))
                return result

            # Only expose pending-reply tools when: (a) this chat has a non-empty queue, AND
            # (b) the last assistant message was the reconnect notification — prevents
            # "yes" from being misattributed to deliver_pending_replies mid-conversation.
            pending_state = self._load_pending()
            has_pending = bool(pending_state.get(str(chat_id), {}).get("pending"))
            last_was_notification = (
                bool(history)
                and history[-1].get("role") == "assistant"
                and "\U0001f4ec Network is back" in history[-1].get("content", "")
            )
            active_tools = [
                t for t in TOOLS
                if (has_pending and last_was_notification)
                or t["function"]["name"] not in ("deliver_pending_replies", "discard_pending_replies")
            ]

            # Trim to token budget before sending — memory context already consumes
            # up to 150K tokens; without this, a long thread can exceed the 200K window.
            history_for_api = self._trim_history_tokens(history)


            try:
                response = await asyncio.wait_for(
                    self.executor.run_with_tools(
                        inputs={"memory_context": memory_context, "user_query": query},
                        tools=active_tools,
                        tool_dispatch=tool_dispatch,
                        history=history_for_api,
                    ),
                    timeout=240.0,
                )
            except asyncio.TimeoutError:
                log.error("run_with_tools timed out after 240s for chat_id=%s", chat_id)
                if _completed_mutations or _inflight_mutations:
                    parts = []
                    if _completed_mutations:
                        parts.append(
                            "The following action(s) completed before the timeout: "
                            + "; ".join(r for _, r in _completed_mutations)
                        )
                    if _inflight_mutations:
                        parts.append(
                            "The following action(s) were in progress when the timeout fired "
                            "and may or may not have been applied: "
                            + ", ".join(_inflight_mutations)
                        )
                    timeout_msg = (
                        "The request timed out. "
                        + " ".join(parts)
                        + ". Please verify before retrying to avoid duplicates."
                    )
                    log.warning(
                        "Timeout: completed=%s inflight=%s for chat_id=%s",
                        _completed_mutations, _inflight_mutations, chat_id,
                    )
                else:
                    timeout_msg = "Sorry — the request timed out. Try asking a more specific question."
                try:
                    await update.message.reply_text(timeout_msg)
                except Exception:
                    pass
                try:
                    await update.message.set_reaction("❌")
                except Exception:
                    pass
                return

            log.info(f"Response: {len(response) if response else 0} chars")
            if response is None:
                try:
                    await update.message.reply_text(
                        "Sorry — the chat model failed. Check ~/secondbrain/logs/error.log."
                    )
                except (TimedOut, NetworkError) as e:
                    log.error("Couldn't send model-failure notice: %s", e)
                except Exception:
                    pass
                try:
                    await update.message.set_reaction("❌")
                except Exception:
                    pass
                return
            # Deliver response first, only update history on success
            delivered = await self._send_reply(update, response)
            if delivered:
                assistant_text = response[:4096]
                turns = self._chat_history.setdefault(chat_id, [])
                turns.append({"role": "user", "content": query})
                turns.append({"role": "assistant", "content": assistant_text})
                max_msgs = self.HISTORY_WINDOW_TURNS * 2
                if len(turns) > max_msgs:
                    self._chat_history[chat_id] = turns[-max_msgs:]
                self._save_history()
                try:
                    await update.message.set_reaction("✅")
                except Exception:
                    pass
            else:
                # Delivery failed — queue for reconnect
                self._queue_pending_reply(chat_id, query, response)
          except Exception as e:
            log.error(f"Error processing message: {e}", exc_info=True)
            try:
                await update.message.reply_text(f"Sorry — processing failed: {_safe_error(e)}")
            except (TimedOut, NetworkError) as reply_err:
                log.error("Couldn't send failure notice (network): %s", reply_err)
            except Exception:
                pass
            try:
                await update.message.set_reaction("❌")
            except Exception:
                pass

    # ── /circles, /circle, /circle_status commands ────────────────────────────

    def _circles_time_ago(self, iso_str: Optional[str]) -> str:
        """Return human-readable 'N min ago' or 'never' from ISO timestamp."""
        if not iso_str:
            return "never"
        try:
            from datetime import timezone as _tz
            ts = datetime.fromisoformat(iso_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            delta = datetime.now(_tz.utc) - ts
            minutes = int(delta.total_seconds() / 60)
            if minutes < 1:
                return "just now"
            if minutes < 60:
                return f"{minutes} min ago"
            hours = minutes // 60
            if hours < 24:
                return f"{hours}h ago"
            return f"{hours // 24}d ago"
        except Exception:
            return "unknown"

    def _format_circle_rule(self, rule: dict) -> str:
        """Format a single rule dict as a compact string like 'type:calendar_event tags:[family]'."""
        parts = []
        for key, val in rule.items():
            if isinstance(val, list):
                parts.append(f"{key}:[{','.join(str(v) for v in val)}]")
            else:
                parts.append(f"{key}:{val}")
        return " ".join(parts)

    async def cmd_circles(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        circles_dir = DEPLOY_DIR / "circles"
        ruleset_files = sorted(circles_dir.glob("*.yaml")) if circles_dir.exists() else []
        if not ruleset_files:
            await update.message.reply_text("No circles configured. Add YAML files to ~/secondbrain/circles/.")
            return

        try:
            state_path = DEPLOY_DIR / "circle-sync-state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except Exception:
            state = {}

        self._last_circle_set = []
        lines = [f"Circles ({len(ruleset_files)} configured):"]
        for i, path in enumerate(ruleset_files, 1):
            try:
                from circle_ruleset import load_ruleset
                ruleset = load_ruleset(path)
            except ValueError as e:
                lines.append(f"{i}. {path.stem} — ⚠️ malformed ruleset: {e}")
                self._last_circle_set.append(None)
                continue
            circle_state = state.get(ruleset.slug, {})
            synced_count = len(circle_state.get("synced_files", {}))
            last_run = self._circles_time_ago(circle_state.get("last_run"))
            member_count = len(ruleset.members)
            lines.append(
                f"{i}. {ruleset.slug} — {member_count} member{'s' if member_count != 1 else ''}"
                f" · {synced_count} file{'s' if synced_count != 1 else ''} synced"
                f" · last sync {last_run}"
            )
            self._last_circle_set.append(path)
        self._active_list = self._last_circle_set

        await update.message.reply_text("\n".join(lines))

    async def cmd_circle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /circle <N>")
            return
        try:
            n = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /circle <N>")
            return
        if not self._last_circle_set or n < 1 or n > len(self._last_circle_set):
            await update.message.reply_text("Invalid index. Run /circles first.")
            return
        path = self._last_circle_set[n - 1]
        if path is None:
            await update.message.reply_text("That circle has a malformed ruleset. Run /circles for details.")
            return

        try:
            from circle_ruleset import load_ruleset
            ruleset = load_ruleset(path)
        except ValueError as e:
            await update.message.reply_text(f"Failed to load circle: {e}")
            return

        icloud_root = Path(
            self._config.get("circles", {}).get("icloud_root", str(DEFAULT_ICLOUD_ROOT))
        ).expanduser()
        icloud_path = icloud_root / ruleset.icloud_folder
        folder_status = "✓" if icloud_path.exists() else "❌"

        try:
            state_path = DEPLOY_DIR / "circle-sync-state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except Exception:
            state = {}
        circle_state = state.get(ruleset.slug, {})
        synced_count = len(circle_state.get("synced_files", {}))

        member_names = [m.get("name", str(m.get("telegram_user_id", "?"))) for m in ruleset.members]

        lines = [f"{ruleset.slug} ({ruleset.display_name})"]
        lines.append(f"Members: {', '.join(member_names) if member_names else '(none)'}")
        lines.append(f"iCloud folder: {ruleset.icloud_folder} {folder_status}")
        lines.append(f"Synced: {synced_count} file{'s' if synced_count != 1 else ''}")

        if ruleset.include_rules:
            lines.append("")
            lines.append("Include rules:")
            for rule in ruleset.include_rules:
                lines.append(f"  · {self._format_circle_rule(rule)}")
        if ruleset.exclude_rules:
            lines.append("")
            lines.append("Exclude rules:")
            for rule in ruleset.exclude_rules:
                lines.append(f"  · {self._format_circle_rule(rule)}")

        await update.message.reply_text("\n".join(lines))

    async def cmd_circle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        circles_dir = DEPLOY_DIR / "circles"
        ruleset_files = sorted(circles_dir.glob("*.yaml")) if circles_dir.exists() else []
        if not ruleset_files:
            await update.message.reply_text("No circles configured.")
            return

        try:
            state_path = DEPLOY_DIR / "circle-sync-state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except Exception:
            state = {}

        icloud_root = Path(
            self._config.get("circles", {}).get("icloud_root", str(DEFAULT_ICLOUD_ROOT))
        ).expanduser()

        lines = ["Circle sync status:"]
        for path in ruleset_files:
            try:
                from circle_ruleset import load_ruleset
                ruleset = load_ruleset(path)
            except ValueError as e:
                lines.append(f"⚠️  {path.stem} — malformed ruleset: {e}")
                continue
            icloud_path = icloud_root / ruleset.icloud_folder
            folder_ok = icloud_path.exists()
            circle_state = state.get(ruleset.slug, {})
            last_run = self._circles_time_ago(circle_state.get("last_run"))
            synced_count = len(circle_state.get("synced_files", {}))
            status_icon = "✓" if folder_ok else "❌"
            lines.append(
                f"{status_icon} {ruleset.slug}"
                + (f" — {synced_count} files synced · last sync {last_run}" if folder_ok else " — iCloud folder missing")
            )

        await update.message.reply_text("\n".join(lines))

    async def cmd_circle_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /circle_rule add <N> include|exclude type:value [tags:v1,v2 ...]
        /circle_rule remove <N> <rule_index>

        Edits the YAML ruleset file for circle N from the last /circles list.
        Writes atomically; scanner picks up the change on its next cycle.
        """
        if not self._check_auth(update):
            return

        args = context.args or []
        USAGE = (
            "Usage:\n"
            "  /circle_rule add <N> include|exclude type:value [tags:v1,v2 ...]\n"
            "  /circle_rule remove <N> <rule_index>\n\n"
            "Predicates: type:value  tags:v1,v2  category:value\n"
            "            classification:value  hostname:value  source_title:text"
        )

        if len(args) < 3:
            await update.message.reply_text(USAGE)
            return

        subcommand = args[0].lower()
        try:
            circle_n = int(args[1])
        except ValueError:
            await update.message.reply_text(USAGE)
            return

        if not self._last_circle_set or circle_n < 1 or circle_n > len(self._last_circle_set):
            await update.message.reply_text("Invalid circle index. Run /circles first.")
            return

        path = self._last_circle_set[circle_n - 1]
        if path is None:
            await update.message.reply_text("That circle has a malformed ruleset.")
            return

        try:
            raw_data = yaml.safe_load(path.read_text()) or {}
        except Exception as e:
            await update.message.reply_text(f"Failed to read ruleset: {e}")
            return

        if subcommand == "add":
            if len(args) < 4:
                await update.message.reply_text(USAGE)
                return
            direction = args[2].lower()
            if direction not in ("include", "exclude"):
                await update.message.reply_text("Direction must be 'include' or 'exclude'.")
                return

            from circle_ruleset import parse_rule_predicates, write_ruleset_yaml
            rule = parse_rule_predicates(args[3:])
            if not rule:
                await update.message.reply_text(
                    "No valid predicates found.\n"
                    "Examples: type:calendar_event  tags:family,home  classification:marketing"
                )
                return

            rules = raw_data.setdefault("rules", {})
            rules.setdefault(direction, []).append(rule)
            try:
                write_ruleset_yaml(path, raw_data)
            except OSError as e:
                await update.message.reply_text(f"Failed to save ruleset: {e}")
                return

            rule_str = self._format_circle_rule(rule)
            await update.message.reply_text(
                f"Added {direction} rule to {path.stem}:\n  · {rule_str}"
            )

        elif subcommand == "remove":
            try:
                rule_idx = int(args[2])
            except ValueError:
                await update.message.reply_text(USAGE)
                return

            rules = raw_data.get("rules", {})
            include_list = list(rules.get("include", []))
            exclude_list = list(rules.get("exclude", []))
            total = len(include_list) + len(exclude_list)

            if rule_idx < 1 or rule_idx > total:
                await update.message.reply_text(
                    f"Invalid rule index. Circle '{path.stem}' has {total} rule(s).\n"
                    "Run /circle <N> to see numbered rules."
                )
                return

            if rule_idx <= len(include_list):
                removed = include_list.pop(rule_idx - 1)
                removed_from = "include"
            else:
                idx_in_exclude = rule_idx - len(include_list) - 1
                removed = exclude_list.pop(idx_in_exclude)
                removed_from = "exclude"

            rules["include"] = include_list
            rules["exclude"] = exclude_list
            raw_data["rules"] = rules

            from circle_ruleset import write_ruleset_yaml
            try:
                write_ruleset_yaml(path, raw_data)
            except OSError as e:
                await update.message.reply_text(f"Failed to save ruleset: {e}")
                return

            rule_str = self._format_circle_rule(removed)
            await update.message.reply_text(
                f"Removed {removed_from} rule {rule_idx} from {path.stem}:\n  · {rule_str}"
            )

        else:
            await update.message.reply_text(
                f"Unknown subcommand '{subcommand}'. Use 'add' or 'remove'.\n\n{USAGE}"
            )

    async def cmd_circle_invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /circle_invite <N> — Generate a one-time invite code for circle N.

        Stores an 8-char hex code in DEPLOY_DIR/circle-invites.json with a
        24-hour TTL.  Uses a separate file from circle-sync-state.json so the
        scanner never overwrites pending invites.  Circle members redeem the
        code via /join <code> on the circle's member bot.
        """
        if not self._check_auth(update):
            return

        args = context.args or []
        if not args or not args[0].isdigit():
            await update.message.reply_text(
                "Usage: /circle_invite <N>  (run /circles first to get N)"
            )
            return

        n = int(args[0])
        if not self._last_circle_set or n < 1 or n > len(self._last_circle_set):
            await update.message.reply_text("Invalid index. Run /circles first.")
            return

        path = self._last_circle_set[n - 1]
        if path is None:
            await update.message.reply_text("That circle has a malformed ruleset.")
            return

        try:
            from circle_ruleset import load_ruleset
            ruleset = load_ruleset(path)
        except (ValueError, Exception) as e:
            await update.message.reply_text(f"Failed to load circle: {e}")
            return

        code = os.urandom(4).hex()  # 8-char hex, cryptographically random

        invites_file = DEPLOY_DIR / "circle-invites.json"
        invites_state: dict = {}
        if invites_file.exists():
            try:
                invites_state = json.loads(invites_file.read_text())
            except Exception:
                pass

        circle_invites = invites_state.setdefault(ruleset.slug, {})
        circle_invites[code] = {
            "expires_at": time.time() + 86400,  # 24-hour TTL
            "created_by": update.effective_user.id,
        }

        tmp_path = invites_file.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(invites_state, indent=2))
            os.rename(str(tmp_path), str(invites_file))
        except Exception as e:
            log.warning("circle_invite: failed to save invites: %s", e)
            await update.message.reply_text("Failed to generate invite code. Try again.")
            return

        display = ruleset.display_name or ruleset.slug
        await update.message.reply_text(
            f"Invite link for {display}:\n"
            f"Ask them to message the circle bot and send:\n"
            f"  /join {code}\n"
            f"(expires in 24 hours)"
        )

    # ── CommandRouter bridge ──────────────────────────────────────────────────

    def register_with_router(self, router) -> None:
        """Register all cmd_* methods and the LLM chat handler with a CommandRouter.

        Creates lightweight fake Telegram update/context objects so existing
        cmd_* methods work unchanged over non-Telegram transports (e.g. Slack).
        Auth is assumed to have been checked at the adapter boundary.
        """
        import inspect

        allowed_uid = self.allowed_user_id

        def _make_fake(ctx):
            """Return (fake_update, fake_context) wired to ctx.reply / ctx.args."""

            class _FakeMessage:
                def __init__(self):
                    self.text = ""
                    self.document = None
                    self.photo = None
                    self.chat = _FakeChat()

                async def reply_text(self, text: str, **kwargs) -> None:
                    await ctx.reply(text)

                async def set_reaction(self, *args, **kwargs) -> None:
                    pass  # no-op outside Telegram

            class _FakeChat:
                id = allowed_uid

            class _FakeUser:
                id = allowed_uid

            class _FakeBot:
                async def send_chat_action(self, **kwargs) -> None:
                    pass

                async def send_message(self, *args, **kwargs) -> None:
                    pass

            class _FakeUpdate:
                def __init__(self):
                    self.message = _FakeMessage()
                    self.effective_user = _FakeUser()
                    self.effective_chat = _FakeChat()

            class _FakeContext:
                def __init__(self):
                    self.args = list(ctx.args)
                    self.bot = _FakeBot()

            return _FakeUpdate(), _FakeContext()

        # Register every cmd_* method
        for attr_name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if not attr_name.startswith("cmd_"):
                continue
            cmd_name = attr_name[4:]  # strip "cmd_" prefix
            bound = method

            async def _handler(ctx, _m=bound) -> None:
                fake_update, fake_context = _make_fake(ctx)
                await _m(fake_update, fake_context)

            router.register(cmd_name, _handler)

        # Register Telegram command aliases that have no matching cmd_<alias> method.
        # These are wired in setup_handlers() as CommandHandler(alias, self.cmd_target)
        # but register_with_router() only discovers cmd_* methods by name.
        _ALIASES: dict[str, str] = {
            "people":          "contacts",
            "messages":        "comms",
            "communications":  "comms",
            "message":         "comm",
            "communication":   "comm",
            "commands":        "help",
            "fdetail":         "feature_detail",
            "feature_new":     "feature",
        }
        for alias, target in _ALIASES.items():
            target_handler = router._cmd_handlers.get(target)
            if target_handler is not None:
                router.register(alias, target_handler)

        # Register free-text LLM chat as __message__
        async def _message_handler(ctx) -> None:
            fake_update, fake_context = _make_fake(ctx)
            # Inject the original text so handle_message can read it
            fake_update.message.text = getattr(ctx, "raw_text", "")
            await self.handle_message(fake_update, fake_context)

        router.register("__message__", _message_handler)
