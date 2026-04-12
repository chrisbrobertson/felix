---
specmas: 3.0
kind: feature
id: feat-calendar-scanner
version: 1.0.0
created: 2026-04-11
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-commitment-tracker
  - feat-proactive-notifications
  - feat-contact-tracker
---

# Calendar Scanner

## Overview

### Problem Statement

Secondbrain accumulates memories of past activity — browsed pages, emails, meeting
transcripts — but has no awareness of the calendar. Upcoming meetings, deadlines, and
time-boxed commitments are invisible to the bot. The user cannot ask "what do I have
tomorrow?", "who am I meeting with on Thursday?", or "what prep do I need before my
2pm call?" Calendar events are also a rich source of commitment signals (a "Q4 Review"
implies preparation work; a "follow-up with Sarah" implies a prior commitment) that the
Commitment Tracker could extract if it had access to calendar memory files.

The Calendar Scanner reads Apple Calendar.app data every 5 minutes, writes one memory
file per calendar event within a rolling ±7-day window, and feeds those files into the
Commitment Tracker and Proactive Notification system.

### Scope

**In Scope:**
- Ninth async daemon loop, running every 5 minutes (`full` role only)
- Primary data source: `~/Library/Calendars/Calendar Cache` SQLite database
- Fallback: AppleScript to Calendar.app when SQLite database is absent
- Scan window: events starting up to 7 days in the past through 7 days in the future
- One `calendar-event-{date}-{slug}-{id}.md` per calendar event
- Change detection via event modification timestamp (no write if event unchanged)
- LLM-generated summary and tags per event
- `type: calendar_event` in frontmatter — consumed by Commitment Tracker and
  Proactive Notifications

**Out of Scope:**
- CalDAV, Google Calendar, or Exchange calendar sources (Apple Calendar sync only)
- Event creation or modification (read-only)
- Recurring event instances beyond the ±7-day window
- Reminders.app or Tasks.app
- Attendee RSVP status tracking
- iCloud Calendar sharing or multi-user calendars

### Success Metrics

- All calendar events within the window produce memory files within one scan cycle
- Unchanged events produce zero file writes on subsequent scans
- Memory files scannable by the header cache (type and title in first 500 chars)
- AppleScript fallback produces equivalent output to SQLite path
- Processing latency < 5 minutes from event creation/update to memory file write

---

## Functional Requirements

### FR-1: Calendar Cache SQLite Detection and Copy

Locate the Calendar Cache SQLite database and copy it to a temp path before reading,
to avoid locking the live database while Calendar.app is running.

**Database path:**
```
~/Library/Calendars/Calendar Cache
```

The file has no extension and is a standard SQLite3 database. Unlike Apple Mail's
Envelope Index, this path is **not** protected by TCC (Transparency, Consent, and
Control) — no Full Disk Access grant is required to read it. However, the file must be
copied before reading to avoid WAL contention.

**Copy pattern (from `email_scanner.py` `_copy_db()`):**
```python
import shutil, sqlite3

CALENDAR_CACHE_CANDIDATES = [
    Path.home() / "Library" / "Calendars" / "Calendar Cache",
    Path.home() / "Library" / "Group Containers" /
        "group.com.apple.calendar" / "Calendar Cache",
]
CALENDAR_CACHE_TMP = Path("/tmp/second-brain-calendar-cache")

def _copy_db(src: Path) -> sqlite3.Connection:
    shutil.copy2(src, CALENDAR_CACHE_TMP)
    for wal_suffix in ("-wal", "-shm"):
        wal = Path(str(src) + wal_suffix)
        if wal.exists():
            shutil.copy2(wal, Path(str(CALENDAR_CACHE_TMP) + wal_suffix))
    return sqlite3.connect(f"file:{CALENDAR_CACHE_TMP}?mode=ro", uri=True)
```

Try each candidate path in order. If none exist, fall back to FR-3 (AppleScript).

**Validation criteria:**
- Database path exists → SQLite connection opened successfully
- Database absent → AppleScript fallback triggered (no exception propagated)
- Temp file cleaned up in `finally` block after each scan cycle
- WAL and SHM sidecar files copied alongside the main database

---

### FR-2: Event Query with Core Data Timestamp Conversion

Query ZCALENDARITEM, ZCALENDAR, and ZATTENDEE to fetch events within the scan window.

**Core Data epoch offset** (same as email scanner, defined in utils.py or locally):
```python
CORE_DATA_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01
```

**Timestamp conversion:**
```python
def _cd_to_datetime(cd_ts: float) -> datetime:
    return datetime.utcfromtimestamp(cd_ts + CORE_DATA_EPOCH_OFFSET)

def _datetime_to_cd(dt: datetime) -> float:
    return dt.timestamp() - CORE_DATA_EPOCH_OFFSET
```

**Primary query:**
```sql
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
  AND ci.ZMYATTENDEESTATUS != 3   -- 3 = declined
ORDER BY ci.ZSTARTDATE ASC
```

Where `:start_cd` = now − 7 days (as Core Data ts) and `:end_cd` = now + 7 days.

**Attendee query (per event):**
```sql
SELECT ZCOMMONNAME, ZADDRESS
FROM ZATTENDEE
WHERE ZCALENDARITEM = :event_pk
```

**Validation criteria:**
- Events outside the scan window excluded
- Declined events (ZMYATTENDEESTATUS = 3) excluded
- Core Data timestamps correctly converted to UTC datetimes
- All-day events have `all_day: true` and time set to 00:00:00

---

### FR-3: AppleScript Fallback

When the Calendar Cache SQLite database is absent (Calendar.app not configured with
a local cache, or running on a sandboxed build), fall back to AppleScript.

**Pattern from `email_scanner.py` `_run_osascript()`:**
- Run `osascript -e <script>` via `subprocess.Popen`
- Configurable timeout (default 60s); kill the process on timeout
- Use `|||` as field delimiter in output

**AppleScript template:**
```applescript
set output to ""
set lookback to (current date) - (7 * days)
set lookahead to (current date) + (7 * days)
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
```

**Validation criteria:**
- Automation permission to Calendar.app required; logged at WARNING if denied (error -1743)
- Output parsed line-by-line, splitting on `|||`
- Timeout kills the subprocess without calling `proc.wait()` (prevents hang)
- Produces equivalent memory files as SQLite path for the same events

---

### FR-4: Incremental Polling and State File

Persist scan state across daemon restarts to avoid rewriting unchanged events.

**State file:** `DEPLOY_DIR/calendar-scanner-state.json`

```json
{
  "last_scan_time": "2026-04-11T14:30:00",
  "processed": {
    "calendar-event-2026-04-11-team-standup-abc123.md": "2026-04-10T09:00:00"
  }
}
```

- `last_scan_time`: ISO timestamp of last successful scan completion
- `processed`: map of filename → ISO modification timestamp at time of last write

**State management rules:**
- Load state on scanner init; create empty state if file absent (first run)
- After writing or skipping each event, update `processed[filename]` immediately
- Save state after each event (not batch), same atomic write pattern as other scanners
- Cap `processed` map at 5,000 entries; prune oldest entries by modification date

**Validation criteria:**
- State file created on first run
- Daemon restart does not rewrite all events
- New events not in `processed` map are always scanned
- State file write is atomic (temp file + os.rename)

---

### FR-5: Change Detection via Modification Timestamp

Skip writing a memory file if the event has not changed since last scan.

**Mechanism:**
- Compare `ZMODIFIEDDATE` (SQLite) or derived hash of field values (AppleScript) to
  the stored modification timestamp in `processed[filename]`
- If unchanged: update `last_scanned` in frontmatter without a full LLM re-run
- If changed (or new): fetch full event data, call LLM for summary, write file

**Edge case — event deletion:**
- Deleted events disappear from the database
- On each cycle, entries in `processed` that no longer appear in the query results
  are NOT automatically deleted from memory (memory files are append-only by design)
- The Commitment Tracker and Proactive Notifications treat past events as historical
  context and do not require deletion

**Validation criteria:**
- Unchanged event → no LLM call, no file write (only mtime checked)
- Modified event → full re-scan, file overwritten atomically
- New event → full scan, new file written
- 50-event-per-cycle cap enforced before change detection (FR-9)

---

### FR-6: LLM Summary and Tags Generation

Generate a concise summary and tags for each new or changed calendar event.

**LLM route:** `summarize` (Gemini Flash via LiteLLM)

**Prompt input:**
- Event title, date/time, location
- Calendar name (contextual signal: "Work" vs "Personal")
- Notes field (if present)
- Attendee names and emails

**Prompt structure:**
```
Summarize this calendar event in 1-2 sentences. Then provide 3-5 tags.

Title: {title}
Date: {start_datetime} – {end_datetime}
Calendar: {calendar_name}
Location: {location or "not specified"}
Attendees: {comma-separated names}
Notes: {notes or "none"}

Return JSON only:
{
  "summary": "...",
  "tags": ["tag1", "tag2"]
}
```

**Validation criteria:**
- Returns valid JSON; parse failure logged at WARNING, file written with summary = title
- Tags are lowercase kebab-case strings
- Summary fits within 280 characters

---

### FR-7: Memory File Write

Write one `calendar-event-{date}-{slug}-{id}.md` per calendar event.

**Filename:**
```
calendar-event-{YYYY-MM-DD}-{title-slug}-{8-char-hash}.md
```
where `{YYYY-MM-DD}` is the event's start date (local time), `{title-slug}` is the
title lowercased with spaces → hyphens, max 40 chars, and `{8-char-hash}` is the first
8 characters of SHA1(`external_id or title + start_datetime`).

**File format:**
```markdown
---
source_title: "Team Standup"
summary: Weekly engineering standup with the core team to review sprint progress.
tags: [standup, engineering, recurring]
last_scanned: '2026-04-11T14:30:00'
source_url: calendar:abc123def456
type: calendar_event
calendar_name: Work
start_time: '2026-04-11T09:00:00'
end_time: '2026-04-11T09:30:00'
all_day: false
location: Zoom
participants:
  - name: Chris Robertson
    email: chris@example.com
  - name: Sarah Chen
    email: sarah@example.com
recurrence: true
---

## Event Details

**When:** Friday April 11, 2026 at 9:00 AM – 9:30 AM
**Where:** Zoom
**Calendar:** Work
**Attendees:** Chris Robertson, Sarah Chen

## Notes

Sprint review items: velocity chart, blockers, next sprint planning.

## Context

{LLM summary}
```

**Frontmatter field order** (`sort_keys=False`):
`source_title`, `summary`, `tags`, `last_scanned`, `source_url`, `type`,
`calendar_name`, `start_time`, `end_time`, `all_day`, `location`,
`participants`, `recurrence`

**Write rules:**
- Atomic write via temp file + `os.rename()`
- `source_url` uses `calendar:` scheme with the event's external identifier hash

**Validation criteria:**
- `type: calendar_event` in all written files
- `source_url` uses `calendar:` scheme
- File written atomically (no partial iCloud sync)
- Frontmatter parseable by the header cache (first 500 chars includes title and type)

---

### FR-8: Recurring Event Handling

Recurring events appear as individual instances in ZCALENDARITEM — each occurrence
has its own row with a distinct `ZSTARTDATE`. The scanner treats each instance as a
separate event.

**Rules:**
- Each occurrence within the ±7-day window produces its own memory file
- Filename includes the start date, so occurrences of the same series have distinct names
- `recurrence: true` in frontmatter flags the event as part of a recurring series
- No attempt to reconstruct the recurrence rule or list all future occurrences

**Validation criteria:**
- Weekly standup appearing 3 times in the ±7-day window produces 3 files
- Each file has a unique name (different dates in filename)
- All 3 files have `recurrence: true`

---

### FR-9: Rate Limiting (50 Events Per Cycle)

Cap processing at 50 events per scan cycle to bound LLM cost and cycle duration.

**Rules:**
- After fetching events from the database, sort by `ZSTARTDATE` ascending
- Process the first 50 events; skip the rest (they will be picked up in future cycles
  if they enter the window or are modified)
- Events that fit within the 50-cap but are unchanged (FR-5) do not count against LLM
  calls — only new/changed events invoke the LLM

**Validation criteria:**
- Scanner with 60 events in window processes exactly 50 per cycle
- No LLM calls for unchanged events within the 50-event batch

---

## Config

```yaml
calendar_scanner:
  interval_seconds: 300          # scan every 5 minutes
  lookback_days: 7               # events up to N days in the past
  forward_days: 7                # events up to N days in the future
  skip_calendars: []             # calendar names to exclude (e.g. ["Birthdays", "Holidays"])
  max_events_per_cycle: 50
```

No env vars required. Calendar.app data is local; no API credentials needed.

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `specs/feat-calendar-scanner.md` | **This spec** |
| `calendar_scanner.py` | **Create** — CalendarScanner class with SQLite + AppleScript paths |
| `daemon.py` | Add CalendarScanner to full-role gather (loop 9) |
| `config.yaml.template` | Add `calendar_scanner` section |
| `install.sh` | Add `calendar_scanner.py` to DAEMON_FILES |
| `CLAUDE.md` | Update to nine async loops, add CalendarScanner description |
| `README.md` | Document ninth loop, Calendar.app permissions, skip_calendars config |
| `tests/unit/test_calendar_scanner.py` | **Create** |
| `tests/integration/test_pipeline.py` | Add calendar scanner integration test |

---

## Unit Tests (`tests/unit/test_calendar_scanner.py`)

| Test | Assertion |
|------|-----------|
| `test_find_calendar_cache_primary_path` | Returns path when Calendar Cache exists at primary location |
| `test_find_calendar_cache_group_container_path` | Falls back to group container path |
| `test_find_calendar_cache_missing_returns_none` | Returns None when neither path exists |
| `test_convert_core_data_timestamp` | cd_ts + 978307200 = correct Unix datetime |
| `test_convert_core_data_timestamp_zero` | cd_ts=0 → 2001-01-01T00:00:00 |
| `test_events_within_window_included` | Events in ±7-day window returned |
| `test_events_outside_window_excluded` | Events beyond window filtered out |
| `test_declined_events_excluded` | ZMYATTENDEESTATUS=3 events not returned |
| `test_all_day_event_detection` | ZISALLDAY=1 → `all_day: true` in frontmatter |
| `test_recurring_event_flag` | ZHASRECURRENCERULES=1 → `recurrence: true` |
| `test_attendee_extraction` | ZATTENDEE rows mapped to participants list |
| `test_skip_calendars_filtered` | Calendar name in skip_calendars → event excluded |
| `test_change_detection_same_modified` | Same ZMODIFIEDDATE → no LLM call, no write |
| `test_change_detection_updated_event` | New ZMODIFIEDDATE → LLM call + file write |
| `test_new_event_written` | Event not in state → LLM call + file write |
| `test_filename_format` | Filename matches calendar-event-{date}-{slug}-{hash}.md |
| `test_slugify_special_chars` | Punctuation and spaces cleaned from slug |
| `test_write_memory_atomic` | No .tmp file left after write |
| `test_write_memory_type` | `type: calendar_event` in frontmatter |
| `test_write_memory_field_order` | `source_title` first in frontmatter |
| `test_source_url_scheme` | `source_url` starts with `calendar:` |
| `test_rate_limit_50_per_cycle` | 60 events in window → exactly 50 processed |
| `test_state_file_created_on_first_run` | No existing state → state file created |
| `test_state_file_persists_across_scans` | State survives simulated restart |
| `test_state_file_pruned_at_5000` | Processed map capped at 5000 entries |
| `test_applescript_fallback_triggered` | Missing Calendar Cache → AppleScript path taken |
| `test_applescript_output_parsed` | `|||`-delimited output produces correct event dicts |
| `test_applescript_timeout_kills_process` | Timeout kills subprocess, no hang |
