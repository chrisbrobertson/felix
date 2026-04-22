"""
Unit tests for project_inference_scanner.

All external access (LiteLLM, filesystem) is mocked.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import project_inference_scanner as pis
from project_inference_scanner import (
    ProjectInferenceScanner,
    _parse_frontmatter,
    _slugify,
    _title_similarity,
)


@pytest.fixture(autouse=True)
def _isolate_project_inference_paths(monkeypatch, tmp_path_factory):
    """Redirect project_inference_scanner.MEMORIES_DIR and DEPLOY_DIR to per-test tmp paths.

    Prevents test pollution of the production memories directory at the
    fixture layer. Tests that set either constant explicitly via
    `patch.object` still work because their inner patch supersedes this
    outer autouse patch.
    """
    ghost_memories = tmp_path_factory.mktemp("pis-memories")
    ghost_deploy = tmp_path_factory.mktemp("pis-deploy")
    monkeypatch.setattr(pis, "MEMORIES_DIR", ghost_memories, raising=False)
    monkeypatch.setattr(pis, "DEPLOY_DIR", ghost_deploy, raising=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_memory_file(memories_dir, filename, type_, source_title, summary="Test summary"):
    """Write a minimal memory file with the given type."""
    fm = {
        "type": type_,
        "source_title": source_title,
        "summary": summary,
        "meeting_date": "2026-04-15T09:00:00" if type_ == "meeting_transcript" else None,
    }
    path = memories_dir / filename
    path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Summary\n{summary}\n")
    return path


def make_project_file(memories_dir, source_title, type_="project"):
    """Write a project or project-candidate file."""
    fm = {
        "type": type_,
        "source_title": source_title,
        "summary": "Test project",
    }
    slug = _slugify(source_title)
    filename = f"{type_}-{slug}-abc123.md"
    path = memories_dir / filename
    path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Notes\n")
    return path


# ── Helper tests ──────────────────────────────────────────────────────────────

def test_title_similarity_exact_match():
    """Identical titles should have similarity=1.0."""
    assert _title_similarity("Q2 rollout plan", "Q2 rollout plan") == 1.0


def test_title_similarity_partial_match():
    """Partial overlap should return Jaccard index."""
    # "Q2 rollout" vs "Q2 launch"
    # tokens: {q2, rollout} vs {q2, launch}
    # intersection: {q2}, union: {q2, rollout, launch}
    # similarity = 1/3
    sim = _title_similarity("Q2 rollout", "Q2 launch")
    assert 0.3 < sim < 0.4


def test_title_similarity_no_match():
    """Completely different titles should have low similarity."""
    assert _title_similarity("Garden shed", "Spanish lessons") < 0.1


# ── Scanner tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skips_unchanged_mtime(tmp_path):
    """Files with unchanged mtime are not sent to LLM."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"

    # Write a memory file
    mem_file = make_memory_file(
        memories_dir, "meeting-test.md", "meeting_transcript", "Test meeting"
    )
    mtime = mem_file.stat().st_mtime

    # Pre-populate state with same mtime
    state_file.write_text(json.dumps({
        "processed": {"meeting-test.md": mtime},
        "last_scan": "2026-04-15T09:00:00"
    }))

    config_file.write_text(yaml.dump({"project_inference": {"enabled": True}}))

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock()) as mock_llm:

        scanner = ProjectInferenceScanner(role="full")
        await scanner._scan()

        # LLM should not be called because mtime is unchanged
        mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_processes_new_mtime(tmp_path):
    """Files with updated mtime trigger LLM extraction."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"

    # Write a memory file
    mem_file = make_memory_file(
        memories_dir, "meeting-test.md", "meeting_transcript", "Q2 planning session"
    )
    mtime = mem_file.stat().st_mtime

    # Pre-populate state with older mtime
    state_file.write_text(json.dumps({
        "processed": {"meeting-test.md": mtime - 100},
        "last_scan": "2026-04-15T08:00:00"
    }))

    config_file.write_text(yaml.dump({"project_inference": {"enabled": True}}))

    # Mock LLM response
    from unittest.mock import MagicMock
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({"projects": []})

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)) as mock_llm:

        scanner = ProjectInferenceScanner(role="full")
        await scanner._scan()

        # LLM should be called because mtime changed
        mock_llm.assert_called_once()


@pytest.mark.asyncio
async def test_confidence_filter_below_threshold(tmp_path):
    """LLM response with confidence=0.6 → no candidate file written."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"

    mem_file = make_memory_file(
        memories_dir, "meeting-test.md", "meeting_transcript", "Vague discussion"
    )

    state_file.write_text(json.dumps({"processed": {}, "last_scan": None}))
    config_file.write_text(yaml.dump({
        "project_inference": {"enabled": True, "confidence_threshold": 0.7}
    }))

    # Mock LLM response with low confidence
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "projects": [
            {
                "title": "Low confidence project",
                "category_guess": "work",
                "summary": "Not sure about this",
                "confidence": 0.6,
                "due_date_guess": None,
                "evidence_quote": "Maybe we should do something?"
            }
        ]
    })

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):

        scanner = ProjectInferenceScanner(role="full")
        await scanner._scan()

        # No candidate file should be written (below 0.7 threshold)
        candidates = list(memories_dir.glob("project-candidate-*.md"))
        assert len(candidates) == 0


@pytest.mark.asyncio
async def test_confidence_filter_above_threshold(tmp_path):
    """LLM response with confidence=0.75 → candidate file written."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"

    mem_file = make_memory_file(
        memories_dir, "meeting-test.md", "meeting_transcript", "Q2 planning"
    )

    state_file.write_text(json.dumps({"processed": {}, "last_scan": None}))
    config_file.write_text(yaml.dump({
        "project_inference": {"enabled": True, "confidence_threshold": 0.7}
    }))

    # Mock LLM response with high confidence
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "projects": [
            {
                "title": "Q2 rollout plan",
                "category_guess": "work",
                "summary": "Coordinating Q2 launch",
                "confidence": 0.85,
                "due_date_guess": "2026-07-01",
                "evidence_quote": "Q2 launch deadline is July 1st"
            }
        ]
    })

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):

        scanner = ProjectInferenceScanner(role="full")
        await scanner._scan()

        # Candidate file should be written
        candidates = list(memories_dir.glob("project-candidate-*.md"))
        assert len(candidates) == 1

        # Check frontmatter
        fm = _parse_frontmatter(candidates[0].read_text())
        assert fm["type"] == "project_candidate"
        assert fm["confidence"] == 0.85
        assert "meeting-test.md" in fm["evidence"]


@pytest.mark.asyncio
async def test_dedup_existing_project_by_title(tmp_path):
    """Similar title to existing project-*.md → candidate not written."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"

    # Create existing project with similar title
    make_project_file(memories_dir, "Q2 rollout plan")

    mem_file = make_memory_file(
        memories_dir, "meeting-test.md", "meeting_transcript", "Q2 discussion"
    )

    state_file.write_text(json.dumps({"processed": {}, "last_scan": None}))
    config_file.write_text(yaml.dump({"project_inference": {"enabled": True}}))

    # Mock LLM response with very similar title
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "projects": [
            {
                "title": "Q2 Rollout Plan",  # same title, different case
                "category_guess": "work",
                "summary": "Coordinating Q2 launch",
                "confidence": 0.85,
                "due_date_guess": None,
                "evidence_quote": "Q2 planning"
            }
        ]
    })

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):

        scanner = ProjectInferenceScanner(role="full")
        await scanner._scan()

        # No candidate should be written (duplicate detected)
        candidates = list(memories_dir.glob("project-candidate-*.md"))
        assert len(candidates) == 0


@pytest.mark.asyncio
async def test_dedup_rejected_evidence(tmp_path):
    """Source file in rejected-candidates evidence list → skipped."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"
    rejected_file = tmp_path / "rejected-candidates.json"

    mem_file = make_memory_file(
        memories_dir, "meeting-test.md", "meeting_transcript", "Random chat"
    )

    # Pre-populate rejected candidates with this source file
    rejected_file.write_text(json.dumps({
        "rejected": [
            {
                "source_title": "Some project",
                "evidence": ["meeting-test.md"],
                "rejected_at": "2026-04-14T10:00:00"
            }
        ]
    }))

    state_file.write_text(json.dumps({"processed": {}, "last_scan": None}))
    config_file.write_text(yaml.dump({"project_inference": {"enabled": True}}))

    # Mock LLM response
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "projects": [
            {
                "title": "New project",
                "category_guess": "work",
                "summary": "Some work",
                "confidence": 0.85,
                "due_date_guess": None,
                "evidence_quote": "Let's do this"
            }
        ]
    })

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):

        scanner = ProjectInferenceScanner(role="full")
        await scanner._scan()

        # No candidate should be written (source is rejected)
        candidates = list(memories_dir.glob("project-candidate-*.md"))
        assert len(candidates) == 0


@pytest.mark.asyncio
async def test_candidate_file_format(tmp_path):
    """Written candidate has type:project_candidate, required fields."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"

    mem_file = make_memory_file(
        memories_dir, "email-thread-test.md", "email_thread", "Q2 launch email"
    )

    state_file.write_text(json.dumps({"processed": {}, "last_scan": None}))
    config_file.write_text(yaml.dump({"project_inference": {"enabled": True}}))

    # Mock LLM response
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "projects": [
            {
                "title": "Q2 Product Launch",
                "category_guess": "work",
                "summary": "Coordinating cross-functional Q2 launch",
                "confidence": 0.88,
                "due_date_guess": "2026-07-15",
                "evidence_quote": "Q2 launch target is mid-July"
            }
        ]
    })

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):

        scanner = ProjectInferenceScanner(role="full")
        await scanner._scan()

        # Check candidate was written
        candidates = list(memories_dir.glob("project-candidate-*.md"))
        assert len(candidates) == 1

        # Parse and validate frontmatter
        fm = _parse_frontmatter(candidates[0].read_text())
        assert fm["type"] == "project_candidate"
        assert fm["candidate_type"] == "project"
        assert fm["category_guess"] == "work"
        assert fm["source_title"] == "Q2 Product Launch (candidate)"
        assert fm["confidence"] == 0.88
        assert fm["status"] == "pending_confirmation"
        assert "email-thread-test.md" in fm["evidence"]
        assert "extracted_fields" in fm
        assert fm["extracted_fields"]["title"] == "Q2 Product Launch"
        assert fm["extracted_fields"]["due_date"] == "2026-07-15"
        assert "created" in fm


@pytest.mark.asyncio
async def test_cap_20_files_per_cycle(tmp_path):
    """More than 20 changed files → only 20 processed per cycle."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"

    # Create 25 memory files
    for i in range(25):
        make_memory_file(
            memories_dir,
            f"meeting-{i:03d}.md",
            "meeting_transcript",
            f"Meeting {i}"
        )

    state_file.write_text(json.dumps({"processed": {}, "last_scan": None}))
    config_file.write_text(yaml.dump({"project_inference": {"enabled": True}}))

    # Mock LLM response (returns no projects)
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({"projects": []})

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)) as mock_llm:

        scanner = ProjectInferenceScanner(role="full")
        await scanner._scan()

        # LLM should be called exactly 20 times (cap enforced)
        assert mock_llm.call_count == 20
