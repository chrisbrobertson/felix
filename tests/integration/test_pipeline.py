"""
Integration tests: URL entry → SkillExecutor → MemoryWriter → file on disk.

These tests use real file I/O against tmp directories and mock only the
LLM API call (acompletion) and HTTP fetches.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import browser_watcher as bw
import memory_writer as mw
import skill_executor as se

SKILL_MD = """\
---
name: summarize-webpage
version: 1
preferred_model: gemini/gemini-2.0-flash
---

## Instructions

Summarize the webpage concisely.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""

FAKE_SUMMARY = """\
## Summary
An article about LiteLLM's routing capabilities.

## Key Points
- Fallback chains defined in YAML
- OpenAI-compatible interface

## Entities
- **LiteLLM**: open-source router

**Tags:** litellm, routing, llm"""


@pytest.fixture
def infra(tmp_path):
    """Sets up skills + memories dirs and returns paths."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "summarize-webpage.md").write_text(SKILL_MD)

    memories = tmp_path / "memories"
    memories.mkdir()

    seen = tmp_path / "seen.txt"

    return {"skills": skills, "memories": memories, "seen": seen, "root": tmp_path}


async def test_executor_to_memory_file(infra):
    """SkillExecutor.run → MemoryWriter.write produces a valid memory file."""
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = FAKE_SUMMARY

    entry = {
        "url": "https://docs.litellm.ai/docs/routing",
        "title": "LiteLLM Router Documentation",
        "visit_count": 1,
        "browser": "chrome",
    }

    with patch.object(se, "SKILLS_DIR", infra["skills"]), \
         patch.object(mw, "MEMORIES_DIR", infra["memories"]), \
         patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)):

        executor = se.SkillExecutor("summarize-webpage", role="full")
        writer = mw.MemoryWriter()

        body = await executor.run({"url": entry["url"], "title": entry["title"],
                                   "content": "x" * 600})
        assert body is not None

        filename = await writer.write(entry, body)

    memory_files = list(infra["memories"].glob("*.md"))
    assert len(memory_files) == 1
    assert memory_files[0].name == filename

    content = memory_files[0].read_text()
    parts = content.split("---", 2)
    fm = yaml.safe_load(parts[1])

    assert fm["source_url"] == entry["url"]
    assert fm["source_title"] == entry["title"]
    assert "litellm" in fm["tags"]
    assert "## Summary" in content


async def test_execution_logged_to_skill_file(infra):
    """After a successful run, the skill file's Execution History has a new row."""
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = FAKE_SUMMARY

    with patch.object(se, "SKILLS_DIR", infra["skills"]), \
         patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)):
        executor = se.SkillExecutor("summarize-webpage", role="full")
        await executor.run({"url": "u", "title": "t", "content": "c"})

    skill_text = (infra["skills"] / "summarize-webpage.md").read_text()
    rows = [l for l in skill_text.splitlines() if l.strip().startswith("| 20")]
    assert len(rows) == 1


async def test_watcher_seen_urls_prevents_duplicate_processing(infra):
    """A URL in seen_urls must not be processed again."""
    entry = {
        "url": "https://example.com/already-seen",
        "title": "Already Seen",
        "visit_count": 1,
        "browser": "chrome",
    }
    config = {"browser_watcher": {"skip_domains": []}}

    with patch.object(se, "SKILLS_DIR", infra["skills"]), \
         patch.object(mw, "MEMORIES_DIR", infra["memories"]), \
         patch.object(bw, "SEEN_URLS_FILE", infra["seen"]):

        w = bw.BrowserWatcher(role="full")
        w.seen_urls = {entry["url"]}  # pre-mark

        should = w._should_process(entry, config)

    assert should is False
    assert list(infra["memories"].glob("*.md")) == []


async def test_process_url_adds_to_seen_set(infra):
    """After process_url succeeds, the URL should be in seen_urls."""
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = FAKE_SUMMARY

    entry = {
        "url": "https://example.com/new-page",
        "title": "New Page",
        "visit_count": 1,
        "browser": "chrome",
    }

    with patch.object(se, "SKILLS_DIR", infra["skills"]), \
         patch.object(mw, "MEMORIES_DIR", infra["memories"]), \
         patch.object(bw, "SEEN_URLS_FILE", infra["seen"]), \
         patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)), \
         patch("browser_watcher.SkillExecutor",
               side_effect=lambda *a, **kw: se.SkillExecutor(*a, **kw)), \
         patch("browser_watcher.MemoryWriter",
               side_effect=lambda: mw.MemoryWriter()):

        w = bw.BrowserWatcher(role="full")
        w.seen_urls = set()

        # Bypass HTTP — inject content directly
        async def fake_fetch(url):
            return "x" * 600

        w._fetch_content = fake_fetch
        await w.process_url(entry)

    assert entry["url"] in w.seen_urls


async def test_process_url_skips_short_content(infra):
    """Content below min_content_chars must not produce a memory file."""
    entry = {
        "url": "https://example.com/stub",
        "title": "Stub",
        "visit_count": 1,
        "browser": "chrome",
    }

    with patch.object(se, "SKILLS_DIR", infra["skills"]), \
         patch.object(mw, "MEMORIES_DIR", infra["memories"]), \
         patch.object(bw, "SEEN_URLS_FILE", infra["seen"]), \
         patch("browser_watcher.SkillExecutor",
               side_effect=lambda *a, **kw: se.SkillExecutor(*a, **kw)), \
         patch("browser_watcher.MemoryWriter",
               side_effect=lambda: mw.MemoryWriter()):

        w = bw.BrowserWatcher(role="full")
        w.seen_urls = set()

        async def fetch_short(url):
            return "short"  # < 500 chars

        w._fetch_content = fetch_short
        await w.process_url(entry)

    assert list(infra["memories"].glob("*.md")) == []
    assert entry["url"] not in w.seen_urls


async def test_watcher_role_logs_to_jsonl_not_skill_file(infra, tmp_path):
    """Watcher-role executor must not modify the shared skill file."""
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = FAKE_SUMMARY
    local_log = tmp_path / "exec.jsonl"
    original_skill = (infra["skills"] / "summarize-webpage.md").read_text()

    with patch.object(se, "SKILLS_DIR", infra["skills"]), \
         patch.object(se, "LOCAL_EXEC_LOG", local_log), \
         patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)):
        executor = se.SkillExecutor("summarize-webpage", role="watcher")
        await executor.run({"url": "u", "title": "t", "content": "c"})

    assert (infra["skills"] / "summarize-webpage.md").read_text() == original_skill
    assert local_log.exists()
