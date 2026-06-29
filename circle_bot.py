"""
circle_bot.py — Per-circle Telegram bot handler and runner (read-only member view).

Phase C of the Circles feature. Each CircleBotHandler is bound to one circle's
iCloud folder and handles a small set of read-only commands for circle members.
It NEVER reads from MEMORIES_DIR — all file access is strictly scoped to the
injected circle_path.

CircleBotRunner wraps a python-telegram-bot Application and is used by daemon.py
to start one Telegram bot per circle. The /ask LLM command is deferred to a
future iteration.
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from telegram.ext import ApplicationBuilder, CommandHandler

from circle_ruleset import write_ruleset_yaml

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

    def __init__(
        self,
        circle_path: Path,
        display_name: str,
        members: list[dict],
        ruleset_path: Optional[Path] = None,
        invites_file: Optional[Path] = None,
        slug: str = "",
    ):
        """
        Args:
            circle_path: Absolute path to the circle's shared iCloud folder.
                         All reads are scoped to this directory.
            display_name: Human-readable circle name shown in /help.
            members: List of {'telegram_user_id': int, 'name': str} dicts.
                     Empty list → allow any user (obscurity mode).
            ruleset_path: Path to the circle's YAML ruleset file. Required for
                          /join to append new members.
            invites_file: Path to the circle-invites.json state file. Required
                          for /join to validate invite codes.
            slug: Circle slug (matches ruleset filename stem). Required for /join
                  to look up codes in the correct section of invites_file.
        """
        self._circle_path = circle_path
        self._display_name = display_name
        self._member_ids: set[int] = {
            int(m["telegram_user_id"])
            for m in members
            if isinstance(m.get("telegram_user_id"), (int, float))
        }
        self._ruleset_path = ruleset_path
        self._invites_file = invites_file
        self._slug = slug
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

    # ── /join invite flow (FR-6) ──────────────────────────────────────────────

    async def cmd_join(self, update, context):
        """
        /join <code> — Redeem a one-time invite code to join this circle.

        Intentionally skips _check_member: invitees are not members yet.
        Validates the code against invites_file, appends the user to the
        ruleset YAML, and consumes the code (single-use).
        """
        if not context.args:
            await update.message.reply_text("Usage: /join <invite-code>")
            return

        code = context.args[0].strip().lower()

        if not self._invites_file or not self._ruleset_path:
            await update.message.reply_text(
                "Circle configuration error — cannot process invite."
            )
            return

        # Load invites state
        invites_state: dict = {}
        if self._invites_file.exists():
            try:
                invites_state = json.loads(self._invites_file.read_text())
            except Exception:
                pass

        circle_invites: dict = invites_state.get(self._slug, {})
        invite_data = circle_invites.get(code)

        if not invite_data:
            await update.message.reply_text("Invalid or expired invite code.")
            return

        if invite_data.get("expires_at", 0) < time.time():
            del circle_invites[code]
            invites_state[self._slug] = circle_invites
            self._save_invites(invites_state)
            await update.message.reply_text("Invalid or expired invite code.")
            return

        user_id = update.effective_user.id
        first_name = (update.effective_user.first_name or "Member").strip()

        # Load ruleset and append new member (idempotent)
        try:
            ruleset_data = yaml.safe_load(
                self._ruleset_path.read_text(encoding="utf-8")
            ) or {}
        except Exception as e:
            log.error("circle-bot: failed to read ruleset for /join: %s", e)
            await update.message.reply_text(
                "Circle configuration error — please try again later."
            )
            return

        members = ruleset_data.get("members") or []
        if not any(m.get("telegram_user_id") == user_id for m in members):
            members.append({"telegram_user_id": user_id, "name": first_name})
        ruleset_data["members"] = members

        try:
            write_ruleset_yaml(self._ruleset_path, ruleset_data)
        except Exception as e:
            log.error("circle-bot: failed to save ruleset after /join: %s", e)
            await update.message.reply_text(
                "Failed to add you to the circle. Please try again."
            )
            return

        # Consume the invite code (one-time use)
        del circle_invites[code]
        invites_state[self._slug] = circle_invites
        self._save_invites(invites_state)

        # Update in-memory member set so new member can use commands immediately
        self._member_ids.add(user_id)

        await update.message.reply_text(
            f"Welcome to the {self._display_name} circle!"
        )

    def _save_invites(self, invites_state: dict) -> None:
        """Atomically save invites state to invites_file."""
        if not self._invites_file:
            return
        tmp_path = self._invites_file.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(invites_state, indent=2))
            os.rename(str(tmp_path), str(self._invites_file))
        except Exception as e:
            log.error("circle-bot: failed to save invites state: %s", e)
            try:
                tmp_path.unlink()
            except OSError:
                pass


class CircleBotRunner:
    """
    Wraps a python-telegram-bot Application with a CircleBotHandler.
    One instance per circle. Daemon.py creates and starts these at startup.

    Usage:
        runner = CircleBotRunner(ruleset, ruleset_path, icloud_root, invites_file)
        await runner.start()
        tasks.append(runner.poll_loop)   # add to asyncio.gather task list
        # on shutdown:
        await runner.stop()
    """

    def __init__(
        self,
        ruleset,
        ruleset_path: Path,
        icloud_root: Path,
        invites_file: Path,
    ):
        """
        Args:
            ruleset: CircleRuleset dataclass instance.
            ruleset_path: Actual path to the circle's YAML file on disk.
                          Passed directly to avoid slug→path reconstruction bugs
                          when the filename stem differs from the circle slug.
            icloud_root: Root iCloud Drive path for resolving icloud_folder.
            invites_file: Path to circle-invites.json (shared with host bot).
        """
        self._slug = ruleset.slug

        circle_path = icloud_root / ruleset.icloud_folder
        handler = CircleBotHandler(
            circle_path=circle_path,
            display_name=ruleset.display_name,
            members=ruleset.members,
            ruleset_path=ruleset_path,
            invites_file=invites_file,
            slug=ruleset.slug,
        )

        self.app = ApplicationBuilder().token(ruleset.bot_token).build()
        self.app.add_handler(CommandHandler("memories", handler.cmd_memories))
        self.app.add_handler(CommandHandler("search", handler.cmd_search))
        self.app.add_handler(CommandHandler("events", handler.cmd_events))
        self.app.add_handler(CommandHandler("commitments", handler.cmd_commitments))
        self.app.add_handler(CommandHandler("help", handler.cmd_help))
        self.app.add_handler(CommandHandler("join", handler.cmd_join))

    async def start(self) -> None:
        """Initialize and start polling. Call once before adding to task list."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("circle-bot: '%s' started polling", self._slug)

    async def stop(self) -> None:
        """Stop the bot gracefully. Call from the daemon's finally block."""
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as e:
            log.error("circle-bot: '%s' stop error: %s", self._slug, e)
        log.info("circle-bot: '%s' stopped", self._slug)

    async def poll_loop(self, stop_event: asyncio.Event) -> None:
        """Async task that holds open until the daemon stop_event fires."""
        await stop_event.wait()
