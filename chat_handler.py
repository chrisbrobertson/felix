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
        self.app.add_handler(CommandHandler("purge", self.cmd_purge))
        self.app.add_handler(CommandHandler("purgeall", self.cmd_purgeall))
        self.app.add_handler(CommandHandler("memories", self.cmd_memories))
        self.app.add_handler(CommandHandler("search", self.cmd_search))
        self.app.add_handler(CommandHandler("memory", self.cmd_memory))
        self.app.add_handler(CommandHandler("delete", self.cmd_delete))
        self.app.add_handler(CommandHandler("commitments", self.cmd_commitments))
        self.app.add_handler(CommandHandler("complete", self.cmd_complete))
        self.app.add_handler(CommandHandler("dismiss", self.cmd_dismiss))
        # Cache: path -> (mtime, header_text). Invalidated when mtime changes.
        # Avoids reading every file on every chat message.
        self._header_cache: dict = {}
        # Last /memories or /search result set — used by /memory <N> and /delete <N>.
        self._last_results: list = []
        # Last /commitments result set — used by /complete <N> and /dismiss <N>.
        self._last_commitment_set: list = []

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

    def _load_context(self, query: str) -> str:
        """Load memory files into context with relevance sorting and hard char budget."""
        parts = []
        budget = MAX_CONTEXT_CHARS

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

    def _purge_domain(self, domain: str) -> int:
        """Delete all memory files whose source_url frontmatter contains domain.

        Returns the count of deleted files.
        """
        deleted = 0
        for f in (BRAIN_DIR / "memories").glob("*.md"):
            fm = self._parse_frontmatter(f)
            if domain in (fm.get("source_url") or ""):
                f.unlink()
                deleted += 1
        return deleted

    # ── Telegram slash commands ───────────────────────────────────────────────

    def _check_auth(self, update: Update) -> bool:
        if update.effective_user.id != self.allowed_user_id:
            log.warning(f"Ignored command from unauthorised user_id={update.effective_user.id}")
            return False
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

    async def cmd_purge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /purge <domain>")
            return
        domain = context.args[0].lower()
        count = self._purge_domain(domain)
        if count:
            await update.message.reply_text(f"Deleted {count} memories from {domain}.")
        else:
            await update.message.reply_text(f"No memories found for {domain}.")

    async def cmd_purgeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        config = yaml.safe_load((BRAIN_DIR / "config.yaml").read_text())
        domains = config.get("browser_watcher", {}).get("skip_domains", [])
        if not domains:
            await update.message.reply_text("Skip list is empty — nothing to purge.")
            return
        lines = ["Purge complete:"]
        for domain in domains:
            count = self._purge_domain(domain)
            lines.append(f"• {domain} — {count} deleted" if count else f"• {domain} — 0 found")
        await update.message.reply_text("\n".join(lines))

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
        """Convert 1-based index string to a Path from _last_results, or None."""
        try:
            idx = int(n) - 1
        except (ValueError, TypeError):
            return None
        if 0 <= idx < len(self._last_results):
            return self._last_results[idx]
        return None

    def _fmt_memory_line(self, i: int, fm: dict) -> str:
        title = (fm.get("source_title") or "(no title)")[:60]
        date = (fm.get("created") or "")[:10]
        return f"{i}. {title}  ({date})"

    # ── /memories command ─────────────────────────────────────────────────────

    async def cmd_memories(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        lines = [f"Your {len(files)} most recent memories:"]
        for i, f in enumerate(files, 1):
            fm = self._parse_frontmatter(f)
            lines.append(self._fmt_memory_line(i, fm))
        await update.message.reply_text("\n".join(lines))

    # ── /search command ───────────────────────────────────────────────────────

    async def cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /search <query>")
            return

        query = " ".join(context.args)
        files = list((BRAIN_DIR / "memories").glob("*.md"))

        scored = [
            (self._score_relevance(f, query), f.stat().st_mtime, f)
            for f in files
        ]
        matches = sorted(
            [(s, mt, f) for s, mt, f in scored if s > 0],
            key=lambda t: (t[0], t[1]),
            reverse=True
        )[:10]

        if not matches:
            await update.message.reply_text(f"No memories match '{query}'.")
            return

        self._last_results = [f for _, _, f in matches]
        lines = [f"Search results for \"{query}\":"]
        for i, (score, _, f) in enumerate(matches, 1):
            fm = self._parse_frontmatter(f)
            lines.append(self._fmt_memory_line(i, fm) + f" [score: {score}]")
        await update.message.reply_text("\n".join(lines))

    # ── /memory command ───────────────────────────────────────────────────────

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /memory <N>")
            return

        path = self._resolve_index(context.args[0])
        if path is None:
            await update.message.reply_text(
                "Invalid index. Run /memories or /search first."
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

    # ── /delete command ───────────────────────────────────────────────────────

    async def cmd_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /delete <N>")
            return

        path = self._resolve_index(context.args[0])
        if path is None:
            await update.message.reply_text(
                "Invalid index. Run /memories or /search first."
            )
            return

        fm = self._parse_frontmatter(path)
        title = fm.get("source_title") or path.name

        try:
            path.unlink()
        except FileNotFoundError:
            pass  # already gone

        # Remove from result list so subsequent indices still work
        try:
            self._last_results.remove(path)
        except ValueError:
            pass

        await update.message.reply_text(f"Deleted: {title}")

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
