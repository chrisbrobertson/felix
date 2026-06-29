"""
circle_bot.py — Per-circle Telegram bot handler (read-only member view).

Phase C of the Circles feature. Each CircleBotHandler is bound to one circle's
iCloud folder and handles a small set of read-only commands for circle members.
It NEVER reads from MEMORIES_DIR — all file access is strictly scoped to the
injected circle_path.

Daemon wiring (per-circle Application startup) is deferred to a later iteration.
The /ask LLM command and invite flow (FR-6) are also deferred.
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("circle-bot")

MAX_LIST = 20
DETAIL_HINT = "Use /memories <N> for full content."


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from markdown text. Returns {} on any error."""
    try:
        m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not m:
            return {}
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def _get_body(text: str) -> str:
    """Return the body of a markdown file (after the frontmatter block)."""
    m = re.match(r"^---\n.*?\n---\n?(.*)", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _score_keyword(query_words: set[str], fm: dict, body: str) -> int:
    """Simple keyword relevance score for search."""
    score = 0
    title = str(fm.get("source_title") or "").lower()
    summary = str(fm.get("summary") or "").lower()
    tags = " ".join(str(t) for t in (fm.get("tags") or [])).lower()
    for word in query_words:
        if word in title:
            score += 3
        if word in summary:
            score += 2
        if word in tags:
            score += 2
        if word in body.lower():
            score += 1
    return score


class CircleBotHandler:
    """
    Read-only Telegram command handler for a single circle.

    All file reads are restricted to circle_path — MEMORIES_DIR is never
    accessed. This is the security invariant enforced by the injected path.

    Member enforcement: if members list is non-empty, any user whose
    telegram_user_id is not in the list receives "You are not a member of
    this circle." If members is empty, access is by bot-token obscurity
    (Phase B compatibility — host can create a circle with no members listed
    yet and still test the bot).
    """

    HELP_TEXT = (
        "Circle commands:\n"
        "  /memories [N] — browse recent memories (N for detail)\n"
        "  /search <query> — keyword search\n"
        "  /events [N] — calendar events (N for detail)\n"
        "  /commitments — active commitments\n"
        "  /help — this message"
    )

    def __init__(self, circle_path: Path, display_name: str, members: list[dict]):
        """
        Args:
            circle_path: Absolute path to the circle's shared iCloud folder.
                         All reads are scoped to this directory.
            display_name: Human-readable circle name shown in /help.
            members: List of {'telegram_user_id': int, 'name': str} dicts.
                     Empty list → allow any user (obscurity mode).
        """
        self._circle_path = circle_path
        self._display_name = display_name
        self._member_ids: set[int] = {
            int(m["telegram_user_id"])
            for m in members
            if isinstance(m.get("telegram_user_id"), (int, float))
        }
        # Last list populated by /memories or /events (for N-based detail)
        self._last_memory_list: list[dict] = []
        self._last_event_list: list[dict] = []

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _check_member(self, update) -> bool:
        """Return True if the sender is an allowed member (or members is empty)."""
        if not self._member_ids:
            return True
        return update.effective_user.id in self._member_ids

    def _deny(self, update):
        """Coroutine: send rejection message."""
        return update.message.reply_text("You are not a member of this circle.")

    # ── File loading ──────────────────────────────────────────────────────────

    def _load_files(self) -> list[dict]:
        """
        Read all .md files from circle_path.
        Returns list of {'filename', 'frontmatter', 'body'} dicts, sorted by
        filename descending (most-recent files first by YYYY-MM-DD prefix).
        """
        results = []
        if not self._circle_path.exists():
            return results
        for path in sorted(self._circle_path.glob("*.md"), reverse=True):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                fm = _parse_frontmatter(text)
                body = _get_body(text)
                results.append({"filename": path.name, "frontmatter": fm, "body": body})
            except Exception as e:
                log.debug("circle-bot: failed to read %s: %s", path.name, e)
        return results

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_help(self, update, context):
        if not self._check_member(update):
            await self._deny(update)
            return
        await update.message.reply_text(
            f"{self._display_name}\n\n{self.HELP_TEXT}"
        )

    async def cmd_memories(self, update, context):
        """List recent memories or show detail for memory N."""
        if not self._check_member(update):
            await self._deny(update)
            return

        args = list(context.args or [])
        # Detail view: /memories <N>
        if args and args[0].isdigit():
            n = int(args[0])
            if not self._last_memory_list or n < 1 or n > len(self._last_memory_list):
                await update.message.reply_text(
                    "Invalid index. Run /memories first."
                )
                return
            entry = self._last_memory_list[n - 1]
            fm = entry["frontmatter"]
            title = fm.get("source_title") or entry["filename"]
            summary = fm.get("summary") or entry["body"][:500]
            tags = ", ".join(str(t) for t in (fm.get("tags") or []))
            date = str(fm.get("date") or "")[:10]
            lines = [title]
            if date:
                lines.append(f"Date: {date}")
            if tags:
                lines.append(f"Tags: {tags}")
            lines.append("")
            lines.append(summary)
            await update.message.reply_text("\n".join(lines)[:4096])
            return

        # List view
        files = self._load_files()
        if not files:
            await update.message.reply_text("No memories in this circle yet.")
            return

        self._last_memory_list = files[:MAX_LIST]
        shown = self._last_memory_list
        lines = [f"Memories ({len(shown)} of {len(files)}):"]
        for i, entry in enumerate(shown, 1):
            fm = entry["frontmatter"]
            title = (fm.get("source_title") or entry["filename"])[:55]
            mem_type = fm.get("type") or ""
            date = str(fm.get("date") or entry["filename"][:10])[:10]
            type_label = f" [{mem_type}]" if mem_type else ""
            lines.append(f"{i}.{type_label} {title} — {date}")
        lines.append(f"\n{DETAIL_HINT}")
        await update.message.reply_text("\n".join(lines)[:4096])

    async def cmd_search(self, update, context):
        """Keyword search across circle memories."""
        if not self._check_member(update):
            await self._deny(update)
            return

        if not context.args:
            await update.message.reply_text("Usage: /search <query>")
            return

        query = " ".join(context.args).lower()
        query_words = set(query.split())
        files = self._load_files()
        if not files:
            await update.message.reply_text("No memories in this circle yet.")
            return

        scored = [
            (entry, _score_keyword(query_words, entry["frontmatter"], entry["body"]))
            for entry in files
        ]
        scored = [(e, s) for e, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)

        if not scored:
            await update.message.reply_text(f'No results for "{query}".')
            return

        top = scored[:MAX_LIST]
        self._last_memory_list = [e for e, _ in top]
        lines = [f'Search results for "{query}" ({len(scored)} found):']
        for i, (entry, score) in enumerate(top, 1):
            fm = entry["frontmatter"]
            title = (fm.get("source_title") or entry["filename"])[:55]
            mem_type = fm.get("type") or ""
            type_label = f" [{mem_type}]" if mem_type else ""
            lines.append(f"{i}.{type_label} {title}")
        lines.append(f"\n{DETAIL_HINT}")
        await update.message.reply_text("\n".join(lines)[:4096])

    async def cmd_events(self, update, context):
        """List calendar events from the circle folder, or show detail for event N."""
        if not self._check_member(update):
            await self._deny(update)
            return

        args = list(context.args or [])
        if args and args[0].isdigit():
            n = int(args[0])
            if not self._last_event_list or n < 1 or n > len(self._last_event_list):
                await update.message.reply_text(
                    "Invalid index. Run /events first."
                )
                return
            entry = self._last_event_list[n - 1]
            fm = entry["frontmatter"]
            title = fm.get("source_title") or entry["filename"]
            start = str(fm.get("start_time") or fm.get("date") or "")
            end = str(fm.get("end_time") or "")
            location = fm.get("location") or ""
            participants = fm.get("participants") or []
            summary = fm.get("summary") or entry["body"][:500]
            lines = [title]
            if start:
                time_str = f"{start}" + (f" – {end}" if end and end != start else "")
                lines.append(f"When: {time_str}")
            if location:
                lines.append(f"Where: {location}")
            if participants:
                lines.append(f"Attendees: {', '.join(str(p) for p in participants[:10])}")
            if summary:
                lines += ["", summary]
            await update.message.reply_text("\n".join(lines)[:4096])
            return

        # List view — only calendar_event type
        files = self._load_files()
        events = [
            f for f in files
            if f["frontmatter"].get("type") == "calendar_event"
        ]
        if not events:
            await update.message.reply_text("No calendar events in this circle.")
            return

        # Sort by start_time descending
        def _event_sort_key(e):
            fm = e["frontmatter"]
            return str(fm.get("start_time") or fm.get("date") or "")

        events.sort(key=_event_sort_key, reverse=True)
        self._last_event_list = events[:MAX_LIST]
        lines = [f"Events ({len(self._last_event_list)} of {len(events)}):"]
        for i, entry in enumerate(self._last_event_list, 1):
            fm = entry["frontmatter"]
            title = (fm.get("source_title") or entry["filename"])[:55]
            start = str(fm.get("start_time") or fm.get("date") or "")[:16]
            lines.append(f"{i}. {title} — {start}")
        lines.append("\nUse /events <N> for full detail.")
        await update.message.reply_text("\n".join(lines)[:4096])

    async def cmd_commitments(self, update, context):
        """List active commitments from the circle folder."""
        if not self._check_member(update):
            await self._deny(update)
            return

        files = self._load_files()
        commitments = [
            f for f in files
            if f["frontmatter"].get("type") == "commitment"
            and f["frontmatter"].get("status") not in ("completed", "dismissed")
        ]
        if not commitments:
            await update.message.reply_text("No active commitments in this circle.")
            return

        today = datetime.now().date()
        lines = [f"Commitments ({len(commitments)}):"]
        for i, entry in enumerate(commitments[:MAX_LIST], 1):
            fm = entry["frontmatter"]
            desc = (fm.get("source_title") or fm.get("summary") or "")[:50]
            due = fm.get("due_date")
            if due:
                try:
                    overdue = datetime.strptime(str(due), "%Y-%m-%d").date() < today
                    due_str = f" — was due {due} ⚠️" if overdue else f" — due {due}"
                except ValueError:
                    due_str = f" — due {due}"
            else:
                due_str = ""
            ct = fm.get("commitment_type") or "outbound"
            lines.append(f"{i}. [{ct}] {desc}{due_str}")

        if len(commitments) > MAX_LIST:
            lines.append(f"... and {len(commitments) - MAX_LIST} more.")
        await update.message.reply_text("\n".join(lines)[:4096])
