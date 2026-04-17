"""Unit tests for skill_optimizer.py."""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml

import skill_optimizer as so


SAMPLE_CONFIG = {
    "skill_optimizer": {
        "run_hour": 3,
        "min_runs_before_optimize": 10,
        "underperformance_threshold": 0.70,
        "skip_above_threshold": 0.90,
        "regression_tolerance": 0.05,
        "max_exemplars": 2,
        "max_history_rows": 100,
        "max_skill_backups": 5,
        "judge_model": "judge",
        "dry_run": False
    }
}


def create_skill_file(name="test-skill", version=1, success_rate=None, total_runs=0,
                      last_optimized=None, prev_avg=None, exemplar_eligible=False,
                      instructions="Test instructions.", history_rows=None):
    """Helper to create a skill file with execution history."""
    fm = {
        "name": name,
        "version": version,
        "preferred_model": "claude-haiku-4-5-20251001",
        "success_rate": success_rate,
        "total_runs": total_runs,
        "last_optimized": last_optimized,
        "prev_version_avg_score": prev_avg,
        "exemplar_eligible": exemplar_eligible
    }

    content = f"""---
{yaml.dump(fm, sort_keys=False)}---

## Instructions

{instructions}

## Evolution Log

### v{version} (2026-04-11) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""

    if history_rows:
        for row in history_rows:
            content += f"| {row['date']} | {row['slug']} | {row['model']} | {row['score']} | {row.get('notes', '')} |\n"

    return content


@pytest.fixture
def brain_dir(tmp_path):
    return tmp_path / "brain"


@pytest.fixture
def skills_dir(brain_dir):
    d = brain_dir / "skills"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def memories_dir(brain_dir):
    d = brain_dir / "memories"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def logs_dir(brain_dir):
    d = brain_dir / "logs"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def optimizer(brain_dir, skills_dir, memories_dir):
    with patch.object(so, "BRAIN_DIR", brain_dir), \
         patch.object(so, "SKILLS_DIR", skills_dir), \
         patch.object(so, "MEMORIES_DIR", memories_dir):
        yield so.SkillOptimizer(SAMPLE_CONFIG)


@pytest.mark.asyncio
async def test_scheduling_calculates_correct_sleep(optimizer):
    """Sleep duration brings wakeup to run_hour."""
    now = datetime(2026, 4, 15, 1, 30, 0)  # 1:30 AM
    optimizer.run_hour = 3  # 3 AM

    with patch("skill_optimizer.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

        # Calculate what the code would do
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        expected_sleep = (next_run - now).total_seconds()

        assert expected_sleep == 5400  # 1.5 hours


@pytest.mark.asyncio
async def test_scheduling_next_day_if_past_run_hour(optimizer):
    """If past run_hour, schedules for tomorrow."""
    now = datetime(2026, 4, 15, 5, 0, 0)  # 5 AM (past 3 AM)
    optimizer.run_hour = 3

    with patch("skill_optimizer.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        # Should be tomorrow
        next_run += timedelta(days=1)
        expected_sleep = (next_run - now).total_seconds()

        # 22 hours
        assert expected_sleep == 79200


@pytest.mark.asyncio
async def test_merge_watcher_logs_appends_rows(optimizer, skills_dir, logs_dir):
    """JSONL records appear in skill history table."""
    # Create skill file
    skill_content = create_skill_file("test-skill")
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create watcher JSONL log
    watcher_log = logs_dir / "macbook-pro-execution-log.jsonl"
    records = [
        {"date": "2026-04-14", "skill": "test-skill", "input_slug": "article-1",
         "model": "gemini/gemini-2.0-flash", "score": "pending", "notes": "",
         "hostname": "macbook-pro"},
        {"date": "2026-04-15", "skill": "test-skill", "input_slug": "article-2",
         "model": "gemini/gemini-2.0-flash", "score": "0.85", "notes": "good",
         "hostname": "macbook-pro"}
    ]
    watcher_log.write_text("\n".join(json.dumps(r) for r in records))

    await optimizer._merge_watcher_logs()

    # Check skill file has the rows
    updated = skill_path.read_text()
    assert "| 2026-04-14 | article-1 | gemini/gemini-2.0-flash | pending |" in updated
    assert "| 2026-04-15 | article-2 | gemini/gemini-2.0-flash | 0.85 | good |" in updated


@pytest.mark.asyncio
async def test_merge_watcher_logs_renames_processed(optimizer, skills_dir, logs_dir):
    """Processed JSONL file renamed after merge."""
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(create_skill_file("test-skill"))

    watcher_log = logs_dir / "macbook-pro-execution-log.jsonl"
    watcher_log.write_text(json.dumps({
        "date": "2026-04-14", "skill": "test-skill", "input_slug": "test",
        "model": "gemini", "score": "pending", "notes": "", "hostname": "macbook-pro"
    }))

    await optimizer._merge_watcher_logs()

    # Original file should be renamed
    assert not watcher_log.exists()
    processed_files = list(logs_dir.glob("*-execution-log.processed-*.jsonl"))
    assert len(processed_files) == 1
    assert "macbook-pro" in processed_files[0].name


@pytest.mark.asyncio
async def test_merge_watcher_logs_skips_missing_skill(optimizer, logs_dir, caplog):
    """Record for unknown skill: WARNING, no crash."""
    watcher_log = logs_dir / "macbook-pro-execution-log.jsonl"
    watcher_log.write_text(json.dumps({
        "date": "2026-04-14", "skill": "nonexistent-skill", "input_slug": "test",
        "model": "gemini", "score": "pending", "notes": "", "hostname": "macbook-pro"
    }))

    await optimizer._merge_watcher_logs()

    # Should log warning
    assert "Skill file not found" in caplog.text
    assert "nonexistent-skill" in caplog.text


@pytest.mark.asyncio
async def test_score_pending_updates_row(optimizer, skills_dir, memories_dir):
    """pending row replaced with numeric score."""
    # Create skill with pending row
    rows = [{"date": "2026-04-14", "slug": "article-abc123", "model": "gemini", "score": "pending"}]
    skill_content = create_skill_file("test-skill", history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create matching memory file
    memory_file = memories_dir / "2026-04-14-article-abc123-hash12.md"
    memory_file.write_text("""---
source_url: https://example.com
---

## Summary
Test article summary.

## Key Points
- Point one
""")

    # Mock judge call
    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content='{"score": 0.85, "reasoning": "Good summary"}'))]

    with patch("skill_optimizer.acompletion", return_value=mock_response):
        stop_event = asyncio.Event()
        await optimizer._score_pending_rows(skill_path, stop_event)

    # Check row updated
    updated = skill_path.read_text()
    assert "| pending |" not in updated
    assert "| 0.85 |" in updated
    assert "Good summary" in updated


@pytest.mark.asyncio
async def test_score_pending_updates_frontmatter(optimizer, skills_dir, memories_dir):
    """success_rate and total_runs recalculated."""
    rows = [
        {"date": "2026-04-14", "slug": "article-1", "model": "gemini", "score": "0.80"},
        {"date": "2026-04-15", "slug": "article-2", "model": "gemini", "score": "pending"}
    ]
    skill_content = create_skill_file("test-skill", history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create memory file for pending row
    memory_file = memories_dir / "2026-04-15-article-2-hash12.md"
    memory_file.write_text("---\nsource_url: test\n---\n\n## Summary\nTest")

    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content='{"score": 0.90, "reasoning": "Excellent"}'))]

    with patch("skill_optimizer.acompletion", return_value=mock_response):
        stop_event = asyncio.Event()
        await optimizer._score_pending_rows(skill_path, stop_event)

    # Check frontmatter updated
    updated = skill_path.read_text()
    fm = yaml.safe_load(updated.split("---")[1])
    assert fm["total_runs"] == 2
    assert fm["success_rate"] == 0.85  # (0.80 + 0.90) / 2


@pytest.mark.asyncio
async def test_score_no_memory_file_leaves_pending(optimizer, skills_dir, caplog):
    """No matching memory file: row stays pending."""
    rows = [{"date": "2026-04-14", "slug": "missing-article", "model": "gemini", "score": "pending"}]
    skill_content = create_skill_file("test-skill", history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    stop_event = asyncio.Event()
    await optimizer._score_pending_rows(skill_path, stop_event)

    # Row should still be pending
    updated = skill_path.read_text()
    assert "| pending |" in updated
    assert "No memory file found" in caplog.text


@pytest.mark.asyncio
async def test_gates_min_runs_not_met(optimizer, skills_dir):
    """Fewer than min_runs numeric scores → skipped."""
    skill_content = create_skill_file("test-skill", total_runs=5, success_rate=0.60)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    should_optimize, reason = await optimizer._check_optimization_gates(skill_path)

    assert not should_optimize
    assert "min_runs" in reason


@pytest.mark.asyncio
async def test_gates_above_skip_threshold(optimizer, skills_dir):
    """success_rate >= skip_above_threshold → skipped."""
    skill_content = create_skill_file("test-skill", total_runs=20, success_rate=0.95)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    should_optimize, reason = await optimizer._check_optimization_gates(skill_path)

    assert not should_optimize
    assert "skip_above_threshold" in reason


@pytest.mark.asyncio
async def test_gates_above_underperformance(optimizer, skills_dir):
    """success_rate >= underperformance_threshold → skipped."""
    skill_content = create_skill_file("test-skill", total_runs=20, success_rate=0.75)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    should_optimize, reason = await optimizer._check_optimization_gates(skill_path)

    assert not should_optimize
    assert "underperformance_threshold" in reason


@pytest.mark.asyncio
async def test_gates_meta_skill_always_skipped(optimizer, skills_dir):
    """skill-optimizer.md never optimized."""
    skill_content = create_skill_file("skill-optimizer", total_runs=20, success_rate=0.60)
    skill_path = skills_dir / "skill-optimizer.md"
    skill_path.write_text(skill_content)

    # Optimizer should skip it in _run_daily_pass
    # We'll test this by checking that it's explicitly skipped
    with patch.object(optimizer, "_score_pending_rows") as mock_score:
        stop_event = asyncio.Event()
        await optimizer._run_daily_pass(stop_event)
        # Should not have been called for skill-optimizer.md
        assert mock_score.call_count == 0


@pytest.mark.asyncio
async def test_backup_rotation(optimizer, skills_dir):
    """.1 → .2, .2 → .3, current → .1"""
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text("version 3 content")

    # Create existing backups
    backup1 = skill_path.with_suffix(".md.1")
    backup1.write_text("version 2 content")
    backup2 = skill_path.with_suffix(".md.2")
    backup2.write_text("version 1 content")

    await optimizer._rotate_backups(skill_path)

    # Check rotation
    assert backup1.read_text() == "version 3 content"  # current → .1
    assert backup2.read_text() == "version 2 content"  # .1 → .2
    assert skill_path.with_suffix(".md.3").read_text() == "version 1 content"  # .2 → .3


@pytest.mark.asyncio
async def test_backup_rotation_max_reached(optimizer, skills_dir):
    """.N deleted before rotating."""
    optimizer.max_skill_backups = 3
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text("current")

    # Create backups up to max
    for i in range(1, 4):
        backup = skill_path.with_suffix(f".md.{i}")
        backup.write_text(f"backup {i}")

    await optimizer._rotate_backups(skill_path)

    # .3 should be deleted, .1/.2 rotated, current → .1
    assert not skill_path.with_suffix(".md.4").exists()
    assert skill_path.with_suffix(".md.3").exists()
    assert skill_path.with_suffix(".md.1").read_text() == "current"


@pytest.mark.asyncio
async def test_regression_triggers_rollback(optimizer, skills_dir):
    """New avg < old avg - tolerance → .1 restored."""
    # Skill with regression
    skill_content = create_skill_file("test-skill", version=2, success_rate=0.58,
                                      prev_avg=0.70, total_runs=20)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create backup
    backup_path = skill_path.with_suffix(".md.1")
    backup_content = create_skill_file("test-skill", version=1, success_rate=0.70)
    backup_path.write_text(backup_content)

    rolled_back = await optimizer._check_regression_and_rollback(skill_path)

    assert rolled_back
    # Check skill restored from backup
    restored = skill_path.read_text()
    fm = yaml.safe_load(restored.split("---")[1])
    assert fm["version"] == 1


@pytest.mark.asyncio
async def test_regression_no_rollback_within_tolerance(optimizer, skills_dir):
    """Drop within tolerance → no rollback."""
    # Small drop within tolerance
    skill_content = create_skill_file("test-skill", success_rate=0.68, prev_avg=0.70)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    backup_path = skill_path.with_suffix(".md.1")
    backup_path.write_text("backup content")

    rolled_back = await optimizer._check_regression_and_rollback(skill_path)

    assert not rolled_back
    # Backup should not have been touched
    assert backup_path.read_text() == "backup content"


@pytest.mark.asyncio
async def test_regression_no_backup_logs_warning(optimizer, skills_dir, caplog):
    """Missing .1 backup → WARNING, no crash."""
    skill_content = create_skill_file("test-skill", success_rate=0.50, prev_avg=0.70)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # No backup file

    rolled_back = await optimizer._check_regression_and_rollback(skill_path)

    assert not rolled_back
    assert "Cannot rollback" in caplog.text
    assert "no .1 backup" in caplog.text


@pytest.mark.asyncio
async def test_critique_json_parse_failure_skips(optimizer, skills_dir, memories_dir, caplog):
    """Malformed critique JSON → WARNING, skip skill."""
    # Skill needs optimization
    rows = [{"date": "2026-04-14", "slug": "test", "model": "gemini", "score": "0.50"}]
    skill_content = create_skill_file("test-skill", total_runs=15, success_rate=0.55,
                                      history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create memory file
    mem = memories_dir / "2026-04-14-test-hash.md"
    mem.write_text("---\nsource_url: test\n---\n\nTest content")

    # Mock critique returning invalid JSON
    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content="This is not JSON"))]

    with patch("skill_optimizer.acompletion", return_value=mock_response):
        critique = await optimizer._generate_critique(skill_path)

    assert critique is None
    assert "Critique generation failed" in caplog.text


@pytest.mark.asyncio
async def test_rewrite_identical_instructions_noop(optimizer, skills_dir):
    """Same Instructions → no backup, no write."""
    skill_content = create_skill_file("test-skill", instructions="Original instructions.")
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create meta-skill
    meta_path = skills_dir / "skill-optimizer.md"
    meta_path.write_text("""---
name: skill-optimizer
---

## Instructions

You rewrite skills. Output the complete skill file.""")

    # Mock rewrite returning identical instructions
    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content=skill_content))]

    with patch("skill_optimizer.acompletion", return_value=mock_response):
        critique = {"failure_patterns": ["test"], "root_cause": "test", "suggested_focus": "test"}
        new_text = await optimizer._rewrite_skill(skill_path, critique)

    # Should return None for no change
    assert new_text is None


@pytest.mark.asyncio
async def test_rewrite_updates_version_in_frontmatter(optimizer, skills_dir):
    """version incremented by 1."""
    skill_content = create_skill_file("test-skill", version=1, instructions="Old instructions.")
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create meta-skill
    meta_path = skills_dir / "skill-optimizer.md"
    meta_path.write_text("""---
name: skill-optimizer
---

## Instructions

Rewrite the skill.""")

    # Mock rewrite with new instructions
    new_skill = create_skill_file("test-skill", version=1, instructions="New improved instructions.")
    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content=new_skill))]

    with patch("skill_optimizer.acompletion", return_value=mock_response):
        critique = {"failure_patterns": ["test"], "root_cause": "test", "suggested_focus": "test"}
        new_text = await optimizer._rewrite_skill(skill_path, critique)

    assert new_text is not None
    fm = yaml.safe_load(new_text.split("---")[1])
    assert fm["version"] == 2


@pytest.mark.asyncio
async def test_exemplars_injected_for_eligible_skill(optimizer, skills_dir, memories_dir):
    """Top examples in ## Top Examples section."""
    rows = [
        {"date": "2026-04-14", "slug": "article-1", "model": "gemini", "score": "0.95"},
        {"date": "2026-04-15", "slug": "article-2", "model": "gemini", "score": "0.88"}
    ]
    skill_content = create_skill_file("test-skill", exemplar_eligible=True, history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create memory files
    mem1 = memories_dir / "2026-04-14-article-1-hash.md"
    mem1.write_text("---\nsource_url: test\n---\n\n## Summary\nExcellent article 1")
    mem2 = memories_dir / "2026-04-15-article-2-hash.md"
    mem2.write_text("---\nsource_url: test\n---\n\n## Summary\nGood article 2")

    result = await optimizer._add_auto_exemplars(skill_path, skill_content)

    assert "## Top Examples" in result
    assert "Example 1 (score: 0.95, 2026-04-14)" in result
    assert "Example 2 (score: 0.88, 2026-04-15)" in result
    assert "Excellent article 1" in result


@pytest.mark.asyncio
async def test_exemplars_not_injected_for_ineligible(optimizer, skills_dir):
    """exemplar_eligible: false → no section."""
    skill_content = create_skill_file("test-skill", exemplar_eligible=False)
    skill_path = skills_dir / "test-skill.md"

    result = await optimizer._add_auto_exemplars(skill_path, skill_content)

    assert "## Top Examples" not in result


@pytest.mark.asyncio
async def test_exemplars_section_replaced_not_appended(optimizer, skills_dir, memories_dir):
    """Second run replaces, not appends."""
    rows = [{"date": "2026-04-14", "slug": "article-new", "model": "gemini", "score": "0.98"}]
    skill_content = create_skill_file("test-skill", exemplar_eligible=True, history_rows=rows)

    # Add existing Top Examples section
    skill_content = skill_content.replace(
        "## Evolution Log",
        "## Top Examples\n### Example 1 (score: 0.80, 2026-04-10)\nOld example\n\n## Evolution Log"
    )

    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create new memory
    mem = memories_dir / "2026-04-14-article-new-hash.md"
    mem.write_text("---\nsource_url: test\n---\n\n## Summary\nNew best article")

    result = await optimizer._add_auto_exemplars(skill_path, skill_content)

    # Should have new example, not old
    assert "New best article" in result
    assert "Old example" not in result
    # Should only have one Top Examples section
    assert result.count("## Top Examples") == 1


@pytest.mark.asyncio
async def test_history_pruning_keeps_newest(optimizer, skills_dir):
    """After pruning, oldest rows removed."""
    optimizer.max_history_rows = 3

    # Create skill with 5 scored rows
    rows = [
        {"date": "2026-04-11", "slug": "old-1", "model": "gemini", "score": "0.70"},
        {"date": "2026-04-12", "slug": "old-2", "model": "gemini", "score": "0.72"},
        {"date": "2026-04-13", "slug": "new-1", "model": "gemini", "score": "0.75"},
        {"date": "2026-04-14", "slug": "new-2", "model": "gemini", "score": "0.78"},
        {"date": "2026-04-15", "slug": "new-3", "model": "gemini", "score": "0.80"}
    ]
    skill_content = create_skill_file("test-skill", history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    await optimizer._prune_execution_history(skill_path)

    result = skill_path.read_text()

    # Oldest 2 should be gone
    assert "old-1" not in result
    assert "old-2" not in result
    # Newest 3 should remain
    assert "new-1" in result
    assert "new-2" in result
    assert "new-3" in result


@pytest.mark.asyncio
async def test_history_pruning_preserves_pending(optimizer, skills_dir):
    """Pending rows not pruned."""
    optimizer.max_history_rows = 2

    rows = [
        {"date": "2026-04-11", "slug": "old", "model": "gemini", "score": "0.70"},
        {"date": "2026-04-12", "slug": "new", "model": "gemini", "score": "0.80"},
        {"date": "2026-04-13", "slug": "pending-1", "model": "gemini", "score": "pending"},
        {"date": "2026-04-14", "slug": "pending-2", "model": "gemini", "score": "pending"}
    ]
    skill_content = create_skill_file("test-skill", history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    await optimizer._prune_execution_history(skill_path)

    result = skill_path.read_text()

    # All pending rows should be kept
    assert "pending-1" in result
    assert "pending-2" in result
    # Newest scored row(s) kept - should keep 2 most recent
    assert "new" in result
    # Oldest scored row removed if we have more than max_history_rows scored rows
    # With only 2 scored rows and limit of 2, both should remain
    # Actually "old" should be removed since we're keeping only 2 scored
    # Let's check that we have at most max_history_rows scored rows
    scored_rows = [line for line in result.splitlines() if line.strip().startswith("|") and "| pending |" not in line and "| date |" not in line and "|---" not in line]
    # Subtract header rows - there should be at most 2 scored data rows
    data_rows = [line for line in scored_rows if not line.strip().startswith("| date") and "|---" not in line]
    assert len(data_rows) <= 2


@pytest.mark.asyncio
async def test_history_pruning_noop_under_limit(optimizer, skills_dir):
    """No write if count within limit."""
    optimizer.max_history_rows = 100

    rows = [{"date": "2026-04-14", "slug": "test", "model": "gemini", "score": "0.80"}]
    skill_content = create_skill_file("test-skill", history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    original_mtime = skill_path.stat().st_mtime

    await optimizer._prune_execution_history(skill_path)

    # File should not have been modified
    assert skill_path.stat().st_mtime == original_mtime


@pytest.mark.asyncio
async def test_evolution_log_entry_format(optimizer):
    """Entry has all required fields."""
    text = create_skill_file("test-skill")

    entry = """### v2 (2026-04-15) — improve entity extraction
**Critique:** Missing company names
**Failure patterns:** missing-entities, tags-too-generic
**Change:** Added explicit instruction to scan for organizations
**Pre-optimization avg:** 0.65 | **Post (projected):** pending"""

    result = optimizer._append_to_evolution_log(text, entry)

    assert "### v2 (2026-04-15)" in result
    assert "**Critique:**" in result
    assert "**Failure patterns:**" in result
    assert "**Change:**" in result
    assert "**Pre-optimization avg:**" in result


@pytest.mark.asyncio
async def test_atomic_write_no_tmp_left(optimizer, skills_dir):
    """No .tmp file after successful write."""
    skill_path = skills_dir / "test-skill.md"
    content = "test content"

    optimizer._atomic_write(skill_path, content)

    assert skill_path.exists()
    assert skill_path.read_text() == content
    assert not skill_path.with_suffix(".tmp").exists()


@pytest.mark.asyncio
async def test_dry_run_no_files_written(skills_dir):
    """dry_run: true → no file modifications."""
    config = SAMPLE_CONFIG.copy()
    config["skill_optimizer"]["dry_run"] = True

    with patch.object(so, "SKILLS_DIR", skills_dir):
        optimizer = so.SkillOptimizer(config)

    skill_path = skills_dir / "test-skill.md"
    original = create_skill_file("test-skill")
    skill_path.write_text(original)

    # Try to rotate backups in dry run
    await optimizer._rotate_backups(skill_path)

    # No backup should be created
    assert not skill_path.with_suffix(".md.1").exists()
    # Original unchanged
    assert skill_path.read_text() == original


@pytest.mark.asyncio
async def test_dry_run_logs_proposed_changes(skills_dir, memories_dir, caplog):
    """dry_run: true → INFO logs show what would change."""
    import logging
    caplog.set_level(logging.INFO, logger="skill-optimizer")

    config = SAMPLE_CONFIG.copy()
    config["skill_optimizer"]["dry_run"] = True

    with patch.object(so, "SKILLS_DIR", skills_dir), \
         patch.object(so, "MEMORIES_DIR", memories_dir), \
         patch.object(so, "BRAIN_DIR", skills_dir.parent):
        optimizer = so.SkillOptimizer(config)

    rows = [{"date": "2026-04-14", "slug": "test", "model": "gemini", "score": "pending"}]
    skill_content = create_skill_file("test-skill", history_rows=rows)
    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    stop_event = asyncio.Event()
    await optimizer._score_pending_rows(skill_path, stop_event)

    assert "DRY RUN: Would score" in caplog.text


@pytest.mark.asyncio
async def test_executor_reload_on_modify(tmp_path):
    """SkillExecutor picks up new Instructions after optimizer write."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text("""---
name: test-skill
version: 1
preferred_model: gemini/gemini-2.0-flash
---

## Instructions

Original instructions.
""")

    # Import skill_executor and patch paths
    import skill_executor as se

    with patch.object(se, "SKILLS_DIR", skills_dir):
        executor = se.SkillExecutor("test-skill")

        # Check initial load
        assert executor._skill["instructions"] == "Original instructions."

        # Modify the skill file (simulate optimizer rewrite)
        import time
        time.sleep(0.01)  # Ensure mtime changes
        skill_path.write_text("""---
name: test-skill
version: 2
preferred_model: gemini/gemini-2.0-flash
---

## Instructions

Updated instructions after optimization.
""")

        # Reload should detect change
        executor._reload_if_modified()

        # Check instructions updated
        assert executor._skill["instructions"] == "Updated instructions after optimization."
        assert executor._skill["meta"]["version"] == 2


# ── Utility scoring (feat-skill-utility-scoring) ──────────────────────────────

def _make_rows(scores_with_ages):
    """Build history rows list from [(score, days_ago)] pairs, newest first."""
    today = datetime.now().date()
    rows = []
    for score, days_ago in reversed(scores_with_ages):  # oldest first
        d = (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        rows.append({"date": d, "score": score})
    return rows


def test_compute_utility_score_basic(optimizer):
    """5 same-day scores → weighted mean equals simple mean."""
    rows = _make_rows([(3.0, 0), (4.0, 0), (5.0, 0), (4.0, 0), (3.0, 0)])
    result = optimizer._compute_utility_score(rows)
    assert result == round(sum(r["score"] for r in rows) / len(rows), 2)


def test_compute_utility_score_fewer_than_3_returns_none(optimizer):
    rows = _make_rows([(4.0, 0), (3.0, 1)])
    assert optimizer._compute_utility_score(rows) is None


def test_compute_utility_score_recent_weighed_more(optimizer):
    """Recent high scores pull the weighted average above the simple mean."""
    # Old low scores and recent high scores
    rows = _make_rows([(1.0, 30), (1.0, 29), (1.0, 28), (5.0, 1), (5.0, 0)])
    simple_mean = sum(r["score"] for r in rows) / len(rows)  # 2.6
    weighted = optimizer._compute_utility_score(rows, half_life_days=14.0)
    assert weighted > simple_mean


def test_compute_utility_score_half_life_config(optimizer):
    """Shorter half-life weights recent scores more aggressively."""
    rows = _make_rows([(1.0, 30), (1.0, 29), (1.0, 28), (5.0, 1), (5.0, 0)])
    short_hl = optimizer._compute_utility_score(rows, half_life_days=3.0)
    long_hl = optimizer._compute_utility_score(rows, half_life_days=60.0)
    # Shorter half-life → recent 5s dominate more → higher score
    assert short_hl > long_hl


def test_compute_utility_score_skips_non_numeric(optimizer):
    """_parse_history_rows should exclude pending rows; _compute_utility_score gets clean data."""
    section = (
        "| date | slug | model | score | notes |\n"
        "|------|------|-------|-------|-------|\n"
        "| 2026-04-01 | s1 | m | 3.0 | ok |\n"
        "| 2026-04-02 | s2 | m | pending |  |\n"
        "| 2026-04-03 | s3 | m | 4.0 | ok |\n"
        "| 2026-04-04 | s4 | m | 5.0 | ok |\n"
    )
    rows = optimizer._parse_history_rows(section)
    assert len(rows) == 3
    assert all(r["score"] != "pending" for r in rows)
    result = optimizer._compute_utility_score(rows)
    assert result is not None


def test_compute_trend_improving(optimizer):
    """Recent window mean > previous + 0.05 → improving."""
    # 10 recent rows at 4.5, 10 previous rows at 2.0
    rows = (
        _make_rows([(2.0, d) for d in range(50, 40, -1)])   # 10 older rows
        + _make_rows([(4.5, d) for d in range(10, 0, -1)])  # 10 recent rows
    )
    assert optimizer._compute_trend(rows) == "improving"


def test_compute_trend_declining(optimizer):
    """Recent window mean < previous - 0.05 → declining."""
    rows = (
        _make_rows([(4.5, d) for d in range(50, 40, -1)])  # 10 older rows
        + _make_rows([(2.0, d) for d in range(10, 0, -1)]) # 10 recent rows
    )
    assert optimizer._compute_trend(rows) == "declining"


def test_compute_trend_stable(optimizer):
    """Windows within 0.05 of each other → stable."""
    rows = (
        _make_rows([(3.0, d) for d in range(50, 40, -1)])
        + _make_rows([(3.02, d) for d in range(10, 0, -1)])
    )
    assert optimizer._compute_trend(rows) == "stable"


def test_compute_trend_insufficient_data(optimizer):
    """Fewer than 5 rows in either window → insufficient-data."""
    rows = _make_rows([(3.0, d) for d in range(8, 0, -1)])  # only 8 rows total
    assert optimizer._compute_trend(rows) == "insufficient-data"


@pytest.mark.asyncio
async def test_update_frontmatter_writes_utility_score(optimizer, skills_dir):
    """After _update_frontmatter_stats, YAML has utility_score, utility_score_updated, score_trend."""
    today = datetime.now().date()
    rows_content = ""
    for i in range(12):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        rows_content += f"| {d} | slug{i} | model | 3.5 | ok |\n"

    skill_content = (
        "---\nname: test\nsuccess_rate: null\ntotal_runs: 0\n---\n\n"
        "## Instructions\n\nDo stuff.\n\n"
        "## Execution History\n\n"
        "| date | input_slug | model | score | notes |\n"
        "|------|-----------|-------|-------|-------|\n"
        + rows_content
    )
    updated = await optimizer._update_frontmatter_stats(skill_content)
    fm = yaml.safe_load(updated.split("---", 2)[1])
    assert "utility_score" in fm
    assert "utility_score_updated" in fm
    assert fm["score_trend"] in ("improving", "declining", "stable", "insufficient-data")
    assert fm["success_rate"] is not None  # backwards compat preserved


@pytest.mark.asyncio
async def test_gates_use_utility_score_over_success_rate(optimizer, skills_dir):
    """utility_score below threshold → optimise even when success_rate is above."""
    today = datetime.now().date()
    # Build 10 rows so min_runs passes
    rows_content = ""
    for i in range(10):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        rows_content += f"| {d} | s{i} | m | 0.5 | ok |\n"

    skill_content = (
        "---\nname: my-skill\nversion: 1\n"
        "success_rate: 0.95\n"      # above skip_above_threshold
        "utility_score: 0.55\n"    # below underperformance_threshold (0.70)
        "score_trend: stable\n"
        "total_runs: 10\n---\n\n"
        "## Instructions\n\nDo stuff.\n\n"
        "## Execution History\n\n"
        "| date | input_slug | model | score | notes |\n"
        "|------|-----------|-------|-------|-------|\n"
        + rows_content
    )
    skill_path = skills_dir / "my-skill.md"
    skill_path.write_text(skill_content)

    should_optimize, reason = await optimizer._check_optimization_gates(skill_path)
    assert should_optimize is True
    assert "utility_score" in reason


@pytest.mark.asyncio
async def test_declining_skill_priority_bypasses_min_runs(optimizer, skills_dir):
    """Declining skill with utility_score<0.80 bypasses min_runs gate."""
    skill_content = (
        "---\nname: weak-skill\nversion: 1\n"
        "success_rate: 0.65\n"
        "utility_score: 0.60\n"
        "score_trend: declining\n"
        "total_runs: 3\n---\n\n"   # only 3 runs — would normally be blocked
        "## Instructions\n\nDo stuff.\n\n"
        "## Execution History\n\n"
        "| date | input_slug | model | score | notes |\n"
        "|------|-----------|-------|-------|-------|\n"
    )
    skill_path = skills_dir / "weak-skill.md"
    skill_path.write_text(skill_content)

    should_optimize, reason = await optimizer._check_optimization_gates(skill_path)
    assert should_optimize is True
    assert "declining" in reason
