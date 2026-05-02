"""Frontmatter schema contract tests.

Gap 2 remediation: verify that each writer's memory file format contains the
fields that downstream readers access, catching schema drift before it reaches
production as a silent KeyError or empty-string fallback.

Contract pairs tested:
  email_thread     → commitment_tracker._extract_commitments (field access)
  meeting_transcript → commitment_tracker._extract_commitments (field access)
  email_thread     → contact_tracker._extract_participants
  meeting_transcript → contact_tracker._extract_participants
  calendar_event   → contact_tracker._extract_participants
  calendar_event   → chat_handler.cmd_event (start_time, end_time fields)
  commitment       → notification_manager commitment reader fields
"""
import json
import yaml
import pytest
from pathlib import Path
from tests.integration import seed


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_fm(path: Path) -> dict:
    """Parse YAML frontmatter from a seed-written memory file."""
    text = path.read_text()
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"No frontmatter found in {path}"
    fm = yaml.safe_load(parts[1])
    assert isinstance(fm, dict), f"Frontmatter did not parse as dict in {path}"
    return fm


# ── email_thread → commitment_tracker ─────────────────────────────────────────

def test_email_thread_has_type_field(tmp_path):
    p = seed.email_thread(tmp_path)
    fm = _read_fm(p)
    assert fm.get("type") == "email_thread", "commitment_tracker filters on type"


def test_email_thread_has_source_title(tmp_path):
    p = seed.email_thread(tmp_path, subject="Budget Planning")
    fm = _read_fm(p)
    assert fm.get("source_title"), "commitment_tracker uses source_title as fallback label"
    assert "Budget Planning" in fm["source_title"]


def test_email_thread_has_participants_list(tmp_path):
    p = seed.email_thread(tmp_path)
    fm = _read_fm(p)
    participants = fm.get("participants") or fm.get("speakers") or []
    assert isinstance(participants, list), "commitment_tracker iterates participants"
    assert len(participants) > 0, "seed should include at least one participant"


def test_email_thread_has_date_field(tmp_path):
    p = seed.email_thread(tmp_path)
    fm = _read_fm(p)
    date_val = fm.get("last_message") or fm.get("first_message") or fm.get("meeting_date")
    assert date_val, "commitment_tracker uses last_message/first_message/meeting_date as date_str"


def test_email_thread_has_summary(tmp_path):
    p = seed.email_thread(tmp_path)
    fm = _read_fm(p)
    # summary may be empty string but must exist (commitment_tracker includes it in prompt)
    assert "summary" in fm


# ── meeting_transcript → commitment_tracker ───────────────────────────────────

def test_meeting_transcript_has_type_field(tmp_path):
    p = seed.meeting(tmp_path)
    fm = _read_fm(p)
    assert fm.get("type") == "meeting_transcript"


def test_meeting_transcript_has_participants_or_speakers(tmp_path):
    p = seed.meeting(tmp_path)
    fm = _read_fm(p)
    participants = fm.get("participants") or fm.get("speakers") or []
    assert isinstance(participants, list)
    assert len(participants) > 0


def test_meeting_transcript_has_meeting_date(tmp_path):
    p = seed.meeting(tmp_path)
    fm = _read_fm(p)
    date_val = fm.get("meeting_date") or fm.get("last_message") or fm.get("first_message")
    assert date_val, "commitment_tracker uses meeting_date as the primary date field for meetings"


# ── email_thread → contact_tracker ───────────────────────────────────────────

def test_email_thread_participants_are_strings_for_contact_tracker(tmp_path):
    p = seed.email_thread(tmp_path)
    fm = _read_fm(p)
    participants = fm.get("participants", [])
    assert all(isinstance(p, (str, dict)) for p in participants), (
        "contact_tracker._extract_participants handles str (email) and dict ({name, email})"
    )
    # For email_thread, participants should be email strings
    assert any("@" in str(p) for p in participants), (
        "email_thread participants should contain email addresses"
    )


def test_email_thread_contact_tracker_extract_participants_returns_list(tmp_path):
    p = seed.email_thread(tmp_path)
    fm = _read_fm(p)
    from contact_tracker import ContactTracker
    tracker = ContactTracker.__new__(ContactTracker)
    result = tracker._extract_participants(fm, "email_thread")
    assert isinstance(result, list)
    # Should find at least the seeded alice@example.com participant
    emails = [e for _, e in result if e]
    assert any("@" in e for e in emails)


# ── meeting_transcript → contact_tracker ──────────────────────────────────────

def test_meeting_transcript_contact_tracker_extract_participants_returns_list(tmp_path):
    p = seed.meeting(tmp_path)
    fm = _read_fm(p)
    from contact_tracker import ContactTracker
    tracker = ContactTracker.__new__(ContactTracker)
    result = tracker._extract_participants(fm, "meeting_transcript")
    assert isinstance(result, list)
    assert len(result) > 0, "seed meeting should yield at least one participant"


# ── calendar_event → contact_tracker ─────────────────────────────────────────

def test_calendar_event_contact_tracker_extract_participants_returns_list(tmp_path):
    p = seed.calendar_event(tmp_path)
    fm = _read_fm(p)
    from contact_tracker import ContactTracker
    tracker = ContactTracker.__new__(ContactTracker)
    result = tracker._extract_participants(fm, "calendar_event")
    assert isinstance(result, list)


# ── calendar_event → chat_handler.cmd_event ───────────────────────────────────

def test_calendar_event_has_start_time_field(tmp_path):
    """start_time is the field cmd_event and notification_manager read.

    This test caught a bug in seed.calendar_event where the field was named
    'start' instead of 'start_time', making the pre-meeting notification
    and /event detail silently show no start time.
    """
    p = seed.calendar_event(tmp_path)
    fm = _read_fm(p)
    assert "start_time" in fm, (
        "calendar_event frontmatter must use 'start_time', not 'start' — "
        "notification_manager and cmd_event both call fm.get('start_time')"
    )
    assert fm["start_time"], "start_time must be a non-empty ISO datetime string"


def test_calendar_event_has_end_time_field(tmp_path):
    p = seed.calendar_event(tmp_path)
    fm = _read_fm(p)
    assert "end_time" in fm, (
        "calendar_event frontmatter must use 'end_time' — cmd_event reads fm.get('end_time')"
    )


def test_calendar_event_has_source_title(tmp_path):
    p = seed.calendar_event(tmp_path, title="Weekly Sync")
    fm = _read_fm(p)
    assert fm.get("source_title") == "Weekly Sync", (
        "cmd_event reads source_title for display"
    )


def test_calendar_event_start_time_is_parseable_as_iso(tmp_path):
    from datetime import datetime
    p = seed.calendar_event(tmp_path, start_iso="2026-05-01T09:00:00-07:00")
    fm = _read_fm(p)
    start_val = fm.get("start_time")
    assert start_val is not None, "start_time must be present"
    # Must be a string — notification_manager calls datetime.fromisoformat(start_time_str)
    # PyYAML parses unquoted ISO strings as datetime objects; yaml.dump in seed quotes them
    assert isinstance(start_val, str), (
        f"start_time must be a string, not {type(start_val).__name__}. "
        "Unquoted ISO datetimes in YAML are parsed as datetime objects, "
        "causing notification_manager's fromisoformat() to fail."
    )
    dt = datetime.fromisoformat(start_val)
    assert dt.year == 2026


# ── commitment → notification_manager field access ────────────────────────────

def test_commitment_has_required_fields_for_notification_manager(tmp_path):
    p = seed.commitment(tmp_path, title="Send report", status="active")
    fm = _read_fm(p)
    # notification_manager reads these fields on commitment files
    assert fm.get("type") == "commitment"
    assert fm.get("source_title"), "source_title used in notification text"
    # commitment_type defaults to "outbound" if missing — but should be explicit
    assert "commitment_type" in fm
    # status is checked to filter active vs completed
    assert fm.get("status") in ("active", "completed", "dismissed", "needs-review")
    # tags must be a list (needs_review check iterates it)
    assert isinstance(fm.get("tags", []), list)


def test_commitment_owner_and_due_date_are_accessible(tmp_path):
    p = seed.commitment(tmp_path, title="Review budget")
    fm = _read_fm(p)
    # These fields may be None/empty but must not raise KeyError
    _ = fm.get("owner", "")
    _ = fm.get("due_date")
    _ = fm.get("recipient", "")


def test_commitment_notification_safe_frontmatter_roundtrip(tmp_path):
    """Verify a commitment file survives the notification_manager JSON cache roundtrip."""
    import json as _json
    p = seed.commitment(tmp_path, title="Ship the thing")
    fm = _read_fm(p)
    # notification_manager stores frontmatter as JSON string in SQLite cache,
    # then reads it back via _safe_frontmatter(entry["frontmatter"])
    json_str = _json.dumps(fm)
    from notification_manager import _safe_frontmatter
    recovered = _safe_frontmatter(json_str)
    assert isinstance(recovered, dict)
    assert recovered.get("type") == "commitment"
    assert recovered.get("source_title") == "Ship the thing"
