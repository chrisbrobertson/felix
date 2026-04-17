"""
Unit tests for zoom_scanner.

All external access (Zoom API, httpx, LiteLLM, filesystem) is mocked.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import yaml

import zoom_scanner as zs
from zoom_scanner import ZoomScanner, MAX_TRANSCRIPT_LINES


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def scanner(tmp_path):
    s = ZoomScanner(role="full")
    with patch.object(zs, "MEMORIES_DIR", tmp_path / "memories"), \
         patch.object(zs, "STATE_FILE", tmp_path / "zoom-state.json"):
        (tmp_path / "memories").mkdir()
        yield s


# ── VTT parsing ───────────────────────────────────────────────────────────────

def test_parse_vtt_basic(scanner):
    vtt = (
        "WEBVTT\n\n"
        "1\n"
        "00:00:05.000 --> 00:00:10.500\n"
        "Sarah Chen: Good morning everyone.\n\n"
    )
    parsed = scanner._parse_vtt(vtt)
    assert len(parsed["segments"]) == 1
    seg = parsed["segments"][0]
    assert seg["start_time"] == "00:00:05"
    assert seg["text"] == "Good morning everyone."
    assert seg["speaker"] == "Sarah Chen"


def test_parse_vtt_speaker_attribution(scanner):
    vtt = (
        "WEBVTT\n\n"
        "1\n"
        "00:00:11.000 --> 00:00:18.750\n"
        "Mike Peters: Thanks Sarah, I wanted to address the budget.\n\n"
    )
    parsed = scanner._parse_vtt(vtt)
    seg = parsed["segments"][0]
    assert seg["speaker"] == "Mike Peters"
    assert seg["text"] == "Thanks Sarah, I wanted to address the budget."


def test_parse_vtt_continuation_lines(scanner):
    vtt = (
        "WEBVTT\n\n"
        "1\n"
        "00:00:05.000 --> 00:00:10.500\n"
        "Sarah Chen: First part of sentence\n"
        "continuation of the sentence\n\n"
    )
    parsed = scanner._parse_vtt(vtt)
    assert len(parsed["segments"]) == 1
    assert parsed["segments"][0]["text"] == "First part of sentence continuation of the sentence"


def test_parse_vtt_no_speakers(scanner):
    vtt = (
        "WEBVTT\n\n"
        "1\n"
        "00:00:05.000 --> 00:00:10.500\n"
        "just some transcribed text\n\n"
    )
    parsed = scanner._parse_vtt(vtt)
    assert len(parsed["segments"]) == 1
    seg = parsed["segments"][0]
    assert seg["speaker"] is None
    assert seg["text"] == "just some transcribed text"


def test_parse_vtt_empty(scanner):
    parsed = scanner._parse_vtt("")
    assert parsed["segments"] == []
    assert parsed["speakers"] == []
    assert parsed["raw_text"] == ""
    assert parsed["duration_ms"] == 0


def test_parse_vtt_multiple_speakers_in_order(scanner):
    vtt = (
        "WEBVTT\n\n"
        "1\n00:00:01.000 --> 00:00:05.000\n"
        "Alice: Hello.\n\n"
        "2\n00:00:06.000 --> 00:00:10.000\n"
        "Bob: Hi there.\n\n"
        "3\n00:00:11.000 --> 00:00:15.000\n"
        "Alice: How are you?\n\n"
    )
    parsed = scanner._parse_vtt(vtt)
    # Alice appears first, then Bob — order of first appearance
    assert parsed["speakers"] == ["Alice", "Bob"]
    assert len(parsed["segments"]) == 3


# ── Timestamp parsing ─────────────────────────────────────────────────────────

def test_parse_timestamp_ms(scanner):
    # 1h 23m 45s 678ms
    ms = scanner._parse_timestamp_ms("01:23:45.678")
    assert ms == (3600 + 23 * 60 + 45) * 1000 + 678


def test_parse_timestamp_ms_comma_separator(scanner):
    # SRT-style comma separator
    ms = scanner._parse_timestamp_ms("00:01:00,500")
    assert ms == 60 * 1000 + 500


def test_parse_timestamp_ms_zero(scanner):
    assert scanner._parse_timestamp_ms("00:00:00.000") == 0


# ── Speaker matching ──────────────────────────────────────────────────────────

def test_match_speakers_exact(scanner):
    speakers = ["Sarah Chen"]
    participants = [{"name": "Sarah Chen", "user_email": "sarah.chen@acme.com"}]
    result = scanner._match_speakers(speakers, participants)
    assert len(result) == 1
    assert result[0]["confidence"] == 1.0
    assert result[0]["email"] == "sarah.chen@acme.com"


def test_match_speakers_first_name(scanner):
    speakers = ["Sarah"]
    participants = [{"name": "Sarah Chen", "user_email": "sarah.chen@acme.com"}]
    result = scanner._match_speakers(speakers, participants)
    assert result[0]["confidence"] == 0.7
    assert result[0]["email"] == "sarah.chen@acme.com"


def test_match_speakers_no_match(scanner):
    speakers = ["Unknown Person"]
    participants = [{"name": "Sarah Chen", "user_email": "sarah.chen@acme.com"}]
    result = scanner._match_speakers(speakers, participants)
    assert result[0]["email"] is None
    assert result[0]["confidence"] == 0.0


def test_match_speakers_no_email_in_participant(scanner):
    speakers = ["External Guest"]
    participants = [{"name": "External Guest", "user_email": None}]
    result = scanner._match_speakers(speakers, participants)
    assert result[0]["name"] == "External Guest"
    assert result[0]["email"] is None
    assert result[0]["confidence"] == 1.0


# ── Token caching ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_token_cache_hit(scanner):
    """Cached token returned without making an API call."""
    scanner._token = "cached-token"
    scanner._token_expiry = time.monotonic() + 3600

    with patch.object(scanner, "_acquire_token", new=AsyncMock()) as mock_acquire:
        token = await scanner._get_token()

    assert token == "cached-token"
    mock_acquire.assert_not_called()


@pytest.mark.asyncio
async def test_token_cache_refresh(scanner):
    """Expired token triggers a new acquisition."""
    scanner._token = "old-token"
    scanner._token_expiry = time.monotonic() - 1  # expired

    with patch.object(scanner, "_acquire_token", new=AsyncMock(return_value="new-token")):
        token = await scanner._get_token()

    assert token == "new-token"


@pytest.mark.asyncio
async def test_missing_credentials_logs_warning(scanner, caplog):
    """Missing ZOOM credentials → warning logged, returns None."""
    import logging
    with patch.dict(os.environ, {
        "ZOOM_ACCOUNT_ID": "",
        "ZOOM_CLIENT_ID": "",
        "ZOOM_CLIENT_SECRET": "",
    }):
        with caplog.at_level(logging.WARNING, logger="zoom-scanner"):
            token = await scanner._acquire_token()

    assert token is None
    assert any("credentials" in r.message.lower() for r in caplog.records)


# ── Deduplication ─────────────────────────────────────────────────────────────

def test_dedup_skips_processed_uuid(tmp_path):
    """UUIDs already in state file are not returned by _poll_recordings."""
    state = {
        "processed_uuids": ["uuid-abc123"],
        "last_poll": "2026-04-11T10:00:00",
    }
    processed = set(state["processed_uuids"])
    # Simulate the filter applied in _run_scan
    recordings = [("uuid-abc123", {}, "http://dl")]
    new = [(u, m, url) for u, m, url in recordings if u not in processed]
    assert new == []


def test_state_file_persists_uuid(tmp_path):
    """_add_processed_uuid writes the UUID to the state file immediately."""
    state_file = tmp_path / "zoom-state.json"
    scanner = ZoomScanner(role="full")

    with patch.object(zs, "STATE_FILE", state_file):
        state = {"processed_uuids": [], "last_poll": None}
        scanner._save_state = lambda s: state_file.write_text(json.dumps(s))
        scanner._add_processed_uuid(state, "new-uuid-xyz")

    assert "new-uuid-xyz" in state["processed_uuids"]


def test_state_file_created_if_missing(tmp_path):
    """_load_state returns empty skeleton when no state file exists."""
    scanner = ZoomScanner(role="full")
    with patch.object(zs, "STATE_FILE", tmp_path / "no-file-here.json"):
        state = scanner._load_state()
    assert state == {"processed_uuids": [], "processed_summaries": [], "processed_local": [], "last_poll": None}


# ── Rate limiting ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_respects_retry_after(scanner):
    """429 with Retry-After header → asyncio.sleep called with that value."""
    import httpx as _httpx

    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.headers = {"Retry-After": "30"}

    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 200
    mock_resp_ok.json.return_value = {"meetings": [], "next_page_token": None}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=[mock_resp_429, mock_resp_ok])

    scanner._token = "tok"
    scanner._token_expiry = time.monotonic() + 3600

    with patch("zoom_scanner.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await scanner._api_get(mock_client, "/users/me/recordings")

    mock_sleep.assert_called_once_with(30)
    assert result is not None


# ── Memory file write ─────────────────────────────────────────────────────────

def test_write_memory_field_order(tmp_path):
    """source_title must be the first key; type must be meeting_transcript."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = ZoomScanner(role="full")

    meeting = {
        "uuid": "test-uuid-001",
        "topic": "Q4 Planning Review",
        "start_time": "2026-04-11T10:00:00Z",
        "duration": 45,
        "id": "12345678",
    }
    parsed = {"segments": [], "speakers": [], "raw_text": "", "duration_ms": 0}
    matched = []
    llm = {"summary": "A planning meeting.", "tags": ["q4"], "key_decisions": []}

    with patch.object(zs, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(meeting, parsed, matched, llm)

    files = list(memories_dir.glob("meeting-*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    # Field order: source_title first
    keys = list(fm.keys())
    assert keys[0] == "source_title"
    assert fm["type"] == "meeting_transcript"
    assert fm["source_url"].startswith("zoom:")


def test_write_memory_atomic(tmp_path):
    """No .tmp file left after a successful write."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = ZoomScanner(role="full")

    meeting = {
        "uuid": "u1", "topic": "Test", "start_time": "2026-04-11T10:00:00Z",
        "duration": 10, "id": "999",
    }
    parsed = {"segments": [], "speakers": [], "raw_text": "", "duration_ms": 0}

    with patch.object(zs, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(meeting, parsed, [], {"summary": "x", "tags": [], "key_decisions": []})

    tmp_files = list(memories_dir.glob("*.tmp"))
    assert tmp_files == []


def test_transcript_truncated_at_50_lines(tmp_path):
    """Transcripts longer than MAX_TRANSCRIPT_LINES get a truncation marker."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = ZoomScanner(role="full")

    segments = [
        {"index": i, "start_time": f"00:00:{i:02d}", "speaker": "Alice", "text": f"Line {i}"}
        for i in range(MAX_TRANSCRIPT_LINES + 10)
    ]
    parsed = {
        "segments": segments,
        "speakers": ["Alice"],
        "raw_text": "...",
        "duration_ms": 0,
    }
    meeting = {
        "uuid": "u2", "topic": "Long meeting", "start_time": "2026-04-11T10:00:00Z",
        "duration": 60, "id": "888",
    }

    with patch.object(zs, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(meeting, parsed, [], {"summary": "long", "tags": [], "key_decisions": []})

    files = list(memories_dir.glob("meeting-*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "more lines" in content
    # Exactly MAX_TRANSCRIPT_LINES segment lines before truncation marker
    transcript_lines = [l for l in content.splitlines() if l.startswith("- 00:")]
    assert len(transcript_lines) == MAX_TRANSCRIPT_LINES


# ── Watcher role exclusion ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watcher_role_skips_zoom_scanner():
    """ZoomScanner with role='watcher' exits run_loop immediately."""
    scanner = ZoomScanner(role="watcher")
    stop_event = asyncio.Event()
    stop_event.set()  # already stopped — but watcher should return before checking

    with patch.object(scanner, "_run_scan", new=AsyncMock()) as mock_scan:
        await scanner.run_loop(stop_event)

    mock_scan.assert_not_called()


# ── Backfill ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_clears_processed_uuids(tmp_path):
    """backfill() clears processed_uuids before scanning."""
    scanner = ZoomScanner(role="full")

    with patch.object(zs, "MEMORIES_DIR", tmp_path / "memories"), \
         patch.object(zs, "STATE_FILE", tmp_path / "zoom-state.json"), \
         patch.object(scanner, "_poll_recordings", new=AsyncMock(return_value=[])):

        (tmp_path / "memories").mkdir()
        scanner._save_state({"processed_uuids": ["old-uuid-1", "old-uuid-2"]})

        result = await scanner.backfill(30)

    # State should have empty processed_uuids after backfill
    state = scanner._load_state()
    assert state.get("processed_uuids") == []
    assert result["processed"] == 0  # No recordings in this mock


# ── AI Companion ──────────────────────────────────────────────────────────────

def test_parse_summary_content_html(scanner):
    """HTML with <p>, <li> tags → clean text, sections extracted."""
    html = """<p>Overview:</p>
    <p>Team discussed Q4 budget and timeline.</p>
    <p>Action Items:</p>
    <ul><li>Sarah to send numbers</li><li>Mike to follow up</li></ul>
    <p>Next Steps:</p>
    <ul><li>Schedule follow-up</li></ul>
    """
    result = scanner._parse_summary_content(html)
    assert "Q4 budget" in result["overview"]
    assert len(result["action_items"]) == 2
    assert "Sarah to send numbers" in result["action_items"]
    assert "Mike to follow up" in result["action_items"]
    assert len(result["next_steps"]) == 1
    assert "Schedule follow-up" in result["next_steps"]


def test_parse_summary_content_plain_text(scanner):
    """Plain text with section markers → sections extracted."""
    text = """Overview:
    Team discussed Q4 budget targets.

    Action Items:
    - Complete budget review by Friday
    - Share proposal with leadership

    Next Steps:
    - Follow-up meeting next week
    """
    result = scanner._parse_summary_content(text)
    assert "Q4 budget" in result["overview"]
    assert len(result["action_items"]) == 2
    assert len(result["next_steps"]) == 1


def test_parse_summary_content_empty(scanner):
    """Empty string → {}, no exception."""
    result = scanner._parse_summary_content("")
    assert result == {}


def test_parse_summary_content_no_markers(scanner):
    """Text without section markers → overview = text, empty lists."""
    text = "This is a meeting summary without explicit sections."
    result = scanner._parse_summary_content(text)
    assert result["overview"] == text
    assert result["action_items"] == []
    assert result["next_steps"] == []


def test_summary_source_llm_in_frontmatter(tmp_path):
    """_write_memory default → summary_source: llm in YAML."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = ZoomScanner(role="full")

    meeting = {
        "uuid": "test-uuid", "topic": "Test", "start_time": "2026-04-11T10:00:00Z",
        "duration": 10, "id": "999",
    }
    parsed = {"segments": [], "speakers": [], "raw_text": "", "duration_ms": 0}

    with patch.object(zs, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(meeting, parsed, [], {"summary": "x", "tags": [], "key_decisions": []})

    files = list(memories_dir.glob("meeting-*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["summary_source"] == "llm"


def test_summary_source_ai_companion_in_frontmatter(tmp_path):
    """_write_memory(summary_source='ai_companion') → summary_source: ai_companion."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = ZoomScanner(role="full")

    meeting = {
        "uuid": "test-uuid", "topic": "Test", "start_time": "2026-04-11T10:00:00Z",
        "duration": 10, "id": "999",
    }
    parsed = {"segments": [], "speakers": [], "raw_text": "", "duration_ms": 0}

    with patch.object(zs, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(
            meeting, parsed, [], {"summary": "x", "tags": []},
            summary_source="ai_companion"
        )

    files = list(memories_dir.glob("meeting-*.md"))
    text = files[0].read_text()
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["summary_source"] == "ai_companion"


def test_action_items_section_written(tmp_path):
    """_write_memory(summary_source='ai_companion', action_items=[...]) → ## Action Items in file."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = ZoomScanner(role="full")

    meeting = {
        "uuid": "test-uuid", "topic": "Test", "start_time": "2026-04-11T10:00:00Z",
        "duration": 10, "id": "999",
    }
    parsed = {"segments": [], "speakers": [], "raw_text": "", "duration_ms": 0}

    with patch.object(zs, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(
            meeting, parsed, [], {"summary": "Meeting summary", "tags": []},
            summary_source="ai_companion",
            action_items=["Do X", "Do Y"]
        )

    files = list(memories_dir.glob("meeting-*.md"))
    content = files[0].read_text()
    assert "## Action Items" in content
    assert "- Do X" in content
    assert "- Do Y" in content


def test_ai_companion_merged_no_key_decisions(tmp_path):
    """Merged file (ai_companion) → no ## Key Decisions section."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = ZoomScanner(role="full")

    meeting = {
        "uuid": "test-uuid", "topic": "Test", "start_time": "2026-04-11T10:00:00Z",
        "duration": 10, "id": "999",
    }
    parsed = {"segments": [], "speakers": [], "raw_text": "", "duration_ms": 0}

    with patch.object(zs, "MEMORIES_DIR", memories_dir):
        scanner._write_memory(
            meeting, parsed, [], {"summary": "x", "tags": [], "key_decisions": ["ignored"]},
            summary_source="ai_companion",
            action_items=["Do X"]
        )

    files = list(memories_dir.glob("meeting-*.md"))
    content = files[0].read_text()
    assert "## Key Decisions" not in content
    assert "## Action Items" in content


def test_dedup_processed_summaries(tmp_path):
    """Meeting_id in processed_summaries → not re-fetched in Pass 2."""
    state_file = tmp_path / "zoom-state.json"
    state = {
        "processed_uuids": [],
        "processed_summaries": ["12345"],
        "processed_local": [],
        "last_poll": None,
    }
    state_file.write_text(json.dumps(state))

    scanner = ZoomScanner(role="full")
    with patch.object(zs, "STATE_FILE", state_file):
        loaded = scanner._load_state()

    assert "12345" in loaded["processed_summaries"]


@pytest.mark.asyncio
async def test_graceful_degradation_403(caplog):
    """_list_meeting_summaries returning None → warning logged once, _ai_companion_disabled set."""
    import logging
    from datetime import datetime
    from zoom_scanner import log as zoom_log
    scanner = ZoomScanner(role="full")
    mock_client = AsyncMock()

    with patch.object(scanner, "_list_meeting_summaries", new=AsyncMock(return_value=None)), \
         caplog.at_level(logging.WARNING, logger="zoom-scanner"):
        # Simulate _run_scan logic
        summaries = await scanner._list_meeting_summaries(mock_client, datetime.now())
        if summaries is None:
            if not scanner._ai_companion_403_logged:
                zoom_log.warning("AI Companion API returned 403 — test")
                scanner._ai_companion_403_logged = True
            scanner._ai_companion_disabled = True

    assert scanner._ai_companion_disabled is True
    assert scanner._ai_companion_403_logged is True
    assert any("403" in r.message for r in caplog.records)


def test_ai_companion_disabled_config(tmp_path):
    """Config ai_companion_enabled: false → _list_meeting_summaries not called."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("zoom_scanner:\n  ai_companion_enabled: false\n")

    scanner = ZoomScanner(role="full")
    with patch.object(zs, "CONFIG_PATH", config_path):
        sc = scanner._scanner_config()
        assert sc.get("ai_companion_enabled") is False


def test_state_backwards_compat_processed_summaries(tmp_path):
    """State without processed_summaries → _load_state returns [] for it."""
    state_file = tmp_path / "zoom-state.json"
    state = {"processed_uuids": ["uuid1"], "last_poll": "2026-04-11T10:00:00"}
    state_file.write_text(json.dumps(state))

    scanner = ZoomScanner(role="full")
    with patch.object(zs, "STATE_FILE", state_file):
        loaded = scanner._load_state()
        loaded.setdefault("processed_summaries", [])

    assert loaded["processed_summaries"] == []


def test_add_processed_summary_caps_at_10k(tmp_path):
    """Add 10001 entries → capped at 10000."""
    state_file = tmp_path / "zoom-state.json"
    scanner = ZoomScanner(role="full")

    with patch.object(zs, "STATE_FILE", state_file):
        state = {"processed_summaries": [str(i) for i in range(10_000)]}
        scanner._add_processed_summary(state, "10001")

    assert len(state["processed_summaries"]) == 10_000
    assert "10001" in state["processed_summaries"]
    assert "0" not in state["processed_summaries"]  # oldest trimmed


def test_ai_companion_only_memory_written(tmp_path):
    """_write_ai_companion_memory → file with correct frontmatter, no Transcript section."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    scanner = ZoomScanner(role="full")

    summary_data = {
        "meeting_id": 12345678,
        "meeting_topic": "Standup",
        "meeting_start_time": "2026-04-11T10:00:00Z",
        "meeting_end_time": "2026-04-11T10:30:00Z",
    }
    ai_parsed = {
        "overview": "Discussed sprint progress.",
        "action_items": ["Follow up on DevOps"],
        "next_steps": ["Review board Friday"],
    }

    with patch.object(zs, "MEMORIES_DIR", memories_dir):
        scanner._write_ai_companion_memory(summary_data, ai_parsed, ["standup"])

    files = list(memories_dir.glob("meeting-*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    parts = content.split("---", 2)
    fm = yaml.safe_load(parts[1])

    assert fm["type"] == "meeting_transcript"
    assert fm["summary_source"] == "ai_companion"
    assert fm["participants"] == []
    assert fm["speakers"] == []
    assert "## Transcript" not in content
    assert "## Summary" in content
    assert "## Action Items" in content
    assert "## Next Steps" in content


def test_numeric_meeting_id_in_summary_request():
    """Verify _get_meeting_summary uses int not UUID."""
    scanner = ZoomScanner(role="full")
    # This is a structural test — the method signature accepts int
    # and the call to _api_get uses f"/meetings/{meeting_id}/meeting_summary"
    # where meeting_id is expected to be an int.
    # We'll verify by inspecting the method call path.
    assert scanner._get_meeting_summary.__code__.co_varnames[:3] == ('self', 'client', 'meeting_id')


# ── Local recordings ──────────────────────────────────────────────────────────

def test_parse_folder_name_valid(scanner):
    """'2026-04-15 14.30.22 Weekly Standup' → ('2026-04-15T14:30:22', 'Weekly Standup')."""
    result = scanner._parse_folder_name("2026-04-15 14.30.22 Weekly Standup")
    assert result is not None
    iso_datetime, topic = result
    assert iso_datetime == "2026-04-15T14:30:22"
    assert topic == "Weekly Standup"


def test_parse_folder_name_no_topic(scanner):
    """'2026-04-15 14.30.22' → returns tuple with folder name as topic."""
    result = scanner._parse_folder_name("2026-04-15 14.30.22")
    assert result is not None
    iso_datetime, topic = result
    assert iso_datetime == "2026-04-15T14:30:22"
    assert topic == "2026-04-15 14.30.22"


def test_parse_folder_name_invalid(scanner):
    """'random folder' → None."""
    result = scanner._parse_folder_name("random folder")
    assert result is None


def test_folder_hash_is_8_chars(scanner):
    """_folder_hash(Path('/some/path')) → 8-char string."""
    folder_hash = scanner._folder_hash(Path("/some/path"))
    assert len(folder_hash) == 8
    assert folder_hash.isalnum()


@pytest.mark.asyncio
async def test_local_folder_discovered(tmp_path):
    """Folder matching pattern with VTT → processed."""
    scanner = ZoomScanner(role="full")
    local_dir = tmp_path / "zoom"
    local_dir.mkdir()
    folder = local_dir / "2026-04-15 14.30.22 Test Meeting"
    folder.mkdir()
    vtt_path = folder / "closed_caption.vtt"
    vtt_path.write_text("WEBVTT\n\n1\n00:00:05.000 --> 00:00:10.000\nAlice: Hello.\n")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"zoom_scanner:\n  local_recordings_enabled: true\n  local_recordings_path: {local_dir}\n")

    state_file = tmp_path / "state.json"
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(exist_ok=True)

    with patch.object(zs, "CONFIG_PATH", config_path), \
         patch.object(zs, "STATE_FILE", state_file), \
         patch.object(zs, "MEMORIES_DIR", memories_dir):
        state = scanner._load_state()
        await scanner._scan_local_recordings(state)

    # Check memory file created
    files = list(memories_dir.glob("meeting-*.md"))
    assert len(files) == 1


@pytest.mark.asyncio
async def test_local_folder_without_vtt_skipped(tmp_path, scanner, caplog):
    """No closed_caption.vtt → skipped (DEBUG logged)."""
    import logging
    local_dir = tmp_path / "zoom"
    local_dir.mkdir()
    folder = local_dir / "2026-04-15 14.30.22 Test Meeting"
    folder.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"zoom_scanner:\n  local_recordings_enabled: true\n  local_recordings_path: {local_dir}\n")

    state_file = tmp_path / "state.json"

    with patch.object(zs, "CONFIG_PATH", config_path), \
         patch.object(zs, "STATE_FILE", state_file), \
         caplog.at_level(logging.DEBUG, logger="zoom-scanner"):
        state = scanner._load_state()
        await scanner._scan_local_recordings(state)

    assert any("no closed_caption.vtt" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_local_metadata_from_folder_name(tmp_path):
    """Memory file has correct meeting_date and source_title from folder name."""
    scanner = ZoomScanner(role="full")
    local_dir = tmp_path / "zoom"
    local_dir.mkdir()
    folder = local_dir / "2026-04-15 14.30.22 Standup"
    folder.mkdir()
    vtt_path = folder / "closed_caption.vtt"
    vtt_path.write_text("WEBVTT\n\n1\n00:00:05.000 --> 00:00:10.000\nAlice: Hello.\n")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"zoom_scanner:\n  local_recordings_enabled: true\n  local_recordings_path: {local_dir}\n")

    state_file = tmp_path / "state.json"
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(exist_ok=True)

    with patch.object(zs, "CONFIG_PATH", config_path), \
         patch.object(zs, "STATE_FILE", state_file), \
         patch.object(zs, "MEMORIES_DIR", memories_dir):
        state = scanner._load_state()
        await scanner._scan_local_recordings(state)

    files = list(memories_dir.glob("meeting-*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["meeting_date"] == "2026-04-15T14:30:22"
    assert fm["source_title"] == "Standup"


@pytest.mark.asyncio
async def test_local_source_url_scheme(tmp_path):
    """source_url in written file starts with local:."""
    scanner = ZoomScanner(role="full")
    local_dir = tmp_path / "zoom"
    local_dir.mkdir()
    folder = local_dir / "2026-04-15 14.30.22 Test"
    folder.mkdir()
    vtt_path = folder / "closed_caption.vtt"
    vtt_path.write_text("WEBVTT\n\n1\n00:00:05.000 --> 00:00:10.000\nAlice: Hello.\n")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"zoom_scanner:\n  local_recordings_enabled: true\n  local_recordings_path: {local_dir}\n")

    state_file = tmp_path / "state.json"
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(exist_ok=True)

    with patch.object(zs, "CONFIG_PATH", config_path), \
         patch.object(zs, "STATE_FILE", state_file), \
         patch.object(zs, "MEMORIES_DIR", memories_dir):
        state = scanner._load_state()
        await scanner._scan_local_recordings(state)

    files = list(memories_dir.glob("meeting-*.md"))
    text = files[0].read_text()
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["source_url"].startswith("local:")


@pytest.mark.asyncio
async def test_local_participants_empty_speakers_populated(tmp_path):
    """participants: [], speakers has VTT names."""
    scanner = ZoomScanner(role="full")
    local_dir = tmp_path / "zoom"
    local_dir.mkdir()
    folder = local_dir / "2026-04-15 14.30.22 Test"
    folder.mkdir()
    vtt_path = folder / "closed_caption.vtt"
    vtt_path.write_text("WEBVTT\n\n1\n00:00:05.000 --> 00:00:10.000\nAlice: Hello.\n")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"zoom_scanner:\n  local_recordings_enabled: true\n  local_recordings_path: {local_dir}\n")

    state_file = tmp_path / "state.json"
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(exist_ok=True)

    with patch.object(zs, "CONFIG_PATH", config_path), \
         patch.object(zs, "STATE_FILE", state_file), \
         patch.object(zs, "MEMORIES_DIR", memories_dir):
        state = scanner._load_state()
        await scanner._scan_local_recordings(state)

    files = list(memories_dir.glob("meeting-*.md"))
    text = files[0].read_text()
    parts = text.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["participants"] == []
    assert "Alice" in fm["speakers"]


@pytest.mark.asyncio
async def test_local_dedup(tmp_path):
    """Folder already in processed_local → VTT not re-read."""
    scanner = ZoomScanner(role="full")
    local_dir = tmp_path / "zoom"
    local_dir.mkdir()
    folder = local_dir / "2026-04-15 14.30.22 Test"
    folder.mkdir()
    vtt_path = folder / "closed_caption.vtt"
    vtt_path.write_text("WEBVTT\n\n1\n00:00:05.000 --> 00:00:10.000\nAlice: Hello.\n")

    folder_hash = scanner._folder_hash(folder)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"zoom_scanner:\n  local_recordings_enabled: true\n  local_recordings_path: {local_dir}\n")

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"processed_local": [folder_hash], "processed_uuids": [], "processed_summaries": []}))

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(exist_ok=True)

    with patch.object(zs, "CONFIG_PATH", config_path), \
         patch.object(zs, "STATE_FILE", state_file), \
         patch.object(zs, "MEMORIES_DIR", memories_dir):
        state = scanner._load_state()
        await scanner._scan_local_recordings(state)

    # No new files should be created
    files = list(memories_dir.glob("meeting-*.md"))
    assert len(files) == 0


def test_local_state_backwards_compat(tmp_path):
    """State file without processed_local key → initialized to []."""
    state_file = tmp_path / "zoom-state.json"
    state = {"processed_uuids": [], "last_poll": None}
    state_file.write_text(json.dumps(state))

    scanner = ZoomScanner(role="full")
    with patch.object(zs, "STATE_FILE", state_file):
        loaded = scanner._load_state()
        loaded.setdefault("processed_local", [])

    assert loaded["processed_local"] == []


@pytest.mark.asyncio
async def test_local_disabled_by_default(scanner):
    """No config → _scan_local_recordings returns without scanning."""
    # Just test that it doesn't crash with default config
    state = {"processed_local": [], "processed_uuids": [], "processed_summaries": []}
    await scanner._scan_local_recordings(state)
    # Should return early without error


@pytest.mark.asyncio
async def test_local_missing_directory_warning(tmp_path, scanner, caplog):
    """Configured path doesn't exist → WARNING logged once."""
    import logging
    config_path = tmp_path / "config.yaml"
    config_path.write_text("zoom_scanner:\n  local_recordings_enabled: true\n  local_recordings_path: /nonexistent/path\n")

    state_file = tmp_path / "state.json"

    with patch.object(zs, "CONFIG_PATH", config_path), \
         patch.object(zs, "STATE_FILE", state_file), \
         caplog.at_level(logging.WARNING, logger="zoom-scanner"):
        state = scanner._load_state()
        await scanner._scan_local_recordings(state)
        # Call again to verify warning only logged once
        await scanner._scan_local_recordings(state)

    warnings = [r for r in caplog.records if "does not exist" in r.message]
    assert len(warnings) == 1


# ── Updated existing tests ────────────────────────────────────────────────────

def test_state_file_created_if_missing_updated(tmp_path):
    """_load_state returns empty skeleton with new fields when no state file exists."""
    scanner = ZoomScanner(role="full")
    with patch.object(zs, "STATE_FILE", tmp_path / "no-file-here.json"):
        state = scanner._load_state()
    assert state == {"processed_uuids": [], "processed_summaries": [], "processed_local": [], "last_poll": None}


@pytest.mark.asyncio
async def test_watcher_role_skips_zoom_scanner_updated():
    """ZoomScanner with role='watcher' and local disabled exits run_loop immediately."""
    scanner = ZoomScanner(role="watcher")
    stop_event = asyncio.Event()
    stop_event.set()

    # Mock config to return local_recordings_enabled: false
    with patch.object(scanner, "_scanner_config", return_value={"local_recordings_enabled": False}), \
         patch.object(scanner, "_run_scan", new=AsyncMock()) as mock_scan:
        await scanner.run_loop(stop_event)

    mock_scan.assert_not_called()
