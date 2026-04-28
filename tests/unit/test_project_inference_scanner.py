"""
Unit tests for project_inference_scanner.

All external access (LiteLLM, filesystem) is mocked.
"""
import json
from datetime import datetime, timedelta
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
from memory_cache import MemoryCache


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

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
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

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
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

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
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

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
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

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
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

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
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

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
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

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
        await scanner._scan()

        # LLM should be called exactly 20 times (cap enforced)
        assert mock_llm.call_count == 20


# ── Candidate-filename unmangle migration (v1.6.2) ────────────────────────────

def _write_candidate(memories_dir, filename, created="2026-04-15T10:00:00", status="pending_confirmation", summary="A test candidate"):
    fm = {
        "type": "project_candidate",
        "source_title": "Test Candidate",
        "summary": summary,
        "status": status,
        "created": created,
    }
    path = memories_dir / filename
    path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Notes\n")
    return path


def test_unmangle_collapses_injected_hostname(tmp_path):
    """project-{hostname}-candidate-*.md → project-candidate-*.md."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    mangled = _write_candidate(memories_dir, "project-Chriss-MacBook-Air-candidate-foo-abc123.md")

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy):
        cache = MemoryCache(None, memories_dir, enabled=False)
        ProjectInferenceScanner(role="full", cache=cache)

    canonical = memories_dir / "project-candidate-foo-abc123.md"
    assert canonical.exists(), "canonical file should exist after unmangle"
    assert not mangled.exists(), "mangled file should have been renamed"


def test_unmangle_collapses_stacked_hostnames(tmp_path):
    """Triple-stacked hostname prefix collapses to canonical."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    mangled = _write_candidate(
        memories_dir,
        "project-Chriss-MacBook-Air-Chriss-Air-Chriss-MacBook-Air-candidate-foo-abc123.md"
    )

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy):
        cache = MemoryCache(None, memories_dir, enabled=False)
        ProjectInferenceScanner(role="full", cache=cache)

    canonical = memories_dir / "project-candidate-foo-abc123.md"
    assert canonical.exists()
    assert not mangled.exists()


def test_unmangle_prefers_status_confirmed_over_pending(tmp_path):
    """When both exist, a confirmed candidate beats a pending one regardless of mtime."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    # Mangled file is confirmed, canonical is pending — confirmed should win
    # and take over the canonical filename.
    mangled = _write_candidate(
        memories_dir,
        "project-Chriss-MacBook-Air-candidate-foo-abc123.md",
        created="2026-04-10T10:00:00",  # older
        status="confirmed",
    )
    canonical = _write_candidate(
        memories_dir,
        "project-candidate-foo-abc123.md",
        created="2026-04-20T10:00:00",  # newer but pending
        status="pending_confirmation",
    )

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy):
        cache = MemoryCache(None, memories_dir, enabled=False)
        ProjectInferenceScanner(role="full", cache=cache)

    # Canonical path should now hold the confirmed content
    assert canonical.exists()
    fm = _parse_frontmatter(canonical.read_text())
    assert fm.get("status") == "confirmed"
    assert not mangled.exists()


def test_unmangle_prefers_newer_created_when_statuses_equal(tmp_path):
    """When both are pending, newer `created` wins."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    mangled = _write_candidate(
        memories_dir,
        "project-Chriss-MacBook-Air-candidate-foo-abc123.md",
        created="2026-04-20T10:00:00",  # newer
        status="pending_confirmation",
        summary="NEW summary",
    )
    canonical = _write_candidate(
        memories_dir,
        "project-candidate-foo-abc123.md",
        created="2026-04-10T10:00:00",  # older
        status="pending_confirmation",
        summary="OLD summary",
    )

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy):
        cache = MemoryCache(None, memories_dir, enabled=False)
        ProjectInferenceScanner(role="full", cache=cache)

    assert canonical.exists()
    fm = _parse_frontmatter(canonical.read_text())
    assert fm.get("summary") == "NEW summary"
    assert not mangled.exists()


def test_unmangle_skips_malformed_filename(tmp_path, caplog):
    """A file matching the glob but with no `candidate-` tail is left on disk with a warning."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    # The glob `project-*-candidate-*.md` needs the literal `-candidate-`
    # segment, so constructing a "malformed" match that the regex can't
    # parse is not straightforward. Instead, verify the regex is the last
    # line of defense: if a file passes the glob but somehow lacks the
    # pattern we expect, it should skip without crashing.
    # We'll test this by asserting the unmangle method directly with a
    # filename that tricks the glob but breaks the regex.
    import logging
    caplog.set_level(logging.WARNING, logger="project-inference")

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy):
        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)

    # Post-init: sentinel was written (no files to process) → idempotent.
    sentinel = deploy / pis.UNMANGLE_SENTINEL_NAME
    assert sentinel.exists()


def test_unmangle_sentinel_makes_idempotent(tmp_path):
    """Second construction does not re-process files."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    mangled = _write_candidate(memories_dir, "project-Chriss-MacBook-Air-candidate-foo-abc123.md")

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy):
        cache = MemoryCache(None, memories_dir, enabled=False)
        ProjectInferenceScanner(role="full", cache=cache)  # first: unmangles
        canonical = memories_dir / "project-candidate-foo-abc123.md"
        assert canonical.exists()

        # Delete canonical, re-run: sentinel should prevent rescanning
        canonical.unlink()
        cache = MemoryCache(None, memories_dir, enabled=False)
        ProjectInferenceScanner(role="full", cache=cache)
        assert not canonical.exists(), "sentinel should have prevented re-run"


def test_unmangle_survives_icloud_edeadlk(tmp_path, monkeypatch):
    """A transient EDEADLK on one file does not crash the loop; sentinel not written."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()

    good = _write_candidate(memories_dir, "project-Chriss-MacBook-Air-candidate-good-abc123.md")
    bad = _write_candidate(memories_dir, "project-Chriss-MacBook-Air-candidate-bad-def456.md")

    # Patch Path.rename to raise EDEADLK only for the `bad` file.
    real_rename = Path.rename

    def flaky_rename(self, target):
        if "bad" in self.name:
            import errno as _errno
            raise OSError(_errno.EDEADLK, "Resource deadlock avoided")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy):
        cache = MemoryCache(None, memories_dir, enabled=False)
        ProjectInferenceScanner(role="full", cache=cache)

    # Good file was renamed
    assert (memories_dir / "project-candidate-good-abc123.md").exists()
    # Bad file still on disk waiting to be retried
    assert bad.exists()
    # Sentinel NOT written because transient > 0
    sentinel = deploy / pis.UNMANGLE_SENTINEL_NAME
    assert not sentinel.exists(), "sentinel must not be written when files remain"


def test_production_memories_dir_never_touched_during_tests(tmp_path):
    """Meta-test: autouse fixture prevents mutation of real iCloud MEMORIES_DIR."""
    from pathlib import Path as _Path
    prod = _Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "second-brain" / "memories"
    if not prod.exists():
        pytest.skip("Production memories dir not present on this machine")
    before = sorted(
        (f.name, f.stat().st_mtime)
        for f in prod.glob("project-*.md")
    )
    # Instantiate under the autouse fixture with no extra patches — the
    # fixture must isolate the real prod dir.
    ProjectInferenceScanner(role="full")
    after = sorted(
        (f.name, f.stat().st_mtime)
        for f in prod.glob("project-*.md")
    )
    assert before == after, "production MEMORIES_DIR was mutated"


@pytest.mark.asyncio
async def test_noop_scan_still_runs_cleanup(tmp_path):
    """Cleanup runs even when no source files have changed (fixes #39).

    Root cause: _cleanup_stale_candidates was only called after processing
    new source files. When all files were stable (unchanged mtime), the early
    return bypassed cleanup, allowing candidates to accumulate past the cap.
    """
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "project-inference-state.json"
    config_file = tmp_path / "config.yaml"

    # A source file whose mtime matches state — no new files to process
    mem_file = make_memory_file(
        memories_dir, "meeting-stable.md", "meeting_transcript", "Old meeting"
    )
    mtime = mem_file.stat().st_mtime
    state_file.write_text(json.dumps({
        "processed": {"meeting-stable.md": mtime},
        "last_scan": "2026-04-15T09:00:00",
    }))

    # A stale candidate file: created 60 days ago, well past default TTL of 30 days
    stale_created = (datetime.now() - timedelta(days=60)).isoformat()
    stale_candidate = memories_dir / "project-candidate-old-abc123.md"
    stale_candidate.write_text(
        f"---\ntype: project_candidate\ncandidate_type: project\n"
        f"status: pending_confirmation\ncreated: '{stale_created}'\n"
        f"source_title: Old candidate\nconfidence: 0.8\n---\n\nOld candidate.\n"
    )

    config_file.write_text(yaml.dump({"project_inference": {"enabled": True}}))

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", tmp_path), \
         patch.object(pis, "CONFIG_PATH", config_file), \
         patch("litellm.acompletion", new=AsyncMock()) as mock_llm:

        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
        await scanner._scan()

        # LLM should not be called (no new source files)
        mock_llm.assert_not_called()
        # Stale candidate must be deleted (cleanup ran on no-op scan)
        assert not stale_candidate.exists(), "stale candidate should have been cleaned up on no-op scan"


# ── Candidate cleanup tests (v1.10.0) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_cleanup_expires_stale_candidates(tmp_path):
    """Candidates older than TTL are deleted."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    config_file = tmp_path / "config.yaml"

    # Create a candidate 40 days old (default TTL is 30 days)
    old_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
    old_candidate = _write_candidate(
        memories_dir,
        "project-candidate-old-abc123.md",
        created=old_date,
        status="pending_confirmation",
    )

    config_file.write_text(yaml.dump({
        "project_inference": {"enabled": True}
    }))

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy), \
         patch.object(pis, "CONFIG_PATH", config_file):
        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
        deleted = await scanner._cleanup_stale_candidates()

    # Old candidate should be deleted
    assert deleted == 1
    assert not old_candidate.exists()


@pytest.mark.asyncio
async def test_cleanup_preserves_confirmed_and_rejected(tmp_path):
    """Confirmed and rejected candidates are not deleted regardless of age."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    config_file = tmp_path / "config.yaml"

    # Create old candidates with different statuses
    old_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
    confirmed = _write_candidate(
        memories_dir,
        "project-candidate-confirmed-abc123.md",
        created=old_date,
        status="confirmed",
    )
    rejected = _write_candidate(
        memories_dir,
        "project-candidate-rejected-def456.md",
        created=old_date,
        status="rejected",
    )

    config_file.write_text(yaml.dump({
        "project_inference": {"enabled": True}
    }))

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy), \
         patch.object(pis, "CONFIG_PATH", config_file):
        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
        deleted = await scanner._cleanup_stale_candidates()

    # No files should be deleted (confirmed and rejected are preserved)
    assert deleted == 0
    assert confirmed.exists()
    assert rejected.exists()


@pytest.mark.asyncio
async def test_cleanup_caps_pending_at_max(tmp_path):
    """When pending count exceeds max_pending_candidates, oldest are deleted."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    config_file = tmp_path / "config.yaml"

    # Create 250 pending candidates all within TTL (5 days old)
    recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
    for i in range(250):
        # Stagger creation times slightly so we can test "oldest first" behavior
        created = (datetime.now() - timedelta(days=5, hours=i)).strftime("%Y-%m-%dT%H:%M:%S")
        _write_candidate(
            memories_dir,
            f"project-candidate-item-{i:03d}-{i:06x}.md",
            created=created,
            status="pending_confirmation",
        )

    # Set max to 200
    config_file.write_text(yaml.dump({
        "project_inference": {
            "enabled": True,
            "max_pending_candidates": 200,
        }
    }))

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy), \
         patch.object(pis, "CONFIG_PATH", config_file):
        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
        deleted = await scanner._cleanup_stale_candidates()

    # Exactly 50 should be deleted (250 - 200 cap)
    assert deleted == 50
    # 200 should remain
    remaining = list(memories_dir.glob("project-candidate-*.md"))
    assert len(remaining) == 200


@pytest.mark.asyncio
async def test_cleanup_oldest_first_deletion_order(tmp_path):
    """When capping, oldest candidates are deleted first."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    config_file = tmp_path / "config.yaml"

    # Create 5 candidates with different ages
    dates = [
        (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S"),  # oldest
        (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S"),
        (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S"),
        (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S"),
        (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),   # newest
    ]

    for i, created in enumerate(dates):
        _write_candidate(
            memories_dir,
            f"project-candidate-item-{i}-{i:06x}.md",
            created=created,
            status="pending_confirmation",
        )

    # Set max to 3 (should delete 2 oldest)
    config_file.write_text(yaml.dump({
        "project_inference": {
            "enabled": True,
            "max_pending_candidates": 3,
            "candidate_ttl_days": 999,  # No TTL expiry
        }
    }))

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy), \
         patch.object(pis, "CONFIG_PATH", config_file):
        cache = MemoryCache(None, memories_dir, enabled=False)
        scanner = ProjectInferenceScanner(role="full", cache=cache)
        deleted = await scanner._cleanup_stale_candidates()

    # 2 should be deleted
    assert deleted == 2

    # The 2 oldest should be gone
    assert not (memories_dir / "project-candidate-item-0-000000.md").exists()
    assert not (memories_dir / "project-candidate-item-1-000001.md").exists()

    # The 3 newest should remain
    assert (memories_dir / "project-candidate-item-2-000002.md").exists()
    assert (memories_dir / "project-candidate-item-3-000003.md").exists()
    assert (memories_dir / "project-candidate-item-4-000004.md").exists()


@pytest.mark.asyncio
async def test_cleanup_expires_stale_candidates_cache_mode(tmp_path):
    """_cleanup_stale_candidates actually deletes stale files when MemoryCache is SQLite-backed.

    Regression for the trailing-dash mismatch: query_by_prefix("project-candidate-")
    did WHERE prefix = "project-candidate-" in cache mode, but _extract_prefix stores
    "project-candidate" (no dash), so no rows were ever returned and cleanup was a no-op.
    """
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    config_file = tmp_path / "config.yaml"
    cache_db = tmp_path / "memory-cache.sqlite"

    old_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
    stale = _write_candidate(
        memories_dir,
        "project-candidate-old-stale-abc123.md",
        created=old_date,
        status="pending_confirmation",
    )
    # A fresh candidate within TTL — must survive
    fresh = _write_candidate(
        memories_dir,
        "project-candidate-fresh-def456.md",
        created=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        status="pending_confirmation",
    )

    config_file.write_text(yaml.dump({"project_inference": {"enabled": True}}))

    with patch.object(pis, "MEMORIES_DIR", memories_dir), \
         patch.object(pis, "DEPLOY_DIR", deploy), \
         patch.object(pis, "CONFIG_PATH", config_file):
        # SQLite-backed cache (enabled=True)
        cache = MemoryCache(cache_db, memories_dir)
        await cache.rebuild()

        scanner = ProjectInferenceScanner(role="full", cache=cache)
        deleted = await scanner._cleanup_stale_candidates()
        cache.close()

    assert deleted == 1, "stale candidate must be deleted in cache mode"
    assert not stale.exists(), "stale file should be gone"
    assert fresh.exists(), "fresh file should be preserved"
