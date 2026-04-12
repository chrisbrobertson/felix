"""Unit tests for skill_executor.py."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skill_executor as se

SKILL_CONTENT = """\
---
name: summarize-webpage
version: 1
preferred_model: gemini/gemini-2.0-flash
fallback_model: claude-haiku-4-5-20251001
success_rate: null
total_runs: 0
---

## Instructions

You are creating a long-term memory entry from a webpage.

Given title, URL, and content — produce a concise summary.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""


@pytest.fixture
def skills_dir(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    (d / "summarize-webpage.md").write_text(SKILL_CONTENT)
    return d


@pytest.fixture
def executor_full(skills_dir):
    with patch.object(se, "SKILLS_DIR", skills_dir):
        yield se.SkillExecutor("summarize-webpage", role="full")


@pytest.fixture
def executor_watcher(skills_dir):
    with patch.object(se, "SKILLS_DIR", skills_dir):
        yield se.SkillExecutor("summarize-webpage", role="watcher")


# --- Parsing ---

def test_loads_instructions(executor_full):
    assert "long-term memory entry" in executor_full._skill["instructions"]


def test_loads_preferred_model(executor_full):
    assert executor_full._skill["meta"]["preferred_model"] == "gemini/gemini-2.0-flash"


def test_loads_skill_name(executor_full):
    assert executor_full._skill["meta"]["name"] == "summarize-webpage"


# --- run() success path ---

async def test_run_returns_llm_content(executor_full):
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "## Summary\nGreat article."
    with patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)):
        result = await executor_full.run({"url": "https://x.com", "title": "X", "content": "body"})
    assert result == "## Summary\nGreat article."


async def test_run_uses_preferred_model(executor_full):
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "output"
    with patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)) as mock_ac:
        await executor_full.run({"url": "u", "title": "t", "content": "c"})
    call_kwargs = mock_ac.call_args
    assert call_kwargs.kwargs["model"] == "gemini/gemini-2.0-flash"


# --- Execution logging: full node writes to skill file ---

async def test_full_node_appends_row_to_skill_file(executor_full, skills_dir):
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "output"
    with patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)):
        await executor_full.run({"url": "https://x.com", "title": "T", "content": "c"})
    skill_text = (skills_dir / "summarize-webpage.md").read_text()
    # A new table row (date starts with 20xx) should have been appended
    lines_with_pipe = [l for l in skill_text.splitlines() if l.strip().startswith("| 20")]
    assert len(lines_with_pipe) == 1


async def test_full_node_creates_execution_history_section_if_missing(skills_dir):
    """If skill file has no Execution History section, one is created."""
    skill_without_history = SKILL_CONTENT.replace(
        "## Execution History\n\n| date | input_slug | model | score | notes |\n|------|-----------|-------|-------|-------|\n",
        ""
    )
    (skills_dir / "summarize-webpage.md").write_text(skill_without_history)
    with patch.object(se, "SKILLS_DIR", skills_dir):
        executor = se.SkillExecutor("summarize-webpage", role="full")
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "output"
    with patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)):
        await executor.run({"url": "u", "title": "t", "content": "c"})
    assert "## Execution History" in (skills_dir / "summarize-webpage.md").read_text()


# --- Execution logging: watcher node writes to local JSONL ---

async def test_watcher_writes_to_local_jsonl(executor_watcher, tmp_path):
    brain_dir = tmp_path / "brain"
    logs_dir = brain_dir / "logs"
    logs_dir.mkdir(parents=True)

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "output"
    with patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)), \
         patch.object(se, "BRAIN_DIR", brain_dir):
        await executor_watcher.run({"url": "u", "title": "t", "content": "c"})

    # Find the log file (name includes hostname)
    log_files = list(logs_dir.glob("*-execution-log.jsonl"))
    assert len(log_files) == 1

    record = json.loads(log_files[0].read_text().strip())
    assert record["skill"] == "summarize-webpage"
    assert "hostname" in record
    assert "date" in record


async def test_watcher_does_not_modify_skill_file(executor_watcher, skills_dir, tmp_path):
    brain_dir = tmp_path / "brain"
    logs_dir = brain_dir / "logs"
    logs_dir.mkdir(parents=True)

    original = (skills_dir / "summarize-webpage.md").read_text()
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "output"
    with patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)), \
         patch.object(se, "BRAIN_DIR", brain_dir):
        await executor_watcher.run({"url": "u", "title": "t", "content": "c"})
    assert (skills_dir / "summarize-webpage.md").read_text() == original


# --- Error handling ---

async def test_run_returns_none_on_api_error(executor_full, tmp_path):
    error_log = tmp_path / "errors.log"
    with patch("skill_executor.acompletion", new=AsyncMock(side_effect=Exception("timeout"))), \
         patch.object(se, "ERROR_LOG", error_log):
        result = await executor_full.run({"url": "u", "title": "t", "content": "c"})
    assert result is None


async def test_run_writes_error_log_on_failure(executor_full, tmp_path):
    error_log = tmp_path / "errors.log"
    with patch("skill_executor.acompletion", new=AsyncMock(side_effect=Exception("API down"))), \
         patch.object(se, "ERROR_LOG", error_log):
        await executor_full.run({"url": "u", "title": "t", "content": "c"})
    assert error_log.exists()
    assert "API down" in error_log.read_text()


async def test_error_score_logged_as_zero(executor_full, skills_dir, tmp_path):
    error_log = tmp_path / "errors.log"
    with patch("skill_executor.acompletion", new=AsyncMock(side_effect=Exception("fail"))), \
         patch.object(se, "ERROR_LOG", error_log):
        await executor_full.run({"url": "u", "title": "t", "content": "c"})
    skill_text = (skills_dir / "summarize-webpage.md").read_text()
    assert "| 0.00 |" in skill_text
