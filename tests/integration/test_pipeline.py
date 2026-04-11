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
import chat_handler as ch
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


BRAIN_CONFIG_YAML = """\
telegram:
  bot_token: fake-token
user:
  telegram_user_id: "12345"
  name: Chris
browser_watcher:
  skip_domains:
    - google.com
"""


@pytest.fixture
def infra(tmp_path):
    """Sets up skills + memories dirs and returns paths."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "summarize-webpage.md").write_text(SKILL_MD)

    memories = tmp_path / "memories"
    memories.mkdir()

    (tmp_path / "config.yaml").write_text(BRAIN_CONFIG_YAML)

    seen = tmp_path / "seen.txt"

    return {"skills": skills, "memories": memories, "seen": seen, "root": tmp_path}


@pytest.fixture
def chat_handler_instance(infra):
    """TelegramChatHandler wired to the infra tmp_path brain dir."""
    mock_app = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    with patch.object(ch, "BRAIN_DIR", infra["root"]), \
         patch("chat_handler.ApplicationBuilder", return_value=mock_builder), \
         patch("chat_handler.SkillExecutor"):
        handler = ch.TelegramChatHandler()
        handler.allowed_user_id = 12345
        yield handler


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


# ── Domain skip filter integration ────────────────────────────────────────────

async def test_skip_command_persists_and_watcher_ignores_domain(
    infra, chat_handler_instance
):
    """/skip writes to config.yaml; watcher then rejects URLs from that domain."""
    # Step 1: add twitter.com via the /skip command
    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.args = ["twitter.com"]

    await chat_handler_instance.cmd_skip(mock_update, mock_ctx)

    # Verify config.yaml was updated
    config = yaml.safe_load((infra["root"] / "config.yaml").read_text())
    assert "twitter.com" in config["browser_watcher"]["skip_domains"]

    # Step 2: confirm the browser watcher respects the updated config
    entry = {"url": "https://twitter.com/something", "title": "Tweet",
             "visit_count": 1, "browser": "chrome"}

    with patch.object(bw, "SEEN_URLS_FILE", infra["seen"]):
        w = bw.BrowserWatcher(role="full")
        w.seen_urls = set()
        should = w._should_process(entry, config)

    assert should is False


async def test_purge_command_removes_correct_memories(infra, chat_handler_instance):
    """/purge deletes memories matching the domain and leaves others intact."""
    m = infra["root"] / "memories"

    # Write two memories — one for example.com, one for other.com
    target = m / "2026-04-11-ex-aaa111.md"
    target.write_text(
        "---\nsource_url: https://example.com/article\ntags: []\n---\n\n## Summary\nEx"
    )
    keeper = m / "2026-04-11-other-bbb222.md"
    keeper.write_text(
        "---\nsource_url: https://other.com/page\ntags: []\n---\n\n## Summary\nOther"
    )

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.args = ["example.com"]

    await chat_handler_instance.cmd_purge(mock_update, mock_ctx)

    assert not target.exists()
    assert keeper.exists()
    reply = mock_update.message.reply_text.call_args[0][0]
    assert "Deleted 1" in reply


async def test_purgeall_command_clears_all_skip_domain_memories(
    infra, chat_handler_instance
):
    """/purgeall removes memories for every domain currently on the skip list."""
    m = infra["root"] / "memories"

    # google.com is already in the skip list from the fixture config
    g = m / "2026-04-11-google-aaa111.md"
    g.write_text(
        "---\nsource_url: https://google.com/search\ntags: []\n---\n\n## Summary\nG"
    )
    keeper = m / "2026-04-11-other-bbb222.md"
    keeper.write_text(
        "---\nsource_url: https://other.com/page\ntags: []\n---\n\n## Summary\nOther"
    )

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_ctx = MagicMock()

    await chat_handler_instance.cmd_purgeall(mock_update, mock_ctx)

    assert not g.exists()
    assert keeper.exists()
    reply = mock_update.message.reply_text.call_args[0][0]
    assert "google.com" in reply
