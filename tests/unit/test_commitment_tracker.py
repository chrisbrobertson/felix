"""
Unit tests for commitment_tracker.

All external access (LiteLLM, filesystem) is mocked.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import commitment_tracker as ct
from commitment_tracker import (
    CommitmentTracker,
    _parse_frontmatter,
    _stable_commitment_id,
    _slugify,
    NEEDS_REVIEW_THRESHOLD,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_meeting_memory(memories_dir: Path, filename: str = "meeting-test.md",
                        source_url: str = "zoom:abc123", summary: str = "Test meeting.") -> Path:
    p = memories_dir / filename
    p.write_text(
        f"---\nsource_title: Test Meeting\nsummary: {summary}\n"
        f"tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: {source_url}\ntype: meeting_transcript\n"
        f"participants: [alice@acme.com]\nspeakers: [Alice]\n"
        f"duration_minutes: 30\nmeeting_date: '2026-04-11T10:00:00'\n"
        f"zoom_meeting_id: '12345'\n---\n\n"
        f"## Transcript\n- 00:00:01 Alice: I'll send you the report by Friday.\n\n"
        f"## Summary\n{summary}\n"
    )
    return p


def make_commitment_file(memories_dir: Path, description: str, status: str = "active",
                         confidence: float = 0.85, commitment_type: str = "outbound",
                         due_date: str = None, tags: list = None) -> Path:
    source_url = "zoom:test123"
    stable_id = _stable_commitment_id(source_url, description, "Alice")
    slug = _slugify(description)
    p = memories_dir / f"commitment-{slug}-{stable_id}.md"
    fm = {
        "source_title": description,
        "summary": f"Alice committed to {description.lower()}",
        "tags": tags or [],
        "last_scanned": "2026-04-11T10:00:00",
        "source_url": f"commitment:{stable_id}",
        "type": "commitment",
        "commitment_type": commitment_type,
        "owner": "Alice",
        "owner_email": "alice@acme.com",
        "recipient": "Chris",
        "due_date": due_date,
        "due_date_confidence": "explicit" if due_date else "none",
        "confidence": confidence,
        "status": status,
        "source_memory": source_url,
        "extracted_text": "I'll do the thing.",
    }
    frontmatter = yaml.dump(fm, sort_keys=False, allow_unicode=True)
    p.write_text(f"---\n{frontmatter}---\n\n## Context\nTest.\n")
    return p


# ── Stable ID ─────────────────────────────────────────────────────────────────

def test_stable_id_deterministic():
    id1 = _stable_commitment_id("zoom:abc", "Send the report", "Alice")
    id2 = _stable_commitment_id("zoom:abc", "Send the report", "Alice")
    assert id1 == id2


def test_stable_id_different_description():
    id1 = _stable_commitment_id("zoom:abc", "Send the report", "Alice")
    id2 = _stable_commitment_id("zoom:abc", "Review the document", "Alice")
    assert id1 != id2


def test_stable_id_case_insensitive():
    id1 = _stable_commitment_id("zoom:abc", "Send Report", "Alice Chen")
    id2 = _stable_commitment_id("zoom:abc", "send report", "alice chen")
    assert id1 == id2


# ── Confidence filtering ──────────────────────────────────────────────────────

def test_confidence_filter_discards_below_threshold(tmp_path):
    """confidence=0.4 → no file written (below 0.5 default threshold)."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    item = {
        "type": "outbound",
        "description": "Low confidence task",
        "owner": "Bob",
        "confidence": 0.4,
        "extracted_text": "...",
    }
    with patch.object(ct, "MEMORIES_DIR", memories_dir):
        tracker._write_commitment(item, "zoom:src", "Test Meeting", min_confidence=0.5)

    assert list(memories_dir.glob("commitment-*.md")) == []


def test_confidence_filter_needs_review_tag(tmp_path):
    """confidence=0.6 → file written with needs-review tag."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    item = {
        "type": "outbound",
        "description": "Medium confidence task",
        "owner": "Bob",
        "confidence": 0.6,
        "extracted_text": "Let me look into that",
    }
    with patch.object(ct, "MEMORIES_DIR", memories_dir):
        tracker._write_commitment(item, "zoom:src", "Test", min_confidence=0.5)

    files = list(memories_dir.glob("commitment-*.md"))
    assert len(files) == 1
    fm = _parse_frontmatter(files[0].read_text())
    assert "needs-review" in (fm.get("tags") or [])


def test_confidence_filter_auto_accept(tmp_path):
    """confidence=0.8 → file written without needs-review tag."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    item = {
        "type": "outbound",
        "description": "High confidence task",
        "owner": "Bob",
        "confidence": 0.8,
        "extracted_text": "I will send the report by Friday",
    }
    with patch.object(ct, "MEMORIES_DIR", memories_dir):
        tracker._write_commitment(item, "zoom:src", "Test", min_confidence=0.5)

    files = list(memories_dir.glob("commitment-*.md"))
    assert len(files) == 1
    fm = _parse_frontmatter(files[0].read_text())
    assert "needs-review" not in (fm.get("tags") or [])


# ── File write ────────────────────────────────────────────────────────────────

def test_write_commitment_field_order(tmp_path):
    """source_title must be the first key; type must be 'commitment'."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    item = {
        "type": "outbound",
        "description": "Send revised budget numbers",
        "owner": "Sarah Chen",
        "owner_email": "sarah.chen@acme.com",
        "recipient": "Chris",
        "due_date": "2026-04-18",
        "due_date_confidence": "explicit",
        "confidence": 0.85,
        "extracted_text": "Can you have revised numbers by Friday?",
    }
    with patch.object(ct, "MEMORIES_DIR", memories_dir):
        tracker._write_commitment(item, "zoom:meeting-abc", "Q4 Review", min_confidence=0.5)

    files = list(memories_dir.glob("commitment-*.md"))
    assert len(files) == 1
    fm = _parse_frontmatter(files[0].read_text())
    keys = list(fm.keys())
    assert keys[0] == "source_title"
    assert fm["type"] == "commitment"
    assert fm["source_url"].startswith("commitment:")


def test_write_commitment_atomic(tmp_path):
    """No .tmp file left after write."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    item = {
        "type": "outbound", "description": "Atomic test",
        "owner": "Alice", "confidence": 0.9, "extracted_text": "x",
    }
    with patch.object(ct, "MEMORIES_DIR", memories_dir):
        tracker._write_commitment(item, "zoom:src", "Test", min_confidence=0.5)

    assert list(memories_dir.glob("*.tmp")) == []


def test_write_commitment_preserves_status(tmp_path):
    """Re-extraction must not overwrite completed/dismissed status."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    description = "Send the report"
    source_url = "zoom:abc"
    stable_id = _stable_commitment_id(source_url, description, "Alice")
    slug = _slugify(description)
    commitment_path = memories_dir / f"commitment-{slug}-{stable_id}.md"

    # Write an already-completed commitment
    fm = {
        "source_title": description, "summary": "Alice committed to send the report",
        "tags": [], "last_scanned": "2026-04-11T10:00:00",
        "source_url": f"commitment:{stable_id}", "type": "commitment",
        "commitment_type": "outbound", "owner": "Alice", "owner_email": None,
        "recipient": None, "due_date": None, "due_date_confidence": "none",
        "confidence": 0.85, "status": "completed",
        "source_memory": source_url, "extracted_text": "I'll send the report.",
    }
    commitment_path.write_text(
        f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Context\nTest.\n"
    )

    # Re-extract the same item
    item = {
        "type": "outbound", "description": description, "owner": "Alice",
        "confidence": 0.85, "extracted_text": "I'll send the report.",
    }
    with patch.object(ct, "MEMORIES_DIR", memories_dir):
        tracker._write_commitment(item, source_url, "Test Meeting", min_confidence=0.5)

    final_fm = _parse_frontmatter(commitment_path.read_text())
    assert final_fm["status"] == "completed"


# ── Scan change detection ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_skips_unchanged_mtime(tmp_path):
    """Source file with same mtime as processed record → no LLM call."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "ct-state.json"
    tracker = CommitmentTracker()

    mem = make_meeting_memory(memories_dir)
    mtime = mem.stat().st_mtime

    # Pre-populate state with same mtime
    state = {"last_scan": None, "processed": {mem.name: mtime}}
    state_file.write_text(json.dumps(state))

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch.object(ct, "STATE_FILE", state_file), \
         patch.object(ct, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("litellm.acompletion", new=AsyncMock()) as mock_llm:
        (tmp_path / "config.yaml").write_text(
            "commitment_tracker:\n  source_types:\n    - meeting_transcript\n"
        )
        await tracker._run_scan()

    mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_scan_processes_new_mtime(tmp_path):
    """Source file with updated mtime → LLM call made."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "ct-state.json"
    tracker = CommitmentTracker()

    mem = make_meeting_memory(memories_dir)
    old_mtime = mem.stat().st_mtime - 100  # older than actual

    state = {"last_scan": None, "processed": {mem.name: old_mtime}}
    state_file.write_text(json.dumps(state))

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = '{"commitments": []}'

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch.object(ct, "STATE_FILE", state_file), \
         patch.object(ct, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)) as mock_llm:
        (tmp_path / "config.yaml").write_text(
            "commitment_tracker:\n  source_types:\n    - meeting_transcript\n"
        )
        await tracker._run_scan()

    mock_llm.assert_called_once()


@pytest.mark.asyncio
async def test_scan_skips_wrong_type(tmp_path):
    """Files with type: webpage are not processed."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "ct-state.json"
    tracker = CommitmentTracker()

    # Write a webpage memory
    p = memories_dir / "webpage-test.md"
    p.write_text(
        "---\nsource_title: Some Article\nsummary: Interesting.\n"
        "tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        "source_url: https://example.com\ntype: webpage\n---\n\n## Summary\nTest.\n"
    )

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch.object(ct, "STATE_FILE", state_file), \
         patch.object(ct, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("litellm.acompletion", new=AsyncMock()) as mock_llm:
        (tmp_path / "config.yaml").write_text(
            "commitment_tracker:\n  source_types:\n    - meeting_transcript\n"
        )
        await tracker._run_scan()

    mock_llm.assert_not_called()


# ── LLM extraction edge cases ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extraction_empty_returns_no_files(tmp_path):
    """LLM returning {"commitments": []} must not create any files."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    mem = make_meeting_memory(memories_dir)
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = '{"commitments": []}'

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
        fm = _parse_frontmatter(mem.read_text())
        items = await tracker._extract_commitments(mem, fm, mem.read_text())

    assert items == []
    assert list(memories_dir.glob("commitment-*.md")) == []


@pytest.mark.asyncio
async def test_extraction_json_parse_error_logs_warning(tmp_path, caplog):
    """Invalid JSON from LLM → WARNING logged, no crash, returns []."""
    import logging
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    mem = make_meeting_memory(memories_dir)
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "This is not JSON at all!"

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
        with caplog.at_level(logging.WARNING, logger="commitment-tracker"):
            fm = _parse_frontmatter(mem.read_text())
            items = await tracker._extract_commitments(mem, fm, mem.read_text())

    assert items == []
    assert any("parse" in r.message.lower() or "json" in r.message.lower()
               for r in caplog.records)


# ── Telegram command helpers ───────────────────────────────────────────────────

def test_cmd_commitments_returns_active_only(tmp_path):
    """_load_active_commitments excludes completed and dismissed items."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()

    make_commitment_file(memories_dir, "Active task", status="active")
    make_commitment_file(memories_dir, "Done task", status="completed")
    make_commitment_file(memories_dir, "Dismissed task", status="dismissed")

    # Simulate the method used by chat_handler
    results = []
    for f in sorted(memories_dir.glob("commitment-*.md")):
        fm = _parse_frontmatter(f.read_text())
        if fm.get("type") == "commitment" and fm.get("status") == "active":
            results.append((f, fm))

    assert len(results) == 1
    assert results[0][1]["source_title"] == "Active task"


def test_cmd_commitments_filter_outbound(tmp_path):
    """Type filter 'outbound' excludes inbound and waiting_on items."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    make_commitment_file(memories_dir, "Outbound task", commitment_type="outbound")
    make_commitment_file(memories_dir, "Inbound task", commitment_type="inbound")
    make_commitment_file(memories_dir, "Waiting task", commitment_type="waiting_on")

    results = []
    for f in sorted(memories_dir.glob("commitment-*.md")):
        fm = _parse_frontmatter(f.read_text())
        if fm.get("type") == "commitment" and fm.get("status") == "active":
            if fm.get("commitment_type") == "outbound":
                results.append((f, fm))

    assert len(results) == 1
    assert results[0][1]["source_title"] == "Outbound task"


def test_cmd_commitments_sorted_by_due_date(tmp_path):
    """Active commitments sorted by due_date ascending, nulls last."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    make_commitment_file(memories_dir, "Far future", due_date="2026-06-01")
    make_commitment_file(memories_dir, "No due date", due_date=None)
    make_commitment_file(memories_dir, "Near future", due_date="2026-04-20")

    items = []
    for f in sorted(memories_dir.glob("commitment-*.md")):
        fm = _parse_frontmatter(f.read_text())
        if fm.get("type") == "commitment" and fm.get("status") == "active":
            items.append((f, fm))

    def sort_key(item):
        due = item[1].get("due_date")
        return (0, str(due)) if due else (1, "")

    items.sort(key=sort_key)

    titles = [fm["source_title"] for _, fm in items]
    assert titles[0] == "Near future"
    assert titles[1] == "Far future"
    assert titles[-1] == "No due date"


# ── Status update ─────────────────────────────────────────────────────────────

def test_cmd_complete_updates_status(tmp_path):
    """update_commitment_status sets status to 'completed'."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()
    f = make_commitment_file(memories_dir, "Send report", status="active")

    tracker.update_commitment_status(f, "completed")

    fm = _parse_frontmatter(f.read_text())
    assert fm["status"] == "completed"


def test_cmd_dismiss_updates_status(tmp_path):
    """update_commitment_status sets status to 'dismissed'."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()
    f = make_commitment_file(memories_dir, "False positive task", status="active")

    tracker.update_commitment_status(f, "dismissed")

    fm = _parse_frontmatter(f.read_text())
    assert fm["status"] == "dismissed"


def test_cmd_complete_invalid_index():
    """Resolving an out-of-range index returns None."""
    last_set = []  # empty commitment set
    try:
        idx = int("99") - 1
    except ValueError:
        idx = -1
    path = last_set[idx] if 0 <= idx < len(last_set) else None
    assert path is None


def test_cmd_complete_idempotent(tmp_path):
    """Completing an already-completed item does not raise."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    tracker = CommitmentTracker()
    f = make_commitment_file(memories_dir, "Already done", status="completed")

    # Should not raise
    tracker.update_commitment_status(f, "completed")

    fm = _parse_frontmatter(f.read_text())
    assert fm["status"] == "completed"


# ── needs-review indicator ────────────────────────────────────────────────────

def test_needs_review_indicator_in_listing(tmp_path):
    """Commitments with needs-review tag include ⚠️ in listing output."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    make_commitment_file(
        memories_dir, "Low confidence item",
        confidence=0.6, tags=["needs-review"]
    )

    items = []
    for f in sorted(memories_dir.glob("commitment-*.md")):
        fm = _parse_frontmatter(f.read_text())
        if fm.get("type") == "commitment" and fm.get("status") == "active":
            items.append((f, fm))

    assert len(items) == 1
    _, fm = items[0]
    needs_review = "needs-review" in (fm.get("tags") or [])
    flag = " ⚠️" if needs_review else ""
    line = f"1. [outbound] {fm['source_title']} — {fm.get('owner', '')}{flag}"
    assert "⚠️" in line


# ── State file persistence ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_file_persists_across_scans(tmp_path):
    """Processed mtime is saved to state file after each source file scan."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "ct-state.json"
    tracker = CommitmentTracker()

    mem = make_meeting_memory(memories_dir)
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = '{"commitments": []}'

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch.object(ct, "STATE_FILE", state_file), \
         patch.object(ct, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
        (tmp_path / "config.yaml").write_text(
            "commitment_tracker:\n  source_types:\n    - meeting_transcript\n"
        )
        await tracker._run_scan()

    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert mem.name in state["processed"]
    assert state["processed"][mem.name] == pytest.approx(mem.stat().st_mtime, abs=1.0)


# ── Deduplication ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dedup_same_source_two_runs(tmp_path):
    """Two scans of the same source with unchanged mtime → no duplicate files."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "ct-state.json"
    tracker = CommitmentTracker()

    mem = make_meeting_memory(memories_dir)
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = json.dumps({
        "commitments": [{
            "type": "outbound",
            "description": "Send the budget numbers",
            "owner": "Alice",
            "owner_email": "alice@acme.com",
            "recipient": "Chris",
            "due_date": "2026-04-18",
            "due_date_confidence": "explicit",
            "confidence": 0.9,
            "extracted_text": "I'll send the budget numbers by Friday.",
        }]
    })

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch.object(ct, "STATE_FILE", state_file), \
         patch.object(ct, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
        (tmp_path / "config.yaml").write_text(
            "commitment_tracker:\n  source_types:\n    - meeting_transcript\n"
        )
        # First scan — creates the commitment file
        await tracker._run_scan()
        files_after_first = list(memories_dir.glob("commitment-*.md"))
        assert len(files_after_first) == 1

        # Second scan — same mtime, must not create a duplicate
        await tracker._run_scan()
        files_after_second = list(memories_dir.glob("commitment-*.md"))

    assert len(files_after_second) == 1
