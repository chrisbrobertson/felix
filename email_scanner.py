import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from llm_routes import resolve

log = logging.getLogger("email-scanner")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "email-scanner-state.json"

# Seconds between 1970-01-01 and 2001-01-01 (Core Data epoch offset)
CORE_DATA_EPOCH_OFFSET = 978307200

def _applescript_escape(s: str) -> str:
    """Escape a string for safe interpolation into AppleScript string literals.

    AppleScript string literals use backslash escaping like C:
    - Backslash must be escaped first (to avoid double-escaping)
    - Double-quote must be escaped
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


# RE:/FW: prefix patterns to strip for subject normalization
_RE_FW_PATTERN = re.compile(
    r'^(re|fw|fwd|aw|r|sv|vs|antw)\s*:\s*',
    re.IGNORECASE
)

# Max threads per scan cycle (rate limiting)
MAX_THREADS_PER_CYCLE = 50

# Default excluded mailbox names
DEFAULT_SKIP_MAILBOXES = {
    "trash", "junk", "spam", "archive", "deleted messages",
    "deleted", "sent", "drafts",  # Sent/Drafts excluded from thread perspective
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _normalize_subject(subject: str) -> str:
    """Strip RE:/FW: prefixes until clean."""
    s = subject.strip()
    while True:
        m = _RE_FW_PATTERN.match(s)
        if not m:
            break
        s = s[m.end():].strip()
    return s


def _subject_to_conv_id(normalized: str) -> int:
    """Deterministic int conversation_id from normalized subject (AppleScript fallback)."""
    h = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    return int(h[:12], 16)  # 48-bit int, fits in a SQLite integer


def _mailbox_name_from_url(url: str) -> str:
    """Extract last path component from mailbox URL for skip-list matching."""
    if not url:
        return ""
    # mailbox://user@host/INBOX/Subfolder → "Subfolder"
    # mailbox://user@host/INBOX → "INBOX"
    return url.rstrip("/").split("/")[-1]


# ── Data source base ──────────────────────────────────────────────────────────

class MailDataSource:
    """Abstract base. Concrete subclasses: EnvelopeIndexSource, AppleScriptSource."""

    @classmethod
    def detect(cls):
        """Factory: return best available source, or None if nothing works."""
        src = EnvelopeIndexSource.create()
        if src:
            return src
        log.warning(
            "Envelope Index unavailable — falling back to AppleScript. "
            "For faster scanning, grant Full Disk Access in "
            "System Settings → Privacy & Security → Full Disk Access. "
            "See https://github.com/chrisbrobertson/felix#full-disk-access-for-email-scanner-sqlite-path "
            "for setup details."
        )
        return AppleScriptSource()

    def get_threads_since(self, since, excluded_mailboxes):
        raise NotImplementedError

    def get_threads_updated_since(self, since, high_water_rowid, excluded_mailboxes):
        raise NotImplementedError


# ── SQLite: Envelope Index ────────────────────────────────────────────────────

class EnvelopeIndexSource(MailDataSource):
    def __init__(self, db_path: Path):
        self._db_path = db_path

    @classmethod
    def _find_db_path(cls):
        candidates = sorted(
            Path.home().glob("Library/Mail/V*/Envelope Index"),
            key=lambda p: int(re.search(r'V(\d+)', str(p)).group(1)),
            reverse=True,
        )
        return candidates[0] if candidates else None

    @classmethod
    def create(cls):
        """Return an EnvelopeIndexSource if DB is accessible, else None."""
        path = cls._find_db_path()
        if not path:
            log.debug("No Envelope Index found")
            return None
        # Probe accessibility before copying
        try:
            path.stat()
        except PermissionError:
            log.warning(
                "Cannot read Envelope Index at %s — Full Disk Access required. "
                "Grant it in System Settings → Privacy & Security → Full Disk Access. "
                "See https://github.com/chrisbrobertson/felix#full-disk-access-for-email-scanner-sqlite-path "
                "for setup details.",
                path
            )
            return None
        except Exception as e:
            log.debug("Envelope Index not accessible: %s", e)
            return None
        return cls(path)

    def _copy_db(self) -> Path:
        """Copy to /tmp to avoid WAL lock issues while Mail.app is running."""
        tmp = Path("/tmp") / "second-brain-envelope-index"
        try:
            shutil.copy2(str(self._db_path), str(tmp))
            # Also copy WAL/SHM files if present
            for ext in ("-wal", "-shm"):
                src = self._db_path.with_name(self._db_path.name + ext)
                if src.exists():
                    shutil.copy2(str(src), str(tmp.with_name(tmp.name + ext)))
        except Exception as e:
            log.warning("Failed to copy Envelope Index: %s", e)
            raise
        return tmp

    def _convert_timestamp(self, core_data_ts) -> datetime:
        """Convert Core Data timestamp (seconds since 2001-01-01) to datetime."""
        if not core_data_ts:
            return datetime(2001, 1, 1)
        return datetime.utcfromtimestamp(float(core_data_ts) + CORE_DATA_EPOCH_OFFSET)

    def _dt_to_core_data(self, dt: datetime) -> float:
        """Convert datetime to Core Data timestamp for SQL WHERE clauses."""
        epoch = datetime(1970, 1, 1)
        unix_ts = (dt - epoch).total_seconds()
        return unix_ts - CORE_DATA_EPOCH_OFFSET

    def _query_messages(self, conn, where_clause: str, params: tuple) -> list:
        """Run the standard message join query with a custom WHERE clause."""
        sql = f"""
            SELECT m.conversation_id,
                   s.subject,
                   m.date_received,
                   m.date_sent,
                   m.snippet,
                   m.read,
                   m.flagged,
                   a.address  AS sender_address,
                   a.comment  AS sender_name,
                   mb.url     AS mailbox_url,
                   m.ROWID
            FROM messages m
            JOIN subjects  s  ON m.subject  = s.ROWID
            JOIN addresses a  ON m.sender   = a.ROWID
            JOIN mailboxes mb ON m.mailbox  = mb.ROWID
            WHERE {where_clause}
              AND m.deleted = 0
            ORDER BY m.conversation_id, m.date_received ASC
        """
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()
        except sqlite3.OperationalError as e:
            log.warning("Envelope Index query failed (schema may have changed): %s", e)
            return []

    def _rows_to_threads(self, rows, excluded_mailboxes: set) -> list:
        """Group raw DB rows into thread dicts, applying mailbox exclusion."""
        threads = {}
        max_rowid = 0
        for row in rows:
            (conv_id, subject, date_recv, date_sent, snippet,
             read_flag, flagged, sender_addr, sender_name, mb_url, rowid) = row

            mb_name = _mailbox_name_from_url(mb_url or "").lower()
            if mb_name in excluded_mailboxes:
                continue

            max_rowid = max(max_rowid, rowid or 0)
            recv_dt = self._convert_timestamp(date_recv)
            recv_str = recv_dt.strftime("%Y-%m-%dT%H:%M:%S")
            date_label = recv_dt.strftime("%Y-%m-%d")
            name = sender_name or sender_addr or "unknown"
            snippet_clean = (snippet or "").strip().replace("\n", " ")
            msg_line = f"{date_label} {name}: {snippet_clean}"

            if conv_id not in threads:
                threads[conv_id] = {
                    "conversation_id": conv_id,
                    "subject": _normalize_subject(subject or ""),
                    "raw_subject": subject or "",
                    "first_message": recv_str,
                    "last_message": recv_str,
                    "message_count": 0,
                    "participants": set(),
                    "messages": [],
                    "max_rowid": rowid or 0,
                }
            t = threads[conv_id]
            t["message_count"] += 1
            t["last_message"] = recv_str  # rows ordered ASC so last row = latest
            t["max_rowid"] = max(t["max_rowid"], rowid or 0)
            if sender_addr:
                name_clean = (sender_name or "").strip() or None
                addr_clean = sender_addr.lower()
                t["participants"].add((name_clean, addr_clean))
            if msg_line.strip():
                t["messages"].append(msg_line)

        # Finalize — match the AppleScript path shape (lines 431-434):
        # dict when display name present, bare string when not
        result = []
        for t in threads.values():
            t["participants"] = [
                {"name": n, "email": e} if n else e
                for (n, e) in sorted(t["participants"], key=lambda x: x[1])
            ]
            result.append(t)
        return result, max_rowid

    def get_threads_since(self, since: datetime, excluded_mailboxes: set):
        tmp = self._copy_db()
        try:
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            conn.row_factory = None
            cutoff = self._dt_to_core_data(since)
            rows = self._query_messages(conn, "m.date_received > ?", (cutoff,))
            conn.close()
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
        return self._rows_to_threads(rows, excluded_mailboxes)

    def get_threads_updated_since(self, since: datetime, high_water_rowid: int, excluded_mailboxes: set):
        tmp = self._copy_db()
        try:
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            cutoff = self._dt_to_core_data(since)
            rows = self._query_messages(
                conn,
                "m.ROWID > ? AND m.date_received > ?",
                (high_water_rowid, cutoff)
            )
            conn.close()
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
        return self._rows_to_threads(rows, excluded_mailboxes)


# ── AppleScript fallback ──────────────────────────────────────────────────────

class AppleScriptSource(MailDataSource):
    """Fallback when Envelope Index is unavailable (no FDA)."""

    # Limit per mailbox: fetch only the last N messages (by insertion order,
    # which approximates recency). Avoids a full-mailbox scan with `whose`.
    MAX_MESSAGES_PER_MAILBOX = 500

    def _run_osascript(self, script: str, timeout: int = 120) -> str:
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                # Do NOT call proc.wait() — osascript may not die if blocked
                # in a GUI syscall with Mail.app, causing an indefinite hang.
                log.warning("AppleScript timed out after %ds", timeout)
                return ""
            if proc.returncode != 0:
                log.warning("osascript error: %s", stderr.strip())
                return ""
            return stdout.strip()
        except Exception as e:
            log.warning("osascript failed: %s", e)
            return ""

    def _is_mail_running(self) -> bool:
        # Check without System Events — the daemon (launchd) lacks Automation
        # permission for System Events, so that approach always returns false.
        out = self._run_osascript('application "Mail" is running')
        return out.strip().lower() == "true"

    def _fetch_messages_raw(self, since: datetime, excluded_mailboxes: set) -> str:
        since_str = since.strftime("%m/%d/%Y %H:%M:%S")
        exclude_check = " and ".join(
            f'mbName is not "{_applescript_escape(mb.title())}"'
            for mb in sorted(excluded_mailboxes)[:8]  # AppleScript has string length limits
        ) or "true"

        # Only scan Inbox and Sent. Avoid `whose date received >= cutoff` —
        # that clause performs a full-mailbox scan (O(n)) in AppleScript and
        # will time out on large inboxes. Instead, slice the last
        # MAX_MESSAGES_PER_MAILBOX messages by insertion order (which
        # approximates recency) and filter by date inside the loop.
        max_msgs = self.MAX_MESSAGES_PER_MAILBOX
        script = f'''
if application "Mail" is not running then return ""
tell application "Mail"
    set cutoff to date "{since_str}"
    set output to ""
    repeat with acct in accounts
        repeat with mb in mailboxes of acct
            set mbName to name of mb
            if (mbName is "INBOX" or mbName is "Inbox" or mbName is "Sent" or mbName is "Sent Messages") and {exclude_check} then
                set allMsgs to every message of mb
                set msgCount to count of allMsgs
                if msgCount > 0 then
                set endIdx to {max_msgs}
                if endIdx > msgCount then set endIdx to msgCount
                set msgs to items 1 thru endIdx of allMsgs
                repeat with m in msgs
                    set d to date received of m
                    if d >= cutoff then
                        set mYear to (year of d) as string
                        set mMon to text -2 thru -1 of ("0" & ((month of d) as integer) as string)
                        set mDay to text -2 thru -1 of ("0" & (day of d) as string)
                        set t to time of d
                        set mHour to text -2 thru -1 of ("0" & ((t div 3600) as string))
                        set mMin to text -2 thru -1 of ("0" & (((t mod 3600) div 60) as string))
                        set mSec to text -2 thru -1 of ("0" & ((t mod 60) as string))
                        set mDate to mYear & "-" & mMon & "-" & mDay & "T" & mHour & ":" & mMin & ":" & mSec
                        set output to output & (subject of m) & "|||" & (sender of m) & "|||" & mDate & "|||" & (message id of m) & linefeed
                    end if
                end repeat
                end if
            end if
        end repeat
    end repeat
    return output
end tell
'''
        return self._run_osascript(script)

    def _parse_raw(self, raw: str, excluded_mailboxes: set):
        threads = {}
        max_rowid = 0
        for line in raw.splitlines():
            parts = line.split("|||")
            if len(parts) < 4:
                continue
            subject_raw = parts[0].strip()
            sender = parts[1].strip()
            date_str = parts[2].strip()
            msg_id = parts[3].strip()
            snippet = parts[4].strip() if len(parts) > 4 else ""

            normalized = _normalize_subject(subject_raw)
            conv_id = _subject_to_conv_id(normalized)

            try:
                recv_dt = datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                recv_dt = datetime.now()

            recv_str = recv_dt.strftime("%Y-%m-%dT%H:%M:%S")
            date_label = recv_dt.strftime("%Y-%m-%d")
            msg_line = f"{date_label} {sender}: {snippet[:150]}"

            if conv_id not in threads:
                threads[conv_id] = {
                    "conversation_id": conv_id,
                    "subject": normalized,
                    "raw_subject": subject_raw,
                    "first_message": recv_str,
                    "last_message": recv_str,
                    "message_count": 0,
                    "participants": set(),
                    "messages": [],
                    "max_rowid": 0,
                }
            t = threads[conv_id]
            t["message_count"] += 1
            if recv_str > t["last_message"]:
                t["last_message"] = recv_str
            if recv_str < t["first_message"]:
                t["first_message"] = recv_str
            # Parse "Name <email>" or bare email address
            m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', sender)
            if m:
                name = m.group(1).strip() or None
                addr = m.group(2).strip().lower()
            else:
                name = None
                addr = sender.strip().lower()
            t["participants"].add((name, addr))
            if msg_line.strip():
                t["messages"].append(msg_line)

        result = []
        for t in threads.values():
            t["participants"] = [
                {"name": n, "email": e} if n else e
                for (n, e) in sorted(t["participants"], key=lambda x: x[1])
            ]
            result.append(t)
        return result, max_rowid

    def get_threads_since(self, since: datetime, excluded_mailboxes: set):
        if not self._is_mail_running():
            log.warning("Mail.app is not running — skipping AppleScript scan")
            return [], 0
        # AppleScript iterates every message in every mailbox — scanning 30 days
        # across all folders times out on large mailboxes. Cap to 7 days and
        # Inbox/Sent only. The SQLite path (with FDA) does the full scan.
        as_cutoff = max(since, datetime.now() - timedelta(days=7))
        log.debug("AppleScript scan: cutoff %s", as_cutoff.strftime("%Y-%m-%d"))
        raw = self._fetch_messages_raw(as_cutoff, excluded_mailboxes)
        threads, max_rowid = self._parse_raw(raw, excluded_mailboxes)
        log.info("AppleScript scan: %d bytes raw, %d threads parsed", len(raw), len(threads))
        return threads, max_rowid

    def get_threads_updated_since(self, since: datetime, high_water_rowid: int, excluded_mailboxes: set):
        # AppleScript has no row-level ID; fall back to time-based query
        return self.get_threads_since(since, excluded_mailboxes)


# ── Email Scanner ─────────────────────────────────────────────────────────────

class EmailScanner:
    def __init__(self, role: str = "full"):
        self.role = role
        self._executor = None  # lazy LLM executor
        self.notification_callback = None  # Set by daemon.py for watchlist notifications

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

    def _scanner_config(self) -> dict:
        return self._load_config().get("email_scanner", {
            "classification_enabled": True,
        })

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {"high_water_rowid": 0, "last_scan_time": None, "data_source": None}

    def _save_state(self, state: dict):
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(STATE_FILE))
        except Exception as e:
            log.warning("Failed to save scanner state: %s", e)

    def _excluded_mailboxes(self, sc: dict) -> set:
        cfg_skip = sc.get("skip_mailboxes", [])
        return DEFAULT_SKIP_MAILBOXES | {m.lower() for m in cfg_skip}

    async def run_loop(self, stop_event: asyncio.Event):
        sc = self._scanner_config()
        interval = sc.get("interval_seconds", 300)
        log.info("Email scanner started — polling every %ds", interval)

        while not stop_event.is_set():
            try:
                await self._run_scan()
            except Exception:
                log.exception("Uncaught error in email scanner cycle")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _run_scan(self):
        sc = self._scanner_config()
        state = self._load_state()

        # Check for full_rescan flag
        full_rescan = sc.get("full_rescan", False)
        if full_rescan:
            log.info("Full rescan requested — resetting high-water mark")
            state["high_water_rowid"] = 0

        lookback_days = min(int(sc.get("initial_lookback_days", 30)), 90)
        since = datetime.now() - timedelta(days=lookback_days)
        excluded = self._excluded_mailboxes(sc)
        archive_days = int(sc.get("archive_after_days", 90))
        archive_cutoff = datetime.now() - timedelta(days=archive_days)

        source = self._get_data_source()
        if source is None:
            log.warning("No mail data source available — skipping scan")
            return

        high_water = state.get("high_water_rowid", 0)
        loop = asyncio.get_running_loop()
        if high_water > 0 and not full_rescan:
            # Run in executor: get_threads_updated_since calls osascript via
            # subprocess.Popen + communicate(), which is blocking. Running it
            # on the event loop thread triggers EDEADLK on macOS.
            threads, new_max_rowid = await loop.run_in_executor(
                None, source.get_threads_updated_since, since, high_water, excluded
            )
        else:
            threads, new_max_rowid = await loop.run_in_executor(
                None, source.get_threads_since, since, excluded
            )

        if not threads:
            log.debug("No new email threads to process")
        else:
            log.info("Processing %d email thread(s)", min(len(threads), MAX_THREADS_PER_CYCLE))

        processed = 0
        for thread in threads:
            if processed >= MAX_THREADS_PER_CYCLE:
                log.debug("Hit per-cycle limit of %d threads", MAX_THREADS_PER_CYCLE)
                break
            try:
                # Skip threads older than archive threshold
                try:
                    last_dt = datetime.fromisoformat(thread.get("last_message", ""))
                    if last_dt < archive_cutoff:
                        log.debug("Archiving stale thread: %s", thread.get("subject"))
                        continue
                except ValueError:
                    pass

                memory_path = self._memory_path(thread)
                if not self._needs_update(thread, memory_path):
                    continue

                summary, tags, classification = self._get_existing_summary_and_tags(memory_path, thread)
                if not summary or not tags:
                    summary, tags, classification = await self._generate_summary_and_tags(thread)
                if not summary:
                    summary = thread.get("subject", "")
                if not tags:
                    tags = self._tags_from_participants(thread)
                if not classification:
                    classification = "unknown"

                self._write_memory(thread, summary, tags, classification)
                processed += 1
            except Exception:
                log.exception("Error processing thread: %s", thread.get("subject"))

        # Update state
        if new_max_rowid > high_water:
            state["high_water_rowid"] = new_max_rowid
        state["last_scan_time"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        state["data_source"] = type(source).__name__

        # Clear full_rescan flag from config if it was set
        if full_rescan:
            self._clear_full_rescan_flag()

        self._save_state(state)

        if processed:
            log.info("Email scan complete — %d thread(s) updated", processed)

    def _get_data_source(self):
        return MailDataSource.detect()

    def _memory_path(self, thread: dict) -> Path:
        slug = self._slugify(thread.get("subject", "thread"))
        conv_id = thread.get("conversation_id", 0)
        return MEMORIES_DIR / f"email-thread-{slug}-{conv_id}.md"

    def _needs_update(self, thread: dict, memory_path: Path) -> bool:
        if not memory_path.exists():
            return True
        try:
            fm = _parse_frontmatter(memory_path.read_text())
            stored_count = fm.get("message_count", -1)
            stored_last = str(fm.get("last_message", ""))
            current_count = thread.get("message_count", 0)
            current_last = thread.get("last_message", "")
            return stored_count != current_count or stored_last != current_last
        except Exception:
            return True

    def _get_existing_summary_and_tags(self, memory_path: Path, thread: dict) -> tuple[str, list, str]:
        if not memory_path.exists():
            return None, None, None
        try:
            fm = _parse_frontmatter(memory_path.read_text())
            stored_count = fm.get("message_count", -1)
            current_count = thread.get("message_count", 0)
            if stored_count != current_count:
                return None, None, None  # New messages — regenerate
            summary = fm.get("summary", "")
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            classification = fm.get("classification", "unknown")
            return summary, tags, classification
        except Exception:
            return None, None, None

    async def _generate_summary_and_tags(self, thread: dict) -> tuple[str, list, str]:
        subject = thread.get("subject", "")
        participants = thread.get("participants", [])
        messages = thread.get("messages", [])

        # Build prompt content from snippets (no full bodies)
        msg_block = "\n".join(messages[-10:])
        if len(msg_block) > 3000:
            msg_block = msg_block[-3000:]

        sc = self._scanner_config()
        classification_enabled = sc.get("classification_enabled", True)

        prompt = (
            "You are summarizing an email thread for a personal knowledge base.\n\n"
            f"Subject: {subject}\n"
            f"Participants: {', '.join(p['email'] if isinstance(p, dict) else p for p in participants[:10])}\n"
            f"Messages ({thread.get('message_count', 0)} total):\n"
            f"{msg_block}\n\n"
            "Respond with EXACTLY this format (no other text):\n"
            "SUMMARY: <1-2 sentence description of thread topic and key decisions>\n"
            "TAGS: <3-6 lowercase comma-separated tags from domains, subject keywords>\n"
        )

        if classification_enabled:
            prompt += (
                "CLASSIFICATION: <one of: human | transactional | marketing | automated>\n"
                "\n"
                "Classification guide:\n"
                "  human = real person-to-person correspondence (colleagues, vendors, family)\n"
                "  transactional = receipts, order/shipping, account alerts, calendar invites\n"
                "  marketing = newsletters, promotions, sales pitches, product announcements\n"
                "  automated = CI/CD, monitoring, build reports, OTP codes, password resets"
            )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("summarize"),
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            text = resp.choices[0].message.content.strip()
            summary = ""
            tags = []
            classification = "unknown"
            for line in text.splitlines():
                if line.startswith("SUMMARY:"):
                    summary = line[len("SUMMARY:"):].strip()
                elif line.startswith("TAGS:"):
                    raw = line[len("TAGS:"):].strip()
                    tags = [t.strip().lower() for t in raw.split(",") if t.strip()]
                elif line.startswith("CLASSIFICATION:"):
                    raw = line[len("CLASSIFICATION:"):].strip().lower()
                    if raw in {"human", "transactional", "marketing", "automated"}:
                        classification = raw
            return summary, tags, classification
        except Exception:
            log.exception("LLM call failed for email thread summary: %s", subject)
            return "", [], "unknown"

    def _tags_from_participants(self, thread: dict) -> list:
        """Fallback tags derived from participant email domains."""
        tags = []
        for p in thread.get("participants", [])[:5]:
            addr = p["email"] if isinstance(p, dict) else p
            domain = addr.split("@")[-1].split(".")[0].lower() if "@" in addr else ""
            if domain and domain not in ("gmail", "yahoo", "hotmail", "icloud", "me", "mac"):
                tags.append(domain)
        # Add first word of subject
        subject = thread.get("subject", "")
        first_word = re.sub(r'[^a-z0-9]', '', subject.split()[0].lower()) if subject.split() else ""
        if first_word and first_word not in tags:
            tags.append(first_word)
        return tags[:6]

    def _slugify(self, subject: str) -> str:
        s = subject.lower()
        s = re.sub(r'[^a-z0-9]+', '-', s)
        s = s.strip('-')
        return s[:40].rstrip('-')

    def _write_memory(self, thread: dict, summary: str, tags: list, classification: str = "unknown"):
        if not isinstance(tags, list):
            tags = [tags]
        memory_path = self._memory_path(thread)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        conv_id = thread.get("conversation_id", 0)

        fm = {
            "source_title": thread.get("raw_subject") or thread.get("subject", ""),
            "summary": summary,
            "tags": tags,
            "classification": classification,
            "last_scanned": now,
            "source_url": f"mailto:conversation-{conv_id}",
            "type": "email_thread",
            "participants": thread.get("participants", []),
            "message_count": thread.get("message_count", 0),
            "last_message": thread.get("last_message", ""),
            "first_message": thread.get("first_message", ""),
            "conversation_id": conv_id,
        }
        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

        # Message log (most recent 10)
        messages = thread.get("messages", [])
        msg_lines = "\n".join(f"- {m}" for m in messages[-10:]) or "- (no messages)"

        content = (
            f"---\n{frontmatter}---\n\n"
            f"## Messages\n{msg_lines}\n\n"
            f"## Context\n{summary}\n"
        )

        tmp_path = memory_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(memory_path))
            log.debug("Wrote %s", memory_path.name)

            # Check watchlists after successful write
            if self.notification_callback:
                from watchlist_checker import check_watchlists
                check_watchlists(memory_path, MEMORIES_DIR, self.notification_callback)

        except Exception:
            log.exception("Failed to write %s", memory_path)
            try:
                tmp_path.unlink()
            except Exception:
                pass

    async def backfill(self, days: int) -> dict:
        """Reprocess email threads from the last N days (max 90). Returns dict with counts."""
        days = min(days, 90)
        state = self._load_state()
        saved_high_water = state.get("high_water_rowid", 0)

        # Temporarily zero high_water_rowid to force rescan
        state["high_water_rowid"] = 0

        sc = self._scanner_config()
        since = datetime.now() - timedelta(days=days)
        excluded = self._excluded_mailboxes(sc)

        source = self._get_data_source()
        if source is None:
            return {"processed": 0, "skipped": 0, "errors": 0, "notes": "No mail data source available"}

        threads, new_max_rowid = source.get_threads_since(since, excluded)

        processed = 0
        skipped = 0
        errors = 0

        for thread in threads:
            try:
                memory_path = self._memory_path(thread)
                if not self._needs_update(thread, memory_path):
                    skipped += 1
                    continue

                summary, tags, classification = self._get_existing_summary_and_tags(memory_path, thread)
                if not summary or not tags:
                    summary, tags, classification = await self._generate_summary_and_tags(thread)
                if not summary:
                    summary = thread.get("subject", "")
                if not tags:
                    tags = self._tags_from_participants(thread)
                if not classification:
                    classification = "unknown"

                self._write_memory(thread, summary, tags, classification)
                processed += 1
            except Exception as e:
                log.error(f"Backfill error processing thread {thread.get('subject')}: {e}")
                errors += 1

        # Update state with new high_water (don't restore saved value)
        if new_max_rowid > 0:
            state["high_water_rowid"] = new_max_rowid
        state["last_scan_time"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._save_state(state)

        return {
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "notes": f"Scanned {days} days of email history"
        }

    def _clear_full_rescan_flag(self):
        """Set full_rescan: false in config.yaml after a forced rescan."""
        try:
            cfg = self._load_config()
            if cfg.get("email_scanner", {}).get("full_rescan"):
                cfg["email_scanner"]["full_rescan"] = False
                tmp = CONFIG_PATH.with_suffix(".tmp")
                tmp.write_text(yaml.dump(cfg, sort_keys=False, allow_unicode=True))
                os.rename(str(tmp), str(CONFIG_PATH))
        except Exception as e:
            log.debug("Could not clear full_rescan flag: %s", e)
