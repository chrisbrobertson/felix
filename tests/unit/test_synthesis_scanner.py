"""Tests for synthesis_scanner.py"""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import synthesis_scanner
from synthesis_scanner import (
    _cluster_hash,
    _build_clusters,
    _jaccard,
    _parse_frontmatter,
    SynthesisScanner,
    load_state,
    save_state,
)


@pytest.fixture
def tmp_memories(tmp_path):
    """Create a temporary memories directory."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    return memories_dir


@pytest.fixture
def tmp_deploy(tmp_path):
    """Create a temporary deploy directory."""
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    return deploy_dir


@pytest.fixture
def mock_paths(tmp_memories, tmp_deploy):
    """Patch module-level constants to use temp directories."""
    with patch.object(synthesis_scanner, "MEMORIES_DIR", tmp_memories), \
         patch.object(synthesis_scanner, "DEPLOY_DIR", tmp_deploy), \
         patch.object(synthesis_scanner, "STATE_FILE", tmp_deploy / "synthesis-state.json"):
        yield


def _write_memory(path: Path, frontmatter: dict, body: str = "Test body"):
    """Helper to write a memory file with frontmatter."""
    import yaml
    fm_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
    content = f"---\n{fm_str}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")


# ── Test cluster_hash ─────────────────────────────────────────────────────────


def test_cluster_hash_deterministic(tmp_memories):
    """Same paths should produce same hash regardless of order."""
    path1 = tmp_memories / "file1.md"
    path2 = tmp_memories / "file2.md"
    path3 = tmp_memories / "file3.md"

    hash1 = _cluster_hash([path1, path2, path3])
    hash2 = _cluster_hash([path3, path1, path2])
    hash3 = _cluster_hash([path2, path3, path1])

    assert hash1 == hash2 == hash3
    assert len(hash1) == 40  # SHA1 hex digest


# ── Test build_clusters ───────────────────────────────────────────────────────


def test_build_clusters_finds_related_by_tags(tmp_memories, mock_paths):
    """Memories sharing ≥2 tags should form a cluster."""
    # Create 3 memories with shared tags
    _write_memory(
        tmp_memories / "mem1.md",
        {"type": "web", "source_title": "AI Research", "tags": ["ai", "research", "ml"], "summary": "Summary 1"}
    )
    _write_memory(
        tmp_memories / "mem2.md",
        {"type": "web", "source_title": "Machine Learning", "tags": ["ai", "ml", "neural"], "summary": "Summary 2"}
    )
    _write_memory(
        tmp_memories / "mem3.md",
        {"type": "web", "source_title": "Deep Learning", "tags": ["ai", "research", "neural"], "summary": "Summary 3"}
    )

    clusters = _build_clusters(tmp_memories)

    # All 3 should be in one cluster (each pair shares ≥2 tags)
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_build_clusters_finds_related_by_title_similarity(tmp_memories, mock_paths):
    """Memories with similar titles (Jaccard ≥ 0.40) should form a cluster."""
    _write_memory(
        tmp_memories / "mem1.md",
        {"type": "web", "source_title": "Introduction to Neural Networks", "tags": [], "summary": "Summary 1"}
    )
    _write_memory(
        tmp_memories / "mem2.md",
        {"type": "web", "source_title": "Neural Networks Introduction Tutorial", "tags": [], "summary": "Summary 2"}
    )
    _write_memory(
        tmp_memories / "mem3.md",
        {"type": "web", "source_title": "Getting Started with Neural Networks", "tags": [], "summary": "Summary 3"}
    )

    clusters = _build_clusters(tmp_memories)

    # Should form one cluster based on title similarity
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_build_clusters_skips_synthesis_type(tmp_memories, mock_paths):
    """Synthesis memories should not be clustered."""
    _write_memory(
        tmp_memories / "synth1.md",
        {"type": "synthesis", "source_title": "Synthesis", "tags": ["ai", "ml"], "summary": "Summary"}
    )
    _write_memory(
        tmp_memories / "synth2.md",
        {"type": "synthesis", "source_title": "Another Synthesis", "tags": ["ai", "ml"], "summary": "Summary"}
    )
    _write_memory(
        tmp_memories / "synth3.md",
        {"type": "synthesis", "source_title": "Third Synthesis", "tags": ["ai", "ml"], "summary": "Summary"}
    )

    clusters = _build_clusters(tmp_memories)

    # No clusters should be formed
    assert len(clusters) == 0


def test_build_clusters_skips_excluded_types(tmp_memories, mock_paths):
    """Code, goal, project, calendar_event, commitment types should be skipped."""
    _write_memory(
        tmp_memories / "code1.md",
        {"type": "code", "source_title": "Repo", "tags": ["python"], "summary": "Code"}
    )
    _write_memory(
        tmp_memories / "goal1.md",
        {"type": "goal", "source_title": "Goal", "tags": ["work"], "summary": "Goal"}
    )
    _write_memory(
        tmp_memories / "proj1.md",
        {"type": "project", "source_title": "Project", "tags": ["work"], "summary": "Project"}
    )
    _write_memory(
        tmp_memories / "cal1.md",
        {"type": "calendar_event", "source_title": "Meeting", "tags": ["work"], "summary": "Meeting"}
    )
    _write_memory(
        tmp_memories / "commit1.md",
        {"type": "commitment", "source_title": "Task", "tags": ["work"], "summary": "Task"}
    )

    clusters = _build_clusters(tmp_memories)

    # No clusters from excluded types
    assert len(clusters) == 0


def test_small_cluster_below_threshold_excluded(tmp_memories, mock_paths):
    """Clusters with <3 members should not be returned."""
    # Create 2 related memories (below threshold)
    _write_memory(
        tmp_memories / "mem1.md",
        {"type": "web", "source_title": "AI", "tags": ["ai", "ml"], "summary": "Summary"}
    )
    _write_memory(
        tmp_memories / "mem2.md",
        {"type": "web", "source_title": "ML", "tags": ["ai", "ml"], "summary": "Summary"}
    )

    clusters = _build_clusters(tmp_memories)

    # No cluster should be returned (size < 3)
    assert len(clusters) == 0


# ── Test run_once ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_skips_already_processed(tmp_memories, tmp_deploy, mock_paths):
    """Clusters already in state should be skipped."""
    # Create a cluster
    _write_memory(
        tmp_memories / "mem1.md",
        {"type": "web", "source_title": "AI", "tags": ["ai", "ml", "research"], "summary": "Summary 1"}
    )
    _write_memory(
        tmp_memories / "mem2.md",
        {"type": "web", "source_title": "ML", "tags": ["ai", "ml", "research"], "summary": "Summary 2"}
    )
    _write_memory(
        tmp_memories / "mem3.md",
        {"type": "web", "source_title": "Research", "tags": ["ai", "ml", "research"], "summary": "Summary 3"}
    )

    # Pre-populate state with this cluster's hash
    cluster_paths = list(tmp_memories.glob("*.md"))
    cluster_id = _cluster_hash(cluster_paths)
    state = {"processed_clusters": [cluster_id]}
    state_file = tmp_deploy / "synthesis-state.json"
    state_file.write_text(json.dumps(state))

    scanner = SynthesisScanner()

    # Mock MemoryWriter
    with patch("synthesis_scanner.MemoryWriter") as mock_writer_class:
        mock_writer = AsyncMock()
        mock_writer_class.return_value = mock_writer

        count = await scanner.run_once()

        # Should skip the already-processed cluster
        assert count == 0
        mock_writer.write.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_writes_synthesis_memory(tmp_memories, tmp_deploy, mock_paths):
    """New cluster should trigger synthesis memory write."""
    # Create a cluster
    _write_memory(
        tmp_memories / "mem1.md",
        {"type": "web", "source_title": "Artificial Intelligence", "tags": ["ai", "ml", "research"], "summary": "AI summary"}
    )
    _write_memory(
        tmp_memories / "mem2.md",
        {"type": "web", "source_title": "Machine Learning", "tags": ["ai", "ml", "research"], "summary": "ML summary"}
    )
    _write_memory(
        tmp_memories / "mem3.md",
        {"type": "web", "source_title": "Research Methods", "tags": ["ai", "ml", "research"], "summary": "Research summary"}
    )

    scanner = SynthesisScanner()

    # Mock MemoryWriter and acompletion
    mock_synthesis_text = "**Synthesis**: This is a test synthesis.\n**Cross-cutting themes**:\n- Theme 1\n- Theme 2"

    with patch("synthesis_scanner.MemoryWriter") as mock_writer_class, \
         patch("synthesis_scanner.acompletion") as mock_acompletion:

        mock_writer = AsyncMock()
        mock_writer.write.return_value = "2026-04-18-synthesis-abc123.md"
        mock_writer_class.return_value = mock_writer

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = mock_synthesis_text
        mock_acompletion.return_value = mock_resp

        count = await scanner.run_once()

        # Should write 1 synthesis memory
        assert count == 1
        mock_writer.write.assert_called_once()

        # Check the entry passed to write
        call_args = mock_writer.write.call_args
        entry = call_args[0][0]
        body = call_args[0][1]

        assert entry["type"] == "synthesis"
        assert entry["content_type"] == "synthesis"
        assert "synthesis://" in entry["url"]
        assert "Synthesis:" in entry["title"]
        assert len(entry["source_files"]) == 3
        assert body == mock_synthesis_text

        # State should be updated
        state_file = tmp_deploy / "synthesis-state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert len(state["processed_clusters"]) == 1


# ── Test Jaccard similarity ───────────────────────────────────────────────────


def test_jaccard_similarity():
    """Test Jaccard similarity calculation."""
    # Identical texts
    assert _jaccard("hello world", "hello world") == 1.0

    # Completely different
    assert _jaccard("hello", "world") == 0.0

    # Partial overlap
    # "neural networks" and "networks deep" share "networks"
    # Set A: {neural, networks}, Set B: {networks, deep}
    # Intersection: {networks} = 1, Union: {neural, networks, deep} = 3
    # Jaccard = 1/3 ≈ 0.333
    sim = _jaccard("neural networks", "networks deep")
    assert 0.3 < sim < 0.4

    # Empty strings
    assert _jaccard("", "") == 0.0
    assert _jaccard("hello", "") == 0.0
