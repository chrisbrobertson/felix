"""
Unit tests for goal_project_agent.

All external access (LiteLLM, filesystem, GoalManager) is mocked.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import goal_project_agent as gpa
from goal_project_agent import GoalProjectAgent, _parse_frontmatter, _slugify, _title_similarity
from memory_cache import MemoryCache


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cache(memories_dir):
    """Create a pass-through MemoryCache for testing."""
    return MemoryCache(None, memories_dir, enabled=False)

def make_goal_file(memories_dir, title, status="active", agent=None, due_date=None, created="2026-04-01T00:00:00"):
    """Write a goal file with the given status."""
    slug = _slugify(title)
    fm = {
        "type": "goal",
        "source_title": title,
        "status": status,
        "summary": "Test goal",
        "created": created,
        "tags": ["test"],
        "notes": "",
    }
    if agent is not None:
        fm["agent"] = agent
    if due_date:
        fm["due_date"] = due_date
    path = memories_dir / f"goal-{slug}-abc123.md"
    path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Notes\n")
    return path


def make_project_file(memories_dir, title, status="active", category="work", agent=None):
    """Write a project file with the given status."""
    slug = _slugify(title)
    fm = {
        "type": "project",
        "source_title": title,
        "status": status,
        "category": category,
        "summary": "Test project",
        "created": "2026-04-01T00:00:00",
        "tags": ["test"],
        "notes": "",
    }
    if agent is not None:
        fm["agent"] = agent
    path = memories_dir / f"project-{category}-{slug}-abc123.md"
    path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Notes\n")
    return path


def make_memory_file(memories_dir, filename, type_, title, tags=None, summary="Test summary"):
    """Write a memory file."""
    fm = {
        "type": type_,
        "source_title": title,
        "summary": summary,
        "tags": tags or [],
        "created": "2026-04-15T10:00:00",
    }
    path = memories_dir / filename
    path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Content\n{summary}\n")
    return path


# ── Helper tests ──────────────────────────────────────────────────────────────

def test_title_similarity_exact():
    assert _title_similarity("Q2 Launch", "Q2 Launch") == 1.0


def test_title_similarity_partial():
    # "Q2 launch" vs "Q2 rollout"
    # tokens: {q2, launch} vs {q2, rollout}
    # intersection: {q2}, union: {q2, launch, rollout}
    # similarity = 1/3
    sim = _title_similarity("Q2 launch", "Q2 rollout")
    assert 0.3 < sim < 0.4


def test_title_similarity_no_match():
    assert _title_similarity("Garden shed", "Spanish lessons") < 0.1


# ── Agent tests ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_skips_watcher_role(tmp_path):
    """Watcher role → run_loop returns immediately."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="watcher", cache=_make_cache(memories_dir))
        # Should return immediately without error
        import asyncio
        stop_event = asyncio.Event()
        stop_event.set()
        await agent.run_loop(stop_event)


@pytest.mark.asyncio
async def test_scan_skips_disabled_config(tmp_path):
    """enabled: false → run_loop returns immediately."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": False}}))

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        import asyncio
        stop_event = asyncio.Event()
        stop_event.set()
        await agent.run_loop(stop_event)


@pytest.mark.asyncio
async def test_scan_respects_agent_false_frontmatter(tmp_path):
    """agent: false in frontmatter → item skipped."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    make_goal_file(memories_dir, "Test Goal", status="active", agent=False)

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        items = await agent._select_items()
        assert len(items) == 0


@pytest.mark.asyncio
async def test_scan_skips_completed_goals(tmp_path):
    """status: completed → goal skipped."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    make_goal_file(memories_dir, "Completed Goal", status="completed")

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        items = await agent._select_items()
        assert len(items) == 0


@pytest.mark.asyncio
async def test_related_memories_seed_from_inferred_from(tmp_path):
    """inferred_from field → memory included."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    make_memory_file(memories_dir, "email-test.md", "email_thread", "Test Email")

    # Create goal with inferred_from
    slug = _slugify("Test Goal")
    fm = {
        "type": "goal",
        "source_title": "Test Goal",
        "status": "active",
        "summary": "Test goal",
        "created": "2026-04-01T00:00:00",
        "tags": [],
        "notes": "",
        "inferred_from": ["email-test.md"],
    }
    goal_path = memories_dir / f"goal-{slug}-abc123.md"
    goal_path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Notes\n")

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        related = await agent._find_related_memories(goal_path, fm, None)
        assert len(related) == 1
        assert related[0][0].name == "email-test.md"


@pytest.mark.asyncio
async def test_related_memories_tag_overlap(tmp_path):
    """Tag intersection → memory included."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    make_memory_file(memories_dir, "email-test.md", "email_thread", "Test Email", tags=["launch", "q2"])

    goal_path = make_goal_file(memories_dir, "Q2 Launch")
    fm = _parse_frontmatter(goal_path.read_text())
    fm["tags"] = ["launch", "planning"]

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        related = await agent._find_related_memories(goal_path, fm, None)
        assert len(related) == 1


@pytest.mark.asyncio
async def test_related_memories_title_jaccard_threshold(tmp_path):
    """Title Jaccard >= 0.3 → memory included."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    make_memory_file(memories_dir, "email-test.md", "email_thread", "Q2 Launch Planning")

    goal_path = make_goal_file(memories_dir, "Q2 Launch Execution")
    fm = _parse_frontmatter(goal_path.read_text())

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        related = await agent._find_related_memories(goal_path, fm, None)
        assert len(related) == 1


@pytest.mark.asyncio
async def test_related_memories_recency_filter(tmp_path):
    """mtime > last_checked → memory included, else skipped."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    mem_path = make_memory_file(memories_dir, "email-test.md", "email_thread", "Test Email", tags=["launch"])
    goal_path = make_goal_file(memories_dir, "Launch Goal")
    fm = _parse_frontmatter(goal_path.read_text())
    fm["tags"] = ["launch"]

    # Set last_checked to a future timestamp so the memory (written now) is skipped
    from datetime import datetime, timedelta
    last_checked = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        related = await agent._find_related_memories(goal_path, fm, last_checked)
        # Memory is older than last_checked → should be skipped
        assert len(related) == 0


@pytest.mark.asyncio
async def test_related_memories_max_cap(tmp_path):
    """Cap at max_memories_per_item."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "goal_agent": {"enabled": True, "max_memories_per_item": 5}
    }))

    # Create 10 memories with same tag
    for i in range(10):
        make_memory_file(memories_dir, f"email-{i:02d}.md", "email_thread", f"Email {i}", tags=["launch"])

    goal_path = make_goal_file(memories_dir, "Launch Goal")
    fm = _parse_frontmatter(goal_path.read_text())
    fm["tags"] = ["launch"]

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        related = await agent._find_related_memories(goal_path, fm, None)
        # Should be capped at 5
        assert len(related) == 5


@pytest.mark.asyncio
async def test_llm_dedup_by_report_hash(tmp_path):
    """Same report hash → skip."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))
    state_file = tmp_path / "goal-agent-state.json"

    goal_path = make_goal_file(memories_dir, "Test Goal")
    fm = _parse_frontmatter(goal_path.read_text())

    # Pre-populate state with a report hash
    import hashlib
    report_text = "Test report"
    report_hash = hashlib.sha1(report_text.encode()).hexdigest()[:12]
    state = {
        "goals": {
            goal_path.name: {
                "last_checked": "2026-04-15T10:00:00",
                "last_report_hash": report_hash,
            }
        }
    }
    state_file.write_text(json.dumps(state))

    # Mock LLM to return same report
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "has_update": True,
        "urgency": "low",
        "report": report_text,
        "actions": [],
        "evidence": [],
    })

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        state_entry = state["goals"][goal_path.name]
        result = await agent._generate_report(goal_path, fm, [], state_entry)
        # Should return None because hash matches
        assert result is None


@pytest.mark.asyncio
async def test_llm_confidence_filter(tmp_path):
    """Actions with confidence < min_confidence → filtered out."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "goal_agent": {"enabled": True, "min_confidence": 0.7}
    }))

    goal_path = make_goal_file(memories_dir, "Test Goal")
    fm = _parse_frontmatter(goal_path.read_text())

    # Mock LLM to return action with low confidence
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "has_update": True,
        "urgency": "low",
        "report": "New report",
        "actions": [
            {"action_type": "add_note", "target": goal_path.name, "args": {"text": "Test"}, "confidence": 0.5, "rationale": "Low conf"}
        ],
        "evidence": ["email-test.md"],
    })

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        result = await agent._generate_report(goal_path, fm, [], {})
        # Action should be filtered out (0.5 < 0.7)
        assert len(result["actions"]) == 0


@pytest.mark.asyncio
async def test_staleness_emits_low_urgency_report(tmp_path):
    """No related memories + age >= stale_threshold → synthesized report."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "goal_agent": {"enabled": True, "stale_threshold_days": 7}
    }))
    state_file = tmp_path / "goal-agent-state.json"
    state_file.write_text(json.dumps({"goals": {}, "projects": {}}))

    # Create old goal (20 days ago)
    old_created = "2026-03-27T00:00:00"
    goal_path = make_goal_file(memories_dir, "Stale Goal", created=old_created)
    fm = _parse_frontmatter(goal_path.read_text())

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        state = agent._load_state()
        await agent._process_item(goal_path, fm, state)

        # Check state was updated with a report
        assert goal_path.name in state["goals"]
        last_report = state["goals"][goal_path.name].get("last_report", "")
        assert "no new related activity" in last_report


def test_action_file_written_atomically(tmp_path):
    """Action file uses tmp + rename pattern."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    goal_path = make_goal_file(memories_dir, "Test Goal")
    fm = _parse_frontmatter(goal_path.read_text())

    action_dict = {
        "action_type": "add_note",
        "target": goal_path.name,
        "args": {"text": "Test note"},
        "confidence": 0.85,
        "rationale": "Test rationale",
        "evidence": ["email-test.md"],
    }

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        agent._write_action(goal_path, fm, action_dict)

        # Check action file was written
        action_files = list(memories_dir.glob("action-*.md"))
        assert len(action_files) == 1

        # Check no .tmp file left behind
        tmp_files = list(memories_dir.glob("*.tmp"))
        assert len(tmp_files) == 0


@pytest.mark.asyncio
async def test_urgent_ping_rate_limited(tmp_path):
    """Urgent ping sent only once per cooldown period."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "goal_agent": {"enabled": True, "urgent_cooldown_hours": 24}
    }))
    state_file = tmp_path / "goal-agent-state.json"

    goal_path = make_goal_file(memories_dir, "Test Goal")
    fm = _parse_frontmatter(goal_path.read_text())
    make_memory_file(memories_dir, "email-test.md", "email_thread", "Test Email", tags=["test"])

    # Pre-populate state with recent urgent ping
    from datetime import datetime
    recent_ping = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    state = {
        "goals": {
            goal_path.name: {
                "last_checked": "2026-04-15T00:00:00",
                "last_urgent_ping": recent_ping,
            }
        },
        "projects": {}
    }
    state_file.write_text(json.dumps(state))

    # Mock LLM to return high urgency
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({
        "has_update": True,
        "urgency": "high",
        "report": "Urgent update",
        "actions": [],
        "evidence": ["email-test.md"],
    })

    ping_called = False
    async def mock_send_message(msg):
        nonlocal ping_called
        ping_called = True

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        agent.notification_callback = mock_send_message
        state = agent._load_state()
        await agent._process_item(goal_path, fm, state)

        # Ping should not be sent (within cooldown)
        assert not ping_called


def test_state_file_persisted_and_loaded(tmp_path):
    """State is written atomically and can be loaded."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))
    state_file = tmp_path / "goal-agent-state.json"

    state = {
        "goals": {
            "goal-test-abc123.md": {
                "last_checked": "2026-04-16T10:00:00",
                "last_report_hash": "abc123",
            }
        },
        "projects": {}
    }

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        agent._save_state(state)

        # Load back
        loaded = agent._load_state()
        assert loaded["goals"]["goal-test-abc123.md"]["last_checked"] == "2026-04-16T10:00:00"

        # Check no .tmp file left behind
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


@pytest.mark.asyncio
async def test_archive_superseded_action(tmp_path):
    """Precondition fail → action marked superseded."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    goal_path = make_goal_file(memories_dir, "Test Goal")

    # Create action with target that doesn't exist
    fm = {
        "type": "agent_action",
        "action_id": "abc123",
        "action_type": "add_milestone",
        "status": "pending",
        "target": "nonexistent.md",
        "args": {"text": "Test"},
        "confidence": 0.85,
        "rationale": "Test",
        "evidence": [],
        "proposed_at": "2026-04-16T10:00:00",
        "source_goal": goal_path.name,
    }
    action_path = memories_dir / "action-test-goal-abc123.md"
    action_path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Rationale\nTest\n")

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        await agent._check_superseded_actions()

        # Action should not be superseded yet (target check is done on execute, not on source_goal)
        # Let's instead test milestone already exists case

    # Reset — test milestone already exists
    project_path = make_project_file(memories_dir, "Test Project")
    project_text = project_path.read_text()
    project_text += "\n\nMilestone: Test milestone already here\n"
    project_path.write_text(project_text)

    fm["action_type"] = "add_milestone"
    fm["target"] = project_path.name
    fm["args"] = {"text": "Test milestone already here"}
    action_path.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Rationale\nTest\n")

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        await agent._check_superseded_actions()

        # Load action and check status
        fresh_fm = _parse_frontmatter(action_path.read_text())
        assert fresh_fm.get("status") == "superseded"
        assert "already exists" in fresh_fm.get("superseded_reason", "").lower()


# ── generate_change_digest tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_change_digest_empty_when_no_items(tmp_path):
    """No active goals/projects → empty digest."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        results = await agent.generate_change_digest(hours=24)
        assert results == []


@pytest.mark.asyncio
async def test_generate_change_digest_excludes_stale_memories(tmp_path):
    """Memories older than the cutoff are not included."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    # Create an active project with a matching tag
    make_project_file(memories_dir, "Alpha Project", status="active", category="work")
    # Create a memory with the matching tag but set its mtime to > 24h ago
    mem_path = make_memory_file(
        memories_dir, "email-alpha.md", "email_thread", "Alpha email", tags=["test"]
    )
    old_time = __import__("time").time() - 48 * 3600
    import os
    os.utime(str(mem_path), (old_time, old_time))

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        results = await agent.generate_change_digest(hours=24)
        # Memory is outside window → no results
        assert results == []


@pytest.mark.asyncio
async def test_generate_change_digest_returns_entry_for_recent_activity(tmp_path):
    """A project with a recently-modified related memory produces one digest entry."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    make_project_file(memories_dir, "Beta Project", status="active", category="work")
    # Recent memory with matching tag
    make_memory_file(
        memories_dir, "email-beta.md", "email_thread", "Beta email", tags=["test"]
    )

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "Beta project had an email discussion."

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        results = await agent.generate_change_digest(hours=24)

    assert len(results) == 1
    assert results[0]["title"] == "Beta Project"
    assert results[0]["type"] == "project"
    assert results[0]["memory_count"] == 1
    assert "Beta project" in results[0]["summary"]


@pytest.mark.asyncio
async def test_generate_change_digest_fallback_on_llm_error(tmp_path):
    """LLM failure → fallback summary listing memory titles."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({"goal_agent": {"enabled": True}}))

    make_project_file(memories_dir, "Gamma Project", status="active", category="work")
    make_memory_file(
        memories_dir, "email-gamma.md", "email_thread", "Gamma email", tags=["test"]
    )

    with patch.object(gpa, "CONFIG_PATH", config_file), \
         patch.object(gpa, "DEPLOY_DIR", tmp_path), \
         patch.object(gpa, "MEMORIES_DIR", memories_dir), \
         patch("litellm.acompletion", new=AsyncMock(side_effect=Exception("LLM down"))):
        agent = GoalProjectAgent(role="full", cache=_make_cache(memories_dir))
        results = await agent.generate_change_digest(hours=24)

    assert len(results) == 1
    assert results[0]["memory_count"] == 1
    # Fallback summary mentions the memory title or count
    assert "1" in results[0]["summary"] or "Gamma" in results[0]["summary"]
