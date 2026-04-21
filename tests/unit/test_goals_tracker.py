"""
Unit tests for goals_tracker.

All external access (filesystem) is mocked via tmp_path and patch.
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import goals_tracker as gt
from goals_tracker import GoalManager


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def memories_dir(tmp_path):
    """Temporary memories directory."""
    return tmp_path / "memories"


@pytest.fixture
def config():
    """Minimal config with goals categories."""
    return {
        "goals": {
            "categories": ["personal", "work", "family", "learning", "other"],
            "deadline_horizons": [7, 1],
            "max_context_items": 5,
        }
    }


@pytest.fixture
def manager(memories_dir, config):
    """GoalManager instance with patched MEMORIES_DIR."""
    with patch.object(gt, "MEMORIES_DIR", memories_dir):
        yield GoalManager(memories_dir, config)


# ── Goal Creation Tests ───────────────────────────────────────────────────────
def test_create_goal_writes_file(manager, memories_dir):
    """File is written with type: goal and correct frontmatter."""
    path = manager.create_goal(
        title="Run a 5K",
        category="personal",
        due_date="2026-06-30",
        priority="high",
        tags=["fitness", "health"],
        notes="Training plan needed"
    )

    assert path.exists()
    with open(path) as f:
        content = f.read()

    # Parse frontmatter
    assert "---\n" in content
    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)

    assert fm["type"] == "goal"
    assert fm["category"] == "personal"
    assert fm["source_title"] == "Run a 5K"
    assert fm["due_date"] == "2026-06-30"
    assert fm["status"] == "active"
    assert fm["priority"] == "high"
    assert fm["tags"] == ["fitness", "health"]
    assert fm["notes"] == "Training plan needed"
    assert fm["linked_projects"] == []


def test_create_goal_field_order(manager, memories_dir):
    """type is the first field in frontmatter (sort_keys=False preserved)."""
    path = manager.create_goal(title="Test Goal", category="work")

    with open(path) as f:
        content = f.read()

    # Extract frontmatter YAML text
    fm_text = content.split("---\n")[1].split("\n---")[0]
    lines = fm_text.strip().split("\n")

    # First non-comment line should be "type: goal"
    first_field = lines[0]
    assert first_field == "type: goal"


def test_create_goal_invalid_category(manager):
    """Invalid category raises ValueError with configured list in message."""
    with pytest.raises(ValueError) as exc:
        manager.create_goal(title="Test", category="invalid_category")

    assert "Invalid category" in str(exc.value)
    assert "personal" in str(exc.value)


def test_create_goal_code_category_rejected(manager, config):
    """Category 'code' is rejected even if added to config list."""
    # Add 'code' to config categories
    config["goals"]["categories"].append("code")
    manager_with_code = GoalManager(manager.memories_dir, config)

    with pytest.raises(ValueError) as exc:
        manager_with_code.create_goal(title="Test", category="code")

    assert "reserved for code repositories" in str(exc.value)


def test_create_goal_invalid_due_date(manager):
    """Invalid date format raises ValueError."""
    with pytest.raises(ValueError) as exc:
        manager.create_goal(title="Test", category="work", due_date="2026/04/15")

    assert "Invalid due_date format" in str(exc.value)


def test_create_goal_null_due_date(manager, memories_dir):
    """due_date=None writes file without error, frontmatter has due_date: null."""
    path = manager.create_goal(title="Test", category="personal", due_date=None)

    assert path.exists()
    with open(path) as f:
        content = f.read()

    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)
    assert fm["due_date"] is None


def test_create_goal_stable_id_dedup(manager, memories_dir):
    """Calling twice with same title+timestamp produces same filename, no overwrite."""
    # First call
    path1 = manager.create_goal(title="Duplicate Test", category="work")
    original_content = path1.read_text()

    # Modify content to detect overwrite
    path1.write_text(original_content + "\n<!-- modified -->")

    # Second call with same title (created timestamp will be same due to stable ID)
    # Note: This test relies on the stable ID being generated from title+timestamp.
    # Since we can't control datetime.now(timezone.utc), we'll patch create_goal to reuse
    # the same created timestamp.
    with patch("goals_tracker.datetime") as mock_dt:
        # Extract created timestamp from first file
        fm_text = original_content.split("---\n")[1]
        fm = yaml.safe_load(fm_text)
        created_iso = fm["created"]

        # Mock datetime.now() to return the same timestamp
        mock_dt.now.return_value.isoformat.return_value = created_iso
        path2 = manager.create_goal(title="Duplicate Test", category="work")

    assert path1 == path2
    # Content should still have our modification (no overwrite)
    assert "<!-- modified -->" in path2.read_text()


def test_create_goal_atomic_write(manager, memories_dir):
    """No .tmp file left after write."""
    path = manager.create_goal(title="Atomic Test", category="personal")

    # Check no .tmp file exists
    tmp_path = path.with_suffix(".tmp")
    assert not tmp_path.exists()


# ── Goal Status Update Tests ──────────────────────────────────────────────────
def test_update_goal_status_active_to_completed(manager, memories_dir):
    """status: completed written correctly."""
    path = manager.create_goal(title="Complete Me", category="work")
    manager.update_goal_status(path, "completed")

    with open(path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)

    assert fm["status"] == "completed"


def test_update_goal_status_active_to_abandoned(manager, memories_dir):
    """status: abandoned written correctly."""
    path = manager.create_goal(title="Abandon Me", category="personal")
    manager.update_goal_status(path, "abandoned")

    with open(path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)

    assert fm["status"] == "abandoned"


def test_update_goal_status_invalid_transition(manager, memories_dir):
    """completed → active raises ValueError."""
    path = manager.create_goal(title="Terminal", category="work")
    manager.update_goal_status(path, "completed")

    with pytest.raises(ValueError) as exc:
        manager.update_goal_status(path, "active")

    assert "Invalid status transition" in str(exc.value)


def test_update_goal_status_idempotent(manager, memories_dir):
    """Completing already-completed goal returns without error."""
    path = manager.create_goal(title="Idempotent", category="work")
    manager.update_goal_status(path, "completed")

    # Should not raise
    manager.update_goal_status(path, "completed")

    with open(path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)

    assert fm["status"] == "completed"


# ── Project Creation Tests ────────────────────────────────────────────────────
def test_create_project_code_category_rejected(manager):
    """category: code raises ValueError."""
    with pytest.raises(ValueError) as exc:
        manager.create_project(title="Test Project", category="code")

    assert "reserved for code repositories" in str(exc.value)


def test_create_project_milestone_starts_empty(manager, memories_dir):
    """New project has milestones: []."""
    path = manager.create_project(title="Project", category="work")

    with open(path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)

    assert fm["milestones"] == []


# ── Milestone Tests ───────────────────────────────────────────────────────────
def test_add_milestone(manager, memories_dir):
    """Milestone appended with done: False."""
    path = manager.create_project(title="Milestone Test", category="work")
    manager.add_milestone(path, "First milestone")

    with open(path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)

    assert len(fm["milestones"]) == 1
    assert fm["milestones"][0]["text"] == "First milestone"
    assert fm["milestones"][0]["done"] is False


def test_toggle_milestone(manager, memories_dir):
    """done: False flips to True."""
    path = manager.create_project(title="Toggle Test", category="work")
    manager.add_milestone(path, "Task 1")
    manager.toggle_milestone(path, 1)

    with open(path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm = yaml.safe_load(fm_text)

    assert fm["milestones"][0]["done"] is True


def test_toggle_milestone_invalid_index(manager, memories_dir):
    """Index 5 when 2 milestones raises ValueError."""
    path = manager.create_project(title="Index Test", category="work")
    manager.add_milestone(path, "Task 1")
    manager.add_milestone(path, "Task 2")

    with pytest.raises(ValueError) as exc:
        manager.toggle_milestone(path, 5)

    assert "out of range" in str(exc.value)
    assert "2 milestones" in str(exc.value)


# ── Goal↔Project Linking Tests ────────────────────────────────────────────────
def test_link_goal_to_project(manager, memories_dir):
    """Both files updated; linked_goal set on project, linked_projects has project filename on goal."""
    goal_path = manager.create_goal(title="Goal", category="work")
    project_path = manager.create_project(title="Project", category="work")

    manager.link_goal_to_project(project_path, goal_path)

    # Check project
    with open(project_path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm_project = yaml.safe_load(fm_text)
    assert fm_project["linked_goal"] == goal_path.name

    # Check goal
    with open(goal_path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm_goal = yaml.safe_load(fm_text)
    assert project_path.name in fm_goal["linked_projects"]


def test_link_goal_rollback_on_project_write_failure(manager, memories_dir):
    """Goal write succeeds, project write fails → goal file reverted (no stale link)."""
    goal_path = manager.create_goal(title="Goal", category="work")
    project_path = manager.create_project(title="Project", category="work")

    # Patch _atomic_write to fail on the second call (project write)
    original_atomic_write = manager._atomic_write
    call_count = [0]

    def mock_atomic_write(path, fm, body):
        call_count[0] += 1
        if call_count[0] == 2:  # Second call (project write)
            raise OSError("Simulated write failure")
        return original_atomic_write(path, fm, body)

    with patch.object(manager, "_atomic_write", side_effect=mock_atomic_write):
        with pytest.raises(ValueError) as exc:
            manager.link_goal_to_project(project_path, goal_path)

        assert "rolled back goal" in str(exc.value)

    # Check that goal file was reverted (no project in linked_projects)
    with open(goal_path) as f:
        content = f.read()
    fm_text = content.split("---\n")[1]
    fm_goal = yaml.safe_load(fm_text)
    assert fm_goal["linked_projects"] == []
