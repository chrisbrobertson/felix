import asyncio
import logging
import os
import re
import yaml
from pathlib import Path

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from skill_executor import SkillExecutor

log = logging.getLogger("chat-handler")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
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
        ("forget",         "Forget item N from your last list, or all captures from a domain"),
        ("people",         "List contacts (alias of /contacts)"),
        ("contacts",       "List people you've interacted with"),
        ("contact",        "Show contact by name or N"),
        ("projects",       "List code/work/person projects (optional category filter)"),
        ("project",        "Show project N from last list"),
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
    ],
}


class TelegramChatHandler:
    def __init__(self):
        config = yaml.safe_load((BRAIN_DIR / "config.yaml").read_text())
        self.token = config["telegram"]["bot_token"]
        self.allowed_user_id = int(config["user"]["telegram_user_id"])
        self.executor = SkillExecutor("chat")
        self.app = ApplicationBuilder().token(self.token).build()
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
        self.app.add_handler(CommandHandler("projects", self.cmd_projects))
        self.app.add_handler(CommandHandler("project", self.cmd_project))
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
        self.app.add_handler(CommandHandler("settings", self.cmd_settings))
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
        # Last /projects result set — used by /project <N>.
        self._last_project_set: list = []
        # Last /events result set — used by /event <N>.
        self._last_event_set: list = []
        # Last /meetings result set — used by /meeting <N>.
        self._last_meeting_set: list = []
        # Last /comms result set — used by /comm <N>.
        self._last_comms_set: list = []
        # Last /features result set
        self._last_feature_set: list = []
        # Last /skill-drafts result set
        self._last_skill_draft_set: list = []
        # Last /reports result set
        self._last_report_set: list = []
        # Notification manager reference (set by daemon.py)
        self.notification_manager = None
        # Skill creator and report scheduler (set by daemon.py)
        self.skill_creator = None
        self.report_scheduler = None

    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot polling started")

    async def stop(self):
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
        header = path.read_text()[:500]
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
        from datetime import datetime
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
        from datetime import datetime
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

    def _load_context(self, query: str) -> str:
        """Load memory files into context with relevance sorting and hard char budget."""
        parts = []
        budget = MAX_CONTEXT_CHARS

        # Always inject the full command list so the LLM knows what's available
        cmd_block = self._fmt_command_list()
        parts.append(cmd_block)
        budget -= len(cmd_block)

        index_path = BRAIN_DIR / "index.md"
        if index_path.exists():
            chunk = f"# Memory Index\n{index_path.read_text()}"
            parts.append(chunk)
            budget -= len(chunk)

        memory_files = list((BRAIN_DIR / "memories").glob("*.md"))

        # Score using cached headers — O(cache_size) not O(files * file_size)
        scored = sorted(
            memory_files,
            key=lambda p: (self._score_relevance(p, query), p.stat().st_mtime),
            reverse=True
        )

        for f in scored:
            if budget <= 0:
                log.debug(f"Context budget exhausted after {len(parts) - 1} memory files")
                break
            text = f.read_text()
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

    async def cmd_readings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        try:
            limit = int(context.args[0]) if context.args else 10
            limit = max(1, min(limit, 50))
        except (ValueError, IndexError):
            limit = 10

        files = list((BRAIN_DIR / "memories").glob("*.md"))
        if not files:
            await update.message.reply_text("No memories found.")
            return

        # Sort by mtime descending (fast — no file reads needed)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        files = files[:limit]
        self._last_results = files
        self._active_list = files

        lines = [f"Your {len(files)} most recent memories:"]
        for i, f in enumerate(files, 1):
            fm = self._parse_frontmatter(f)
            lines.append(self._fmt_memory_line(i, fm))
        await update.message.reply_text("\n".join(lines))

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
        type_filter: set | None = None
        filter_keyword: str | None = None
        if context.args[0].lower() in self._SEARCH_TYPE_FILTERS:
            if len(context.args) < 2:
                await update.message.reply_text(
                    f"Usage: /search {context.args[0].lower()} <query>"
                )
                return
            filter_keyword = context.args[0].lower()
            type_filter = self._SEARCH_TYPE_FILTERS[filter_keyword]
            query = " ".join(context.args[1:])
        else:
            query = " ".join(context.args)

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
            await update.message.reply_text(f"No memories match '{query}'.")
            return

        # Apply type filter if specified
        if type_filter is not None:
            def _matches_type(f):
                fm = self._parse_frontmatter(f)
                t = fm.get("type") or None
                return t in type_filter
            matches = [(s, mt, f) for s, mt, f in matches if _matches_type(f)]
            if not matches:
                await update.message.reply_text(
                    f"No {filter_keyword} memories match '{query}'."
                )
                return

        if type_filter is not None:
            # Flat list for filtered mode
            self._last_results = [f for _, _, f in matches]
            self._active_list = self._last_results
            lines = [f"Search results for \"{query}\" ({filter_keyword}) — {len(matches)} match{'es' if len(matches) != 1 else ''}:"]
            for i, (_, _, f) in enumerate(matches, 1):
                fm = self._parse_frontmatter(f)
                mem_type = fm.get("type") or None
                lines.append(self._fmt_search_line(i, fm, mem_type))
            lines.append("\nUse /reading N for detail on any item.")
            await self._send_reply(update, "\n".join(lines))
            return

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

        await self._send_reply(update, "\n".join(lines))

    # ── /reading command ──────────────────────────────────────────────────────

    async def cmd_reading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /reading <N>")
            return

        path = self._resolve_index(context.args[0])
        if path is None:
            await update.message.reply_text(
                "Invalid index. Run /readings or /search first."
            )
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

    async def cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text(
                "Usage:\n"
                "  /forget <N>        — forget item N from your last list\n"
                "  /forget <domain>   — forget all web captures from a domain"
            )
            return
        arg = context.args[0]
        if arg.isdigit():
            path = self._resolve_index(arg)
            if path is None:
                await update.message.reply_text("Invalid N. Run a list command first.")
                return
            fm = self._parse_frontmatter(path)
            title = fm.get("source_title") or fm.get("title") or path.name
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            try:
                self._active_list.remove(path)
            except ValueError:
                pass
            await update.message.reply_text(f"Forgotten: {title}")
            return
        # Non-numeric → domain purge
        domain = arg.lower()
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

    async def cmd_commitments(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        type_filter = context.args[0] if context.args else None
        items = self._load_active_commitments(type_filter)

        if not items:
            msg = (
                f"No active {type_filter} commitments."
                if type_filter
                else "No active commitments."
            )
            await update.message.reply_text(msg)
            self._last_commitment_set = []
            return

        self._last_commitment_set = [f for f, _ in items]
        self._active_list = self._last_commitment_set
        total = len(items)
        lines = [f"Active commitments ({total} total):"]

        for i, (_, fm) in enumerate(items[:20], 1):
            ct = fm.get("commitment_type", "outbound")
            desc = (fm.get("source_title") or fm.get("summary") or "")[:50]
            owner = fm.get("owner", "")
            due = fm.get("due_date")
            due_str = f" — due {due}" if due else " — due unknown"
            needs_review = "needs-review" in (fm.get("tags") or [])
            flag = " \u26a0\ufe0f" if needs_review else ""
            lines.append(f"{i}. [{ct}] {desc} — {owner}{due_str}{flag}")

        if total > 20:
            lines.append(f"... and {total - 20} more.")

        lines.append("\nUse /complete N or /dismiss N to update status.")
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
            await update.message.reply_text("Usage: /complete N")
            return

        path = self._resolve_commitment_index(context.args[0])
        if path is None:
            await update.message.reply_text("Invalid index. Run /commitments first.")
            return

        from commitment_tracker import CommitmentTracker
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title") or "commitment"
        owner = fm.get("owner", "")
        label = f'"{title}"' + (f" ({owner})" if owner else "")

        try:
            CommitmentTracker().update_commitment_status(path, "completed")
            await update.message.reply_text(f"\u2713 Marked complete: {label}")
        except Exception as e:
            await update.message.reply_text(f"Error updating commitment: {e}")

    async def cmd_dismiss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /dismiss N")
            return

        path = self._resolve_commitment_index(context.args[0])
        if path is None:
            await update.message.reply_text("Invalid index. Run /commitments first.")
            return

        from commitment_tracker import CommitmentTracker
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title") or "commitment"
        owner = fm.get("owner", "")
        label = f'"{title}"' + (f" ({owner})" if owner else "")

        try:
            CommitmentTracker().update_commitment_status(path, "dismissed")
            await update.message.reply_text(f"\u2717 Dismissed: {label}")
        except Exception as e:
            await update.message.reply_text(f"Error updating commitment: {e}")

    # ── /wrong command (FR-11) ────────────────────────────────────────────────

    async def cmd_wrong(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /wrong N")
            return

        path = self._resolve_commitment_index(context.args[0])
        if path is None:
            await update.message.reply_text("Invalid index. Run /commitments first.")
            return

        from commitment_tracker import (
            CommitmentTracker,
            CORRECTIONS_FILE,
            _record_false_positive,
        )
        import json
        from datetime import datetime

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
        from datetime import datetime

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

    # ── /contacts command ─────────────────────────────────────────────────────

    async def cmd_contacts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        try:
            limit = int(context.args[0]) if context.args else 20
            limit = max(1, min(limit, 50))
        except (ValueError, IndexError):
            limit = 20

        files = list((BRAIN_DIR / "memories").glob("contact-*.md"))
        if not files:
            await update.message.reply_text("No contacts found.")
            return

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
        # contacts are aggregated — /forget N not supported for this list

        total = len(list((BRAIN_DIR / "memories").glob("contact-*.md")))
        lines = [f"Contacts ({total} total):"]

        for i, (f, fm) in enumerate(contacts, 1):
            name = fm.get("name", "(no name)")
            last_interaction = (fm.get("last_interaction") or "")[:10]
            score = fm.get("relationship_score", 0.0)
            # Note: source types would require tracking, simplified for now
            lines.append(f"{i}. {name} — last: {last_interaction} — score: {score}")

        lines.append("\nUse /contact <name> or /contact <N> for details.")
        await update.message.reply_text("\n".join(lines))

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

    # ── /help command ─────────────────────────────────────────────────────────

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

    # ── /projects and /project commands ──────────────────────────────────────

    def _resolve_project_index(self, n: str):
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_project_set):
            return self._last_project_set[idx]
        return None

    async def cmd_projects(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        args = list(context.args) if context.args else []
        category_filter = None
        limit = 10

        # Parse: /projects [category] [N]
        if args and not args[0].isdigit():
            category_filter = args[0].lower()
            args = args[1:]
        if args:
            try:
                limit = max(1, min(int(args[0]), 50))
            except ValueError:
                pass

        files = list((BRAIN_DIR / "memories").glob("project-*.md"))
        projects = []
        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") not in ("project", "code_project"):
                continue
            if category_filter and fm.get("category", "code") != category_filter:
                continue
            projects.append((f, fm))

        if not projects:
            msg = (f"No {category_filter} projects found." if category_filter
                   else "No projects found.")
            await update.message.reply_text(msg)
            return

        projects.sort(key=lambda x: x[1].get("last_scanned") or "", reverse=True)
        projects = projects[:limit]
        self._last_project_set = [f for f, _ in projects]
        # projects are aggregated — /forget N not supported for this list

        lines = [f"Projects ({len(projects)} shown):"]
        for i, (_, fm) in enumerate(projects, 1):
            name = fm.get("source_title") or "(no name)"
            cat = fm.get("category") or fm.get("type", "code").replace("_project", "")
            last = (fm.get("last_scanned") or "")[:10]
            lines.append(f"{i}. {name} [{cat}] ({last})")
        lines.append("\nUse /project <N> for details.")
        await update.message.reply_text("\n".join(lines))

    async def cmd_project(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /project <N>")
            return

        path = self._resolve_project_index(context.args[0])
        if path is None:
            await update.message.reply_text("Invalid index. Run /projects first.")
            return

        fm = self._parse_frontmatter(path)
        name = fm.get("source_title") or "(no name)"
        url = fm.get("source_url") or ""
        local = fm.get("local_path") or ""
        langs = ", ".join(fm.get("languages") or []) or "unknown"
        last = (fm.get("last_scanned") or "")[:10]
        summary = fm.get("summary") or ""
        tags = fm.get("tags") or []
        tag_str = f"\nTags: {', '.join(tags)}" if tags else ""
        cat = fm.get("category") or "code"

        lines = [
            f"{name} [{cat}]",
            url,
            f"Local: {local}",
            f"Languages: {langs}",
            f"Last scanned: {last}{tag_str}",
            "",
            summary,
        ]
        await update.message.reply_text("\n".join(lines))

    # ── /events and /event commands ───────────────────────────────────────────

    def _resolve_event_index(self, n: str):
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_event_set):
            return self._last_event_set[idx]
        return None

    async def cmd_events(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        try:
            limit = int(context.args[0]) if context.args else 10
            limit = max(1, min(limit, 50))
        except (ValueError, IndexError):
            limit = 10

        files = list((BRAIN_DIR / "memories").glob("calendar-event-*.md"))
        events = []
        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "calendar_event":
                continue
            events.append((f, fm))

        if not events:
            await update.message.reply_text("No calendar events found.")
            return

        events.sort(key=lambda x: x[1].get("start_time") or "", reverse=False)
        events = events[:limit]
        self._last_event_set = [f for f, _ in events]
        self._active_list = self._last_event_set

        lines = [f"Calendar events ({len(events)} shown):"]
        for i, (_, fm) in enumerate(events, 1):
            title = (fm.get("source_title") or "(no title)")[:50]
            start = self._fmt_datetime(fm.get("start_time") or "")
            duration = self._fmt_duration(fm.get("start_time"), fm.get("end_time"))
            dur_str = f" ({duration})" if duration else ""
            location = fm.get("location") or ""
            loc_str = f" — {location[:30]}" if location else ""
            lines.append(f"{i}. {start}{dur_str} {title}{loc_str}")
        lines.append("\nUse /event <N> for details.")
        await update.message.reply_text("\n".join(lines))

    async def cmd_event(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /event <N>")
            return

        path = self._resolve_event_index(context.args[0])
        if path is None:
            await update.message.reply_text("Invalid index. Run /events first.")
            return

        fm = self._parse_frontmatter(path)
        title = fm.get("source_title") or "(no title)"
        start = fm.get("start_time") or ""
        end = fm.get("end_time") or ""
        all_day = fm.get("all_day", False)
        location = fm.get("location") or ""
        cal = fm.get("calendar_name") or ""
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

    async def cmd_meetings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        try:
            limit = int(context.args[0]) if context.args else 10
            limit = max(1, min(limit, 50))
        except (ValueError, IndexError):
            limit = 10

        files = list((BRAIN_DIR / "memories").glob("meeting-*.md"))
        meetings = []
        for f in files:
            fm = self._parse_frontmatter(f)
            if fm.get("type") != "meeting_transcript":
                continue
            meetings.append((f, fm))

        if not meetings:
            await update.message.reply_text("No meeting transcripts found.")
            return

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
        await update.message.reply_text("\n".join(lines))

    async def cmd_meeting(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /meeting <N>")
            return

        path = self._resolve_meeting_index(context.args[0])
        if path is None:
            await update.message.reply_text("Invalid index. Run /meetings first.")
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
        """Convert 1-based index from args to a Path from _last_feature_set, or None with error reply."""
        if not args:
            return None
        try:
            n = int(args[0])
            idx = n - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_feature_set):
            return self._last_feature_set[idx]
        return None

    def _rewrite_feature_frontmatter(self, path: Path, updates: dict):
        """Update specific frontmatter keys in a feature request file. Preserves body."""
        import os
        text = path.read_text()
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
        from datetime import datetime
        text = path.read_text()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        note_line = f"- {timestamp}: {note}\n"
        if "## Notes" in text:
            text = text.replace("## Notes\n", f"## Notes\n{note_line}", 1)
        else:
            text += f"\n## Notes\n{note_line}"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.rename(tmp, path)

    async def cmd_comms(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return

        args = list(context.args) if context.args else []
        type_filter = None
        limit = 10

        # Parse: /comms [email|slack] [N]
        if args and args[0].lower() in ("email", "slack"):
            type_filter = args[0].lower()
            args = args[1:]
        elif args and not args[0].isdigit():
            await update.message.reply_text(
                "Usage: /comms [email|slack] [N]\n"
                "Filter must be 'email' or 'slack'. Example: /comms email 5"
            )
            return

        if args:
            try:
                limit = max(1, min(int(args[0]), 50))
            except ValueError:
                pass

        type_map = {"email": "email_thread", "slack": "slack_thread"}
        wanted_types = {type_map[type_filter]} if type_filter else {"email_thread", "slack_thread"}

        comms = []
        for glob_pattern, mem_type in [("email-thread-*.md", "email_thread"), ("slack-thread-*.md", "slack_thread")]:
            if mem_type not in wanted_types:
                continue
            for f in (BRAIN_DIR / "memories").glob(glob_pattern):
                fm = self._parse_frontmatter(f)
                if fm.get("type") != mem_type:
                    continue
                comms.append((f, fm, mem_type))

        if not comms:
            msg = (f"No {type_filter} threads found." if type_filter
                   else "No communications found.")
            await update.message.reply_text(msg)
            return

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
                lines.append(f"{i}. {source_tag} {subject}{sender_str} ({date})")
            else:
                channel = fm.get("channel") or fm.get("source_title") or "(no channel)"
                opener = (fm.get("participants") or [""])[0]
                opener_str = f" — {str(opener)[:25]}" if opener else ""
                date = (fm.get("last_reply") or fm.get("created") or "")[:10]
                lines.append(f"{i}. {source_tag} #{channel[:30]}{opener_str} ({date})")
        lines.append("\nUse /comm <N> for details.")
        await update.message.reply_text("\n".join(lines))

    async def cmd_comm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /comm <N>")
            return

        path = self._resolve_comm_index(context.args[0])
        if path is None:
            await update.message.reply_text("Invalid index. Run /comms first.")
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

        # Write memory file
        import hashlib, os
        from datetime import datetime
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

        import hashlib, os
        from datetime import datetime
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
        kind_filter: str | None = None
        status_filter: str | None = None
        show_all = False
        if arg in ("bug", "bugs"):
            kind_filter = "bug"
        elif arg in ("feature", "features"):
            kind_filter = "feature"
        elif arg == "all":
            show_all = True
        elif arg:
            status_filter = arg

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
        path = self._resolve_feature_index(context.args, update)
        if path is None:
            await self._send_reply(update, "Invalid N. Run /features first.")
            return

        fm = self._parse_frontmatter(path)
        text = path.read_text()
        # Strip frontmatter for body
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else text

        lines = [
            f"**{fm.get('title', path.stem)}**",
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
        path = self._resolve_feature_index(context.args, update)
        if path is None:
            await self._send_reply(update, "Invalid N. Run /features first.")
            return
        fm = self._parse_frontmatter(path)
        self._rewrite_feature_frontmatter(path, {"status": "planned"})
        await self._send_reply(update, f"Feature '{fm.get('title','?')[:50]}' marked as planned.")

    async def cmd_feature_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        path = self._resolve_feature_index(context.args, update)
        if path is None:
            await self._send_reply(update, "Invalid N. Run /features first.")
            return
        fm = self._parse_frontmatter(path)
        self._rewrite_feature_frontmatter(path, {"status": "in-progress"})
        await self._send_reply(update, f"Feature '{fm.get('title','?')[:50]}' is now in progress.")

    async def cmd_feature_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        path = self._resolve_feature_index(context.args[:1], update)
        if path is None:
            await self._send_reply(update, "Invalid N. Run /features first.")
            return
        fm = self._parse_frontmatter(path)
        note = " ".join(context.args[1:]) if len(context.args) > 1 else None
        self._rewrite_feature_frontmatter(path, {"status": "done"})
        if note:
            self._append_feature_note(path, note)
        await self._send_reply(update, f"Feature '{fm.get('title','?')[:50]}' marked as done.")

    async def cmd_feature_wont_do(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        path = self._resolve_feature_index(context.args[:1], update)
        if path is None:
            await self._send_reply(update, "Invalid N. Run /features first.")
            return
        fm = self._parse_frontmatter(path)
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
        self._rewrite_feature_frontmatter(path, {"status": "wont-do"})
        if reason:
            self._append_feature_note(path, f"Won't do: {reason}")
        await self._send_reply(update, f"Feature '{fm.get('title','?')[:50]}' marked as won't do.")

    async def cmd_feature_priority(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await self._send_reply(update, "Usage: /feature-priority <N> <low|medium|high|critical>")
            return
        path = self._resolve_feature_index(context.args[:1], update)
        if path is None:
            await self._send_reply(update, "Invalid N. Run /features first.")
            return
        priority = context.args[1].lower()
        if priority not in ("low", "medium", "high", "critical"):
            await self._send_reply(update, "Priority must be: low, medium, high, or critical")
            return
        fm = self._parse_frontmatter(path)
        self._rewrite_feature_frontmatter(path, {"priority": priority})
        await self._send_reply(update, f"Priority updated: '{fm.get('title','?')[:50]}' is now {priority}.")

    async def cmd_feature_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if len(context.args) < 2:
            await self._send_reply(update, "Usage: /feature-note <N> <text>")
            return
        path = self._resolve_feature_index(context.args[:1], update)
        if path is None:
            await self._send_reply(update, "Invalid N. Run /features first.")
            return
        fm = self._parse_frontmatter(path)
        note = " ".join(context.args[1:])
        self._append_feature_note(path, note)
        await self._send_reply(update, f"Note added to '{fm.get('title','?')[:50]}'.")

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
        try:
            n = int(context.args[0])
            draft = self._last_skill_draft_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, "Invalid N. Run /skill-drafts first.")
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
            await self._send_reply(update, "Invalid N. Run /skill-drafts first.")
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
            await self._send_reply(update, "Invalid N. Run /skill-drafts first.")
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
        try:
            n = int(context.args[0])
            r = self._last_report_set[n - 1]
        except (ValueError, IndexError):
            await self._send_reply(update, "Invalid N. Run /reports first.")
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
            await self._send_reply(update, "Invalid N. Run /reports first.")
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
            await self._send_reply(update, "Invalid N. Run /reports first.")
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
            await self._send_reply(update, "Invalid N. Run /reports first.")
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
            await self._send_reply(update, "Invalid N. Run /reports first.")
            return
        chat_id = update.effective_chat.id
        await self._send_reply(update, f"Running report '{r['name']}'...")
        try:
            await self.report_scheduler.trigger_report(r["name"], r, chat_id)
        except Exception as e:
            await self._send_reply(update, f"Report failed: {e}")

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

    async def _send_reply(self, update: Update, text: str):
        """Chunk response into ≤4096-char messages to respect Telegram's hard limit."""
        if not text:
            await update.message.reply_text("No response generated.")
            return
        for i in range(0, len(text), TG_MAX_CHARS):
            await update.message.reply_text(text[i:i + TG_MAX_CHARS])

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        log.info(f"Message received from user_id={user_id}")

        if user_id != self.allowed_user_id:
            log.warning(f"Ignored message from unauthorised user_id={user_id} (allowed={self.allowed_user_id})")
            return

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
                return
            # Handle the structured reply
            await self._handle_missed_reply(update, context)
            return

        query = update.message.text
        log.info(f"Processing query: {query[:80]!r}")

        memory_context = self._load_context(query)
        log.info(f"Context loaded: {len(memory_context)} chars")

        response = await self.executor.run({
            "memory_context": memory_context,
            "user_query": query
        })

        log.info(f"Response: {len(response) if response else 0} chars")
        await self._send_reply(update, response)
