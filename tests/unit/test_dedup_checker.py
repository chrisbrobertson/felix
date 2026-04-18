"""Unit tests for dedup_checker.py."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import dedup_checker as dc


def write_memory(memories_dir: Path, filename: str, mem_type: str,
                 source_url: str = "", source_title: str = "",
                 body: str = "test content") -> Path:
    """Helper to write a memory file with frontmatter."""
    path = memories_dir / filename
    url_line = f"source_url: {source_url}\n" if source_url else ""
    title_line = f"source_title: {source_title}\n" if source_title else ""
    path.write_text(
        f"---\ntype: {mem_type}\n{url_line}{title_line}---\n\n{body}"
    )
    return path


def test_url_auto_merge_same_url(tmp_path):
    """Two files with same normalized URL and type should be auto-merged."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    # Two files with same URL, different tracking params
    write_memory(memories_dir, "file1.md", "reading",
                 source_url="https://example.com/article?utm_source=twitter",
                 source_title="Test Article",
                 body="short content")
    write_memory(memories_dir, "file2.md", "reading",
                 source_url="https://example.com/article?utm_medium=email",
                 source_title="Test Article",
                 body="longer content here with more text")

    result = dc.run(memories_dir, deploy_dir)

    assert result["auto_merged"] == 1
    # Only one file should remain
    remaining = list(memories_dir.glob("*.md"))
    assert len(remaining) == 1


def test_url_auto_merge_keeps_richer(tmp_path):
    """Auto-merge should keep the file with longer body."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    write_memory(memories_dir, "file1.md", "reading",
                 source_url="https://example.com/page",
                 source_title="Page",
                 body="short")
    write_memory(memories_dir, "file2.md", "reading",
                 source_url="https://example.com/page",
                 source_title="Page",
                 body="much longer content that should be kept")

    dc.run(memories_dir, deploy_dir)

    remaining = list(memories_dir.glob("*.md"))
    assert len(remaining) == 1
    # file2 should be kept (longer)
    kept_content = remaining[0].read_text()
    assert "much longer content" in kept_content


def test_url_normalization_strips_utm(tmp_path):
    """URL normalization should strip utm_* tracking parameters."""
    url1 = "https://example.com/page?utm_source=twitter&utm_campaign=test"
    url2 = "https://example.com/page"

    normalized1 = dc._normalize_url(url1)
    normalized2 = dc._normalize_url(url2)

    assert normalized1 == normalized2
    assert "utm_source" not in normalized1
    assert "utm_campaign" not in normalized1


def test_jaccard_above_threshold_creates_candidate(tmp_path):
    """Titles with ≥0.70 similarity should create a candidate."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    # Very similar titles, different URLs
    write_memory(memories_dir, "file1.md", "reading",
                 source_url="https://example.com/page1",
                 source_title="Advanced Machine Learning Techniques for Deep Neural Networks")
    write_memory(memories_dir, "file2.md", "reading",
                 source_url="https://example.com/page2",
                 source_title="Advanced Machine Learning Techniques for Neural Networks")

    result = dc.run(memories_dir, deploy_dir)

    assert result["new_candidates"] == 1

    # Check state file
    state = json.loads((deploy_dir / "dedup-state.json").read_text())
    assert len(state["candidates"]) == 1
    assert state["candidates"][0]["similarity"] >= 0.70


def test_jaccard_below_threshold_no_candidate(tmp_path):
    """Titles with <0.70 similarity should not create a candidate."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    # Very different titles
    write_memory(memories_dir, "file1.md", "reading",
                 source_url="https://example.com/page1",
                 source_title="Machine Learning Basics")
    write_memory(memories_dir, "file2.md", "reading",
                 source_url="https://example.com/page2",
                 source_title="Cooking Recipes for Beginners")

    result = dc.run(memories_dir, deploy_dir)

    assert result["new_candidates"] == 0


def test_dismissed_pair_not_re_added(tmp_path):
    """Pair in dismissed list should not be re-added to candidates."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    # Pre-populate state with dismissed pair
    state_file = deploy_dir / "dedup-state.json"
    state_file.write_text(json.dumps({
        "candidates": [],
        "dismissed": [{
            "a": "file1.md",
            "b": "file2.md",
            "similarity": 0.85,
            "detected_at": "2026-04-17T12:00:00"
        }]
    }))

    # Create files that would normally create a candidate
    write_memory(memories_dir, "file1.md", "reading",
                 source_url="https://example.com/page1",
                 source_title="Advanced Machine Learning Techniques for Deep Neural Networks")
    write_memory(memories_dir, "file2.md", "reading",
                 source_url="https://example.com/page2",
                 source_title="Advanced Machine Learning Techniques for Neural Networks")

    result = dc.run(memories_dir, deploy_dir)

    # Should not create new candidate since pair is dismissed
    assert result["new_candidates"] == 0

    # Verify dismissed list unchanged
    state = json.loads(state_file.read_text())
    assert len(state["dismissed"]) == 1
    assert len(state["candidates"]) == 0


def test_url_normalization_strips_www(tmp_path):
    """URL normalization should strip www. prefix."""
    url1 = "https://www.example.com/page"
    url2 = "https://example.com/page"

    normalized1 = dc._normalize_url(url1)
    normalized2 = dc._normalize_url(url2)

    assert normalized1 == normalized2
    assert "www." not in normalized1


def test_different_types_no_merge(tmp_path):
    """Files with different types should not be merged even with same URL."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    write_memory(memories_dir, "file1.md", "reading",
                 source_url="https://example.com/page",
                 source_title="Test")
    write_memory(memories_dir, "file2.md", "email_thread",
                 source_url="https://example.com/page",
                 source_title="Test")

    result = dc.run(memories_dir, deploy_dir)

    # Should not merge different types
    assert result["auto_merged"] == 0
    remaining = list(memories_dir.glob("*.md"))
    assert len(remaining) == 2


def test_empty_url_no_auto_merge(tmp_path):
    """Files with empty URLs should not participate in auto-merge."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    write_memory(memories_dir, "file1.md", "reading",
                 source_url="",
                 source_title="Test Article")
    write_memory(memories_dir, "file2.md", "reading",
                 source_url="",
                 source_title="Test Article")

    result = dc.run(memories_dir, deploy_dir)

    # Should not auto-merge empty URLs
    assert result["auto_merged"] == 0
    remaining = list(memories_dir.glob("*.md"))
    assert len(remaining) == 2
