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
    CLASSIFIER_VERSION,
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
                       summary: str = "A test email thread.",
                       classifier_version: int = None):
    if classifier_version is None:
        classifier_version = CLASSIFIER_VERSION
    slug = re.sub(r'[^a-z0-9]+', '-', subject.lower()).strip('-')[:40]
    mem = memories_dir / f"email-thread-{slug}-{conv_id}.md"
    content = (
        f"---\nsource_title: {subject}\nsummary: {summary}\n"
        f"tags: [test]\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: mailto:conversation-{conv_id}\ntype: email_thread\n"
        f"classifier_version: {classifier_version}\n"
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
    participants = threads[0]["participants"]
    emails = [p["email"] for p in participants]
    names = [p["name"] for p in participants]
    assert "alice@a.com" in emails
    assert "bob@b.com" in emails
    assert "Alice" in names
    assert "Bob" in names


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


def test_rows_to_threads_captures_display_name():
    src = _make_src()
    t1 = datetime(2026, 4, 10, 9, 0)
    rows = [
        (1001, "Test Subject", _fake_ts(t1), _fake_ts(t1), "Message body",
         0, 0, "alice@example.com", "Alice Example", "mailbox://u@h/INBOX", 100),
    ]
    threads, _ = src._rows_to_threads(rows, set())
    assert len(threads) == 1
    participants = threads[0]["participants"]
    assert len(participants) == 1
    assert participants[0] == {"name": "Alice Example", "email": "alice@example.com"}


def test_rows_to_threads_falls_back_to_bare_email_when_name_missing():
    src = _make_src()
    t1 = datetime(2026, 4, 10, 9, 0)
    rows = [
        (1001, "Test Subject", _fake_ts(t1), _fake_ts(t1), "Message body",
         0, 0, "alice@example.com", None, "mailbox://u@h/INBOX", 100),
    ]
    threads, _ = src._rows_to_threads(rows, set())
    assert len(threads) == 1
    participants = threads[0]["participants"]
    assert len(participants) == 1
    assert participants[0] == "alice@example.com"


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
    # Fields after tags may vary (classification field added); assert both are present
    fm_keys = [l.split(":")[0] for l in lines[1:] if l and ":" in l and not l.startswith(" ")]
    assert "last_scanned" in fm_keys
    assert "classification" in fm_keys


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


def test_applescript_generated_script_has_no_literal_newline_in_string():
    """Regression guard: a past bug used "\\n" inside a Python f-string,
    which emitted a raw LF into an AppleScript string literal — a syntax
    error that made osascript fail to compile. The fix is `linefeed`."""
    src = AppleScriptSource()
    captured = {}

    def fake_run(script, timeout=120):
        captured["script"] = script
        return ""

    src._run_osascript = fake_run
    src._fetch_messages_raw(datetime(2026, 4, 1), set())
    script = captured["script"]
    assert "linefeed" in script, "should use AppleScript linefeed, not \\n"
    # Walk the script char-by-char; no literal LF allowed inside "..." regions.
    in_quote = False
    for ch in script:
        if ch == '"':
            in_quote = not in_quote
        elif ch == "\n" and in_quote:
            raise AssertionError("literal LF inside AppleScript string literal")


def test_applescript_extracts_display_name_from_sender():
    """'Alice Jones <alice@a.com>' should produce a participant dict with name+email."""
    src = AppleScriptSource()
    raw = 'RE: Budget|||"Alice Jones" <alice@a.com>|||2026-04-10T09:00:00|||id1|||\n'
    threads, _ = src._parse_raw(raw, set())
    assert len(threads) == 1
    participants = threads[0]["participants"]
    assert len(participants) == 1
    p = participants[0]
    assert isinstance(p, dict)
    assert p["name"] == "Alice Jones"
    assert p["email"] == "alice@a.com"


def test_applescript_bare_email_has_no_name():
    """A bare email with no display name should produce a plain string participant."""
    src = AppleScriptSource()
    raw = "Meeting Follow-up|||bob@b.com|||2026-04-10T09:00:00|||id2|||\n"
    threads, _ = src._parse_raw(raw, set())
    assert len(threads) == 1
    p = threads[0]["participants"][0]
    assert p == "bob@b.com"


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


# ── Email Classification (FR-11) ─────────────────────────────────────────────

def test_generated_prompt_asks_for_classification(tmp_path):
    """Prompt includes CLASSIFICATION line and all four label options."""
    scanner = EmailScanner()
    thread = make_thread()

    with patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"):
        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  classification_enabled: true\n"
        )
        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(content="SUMMARY: x\nTAGS: a\nCLASSIFICATION: human"))]
            )
            import asyncio
            asyncio.run(scanner._generate_summary_and_tags(thread))

        # Check the prompt passed to acompletion
        call_args = mock_llm.call_args
        prompt = call_args.kwargs["messages"][0]["content"]
        assert "CLASSIFICATION:" in prompt
        assert "human" in prompt
        assert "transactional" in prompt
        assert "marketing" in prompt
        assert "automated" in prompt


@pytest.mark.asyncio
async def test_parses_classification_from_response(tmp_path):
    """LLM returns CLASSIFICATION: marketing → parsed correctly."""
    scanner = EmailScanner()
    thread = make_thread()

    with patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"):
        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  classification_enabled: true\n"
        )
        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(
                    content="SUMMARY: Newsletter\nTAGS: acme, newsletter\nCLASSIFICATION: marketing"
                ))]
            )
            summary, tags, classification = await scanner._generate_summary_and_tags(thread)

    assert summary == "Newsletter"
    assert "newsletter" in tags
    assert classification == "marketing"


@pytest.mark.asyncio
async def test_classification_invalid_label_becomes_unknown(tmp_path):
    """LLM returns invalid classification → defaults to unknown."""
    scanner = EmailScanner()
    thread = make_thread()

    with patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"):
        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  classification_enabled: true\n"
        )
        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(
                    content="SUMMARY: x\nTAGS: a\nCLASSIFICATION: weird"
                ))]
            )
            _, _, classification = await scanner._generate_summary_and_tags(thread)

    assert classification == "unknown"


@pytest.mark.asyncio
async def test_classification_missing_line_becomes_unknown(tmp_path):
    """LLM omits CLASSIFICATION line → defaults to unknown."""
    scanner = EmailScanner()
    thread = make_thread()

    with patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"):
        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  classification_enabled: true\n"
        )
        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(
                    content="SUMMARY: x\nTAGS: a, b"
                ))]
            )
            _, _, classification = await scanner._generate_summary_and_tags(thread)

    assert classification == "unknown"


def test_write_memory_includes_classification_field(tmp_path):
    """Written memory file contains classification field in frontmatter."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()
    thread = make_thread(conv_id=7001, subject="Marketing Test")

    with patch.object(es, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(thread, "Test summary.", ["test"], "marketing")

    mem = list(memories_dir.glob("email-thread-*.md"))[0]
    fm = _parse_frontmatter(mem.read_text())
    assert fm["classification"] == "marketing"


@pytest.mark.asyncio
async def test_llm_failure_returns_unknown_classification(tmp_path):
    """LLM raises exception → classification is unknown."""
    scanner = EmailScanner()
    thread = make_thread()

    with patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"):
        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  classification_enabled: true\n"
        )
        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = Exception("LLM error")
            summary, tags, classification = await scanner._generate_summary_and_tags(thread)

    assert summary == ""
    assert tags == []
    assert classification == "unknown"


def test_existing_summary_includes_classification(tmp_path):
    """_get_existing_summary_and_tags returns classification from frontmatter."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = EmailScanner()
    thread = make_thread(conv_id=8001, subject="Existing Test", message_count=5)

    mem_path = write_email_memory(
        memories_dir, conv_id=8001, subject="Existing Test",
        message_count=5, last_message="2026-04-10T09:00:00",
        summary="Existing summary"
    )
    # Add classification field
    content = mem_path.read_text()
    content = content.replace("tags: [test]", "tags: [test]\nclassification: automated")
    mem_path.write_text(content)

    with patch.object(es, "MEMORIES_DIR", memories_dir):
        summary, tags, classification = scanner._get_existing_summary_and_tags(mem_path, thread)

    assert summary == "Existing summary"
    assert "test" in tags
    assert classification == "automated"


# ── AppleScript injection guard (C1 fix) ──────────────────────────────────────

def test_applescript_escape_double_quote():
    """_applescript_escape escapes double quotes to prevent injection."""
    escaped = es._applescript_escape('Foo"Bar')
    assert escaped == 'Foo\\"Bar'


def test_applescript_escape_backslash():
    """_applescript_escape escapes backslashes first to avoid double-escaping."""
    escaped = es._applescript_escape('Foo\\Bar')
    assert escaped == 'Foo\\\\Bar'


def test_applescript_escape_combined():
    """_applescript_escape handles backslash + quote in the same string."""
    escaped = es._applescript_escape('Path: C:\\Program Files\\"App"')
    assert escaped == 'Path: C:\\\\Program Files\\\\\\"App\\"'


def test_applescript_injection_blocked():
    """Mailbox name containing injection payload is escaped in AppleScript."""
    source = es.AppleScriptSource()
    malicious_mailbox = 'Foo"; do shell script "rm -rf ~'
    excluded = {malicious_mailbox}
    since = datetime(2026, 4, 1)

    # Call _fetch_messages_raw to generate the AppleScript
    # We'll mock _run_osascript to capture the script
    captured_script = None

    def capture_osascript(script):
        nonlocal captured_script
        captured_script = script
        return ""

    with patch.object(source, "_run_osascript", side_effect=capture_osascript):
        source._fetch_messages_raw(since, excluded)

    # The mailbox name should appear escaped in the script
    assert captured_script is not None
    # After .title() the malicious string becomes 'Foo"; Do Shell Script "Rm -Rf ~'
    # With escaping applied first, it becomes 'Foo\"; Do Shell Script \"Rm -Rf ~'
    # Check that backslashes are present before the double-quotes
    assert 'Foo\\"; Do Shell Script \\"Rm -Rf ~' in captured_script
    # Ensure the unescaped version (with .title() applied) is NOT in the script
    assert 'Foo"; Do Shell Script "Rm -Rf ~' not in captured_script


# ── Classifier version / reclassification (PR #98) ───────────────────────────

@pytest.mark.asyncio
async def test_generate_summary_prompt_contains_forwarded_email_guidance():
    """The classification prompt must include the PR #98 guidance distinguishing forwarded
    emails (human) from automated system reminders (automated/transactional).

    Locks the prompt text so a future edit cannot silently revert the fix.
    """
    scanner = EmailScanner.__new__(EmailScanner)
    scanner.notification_callback = None

    thread = {
        "conversation_id": 54321,
        "subject": "Fw: REMINDER: Centroid OCI Extension",
        "raw_subject": "Fw: REMINDER: Centroid OCI Extension",
        "first_message": "2026-04-27T16:00:00",
        "last_message": "2026-04-27T16:21:46",
        "message_count": 1,
        "participants": [{"name": "Kurt Binder", "email": "kbinder@arlo.com"}],
        "messages": ["2026-04-27 Kurt Binder: Please sign the OCI extension document."],
        "max_rowid": 99,
    }

    captured_prompt = None

    async def capture_call(**kwargs):
        nonlocal captured_prompt
        captured_prompt = kwargs["messages"][0]["content"]
        return MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                "SUMMARY: Forwarded reminder about OCI contract.\n"
                "TAGS: oci, centroid\n"
                "CLASSIFICATION: human"
            )))],
            usage=None,
        )

    with patch.object(es, "CONFIG_PATH", Path("/nonexistent/config.yaml")), \
         patch("litellm.acompletion", side_effect=capture_call):
        await scanner._generate_summary_and_tags(thread)

    assert captured_prompt is not None
    # Verify the PR #98 forwarded-email guidance is present in the prompt
    assert "forwarded emails" in captured_prompt
    assert "forwarded" in captured_prompt.lower()
    # Verify the automated-reminder distinction is present
    assert "not reminders forwarded by a real person" in captured_prompt


@pytest.mark.asyncio
async def test_generate_summary_classifies_forwarded_reminder_as_human():
    """A forwarded REMINDER email from a colleague must be classified as 'human'.

    This is the root cause of #98: previously the LLM prompt lacked guidance,
    causing forwarded business reminders to be classified as 'transactional'.
    """
    scanner = EmailScanner.__new__(EmailScanner)
    scanner.notification_callback = None

    thread = {
        "conversation_id": 54321,
        "subject": "Fw: REMINDER: Centroid OCI Extension Order Document",
        "raw_subject": "Fw: REMINDER: Centroid OCI Extension Order Document",
        "first_message": "2026-04-27T16:00:00",
        "last_message": "2026-04-27T16:21:46",
        "message_count": 1,
        "participants": [{"name": "Kurt Binder", "email": "kbinder@arlo.com"}],
        "messages": ["2026-04-27 Kurt Binder: Please review the OCI Extension Order Document."],
        "max_rowid": 99,
    }

    with patch.object(es, "CONFIG_PATH", Path("/nonexistent/config.yaml")), \
         patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                "SUMMARY: Forwarded reminder about OCI contract document.\n"
                "TAGS: oci, centroid, contract\n"
                "CLASSIFICATION: human"
            )))],
            usage=None,
        )
        summary, tags, classification = await scanner._generate_summary_and_tags(thread)

    assert classification == "human"
    assert summary != ""
    assert len(tags) > 0


def test_needs_update_triggers_on_old_classifier_version(tmp_path):
    """A memory file without classifier_version (or with an old one) must be flagged
    for reclassification even when message_count and last_message are unchanged.

    This ensures existing misclassified threads are reclassified after deploy.
    """
    scanner = EmailScanner.__new__(EmailScanner)
    scanner.notification_callback = None

    thread = make_thread(subject="Fw: REMINDER: OCI", message_count=1,
                         last_message="2026-04-27T16:21:46")

    memory_file = tmp_path / "email-fw-reminder--oci-1001.md"
    # Write a file with classifier_version=1 (pre-PR-#98) but same message_count/last_message
    memory_file.write_text(
        "---\n"
        "source_title: 'Fw: REMINDER: OCI'\n"
        "summary: Old summary.\n"
        "classification: transactional\n"
        "classifier_version: 1\n"
        "message_count: 1\n"
        "last_message: '2026-04-27T16:21:46'\n"
        "---\n\nOld content.\n"
    )

    with patch.object(es, "MEMORIES_DIR", tmp_path):
        result = scanner._needs_update(thread, memory_file)

    assert result is True, "_needs_update must return True when classifier_version is outdated"


def test_needs_update_no_trigger_on_current_classifier_version(tmp_path):
    """A memory file with the current classifier_version must NOT trigger reclassification
    when message_count and last_message are unchanged.
    """
    scanner = EmailScanner.__new__(EmailScanner)
    scanner.notification_callback = None

    thread = make_thread(subject="Normal Thread", message_count=3,
                         last_message="2026-04-10T09:00:00")

    memory_file = tmp_path / "email-normal-thread-1001.md"
    memory_file.write_text(
        f"---\n"
        f"source_title: 'Normal Thread'\n"
        f"summary: Existing summary.\n"
        f"classification: human\n"
        f"classifier_version: {CLASSIFIER_VERSION}\n"
        f"message_count: 3\n"
        f"last_message: '2026-04-10T09:00:00'\n"
        f"---\n\nContent.\n"
    )

    with patch.object(es, "MEMORIES_DIR", tmp_path):
        result = scanner._needs_update(thread, memory_file)

    assert result is False, "_needs_update must return False for up-to-date files"


def test_reclassification_writes_classifier_version(tmp_path):
    """After reclassifying a thread with an old classifier_version, the new memory file
    must contain the current CLASSIFIER_VERSION in its frontmatter.
    """
    import asyncio as _asyncio

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    thread = make_thread(
        subject="Fw: REMINDER: OCI",
        conv_id=54321,
        message_count=1,
        last_message="2026-04-27T16:21:46",
        participants=[{"name": "Kurt Binder", "email": "kbinder@arlo.com"}],
    )

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"high_water_rowid": 0}))

    scanner = EmailScanner()

    # Determine the canonical path the scanner would use for this thread
    with patch.object(es, "MEMORIES_DIR", memories_dir):
        memory_file = scanner._memory_path(thread)

    # Write a pre-#98 file at that path: classified as transactional, no classifier_version
    memory_file.write_text(
        "---\n"
        "source_title: 'Fw: REMINDER: OCI'\n"
        "summary: Old summary.\n"
        "classification: transactional\n"
        "message_count: 1\n"
        "last_message: '2026-04-27T16:21:46'\n"
        "---\n\nOld content.\n"
    )

    mock_source = MagicMock()
    mock_source.get_threads_since.return_value = ([thread], 99)

    with patch.object(es, "MEMORIES_DIR", memories_dir), \
         patch.object(es, "STATE_FILE", state_file), \
         patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(scanner, "_get_data_source", return_value=mock_source), \
         patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:

        (tmp_path / "config.yaml").write_text(
            "email_scanner:\n  archive_after_days: 90\n  initial_lookback_days: 30\n"
            "  skip_mailboxes: []\n"
        )
        mock_llm.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                "SUMMARY: Forwarded OCI reminder.\n"
                "TAGS: oci, centroid\n"
                "CLASSIFICATION: human"
            )))],
            usage=None,
        )
        _asyncio.run(scanner._run_scan())

    # LLM must have been called (reclassification triggered)
    mock_llm.assert_called_once()

    # The rewritten file must have the current classifier_version and updated classification
    fm = _parse_frontmatter(memory_file.read_text())
    assert fm.get("classifier_version") == CLASSIFIER_VERSION
    assert fm.get("classification") == "human"
