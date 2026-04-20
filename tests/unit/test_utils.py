"""Unit tests for utils.py."""
from pathlib import Path
import pytest

from utils import is_conflict_copy, glob_memories


def test_is_conflict_copy_matches_various_formats():
    """Verify conflict copy detection matches all common iCloud formats."""
    assert is_conflict_copy(Path("foo (conflicted copy).md"))
    assert is_conflict_copy(Path("foo (Mac's conflicted copy).md"))
    assert is_conflict_copy(Path("foo (Chris's MacBook Pro's conflicted copy 3).md"))
    assert is_conflict_copy(Path("bar (CONFLICTED COPY).md"))  # case insensitive


def test_is_conflict_copy_false_for_normal_file():
    """Normal files should not be flagged as conflict copies."""
    assert not is_conflict_copy(Path("foo.md"))
    assert not is_conflict_copy(Path("2026-04-19-foo-bar.md"))
    assert not is_conflict_copy(Path("commitment-foo-abc123.md"))


def test_glob_memories_filters_conflict_copies(tmp_path):
    """glob_memories should exclude conflict copies."""
    # Create mix of normal and conflict files
    (tmp_path / "normal.md").write_text("normal")
    (tmp_path / "another.md").write_text("another")
    (tmp_path / "conflict (conflicted copy).md").write_text("conflict")
    (tmp_path / "conflict2 (Mac's conflicted copy).md").write_text("conflict2")

    results = list(glob_memories(tmp_path, "*.md"))

    # Should have only the 2 normal files
    assert len(results) == 2
    names = {p.name for p in results}
    assert names == {"normal.md", "another.md"}
