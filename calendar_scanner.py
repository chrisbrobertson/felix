import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from llm_routes import resolve

log = logging.getLogger("calendar-scanner")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "calendar-scanner-state.json"
MIGRATION_SENTINEL_NAME = ".calendar-migration-hostname-v2.done"

# Regex to extract the canonical "YYYY-MM-DD-{tail}" suffix from a (possibly
# hostname-stacked) calendar-event stem. The tail captures everything from the
# first ISO-date segment onward, which is the part _memory_path() puts after
# the hostname.
_CALENDAR_TAIL_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-.+)$")


def _migration_sentinel_path() -> Path:
    """Path to the one-shot migration sentinel.

    Derived from ``STATE_FILE`` at call time so tests that patch ``STATE_FILE``
    redirect the sentinel with it.
    """
    return STATE_FILE.parent / MIGRATION_SENTINEL_NAME

# Seconds between 1970-01-01 and 2001-01-01 (Core Data epoch offset)
CORE_DATA_EPOCH_OFFSET = 978307200

# Max events per scan cycle (rate limiting)
MAX_EVENTS_PER_CYCLE = 50

# Max state entries before pruning
MAX_STATE_ENTRIES = 5000


def _hostname() -> str:
    return socket.gethostname().split(".")[0]


def _make_calendar_cache_tmp() -> Path:
    """Create an unpredictable temp file for Calendar Cache copy."""
    fd, tmp_path_str = tempfile.mkstemp(prefix="second-brain-", suffix="-calendar-cache")
    os.fchmod(fd, 0o600)
    os.close(fd)
    return Path(tmp_path_str)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _cd_to_datetime(cd_ts: float) -> datetime:
    """Convert Core Data timestamp (seconds since 2001-01-01) to datetime."""
    if not cd_ts:
        return datetime(2001, 1, 1)
    return datetime.utcfromtimestamp(float(cd_ts) + CORE_DATA_EPOCH_OFFSET)


def _datetime_to_cd(dt: datetime) -> float:
    """Convert datetime to Core Data timestamp for SQL WHERE clauses."""
    epoch = datetime(1970, 1, 1)
    unix_ts = (dt - epoch).total_seconds()
    return unix_ts - CORE_DATA_EPOCH_OFFSET


def _slugify(text: str) -> str:
    """Convert text to lowercase slug."""
    s = text.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s[:40].rstrip('-')


def _event_hash(external_id: str, title: str, start_time: str) -> str:
    """Generate 8-char hash for event filename."""
    content = external_id or f"{title}{start_time}"
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:8]


# ── Data source base ──────────────────────────────────────────────────────────

class CalendarDataSource:
    """Abstract base. Concrete subclasses: CalendarCacheSource, EventKitSource, AppleScriptSource."""

    @classmethod
    def detect(cls):
        """Factory: return best available source, or None if nothing works."""
        src = CalendarCacheSource.create()
        if src:
            log.info("Calendar data source: Calendar Cache (SQLite)")
            return src
        log.debug("Calendar Cache unavailable — trying EventKit")
        src = EventKitSource.create()
        if src:
            log.info("Calendar data source: EventKit")
            return src
        log.warning(
            "Calendar Cache and EventKit unavailable — falling back to AppleScript. "
            "Grant Calendar access in System Settings → Privacy & Security → Calendars."
        )
        log.info("Calendar data source: AppleScript")
        return AppleScriptSource()

    def get_events(self, start_date, end_date, skip_calendars):
        raise NotImplementedError


# ── SQLite: Calendar Cache ────────────────────────────────────────────────────

class CalendarCacheSource(CalendarDataSource):
    def __init__(self, db_path: Path):
        self._db_path = db_path

    @classmethod
    def _find_db_path(cls):
        """Find Calendar Cache database among candidates."""
        candidates = [
            Path.home() / "Library" / "Calendars" / "Calendar Cache",
            Path.home() / "Library" / "Group Containers" / "group.com.apple.calendar" / "Calendar Cache",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    @classmethod
    def create(cls):
        """Return a CalendarCacheSource if DB is accessible, else None."""
        path = cls._find_db_path()
        if not path:
            log.debug("No Calendar Cache found")
            return None
        try:
            path.stat()
        except Exception as e:
            log.debug("Calendar Cache not accessible: %s", e)
            return None
        return cls(path)

    def _copy_db(self) -> tuple[Path, list[Path]]:
        """Copy to /tmp to avoid WAL lock issues while Calendar.app is running.

        Returns (main_db_path, [aux_file_paths]) where aux_file_paths are WAL/SHM copies.
        """
        tmp = _make_calendar_cache_tmp()
        aux_files = []
        try:
            shutil.copy2(str(self._db_path), str(tmp))
            # Also copy WAL/SHM files if present
            for ext in ("-wal", "-shm"):
                src = Path(str(self._db_path) + ext)
                if src.exists():
                    # Create temp file for WAL/SHM with unpredictable name
                    fd_aux, tmp_aux_str = tempfile.mkstemp(prefix="second-brain-", suffix=f"-calendar{ext}")
                    os.fchmod(fd_aux, 0o600)
                    os.close(fd_aux)
                    shutil.copy2(str(src), tmp_aux_str)
                    aux_files.append(Path(tmp_aux_str))
        except Exception as e:
            log.warning("Failed to copy Calendar Cache: %s", e)
            # Clean up on failure
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            for aux in aux_files:
                try:
                    os.unlink(aux)
                except FileNotFoundError:
                    pass
            raise
        return tmp, aux_files

    def get_events(self, start_date: datetime, end_date: datetime, skip_calendars: set):
        """Fetch events from ZCALENDARITEM within date range."""
        tmp, aux_files = self._copy_db()
        try:
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            conn.row_factory = None

            start_cd = _datetime_to_cd(start_date)
            end_cd = _datetime_to_cd(end_date)

            # Main event query
            sql = """
                SELECT
                    ci.Z_PK         AS pk,
                    ci.ZTITLE       AS title,
                    ci.ZSTARTDATE   AS start_cd,
                    ci.ZENDDATE     AS end_cd,
                    ci.ZMODIFIEDDATE AS modified_cd,
                    ci.ZLOCATION    AS location,
                    ci.ZNOTES       AS notes,
                    ci.ZISALLDAY    AS all_day,
                    ci.ZHASRECURRENCERULES AS recurring,
                    cal.ZTITLE      AS calendar_name,
                    ci.ZEXTERNALIDENTIFIER AS external_id
                FROM ZCALENDARITEM ci
                JOIN ZCALENDAR cal ON ci.ZCALENDAR = cal.Z_PK
                WHERE ci.ZSTARTDATE >= :start_cd
                  AND ci.ZSTARTDATE <= :end_cd
                  AND ci.ZMYATTENDEESTATUS != 3
                ORDER BY ci.ZSTARTDATE ASC
            """

            try:
                cursor = conn.execute(sql, {"start_cd": start_cd, "end_cd": end_cd})
                rows = cursor.fetchall()
            except sqlite3.OperationalError as e:
                log.warning("Calendar Cache query failed (schema may have changed): %s", e)
                conn.close()
                return []

            # Build event dicts and fetch attendees
            events = []
            for row in rows:
                (pk, title, start_cd, end_cd, modified_cd, location, notes,
                 all_day, recurring, calendar_name, external_id) = row

                # Filter by skip_calendars
                if calendar_name and calendar_name.lower() in {c.lower() for c in skip_calendars}:
                    continue

                # Fetch attendees
                attendee_sql = """
                    SELECT ZCOMMONNAME, ZADDRESS
                    FROM ZATTENDEE
                    WHERE ZCALENDARITEM = ?
                """
                try:
                    attendee_cursor = conn.execute(attendee_sql, (pk,))
                    attendee_rows = attendee_cursor.fetchall()
                    participants = []
                    for att_name, att_email in attendee_rows:
                        participants.append({
                            "name": att_name or "",
                            "email": att_email or ""
                        })
                except Exception:
                    participants = []

                events.append({
                    "pk": pk,
                    "title": title or "Untitled Event",
                    "start_time": _cd_to_datetime(start_cd) if start_cd else datetime.now(),
                    "end_time": _cd_to_datetime(end_cd) if end_cd else datetime.now(),
                    "modified_time": _cd_to_datetime(modified_cd) if modified_cd else datetime.now(),
                    "location": location or "",
                    "notes": notes or "",
                    "all_day": bool(all_day),
                    "recurring": bool(recurring),
                    "calendar_name": calendar_name or "Unknown",
                    "external_id": external_id or "",
                    "participants": participants,
                })

            conn.close()
            return events
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
            for aux in aux_files:
                try:
                    aux.unlink()
                except Exception:
                    pass


# ── EventKit (PyObjC) ─────────────────────────────────────────────────────────

class EventKitSource(CalendarDataSource):
    """Uses EventKit framework via PyObjC. Requires pyobjc-framework-EventKit and
    Calendar permission granted in System Settings → Privacy & Security → Calendars."""

    def __init__(self, store):
        self._store = store

    @classmethod
    def create(cls):
        """Return an EventKitSource if EventKit is available and authorized, else None."""
        try:
            import EventKit as EK
        except ImportError:
            log.debug("pyobjc-framework-EventKit not installed")
            return None

        status = EK.EKEventStore.authorizationStatusForEntityType_(EK.EKEntityTypeEvent)
        # 0=not determined, 1=restricted, 2=denied, 3=authorized, 4=full access (macOS 14+)
        if status == 0:
            # Request access synchronously (blocks briefly; shows dialog on first call)
            import threading
            store = EK.EKEventStore.alloc().init()
            result = {}
            done = threading.Event()

            def completion(granted, error):
                result["granted"] = granted
                done.set()

            store.requestFullAccessToEventsWithCompletion_(completion)
            done.wait(timeout=30)
            if not result.get("granted"):
                log.warning(
                    "EventKit Calendar access denied. "
                    "Grant in System Settings → Privacy & Security → Calendars."
                )
                return None
            return cls(store)
        elif status in (3, 4):
            store = EK.EKEventStore.alloc().init()
            return cls(store)
        else:
            log.warning(
                "EventKit Calendar access denied (status %d) — "
                "grant Calendar access in System Settings → Privacy & Security → Calendars",
                status,
            )
            return None

    def get_events(self, start_date: datetime, end_date: datetime, skip_calendars: set):
        """Fetch events via EventKit."""
        try:
            import EventKit as EK
            import Foundation
        except ImportError:
            return []

        ns_start = Foundation.NSDate.dateWithTimeIntervalSince1970_(start_date.timestamp())
        ns_end = Foundation.NSDate.dateWithTimeIntervalSince1970_(end_date.timestamp())

        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            ns_start, ns_end, None
        )
        ek_events = self._store.eventsMatchingPredicate_(predicate) or []

        skip_lower = {c.lower() for c in skip_calendars}
        events = []
        for ev in ek_events:
            try:
                cal_name = str(ev.calendar().title()) if ev.calendar() else "Unknown"
                if cal_name.lower() in skip_lower:
                    continue

                title = str(ev.title() or "Untitled Event")
                start_ts = float(ev.startDate().timeIntervalSince1970())
                end_ts = float(ev.endDate().timeIntervalSince1970())
                start_time = datetime.fromtimestamp(start_ts)
                end_time = datetime.fromtimestamp(end_ts)
                modified_ts = float(ev.lastModifiedDate().timeIntervalSince1970()) if ev.lastModifiedDate() else start_ts
                modified_time = datetime.fromtimestamp(modified_ts)

                location = str(ev.location() or "")
                notes = str(ev.notes() or "")
                all_day = bool(ev.isAllDay())
                ext_id = str(ev.eventIdentifier() or "")

                # Attendees
                participants = []
                attendees = ev.attendees() or []
                for att in attendees:
                    try:
                        name = str(att.name() or "")
                        # Email is in the URL: mailto:address@example.com
                        url = att.URL()
                        email = str(url).replace("mailto:", "") if url else ""
                        participants.append({"name": name, "email": email})
                    except Exception:
                        pass

                events.append({
                    "pk": 0,
                    "title": title,
                    "start_time": start_time,
                    "end_time": end_time,
                    "modified_time": modified_time,
                    "location": location,
                    "notes": notes,
                    "all_day": all_day,
                    "recurring": bool(ev.hasRecurrenceRules()),
                    "calendar_name": cal_name,
                    "external_id": ext_id,
                    "participants": participants,
                })
            except Exception as e:
                log.debug("EventKit: error reading event: %s", e)

        return events


# ── AppleScript fallback ──────────────────────────────────────────────────────

class AppleScriptSource(CalendarDataSource):
    """Fallback when Calendar Cache is unavailable."""

    def _run_osascript(self, script: str, timeout: int = 60) -> str:
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
                log.warning("AppleScript timed out after %ds", timeout)
                return ""
            if proc.returncode != 0:
                if "-1743" in stderr:
                    log.warning(
                        "Calendar.app Automation permission denied (error -1743). "
                        "Grant Automation access: System Settings → Privacy & Security → Automation."
                    )
                else:
                    log.warning("osascript exited %d: %s", proc.returncode, stderr.strip()[:200])
                return ""
            return stdout.strip()
        except Exception as e:
            log.debug("osascript failed: %s", e)
            return ""

    def get_events(self, start_date: datetime, end_date: datetime, skip_calendars: set):
        """Fetch events via AppleScript."""
        # Format dates for AppleScript
        lookback_days = (datetime.now() - start_date).days
        lookahead_days = (end_date - datetime.now()).days

        script = f'''
set output to ""
set lookback to (current date) - ({lookback_days} * days)
set lookahead to (current date) + ({lookahead_days} * days)
tell application "Calendar"
    repeat with cal in calendars
        repeat with ev in (every event of cal whose start date >= lookback and start date <= lookahead)
            set output to output & (summary of ev) & "|||"
            set output to output & (start date of ev as string) & "|||"
            set output to output & (end date of ev as string) & "|||"
            try
                set output to output & (location of ev) & "|||"
            on error
                set output to output & "|||"
            end try
            set output to output & (name of cal) & "|||"
            set output to output & "
"
        end repeat
    end repeat
end tell
output
'''

        raw = self._run_osascript(script)
        if not raw:
            return []

        # Parse output
        events = []
        for line in raw.splitlines():
            parts = line.split("|||")
            if len(parts) < 5:
                continue

            title = parts[0].strip()
            start_str = parts[1].strip()
            end_str = parts[2].strip()
            location = parts[3].strip()
            calendar_name = parts[4].strip()

            # Filter by skip_calendars
            if calendar_name.lower() in {c.lower() for c in skip_calendars}:
                continue

            # Parse AppleScript date format: "Monday, April 11, 2026 at 9:00:00 AM"
            # Simplified: assume current year if parse fails
            try:
                # Try to parse full format
                start_time = datetime.strptime(start_str, "%A, %B %d, %Y at %I:%M:%S %p")
            except ValueError:
                # Fallback to current time
                start_time = datetime.now()

            try:
                end_time = datetime.strptime(end_str, "%A, %B %d, %Y at %I:%M:%S %p")
            except ValueError:
                end_time = start_time + timedelta(hours=1)

            # Hash for pseudo-external-id
            external_id = _event_hash("", title, start_time.isoformat())

            events.append({
                "pk": 0,
                "title": title or "Untitled Event",
                "start_time": start_time,
                "end_time": end_time,
                "modified_time": start_time,  # AppleScript has no modified_time; use start_time as stable proxy
                "location": location,
                "notes": "",  # Notes not available in simple AppleScript
                "all_day": False,  # Detect via time if needed
                "recurring": False,  # Not available in AppleScript
                "calendar_name": calendar_name,
                "external_id": external_id,
                "participants": [],
            })

        return events


# ── Calendar Scanner ──────────────────────────────────────────────────────────

class CalendarScanner:
    def __init__(self, role: str = "full"):
        self.role = role
        self._migrate_calendar_filenames()

    def _migrate_calendar_filenames(self):
        """One-shot cleanup of stacked/polluted calendar-event filenames.

        Earlier builds prefixed filenames with ``socket.gethostname()`` on every
        ``__init__`` using a naive ``startswith`` idempotency check. Because
        ``socket.gethostname()`` is not stable on macOS (it flips between values
        like ``Chriss-Air`` and ``Chriss-MacBook-Air`` depending on network
        state), the check missed and the migration re-prefixed already-migrated
        files — stacking hostname segments on every flip.

        This implementation is gated by a one-shot sentinel. On first run it
        collapses stacked/polluted filenames to canonical form
        ``calendar-event-{hostname}-{YYYY-MM-DD}-{tail}.md`` by trusting the
        frontmatter ``hostname`` field (authoritative), stamping it if missing,
        and deleting stacked duplicates where the canonical file already exists.
        Corresponding state keys are remapped to canonical form. After the first
        successful run the sentinel is written and subsequent calls return
        immediately.
        """
        sentinel = _migration_sentinel_path()
        if sentinel.exists():
            return
        if not MEMORIES_DIR.exists():
            # Still stamp the sentinel so we don't re-scan an absent dir forever.
            try:
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.touch()
            except Exception:
                log.exception("Failed to write calendar migration sentinel")
            return

        my_hostname = _hostname()
        renamed = 0
        deleted = 0
        stamped = 0
        skipped = 0
        # Load raw state here — we explicitly WANT to see the stacked keys so
        # we can remap them to canonical form before the usual _load_state
        # prune hides them.
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except Exception:
                state = {"last_scan_time": None, "processed": {}}
        else:
            state = {"last_scan_time": None, "processed": {}}
        processed = state.get("processed", {})

        for path in MEMORIES_DIR.glob("calendar-event-*.md"):
            try:
                stem = path.stem
                rest = stem[len("calendar-event-"):]
                m = _CALENDAR_TAIL_RE.search(rest)
                if not m:
                    log.warning(
                        "Calendar cleanup: no date pattern in filename, skipping: %s",
                        path.name,
                    )
                    skipped += 1
                    continue
                canonical_tail = m.group(1)

                text = path.read_text()
                fm = _parse_frontmatter(text)
                fm_hostname = fm.get("hostname", "")

                # Authoritative hostname: frontmatter wins. If absent, assume
                # the current host wrote it and stamp the frontmatter below.
                needs_stamp = False
                if fm_hostname:
                    canonical_host = fm_hostname
                else:
                    canonical_host = my_hostname
                    needs_stamp = True

                canonical_name = f"calendar-event-{canonical_host}-{canonical_tail}.md"
                canonical_path = path.parent / canonical_name

                # If file is already canonical, optionally stamp missing hostname
                # in frontmatter and move on.
                if path.name == canonical_name:
                    if needs_stamp:
                        self._stamp_hostname_in_frontmatter(path, canonical_host)
                        stamped += 1
                    continue

                # Stacked duplicate where canonical already exists — drop it.
                if canonical_path.exists():
                    path.unlink()
                    if path.name in processed:
                        processed.pop(path.name, None)
                    deleted += 1
                    continue

                # Rename to canonical form; stamp hostname into frontmatter if
                # it was missing so future runs (and other readers) can trust it.
                if needs_stamp:
                    self._stamp_hostname_in_frontmatter(path, canonical_host)
                    stamped += 1
                path.rename(canonical_path)
                if path.name in processed:
                    processed[canonical_name] = processed.pop(path.name)
                renamed += 1
            except (OSError, FileNotFoundError):
                pass
            except Exception:
                log.exception("Calendar filename cleanup failed for %s", path)

        if renamed or deleted or stamped:
            state["processed"] = processed
            self._save_state(state)
            log.info(
                "Calendar filename cleanup: renamed=%d deleted=%d stamped=%d skipped=%d",
                renamed, deleted, stamped, skipped,
            )

        # Mark migration complete so future __init__ calls are no-ops.
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        except Exception:
            log.exception("Failed to write calendar migration sentinel")

    @staticmethod
    def _stamp_hostname_in_frontmatter(path: Path, hostname: str):
        """Atomically rewrite ``path`` with ``hostname`` added to its YAML frontmatter.

        No-op if the file has no frontmatter delimiters or already contains a
        hostname field. Uses write-tmp-then-rename for atomicity.
        """
        text = path.read_text()
        parts = text.split("---", 2)
        if len(parts) < 3:
            return
        try:
            fm = yaml.safe_load(parts[1]) or {}
        except Exception:
            return
        if fm.get("hostname"):
            return
        fm["hostname"] = hostname
        new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        new_text = f"---\n{new_fm}---{parts[2]}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text)
        os.rename(str(tmp), str(path))

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

    def _scanner_config(self) -> dict:
        return self._load_config().get("calendar_scanner", {})

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text())
            except Exception:
                return {"last_scan_time": None, "processed": {}}
            state["processed"] = self._prune_stacked_state_keys(state.get("processed", {}))
            return state
        return {"last_scan_time": None, "processed": {}}

    @staticmethod
    def _prune_stacked_state_keys(processed: dict) -> dict:
        """Drop state keys whose filename prefix has a duplicated hostname token.

        Targets the specific pre-v1.6.0 failure mode where
        ``_migrate_calendar_filenames`` repeatedly re-prefixed the hostname
        because ``socket.gethostname()`` flipped across runs, producing stems
        like ``calendar-event-Chriss-MacBook-Air-Chriss-Air-test-host-…`` with
        repeated tokens before the ``YYYY-MM-DD`` tail. These keys no longer
        correspond to any canonical on-disk file.

        Conservative: a key with a single (possibly hyphenated) hostname — even
        literal ``test-host`` from a mock — is retained, because that shape is
        identical to what legitimate tests write.
        """
        if not processed:
            return processed
        pruned = {}
        for k, v in processed.items():
            stem = k[:-3] if k.endswith(".md") else k
            if stem.startswith("calendar-event-"):
                rest = stem[len("calendar-event-"):]
                m = _CALENDAR_TAIL_RE.search(rest)
                if m:
                    prefix = rest[: m.start()].rstrip("-")
                    tokens = [t for t in prefix.split("-") if t]
                    # Repeated token in the hostname prefix = stacked migration.
                    if tokens and len(tokens) != len(set(tokens)):
                        continue
            pruned[k] = v
        return pruned

    def _save_state(self, state: dict):
        """Save state atomically."""
        # Prune processed map to MAX_STATE_ENTRIES
        if len(state.get("processed", {})) > MAX_STATE_ENTRIES:
            processed = state["processed"]
            # Sort by modification timestamp, keep newest
            sorted_items = sorted(
                processed.items(),
                key=lambda x: x[1],
                reverse=True
            )
            state["processed"] = dict(sorted_items[:MAX_STATE_ENTRIES])

        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(STATE_FILE))
        except Exception as e:
            log.warning("Failed to save scanner state: %s", e)

    async def run_loop(self, stop_event: asyncio.Event):
        sc = self._scanner_config()
        interval = sc.get("interval_seconds", 300)
        log.info("Calendar scanner started — polling every %ds", interval)

        while not stop_event.is_set():
            try:
                await self._run_scan()
            except Exception:
                log.exception("Uncaught error in calendar scanner cycle")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def backfill(self, days: int) -> dict:
        """Reprocess calendar events from the last N days (max 180). Returns dict with counts."""
        days = min(days, 180)
        state = self._load_state()

        # Clear processed map to force reprocessing
        state["processed"] = {}

        sc = self._scanner_config()
        skip_calendars = set(sc.get("skip_calendars", []))

        # Use days for both lookback and forward window
        start_date = datetime.now() - timedelta(days=days)
        end_date = datetime.now() + timedelta(days=days)

        source = CalendarDataSource.detect()
        if source is None:
            return {
                "processed": 0,
                "skipped": 0,
                "errors": 0,
                "notes": "No calendar data source available"
            }

        events = source.get_events(start_date, end_date, skip_calendars)

        # Deduplicate (same logic as _run_scan)
        dedup: dict = {}
        for event in events:
            key = (
                event["title"].lower().strip(),
                event["start_time"].strftime("%Y-%m-%dT%H:%M"),
            )
            if key not in dedup:
                event["calendar_names"] = [event["calendar_name"]]
                dedup[key] = event
            else:
                merged = dedup[key]
                cal = event["calendar_name"]
                if cal not in merged["calendar_names"]:
                    merged["calendar_names"].append(cal)
                if event["modified_time"] > merged["modified_time"]:
                    merged["modified_time"] = event["modified_time"]
                seen = {p["email"] or p["name"] for p in merged.get("participants", [])}
                for p in event.get("participants", []):
                    pk = p["email"] or p["name"]
                    if pk and pk not in seen:
                        merged.setdefault("participants", []).append(p)
                        seen.add(pk)

        deduplicated = list(dedup.values())

        processed = 0
        skipped = 0
        errors = 0

        for event in deduplicated:
            try:
                memory_path = self._memory_path(event)
                filename = memory_path.name

                # Generate summary and tags
                summary, tags = await self._generate_summary_and_tags(event)
                if not summary:
                    summary = event["title"]
                if not tags:
                    tags = ["calendar"]

                # Write memory file
                self._write_memory(event, summary, tags)

                # Update state
                modified_str = event["modified_time"].isoformat()
                state.setdefault("processed", {})[filename] = modified_str
                self._save_state(state)

                processed += 1
            except Exception as e:
                log.error(f"Backfill error processing event {event.get('title')}: {e}")
                errors += 1

        state["last_scan_time"] = datetime.now().isoformat()
        self._save_state(state)

        return {
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "notes": f"Scanned ±{days} days of calendar events"
        }

    async def _run_scan(self):
        sc = self._scanner_config()
        state = self._load_state()

        lookback_days = int(sc.get("lookback_days", 7))
        forward_days = int(sc.get("forward_days", 7))
        skip_calendars = set(sc.get("skip_calendars", []))
        max_events = int(sc.get("max_events_per_cycle", MAX_EVENTS_PER_CYCLE))

        start_date = datetime.now() - timedelta(days=lookback_days)
        end_date = datetime.now() + timedelta(days=forward_days)

        source = CalendarDataSource.detect()
        if source is None:
            log.warning("No calendar data source available — skipping scan")
            return

        events = source.get_events(start_date, end_date, skip_calendars)

        # Deduplicate: same event on multiple calendars → one merged entry.
        # Key: (lowercase title, start truncated to minute).  This is stable
        # across CalendarCache, EventKit, and AppleScript sources regardless of
        # per-source external_id differences.
        dedup: dict = {}
        for event in events:
            key = (
                event["title"].lower().strip(),
                event["start_time"].strftime("%Y-%m-%dT%H:%M"),
            )
            if key not in dedup:
                event["calendar_names"] = [event["calendar_name"]]
                dedup[key] = event
            else:
                merged = dedup[key]
                # Accumulate calendar names (no duplicates)
                cal = event["calendar_name"]
                if cal not in merged["calendar_names"]:
                    merged["calendar_names"].append(cal)
                # Keep the most recent modification timestamp
                if event["modified_time"] > merged["modified_time"]:
                    merged["modified_time"] = event["modified_time"]
                # Merge participants — deduplicate by email then name
                seen = {p["email"] or p["name"] for p in merged.get("participants", [])}
                for p in event.get("participants", []):
                    pk = p["email"] or p["name"]
                    if pk and pk not in seen:
                        merged.setdefault("participants", []).append(p)
                        seen.add(pk)

        deduplicated = list(dedup.values())
        if not deduplicated:
            log.info("Calendar scan: 0 events in -%d/+%d day window", lookback_days, forward_days)
            state["last_scan_time"] = datetime.now().isoformat()
            self._save_state(state)
            return
        else:
            raw_count = len(events)
            merged_count = raw_count - len(deduplicated)
            capped = min(len(deduplicated), max_events)
            msg = f"Calendar scan: {capped} event(s) in window"
            if merged_count:
                msg += f" ({merged_count} cross-calendar duplicate(s) merged)"
            log.info(msg)

        processed_count = 0
        for event in deduplicated[:max_events]:
            try:
                memory_path = self._memory_path(event)
                filename = memory_path.name

                # Check if event needs update
                modified_str = event["modified_time"].isoformat()
                stored_modified = state.get("processed", {}).get(filename)

                if stored_modified == modified_str:
                    # Event unchanged, skip
                    continue

                # Generate summary and tags
                summary, tags = await self._generate_summary_and_tags(event)
                if not summary:
                    summary = event["title"]
                if not tags:
                    tags = ["calendar"]

                # Write memory file
                self._write_memory(event, summary, tags)

                # Update state
                state.setdefault("processed", {})[filename] = modified_str
                self._save_state(state)

                processed_count += 1
            except Exception:
                log.exception("Error processing event: %s", event.get("title"))

        state["last_scan_time"] = datetime.now().isoformat()
        self._save_state(state)

        skipped_count = len(deduplicated[:max_events]) - processed_count
        if processed_count:
            log.info("Calendar scan complete — %d updated, %d unchanged", processed_count, skipped_count)
        else:
            log.info("Calendar scan complete — 0 updated, %d event(s) already current", skipped_count)

    def _memory_path(self, event: dict) -> Path:
        """Generate memory file path for event.

        Filename includes hostname so events from different machines never collide.
        Hash is based on title + start-minute only — external_id is deliberately
        excluded so the same event appearing on multiple calendars always maps to
        the same file regardless of per-calendar identifier differences.
        """
        start_date_str = event["start_time"].strftime("%Y-%m-%d")
        slug = _slugify(event["title"])
        # Truncate to minute so minor timestamp jitter across calendars stays stable
        hash_val = _event_hash(
            "",
            event["title"],
            event["start_time"].strftime("%Y-%m-%dT%H:%M"),
        )
        hostname = _hostname()
        return MEMORIES_DIR / f"calendar-event-{hostname}-{start_date_str}-{slug}-{hash_val}.md"

    async def _generate_summary_and_tags(self, event: dict) -> tuple:
        """Generate LLM summary and tags for event."""
        title = event["title"]
        start_time = event["start_time"].strftime("%A %B %d, %Y at %I:%M %p")
        end_time = event["end_time"].strftime("%I:%M %p")
        calendar_name = event["calendar_name"]
        location = event["location"] or "not specified"
        notes = event["notes"] or "none"
        attendees = ", ".join(
            p["name"] or p["email"] for p in event.get("participants", [])
        ) or "none"

        prompt = f"""Summarize this calendar event in 1-2 sentences. Then provide 3-5 tags.

Title: {title}
Date: {start_time} – {end_time}
Calendar: {calendar_name}
Location: {location}
Attendees: {attendees}
Notes: {notes}

Return JSON only:
{{
  "summary": "...",
  "tags": ["tag1", "tag2"]
}}
"""

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("summarize"),
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            text = resp.choices[0].message.content.strip()

            # Parse JSON response
            try:
                data = json.loads(text)
                summary = data.get("summary", "")
                tags = data.get("tags", [])
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",")]
                # Normalize tags
                tags = [t.lower().replace(" ", "-") for t in tags if t]
                return summary[:280], tags[:6]
            except json.JSONDecodeError:
                log.warning("LLM returned invalid JSON for event: %s", title)
                return "", []
        except Exception:
            log.exception("LLM call failed for calendar event: %s", title)
            return "", []

    def _write_memory(self, event: dict, summary: str, tags: list):
        """Write event memory file atomically."""
        memory_path = self._memory_path(event)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # calendar_names is set by deduplication; fall back for direct calls in tests
        calendar_names = event.get("calendar_names") or [event.get("calendar_name", "Unknown")]

        # Build frontmatter
        fm = {
            "source_title": event["title"],
            "summary": summary,
            "tags": tags,
            "last_scanned": now,
            "source_url": f"calendar:{_event_hash('', event['title'], event['start_time'].strftime('%Y-%m-%dT%H:%M'))}",
            "type": "calendar_event",
            "hostname": _hostname(),
            "calendar_names": calendar_names,
            "start_time": event["start_time"].isoformat(),
            "end_time": event["end_time"].isoformat(),
            "all_day": event["all_day"],
            "location": event["location"],
            "participants": event.get("participants", []),
            "recurrence": event["recurring"],
        }
        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

        # Build body
        start_display = event["start_time"].strftime("%A %B %d, %Y at %I:%M %p")
        end_display = event["end_time"].strftime("%I:%M %p")
        location_display = event["location"] or "Not specified"
        attendees_display = ", ".join(
            p["name"] or p["email"] for p in event.get("participants", [])
        ) or "None"

        calendars_display = ", ".join(calendar_names)
        content = f"""---
{frontmatter}---

## Event Details

**When:** {start_display} – {end_display}
**Where:** {location_display}
**Calendar:** {calendars_display}
**Attendees:** {attendees_display}

## Notes

{event['notes'] or '(No notes)'}

## Context

{summary}
"""

        # Atomic write
        tmp_path = memory_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(memory_path))
            log.debug("Wrote %s", memory_path.name)
        except Exception:
            log.exception("Failed to write %s", memory_path)
            try:
                tmp_path.unlink()
            except Exception:
                pass
