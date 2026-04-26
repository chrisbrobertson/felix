"""Unit tests for memory_cache.py"""

import asyncio
import errno
import json
import pytest
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml


@pytest.fixture
def memories_dir(tmp_path):
    """Create a temporary memories directory."""
    d = tmp_path / "memories"
    d.mkdir()
    return d


@pytest.fixture
def cache_db(tmp_path):
    """Create a temporary cache database path."""
    return tmp_path / "memory-cache.sqlite"


def _write_memory(memories_dir: Path, filename: str, frontmatter: dict, body: str = ""):
    """Helper to write a memory file."""
    content = f"---\n{yaml.dump(frontmatter, sort_keys=False)}---\n\n{body}\n"
    path = memories_dir / filename
    path.write_text(content)
    return path


@pytest.mark.asyncio
async def test_schema_creation(cache_db, memories_dir):
    """Schema creation and idempotent re-open."""
    from memory_cache import MemoryCache

    cache = MemoryCache(cache_db, memories_dir)
    assert cache_db.exists()

    # Verify schema
    conn = sqlite3.connect(str(cache_db))
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    assert ("memories",) in tables

    # Verify indexes
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    index_names = [row[0] for row in indexes]
    assert "idx_memories_type" in index_names
    assert "idx_memories_prefix" in index_names

    # Verify WAL mode
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"

    conn.close()

    # Re-open should be idempotent
    cache2 = MemoryCache(cache_db, memories_dir)
    cache2.close()
    cache.close()


@pytest.mark.asyncio
async def test_pass_through_mode(memories_dir):
    """Pass-through mode: db_path=None → no SQLite file created."""
    from memory_cache import MemoryCache

    # Create a test file
    _write_memory(
        memories_dir,
        "2026-04-25-test-abc123.md",
        {"type": "test", "status": "active"},
        "Test body content"
    )

    # Init with db_path=None
    cache = MemoryCache(None, memories_dir, enabled=True)

    # Should be able to get the file
    entry = await cache.get("2026-04-25-test-abc123.md")
    assert entry is not None
    assert entry["body"] == "---\ntype: test\nstatus: active\n---\n\nTest body content\n"
    assert entry["type"] == "test"

    # No SQLite file should exist
    assert not (memories_dir.parent / "memory-cache.sqlite").exists()


@pytest.mark.asyncio
async def test_pass_through_mode_enabled_false(cache_db, memories_dir):
    """Pass-through mode: enabled=False → no SQLite operations."""
    from memory_cache import MemoryCache

    _write_memory(
        memories_dir,
        "2026-04-25-test-abc123.md",
        {"type": "test"},
        "Test body"
    )

    cache = MemoryCache(cache_db, memories_dir, enabled=False)

    # Should be able to get the file via pass-through
    entry = await cache.get("2026-04-25-test-abc123.md")
    assert entry is not None
    assert "Test body" in entry["body"]

    # SQLite file may have been created but is never used
    cache.close()


@pytest.mark.asyncio
async def test_lazy_population(cache_db, memories_dir):
    """get() on a missing file reads from disk and populates cache."""
    from memory_cache import MemoryCache

    _write_memory(
        memories_dir,
        "2026-04-25-test-abc123.md",
        {"type": "test", "status": "active"},
        "Test body"
    )

    cache = MemoryCache(cache_db, memories_dir)

    # Cache starts empty
    conn = sqlite3.connect(str(cache_db))
    count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == 0
    conn.close()

    # First get() → cache miss → populates
    entry = await cache.get("2026-04-25-test-abc123.md")
    assert entry is not None
    assert "Test body" in entry["body"]
    assert entry["type"] == "test"
    assert entry["status"] == "active"

    # Now in cache
    conn = sqlite3.connect(str(cache_db))
    count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == 1
    conn.close()

    # Second get() → cache hit (no disk read)
    entry2 = await cache.get("2026-04-25-test-abc123.md")
    assert entry2["body"] == entry["body"]

    cache.close()


@pytest.mark.asyncio
async def test_query_by_type(cache_db, memories_dir):
    """query_by_type correctness."""
    from memory_cache import MemoryCache

    _write_memory(memories_dir, "commitment-1-abc123.md", {"type": "commitment", "status": "active"}, "C1")
    _write_memory(memories_dir, "commitment-2-def456.md", {"type": "commitment", "status": "completed"}, "C2")
    _write_memory(memories_dir, "goal-1-ghi789.md", {"type": "goal", "status": "active"}, "G1")

    cache = MemoryCache(cache_db, memories_dir)

    # Populate cache
    await cache.rebuild()

    # Query all commitments
    results = await cache.query_by_type("commitment")
    assert len(results) == 2
    filenames = [r["filename"] for r in results]
    assert "commitment-1-abc123.md" in filenames
    assert "commitment-2-def456.md" in filenames

    # Query active commitments only
    results = await cache.query_by_type("commitment", status="active")
    assert len(results) == 1
    assert results[0]["filename"] == "commitment-1-abc123.md"

    # Query goals
    results = await cache.query_by_type("goal")
    assert len(results) == 1
    assert results[0]["filename"] == "goal-1-ghi789.md"

    cache.close()


@pytest.mark.asyncio
async def test_query_by_prefix(cache_db, memories_dir):
    """query_by_prefix correctness."""
    from memory_cache import MemoryCache

    _write_memory(memories_dir, "calendar-event-macstudio-2026-04-25-test-abc123.md",
                 {"type": "calendar_event"}, "Event 1")
    _write_memory(memories_dir, "calendar-event-macbook-2026-04-25-test-def456.md",
                 {"type": "calendar_event"}, "Event 2")
    _write_memory(memories_dir, "email-thread-test-ghi789.md",
                 {"type": "email_thread"}, "Email 1")

    cache = MemoryCache(cache_db, memories_dir)
    await cache.rebuild()

    # Query calendar events
    results = await cache.query_by_prefix("calendar-event")
    assert len(results) == 2
    filenames = [r["filename"] for r in results]
    assert "calendar-event-macstudio-2026-04-25-test-abc123.md" in filenames
    assert "calendar-event-macbook-2026-04-25-test-def456.md" in filenames

    # Query email threads
    results = await cache.query_by_prefix("email-thread")
    assert len(results) == 1
    assert results[0]["filename"] == "email-thread-test-ghi789.md"

    cache.close()


@pytest.mark.asyncio
async def test_score_keywords(cache_db, memories_dir):
    """score_keywords uses same algorithm as _score_relevance."""
    from memory_cache import MemoryCache

    # Create files with varying keyword overlap
    _write_memory(
        memories_dir,
        "2026-04-25-litellm-routing-abc123.md",
        {"type": "test", "tags": ["litellm", "routing"]},
        "Summary: LiteLLM router supports fallback chains and load balancing."
    )
    _write_memory(
        memories_dir,
        "2026-04-25-anthropic-mcp-def456.md",
        {"type": "test", "tags": ["anthropic", "mcp"]},
        "Summary: Anthropic's Model Context Protocol specification."
    )
    _write_memory(
        memories_dir,
        "2026-04-25-unrelated-ghi789.md",
        {"type": "test"},
        "Summary: Something completely different."
    )

    cache = MemoryCache(cache_db, memories_dir)
    await cache.rebuild()

    # Query for "litellm routing"
    scored = await cache.score_keywords("litellm routing", top_n=10)

    # Should find the litellm file with score=2 (both tokens present)
    assert len(scored) >= 1
    assert scored[0][0] == "2026-04-25-litellm-routing-abc123.md"
    assert scored[0][1] == 2.0

    # Query for "anthropic"
    scored = await cache.score_keywords("anthropic", top_n=10)
    assert len(scored) >= 1
    assert scored[0][0] == "2026-04-25-anthropic-mcp-def456.md"
    assert scored[0][1] == 1.0

    # Query for "foo bar" (no matches)
    scored = await cache.score_keywords("foo bar baz", top_n=10)
    assert len(scored) == 0

    cache.close()


@pytest.mark.asyncio
async def test_invalidate(cache_db, memories_dir):
    """invalidate() re-reads and upserts."""
    from memory_cache import MemoryCache

    path = _write_memory(
        memories_dir,
        "2026-04-25-test-abc123.md",
        {"type": "test", "status": "active"},
        "Original body"
    )

    cache = MemoryCache(cache_db, memories_dir)

    # Invalidate (first time → insert)
    await cache.invalidate("2026-04-25-test-abc123.md")

    entry = await cache.get("2026-04-25-test-abc123.md")
    assert "Original body" in entry["body"]
    assert entry["status"] == "active"

    # Modify file
    time.sleep(0.01)  # Ensure mtime changes
    _write_memory(
        memories_dir,
        "2026-04-25-test-abc123.md",
        {"type": "test", "status": "completed"},
        "Updated body"
    )

    # Invalidate again (update)
    await cache.invalidate("2026-04-25-test-abc123.md")

    entry = await cache.get("2026-04-25-test-abc123.md")
    assert "Updated body" in entry["body"]
    assert entry["status"] == "completed"

    cache.close()


@pytest.mark.asyncio
async def test_invalidate_missing_file(cache_db, memories_dir):
    """invalidate() on a missing file deletes the row."""
    from memory_cache import MemoryCache

    _write_memory(
        memories_dir,
        "2026-04-25-test-abc123.md",
        {"type": "test"},
        "Test"
    )

    cache = MemoryCache(cache_db, memories_dir)
    await cache.invalidate("2026-04-25-test-abc123.md")

    # Verify it's in cache
    entry = await cache.get("2026-04-25-test-abc123.md")
    assert entry is not None

    # Delete file
    (memories_dir / "2026-04-25-test-abc123.md").unlink()

    # Invalidate → should remove row
    await cache.invalidate("2026-04-25-test-abc123.md")

    entry = await cache.get("2026-04-25-test-abc123.md")
    assert entry is None

    cache.close()


@pytest.mark.asyncio
async def test_sweep(cache_db, memories_dir):
    """sweep() detects added, updated, and removed files."""
    from memory_cache import MemoryCache

    # Initial file
    _write_memory(
        memories_dir,
        "2026-04-25-old-abc123.md",
        {"type": "test"},
        "Old file"
    )

    cache = MemoryCache(cache_db, memories_dir)
    await cache.rebuild()

    # Add a new file
    _write_memory(
        memories_dir,
        "2026-04-25-new-def456.md",
        {"type": "test"},
        "New file"
    )

    # Modify existing file
    time.sleep(0.01)
    _write_memory(
        memories_dir,
        "2026-04-25-old-abc123.md",
        {"type": "test"},
        "Modified old file"
    )

    # Delete a file (add one then delete it to test removal)
    _write_memory(
        memories_dir,
        "2026-04-25-deleted-ghi789.md",
        {"type": "test"},
        "To be deleted"
    )
    await cache.invalidate("2026-04-25-deleted-ghi789.md")
    (memories_dir / "2026-04-25-deleted-ghi789.md").unlink()

    # Sweep
    added, updated, removed = await cache.sweep()

    # Should detect 1 added, 1 updated, 1 removed
    assert added == 1
    assert updated == 1
    assert removed == 1

    # Verify new file is in cache
    entry = await cache.get("2026-04-25-new-def456.md")
    assert entry is not None

    # Verify modified file is updated
    entry = await cache.get("2026-04-25-old-abc123.md")
    assert "Modified old file" in entry["body"]

    # Verify deleted file is gone
    entry = await cache.get("2026-04-25-deleted-ghi789.md")
    assert entry is None

    cache.close()


@pytest.mark.asyncio
async def test_rebuild(cache_db, memories_dir):
    """rebuild() wipes and repopulates."""
    from memory_cache import MemoryCache

    # Create some files
    _write_memory(memories_dir, "file1.md", {"type": "test"}, "Body 1")
    _write_memory(memories_dir, "file2.md", {"type": "test"}, "Body 2")
    _write_memory(memories_dir, "file3.md", {"type": "test"}, "Body 3")

    cache = MemoryCache(cache_db, memories_dir)

    # Rebuild
    count = await cache.rebuild()
    assert count == 3

    # Verify all are in cache
    assert await cache.get("file1.md") is not None
    assert await cache.get("file2.md") is not None
    assert await cache.get("file3.md") is not None

    # Add stale data
    _write_memory(memories_dir, "stale.md", {"type": "test"}, "Stale")
    await cache.invalidate("stale.md")
    (memories_dir / "stale.md").unlink()

    # Rebuild should clear stale
    count = await cache.rebuild()
    assert count == 3
    assert await cache.get("stale.md") is None

    cache.close()


@pytest.mark.asyncio
async def test_corrupt_db_recovery(tmp_path, memories_dir):
    """Corrupt DB at open → unlink + recreate."""
    from memory_cache import MemoryCache

    db_path = tmp_path / "corrupt.db"

    # Write garbage to the DB file
    db_path.write_bytes(b"This is not a SQLite database\x00\x00\x00")

    # Should recover gracefully
    cache = MemoryCache(db_path, memories_dir)

    # Should have a working cache
    _write_memory(memories_dir, "test.md", {"type": "test"}, "Test")
    await cache.invalidate("test.md")

    entry = await cache.get("test.md")
    assert entry is not None

    cache.close()


@pytest.mark.asyncio
async def test_edeadlk_handling(cache_db, memories_dir):
    """EDEADLK simulation: retry helper handles it gracefully."""
    from memory_cache import MemoryCache

    _write_memory(
        memories_dir,
        "2026-04-25-test-abc123.md",
        {"type": "test"},
        "Test body"
    )

    cache = MemoryCache(cache_db, memories_dir)

    # Patch read_text_with_retry_async to raise EDEADLK once, then succeed
    call_count = 0

    async def mock_read(path, default=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: simulate EDEADLK → should return default
            return default
        # Second call: succeed
        return path.read_text()

    with patch("memory_cache.read_text_with_retry_async", side_effect=mock_read):
        # First invalidate → EDEADLK → skips
        await cache.invalidate("2026-04-25-test-abc123.md")

        # Cache should be empty (file wasn't added)
        conn = sqlite3.connect(str(cache_db))
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()
        # Since default=None, invalidate sees None and tries to DELETE (no-op if not present)
        assert count == 0

    # Reset call count and try again (second call succeeds)
    call_count = 0
    with patch("memory_cache.read_text_with_retry_async", side_effect=mock_read):
        await cache.invalidate("2026-04-25-test-abc123.md")

    # Now should be populated (second attempt)
    entry = await cache.get("2026-04-25-test-abc123.md")
    assert entry is not None

    cache.close()


@pytest.mark.asyncio
async def test_conflict_copy_filtering(cache_db, memories_dir):
    """Conflict copies are filtered out during sweep."""
    from memory_cache import MemoryCache

    # Create normal file
    _write_memory(
        memories_dir,
        "2026-04-25-test-abc123.md",
        {"type": "test"},
        "Normal file"
    )

    # Create iCloud conflict copy
    _write_memory(
        memories_dir,
        "2026-04-25-test (Chris's MacBook Pro's conflicted copy).md",
        {"type": "test"},
        "Conflict copy"
    )

    cache = MemoryCache(cache_db, memories_dir)
    await cache.rebuild()

    # Only the normal file should be in cache
    conn = sqlite3.connect(str(cache_db))
    count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    filenames = [row[0] for row in conn.execute("SELECT filename FROM memories")]
    conn.close()

    assert count == 1
    assert "2026-04-25-test-abc123.md" in filenames
    assert "2026-04-25-test (Chris's MacBook Pro's conflicted copy).md" not in filenames

    cache.close()
