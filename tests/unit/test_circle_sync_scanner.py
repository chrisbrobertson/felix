"""Unit tests for circle_sync_scanner.py."""
import asyncio
import json
import logging
import os
import pytest
import yaml
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import circle_sync_scanner as css_module
from circle_sync_scanner import CircleSyncScanner


@pytest.fixture
def dirs(tmp_path):
    """Set up temporary directories for testing."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    circles_dir = tmp_path / "circles"
    circles_dir.mkdir()
    icloud_root = tmp_path / "icloud"
    icloud_root.mkdir()
    state_file = tmp_path / "circle-sync-state.json"
    config_path = tmp_path / "config.yaml"

    # Write a minimal config
    config_path.write_text(yaml.dump({
        "circles": {
            "enabled": True,
            "dir": str(circles_dir),
            "icloud_root": str(icloud_root),
            "scan_interval_seconds": 300,
        }
    }))

    return {
        "memories": memories_dir,
        "circles": circles_dir,
        "icloud_root": icloud_root,
        "state_file": state_file,
        "config": config_path,
    }


@pytest.fixture
def scanner(dirs):
    """Create a scanner with patched module-level constants."""
    with patch.multiple(
        css_module,
        MEMORIES_DIR=dirs["memories"],
        CONFIG_PATH=dirs["config"],
        STATE_FILE=dirs["state_file"],
        DEFAULT_ICLOUD_ROOT=dirs["icloud_root"],
        DEFAULT_CIRCLES_DIR=dirs["circles"],
    ):
        s = CircleSyncScanner(role="full")
        s._load_config()
        yield s


def _write_memory(memories_dir, filename, frontmatter):
    """Helper to write a memory file with frontmatter."""
    content = "---\n" + yaml.dump(frontmatter) + "---\n\nBody text here."
    (memories_dir / filename).write_text(content)


def _write_ruleset(circles_dir, slug, include_rules, exclude_rules=None):
    """Helper to write a circle ruleset file."""
    data = {
        "circle": slug,
        "display_name": slug.title(),
        "icloud_folder": slug + "/memories",
        "rules": {
            "include": include_rules,
            "exclude": exclude_rules or [],
        },
    }
    (circles_dir / f"{slug}.yaml").write_text(yaml.dump(data))


@pytest.mark.asyncio
async def test_sync_adds_matching_file(dirs, scanner):
    """Test that matching files are synced to the iCloud folder."""
    # Create a memory file that matches the rule
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["family"],
    })

    # Create ruleset
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ])

    # Create iCloud folder
    icloud_folder = dirs["icloud_root"] / "family" / "memories"
    icloud_folder.mkdir(parents=True)

    # Run cycle
    await scanner._run_cycle()

    # Check file was synced
    assert (icloud_folder / "test-memory.md").exists()
    assert scanner._state["family"]["synced_files"]["test-memory.md"] > 0


@pytest.mark.asyncio
async def test_sync_excludes_blocked_file(dirs, scanner):
    """Test that files matching exclude rules are not synced."""
    # Create a memory file that matches both include and exclude
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["family", "work"],
    })

    # Create ruleset with exclude rule
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ], exclude_rules=[
        {"tags_contains_any": ["work"]}
    ])

    # Create iCloud folder
    icloud_folder = dirs["icloud_root"] / "family" / "memories"
    icloud_folder.mkdir(parents=True)

    # Run cycle
    await scanner._run_cycle()

    # Check file was NOT synced
    assert not (icloud_folder / "test-memory.md").exists()


@pytest.mark.asyncio
async def test_sync_removes_stale_file(dirs, scanner):
    """Test that files no longer matching rules are removed."""
    # Create a memory file
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["work"],  # different tags
    })

    # Create ruleset
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ])

    # Create iCloud folder with pre-existing file
    icloud_folder = dirs["icloud_root"] / "family" / "memories"
    icloud_folder.mkdir(parents=True)
    (icloud_folder / "test-memory.md").write_text("old content")

    # Pre-populate state to indicate file was previously synced
    scanner._state = {
        "family": {
            "synced_files": {"test-memory.md": 1234567890.0},
            "last_run": None,
        }
    }

    # Run cycle
    await scanner._run_cycle()

    # Check file was removed
    assert not (icloud_folder / "test-memory.md").exists()
    assert "test-memory.md" not in scanner._state["family"]["synced_files"]


@pytest.mark.asyncio
async def test_sync_updates_changed_file(dirs, scanner):
    """Test that files with newer mtime are re-synced."""
    # Create a memory file
    mem_path = dirs["memories"] / "test-memory.md"
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["family"],
    })
    new_mtime = mem_path.stat().st_mtime

    # Create ruleset
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ])

    # Create iCloud folder with old file
    icloud_folder = dirs["icloud_root"] / "family" / "memories"
    icloud_folder.mkdir(parents=True)
    (icloud_folder / "test-memory.md").write_text("old content")

    # Pre-populate state with old mtime
    old_mtime = new_mtime - 100
    scanner._state = {
        "family": {
            "synced_files": {"test-memory.md": old_mtime},
            "last_run": None,
        }
    }

    # Run cycle
    await scanner._run_cycle()

    # Check file was updated
    assert (icloud_folder / "test-memory.md").exists()
    assert scanner._state["family"]["synced_files"]["test-memory.md"] == new_mtime


@pytest.mark.asyncio
async def test_sync_skips_missing_icloud_folder(dirs, scanner, caplog):
    """Test that missing iCloud folder logs warning and skips sync."""
    # Create a memory file
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["family"],
    })

    # Create ruleset
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ])

    # Do NOT create the iCloud folder

    # Run cycle
    with caplog.at_level(logging.WARNING):
        await scanner._run_cycle()

    # Check warning was logged
    assert "iCloud folder not found" in caplog.text
    assert "family" not in scanner._state or not scanner._state["family"]["synced_files"]


@pytest.mark.asyncio
async def test_sync_skips_malformed_ruleset(dirs, scanner, caplog):
    """Test that malformed ruleset is skipped without crashing."""
    # Create a memory file
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["family"],
    })

    # Create malformed ruleset (missing 'circle' field)
    bad_ruleset = dirs["circles"] / "bad.yaml"
    bad_ruleset.write_text(yaml.dump({"display_name": "Bad"}))

    # Run cycle
    with caplog.at_level(logging.ERROR):
        await scanner._run_cycle()

    # Check error was logged
    assert "malformed circle ruleset" in caplog.text


@pytest.mark.asyncio
async def test_state_file_written(dirs, scanner):
    """Test that state file is written after cycle."""
    # Create a memory file
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["family"],
    })

    # Create ruleset
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ])

    # Create iCloud folder
    icloud_folder = dirs["icloud_root"] / "family" / "memories"
    icloud_folder.mkdir(parents=True)

    # Run cycle
    await scanner._run_cycle()

    # Check state file was written
    assert dirs["state_file"].exists()
    state = json.loads(dirs["state_file"].read_text())
    assert "family" in state
    assert "test-memory.md" in state["family"]["synced_files"]


@pytest.mark.asyncio
async def test_missing_state_file_treated_as_empty(dirs, scanner):
    """Test that missing state file is treated as empty state."""
    # State file does not exist
    assert not dirs["state_file"].exists()

    # Load state
    scanner._load_state()

    # Check state is empty
    assert scanner._state == {}


@pytest.mark.asyncio
async def test_circles_disabled(dirs):
    """Test that loop exits immediately when circles are disabled."""
    # Create config with circles disabled
    config_path = dirs["config"]
    config_path.write_text(yaml.dump({
        "circles": {
            "enabled": False,
        }
    }))

    with patch.multiple(
        css_module,
        MEMORIES_DIR=dirs["memories"],
        CONFIG_PATH=config_path,
        STATE_FILE=dirs["state_file"],
        DEFAULT_ICLOUD_ROOT=dirs["icloud_root"],
        DEFAULT_CIRCLES_DIR=dirs["circles"],
    ):
        scanner = CircleSyncScanner(role="full")
        stop_event = asyncio.Event()
        stop_event.set()  # Set immediately

        # Run loop should exit without doing anything
        await scanner.run_loop(stop_event)

        # No state file should be created
        assert not dirs["state_file"].exists()


@pytest.mark.asyncio
async def test_atomic_write(dirs, scanner):
    """Test that atomic write pattern is used (no .tmp remains)."""
    # Create a memory file
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["family"],
    })

    # Create ruleset
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ])

    # Create iCloud folder
    icloud_folder = dirs["icloud_root"] / "family" / "memories"
    icloud_folder.mkdir(parents=True)

    # Run cycle
    await scanner._run_cycle()

    # Check no .tmp files remain
    tmp_files = list(icloud_folder.glob("*.tmp"))
    assert len(tmp_files) == 0


@pytest.mark.asyncio
async def test_deletion_missing_file_is_noop(dirs, scanner):
    """Test that deleting a file that's already gone is a no-op."""
    # Create ruleset
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ])

    # Create iCloud folder (file does NOT exist in folder)
    icloud_folder = dirs["icloud_root"] / "family" / "memories"
    icloud_folder.mkdir(parents=True)

    # Pre-populate state with a file that doesn't exist
    scanner._state = {
        "family": {
            "synced_files": {"test-memory.md": 1234567890.0},
            "last_run": None,
        }
    }

    # Run cycle (no matching memory file exists, so deletion is attempted)
    await scanner._run_cycle()

    # Check state was cleaned up (no error)
    assert "test-memory.md" not in scanner._state["family"]["synced_files"]


@pytest.mark.asyncio
async def test_ruleset_change_detected_each_cycle(dirs, scanner):
    """Test that ruleset changes are detected on each cycle."""
    # Create a memory file
    _write_memory(dirs["memories"], "test-memory.md", {
        "type": "calendar_event",
        "tags": ["family"],
    })

    # Create narrow ruleset (no files match)
    _write_ruleset(dirs["circles"], "family", [
        {"type": "goal"}  # memory has type calendar_event, won't match
    ])

    # Create iCloud folder
    icloud_folder = dirs["icloud_root"] / "family" / "memories"
    icloud_folder.mkdir(parents=True)

    # Run cycle 1
    await scanner._run_cycle()
    assert not (icloud_folder / "test-memory.md").exists()

    # Update ruleset on disk to include the memory
    _write_ruleset(dirs["circles"], "family", [
        {"type": "calendar_event", "tags_contains_any": ["family"]}
    ])

    # Run cycle 2
    await scanner._run_cycle()

    # Check file is now synced
    assert (icloud_folder / "test-memory.md").exists()
