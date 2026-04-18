"""
Unit tests for watchlist_checker.py
"""
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import yaml

from watchlist_checker import check_watchlists


@pytest.fixture
def memories_dir(tmp_path):
    """Create a temporary memories directory."""
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    return mem_dir


def write_watchlist(memories_dir: Path, slug: str, frontmatter: dict, body: str = ""):
    """Helper to write a watchlist file."""
    filename = f"watchlist-{slug}.md"
    path = memories_dir / filename
    fm_yaml = yaml.dump(frontmatter, default_flow_style=None, allow_unicode=True, sort_keys=False)
    content = f"---\n{fm_yaml}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    return path


def write_memory(memories_dir: Path, filename: str, frontmatter: dict, body: str = ""):
    """Helper to write a memory file."""
    path = memories_dir / filename
    fm_yaml = yaml.dump(frontmatter, default_flow_style=None, allow_unicode=True, sort_keys=False)
    content = f"---\n{fm_yaml}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    return path


def read_frontmatter(path: Path) -> dict:
    """Helper to read frontmatter from a file."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    return yaml.safe_load(parts[1]) or {}


@pytest.mark.asyncio
async def test_active_watchlist_matches_topic(memories_dir):
    """Watchlist with matching topic triggers."""
    # Create active watchlist
    wl_fm = {
        "type": "watchlist",
        "id": "abc123",
        "title": "Watch for: API redesign",
        "status": "active",
        "topic": "API redesign",
        "watch_type": "any",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    wl_path = write_watchlist(memories_dir, "api-redesign-abc123", wl_fm, "Watching for API redesign")

    # Create matching memory
    mem_fm = {
        "source_title": "Email about API redesign discussion",
        "type": "email_thread",
        "participants": [],
    }
    mem_path = write_memory(
        memories_dir,
        "email-thread-api-redesign-xyz.md",
        mem_fm,
        "We need to discuss the API redesign next week."
    )

    # Check watchlists
    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    assert count == 1
    notify_fn.assert_called_once()

    # Verify watchlist status updated
    updated_fm = read_frontmatter(wl_path)
    assert updated_fm["status"] == "triggered"
    assert "triggered_at" in updated_fm


@pytest.mark.asyncio
async def test_expired_watchlist_not_triggered(memories_dir):
    """Watchlist with past expires date is marked expired and not triggered."""
    # Create watchlist with expiry in the past
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    wl_fm = {
        "type": "watchlist",
        "id": "def456",
        "title": "Watch for: budget",
        "status": "active",
        "topic": "budget",
        "watch_type": "any",
        "created": datetime.now(timezone.utc).isoformat(),
        "expires": past,
    }
    wl_path = write_watchlist(memories_dir, "budget-def456", wl_fm, "Watching for budget")

    # Create matching memory
    mem_fm = {
        "source_title": "Budget discussion",
        "type": "email_thread",
    }
    mem_path = write_memory(
        memories_dir,
        "email-thread-budget-xyz.md",
        mem_fm,
        "Budget was approved"
    )

    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    assert count == 0
    notify_fn.assert_not_called()

    # Verify watchlist marked expired
    updated_fm = read_frontmatter(wl_path)
    assert updated_fm["status"] == "expired"


@pytest.mark.asyncio
async def test_person_filter_blocks_nonmatch(memories_dir):
    """Watchlist with person filter not triggered when person absent."""
    wl_fm = {
        "type": "watchlist",
        "id": "ghi789",
        "title": "Watch for: deployment",
        "status": "active",
        "topic": "deployment",
        "person": "Sarah",
        "watch_type": "any",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    write_watchlist(memories_dir, "deployment-ghi789", wl_fm, "Watching for deployment from Sarah")

    # Create memory without Sarah
    mem_fm = {
        "source_title": "Deployment update",
        "type": "email_thread",
        "participants": [{"name": "John Doe", "email": "john@example.com"}],
    }
    mem_path = write_memory(
        memories_dir,
        "email-thread-deployment-xyz.md",
        mem_fm,
        "Deployment is scheduled for Monday"
    )

    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    assert count == 0
    notify_fn.assert_not_called()


@pytest.mark.asyncio
async def test_person_filter_matches(memories_dir):
    """Watchlist triggers when person appears in participants."""
    wl_fm = {
        "type": "watchlist",
        "id": "jkl012",
        "title": "Watch for: contract",
        "status": "active",
        "topic": "contract",
        "person": "Sarah",
        "watch_type": "any",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    wl_path = write_watchlist(memories_dir, "contract-jkl012", wl_fm, "Watching for contract from Sarah")

    # Create memory with Sarah in participants
    mem_fm = {
        "source_title": "Contract review",
        "type": "email_thread",
        "participants": [
            {"name": "Sarah Johnson", "email": "sarah@example.com"},
            {"name": "John Doe", "email": "john@example.com"}
        ],
    }
    mem_path = write_memory(
        memories_dir,
        "email-thread-contract-xyz.md",
        mem_fm,
        "The contract has been reviewed"
    )

    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    assert count == 1
    notify_fn.assert_called_once()

    updated_fm = read_frontmatter(wl_path)
    assert updated_fm["status"] == "triggered"


@pytest.mark.asyncio
async def test_type_filter_blocks_nonmatch(memories_dir):
    """Email watchlist not triggered by slack memory."""
    wl_fm = {
        "type": "watchlist",
        "id": "mno345",
        "title": "Watch for: release",
        "status": "active",
        "topic": "release",
        "watch_type": "email",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    write_watchlist(memories_dir, "release-mno345", wl_fm, "Watching for release in email")

    # Create slack memory with matching topic
    mem_fm = {
        "source_title": "Release announcement",
        "type": "slack_thread",
    }
    mem_path = write_memory(
        memories_dir,
        "slack-thread-release-xyz.md",
        mem_fm,
        "Release is scheduled for Friday"
    )

    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    assert count == 0
    notify_fn.assert_not_called()


@pytest.mark.asyncio
async def test_triggered_watchlist_status_updated(memories_dir):
    """Verify watchlist file status changes to triggered."""
    wl_fm = {
        "type": "watchlist",
        "id": "pqr678",
        "title": "Watch for: migration",
        "status": "active",
        "topic": "migration",
        "watch_type": "any",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    wl_path = write_watchlist(memories_dir, "migration-pqr678", wl_fm, "Watching for migration")

    # Create matching memory
    mem_fm = {
        "source_title": "Database migration update",
        "type": "email_thread",
    }
    mem_path = write_memory(
        memories_dir,
        "email-thread-migration-xyz.md",
        mem_fm,
        "Migration completed successfully"
    )

    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    assert count == 1

    # Read watchlist file and verify status
    updated_fm = read_frontmatter(wl_path)
    assert updated_fm["status"] == "triggered"
    assert "triggered_at" in updated_fm

    # Verify triggered_at is recent
    triggered_at = datetime.fromisoformat(updated_fm["triggered_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert (now - triggered_at).total_seconds() < 10


@pytest.mark.asyncio
async def test_expired_datetime_marks_expired(memories_dir):
    """Watchlist with expires datetime in the past is marked expired."""
    # Expires 2 hours ago
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    wl_fm = {
        "type": "watchlist",
        "id": "stu901",
        "title": "Watch for: announcement",
        "status": "active",
        "topic": "announcement",
        "watch_type": "any",
        "created": datetime.now(timezone.utc).isoformat(),
        "expires": past,
    }
    wl_path = write_watchlist(memories_dir, "announcement-stu901", wl_fm, "Watching for announcement")

    # Create matching memory
    mem_fm = {
        "source_title": "New announcement",
        "type": "email_thread",
    }
    mem_path = write_memory(
        memories_dir,
        "email-thread-announcement-xyz.md",
        mem_fm,
        "Important announcement coming soon"
    )

    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    # Should not trigger
    assert count == 0
    notify_fn.assert_not_called()

    # Should be marked expired
    updated_fm = read_frontmatter(wl_path)
    assert updated_fm["status"] == "expired"


@pytest.mark.asyncio
async def test_multiple_keywords_all_must_match(memories_dir):
    """Topic with multiple keywords requires all to be present."""
    wl_fm = {
        "type": "watchlist",
        "id": "vwx234",
        "title": "Watch for: API redesign approval",
        "status": "active",
        "topic": "API redesign approval",
        "watch_type": "any",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    write_watchlist(memories_dir, "api-redesign-approval-vwx234", wl_fm, "Watching for API redesign approval")

    # Memory with only some keywords
    mem_fm = {
        "source_title": "API redesign discussion",
        "type": "email_thread",
    }
    mem_path = write_memory(
        memories_dir,
        "email-thread-api-redesign-xyz.md",
        mem_fm,
        "We discussed the API redesign"  # Missing "approval"
    )

    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    # Should not trigger — "approval" keyword missing
    assert count == 0
    notify_fn.assert_not_called()


@pytest.mark.asyncio
async def test_person_match_in_body(memories_dir):
    """Person filter matches when person appears in body text."""
    wl_fm = {
        "type": "watchlist",
        "id": "yza567",
        "title": "Watch for: security audit",
        "status": "active",
        "topic": "security audit",
        "person": "Alice",
        "watch_type": "any",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    wl_path = write_watchlist(memories_dir, "security-audit-yza567", wl_fm, "Watching for security audit from Alice")

    # Memory without Alice in participants but in body
    mem_fm = {
        "source_title": "Security audit report",
        "type": "email_thread",
        "participants": [],
    }
    mem_path = write_memory(
        memories_dir,
        "email-thread-security-audit-xyz.md",
        mem_fm,
        "Alice sent the security audit report yesterday"
    )

    notify_fn = AsyncMock()
    count = check_watchlists(mem_path, memories_dir, notify_fn)

    assert count == 1
    notify_fn.assert_called_once()

    updated_fm = read_frontmatter(wl_path)
    assert updated_fm["status"] == "triggered"
