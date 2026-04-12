"""
Unit tests for skill_creator.py

Tests the automatic skill creation lifecycle: gap detection, seed generation,
approval workflow, probation tracking, and graduation checks.
"""

import asyncio
import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import yaml


@pytest.fixture
def temp_dirs(tmp_path):
    """Create temporary directories for testing."""
    brain_dir = tmp_path / "brain"
    deploy_dir = tmp_path / "deploy"
    skills_dir = brain_dir / "skills"
    drafts_dir = brain_dir / "skill-drafts"

    skills_dir.mkdir(parents=True)
    drafts_dir.mkdir(parents=True)
    deploy_dir.mkdir(parents=True)

    # Create template skill
    template_content = """---
name: summarize-webpage
version: 1
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: true
---

## Instructions

You are creating a long-term memory entry from a webpage.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v1 (2026-04-11) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""
    (skills_dir / "summarize-webpage.md").write_text(template_content)

    return {
        "brain_dir": brain_dir,
        "deploy_dir": deploy_dir,
        "skills_dir": skills_dir,
        "drafts_dir": drafts_dir,
    }


@pytest.fixture
def config():
    """Default config for SkillCreator."""
    return {
        "skill_creation": {
            "enabled": True,
            "require_approval": False,
            "probation_executions": 5,
            "graduation_utility_threshold": 0.6,
            "model_route": "chat",
            "rejection_cooldown_hours": 24,
            "max_graduation_attempts": 3,
        }
    }


@pytest.fixture
def mock_skill_router():
    """Mock skill_router module."""
    with patch.dict("sys.modules", {"skill_router": MagicMock()}):
        import sys
        mock_router = sys.modules["skill_router"]
        mock_router.SKILL_REGISTRY = {
            "default": "summarize-webpage",
        }
        yield mock_router


@pytest.mark.asyncio
async def test_handle_gap_disabled(temp_dirs, config):
    """Test that handle_gap returns None when disabled."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        config["skill_creation"]["enabled"] = False
        creator = SkillCreator(config)

        result = await creator.handle_gap("research-paper", "https://example.com", "content")
        assert result is None


@pytest.mark.asyncio
async def test_handle_gap_in_cooldown(temp_dirs, config, mock_skill_router):
    """Test that handle_gap returns None when content_type is in cooldown."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Add rejected_types entry
        registry_file = temp_dirs["deploy_dir"] / "skills-registry.json"
        registry = {
            "require_approval_runtime_override": None,
            "skills": {},
            "rejected_types": {
                "research-paper": datetime.now().isoformat()
            }
        }
        registry_file.write_text(json.dumps(registry))

        result = await creator.handle_gap("research-paper", "https://example.com", "content")
        assert result is None


@pytest.mark.asyncio
async def test_handle_gap_creates_draft_with_approval(temp_dirs, config, mock_skill_router):
    """Test that handle_gap creates a draft when approval is required."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]), \
         patch("skill_creator.acompletion") as mock_acompletion:

        from skill_creator import SkillCreator

        # Mock LLM response
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="""---
name: summarize-research-paper
version: 1
content_type: research-paper
total_runs: 0
success_rate: null
---

## Instructions

Test instructions for research papers.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""))]
        mock_acompletion.return_value = mock_response

        config["skill_creation"]["require_approval"] = True
        creator = SkillCreator(config)

        # Mock notification callback
        notification_sent = []
        async def mock_notify(msg):
            notification_sent.append(msg)
        creator._notification_callback = mock_notify

        result = await creator.handle_gap("research-paper", "https://arxiv.org/abs/1234", "Test paper content")

        assert result == "summarize-research-paper"

        # Check draft file was created
        draft_path = temp_dirs["drafts_dir"] / "summarize-research-paper.md"
        assert draft_path.exists()

        # Check registry entry
        registry_file = temp_dirs["deploy_dir"] / "skills-registry.json"
        registry = json.loads(registry_file.read_text())
        assert "summarize-research-paper" in registry["skills"]
        assert registry["skills"]["summarize-research-paper"]["status"] == "pending-approval"

        # Check notification was sent
        assert len(notification_sent) == 1
        assert "summarize-research-paper" in notification_sent[0]


@pytest.mark.asyncio
async def test_handle_gap_creates_skill_without_approval(temp_dirs, config, mock_skill_router):
    """Test that handle_gap creates an active skill when approval is not required."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]), \
         patch("skill_creator.acompletion") as mock_acompletion:

        from skill_creator import SkillCreator

        # Mock LLM response
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="""---
name: summarize-documentation
version: 1
content_type: documentation
total_runs: 0
success_rate: null
---

## Instructions

Test instructions for documentation.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""))]
        mock_acompletion.return_value = mock_response

        creator = SkillCreator(config)

        # Mock notification callback
        notification_sent = []
        async def mock_notify(msg):
            notification_sent.append(msg)
        creator._notification_callback = mock_notify

        result = await creator.handle_gap("documentation", "https://docs.python.org", "Test docs content")

        assert result == "summarize-documentation"

        # Check skill file was created (not draft)
        skill_path = temp_dirs["skills_dir"] / "summarize-documentation.md"
        assert skill_path.exists()

        # Check registry entry
        registry_file = temp_dirs["deploy_dir"] / "skills-registry.json"
        registry = json.loads(registry_file.read_text())
        assert "summarize-documentation" in registry["skills"]
        assert registry["skills"]["summarize-documentation"]["status"] == "probation"
        assert registry["skills"]["summarize-documentation"]["probation_count"] == 0

        # Check SKILL_REGISTRY was updated
        assert mock_skill_router.SKILL_REGISTRY["documentation"] == "summarize-documentation"

        # Check notification was sent
        assert len(notification_sent) == 1
        assert "summarize-documentation" in notification_sent[0]


def test_approve_draft(temp_dirs, config, mock_skill_router):
    """Test approving a pending draft."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Create a draft
        draft_content = """---
name: summarize-test
version: 1
---

## Instructions

Test

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""
        draft_path = temp_dirs["drafts_dir"] / "summarize-test.md"
        draft_path.write_text(draft_content)

        # Create registry entry
        creator._write_registry_entry(
            "summarize-test",
            "test",
            "pending-approval",
            {"draft_path": str(draft_path)}
        )

        # Approve
        result = creator.approve_draft("summarize-test")
        assert result is True

        # Check draft was moved to skills
        assert not draft_path.exists()
        skill_path = temp_dirs["skills_dir"] / "summarize-test.md"
        assert skill_path.exists()

        # Check registry updated
        registry_file = temp_dirs["deploy_dir"] / "skills-registry.json"
        registry = json.loads(registry_file.read_text())
        assert registry["skills"]["summarize-test"]["status"] == "probation"
        assert registry["skills"]["summarize-test"]["probation_count"] == 0


def test_reject_draft(temp_dirs, config, mock_skill_router):
    """Test rejecting a pending draft."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Create a draft
        draft_content = """---
name: summarize-test
version: 1
---

## Instructions

Test
"""
        draft_path = temp_dirs["drafts_dir"] / "summarize-test.md"
        draft_path.write_text(draft_content)

        # Create registry entry
        creator._write_registry_entry(
            "summarize-test",
            "test",
            "pending-approval",
            {"draft_path": str(draft_path)}
        )

        # Reject
        result = creator.reject_draft("summarize-test")
        assert result is True

        # Check draft was deleted
        assert not draft_path.exists()

        # Check registry updated
        registry_file = temp_dirs["deploy_dir"] / "skills-registry.json"
        registry = json.loads(registry_file.read_text())
        assert registry["skills"]["summarize-test"]["status"] == "rejected"
        assert "test" in registry["rejected_types"]


def test_increment_probation(temp_dirs, config):
    """Test incrementing probation count."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Create registry entry
        creator._write_registry_entry(
            "summarize-test",
            "test",
            "probation",
            {"probation_count": 2}
        )

        # Increment
        entry = creator.increment_probation("summarize-test")
        assert entry["probation_count"] == 3

        # Increment again
        entry = creator.increment_probation("summarize-test")
        assert entry["probation_count"] == 4


def test_set_approval_override(temp_dirs, config):
    """Test setting approval override."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Initially should use config value (False)
        assert creator.get_effective_approval_mode() is False

        # Set override to True
        creator.set_approval_override(True)
        assert creator.get_effective_approval_mode() is True

        # Set override to False
        creator.set_approval_override(False)
        assert creator.get_effective_approval_mode() is False

        # Clear override (None)
        creator.set_approval_override(None)
        assert creator.get_effective_approval_mode() is False  # Back to config


def test_list_pending_drafts(temp_dirs, config):
    """Test listing pending drafts."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Create some drafts
        creator._write_registry_entry(
            "summarize-test1",
            "test1",
            "pending-approval",
            {"draft_path": "/tmp/test1.md", "example_url": "https://example.com/1"}
        )
        creator._write_registry_entry(
            "summarize-test2",
            "test2",
            "probation",
            {"probation_count": 1}
        )
        creator._write_registry_entry(
            "summarize-test3",
            "test3",
            "pending-approval",
            {"draft_path": "/tmp/test3.md", "example_url": "https://example.com/3"}
        )

        # List pending
        pending = creator.list_pending_drafts()
        assert len(pending) == 2
        assert any(d["skill_name"] == "summarize-test1" for d in pending)
        assert any(d["skill_name"] == "summarize-test3" for d in pending)
        assert all(d["skill_name"] != "summarize-test2" for d in pending)


@pytest.mark.asyncio
async def test_run_probation_check_graduation(temp_dirs, config):
    """Test probation check with successful graduation."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Create a skill file with execution history
        skill_content = """---
name: summarize-test
version: 1
status: probation
---

## Instructions

Test

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
| 2026-01-01 | test1 | claude | 0.7 | good |
| 2026-01-02 | test2 | claude | 0.8 | good |
| 2026-01-03 | test3 | claude | 0.75 | good |
"""
        skill_path = temp_dirs["skills_dir"] / "summarize-test.md"
        skill_path.write_text(skill_content)

        # Create registry entry (probation_count >= target)
        creator._write_registry_entry(
            "summarize-test",
            "test",
            "probation",
            {"probation_count": 5}
        )

        # Run probation check
        await creator.run_probation_check()

        # Reload creator to get updated registry
        creator = SkillCreator(config)

        # Check skill was graduated
        registry = creator._load_registry()
        assert registry["skills"]["summarize-test"]["status"] == "active"
        assert "graduated" in registry["skills"]["summarize-test"]


@pytest.mark.asyncio
async def test_run_probation_check_failed(temp_dirs, config):
    """Test probation check with failed graduation."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Create a skill file with low scores
        skill_content = """---
name: summarize-test
version: 1
status: probation
---

## Instructions

Test

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
| 2026-01-01 | test1 | claude | 0.3 | poor |
| 2026-01-02 | test2 | claude | 0.4 | poor |
| 2026-01-03 | test3 | claude | 0.35 | poor |
"""
        skill_path = temp_dirs["skills_dir"] / "summarize-test.md"
        skill_path.write_text(skill_content)

        # Create registry entry (probation_count >= target, max attempts reached)
        creator._write_registry_entry(
            "summarize-test",
            "test",
            "probation",
            {"probation_count": 5, "graduation_attempts": 2}
        )

        # Run probation check
        await creator.run_probation_check()

        # Reload creator to get updated registry
        creator = SkillCreator(config)

        # Check skill was failed
        registry = creator._load_registry()
        assert registry["skills"]["summarize-test"]["status"] == "failed"
        assert "failed" in registry["skills"]["summarize-test"]


def test_atomic_write(temp_dirs, config):
    """Test that atomic writes work correctly."""
    with patch("skill_creator.BRAIN_DIR", temp_dirs["brain_dir"]), \
         patch("skill_creator.DEPLOY_DIR", temp_dirs["deploy_dir"]), \
         patch("skill_creator.SKILLS_DIR", temp_dirs["skills_dir"]), \
         patch("skill_creator.DRAFTS_DIR", temp_dirs["drafts_dir"]):

        from skill_creator import SkillCreator

        creator = SkillCreator(config)

        # Write a file
        test_file = temp_dirs["deploy_dir"] / "test.txt"
        creator._atomic_write(test_file, "test content")

        assert test_file.exists()
        assert test_file.read_text() == "test content"

        # Verify tmp file was cleaned up
        tmp_file = test_file.with_suffix(".tmp")
        assert not tmp_file.exists()
