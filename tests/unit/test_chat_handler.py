"""Unit tests for chat_handler.py."""
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chat_handler as ch

CONFIG_YAML = """\
telegram:
  bot_token: fake-token
user:
  telegram_user_id: "12345"
  name: Chris
  timezone: America/Los_Angeles
"""


@pytest.fixture
def brain_dir(tmp_path):
    d = tmp_path / "brain"
    d.mkdir()
    (d / "memories").mkdir()
    (d / "config.yaml").write_text(CONFIG_YAML)
    return d


@pytest.fixture
def handler(brain_dir):
    mock_app = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    with patch.object(ch, "BRAIN_DIR", brain_dir), \
         patch("chat_handler.ApplicationBuilder", return_value=mock_builder), \
         patch("chat_handler.SkillExecutor"):
        h = ch.TelegramChatHandler()
        h.allowed_user_id = 12345
        yield h


def write_memory(memories_dir: Path, slug: str, tags: list, title: str, body: str = "content") -> Path:
    path = memories_dir / f"2026-04-11-{slug}.md"
    path.write_text(f"---\ntags: {tags}\nsource_title: {title}\n---\n\n## Summary\n{body}")
    return path


# --- _score_relevance ---

def test_score_exact_keyword_match(handler, brain_dir):
    p = write_memory(brain_dir / "memories", "litellm-abc123", ["litellm", "routing"], "LiteLLM Router")
    score = handler._score_relevance(p, "litellm routing")
    assert score >= 2


def test_score_zero_when_no_match(handler, brain_dir):
    p = write_memory(brain_dir / "memories", "cooking-abc123", ["cooking", "food"], "Recipes")
    assert handler._score_relevance(p, "litellm routing") == 0


def test_score_ignores_tokens_under_3_chars(handler, brain_dir):
    p = write_memory(brain_dir / "memories", "test-abc123", ["go", "ai"], "Go AI")
    # "go" and "ai" are 2 chars — below the 3-char threshold
    assert handler._score_relevance(p, "go ai") == 0


def test_score_is_case_insensitive(handler, brain_dir):
    p = write_memory(brain_dir / "memories", "python-abc123", ["Python", "Async"], "Python Guide")
    score = handler._score_relevance(p, "PYTHON ASYNC")
    assert score >= 2


# --- _get_header cache ---

def test_header_cached_after_first_read(handler, brain_dir):
    p = write_memory(brain_dir / "memories", "test-abc123", ["test"], "Test Page")
    handler._get_header(p)
    assert p in handler._header_cache


def test_header_cache_invalidated_when_mtime_changes(handler, brain_dir):
    p = write_memory(brain_dir / "memories", "test-abc123", ["old"], "Old Title")
    handler._get_header(p)  # populate cache
    time.sleep(0.05)
    p.write_text("---\ntags: [new]\nsource_title: New Title\n---\n\ncontent")
    os.utime(p, None)  # bump mtime
    header = handler._get_header(p)
    assert "New Title" in header


def test_header_cache_reused_when_mtime_unchanged(handler, brain_dir):
    p = write_memory(brain_dir / "memories", "test-abc123", ["cached"], "Cached")
    first = handler._get_header(p)
    # Don't touch the file — cache should be reused
    second = handler._get_header(p)
    assert first == second


# --- _load_context ---

def test_context_prepends_index_when_present(handler, brain_dir):
    (brain_dir / "index.md").write_text("Weekly index content.")
    ctx = handler._load_context("anything")
    assert ctx.startswith("# Memory Index")
    assert "Weekly index content." in ctx


def test_context_includes_all_memories(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "page-one-aaa111", ["python"], "Python Guide")
    write_memory(m, "page-two-bbb222", ["rust"], "Rust Guide")
    ctx = handler._load_context("anything")
    assert "Python Guide" in ctx
    assert "Rust Guide" in ctx


def test_context_sorts_by_relevance(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "relevant-aaa111", ["litellm", "llm", "routing"], "LiteLLM Guide")
    write_memory(m, "unrelated-bbb222", ["cooking", "food"], "Cooking Tips")
    ctx = handler._load_context("litellm routing llm")
    assert ctx.index("LiteLLM Guide") < ctx.index("Cooking Tips")


def test_context_respects_char_budget(handler, brain_dir):
    m = brain_dir / "memories"
    big = "word " * 3000  # ~15KB
    for i in range(10):
        write_memory(m, f"big-{i:02d}-{i:06x}", [f"t{i}"], f"File {i}", big)
    ctx = handler._load_context("test")
    assert len(ctx) <= ch.MAX_CONTEXT_CHARS + 500  # small tolerance for separators


def test_context_empty_when_no_memories_and_no_index(handler, brain_dir):
    ctx = handler._load_context("anything")
    assert ctx == ""


def test_context_index_only_when_no_memories(handler, brain_dir):
    (brain_dir / "index.md").write_text("Just the index.")
    ctx = handler._load_context("anything")
    assert "Just the index." in ctx


# --- _send_reply chunking ---

async def test_send_reply_single_short_message(handler):
    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    await handler._send_reply(mock_update, "Hello!")
    mock_update.message.reply_text.assert_called_once_with("Hello!")


async def test_send_reply_chunks_at_4096(handler):
    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    text = "A" * 10000  # needs ceil(10000 / 4096) = 3 chunks
    await handler._send_reply(mock_update, text)
    assert mock_update.message.reply_text.call_count == 3


async def test_send_reply_exact_chunk_boundary(handler):
    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    text = "B" * (ch.TG_MAX_CHARS * 2)  # exactly 2 chunks
    await handler._send_reply(mock_update, text)
    assert mock_update.message.reply_text.call_count == 2


async def test_send_reply_empty_text_sends_fallback(handler):
    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    await handler._send_reply(mock_update, "")
    mock_update.message.reply_text.assert_called_once_with("No response generated.")


async def test_send_reply_none_sends_fallback(handler):
    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    await handler._send_reply(mock_update, None)
    mock_update.message.reply_text.assert_called_once_with("No response generated.")


# --- handle_message user ID whitelist ---

async def test_handle_message_ignores_unauthorised_user(handler):
    mock_update = MagicMock()
    mock_update.effective_user.id = 99999
    mock_update.message.text = "hello"
    await handler.handle_message(mock_update, MagicMock())
    mock_update.message.reply_text.assert_not_called()


async def test_handle_message_processes_authorised_user(handler, brain_dir):
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "Here is your answer."
    handler.executor = MagicMock()
    handler.executor.run = AsyncMock(return_value="Here is your answer.")

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "What did I read about LLMs?"

    await handler.handle_message(mock_update, MagicMock())
    mock_update.message.reply_text.assert_called()
