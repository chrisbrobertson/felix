"""
Unit tests for email_scanner.

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

import email_scanner as es
from email_scanner import (
    EmailScanner,
    EnvelopeIndexSource,
    AppleScriptSource,
    _normalize_subject,
    _subject_to_conv_id,
    _mailbox_name_from_url,
    _parse_frontmatter,
    CORE_DATA_EPOCH_OFFSET,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_thread(conv_id=1001, subject="Project Update", message_count=3,
                last_message="2026-04-10T09:00:00", first_message="2026-04-05T08:00:00",
                participants=None):
    return {
        "conversation_id": conv_id,
        "subject": subject,
        "raw_subject": f"RE: {subject}",
        "first_message": first_message,
        "last_message": last_message,
        "message_count": message_count,
        "participants": participants or ["alice@acme.com", "bob@acme.com"],
        "messages": [
            "2026-04-10 Alice: Latest update on the project",
            "2026-04-08 Bob: Reviewed the doc",
            "2026-04-05 Alice: Starting thread",
        ],
        "max_rowid": 12345,
    }


def write_email_memory(memories_dir: Path, conv_id: int, subject: str = "RE: Test",
                       message_count: int = 3, last_message: str = "2026-04-10T09:00:00",
                       summary: str = "A test email thread."):
    slug = re.sub(r'[^a-z0-9]+', '-', subject.lower()).strip('-')[:40]
    mem = memories_dir / f"email-thread-{slug}-{conv_id}.md"
    content = (
        f"---\nsource_title: {subject}\nsummary: {summary}\n"
        f"tags: [test]\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: mailto:conversation-{conv_id}\ntype: email_thread\n"
        f"participants: [alice@acme.com]\nmessage_count: {message_count}\n"
        f"last_message: '{last_message}'\nfirst_message: '2026-04-05T08:00:00'\n"
        f"conversation_id: {conv_id}\n---\n\n## Messages\n- test\n"
    )
    mem.write_text(content)
    return mem


import re  # needed by write_email_memory above


# ── _normalize_subject ────────────────────────────────────────────────────────

def test_applescript_strips_re():
    assert _normalize_subject("RE: Project Update") == "Project Update"


def test_applescript_strips_fw():
    assert _normalize_subject("FW: Project Update") == "Project Update"


def test_applescript_strips_re_fw():
    assert _normalize_subject("RE: FW: RE: Project Update") == "Project Update"


def test_applescript_strips_case_insensitive():
    assert _normalize_subject("re: Topic") == "Topic"
    assert _normalize_subject("Fwd: Topic") == "Topic"


def test_normalize_already_clean():
    assert _normalize_subject("Clean Subject") == "Clean Subject"


# ── _mailbox_name_from_url ────────────────────────────────────────────────────

def test_mailbox_name_from_url_inbox():
    assert _mailbox_name_from_url("mailbox://user@host/INBOX") == "INBOX"


def test_mailbox_name_from_url_subfolder():
    assert _mailbox_name_from_url("mailbox://user@host/INBOX/Trash") == "Trash"


def test_mailbox_name_from_url_empty():
    assert _mailbox_name_from_url("") == ""


# ── Core Data timestamp conversion ───────────────────────────────────────────

def test_convert_core_data_timestamp():
    src = EnvelopeIndexSource.__new__(EnvelopeIndexSource)
    # Core Data ts = 0 should give 2001-01-01 00:00:00
    result = src._convert_timestamp(0)
    assert result.year == 2001
    assert result.month == 1
    assert result.day == 1


def test_convert_core_data_timestamp_recent():
    src = EnvelopeIndexSource.__new__(EnvelopeIndexSource)
    # 2026-04-11 00:00:00 UTC → unix ts = 1775865600
    # core data ts = 1775865600 - 978307200 = 797558400
    core_ts = 797558400
    result = src._convert_timestamp(core_ts)
    assert result.year == 2026
    assert result.month == 4
    assert result.day == 11


def test_dt_to_core_data_roundtrip():
    src = EnvelopeIndexSource.__new__(EnvelopeIndexSource)
    dt = datetime(2026, 4, 11, 12, 0, 0)
    core_ts = src._dt_to_core_data(dt)
    result = src._convert_timestamp(core_ts)
    assert result.year == dt.year
    assert result.month == dt.month
    assert result.day == dt.day


# ── EnvelopeIndexSource._find_db_path ─────────────────────────────────────────

def test_find_envelope_index_path(tmp_path):
    mail_dir = tmp_path / "Library" / "Mail"
    v10 = mail_dir / "V10"
    v9 = mail_dir / "V9"
    v10.mkdir(parents=True)
    v9.mkdir(parents=True)
    (v10 / "Envelope Index").touch()
    (v9 / "Envelope Index").touch()

    with patch("pathlib.Path.home", return_value=tmp_path):
        result = EnvelopeIndexSource._find_db_path()

    assert result is not None
    assert "V10" in str(result)


def test_find_envelope_index_missing(tmp_path):
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = EnvelopeIndexSource._find_db_path()
    assert result is None


# ── EnvelopeIndexSource FDA check ─────────────────────────────────────────────

def test_fda_check_logs_warning_and_returns_none(tmp_path, caplog):
    fake_path = tmp_path / "Library" / "Mail" / "V10" / "Envelope Index"
    fake_path.parent.mkdir(parents=True)
    fake_path.touch()

    # Patch _find_db_path to return our fake path, then make stat() raise PermissionError
    with patch.object(EnvelopeIndexSource, "_find_db_path", return_value=fake_path), \
         patch("email_scanner.Path.stat", side_effect=PermissionError("access denied")), \
         caplog.at_level("WARNING", logger="email-scanner"):
        result = EnvelopeIndexSource.create()

    assert result is None
    assert "Full Disk Access" in caplog.text
    assert "github.com/chrisbrobertson/felix" in caplog.text


# ── _rows_to_threads ──────────────────────────────────────────────────────────

def _make_src():
    src = EnvelopeIndexSource.__new__(EnvelopeIndexSource)
    src._db_path = Path("/fake")
    return src


def _fake_ts(dt: datetime) -> float:
    epoch = datetime(1970, 1, 1)
    return (dt - epoch).total_seconds() - CORE_DATA_EPOCH_OFFSET


def test_threads_grouped_by_conversation_id():
    src = _make_src()
    t1 = datetime(2026, 4, 10, 9, 0)
    t2 = datetime(2026, 4, 11, 10, 0)
    rows = [
        (1001, "Project Update", _fake_ts(t1), _fake_ts(t1), "First message",
         0, 0, "alice@a.com", "Alice", "mailbox://u@h/INBOX", 100),
        (1001, "RE: Project Update", _fake_ts(t2), _fake_ts(t2), "Second message",
         1, 0, "bob@b.com", "Bob", "mailbox://u@h/INBOX", 101),
    ]
    threads, max_rowid = src._rows_to_threads(rows, set())
    assert len(threads) == 1
    assert threads[0]["message_count"] == 2
    assert max_rowid == 101
    assert "alice@a.com" in threads[0]["participants"]
    assert "bob@b.com" in threads[0]["participants"]


def test_excluded_mailboxes_filtered():
    src = _make_src()
    t1 = datetime(2026, 4, 10, 9, 0)
    rows = [
        (1001, "Important", _fake_ts(t1), _fake_ts(t1), "Keep me",
         0, 0, "a@b.com", "A", "mailbox://u@h/INBOX", 100),
        (1002, "Spam msg", _fake_ts(t1), _fake_ts(t1), "Ignore me",
         0, 0, "c@d.com", "C", "mailbox://u@h/Trash", 101),
    ]
    threads, _ = src._rows_to_threads(rows, {"trash"})
    conv_ids = [t["conversation_id"] for t in threads]
    assert 1001 in conv_ids
    assert 1002 not in conv_ids


# ── Change detection ──────────────────────────────────────────────────────────

def test_needs_update_true_when_no_memory(tmp_path):
    scanner = EmailScanner()
    thread = make_thread(conv_id=999)
    memory_path = tmp_path / "email-thread-project-update-999.md"
    assert scanner._needs_update(thread, memory_path) is True


def test_needs_update_false_when_unchanged(tmp_path):
    scanner = EmailScanner()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    thread = make_thread(conv_id=1001, message_count=3, last_message="2026-04-10T09:00:00")
    mem = write_email_memory(memories_dir, 1001, subject="Test",
                             message_count=3, last_message="2026-04-10T09:00:00")
    with patch.object(scanner, "_memory_path", return_value=mem):
        assert scanner._needs_update(thread, mem) is False


def test_needs_update_true_when_new_messages(tmp_path):
    scanner = EmailScanner()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    thread = make_thread(conv_id=1001, message_count=4, last_message="2026-04-11T10:00:00")
    mem = write_email_memory(memories_dir, 1001, subject="Test",
                             message_count=3, last_message="2026-04-10T09:00:00")
    assert scanner._needs_update(thread, mem) is True


# ── Slugify ───────────────────────────────────────────────────────────────────

def test_slugify_special_chars():
    scanner = EmailScanner()
    assert scanner._slugify("RE: Q3 Planning — Budget!") == "re-q3-planning-budget"


def test_slugify_truncation():
    scanner = EmailScanner()
    long_subject = "a" * 100
    result = scanner._slugify(long_subject)
    assert len(result) <= 40


def test_slugify_strips_leading_trailing_hyphens():
    scanner = EmailScanner()
    result = scanner._slugify("  ---Hello World---  ")
    assert not result.startswith("-")
    assert not result.endswith("-")


# ── Memory file write ─────────────────────────────────────────────────────────

def test_write_memory_field_order(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()
    thread = make_thread(conv_id=2001, subject="Budget Discussion")

    with patch.object(es, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(thread, "Discussion about Q3 budget.", ["finance", "q3"])

    files = list(memories_dir.glob("email-thread-*.md"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert lines[0] == "---"
    assert lines[1].startswith("source_title:")
    assert lines[2].startswith("summary:")
    assert lines[3].startswith("tags:")
    assert lines[4].startswith("last_scanned:")


def test_write_memory_atomic(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()
    thread = make_thread(conv_id=3001, subject="Atomic Test")

    with patch.object(es, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(thread, "Atomic write test.", ["test"])

    assert list(memories_dir.glob("*.tmp")) == []
    assert len(list(memories_dir.glob("email-thread-*.md"))) == 1


def test_write_memory_type_email_thread(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()
    thread = make_thread(conv_id=4001, subject="Type Check")

    with patch.object(es, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(thread, "Type check.", ["check"])

    mem = list(memories_dir.glob("email-thread-*.md"))[0]
    fm = _parse_frontmatter(mem.read_text())
    assert fm["type"] == "email_thread"


def test_write_memory_frontmatter_parseable(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()
    thread = make_thread(conv_id=5001, subject="Parse Test",
                         participants=["alice@acme.com", "bob@acme.com"])

    with patch.object(es, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(thread, "Parse test summary.", ["acme", "test"])

    mem = list(memories_dir.glob("email-thread-*.md"))[0]
    fm = _parse_frontmatter(mem.read_text())
    assert fm["source_url"] == "mailto:conversation-5001"
    assert fm["conversation_id"] == 5001
    assert fm["message_count"] == 3
    assert isinstance(fm["participants"], list)
    assert isinstance(fm["tags"], list)


# ── State file ────────────────────────────────────────────────────────────────

def test_state_file_persists_high_water(tmp_path):
    scanner = EmailScanner()

    with patch.object(es, "STATE_FILE", tmp_path / "email-scanner-state.json"):
        scanner._save_state({"high_water_rowid": 9999, "last_scan_time": "2026-04-11T10:00:00"})
        loaded = scanner._load_state()

    assert loaded["high_water_rowid"] == 9999


def test_load_state_returns_defaults_when_missing(tmp_path):
    scanner = EmailScanner()
    with patch.object(es, "STATE_FILE", tmp_path / "nonexistent.json"):
        state = scanner._load_state()
    assert state["high_water_rowid"] == 0


# ── AppleScript normalization ─────────────────────────────────────────────────

def test_applescript_groups_by_subject():
    src = AppleScriptSource()
    raw = (
        "RE: Budget Talk|||alice@a.com|||2026-04-10T09:00:00|||id1|||\n"
        "RE: Budget Talk|||bob@b.com|||2026-04-11T10:00:00|||id2|||\n"
    )
    threads, _ = src._parse_raw(raw, set())
    assert len(threads) == 1
    assert threads[0]["message_count"] == 2


def test_subject_to_conv_id_stable():
    """Same subject always produces same conv_id."""
    id1 = _subject_to_conv_id("Budget Talk")
    id2 = _subject_to_conv_id("Budget Talk")
    assert id1 == id2


def test_subject_to_conv_id_different_subjects():
    id1 = _subject_to_conv_id("Budget Talk")
    id2 = _subject_to_conv_id("Project Update")
    assert id1 != id2


# ── Archive threshold ─────────────────────────────────────────────────────────

def test_archive_skips_stale_threads(tmp_path):
    """Threads older than archive_after_days should not be written."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()

    old_thread = make_thread(
        conv_id=6001, subject="Old Thread",
        last_message=(datetime.now() - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S")
    )

    written = []
    with patch.object(es, "MEMORIES_DIR", memories_dir), \
         patch.object(es, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(scanner, "_get_data_source") as mock_src:

        mock_source = MagicMock()
        mock_source.get_threads_since.return_value = ([old_thread], 6001)
        mock_source.get_threads_updated_since.return_value = ([old_thread], 6001)
        mock_src.return_value = mock_source

        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  archive_after_days: 90\n  initial_lookback_days: 30\n"
            "  skip_mailboxes: []\n  full_rescan: false\n"
        )

        original_write = scanner._write_memory
        scanner._write_memory = lambda *a, **kw: written.append(a[0]["subject"])

        import asyncio
        asyncio.run(scanner._run_scan())

    assert "Old Thread" not in written


# ── Scanner config defaults ───────────────────────────────────────────────────

def test_scanner_config_defaults(tmp_path):
    scanner = EmailScanner()
    with patch.object(es, "CONFIG_PATH", tmp_path / "nonexistent.yaml"):
        sc = scanner._scanner_config()
    # Should return empty dict without error
    assert isinstance(sc, dict)


def test_incremental_uses_high_water_rowid(tmp_path):
    """When high_water_rowid > 0, get_threads_updated_since is called."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()

    with patch.object(es, "MEMORIES_DIR", memories_dir), \
         patch.object(es, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(scanner, "_get_data_source") as mock_src:

        mock_source = MagicMock()
        mock_source.get_threads_updated_since.return_value = ([], 0)
        mock_src.return_value = mock_source

        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  archive_after_days: 90\n  initial_lookback_days: 30\n"
            "  skip_mailboxes: []\n  full_rescan: false\n"
        )
        # Pre-populate state with a high-water mark
        scanner._save_state({"high_water_rowid": 500, "last_scan_time": "2026-04-11T00:00:00"})

        import asyncio
        asyncio.run(scanner._run_scan())

    mock_source.get_threads_updated_since.assert_called_once()
    mock_source.get_threads_since.assert_not_called()


def test_full_rescan_resets_high_water(tmp_path):
    """When full_rescan: true, high-water is reset and get_threads_since is called."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()

    with patch.object(es, "MEMORIES_DIR", memories_dir), \
         patch.object(es, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(scanner, "_get_data_source") as mock_src, \
         patch.object(scanner, "_clear_full_rescan_flag"):

        mock_source = MagicMock()
        mock_source.get_threads_since.return_value = ([], 0)
        mock_src.return_value = mock_source

        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  archive_after_days: 90\n  initial_lookback_days: 30\n"
            "  skip_mailboxes: []\n  full_rescan: true\n"
        )
        scanner._save_state({"high_water_rowid": 9999, "last_scan_time": "2026-04-10T00:00:00"})

        import asyncio
        asyncio.run(scanner._run_scan())

    # get_threads_since (not updated_since) should be called for full rescan
    mock_source.get_threads_since.assert_called_once()
    mock_source.get_threads_updated_since.assert_not_called()


async def test_backfill_resets_high_water_and_rescans(tmp_path):
    """backfill() zeros high_water_rowid and triggers scan."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()

    with patch.object(es, "MEMORIES_DIR", memories_dir), \
         patch.object(es, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(scanner, "_get_data_source") as mock_src:

        mock_source = MagicMock()
        mock_source.get_threads_since.return_value = ([], 0)
        mock_src.return_value = mock_source

        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  archive_after_days: 90\n  initial_lookback_days: 30\n"
            "  skip_mailboxes: []\n"
        )
        scanner._save_state({"high_water_rowid": 9999, "last_scan_time": "2026-04-10T00:00:00"})

        result = await scanner.backfill(30)

    assert result["processed"] >= 0
    # get_threads_since should be called (not updated_since)
    mock_source.get_threads_since.assert_called_once()
    mock_source.get_threads_updated_since.assert_not_called()
