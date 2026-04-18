import asyncio
import json
import logging
import os
import re
import socket
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TimedOut, NetworkError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from skill_executor import SkillExecutor
from content_fetcher import fetch_url_content
from github_client import GitHubClient, _STANDARD_LABELS
from goals_tracker import GoalManager

log = logging.getLogger("chat-handler")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
MAX_CONTEXT_CHARS = 80_000
TG_MAX_CHARS = 4096  # Telegram hard limit per message

# Single source of truth for all Telegram commands and their descriptions.
# /help iterates this to render the grouped help text.
# A test enforces that every CommandHandler registration appears here.
COMMAND_REGISTRY: dict[str, list[tuple[str, str]]] = {
    "Knowledge listings": [
        ("readings",       "List recent web captures"),
        ("search",         "Keyword search — grouped by type. /search <type> <query> to filter"),
        ("reading",        "Show reading N from last list"),
        ("forget",         "Forget item(s) N [N...] from your last list, or all captures from a domain"),
        ("people",         "List contacts (alias of /contacts)"),
        ("contacts",       "List people you've interacted with"),
        ("contact",        "Show contact by name or N"),
        ("code",           "List git repos"),
        ("events",         "List recent and upcoming calendar events"),
        ("event",          "Show event N from last list"),
        ("meetings",       "List recent meeting transcripts"),
        ("meeting",        "Show meeting N from last list"),
        ("comms",          "List recent email + slack threads (optional 'email' or 'slack' filter)"),
        ("comm",           "Show comm N from last list"),
        ("messages",       "Alias of /comms"),
        ("communications", "Alias of /comms"),
        ("message",        "Alias of /comm"),
        ("communication",  "Alias of /comm"),
    ],
    "Commitments": [
        ("commitments", "List active commitments"),
        ("complete",    "Mark commitment N complete"),
        ("dismiss",     "Dismiss commitment N"),
        ("wrong",       "Mark extracted commitment N as a false positive"),
        ("missed",      "Manually add a commitment the bot missed"),
        ("accuracy",    "Show extraction precision per source type"),
    ],
    "Goals": [
        ("addgoal",       "Add a new goal"),
        ("goals",         "List goals (filter: /goals [category|status])"),
        ("goal",          "Show goal N from last list"),
        ("completegoal",  "Mark goal N as completed"),
        ("abandongoal",   "Mark goal N as abandoned"),
    ],
    "Projects": [
        ("addproject",      "Add a new project"),
        ("projects",        "List projects (filter: /projects [category|status])"),
        ("project",         "Show project N from last list"),
        ("completeproject", "Mark project N as completed"),
        ("abandonproject",  "Mark project N as abandoned"),
        ("holdproject",     "Put project N on hold"),
        ("addmilestone",    "Add milestone to project N: /addmilestone N text"),
        ("milestone",       "Toggle milestone M on project N: /milestone N M"),
        ("linkgoal",        "Link project N to goal M: /linkgoal N M"),
        ("unlinkgoal",      "Unlink project N from its goal"),
    ],
    "Review": [
        ("review",   "List pending project/repo candidates (/review N for detail)"),
        ("confirm",  "Confirm candidate N (/confirm N [category])"),
        ("reject",   "Reject candidate N"),
        ("edit",     "Edit a candidate field (/edit N field=value)"),
    ],
    "Agent actions": [
        ("actions", "List pending agent-proposed actions (filter: approved, all)"),
        ("action",  "Show full detail for action N"),
        ("run",     "Approve and execute action N"),
        ("drop",    "Reject action N"),
        ("defer",   "Snooze action N for N hours (default 24)"),
    ],
    "Notifications": [
        ("briefing", "Trigger today's briefing now"),
        ("mute",     "Suppress proactive notifications"),
        ("unmute",   "Resume proactive notifications"),
    ],
    "Domain filter": [
        ("skip",     "Add a domain to the ignore list"),
        ("unskip",   "Remove a domain from the ignore list"),
        ("skiplist", "Show currently skipped domains"),
    ],
    "Feature Requests": [
        ("feature",          "Capture a new feature request"),
        ("feature_new",      "Alias of /feature"),
        ("bug",              "Capture a new bug report"),
        ("bugs",             "List bug reports (alias of /features bug)"),
        ("features",         "List feature requests. Filter: bug|feature|<status>|all"),
        ("feature_detail",   "Show feature N from last list"),
        ("fdetail",          "Alias of /feature-detail"),
        ("feature_priority", "Set priority of feature N (low/medium/high/critical)"),
        ("feature_plan",     "Mark feature N as planned"),
        ("feature_start",    "Mark feature N as in-progress"),
        ("feature_done",     "Mark feature N as done (optional closing note)"),
        ("feature_wont_do",  "Mark feature N as won't do (optional reason)"),
        ("feature_note",     "Append a timestamped note to feature N"),
        ("feature_import",   "Import existing local feature files into GitHub issues (one-time migration)"),
    ],
    "Skill Management": [
        ("skill_drafts",    "List pending skill drafts awaiting approval"),
        ("skill_draft",     "Show full draft N from last list"),
        ("approve_skill",   "Approve skill draft N and enter probation"),
        ("reject_skill",    "Reject skill draft N"),
        ("skill_approval",  "Toggle HITL mode: /skill-approval on|off|status"),
        ("skill_health",    "Show all skills with utility scores and trends"),
    ],
    "Reports": [
        ("reports",        "List all configured reports"),
        ("report",         "Show report N detail from last list"),
        ("report_add",     "Add a new report: /report-add <schedule> <type> <sources...>"),
        ("report_remove",  "Remove runtime report N"),
        ("report_pause",   "Pause report N"),
        ("report_resume",  "Resume paused report N"),
        ("report_run",     "Run report N immediately"),
    ],
    "Meta": [
        ("help",     "Show this list"),
        ("commands", "Alias of /help"),
        ("settings", "View or change display settings (date_format, timezone)"),
        ("reset",    "Clear conversation history (history is lost on daemon restart anyway)"),
        ("deliver",  "Send queued replies that couldn't be delivered due to network issues"),
        ("discard",  "Drop queued replies that couldn't be delivered due to network issues"),
    ],
    "System": [
        ("backfill", "Reprocess historical data: /backfill <type> [days] [host]. Types: readings, email, zoom, calendar, slack, projects"),
        ("remember", "Fetch a URL and save a reading memory: /remember <url>"),
        ("note", "Fetch a URL and save detailed study notes: /note <url>"),
        ("version", "Show the running daemon version"),
    ],
}

# Backfill configuration: default and max days per scanner type
BACKFILL_CONFIG = {
    "readings": {"default_days": 30, "max_days": 90},
    "email":    {"default_days": 30, "max_days": 90},
    "zoom":     {"default_days": 30, "max_days": 180},
    "calendar": {"default_days": 30, "max_days": 180},
    "slack":    {"default_days": 30, "max_days": 90},
    "code":     {"default_days": 0,  "max_days": 0},
}


def _safe_read_text(path: Path) -> Optional[str]:
    """Read iCloud file, returning None on transient OSError (e.g. errno 11 EDEADLK)."""
    try:
        return path.read_text()
    except OSError as e:
        log.warning("Read failed for %s: %s", path, e)
        return None


class TelegramChatHandler:
    PENDING_FILE = DEPLOY_DIR / "pending-replies.json"
    HISTORY_FILE = DEPLOY_DIR / "chat-history.json"

    def __init__(self, scanners: dict = None):
        try:
            config_text = (BRAIN_DIR / "config.yaml").read_text()
            config = yaml.safe_load(config_text) or {}
        except OSError as e:
            log.warning("Could not read config.yaml at startup: %s — using defaults", e)
            config = {}
        self._config = config  # Store for access by tools and helpers
        self.token = config["telegram"]["bot_token"]
        self.allowed_user_id = int(config["user"]["telegram_user_id"])
        self.executor = SkillExecutor("chat")
        self.app = ApplicationBuilder().token(self.token).build()
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
        self.app.add_handler(CommandHandler("readings", self.cmd_readings))
        self.app.add_handler(CommandHandler("search", self.cmd_search))
        self.app.add_handler(CommandHandler("reading", self.cmd_reading))
        self.app.add_handler(CommandHandler("forget", self.cmd_forget))
        self.app.add_handler(CommandHandler("commitments", self.cmd_commitments))
        self.app.add_handler(CommandHandler("complete", self.cmd_complete))
        self.app.add_handler(CommandHandler("dismiss", self.cmd_dismiss))
        self.app.add_handler(CommandHandler("wrong", self.cmd_wrong))
        self.app.add_handler(CommandHandler("missed", self.cmd_missed))
        self.app.add_handler(CommandHandler("accuracy", self.cmd_accuracy))
        self.app.add_handler(CommandHandler("contacts", self.cmd_contacts))
        self.app.add_handler(CommandHandler("contact", self.cmd_contact))
        self.app.add_handler(CommandHandler("people", self.cmd_contacts))
        self.app.add_handler(CommandHandler("code", self.cmd_code))
        self.app.add_handler(CommandHandler("events", self.cmd_events))
        self.app.add_handler(CommandHandler("event", self.cmd_event))
        self.app.add_handler(CommandHandler("meetings", self.cmd_meetings))
        self.app.add_handler(CommandHandler("meeting", self.cmd_meeting))
        self.app.add_handler(CommandHandler("comms", self.cmd_comms))
        self.app.add_handler(CommandHandler("messages", self.cmd_comms))
        self.app.add_handler(CommandHandler("communications", self.cmd_comms))
        self.app.add_handler(CommandHandler("comm", self.cmd_comm))
        self.app.add_handler(CommandHandler("message", self.cmd_comm))
        self.app.add_handler(CommandHandler("communication", self.cmd_comm))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("commands", self.cmd_help))
        self.app.add_handler(CommandHandler("version", self.cmd_version))
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
        # Projects
        self.app.add_handler(CommandHandler("addproject", self.cmd_addproject))
        self.app.add_handler(CommandHandler("projects", self.cmd_projects))
        self.app.add_handler(CommandHandler("project", self.cmd_project))
        self.app.add_handler(CommandHandler("completeproject", self.cmd_completeproject))
        self.app.add_handler(CommandHandler("abandonproject", self.cmd_abandonproject))
        self.app.add_handler(CommandHandler("holdproject", self.cmd_holdproject))
        self.app.add_handler(CommandHandler("addmilestone", self.cmd_addmilestone))
        self.app.add_handler(CommandHandler("milestone", self.cmd_milestone))
        self.app.add_handler(CommandHandler("linkgoal", self.cmd_linkgoal))
        self.app.add_handler(CommandHandler("unlinkgoal", self.cmd_unlinkgoal))
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
        self.app.add_handler(CommandHandler("review", self.cmd_review))
        self.app.add_handler(CommandHandler("confirm", self.cmd_confirm))
        self.app.add_handler(CommandHandler("reject", self.cmd_reject))
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
        self.app.add_handler(CommandHandler("note", self.cmd_note))
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
        self.HISTORY_WINDOW_TURNS = 6       # keep last 6 user+assistant pairs (12 messages)
        # Last /review result set — used by /review <N>, /confirm <N>, /reject <N>, /edit <N>.
        self._last_candidate_set: list = []
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
            config = yaml.safe_load((BRAIN_DIR / "config.yaml").read_text())
            return config.get("display", {})
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

    def _build_goal_project_context(self) -> str:
        """Build context block for active goals and projects (FR-7).

        Always injected into chat LLM context, bypassing keyword relevance.
        Returns empty string if no active goals or projects exist.
        """
        import re
        import yaml

        max_items = self._config.get("goals", {}).get("max_context_items", 5)
        lines = []

        # Active goals
        try:
            active_goals = self._goal_manager.list_goals(status="active")[:max_items]
            if active_goals:
                lines.append("## Active Goals")
                for goal_path in active_goals:
                    try:
                        with open(goal_path) as f:
                            content = f.read()
                        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
                        if match:
                            fm = yaml.safe_load(match.group(1))
                            title = fm.get("source_title", goal_path.stem)
                            category = fm.get("category", "")
                            due = fm.get("due_date", "")
                            due_str = f" — due {due}" if due else ""
                            lines.append(f"- {title} [{category}]{due_str}")
                    except Exception:
                        continue
        except Exception:
            pass

        # Active + on-hold projects
        try:
            all_projects = self._goal_manager.list_projects()
            # Filter to active or on-hold
            active_projects = []
            for project_path in all_projects:
                try:
                    with open(project_path) as f:
                        content = f.read()
                    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
                    if match:
                        fm = yaml.safe_load(match.group(1))
                        status = fm.get("status", "")
                        if status in ("active", "on-hold"):
                            active_projects.append((project_path, fm))
                except Exception:
                    continue

            # Cap to max_items
            active_projects = active_projects[:max_items]

            if active_projects:
                if lines:
                    lines.append("")
                lines.append("## Active Projects")
                for project_path, fm in active_projects:
                    title = fm.get("source_title", project_path.stem)
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

    def _load_context(self, query: str, history: list = None) -> str:
        """Load memory files into context with relevance sorting and hard char budget."""
        parts = []
        budget = MAX_CONTEXT_CHARS

        index_path = BRAIN_DIR / "index.md"
        if index_path.exists():
            try:
                chunk = f"# Memory Index\n{index_path.read_text()}"
                parts.append(chunk)
                budget -= len(chunk)
            except OSError:
                pass

        # FR-7: Inject active goals and projects before keyword-matched memories
        goal_context = self._build_goal_project_context()
        if goal_context:
            parts.append(goal_context)
            budget -= len(goal_context)

        memory_files = list((BRAIN_DIR / "memories").glob("*.md"))

        # Augment short queries with recent user messages for better memory scoring
        score_query = query
        if history:
            recent_tokens = {w for w in re.findall(r'\b\w{3,}\b', query.lower())}
            if len(recent_tokens) < 3:
                recent_text = " ".join(
                    turn["content"] for turn in history[-4:]
                    if turn.get("role") == "user"
                )
                score_query = query + " " + recent_text

        # Score using cached headers — O(cache_size) not O(files * file_size)
        scored = sorted(
            memory_files,
            key=lambda p: (self._score_relevance(p, score_query), p.stat().st_mtime),
            reverse=True
        )

        for f in scored:
            if budget <= 0:
                log.debug(f"Context budget exhausted after {len(parts) - 1} memory files")
                break
            try:
                text = f.read_text()
            except OSError:
                continue
            if len(text) > budget:
                text = text[:budget] + "\n[truncated]"
            parts.append(text)
            budget -= len(text)

        return "\n\n---\n\n".join(parts)

    def _edit_skip_domains(self, action: str, domain: str):
        """Add or remove a domain from browser_watcher.skip_domains in config.yaml.

        Returns an error/info string if no change was made, or None on success.
        Writes atomically to avoid corrupting the iCloud-synced config file.
        """
        config_path = BRAIN_DIR / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        domains = config.setdefault("browser_watcher", {}).setdefault("skip_domains", [])
        if action == "add":
            if domain in domains:
                return f"{domain} is already on the skip list."
            domains.append(domain)
        elif action == "remove":
            if domain not in domains:
                return f"{domain} was not on the skip list."
            domains.remove(domain)
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(yaml.dump(config, default_flow_style=False))
        os.rename(tmp, config_path)
        return None

    def _url_matches_domain(self, url: str, domain: str) -> bool:
        """Check if a URL's hostname matches the given domain."""
        from urllib.parse import urlparse
        try:
            host = urlparse(url).hostname or ""
            return host == domain or host.endswith("." + domain)
        except Exception:
            return False

    def _purge_domain(self, domain: str) -> int:
        """Delete all memory files whose source_url frontmatter contains domain.

        Returns the count of deleted files.
        """
        deleted = 0
        for f in (BRAIN_DIR / "memories").glob("*.md"):
            fm = self._parse_frontmatter(f)
            url = fm.get("source_url", "")
            if url and self._url_matches_domain(url, domain):
                f.unlink()
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

            state = self._load_pending()
            if not state:
                continue
            if not await self._is_telegram_reachable():
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
        config = yaml.safe_load((BRAIN_DIR / "config.yaml").read_text())
        domains = config.get("browser_watcher", {}).get("skip_domains", [])
        if not domains:
            await update.message.reply_text("Skip list is empty.")
            return
        lines = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(domains))
        await update.message.reply_text(f"Skipped domains:\n{lines}")


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

    def _list_readings_text(self, limit: int = 10) -> str:
        """Return formatted readings list text (called by cmd_readings and tool dispatch)."""
        limit = max(1, min(limit, 50))
        files = list((BRAIN_DIR / "memories").glob("*.md"))
        if not files:
            return "No memories found."

        # Sort by mtime descending (fast — no file reads needed)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        files = files[:limit]
        self._last_results = files
        self._active_list = files

        lines = [f"Your {len(files)} most recent memories:"]
        for i, f in enumerate(files, 1):
            fm = self._parse_frontmatter(f)
            lines.append(self._fmt_memory_line(i, fm))
        return "\n".join(lines)

    async def cmd_readings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        try:
            limit = int(context.args[0]) if context.args else 10
        except (ValueError, IndexError):
            limit = 10
        text = self._list_readings_text(limit)
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

    def _search_memories_text(self, query: str, type_filter: Optional[str] = None) -> str:
        """Return formatted search results text (called by cmd_search and tool dispatch).
        type_filter is the keyword string like "email", "meeting", not the set."""
        # Resolve type_filter keyword to set
        filter_set: Optional[set] = None
        if type_filter:
            filter_set = self._SEARCH_TYPE_FILTERS.get(type_filter.lower())
            if filter_set is None:
                return f"Unknown type filter: {type_filter!r}. Valid types: email, slack, meeting, project, commitment, event, contact, web"

        files = list((BRAIN_DIR / "memories").glob("*.md"))
        scored = [
            (self._score_relevance(f, query), f.stat().st_mtime, f)
            for f in files
        ]
        matches = sorted(
            [(s, mt, f) for s, mt, f in scored if s > 0],
            key=lambda t: (t[0], t[1]),
            reverse=True,
        )[:50]

        if not matches:
            return f"No memories match '{query}'."

        # Apply type filter if specified
        if filter_set is not None:
            def _matches_type(f):
                fm = self._parse_frontmatter(f)
                t = fm.get("type") or None
                return t in filter_set
            matches = [(s, mt, f) for s, mt, f in matches if _matches_type(f)]
            if not matches:
                return f"No {type_filter} memories match '{query}'."

        if filter_set is not None:
            # Flat list for filtered mode
            self._last_results = [f for _, _, f in matches]
            self._active_list = self._last_results
            lines = [f"Search results for \"{query}\" ({type_filter}) — {len(matches)} match{'es' if len(matches) != 1 else ''}:"]
            for i, (_, _, f) in enumerate(matches, 1):
                fm = self._parse_frontmatter(f)
                mem_type = fm.get("type") or None
                lines.append(self._fmt_search_line(i, fm, mem_type))
            lines.append("\nUse /reading N for detail on any item.")
            return "\n".join(lines)

        # Grouped mode: assign global indices in group-display order
        # Build lookup: path → (score, mtime, fm, type)
        path_data: dict = {}
        for s, mt, f in matches:
            fm = self._parse_frontmatter(f)
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

        text = self._search_memories_text(query, filter_keyword)
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
        count = self._purge_domain(domain)
        if count:
            await update.message.reply_text(f"Forgotten {count} captures from {domain}.")
        else:
            await update.message.reply_text(f"No captures found for {domain}.")

    # ── /commitments command ──────────────────────────────────────────────────

    def _load_active_commitments(self, type_filter: str = None) -> list:
        """Return (path, frontmatter) pairs for active commitment files."""
        results = []
        for f in sorted((BRAIN_DIR / "memories").glob("commitment-*.md")):
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "commitment":
                continue
            if fm.get("status") != "active":
                continue
            if type_filter:
                ct = fm.get("commitment_type", "")
                wanted = "waiting_on" if type_filter.lower() == "waiting" else type_filter.lower()
                if ct != wanted:
                    continue
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

    def _list_commitments_text(self, limit: int = 20) -> str:
        """Return formatted commitments list text (called by cmd_commitments and tool dispatch)."""
        limit = max(1, min(limit, 100))
        items = self._load_active_commitments(type_filter=None)

        if not items:
            return "No active commitments."

        self._last_commitment_set = [f for f, _ in items]
        self._active_list = self._last_commitment_set
        total = len(items)
        lines = [f"Active commitments ({total} total):"]

        for i, (_, fm) in enumerate(items[:limit], 1):
            ct = fm.get("commitment_type", "outbound")
            desc = (fm.get("source_title") or fm.get("summary") or "")[:50]
            owner = fm.get("owner", "")
            due = fm.get("due_date")
            due_str = f" — due {due}" if due else " — due unknown"
            needs_review = "needs-review" in (fm.get("tags") or [])
            flag = " ⚠️" if needs_review else ""
            lines.append(f"{i}. [{ct}] {desc} — {owner}{due_str}{flag}")

        if total > limit:
            lines.append(f"... and {total - limit} more.")

        lines.append("\nUse /complete N or /dismiss N to update status.")
        return "\n".join(lines)

    async def cmd_commitments(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        type_filter = context.args[0] if context.args else None
        # For cmd, we allow type_filter but the tool doesn't expose it — the tool always passes None
        if type_filter:
            # Custom logic for filtered cmd
            items = self._load_active_commitments(type_filter)
            if not items:
                await update.message.reply_text(f"No active {type_filter} commitments.")
                self._last_commitment_set = []
                return
            self._last_commitment_set = [f for f, _ in items]
            self._active_list = self._last_commitment_set
            total = len(items)
            lines = [f"Active {type_filter} commitments ({total} total):"]
            for i, (_, fm) in enumerate(items[:20], 1):
                ct = fm.get("commitment_type", "outbound")
                desc = (fm.get("source_title") or fm.get("summary") or "")[:50]
                owner = fm.get("owner", "")
                due = fm.get("due_date")
                due_str = f" — due {due}" if due else " — due unknown"
                needs_review = "needs-review" in (fm.get("tags") or [])
                flag = " ⚠️" if needs_review else ""
                lines.append(f"{i}. [{ct}] {desc} — {owner}{due_str}{flag}")
            if total > 20:
                lines.append(f"... and {total - 20} more.")
            lines.append("\nUse /complete N or /dismiss N to update status.")
            await update.message.reply_text("\n".join(lines))
        else:
            text = self._list_commitments_text(limit=20)
            await update.message.reply_text(text)

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
                CommitmentTracker().update_commitment_status(path, "completed")
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
                CommitmentTracker().update_commitment_status(path, "dismissed")
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
            CommitmentTracker().update_commitment_status(path, "dismissed")

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
            await update.message.reply_text(f"Error marking as wrong: {e}")

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
            await update.message.reply_text(f"Error creating commitment: {e}")
        finally:
            context.user_data["awaiting_missed_reply"] = False

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

    # ── Agent actions commands ────────────────────────────────────────────────

    def _load_action_set(self, filter_status: Optional[str] = None) -> list:
        """Load action-*.md files and filter by status. Returns list of (path, fm) tuples."""
        actions = []
        now = datetime.now()
        memories_dir = BRAIN_DIR / "memories"

        for path in memories_dir.glob("action-*.md"):
            try:
                fm = self._parse_frontmatter(path)
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
        self._last_action_set = actions
        return actions

    async def cmd_actions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List pending agent-proposed actions."""
        if not self._check_auth(update):
            return

        filter_arg = context.args[0].lower() if context.args else None
        actions = self._load_action_set(filter_status=filter_arg)

        if not actions:
            msg = "No pending agent actions."
            if filter_arg:
                msg += f" (filter: {filter_arg})"
            msg += " Use /actions all to see all."
            await update.message.reply_text(msg)
            return

        lines = [f"Agent-proposed actions ({len(actions)}):"]
        for i, (path, fm) in enumerate(actions, 1):
            action_type = fm.get("action_type", "")
            target = fm.get("target") or fm.get("source_goal", "")
            rationale = fm.get("rationale", "")[:60]
            lines.append(f"{i}. [{action_type}] {target} — {rationale}")

        lines.append("")
        lines.append("Use /action N for details, /run N to approve and execute.")
        await update.message.reply_text("\n".join(lines))

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
            await update.message.reply_text(f"Error reading action file: {e}")
            return

        if fresh_fm.get("status") != "pending":
            await update.message.reply_text(f"Action {idx+1} is no longer pending (status: {fresh_fm.get('status')})")
            return

        # Execute
        from goal_project_agent import GoalProjectAgent
        agent = GoalProjectAgent(role="full")
        try:
            msg = agent._execute_action(path, fresh_fm)
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
                await update.message.reply_text(f"Error executing action {idx+1}: {e}")

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
            await update.message.reply_text(f"Error reading action file: {e}")
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
            await update.message.reply_text(f"Error reading action file: {e}")
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
            await update.message.reply_text(f"Error creating goal: {e}")
        except Exception as e:
            log.exception("Error in _handle_addgoal_reply")
            await update.message.reply_text(f"Error creating goal: {e}")
        finally:
            context.user_data["awaiting_addgoal_reply"] = False

    async def cmd_goals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        # Parse optional filter argument
        category = None
        status = "active"  # default
        if context.args:
            arg = context.args[0]
            # Check if it's a status
            if arg in ["active", "completed", "abandoned"]:
                status = arg
            # Otherwise treat as category
            else:
                category = arg
                status = None

        try:
            goals = self._goal_manager.list_goals(category=category, status=status)
            self._last_goal_set = goals
            self._active_list = self._last_goal_set

            if not goals:
                await update.message.reply_text("No goals found.")
                return

            # Format the list
            from datetime import datetime, timedelta
            lines = []
            header = f"{status.capitalize() if status else 'All'} goals ({len(goals)} total):"
            lines.append(header)

            for i, path in enumerate(goals, 1):
                fm = self._parse_frontmatter(path)
                cat = fm.get("category", "")
                title = fm.get("source_title", "")
                due = fm.get("due_date")
                due_str = f"due {due}" if due else "no due date"

                # Add deadline proximity indicator if within 7 days
                proximity = ""
                if due:
                    try:
                        due_dt = datetime.strptime(due, "%Y-%m-%d")
                        now = datetime.now()
                        days_until = (due_dt - now).days
                        if 0 <= days_until <= 7:
                            proximity = f" ⚠️ {days_until} days"
                    except ValueError:
                        pass

                lines.append(f"{i}. [{cat}] {title} — {due_str}{proximity}")

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("Error in cmd_goals")
            await update.message.reply_text(f"Error listing goals: {e}")

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
            await update.message.reply_text(f"Error showing goal: {e}")

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
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Error in cmd_completegoal")
            await update.message.reply_text(f"Error completing goal: {e}")

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
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Error in cmd_abandongoal")
            await update.message.reply_text(f"Error abandoning goal: {e}")

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
            await update.message.reply_text(f"Error creating project: {e}")
        except Exception as e:
            log.exception("Error in _handle_addproject_reply")
            await update.message.reply_text(f"Error creating project: {e}")
        finally:
            context.user_data["awaiting_addproject_reply"] = False

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
            projects = self._goal_manager.list_projects(category=category, status=status)
            self._last_project_set = projects
            self._active_list = self._last_project_set

            if not projects:
                await update.message.reply_text("No projects found.")
                return

            # Format the list
            from datetime import datetime, timedelta
            lines = []
            header = f"{status.capitalize() if status else 'All'} projects ({len(projects)} total):"
            lines.append(header)

            for i, path in enumerate(projects, 1):
                fm = self._parse_frontmatter(path)
                cat = fm.get("category", "")
                title = fm.get("source_title", "")
                proj_status = fm.get("status", "")
                due = fm.get("due_date")
                due_str = f"due {due}" if due else "no due date"

                # Milestone summary
                milestones = fm.get("milestones", [])
                if milestones:
                    done_count = sum(1 for m in milestones if m.get("done"))
                    milestone_str = f" (milestones: {done_count}/{len(milestones)} done)"
                else:
                    milestone_str = ""

                lines.append(f"{i}. [{cat}] {title} — {proj_status} — {due_str}{milestone_str}")

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("Error in cmd_projects")
            await update.message.reply_text(f"Error listing projects: {e}")

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
            await update.message.reply_text(f"Error showing project: {e}")

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
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Error in cmd_completeproject")
            await update.message.reply_text(f"Error completing project: {e}")

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
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Error in cmd_abandonproject")
            await update.message.reply_text(f"Error abandoning project: {e}")

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
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Error in cmd_holdproject")
            await update.message.reply_text(f"Error putting project on hold: {e}")

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
            await update.message.reply_text(f"Error adding milestone: {e}")

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
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Error in cmd_milestone")
            await update.message.reply_text(f"Error toggling milestone: {e}")

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
            await update.message.reply_text(f"Error: {e}")
        except Exception as e:
            log.exception("Error in cmd_linkgoal")
            await update.message.reply_text(f"Error linking goal: {e}")

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
            await update.message.reply_text(f"Error unlinking goal: {e}")

    # ── /contacts command ─────────────────────────────────────────────────────

    def _list_contacts_text(self, limit: int = 30) -> str:
        """Return formatted contacts list text (called by cmd_contacts and tool dispatch)."""
        limit = max(1, min(limit, 200))
        files = list((BRAIN_DIR / "memories").glob("contact-*.md"))
        if not files:
            return "No contacts found."

        # Load frontmatter and sort by last_interaction descending
        contacts = []
        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "contact":
                continue
            contacts.append((f, fm))

        contacts.sort(
            key=lambda x: x[1].get("last_interaction") or "",
            reverse=True
        )
        contacts = contacts[:limit]
        self._last_contact_set = [f for f, _ in contacts]

        total = len(list((BRAIN_DIR / "memories").glob("contact-*.md")))
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
        text = self._list_contacts_text(limit)
        await update.message.reply_text(text)

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

    def _find_contact_by_name(self, query: str):
        """Find contact file by case-insensitive substring match on name field."""
        query_lower = query.lower()
        files = list((BRAIN_DIR / "memories").glob("contact-*.md"))

        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "contact":
                continue
            name = fm.get("name", "")
            if query_lower in name.lower():
                return f, fm

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
            fm = self._parse_frontmatter(path)
        else:
            # Try name match
            path, fm = self._find_contact_by_name(arg)
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
        commitment_files = list((BRAIN_DIR / "memories").glob("commitment-*.md"))
        open_commitments = []
        for cf in commitment_files:
            cfm = self._parse_frontmatter(cf)
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
            content = path.read_text()
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

    async def cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List pending project/repo candidates, or show detail of candidate N."""
        if not self._check_auth(update):
            return

        memories_dir = BRAIN_DIR / "memories"
        if not memories_dir.exists():
            await update.message.reply_text("No memories directory found.")
            return

        # Collect pending candidates
        project_candidates = []
        code_candidates = []

        for f in sorted(memories_dir.glob("project-candidate-*.md")):
            try:
                header = f.read_text(encoding="utf-8")[:500]
                fm_type = ""
                status = ""
                candidate_type = ""
                for line in header.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("type:"):
                        fm_type = stripped[5:].strip().strip('"').strip("'")
                    elif stripped.startswith("status:"):
                        status = stripped[7:].strip().strip('"').strip("'")
                    elif stripped.startswith("candidate_type:"):
                        candidate_type = stripped[15:].strip().strip('"').strip("'")

                if fm_type == "project_candidate" and status == "pending_confirmation":
                    if candidate_type == "project":
                        project_candidates.append(f)
                    elif candidate_type == "code_repo":
                        code_candidates.append(f)
            except Exception:
                continue

        # Sort by created descending (newest first)
        def created_key(p: Path) -> str:
            try:
                content = p.read_text(encoding="utf-8")
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = yaml.safe_load(parts[1]) or {}
                    return fm.get("created", "")
            except Exception:
                return ""
            return ""

        project_candidates.sort(key=created_key, reverse=True)
        code_candidates.sort(key=created_key, reverse=True)

        # Populate session result set
        self._last_candidate_set = project_candidates + code_candidates
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
            for i, f in enumerate(project_candidates, 1):
                try:
                    content = f.read_text(encoding="utf-8")
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        title = fm.get("source_title", "Untitled").replace(" (candidate)", "")
                        category = fm.get("category_guess", "other")
                        confidence = fm.get("confidence", 0.0)
                        evidence = fm.get("evidence", [])
                        evidence_str = f"from {evidence[0]}" if evidence else ""
                        lines.append(
                            f"{i}. {title} — {category} — confidence {int(confidence * 100)}% ({evidence_str})"
                        )
                except Exception:
                    lines.append(f"{i}. (error reading candidate)")

        if code_candidates:
            start_idx = len(project_candidates) + 1
            lines.append(f"\nCode repos ({len(code_candidates)}):")
            for i, f in enumerate(code_candidates, start_idx):
                try:
                    content = f.read_text(encoding="utf-8")
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        title = fm.get("source_title", "Untitled").replace(" (candidate)", "")
                        lines.append(f"{i}. {title} — awaiting confirmation")
                except Exception:
                    lines.append(f"{i}. (error reading candidate)")

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
            await update.message.reply_text(f"Error reading candidate: {e}")
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
                config = yaml.safe_load((BRAIN_DIR / "config.yaml").read_text())
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
                title = extracted.get("title", source_title.replace(" (candidate)", ""))
                await update.message.reply_text(f"Project confirmed: \"{title}\" [{category}]")
            except ValueError as e:
                await update.message.reply_text(f"Error: {e}")
            except Exception as e:
                log.exception("Error confirming project candidate")
                await update.message.reply_text(f"Error confirming candidate: {e}")

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

                # Delete candidate
                path.unlink()

                await update.message.reply_text(f"Repo confirmed: \"{name}\" added to code index")

            except Exception as e:
                log.exception("Error confirming code_repo candidate")
                await update.message.reply_text(f"Error confirming repo: {e}")

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
            await update.message.reply_text(f"Error reading candidate: {e}")
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
            await update.message.reply_text(f"Error saving rejected list: {e}")
            return

        # Delete candidate
        try:
            path.unlink()
            await update.message.reply_text(f"Rejected: \"{source_title}\"")
        except Exception as e:
            await update.message.reply_text(f"Error deleting candidate: {e}")

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
            await update.message.reply_text(f"Error reading candidate: {e}")
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
            await update.message.reply_text(f"Updated {key} → {value} on candidate \"{source_title}\"")
        except Exception as e:
            log.exception("Error editing candidate")
            await update.message.reply_text(f"Error editing candidate: {e}")

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

    async def cmd_remember(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Fetch a URL and save a reading memory: /remember <url>"""
        if not self._check_auth(update):
            return
        if not context.args or not context.args[0].startswith("http"):
            await update.message.reply_text(
                "Usage: /remember <url>\nExample: /remember https://example.com/article"
            )
            return

        url = context.args[0]
        await update.message.reply_text(f"📥 Fetching {url[:60]}...")

        try:
            from skill_router import detect_content_type, SKILL_REGISTRY
            from memory_writer import MemoryWriter

            title, content = await fetch_url_content(url)
            if not content:
                await update.message.reply_text(
                    "Could not fetch content from that URL. "
                    "The page may require JavaScript or block bots."
                )
                return

            content_type = detect_content_type(url=url, content=content[:3000])
            skill_name = SKILL_REGISTRY.get(content_type, "summarize-webpage")
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
            filename = await MemoryWriter().write(entry, memory_body)
            preview = memory_body[:300].replace("\n", " ")
            await self._send_reply(
                update,
                f"✅ Saved: {title or url}\n→ {filename}\n\n{preview}…"
            )
        except Exception as e:
            log.exception("cmd_remember failed for %s", url)
            await update.message.reply_text(f"Remember failed: {e}")

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
        except Exception as e:
            log.exception("cmd_note failed for %s", url)
            await update.message.reply_text(f"Note failed: {e}")

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

    # ── /code command ─────────────────────────────────────────────────────────

    def _resolve_code_index(self, n: str):
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_code_set):
            return self._last_code_set[idx]
        return None

    def _list_code_text(self, limit: int = 50) -> str:
        """Return formatted code repos list text (called by cmd_code and tool dispatch)."""
        limit = max(1, min(limit, 100))
        files = list((BRAIN_DIR / "memories").glob("code-*.md"))
        # Also include legacy project-*.md files that are type:code or type:project+category:code
        for f in (BRAIN_DIR / "memories").glob("project-*.md"):
            fm = self._parse_frontmatter(f)
            if fm.get("type") == "code" or (fm.get("type") == "project" and fm.get("category") == "code"):
                files.append(f)

        code_repos = []
        for f in files:
            fm = self._parse_frontmatter(f)
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

        text = self._list_code_text(limit)
        await update.message.reply_text(text)

    async def _cmd_code_detail(self, update: Update, index_str: str):
        """Show detail for code repo N."""
        path = self._resolve_code_index(index_str)
        if path is None:
            await update.message.reply_text(self._format_group_help("Knowledge listings", "code"))
            return

        # Find all hosts that have the same base repo
        # Extract base name from the selected file
        fm = self._parse_frontmatter(path)
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
        for f in (BRAIN_DIR / "memories").glob("code-*.md"):
            f_fm = self._parse_frontmatter(f)
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
        for f in (BRAIN_DIR / "memories").glob("project-*.md"):
            f_fm = self._parse_frontmatter(f)
            # Only include if type:code or type:project+category:code
            if f_fm.get("type") not in ("code", "project", "code_project"):
                continue
            if f_fm.get("type") == "project" and f_fm.get("category") != "code":
                continue
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

    def _list_events_text(self, limit: int = 20, calendar_filter=None) -> str:
        """Return formatted events list text (called by cmd_events and tool dispatch)."""
        limit = max(1, min(limit, 100))
        files = list((BRAIN_DIR / "memories").glob("calendar-event-*.md"))
        all_events = []
        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "calendar_event":
                continue
            all_events.append((f, fm))

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
        text = self._list_events_text(limit, calendar_filter=calendar_filter)
        await update.message.reply_text(text)

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

        fm = self._parse_frontmatter(path)
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

    # ── /meetings and /meeting commands ──────────────────────────────────────

    def _resolve_meeting_index(self, n: str):
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_meeting_set):
            return self._last_meeting_set[idx]
        return None

    def _list_meetings_text(self, limit: int = 20) -> str:
        """Return formatted meetings list text (called by cmd_meetings and tool dispatch)."""
        limit = max(1, min(limit, 100))
        files = list((BRAIN_DIR / "memories").glob("meeting-*.md"))
        meetings = []
        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "meeting_transcript":
                continue
            meetings.append((f, fm))

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
        text = self._list_meetings_text(limit)
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

        fm = self._parse_frontmatter(path)
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

    def _resolve_feature_index(self, args, update: Update):
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
            for f in sorted(memories_dir.glob("feature-request-*.md")):
                fm = self._parse_frontmatter(f)
                if fm.get("short_id") == arg:
                    return f
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

    def _tags_from_labels(self, issue: dict) -> list[str]:
        reserved_prefixes = ("kind:", "status:", "priority:")
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

    async def _list_features_from_github(self, update, kind_filter, status_filter, show_all) -> None:
        state = "all" if show_all or status_filter in ("done", "wont-do") else "open"
        labels = []
        if kind_filter:
            labels.append(f"kind:{kind_filter}")
        if status_filter in ("planned", "in-progress"):
            labels.append(f"status:{status_filter}")
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
            created = issue.get("created_at", "")[:10]
            title = issue.get("title", "")[:60]
            kind_tag = f"[{kind[:4]}] " if not kind_filter else ""
            lines.append(f"{idx}. {kind_tag}[{status}] [{priority}] {title} ({created}) #{issue['number']}")
        await self._send_reply(update, "\n".join(lines))

    def _get_memory_text(self, name: str) -> str:
        """Search memories by title or filename and return full file contents.
        Called by tool dispatch for get_memory tool."""
        name_lower = name.lower()
        files = list((BRAIN_DIR / "memories").glob("*.md"))

        for f in files:
            # Check filename match
            if name_lower in f.stem.lower():
                text = _safe_read_text(f)
                return text if text is not None else ""
            # Check frontmatter source_title match
            fm = self._parse_frontmatter(f)
            source_title = (fm.get("source_title") or "").lower()
            if name_lower in source_title:
                text = _safe_read_text(f)
                return text if text is not None else ""

        return f"Memory not found: {name}"

    def _list_comms_text(self, kind: Optional[str] = None, limit: int = 20, show_all: bool = False) -> str:
        """Return formatted comms list text (called by cmd_comms and tool dispatch).
        kind: 'email' or 'slack' or None for both.
        show_all: if True, show all email threads including marketing/automated."""
        limit = max(1, min(limit, 100))
        type_map = {"email": "email_thread", "slack": "slack_thread"}
        wanted_types = {type_map[kind]} if kind else {"email_thread", "slack_thread"}

        comms = []
        for glob_pattern, mem_type in [("email-thread-*.md", "email_thread"), ("slack-thread-*.md", "slack_thread")]:
            if mem_type not in wanted_types:
                continue
            for f in (BRAIN_DIR / "memories").glob(glob_pattern):
                fm = self._parse_frontmatter(f)
                if fm.get("type") != mem_type:
                    continue

                # Filter email threads by classification unless show_all is True
                if mem_type == "email_thread" and not show_all:
                    classification = fm.get("classification", "human")
                    if classification in {"marketing", "automated"}:
                        continue

                comms.append((f, fm, mem_type))

        if not comms:
            msg = (f"No {kind} threads found." if kind
                   else "No communications found.")
            return msg

        def _sort_key(item):
            _, fm, mem_type = item
            if mem_type == "email_thread":
                return fm.get("last_message") or ""
            # For slack: use mtime of file as fallback
            return fm.get("last_reply") or fm.get("last_message") or ""

        comms.sort(key=_sort_key, reverse=True)
        comms = comms[:limit]
        self._last_comms_set = [f for f, _, _ in comms]
        self._active_list = self._last_comms_set

        lines = [f"Communications ({len(comms)} shown):"]
        for i, (_, fm, mem_type) in enumerate(comms, 1):
            source_tag = "[email]" if mem_type == "email_thread" else "[slack]"
            if mem_type == "email_thread":
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
            else:
                channel = fm.get("channel") or fm.get("source_title") or "(no channel)"
                opener = (fm.get("participants") or [""])[0]
                opener_str = f" — {str(opener)[:25]}" if opener else ""
                date = (fm.get("last_reply") or fm.get("created") or "")[:10]
                lines.append(f"{i}. {source_tag} #{channel[:30]}{opener_str} ({date})")
        lines.append("\nUse /comm <N> for details.")
        return "\n".join(lines)

    async def cmd_comms(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        args = list(context.args) if context.args else []
        type_filter = None
        limit = 10
        show_all = False

        # Parse: /comms [email|slack] [forget N...] [all] [N]
        if args and args[0].lower() in ("email", "slack"):
            type_filter = args[0].lower()
            args = args[1:]
        elif args and not args[0].isdigit() and args[0].lower() not in ("all", "forget"):
            await update.message.reply_text(
                "Usage: /comms [email|slack] [all] [N]\n"
                "Filter must be 'email' or 'slack'. Add 'all' to show marketing/automated emails."
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
            self._list_comms_text(type_filter, limit=50, show_all=True)
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

        text = self._list_comms_text(type_filter, limit, show_all)
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

        fm = self._parse_frontmatter(path)
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
        else:
            channel = fm.get("channel") or title
            date = (fm.get("last_reply") or fm.get("created") or "")[:10]
            lines = [
                f"[slack] #{channel}",
                f"Participants: {parts_str}",
                f"Last reply: {date}",
                "",
                summary,
            ]
        await update.message.reply_text("\n".join(lines))

    # ── Notification commands ─────────────────────────────────────────────────

    async def cmd_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        if self.notification_manager is None:
            await update.message.reply_text("Notification manager not available.")
            return

        # Assemble and send briefing without updating last_briefing_date
        briefing_text = self.notification_manager._assemble_briefing()
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
        # Extract #hashtags as tags
        tags = [t[1:].lower() for t in re.findall(r'#\w+', description)]
        clean_desc = re.sub(r'#\w+', '', description).strip()
        title = " ".join(clean_desc.split()[:8])  # first 8 words as title

        if self.github.enabled:
            await self._gh_ensure_labels()
            from datetime import datetime, timedelta
            body = (
                f"## Request\n\n{clean_desc}\n\n"
                f"## Context\n\nCaptured via /feature command at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\n"
                f"## Notes\n\n"
            )
            labels = ["kind:feature", "priority:medium"] + tags
            try:
                issue = await self.github.create_issue(title, body, labels)
            except Exception as e:
                await self._send_reply(update, f"GitHub error: {e}")
                return
            await self._rewrite_features_index_snapshot()
            await self._send_reply(update,
                f"Feature captured: '{clean_desc[:60]}' (#{issue['number']})\n{issue['html_url']}")
            return

        # --- local fallback ---
        import hashlib, os
        from datetime import datetime, timedelta
        memories_dir = BRAIN_DIR / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)

        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:40].strip('-')
        id_hash = hashlib.sha1(f"{description}{datetime.now().isoformat()}".encode()).hexdigest()[:6]
        filename = f"feature-request-{slug}-{id_hash}.md"

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
        body = f"## Request\n\n{clean_desc}\n\n## Context\n\nCaptured via /feature command at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\n## Notes\n\n"
        content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}"

        target = memories_dir / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content)
        os.rename(tmp, target)

        await self._send_reply(update, f"Feature captured: '{clean_desc[:60]}' (ID: {id_hash})\nUse /features to view all.")

    async def cmd_bug(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await self._send_reply(update, "Usage: /bug <description>")
            return

        description = " ".join(context.args)
        tags = [t[1:].lower() for t in re.findall(r'#\w+', description)]
        clean_desc = re.sub(r'#\w+', '', description).strip()
        title = " ".join(clean_desc.split()[:8])

        if self.github.enabled:
            await self._gh_ensure_labels()
            from datetime import datetime, timedelta
            body = (
                f"## Bug\n\n{clean_desc}\n\n"
                f"## Expected\n\n\n\n"
                f"## Actual\n\n\n\n"
                f"## Steps to reproduce\n\n\n\n"
                f"## Notes\n\nCaptured via /bug at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n"
            )
            labels = ["kind:bug", "priority:medium"] + tags
            try:
                issue = await self.github.create_issue(title, body, labels)
            except Exception as e:
                await self._send_reply(update, f"GitHub error: {e}")
                return
            await self._rewrite_features_index_snapshot()
            await self._send_reply(update,
                f"Bug captured: '{clean_desc[:60]}' (#{issue['number']})\n{issue['html_url']}")
            return

        # --- local fallback ---
        import hashlib, os
        from datetime import datetime, timedelta
        memories_dir = BRAIN_DIR / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:40].strip('-')
        id_hash = hashlib.sha1(f"{description}{datetime.now().isoformat()}".encode()).hexdigest()[:6]
        filename = f"feature-request-{slug}-{id_hash}.md"

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
        body = (
            f"## Bug\n\n{clean_desc}\n\n"
            f"## Expected\n\n\n\n"
            f"## Actual\n\n\n\n"
            f"## Steps to reproduce\n\n\n\n"
            f"## Notes\n\nCaptured via /bug at {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n"
        )
        content = f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n{body}"
        target = memories_dir / filename
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content)
        os.rename(tmp, target)
        await self._send_reply(update, f"Bug captured: '{clean_desc[:60]}' (ID: {id_hash})\nUse /features bug to view all.")

    async def cmd_bugs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Alias for /features bug."""
        context.args = ["bug"]
        await self.cmd_features(update, context)

    async def cmd_features(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        arg = context.args[0].lower() if context.args else None
        kind_filter: Optional[str] = None
        status_filter: Optional[str] = None
        show_all = False
        if arg in ("bug", "bugs"):
            kind_filter = "bug"
        elif arg in ("feature", "features"):
            kind_filter = "feature"
        elif arg == "all":
            show_all = True
        elif arg:
            status_filter = arg

        if self.github.enabled:
            await self._list_features_from_github(update, kind_filter, status_filter, show_all)
            return

        # --- local fallback ---
        memories_dir = BRAIN_DIR / "memories"
        files = sorted(memories_dir.glob("feature-request-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

        results = []
        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "feature_request":
                continue
            status = fm.get("status", "new")
            item_kind = fm.get("kind", "feature")
            # Kind filter
            if kind_filter and item_kind != kind_filter:
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
            kind_tag = f"[{item_kind[:4]}] " if not kind_filter else ""
            lines.append(f"{i}. {kind_tag}[{status}] [{priority}] {title} ({created})")

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

        target = self._resolve_feature_index(context.args, update)
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
        target = self._resolve_feature_index(context.args, update)
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
        target = self._resolve_feature_index(context.args, update)
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
        target = self._resolve_feature_index(context.args[:1], update)
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
        target = self._resolve_feature_index(context.args[:1], update)
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
        target = self._resolve_feature_index(context.args[:1], update)
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
        target = self._resolve_feature_index(context.args[:1], update)
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
        files = list(memories_dir.glob("feature-request-*.md"))
        to_import = []
        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "feature_request":
                continue
            if fm.get("github_issue_number"):
                continue  # already imported
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
        import os
        archive_dir = memories_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
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
            self._rewrite_feature_frontmatter(f, {"github_issue_number": issue["number"]})
            dest = archive_dir / f.name
            os.rename(f, dest)
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
            await update.message.reply_text(f"Could not read config: {e}")
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

        query = update.message.text
        chat_id = update.effective_chat.id
        log.info(f"Processing query: {query[:80]!r}")

        # Serialise per-chat to preserve turn ordering
        lock = self._chat_history_locks.setdefault(chat_id, asyncio.Lock())

        async with lock:
          try:
            history = self._chat_history.get(chat_id, [])

            memory_context = self._load_context(query, history)
            log.info(f"Context loaded: {len(memory_context)} chars")

            from chat_tools import TOOLS, dispatch as _tool_dispatch

            async def tool_dispatch(name: str, args: dict) -> str:
                return await _tool_dispatch(name, args, self)

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

            response = await self.executor.run_with_tools(
                inputs={"memory_context": memory_context, "user_query": query},
                tools=active_tools,
                tool_dispatch=tool_dispatch,
                history=history,
            )

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
                await update.message.reply_text(f"Sorry — processing failed: {e}")
            except (TimedOut, NetworkError) as reply_err:
                log.error("Couldn't send failure notice (network): %s", reply_err)
            except Exception:
                pass
            try:
                await update.message.set_reaction("❌")
            except Exception:
                pass
