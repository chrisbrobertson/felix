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

log = logging.getLogger("calendar-scanner")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "calendar-scanner-state.json"

# Seconds between 1970-01-01 and 2001-01-01 (Core Data epoch offset)
CORE_DATA_EPOCH_OFFSET = 978307200

# Calendar Cache temp path
CALENDAR_CACHE_TMP = Path("/tmp/second-brain-calendar-cache")

# Max events per scan cycle (rate limiting)
MAX_EVENTS_PER_CYCLE = 50

# Max state entries before pruning
MAX_STATE_ENTRIES = 5000


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
    """Abstract base. Concrete subclasses: CalendarCacheSource, AppleScriptSource."""

    @classmethod
    def detect(cls):
        """Factory: return best available source, or None if nothing works."""
        src = CalendarCacheSource.create()
        if src:
            return src
        log.warning(
            "Calendar Cache unavailable — falling back to AppleScript. "
            "Ensure Calendar.app has local calendars configured."
        )
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

    def _copy_db(self) -> Path:
        """Copy to /tmp to avoid WAL lock issues while Calendar.app is running."""
        try:
            shutil.copy2(str(self._db_path), str(CALENDAR_CACHE_TMP))
            # Also copy WAL/SHM files if present
            for ext in ("-wal", "-shm"):
                src = Path(str(self._db_path) + ext)
                if src.exists():
                    shutil.copy2(str(src), str(CALENDAR_CACHE_TMP) + ext)
        except Exception as e:
            log.warning("Failed to copy Calendar Cache: %s", e)
            raise
        return CALENDAR_CACHE_TMP

    def get_events(self, start_date: datetime, end_date: datetime, skip_calendars: set):
        """Fetch events from ZCALENDARITEM within date range."""
        tmp = self._copy_db()
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
                for ext in ("-wal", "-shm"):
                    tmp_ext = Path(str(tmp) + ext)
                    if tmp_ext.exists():
                        tmp_ext.unlink()
            except Exception:
                pass


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
                    log.warning("Calendar.app Automation permission denied (error -1743)")
                else:
                    log.debug("osascript error: %s", stderr.strip())
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
                "modified_time": datetime.now(),  # Unknown in AppleScript
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

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

    def _scanner_config(self) -> dict:
        return self._load_config().get("calendar_scanner", {})

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {"last_scan_time": None, "processed": {}}

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

        if not events:
            log.debug("No calendar events to process")
        else:
            log.info("Processing %d calendar event(s)", min(len(events), max_events))

        processed_count = 0
        for event in events[:max_events]:
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

        if processed_count:
            log.info("Calendar scan complete — %d event(s) updated", processed_count)

    def _memory_path(self, event: dict) -> Path:
        """Generate memory file path for event."""
        start_date_str = event["start_time"].strftime("%Y-%m-%d")
        slug = _slugify(event["title"])
        hash_val = _event_hash(
            event.get("external_id", ""),
            event["title"],
            event["start_time"].isoformat()
        )
        return MEMORIES_DIR / f"calendar-event-{start_date_str}-{slug}-{hash_val}.md"

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
                model="summarize",
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

        # Build frontmatter
        fm = {
            "source_title": event["title"],
            "summary": summary,
            "tags": tags,
            "last_scanned": now,
            "source_url": f"calendar:{_event_hash(event.get('external_id', ''), event['title'], event['start_time'].isoformat())}",
            "type": "calendar_event",
            "calendar_name": event["calendar_name"],
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

        content = f"""---
{frontmatter}---

## Event Details

**When:** {start_display} – {end_display}
**Where:** {location_display}
**Calendar:** {event['calendar_name']}
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
