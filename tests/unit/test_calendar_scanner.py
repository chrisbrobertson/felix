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
    CALENDAR_CACHE_TMP,
)


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

    with patch.object(source, '_copy_db', return_value=db_path):
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

    with patch.object(source, '_copy_db', return_value=db_path):
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

    with patch.object(source, '_copy_db', return_value=db_path):
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

    with patch.object(source, '_copy_db', return_value=db_path):
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

    with patch.object(source, '_copy_db', return_value=db_path):
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

    with patch.object(source, '_copy_db', return_value=db_path):
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

    with patch.object(source, '_copy_db', return_value=db_path):
        events = source.get_events(start_date, end_date, {"birthdays"})

    assert len(events) == 0


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
