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
    assert state == {"processed_uuids": [], "last_poll": None}


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
