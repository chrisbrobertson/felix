"""Transport-agnostic command router and registry.

This module defines:
- COMMAND_REGISTRY: single source of truth for all commands (previously in chat_handler.py)
- CommandRouter: routes CommandContext to registered command handlers

Phase 0: CommandRouter is a thin routing layer. Command handlers are registered
by TelegramChatHandler at startup. Subsequent phases will migrate handlers here.
"""
import logging
from typing import Awaitable, Callable, Dict, Optional

from transport import CommandContext

log = logging.getLogger("command-core")


# ── Command Registry ──────────────────────────────────────────────────────────

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
        ("aichat",         "Browse imported Claude/ChatGPT history. /aichat | /aichat <N> | /aichat search <q>"),
        ("messages",       "Alias of /comms"),
        ("communications", "Alias of /comms"),
        ("message",        "Alias of /comm"),
        ("communication",  "Alias of /comm"),
        ("insights",       "List recent synthesis insights across memories"),
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
    "Deduplication": [
        ("dupes",    "List candidate duplicate memories"),
        ("merge",    "Merge duplicate pair N into one memory"),
        ("keep",     "Dismiss duplicate pair N as intentionally distinct"),
    ],
    "Watchlists": [
        ("watch",    "Create watchlist: /watch \"topic\" [from:person] [type:email|slack|meeting]"),
        ("watches",  "List active watchlists"),
        ("unwatch",  "Deactivate watchlist N"),
    ],
    "Import": [
        ("import_chats", "Import ChatGPT or Claude conversation export (attach ZIP or JSON file)"),
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
    "Circles": [
        ("circles",       "List configured circles with member count and last-sync time"),
        ("circle",        "Show detail for circle N from last /circles list"),
        ("circle_status", "Quick health check: which circles are syncing, which have missing iCloud folders"),
    ],
    "Meta": [
        ("help",     "Show this list"),
        ("commands", "Alias of /help"),
        ("settings", "View or change display settings (date_format, timezone)"),
        ("reset",    "Clear conversation history (history is lost on daemon restart anyway)"),
        ("deliver",  "Send queued replies that couldn't be delivered due to network issues"),
        ("discard",  "Drop queued replies that couldn't be delivered due to network issues"),
        ("version",  "Show the running daemon version"),
    ],
    "System": [
        ("backfill", "Reprocess historical data: /backfill <type> [days] [host]. Types: readings, email, zoom, calendar, slack, projects"),
        ("remember", "Fetch a URL and save a reading memory: /remember <url>"),
        ("deepen",   "Re-process reading N with deep analysis: /deepen N"),
        ("note",     "Fetch a URL and save detailed study notes: /note <url>"),
    ],
}


# ── CommandRouter ─────────────────────────────────────────────────────────────

class CommandRouter:
    """Transport-agnostic command dispatcher.

    In Phase 0, TelegramChatHandler registers its cmd_* methods here.
    In Phase 3, those methods are migrated directly into CommandRouter.

    Usage::
        router = CommandRouter()
        router.register("search", handler_fn)  # handler_fn(ctx: CommandContext)
        await router.dispatch_command(ctx, "search")
    """

    def __init__(self):
        self._cmd_handlers: Dict[str, Callable[[CommandContext], Awaitable[None]]] = {}

    def register(self, name: str, handler: Callable[[CommandContext], Awaitable[None]]) -> None:
        """Register a command handler.  ``name`` is case-insensitive."""
        self._cmd_handlers[name.lower()] = handler

    def register_all(self, mapping: Dict[str, Callable]) -> None:
        """Register multiple command handlers at once."""
        for name, handler in mapping.items():
            self.register(name, handler)

    async def dispatch_command(self, ctx: CommandContext, command: str) -> bool:
        """Route *command* to its registered handler.

        Returns True if the command was handled, False if unknown.
        The caller is responsible for sending an "unknown command" reply when False.
        """
        handler = self._cmd_handlers.get(command.lower())
        if handler is None:
            log.debug("CommandRouter: no handler for %r", command)
            return False
        try:
            await handler(ctx)
        except Exception:
            log.exception("CommandRouter: error in handler for %r", command)
            await ctx.reply(f"Internal error running /{command}. Please try again.")
        return True

    async def handle_message(self, ctx: CommandContext, text: str) -> None:
        """Handle free-text (non-command) message via LLM chat.

        Phase 0 stub — Phase 3 will migrate the full LLM-chat loop here.
        """
        # Delegate to the registered "__message__" handler if available.
        handler = self._cmd_handlers.get("__message__")
        if handler is not None:
            await handler(ctx)

    def format_help(self, use_markdown: bool = False) -> str:
        """Render COMMAND_REGISTRY as plain text or Markdown."""
        lines = []
        for group, commands in COMMAND_REGISTRY.items():
            if use_markdown:
                lines.append(f"*{group}*")
            else:
                lines.append(f"{group}")
            for cmd, desc in commands:
                prefix = "/" if not use_markdown else "/"
                lines.append(f"  {prefix}{cmd} — {desc}")
            lines.append("")
        return "\n".join(lines).rstrip()
