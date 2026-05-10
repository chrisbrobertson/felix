"""
Unit tests for calendar_scanner.

All external access (SQLite, osascript, LiteLLM, filesystem) is mocked.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import yaml

import calendar_scanner as cs
from calendar_scanner import (
    CalendarScanner,
    CalendarCacheSource,
    AppleScriptSource,
    _cd_to_datetime,
    _datetime_to_cd,
    _slugify,
    _event_hash,
    _parse_frontmatter,
    CORE_DATA_EPOCH_OFFSET,
)


@pytest.fixture(autouse=True)
def _isolate_calendar_state_file(monkeypatch, tmp_path_factory):
    """Redirect calendar_scanner.STATE_FILE and MEMORIES_DIR to per-test tmp paths.

    Test pollution of the production state file (observed April 2026: 28
    orphan keys with literal `test-host` hostname) and of the production
    memories directory (observed April 2026: `test_filename_format`
    instantiated CalendarScanner() with `_hostname="test-host"` but forgot
    to patch MEMORIES_DIR, so after v1.6.1's migration fix the production
    cleanup ran during pytest and renamed 11 real calendar-event files to a
    `test-host-` prefix) is prevented at the fixture layer rather than
    relying on every test to remember both patches. Tests that set either
    constant explicitly via `patch.object` still work because their inner
    patch supersedes this outer autouse patch.
    """
    ghost_state = tmp_path_factory.mktemp("calendar-state") / "calendar-scanner-state.json"
    ghost_memories = tmp_path_factory.mktemp("calendar-memories")
    monkeypatch.setattr(cs, "STATE_FILE", ghost_state, raising=False)
    monkeypatch.setattr(cs, "MEMORIES_DIR", ghost_memories, raising=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_event(
    pk=1,
    title="Team Standup",
    start_time=None,
    end_time=None,
    modified_time=None,
    location="Zoom",
    notes="",
    all_day=False,
    recurring=False,
    calendar_name="Work",
    external_id="abc123",
    participants=None
):
    if start_time is None:
        start_time = datetime(2026, 4, 11, 9, 0, 0)
    if end_time is None:
        end_time = datetime(2026, 4, 11, 9, 30, 0)
    if modified_time is None:
        modified_time = datetime(2026, 4, 10, 10, 0, 0)
    if participants is None:
        participants = [{"name": "Chris Robertson", "email": "chris@example.com"}]

    return {
        "pk": pk,
        "title": title,
        "start_time": start_time,
        "end_time": end_time,
        "modified_time": modified_time,
        "location": location,
        "notes": notes,
        "all_day": all_day,
        "recurring": recurring,
        "calendar_name": calendar_name,
        "external_id": external_id,
        "participants": participants,
    }


# ── Core Data timestamp conversion ───────────────────────────────────────────

def test_convert_core_data_timestamp():
    # Core Data ts = 0 should give 2001-01-01 00:00:00
    result = _cd_to_datetime(0)
    assert result.year == 2001
    assert result.month == 1
    assert result.day == 1


def test_convert_core_data_timestamp_zero():
    result = _cd_to_datetime(0)
    assert result == datetime(2001, 1, 1, 0, 0, 0)


def test_convert_core_data_timestamp_recent():
    # 2026-04-11 00:00:00 UTC → unix ts = 1775865600
    # core data ts = 1775865600 - 978307200 = 797558400
    core_ts = 797558400
    result = _cd_to_datetime(core_ts)
    assert result.year == 2026
    assert result.month == 4
    assert result.day == 11


def test_datetime_to_cd_roundtrip():
    dt = datetime(2026, 4, 11, 12, 0, 0)
    core_ts = _datetime_to_cd(dt)
    result = _cd_to_datetime(core_ts)
    assert result.year == dt.year
    assert result.month == dt.month
    assert result.day == dt.day
    assert result.hour == dt.hour


# ── CalendarCacheSource._find_db_path ─────────────────────────────────────────

def test_find_calendar_cache_primary_path(tmp_path):
    """Returns path when Calendar Cache exists at primary location."""
    with patch.object(Path, 'home', return_value=tmp_path):
        cal_dir = tmp_path / "Library" / "Calendars"
        cal_dir.mkdir(parents=True)
        cache = cal_dir / "Calendar Cache"
        cache.touch()

        result = CalendarCacheSource._find_db_path()
        assert result == cache


def test_find_calendar_cache_group_container_path(tmp_path):
    """Falls back to group container path."""
    with patch.object(Path, 'home', return_value=tmp_path):
        group_dir = tmp_path / "Library" / "Group Containers" / "group.com.apple.calendar"
        group_dir.mkdir(parents=True)
        cache = group_dir / "Calendar Cache"
        cache.touch()

        result = CalendarCacheSource._find_db_path()
        assert result == cache


def test_find_calendar_cache_missing_returns_none(tmp_path):
    """Returns None when neither path exists."""
    with patch.object(Path, 'home', return_value=tmp_path):
        result = CalendarCacheSource._find_db_path()
        assert result is None


def test_find_db_path_prefers_calendar_sqlitedb(tmp_path):
    """Modern macOS `Calendar.sqlitedb` takes precedence over legacy candidates."""
    with patch.object(Path, 'home', return_value=tmp_path):
        cal_dir = tmp_path / "Library" / "Calendars"
        cal_dir.mkdir(parents=True)
        sqlitedb = cal_dir / "Calendar.sqlitedb"
        sqlitedb.touch()
        # Legacy names also present — new name must win.
        legacy = cal_dir / "Calendar Cache"
        legacy.touch()

        result = CalendarCacheSource._find_db_path()
        assert result == sqlitedb


def test_sqlite_source_falls_back_when_table_missing(tmp_path):
    """`create()` returns None when expected ZCALENDARITEM table is absent."""
    with patch.object(Path, 'home', return_value=tmp_path):
        cal_dir = tmp_path / "Library" / "Calendars"
        cal_dir.mkdir(parents=True)
        sqlitedb = cal_dir / "Calendar.sqlitedb"
        # Create a valid SQLite DB but without the legacy schema table.
        conn = sqlite3.connect(str(sqlitedb))
        conn.execute("CREATE TABLE CalendarItem (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        result = CalendarCacheSource.create()
        assert result is None


def test_sqlite_source_created_when_legacy_schema_present(tmp_path):
    """`create()` returns a CalendarCacheSource when ZCALENDARITEM table exists."""
    with patch.object(Path, 'home', return_value=tmp_path):
        cal_dir = tmp_path / "Library" / "Calendars"
        cal_dir.mkdir(parents=True)
        sqlitedb = cal_dir / "Calendar.sqlitedb"
        conn = sqlite3.connect(str(sqlitedb))
        conn.execute("CREATE TABLE ZCALENDARITEM (Z_PK INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        result = CalendarCacheSource.create()
        assert result is not None
        assert result._db_path == sqlitedb


# ── Event filtering ───────────────────────────────────────────────────────────

def test_events_within_window_included(tmp_path):
    """Events in ±7-day window returned."""
    # Create a mock database
    db_path = tmp_path / "Calendar Cache"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE ZCALENDAR (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE ZATTENDEE (
            ZCOMMONNAME TEXT,
            ZADDRESS TEXT,
            ZCALENDARITEM INTEGER
        )
    """)
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Work')")

    # Event within window
    now = datetime.now()
    event_start = now + timedelta(days=3)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + timedelta(hours=1))
    modified_cd = _datetime_to_cd(now)

    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'Meeting', ?, ?, ?, 'Office', 'Notes', 0, 0, 0, 'ext1', 1)",
        (start_cd, end_cd, modified_cd)
    )
    conn.commit()
    conn.close()

    source = CalendarCacheSource(db_path)
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=7)

    with patch.object(source, '_copy_db', return_value=(db_path, [])):
        events = source.get_events(start_date, end_date, set())

    assert len(events) == 1
    assert events[0]["title"] == "Meeting"


def test_events_outside_window_excluded(tmp_path):
    """Events beyond window filtered out."""
    db_path = tmp_path / "Calendar Cache"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ZCALENDAR (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)")
    conn.execute("""
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        )
    """)
    conn.execute("CREATE TABLE ZATTENDEE (ZCOMMONNAME TEXT, ZADDRESS TEXT, ZCALENDARITEM INTEGER)")
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Work')")

    # Event far in the future
    now = datetime.now()
    event_start = now + timedelta(days=30)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + timedelta(hours=1))
    modified_cd = _datetime_to_cd(now)

    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'Future Meeting', ?, ?, ?, '', '', 0, 0, 0, 'ext1', 1)",
        (start_cd, end_cd, modified_cd)
    )
    conn.commit()
    conn.close()

    source = CalendarCacheSource(db_path)
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=7)

    with patch.object(source, '_copy_db', return_value=(db_path, [])):
        events = source.get_events(start_date, end_date, set())

    assert len(events) == 0


def test_declined_events_excluded(tmp_path):
    """ZMYATTENDEESTATUS=3 events not returned."""
    db_path = tmp_path / "Calendar Cache"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ZCALENDAR (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)")
    conn.execute("""
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        )
    """)
    conn.execute("CREATE TABLE ZATTENDEE (ZCOMMONNAME TEXT, ZADDRESS TEXT, ZCALENDARITEM INTEGER)")
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Work')")

    now = datetime.now()
    event_start = now + timedelta(days=1)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + timedelta(hours=1))
    modified_cd = _datetime_to_cd(now)

    # Declined event
    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'Declined', ?, ?, ?, '', '', 0, 0, 3, 'ext1', 1)",
        (start_cd, end_cd, modified_cd)
    )
    conn.commit()
    conn.close()

    source = CalendarCacheSource(db_path)
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=7)

    with patch.object(source, '_copy_db', return_value=(db_path, [])):
        events = source.get_events(start_date, end_date, set())

    assert len(events) == 0


def test_all_day_event_detection(tmp_path):
    """ZISALLDAY=1 → `all_day: true` in frontmatter."""
    db_path = tmp_path / "Calendar Cache"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ZCALENDAR (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)")
    conn.execute("""
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        )
    """)
    conn.execute("CREATE TABLE ZATTENDEE (ZCOMMONNAME TEXT, ZADDRESS TEXT, ZCALENDARITEM INTEGER)")
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Personal')")

    now = datetime.now()
    event_start = now + timedelta(days=1)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + timedelta(days=1))
    modified_cd = _datetime_to_cd(now)

    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'All Day Event', ?, ?, ?, '', '', 1, 0, 0, 'ext1', 1)",
        (start_cd, end_cd, modified_cd)
    )
    conn.commit()
    conn.close()

    source = CalendarCacheSource(db_path)
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=7)

    with patch.object(source, '_copy_db', return_value=(db_path, [])):
        events = source.get_events(start_date, end_date, set())

    assert len(events) == 1
    assert events[0]["all_day"] is True


def test_recurring_event_flag(tmp_path):
    """ZHASRECURRENCERULES=1 → `recurrence: true`."""
    db_path = tmp_path / "Calendar Cache"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ZCALENDAR (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)")
    conn.execute("""
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        )
    """)
    conn.execute("CREATE TABLE ZATTENDEE (ZCOMMONNAME TEXT, ZADDRESS TEXT, ZCALENDARITEM INTEGER)")
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Work')")

    now = datetime.now()
    event_start = now + timedelta(days=1)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + timedelta(hours=1))
    modified_cd = _datetime_to_cd(now)

    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'Recurring Meeting', ?, ?, ?, '', '', 0, 1, 0, 'ext1', 1)",
        (start_cd, end_cd, modified_cd)
    )
    conn.commit()
    conn.close()

    source = CalendarCacheSource(db_path)
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=7)

    with patch.object(source, '_copy_db', return_value=(db_path, [])):
        events = source.get_events(start_date, end_date, set())

    assert len(events) == 1
    assert events[0]["recurring"] is True


def test_attendee_extraction(tmp_path):
    """ZATTENDEE rows mapped to participants list."""
    db_path = tmp_path / "Calendar Cache"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ZCALENDAR (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)")
    conn.execute("""
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        )
    """)
    conn.execute("CREATE TABLE ZATTENDEE (ZCOMMONNAME TEXT, ZADDRESS TEXT, ZCALENDARITEM INTEGER)")
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Work')")

    now = datetime.now()
    event_start = now + timedelta(days=1)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + timedelta(hours=1))
    modified_cd = _datetime_to_cd(now)

    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'Team Meeting', ?, ?, ?, '', '', 0, 0, 0, 'ext1', 1)",
        (start_cd, end_cd, modified_cd)
    )
    conn.execute("INSERT INTO ZATTENDEE VALUES ('Alice', 'alice@example.com', 1)")
    conn.execute("INSERT INTO ZATTENDEE VALUES ('Bob', 'bob@example.com', 1)")
    conn.commit()
    conn.close()

    source = CalendarCacheSource(db_path)
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=7)

    with patch.object(source, '_copy_db', return_value=(db_path, [])):
        events = source.get_events(start_date, end_date, set())

    assert len(events) == 1
    participants = events[0]["participants"]
    assert len(participants) == 2
    assert {"name": "Alice", "email": "alice@example.com"} in participants
    assert {"name": "Bob", "email": "bob@example.com"} in participants


def test_skip_calendars_filtered(tmp_path):
    """Calendar name in skip_calendars → event excluded."""
    db_path = tmp_path / "Calendar Cache"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ZCALENDAR (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)")
    conn.execute("""
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        )
    """)
    conn.execute("CREATE TABLE ZATTENDEE (ZCOMMONNAME TEXT, ZADDRESS TEXT, ZCALENDARITEM INTEGER)")
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Birthdays')")

    now = datetime.now()
    event_start = now + timedelta(days=1)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + timedelta(hours=1))
    modified_cd = _datetime_to_cd(now)

    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'Birthday', ?, ?, ?, '', '', 0, 0, 0, 'ext1', 1)",
        (start_cd, end_cd, modified_cd)
    )
    conn.commit()
    conn.close()

    source = CalendarCacheSource(db_path)
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=7)

    with patch.object(source, '_copy_db', return_value=(db_path, [])):
        events = source.get_events(start_date, end_date, {"birthdays"})

    assert len(events) == 0


def test_null_modified_date_stable(tmp_path):
    """NULL ZMODIFIEDDATE → stable modified_time of datetime(2001,1,1), not datetime.now().

    When ZMODIFIEDDATE is NULL (as is common for all-day and recurring events),
    _cd_to_datetime returns datetime(2001,1,1) — a fixed epoch. The old code
    used datetime.now() as fallback, which caused events to be re-processed
    every scan cycle because the stored modified_str never matched the next
    cycle's datetime.now() value (#126).
    """
    db_path = tmp_path / "Calendar Cache"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE ZCALENDAR (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT)")
    conn.execute("""
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        )
    """)
    conn.execute("CREATE TABLE ZATTENDEE (ZCOMMONNAME TEXT, ZADDRESS TEXT, ZCALENDARITEM INTEGER)")
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Work')")

    now = datetime.now()
    event_start = now + timedelta(days=1)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + timedelta(hours=1))

    # Insert event with NULL ZMODIFIEDDATE (None → SQLite NULL)
    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'All Day Event', ?, ?, NULL, '', '', 1, 0, 0, 'ext1', 1)",
        (start_cd, end_cd)
    )
    conn.commit()
    conn.close()

    # Make a copy so the finally block in get_events can delete it without losing the DB
    db_copy = tmp_path / "Calendar Cache Copy"
    import shutil as _shutil
    _shutil.copy2(str(db_path), str(db_copy))

    source = CalendarCacheSource(db_copy)
    start_date = now - timedelta(days=7)
    end_date = now + timedelta(days=7)

    with patch.object(source, '_copy_db', return_value=(db_copy, [])):
        events = source.get_events(start_date, end_date, set())

    assert len(events) == 1
    # modified_time must be the stable epoch date, not near-current time
    assert events[0]["modified_time"] == datetime(2001, 1, 1)


# ── Change detection ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_detection_same_modified(tmp_path):
    """Same ZMODIFIEDDATE → no LLM call, no write."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    state_file = tmp_path / "calendar-scanner-state.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar_scanner:\n  interval_seconds: 300\n")

    event = make_event()
    modified_str = event["modified_time"].isoformat()

    # Pre-populate state using hostname-scoped filename
    state = {
        "processed": {
            f"calendar-event-test-host-2026-04-11-team-standup-{_event_hash('', 'Team Standup', event['start_time'].strftime('%Y-%m-%dT%H:%M'))}.md": modified_str
        }
    }
    state_file.write_text(json.dumps(state))

    with patch.object(cs, 'MEMORIES_DIR', memories_dir), \
         patch.object(cs, 'STATE_FILE', state_file), \
         patch.object(cs, 'CONFIG_PATH', config_path), \
         patch.object(cs, '_hostname', return_value='test-host'), \
         patch('calendar_scanner.CalendarDataSource.detect') as mock_detect, \
         patch('litellm.acompletion', new_callable=AsyncMock) as mock_llm:

        mock_source = MagicMock()
        mock_source.get_events.return_value = [event]
        mock_detect.return_value = mock_source

        scanner = CalendarScanner()
        await scanner._run_scan()

        # LLM should NOT be called
        mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_change_detection_updated_event(tmp_path):
    """New ZMODIFIEDDATE → LLM call + file write."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    state_file = tmp_path / "calendar-scanner-state.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar_scanner:\n  interval_seconds: 300\n")

    event = make_event()
    old_modified = datetime(2026, 4, 9, 10, 0, 0).isoformat()

    # Pre-populate state with old modified time (hostname-scoped filename)
    state = {
        "processed": {
            f"calendar-event-test-host-2026-04-11-team-standup-{_event_hash('', 'Team Standup', event['start_time'].strftime('%Y-%m-%dT%H:%M'))}.md": old_modified
        }
    }
    state_file.write_text(json.dumps(state))

    with patch.object(cs, 'MEMORIES_DIR', memories_dir), \
         patch.object(cs, 'STATE_FILE', state_file), \
         patch.object(cs, 'CONFIG_PATH', config_path), \
         patch.object(cs, '_hostname', return_value='test-host'), \
         patch('calendar_scanner.CalendarDataSource.detect') as mock_detect, \
         patch('litellm.acompletion', new_callable=AsyncMock) as mock_llm:

        mock_source = MagicMock()
        mock_source.get_events.return_value = [event]
        mock_detect.return_value = mock_source

        mock_llm.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content='{"summary": "Test summary", "tags": ["test", "meeting"]}'
            ))]
        )

        scanner = CalendarScanner()
        await scanner._run_scan()

        # LLM should be called
        mock_llm.assert_called_once()

        # File should exist (hostname-scoped)
        expected_file = memories_dir / f"calendar-event-test-host-2026-04-11-team-standup-{_event_hash('', 'Team Standup', event['start_time'].strftime('%Y-%m-%dT%H:%M'))}.md"
        assert expected_file.exists()


@pytest.mark.asyncio
async def test_new_event_written(tmp_path):
    """Event not in state → LLM call + file write."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    state_file = tmp_path / "calendar-scanner-state.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar_scanner:\n  interval_seconds: 300\n")

    event = make_event()

    with patch.object(cs, 'MEMORIES_DIR', memories_dir), \
         patch.object(cs, 'STATE_FILE', state_file), \
         patch.object(cs, 'CONFIG_PATH', config_path), \
         patch.object(cs, '_hostname', return_value='test-host'), \
         patch('calendar_scanner.CalendarDataSource.detect') as mock_detect, \
         patch('litellm.acompletion', new_callable=AsyncMock) as mock_llm:

        mock_source = MagicMock()
        mock_source.get_events.return_value = [event]
        mock_detect.return_value = mock_source

        mock_llm.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content='{"summary": "New event summary", "tags": ["new", "event"]}'
            ))]
        )

        scanner = CalendarScanner()
        await scanner._run_scan()

        # LLM should be called
        mock_llm.assert_called_once()

        # File should exist (hostname-scoped)
        expected_file = memories_dir / f"calendar-event-test-host-2026-04-11-team-standup-{_event_hash('', 'Team Standup', event['start_time'].strftime('%Y-%m-%dT%H:%M'))}.md"
        assert expected_file.exists()


# ── File formatting ───────────────────────────────────────────────────────────

def test_filename_format():
    """Filename matches calendar-event-{hostname}-{date}-{slug}-{hash}.md."""
    event = make_event(
        title="Team Standup",
        start_time=datetime(2026, 4, 11, 9, 0, 0),
        external_id="abc123"
    )
    with patch.object(cs, "_hostname", return_value="test-host"):
        scanner = CalendarScanner()
        path = scanner._memory_path(event)

    assert path.name.startswith("calendar-event-test-host-2026-04-11-team-standup-")
    assert path.name.endswith(".md")
    assert len(path.name.split("-")[-1].replace(".md", "")) == 8  # 8-char hash


def test_slugify_special_chars():
    """Punctuation and spaces cleaned from slug."""
    assert _slugify("Team Standup!") == "team-standup"
    assert _slugify("Q1 Review (2026)") == "q1-review-2026"
    assert _slugify("Follow-up: Meeting Notes") == "follow-up-meeting-notes"


def test_write_memory_atomic(tmp_path):
    """No .tmp file left after write."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    event = make_event()

    with patch.object(cs, 'MEMORIES_DIR', memories_dir):
        scanner = CalendarScanner()
        scanner._write_memory(event, "Summary text", ["tag1", "tag2"])

    # Check no .tmp files
    tmp_files = list(memories_dir.glob("*.tmp"))
    assert len(tmp_files) == 0


def test_write_memory_type(tmp_path):
    """`type: calendar_event` in frontmatter."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    event = make_event()

    with patch.object(cs, 'MEMORIES_DIR', memories_dir):
        scanner = CalendarScanner()
        scanner._write_memory(event, "Summary", ["tag"])

        path = scanner._memory_path(event)
        fm = _parse_frontmatter(path.read_text())
        assert fm["type"] == "calendar_event"


def test_write_memory_field_order(tmp_path):
    """`source_title` first in frontmatter."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    event = make_event()

    with patch.object(cs, 'MEMORIES_DIR', memories_dir):
        scanner = CalendarScanner()
        scanner._write_memory(event, "Summary", ["tag"])

        path = scanner._memory_path(event)
        content = path.read_text()
        # Parse frontmatter manually
        parts = content.split("---")
        fm_text = parts[1]
        first_key = fm_text.strip().split("\n")[0].split(":")[0]
        assert first_key == "source_title"


def test_source_url_scheme(tmp_path):
    """`source_url` starts with `calendar:`."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    event = make_event()

    with patch.object(cs, 'MEMORIES_DIR', memories_dir):
        scanner = CalendarScanner()
        scanner._write_memory(event, "Summary", ["tag"])

        path = scanner._memory_path(event)
        fm = _parse_frontmatter(path.read_text())
        assert fm["source_url"].startswith("calendar:")


# ── Rate limiting ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_50_per_cycle(tmp_path):
    """60 events in window → exactly 50 processed."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    state_file = tmp_path / "calendar-scanner-state.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar_scanner:\n  max_events_per_cycle: 50\n")

    # Generate 60 events
    events = [
        make_event(
            pk=i,
            title=f"Event {i}",
            external_id=f"ext{i}",
            start_time=datetime(2026, 4, 11, 9, 0, 0) + timedelta(hours=i)
        )
        for i in range(60)
    ]

    with patch.object(cs, 'MEMORIES_DIR', memories_dir), \
         patch.object(cs, 'STATE_FILE', state_file), \
         patch.object(cs, 'CONFIG_PATH', config_path), \
         patch('calendar_scanner.CalendarDataSource.detect') as mock_detect, \
         patch('litellm.acompletion', new_callable=AsyncMock) as mock_llm:

        mock_source = MagicMock()
        mock_source.get_events.return_value = events
        mock_detect.return_value = mock_source

        mock_llm.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content='{"summary": "Summary", "tags": ["test"]}'
            ))]
        )

        scanner = CalendarScanner()
        await scanner._run_scan()

        # Exactly 50 files should be written
        written_files = list(memories_dir.glob("calendar-event-*.md"))
        assert len(written_files) == 50


# ── State management ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_file_created_on_first_run(tmp_path):
    """No existing state → state file created."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    state_file = tmp_path / "calendar-scanner-state.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar_scanner:\n  interval_seconds: 300\n")

    with patch.object(cs, 'MEMORIES_DIR', memories_dir), \
         patch.object(cs, 'STATE_FILE', state_file), \
         patch.object(cs, 'CONFIG_PATH', config_path), \
         patch('calendar_scanner.CalendarDataSource.detect') as mock_detect:

        mock_source = MagicMock()
        mock_source.get_events.return_value = []
        mock_detect.return_value = mock_source

        scanner = CalendarScanner()
        await scanner._run_scan()

        assert state_file.exists()


@pytest.mark.asyncio
async def test_state_file_persists_across_scans(tmp_path):
    """State survives simulated restart."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    state_file = tmp_path / "calendar-scanner-state.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar_scanner:\n  interval_seconds: 300\n")

    event = make_event()

    with patch.object(cs, 'MEMORIES_DIR', memories_dir), \
         patch.object(cs, 'STATE_FILE', state_file), \
         patch.object(cs, 'CONFIG_PATH', config_path), \
         patch('calendar_scanner.CalendarDataSource.detect') as mock_detect, \
         patch('litellm.acompletion', new_callable=AsyncMock) as mock_llm:

        mock_source = MagicMock()
        mock_source.get_events.return_value = [event]
        mock_detect.return_value = mock_source

        mock_llm.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content='{"summary": "Summary", "tags": ["test"]}'
            ))]
        )

        # First scan
        scanner1 = CalendarScanner()
        await scanner1._run_scan()

        # Simulated restart - new scanner instance
        scanner2 = CalendarScanner()
        state = scanner2._load_state()

        assert "processed" in state
        assert len(state["processed"]) > 0


def test_state_file_pruned_at_5000(tmp_path):
    """Processed map capped at 5000 entries."""
    state_file = tmp_path / "calendar-scanner-state.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("calendar_scanner:\n  interval_seconds: 300\n")

    # Create state with 5100 entries
    state = {
        "processed": {
            f"calendar-event-2026-04-{i%30+1:02d}-event-{i}-hash{i:04d}.md": f"2026-04-{i%30+1:02d}T10:00:00"
            for i in range(5100)
        }
    }

    with patch.object(cs, 'STATE_FILE', state_file), \
         patch.object(cs, 'CONFIG_PATH', config_path):

        scanner = CalendarScanner()
        scanner._save_state(state)

        # Reload and check
        loaded = scanner._load_state()
        assert len(loaded["processed"]) == 5000


# ── AppleScript fallback ──────────────────────────────────────────────────────

def test_applescript_fallback_triggered(tmp_path):
    """Missing Calendar Cache and no EventKit → AppleScript path taken."""
    with patch.object(Path, 'home', return_value=tmp_path), \
         patch.object(cs.EventKitSource, 'create', return_value=None):
        source = cs.CalendarDataSource.detect()
        assert isinstance(source, AppleScriptSource)


def test_applescript_output_parsed():
    """`|||`-delimited output produces correct event dicts."""
    raw = """Team Standup|||Friday, April 11, 2026 at 9:00:00 AM|||Friday, April 11, 2026 at 9:30:00 AM|||Zoom|||Work|||
Q1 Review|||Monday, April 14, 2026 at 2:00:00 PM|||Monday, April 14, 2026 at 3:00:00 PM|||Conference Room|||Work|||
"""
    source = AppleScriptSource()

    with patch.object(source, '_run_osascript', return_value=raw):
        events = source.get_events(
            datetime.now() - timedelta(days=7),
            datetime.now() + timedelta(days=7),
            set()
        )

    assert len(events) == 2
    assert events[0]["title"] == "Team Standup"
    assert events[0]["location"] == "Zoom"
    assert events[1]["title"] == "Q1 Review"


def test_applescript_timeout_kills_process():
    """Timeout kills subprocess, no hang."""
    source = AppleScriptSource()

    def slow_script(*args, **kwargs):
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("osascript", 60)
        return proc

    with patch('subprocess.Popen', side_effect=slow_script):
        result = source._run_osascript("test script", timeout=1)
        assert result == ""


# ── Backfill ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_clears_processed_map_and_widens_window(tmp_path):
    """backfill() clears processed map and uses wider window."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(cs, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(cs, "CalendarDataSource") as mock_cds, \
         patch("litellm.acompletion", new=AsyncMock()):

        scanner = CalendarScanner(role="full")

        mock_source = MagicMock()
        mock_source.get_events.return_value = []
        mock_cds.detect.return_value = mock_source

        (tmp_path / "config.yaml").write_text("calendar_scanner:\n  skip_calendars: []\n")
        scanner._save_state({"processed": {"old-event.md": "2026-04-01T00:00:00"}})

        result = await scanner.backfill(30)

        # State should have empty processed map after backfill
        state = scanner._load_state()
        assert state.get("processed") == {}
        assert result["processed"] == 0


# ── Hostname in filename / frontmatter ────────────────────────────────────────

def test_write_memory_hostname_in_frontmatter(tmp_path):
    """`hostname:` field written to frontmatter so per-machine provenance is visible."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)
    event = make_event()

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "_hostname", return_value="my-laptop"):
        scanner = CalendarScanner()
        scanner._write_memory(event, "Summary", ["tag"])
        path = scanner._memory_path(event)
        fm = _parse_frontmatter(path.read_text())

    assert fm.get("hostname") == "my-laptop"


def test_memory_path_hostname_scoped(tmp_path):
    """_memory_path() embeds hostname so events from different machines don't collide."""
    event = make_event(title="Standup", start_time=datetime(2026, 4, 11, 9, 0))

    with patch.object(cs, "MEMORIES_DIR", tmp_path), \
         patch.object(cs, "_hostname", return_value="mac-studio"):
        scanner = CalendarScanner()
        path = scanner._memory_path(event)

    assert "mac-studio" in path.name


# ── Legacy filename migration ─────────────────────────────────────────────────

def test_migrate_calendar_filenames_renames_legacy_files(tmp_path):
    """calendar-event-{date}-*.md renamed to calendar-event-{hostname}-{date}-*.md on init."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    # Legacy file: no hostname in name, no hostname in frontmatter
    legacy = memories_dir / "calendar-event-2026-04-11-standup-abc12345.md"
    legacy.write_text("---\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="mac-studio"):
        CalendarScanner()  # migration runs in __init__

    # Legacy file should be gone; new hostname-scoped file should exist
    assert not legacy.exists()
    scoped = memories_dir / "calendar-event-mac-studio-2026-04-11-standup-abc12345.md"
    assert scoped.exists()


def test_migrate_calendar_filenames_updates_state(tmp_path):
    """State file remaps old filename key to new hostname-scoped key."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    legacy_name = "calendar-event-2026-04-11-standup-abc12345.md"
    legacy = memories_dir / legacy_name
    legacy.write_text("---\ntype: calendar_event\n---\n\n## Details\n")

    old_modified = "2026-04-11T08:00:00"
    state_file.write_text(json.dumps({"processed": {legacy_name: old_modified}}))

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="mac-studio"):
        CalendarScanner()

    state = json.loads(state_file.read_text())
    new_name = "calendar-event-mac-studio-2026-04-11-standup-abc12345.md"
    assert new_name in state["processed"]
    assert legacy_name not in state["processed"]
    assert state["processed"][new_name] == old_modified


def test_migrate_calendar_filenames_skips_other_host_files(tmp_path):
    """Files with a different hostname in frontmatter are left untouched."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    other_host_file = memories_dir / "calendar-event-other-host-2026-04-11-standup-abc12345.md"
    other_host_file.write_text("---\nhostname: other-host\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="mac-studio"):
        CalendarScanner()

    # Other host's file should not be touched
    assert other_host_file.exists()


def test_migrate_calendar_filenames_idempotent(tmp_path):
    """Already hostname-scoped files are not renamed on second init."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    scoped = memories_dir / "calendar-event-mac-studio-2026-04-11-standup-abc12345.md"
    scoped.write_text("---\nhostname: mac-studio\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="mac-studio"):
        CalendarScanner()
        CalendarScanner()  # second init should be a no-op

    assert scoped.exists()
    # No duplicate or double-prefixed file
    assert len(list(memories_dir.glob("calendar-event-*.md"))) == 1


# ── Hostname-stacking cleanup (v1.6.0) ────────────────────────────────────────

def test_cleanup_collapses_stacked_hostname_to_canonical(tmp_path):
    """Stacked-hostname filename collapses to canonical once frontmatter hostname exists."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    stacked = memories_dir / (
        "calendar-event-host-host-test-host-host-2026-04-11-meeting-abc123.md"
    )
    stacked.write_text("---\nhostname: host\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="host"):
        CalendarScanner()

    canonical = memories_dir / "calendar-event-host-2026-04-11-meeting-abc123.md"
    assert canonical.exists()
    assert not stacked.exists()


def test_cleanup_handles_overlong_stacked_filename(tmp_path):
    """Stacked filenames near the 255-byte component limit must still collapse.

    Regression: before the reorder fix, the cleanup called
    ``_stamp_hostname_in_frontmatter`` on the stacked path BEFORE renaming it
    to canonical form. That helper writes a ``.md.tmp`` sibling whose filename
    is 4 chars longer than the source, pushing any ~252+ char stacked name
    past the APFS 255-byte per-component limit → ``OSError(63, 'File name too
    long')`` → silently swallowed → file remained stacked forever.
    """
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    # Build a 252-char stacked filename (matches the real production shape).
    stacked_stem = "calendar-event-" + ("test-host-Chriss-MacBook-Air-" * 7) + "2026-04-10-meeting-abc12345"
    stacked_name = stacked_stem + ".md"
    assert 248 <= len(stacked_name) <= 255
    stacked = memories_dir / stacked_name
    # No hostname in frontmatter → forces the stamp-then-rename code path.
    stacked.write_text("---\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="Chriss-MacBook-Air"):
        CalendarScanner()

    canonical = memories_dir / "calendar-event-Chriss-MacBook-Air-2026-04-10-meeting-abc12345.md"
    assert canonical.exists(), "canonical file should exist after cleanup"
    assert not stacked.exists(), "stacked file should have been renamed away"
    fm = _parse_frontmatter(canonical.read_text())
    assert fm.get("hostname") == "Chriss-MacBook-Air"


def test_cleanup_stamps_missing_hostname_in_frontmatter(tmp_path):
    """Legacy file without hostname frontmatter gets hostname stamped during cleanup."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    legacy = memories_dir / "calendar-event-2026-04-11-standup-abc12345.md"
    legacy.write_text("---\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="current-host"):
        CalendarScanner()

    canonical = memories_dir / "calendar-event-current-host-2026-04-11-standup-abc12345.md"
    assert canonical.exists()
    fm = _parse_frontmatter(canonical.read_text())
    assert fm.get("hostname") == "current-host"


def test_cleanup_deletes_stacked_duplicate_when_canonical_exists(tmp_path):
    """When both stacked and canonical names exist on disk, stacked is deleted."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    canonical = memories_dir / "calendar-event-host-2026-04-11-meeting-abc123.md"
    canonical.write_text("---\nhostname: host\ntype: calendar_event\n---\n\n## canonical\n")
    stacked = memories_dir / "calendar-event-host-host-2026-04-11-meeting-abc123.md"
    stacked.write_text("---\nhostname: host\ntype: calendar_event\n---\n\n## stacked\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="host"):
        CalendarScanner()

    assert canonical.exists()
    assert not stacked.exists()
    # Canonical content preserved (not overwritten)
    assert "canonical" in canonical.read_text()


def test_cleanup_respects_foreign_host_frontmatter(tmp_path):
    """Frontmatter hostname wins over runtime hostname during rename."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    # Stacked filename, but frontmatter says otherhost owns it.
    stacked = memories_dir / "calendar-event-otherhost-otherhost-2026-04-11-meeting-abc123.md"
    stacked.write_text("---\nhostname: otherhost\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="currenthost"):
        CalendarScanner()

    # Canonical uses the frontmatter hostname, not the runtime hostname.
    canonical = memories_dir / "calendar-event-otherhost-2026-04-11-meeting-abc123.md"
    assert canonical.exists()
    # Current-host name must NOT be created
    assert not (memories_dir / "calendar-event-currenthost-2026-04-11-meeting-abc123.md").exists()


def test_cleanup_sentinel_makes_migration_idempotent(tmp_path):
    """Second __init__ is a no-op: does not open, rename, or touch any file."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    canonical = memories_dir / "calendar-event-host-2026-04-11-meeting-abc123.md"
    canonical.write_text("---\nhostname: host\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="host"):
        CalendarScanner()  # first run stamps sentinel
        # Sentinel should now exist next to the state file
        sentinel = state_file.parent / cs.MIGRATION_SENTINEL_NAME
        assert sentinel.exists()

        # Corrupt the canonical file so that any re-entry into the migration
        # loop would raise. A no-op second __init__ must never read the file.
        canonical.write_bytes(b"\x00\x01\x02 not valid yaml")

        CalendarScanner()  # second run: no-op, must not touch canonical

        # File still holds the corrupted bytes — proof nothing was rewritten.
        assert canonical.read_bytes() == b"\x00\x01\x02 not valid yaml"


def test_cleanup_remaps_state_keys_to_canonical(tmp_path):
    """State keys for stacked filenames are remapped to canonical form."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    stacked_name = "calendar-event-host-host-2026-04-11-meeting-abc123.md"
    stacked = memories_dir / stacked_name
    stacked.write_text("---\nhostname: host\ntype: calendar_event\n---\n\n## Details\n")

    state_file.write_text(json.dumps({"processed": {stacked_name: "2026-04-11T09:00:00"}}))

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="host"):
        CalendarScanner()

    state = json.loads(state_file.read_text())
    canonical_name = "calendar-event-host-2026-04-11-meeting-abc123.md"
    assert canonical_name in state["processed"]
    assert stacked_name not in state["processed"]
    assert state["processed"][canonical_name] == "2026-04-11T09:00:00"


def test_cleanup_skips_file_without_date_pattern(tmp_path, caplog):
    """Malformed calendar-event filename is left on disk and logged as warning."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    malformed = memories_dir / "calendar-event-nothing-recognisable.md"
    malformed.write_text("---\ntype: calendar_event\n---\n\n## Details\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="host"), \
         caplog.at_level("WARNING", logger="calendar-scanner"):
        CalendarScanner()

    assert malformed.exists()
    assert any("no date pattern" in r.message for r in caplog.records)


def test_eventkit_rejects_writeonly_status(caplog):
    """EKAuthorizationStatusWriteOnly (4) must be rejected with a loud warning.

    macOS 14+ splits Calendar authorization into Add Only (4) and Full Access (5).
    Add Only silently returns [] from every query — the daemon happily logs
    "0 events" forever. This test guards the regression that landed on a
    watcher laptop where the grant had degraded to Add Only.
    """
    from calendar_scanner import EventKitSource
    import sys, types

    # Build a minimal mock EventKit module just for EKAuthorizationStatus.
    fake_ek = types.SimpleNamespace(
        EKEventStore=MagicMock(),
        EKEntityTypeEvent=0,
    )
    fake_ek.EKEventStore.authorizationStatusForEntityType_.return_value = 4

    with patch.dict(sys.modules, {"EventKit": fake_ek}), \
         caplog.at_level("WARNING", logger="calendar-scanner"):
        src = EventKitSource.create()

    assert src is None, "WriteOnly grant must not produce a usable source"
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("Add Only" in m and "WriteOnly" in m for m in warnings), warnings
    assert any("Full Access" in m.upper() or "FULL ACCESS" in m for m in warnings), warnings


def test_eventkit_accepts_fullaccess_status():
    """Status 5 (FullAccess, macOS 14+) must return a usable source."""
    from calendar_scanner import EventKitSource
    import sys, types

    fake_ek = types.SimpleNamespace(
        EKEventStore=MagicMock(),
        EKEntityTypeEvent=0,
    )
    fake_ek.EKEventStore.authorizationStatusForEntityType_.return_value = 5
    fake_ek.EKEventStore.alloc.return_value.init.return_value = MagicMock()

    with patch.dict(sys.modules, {"EventKit": fake_ek}):
        src = EventKitSource.create()

    assert src is not None


def test_eventkit_accepts_legacy_authorized_status():
    """Status 3 (legacy Authorized, pre-macOS 14) must still work."""
    from calendar_scanner import EventKitSource
    import sys, types

    fake_ek = types.SimpleNamespace(
        EKEventStore=MagicMock(),
        EKEntityTypeEvent=0,
    )
    fake_ek.EKEventStore.authorizationStatusForEntityType_.return_value = 3
    fake_ek.EKEventStore.alloc.return_value.init.return_value = MagicMock()

    with patch.dict(sys.modules, {"EventKit": fake_ek}):
        src = EventKitSource.create()

    assert src is not None


def test_eventkit_logs_warning_on_zero_events(caplog):
    """EventKitSource.get_events warns with calendar count when predicate yields zero events."""
    import sys, types
    from calendar_scanner import EventKitSource

    mock_store = MagicMock()
    mock_store.eventsMatchingPredicate_.return_value = []
    # Three visible calendars, but the predicate returned nothing.
    mock_store.calendarsForEntityType_.return_value = [MagicMock(), MagicMock(), MagicMock()]
    # predicateForEventsWithStartDate_endDate_calendars_ can return any sentinel.
    mock_store.predicateForEventsWithStartDate_endDate_calendars_.return_value = object()

    # get_events guards itself with `import EventKit, Foundation` so we must
    # provide stubs for both; otherwise it returns [] before logging anything.
    fake_foundation = types.SimpleNamespace(
        NSDate=MagicMock(
            dateWithTimeIntervalSince1970_=MagicMock(return_value=MagicMock())
        )
    )
    fake_ek = types.SimpleNamespace()

    src = EventKitSource(mock_store)
    start = datetime(2026, 4, 14)
    end = datetime(2026, 4, 28)

    with patch.dict(sys.modules, {"EventKit": fake_ek, "Foundation": fake_foundation}), \
         caplog.at_level("WARNING", logger="calendar-scanner"):
        events = src.get_events(start, end, set())

    assert events == []
    msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("0 events" in m and "3 calendars" in m for m in msgs), msgs


def test_production_state_file_never_written_during_tests(tmp_path):
    """Meta-test: the production STATE_FILE must not be created or modified.

    A test that forgets to redirect STATE_FILE has historically leaked 28
    orphan `test-host` keys into the live daemon's state (April 2026). The
    autouse `_isolate_calendar_state_file` fixture should make that
    impossible. This test snapshots the production path's mtime before and
    after constructing a CalendarScanner and confirms it is unchanged.
    """
    # Resolve the production path independent of any patching — we hardcode
    # the same expression as the module-level constant.
    import os as _os
    from pathlib import Path as _Path
    prod_state = _Path(
        _os.environ.get("SECOND_BRAIN_DIR", str(_Path.home() / "secondbrain"))
    ) / "calendar-scanner-state.json"

    before_mtime = prod_state.stat().st_mtime if prod_state.exists() else None
    before_size = prod_state.stat().st_size if prod_state.exists() else None

    # Construct and exercise a scanner under the autouse redirection. If any
    # code path still writes to the production file this will be flagged.
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "_hostname", return_value="test-host"):
        scanner = CalendarScanner()
        scanner._save_state({"last_scan_time": None, "processed": {"x": "y"}})

    after_mtime = prod_state.stat().st_mtime if prod_state.exists() else None
    after_size = prod_state.stat().st_size if prod_state.exists() else None

    assert before_mtime == after_mtime, "Production STATE_FILE mtime changed"
    assert before_size == after_size, "Production STATE_FILE size changed"


def test_production_memories_dir_never_touched_during_tests(tmp_path):
    """Meta-test: the production MEMORIES_DIR must not be written or renamed-within.

    Regression: `test_filename_format` historically patched `_hostname` to
    "test-host" but forgot to patch `MEMORIES_DIR`, so the v1.6.1 migration
    fix (rename-before-stamp) successfully renamed 11 real calendar-event
    files in iCloud to a `test-host-` prefix during a pytest run. The
    autouse fixture now redirects MEMORIES_DIR; this meta-test asserts no
    calendar-event-*.md file in the real production MEMORIES_DIR is
    created, deleted, or renamed when a CalendarScanner is constructed
    under a `_hostname="test-host"` patch.
    """
    from pathlib import Path as _Path
    prod = _Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "second-brain" / "memories"
    if not prod.exists():
        pytest.skip("Production memories dir not present on this machine")

    before = sorted((f.name, f.stat().st_mtime) for f in prod.glob("calendar-event-*.md"))

    # Reproduce exactly the shape of test_filename_format: patch _hostname
    # but NOT MEMORIES_DIR. The autouse fixture must still isolate it.
    with patch.object(cs, "_hostname", return_value="test-host"):
        CalendarScanner()

    after = sorted((f.name, f.stat().st_mtime) for f in prod.glob("calendar-event-*.md"))
    assert before == after, (
        "Production MEMORIES_DIR was mutated by a test. Filenames or "
        "mtimes changed — likely the autouse fixture stopped isolating it."
    )


def test_load_state_prunes_stacked_hostname_keys(tmp_path):
    """Keys with duplicated hostname tokens are pruned on load; single test-host keys are kept."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"

    # Mix of keys: legitimate, stacked (bogus), and literal test-host (kept).
    state_file.write_text(json.dumps({
        "processed": {
            "calendar-event-Chriss-MacBook-Air-2026-04-11-meeting-abc123.md": "t1",
            "calendar-event-Chriss-Air-Chriss-Air-2026-04-11-meeting-def456.md": "t2",
            "calendar-event-host-host-test-host-host-2026-04-11-x-ghi789.md": "t3",
            "calendar-event-test-host-2026-04-11-legit-test-jkl012.md": "t4",
        }
    }))

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "_hostname", return_value="host"):
        scanner = CalendarScanner()
        loaded = scanner._load_state()

    keys = set(loaded["processed"].keys())
    assert "calendar-event-Chriss-MacBook-Air-2026-04-11-meeting-abc123.md" in keys
    assert "calendar-event-test-host-2026-04-11-legit-test-jkl012.md" in keys
    assert "calendar-event-Chriss-Air-Chriss-Air-2026-04-11-meeting-def456.md" not in keys
    assert "calendar-event-host-host-test-host-host-2026-04-11-x-ghi789.md" not in keys
