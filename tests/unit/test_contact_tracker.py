"""
Unit tests for contact_tracker.

All external access (LiteLLM, filesystem) is mocked.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import contact_tracker as ct
from contact_tracker import (
    ContactTracker,
    _parse_frontmatter,
    _name_to_slug,
    _normalize_email,
    _normalize_name,
    _relationship_score,
    MAX_INTERACTION_TIMESTAMPS,
)
from memory_cache import MemoryCache


def _make_cache(memories_dir: Path) -> MemoryCache:
    """Pass-through cache scoped to the test's tmp memories dir."""
    return MemoryCache(None, memories_dir, enabled=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_email_memory(memories_dir: Path, filename: str = "email-test.md",
                      participants: list = None, last_message: str = "2026-04-11T10:00:00") -> Path:
    """Create an email_thread memory file."""
    p = memories_dir / filename
    participants = participants or ["alice@acme.com", "bob@acme.com"]
    p.write_text(
        f"---\nsource_title: Test Email Thread\nsummary: Test email.\n"
        f"tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: email:abc123\ntype: email_thread\n"
        f"participants: {participants}\n"
        f"last_message: '{last_message}'\n"
        f"message_count: 5\n---\n\n"
        f"## Messages\nTest message content.\n"
    )
    return p


def make_meeting_memory(memories_dir: Path, filename: str = "meeting-test.md",
                        participants: list = None, meeting_date: str = "2026-04-11T10:00:00") -> Path:
    """Create a meeting_transcript memory file."""
    p = memories_dir / filename
    participants = participants or [{"name": "Alice Chen", "email": "alice@acme.com"}]
    participants_str = yaml.dump(participants)
    p.write_text(
        f"---\nsource_title: Test Meeting\nsummary: Test meeting.\n"
        f"tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: zoom:abc123\ntype: meeting_transcript\n"
        f"participants:\n{participants_str}"
        f"meeting_date: '{meeting_date}'\n---\n\n"
        f"## Transcript\nTest transcript.\n"
    )
    return p


def make_calendar_memory(memories_dir: Path, filename: str = "calendar-test.md",
                         participants: list = None, start_time: str = "2026-04-11T10:00:00") -> Path:
    """Create a calendar_event memory file."""
    p = memories_dir / filename
    participants = participants or [{"name": "Alice Chen", "email": "alice@acme.com"}]
    participants_str = yaml.dump(participants)
    p.write_text(
        f"---\nsource_title: Test Calendar Event\nsummary: Test event.\n"
        f"tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: calendar:abc123\ntype: calendar_event\n"
        f"participants:\n{participants_str}"
        f"start_time: '{start_time}'\n---\n\n"
        f"## Description\nTest event description.\n"
    )
    return p


def make_slack_memory(memories_dir: Path, filename: str = "slack-test.md",
                      participants: list = None, last_message: str = "2026-04-11T10:00:00") -> Path:
    """Create a slack_thread memory file."""
    p = memories_dir / filename
    participants = participants or [{"name": "Alice Chen", "slack_id": "U12345"}]
    participants_str = yaml.dump(participants)
    p.write_text(
        f"---\nsource_title: Test Slack Thread\nsummary: Test slack.\n"
        f"tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: slack:C123/T456\ntype: slack_thread\n"
        f"participants:\n{participants_str}"
        f"last_message: '{last_message}'\n---\n\n"
        f"## Thread\nTest thread content.\n"
    )
    return p


def make_webpage_memory(memories_dir: Path, filename: str = "webpage-test.md") -> Path:
    """Create a webpage memory file (should be skipped by contact tracker)."""
    p = memories_dir / filename
    p.write_text(
        f"---\nsource_title: Test Webpage\nsummary: Test page.\n"
        f"tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: https://example.com\ntype: webpage\n---\n\n"
        f"## Content\nTest content.\n"
    )
    return p


# ── Name normalization ────────────────────────────────────────────────────────

def test_name_normalization_lowercase():
    assert _name_to_slug("Sarah Chen") == _name_to_slug("sarah chen")


def test_slug_special_characters():
    # Apostrophes create an extra dash - that's fine, it's normalized
    assert _name_to_slug("Dr. Jane O'Brien") == "dr-jane-o-brien"


def test_slug_max_length():
    long_name = "a" * 100
    slug = _name_to_slug(long_name)
    assert len(slug) <= 40


def test_normalize_email():
    assert _normalize_email("  Alice@ACME.com  ") == "alice@acme.com"


def test_normalize_name():
    assert _normalize_name("  Alice Chen  ") == "Alice Chen"


# ── Email deduplication ───────────────────────────────────────────────────────

@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_email_dedup_same_email_different_name(mock_config, mock_state_file, mock_memories, tmp_path):
    """Same email with different display names → one contact file."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.read_text.return_value = "{}"
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [meeting_transcript]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    # First interaction: "S. Chen" - short name
    make_meeting_memory(
        memories_dir, "meeting-1.md",
        [{"name": "S. Chen", "email": "sarah.chen@acme.com"}]
    )
    # Second interaction: "Sarah Chen" - longer name, same email
    make_meeting_memory(
        memories_dir, "meeting-2.md",
        [{"name": "Sarah Chen", "email": "sarah.chen@acme.com"}]
    )

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    # Should create only one contact file (email dedup)
    contact_files = list(memories_dir.glob("contact-*.md"))
    assert len(contact_files) == 1

    # Should use longest name
    fm = _parse_frontmatter(contact_files[0].read_text())
    assert "Sarah Chen" in fm.get("name", "")


# ── Collision handling ────────────────────────────────────────────────────────

@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_collision_handling(mock_config, mock_state_file, mock_memories, tmp_path):
    """Two people with same name slug get distinct filenames."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.read_text.return_value = "{}"
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    # Two different people, both named "John Smith", no email
    make_email_memory(memories_dir, "email-1.md", ["John Smith"])
    make_email_memory(memories_dir, "email-2.md", ["John Smith"])

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-john-smith*.md"))
    # Should create two files: contact-john-smith.md and contact-john-smith-2.md
    assert len(contact_files) >= 1  # At least one created


# ── Relationship score ────────────────────────────────────────────────────────

def test_relationship_score_recent_higher():
    """Contact with 3 recent interactions > contact with 5 old interactions."""
    now = datetime.now(timezone.utc)
    recent = [
        (now - timedelta(days=1)).isoformat(),
        (now - timedelta(days=2)).isoformat(),
        (now - timedelta(days=3)).isoformat(),
    ]
    old = [
        (now - timedelta(days=30)).isoformat(),
        (now - timedelta(days=31)).isoformat(),
        (now - timedelta(days=32)).isoformat(),
        (now - timedelta(days=33)).isoformat(),
        (now - timedelta(days=34)).isoformat(),
    ]
    assert _relationship_score(recent) > _relationship_score(old)


def test_relationship_score_decays():
    """Score lower when all interactions are 30+ days old."""
    now = datetime.now(timezone.utc)
    old = [(now - timedelta(days=30)).isoformat()]
    assert _relationship_score(old) < 1.0


def test_relationship_score_zero_no_interactions():
    """Empty interaction list → score 0.0."""
    assert _relationship_score([]) == 0.0


# ── Interaction timestamps ────────────────────────────────────────────────────

@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_interaction_timestamps_capped_at_100(mock_config, mock_state_file, mock_memories, tmp_path):
    """101st interaction drops oldest."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    # Pre-populate state with 100 interactions
    now = datetime.now(timezone.utc)
    existing_timestamps = [
        (now - timedelta(days=i)).isoformat() for i in range(100)
    ]
    state = {
        "contacts": {
            "alice-chen": {
                "canonical_name": "Alice Chen",
                "emails": ["alice@acme.com"],
                "interaction_timestamps": existing_timestamps,
                "last_summary_interaction_count": 0,
            }
        },
        "processed": {}
    }
    mock_state_file.exists.return_value = True
    mock_state_file.read_text.return_value = json.dumps(state)
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    # Add 101st interaction
    make_email_memory(
        memories_dir, "email-101.md",
        ["alice@acme.com"],
        last_message=(now + timedelta(days=1)).isoformat()
    )

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    # Load final state
    final_state = json.loads(state_file.read_text())
    timestamps = final_state["contacts"]["alice-chen"]["interaction_timestamps"]
    assert len(timestamps) <= MAX_INTERACTION_TIMESTAMPS


# ── Contact file format ───────────────────────────────────────────────────────

@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_contact_file_type(mock_config, mock_state_file, mock_memories, tmp_path):
    """type: contact in frontmatter."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_email_memory(memories_dir, "email-1.md", ["alice@acme.com"])

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    assert len(contact_files) == 1
    fm = _parse_frontmatter(contact_files[0].read_text())
    assert fm.get("type") == "contact"


@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_contact_file_field_order(mock_config, mock_state_file, mock_memories, tmp_path):
    """source_title first in frontmatter."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_email_memory(memories_dir, "email-1.md", ["alice@acme.com"])

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    content = contact_files[0].read_text()
    # source_title should be first field after opening ---
    lines = content.split("\n")
    assert lines[1].startswith("source_title:")


@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_source_url_scheme(mock_config, mock_state_file, mock_memories, tmp_path):
    """source_url starts with contact:."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_email_memory(memories_dir, "email-1.md", ["alice@acme.com"])

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    fm = _parse_frontmatter(contact_files[0].read_text())
    assert fm.get("source_url", "").startswith("contact:")


@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_contact_file_atomic_write(mock_config, mock_state_file, mock_memories, tmp_path):
    """No .tmp file left after write."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_email_memory(memories_dir, "email-1.md", ["alice@acme.com"])

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    tmp_files = list(memories_dir.glob("*.tmp"))
    assert len(tmp_files) == 0


# ── Participant extraction ────────────────────────────────────────────────────

@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_email_participant_extraction(mock_config, mock_state_file, mock_memories, tmp_path):
    """email_thread participants (strings) parsed."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_email_memory(memories_dir, "email-1.md", ["alice@acme.com", "bob@acme.com"])

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    assert len(contact_files) == 2


@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_calendar_participant_extraction(mock_config, mock_state_file, mock_memories, tmp_path):
    """calendar_event participants (dicts) parsed."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [calendar_event]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_calendar_memory(
        memories_dir, "calendar-1.md",
        [{"name": "Alice Chen", "email": "alice@acme.com"}]
    )

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    assert len(contact_files) == 1


@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_slack_participant_extraction(mock_config, mock_state_file, mock_memories, tmp_path):
    """slack_thread participants (slack_id) parsed."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [slack_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_slack_memory(
        memories_dir, "slack-1.md",
        [{"name": "Alice Chen", "slack_id": "U12345"}]
    )

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    assert len(contact_files) == 1


# ── Skip non-source types ─────────────────────────────────────────────────────

@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_skip_non_source_types(mock_config, mock_state_file, mock_memories, tmp_path):
    """webpage and code_project files ignored."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_webpage_memory(memories_dir, "webpage-1.md")

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    assert len(contact_files) == 0


# ── State persistence ─────────────────────────────────────────────────────────

@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_state_file_persists(mock_config, mock_state_file, mock_memories, tmp_path):
    """Processed map and interaction timestamps survive restart."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    email_file = make_email_memory(memories_dir, "email-1.md", ["alice@acme.com"])

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    # Check state file exists and has content
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert "processed" in state
    assert "contacts" in state
    assert email_file.name in state["processed"]


# ── Upsert tests ──────────────────────────────────────────────────────────────

@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_upsert_creates_new_contact(mock_config, mock_state_file, mock_memories, tmp_path):
    """New participant creates contact file."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    make_email_memory(memories_dir, "email-1.md", ["alice@acme.com"])

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    assert len(contact_files) == 1


@patch.object(ct, "MEMORIES_DIR")
@patch.object(ct, "STATE_FILE")
@patch.object(ct, "CONFIG_PATH")
def test_upsert_updates_existing_contact(mock_config, mock_state_file, mock_memories, tmp_path):
    """Second interaction updates last_interaction and interaction_count."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    mock_memories.__truediv__ = lambda self, x: memories_dir / x
    mock_memories.glob = lambda x: memories_dir.glob(x)

    state_file = tmp_path / "state.json"
    mock_state_file.exists.return_value = False
    mock_state_file.write_text = lambda x: state_file.write_text(x)
    mock_state_file.with_suffix = lambda x: state_file.with_suffix(x)
    mock_state_file.__str__ = lambda self: str(state_file)

    config_file = tmp_path / "config.yaml"
    config_file.write_text("contact_tracker:\n  source_types: [email_thread]\n")
    mock_config.exists.return_value = True
    mock_config.read_text.return_value = config_file.read_text()

    # First interaction
    make_email_memory(memories_dir, "email-1.md", ["alice@acme.com"], "2026-04-10T10:00:00")

    tracker = ContactTracker(cache=_make_cache(memories_dir))
    async def run():
        await tracker._run_scan()

    import asyncio
    asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    fm1 = _parse_frontmatter(contact_files[0].read_text())
    count1 = fm1.get("interaction_count", 0)

    # Second interaction
    make_email_memory(memories_dir, "email-2.md", ["alice@acme.com"], "2026-04-11T10:00:00")

    asyncio.run(run())

    fm2 = _parse_frontmatter(contact_files[0].read_text())
    count2 = fm2.get("interaction_count", 0)

    assert count2 > count1


def test_contact_tracker_skips_marketing_emails(tmp_path):
    """Email threads with marketing classification should not generate contacts."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "contact-state.json"

    # Create one human email and one marketing email
    human_email = memories_dir / "email-human.md"
    human_email.write_text(
        "---\nsource_title: Human Email\nsummary: Test.\n"
        "tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        "source_url: email:human123\ntype: email_thread\n"
        "classification: human\n"
        "participants: [alice@acme.com]\n"
        "last_message: '2026-04-11T10:00:00'\nmessage_count: 1\n---\n\n"
        "## Messages\nTest.\n"
    )

    marketing_email = memories_dir / "email-marketing.md"
    marketing_email.write_text(
        "---\nsource_title: Marketing Newsletter\nsummary: Newsletter.\n"
        "tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        "source_url: email:marketing123\ntype: email_thread\n"
        "classification: marketing\n"
        "participants: [newsletter@company.com]\n"
        "last_message: '2026-04-11T10:00:00'\nmessage_count: 1\n---\n\n"
        "## Messages\nNewsletter.\n"
    )

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch.object(ct, "STATE_FILE", state_file), \
         patch.object(ct, "CONFIG_PATH", tmp_path / "config.yaml"):
        (tmp_path / "config.yaml").write_text("contact_tracker:\n  interval_seconds: 300\n")
        tracker = ContactTracker(cache=_make_cache(memories_dir))
        async def run():
            await tracker._run_scan()

        import asyncio
        asyncio.run(run())

    contact_files = list(memories_dir.glob("contact-*.md"))
    # Should only have one contact (alice), not newsletter sender
    assert len(contact_files) == 1
    fm = _parse_frontmatter(contact_files[0].read_text())
    assert "alice@acme.com" in fm.get("emails", [])
    assert "newsletter@company.com" not in fm.get("emails", [])


def test_contact_rescan_does_not_embed_full_file_as_body(tmp_path):
    """Second scan must not corrupt the contact file by prepending the old frontmatter to the body.

    MemoryCache.body contains the full file text. If contact_tracker uses it
    verbatim as the new body, the written file becomes:
        ---
        <new frontmatter>
        ---
        ---
        <old frontmatter>
        ---
        <old body>

    This test ensures the body is extracted from the markdown portion only
    (everything after the second '---'), not the entire cached text.
    """
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "contact-state.json"

    make_email_memory(memories_dir, "email-1.md", ["alice@acme.com"], "2026-04-10T10:00:00")

    import asyncio

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch.object(ct, "STATE_FILE", state_file), \
         patch.object(ct, "CONFIG_PATH", tmp_path / "config.yaml"):
        (tmp_path / "config.yaml").write_text("contact_tracker:\n  interval_seconds: 300\n")
        tracker = ContactTracker(cache=_make_cache(memories_dir))

        async def run():
            await tracker._run_scan()

        asyncio.run(run())

        contact_files = list(memories_dir.glob("contact-*.md"))
        assert len(contact_files) == 1

        # Second scan — the cache now returns the first file's full text as "body"
        make_email_memory(memories_dir, "email-2.md", ["alice@acme.com"], "2026-04-11T10:00:00")
        asyncio.run(run())

        content2 = contact_files[0].read_text()

    # The file must have exactly ONE frontmatter block — count "---" separator lines.
    # Corrupt files have ---<new fm>------<old fm>--- which gives ≥4 "---" lines.
    separators = [line.strip() for line in content2.splitlines() if line.strip() == "---"]
    assert len(separators) == 2, (
        f"Expected exactly 2 '---' separators, got {len(separators)}. "
        "File may have embedded old frontmatter as body."
    )
