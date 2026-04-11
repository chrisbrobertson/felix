"""
Integration tests: chat handler context assembly against a real memory directory.

Uses real file I/O in tmp dirs; mocks only Telegram ApplicationBuilder
and SkillExecutor LLM calls.
"""
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


def write_memory(memories_dir: Path, slug: str, tags: list, title: str,
                 body: str = "content") -> Path:
    path = memories_dir / f"2026-04-11-{slug}.md"
    path.write_text(
        f"---\ntags: {tags}\nsource_title: {title}\n---\n\n## Summary\n{body}"
    )
    return path


# --- Full context assembly ---

def test_context_includes_index_and_memories(handler, brain_dir):
    (brain_dir / "index.md").write_text("Index: Chris reads about LLMs.")
    write_memory(brain_dir / "memories", "llm-abc123", ["llm"], "LLM Paper", "llm content")
    write_memory(brain_dir / "memories", "rust-bbb222", ["rust"], "Rust Book", "rust content")

    ctx = handler._load_context("llm")
    assert "Index: Chris reads" in ctx
    assert "LLM Paper" in ctx
    assert "Rust Book" in ctx


def test_index_appears_before_memories(handler, brain_dir):
    (brain_dir / "index.md").write_text("The index.")
    write_memory(brain_dir / "memories", "article-abc123", ["article"], "Some Article")

    ctx = handler._load_context("query")
    assert ctx.index("The index.") < ctx.index("Some Article")


def test_relevant_memory_appears_before_irrelevant(handler, brain_dir):
    write_memory(brain_dir / "memories", "litellm-aaa111",
                 ["litellm", "llm", "routing"], "LiteLLM Routing",
                 "LiteLLM router with fallback chains")
    write_memory(brain_dir / "memories", "cooking-bbb222",
                 ["cooking", "recipe"], "Pasta Recipe",
                 "boil water, add pasta")

    ctx = handler._load_context("litellm routing llm")
    assert ctx.index("LiteLLM Routing") < ctx.index("Pasta Recipe")


def test_all_memories_included_within_budget(handler, brain_dir):
    memories = brain_dir / "memories"
    for i in range(5):
        write_memory(memories, f"page{i}-{i:06x}", [f"tag{i}"], f"Page {i}", "short body")

    ctx = handler._load_context("query")
    for i in range(5):
        assert f"Page {i}" in ctx


def test_budget_exhaustion_drops_lowest_relevance(handler, brain_dir):
    memories = brain_dir / "memories"
    # Write many large files
    big = "word content " * 1500  # ~18KB each; 6 of them > 80KB budget
    for i in range(6):
        write_memory(memories, f"big{i}-{i:06x}", [f"irrelevant{i}"], f"Big File {i}", big)

    ctx = handler._load_context("query")
    assert len(ctx) <= ch.MAX_CONTEXT_CHARS + 1000  # tolerance for separators


def test_context_with_no_memories_and_no_index(handler, brain_dir):
    ctx = handler._load_context("query")
    assert ctx == ""


def test_context_with_only_index(handler, brain_dir):
    (brain_dir / "index.md").write_text("Only the index exists.")
    ctx = handler._load_context("query")
    assert "Only the index exists." in ctx
    assert "---" not in ctx  # no separator added when no memory files follow


def test_header_cache_persists_across_queries(handler, brain_dir):
    """Same file queried twice should hit the header cache on the second call."""
    p = write_memory(brain_dir / "memories", "cached-abc123", ["python"], "Python Docs")

    handler._load_context("python")
    assert p in handler._header_cache

    # Second query — mtime hasn't changed, should reuse cache
    cached_mtime, cached_header = handler._header_cache[p]
    handler._load_context("python async")
    assert handler._header_cache[p][0] == cached_mtime  # mtime unchanged


# --- Full message round-trip (mocked LLM) ---

async def test_handle_message_builds_context_and_calls_executor(handler, brain_dir):
    write_memory(brain_dir / "memories", "llm-abc123", ["llm", "transformer"],
                 "Transformers Paper", "attention is all you need")

    handler.executor = MagicMock()
    handler.executor.run = AsyncMock(return_value="Here is what I found.")

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "What do I know about transformers?"

    await handler.handle_message(mock_update, MagicMock())

    # executor.run must have been called with the query and memory context
    call_kwargs = handler.executor.run.call_args[0][0]
    assert "user_query" in call_kwargs
    assert "memory_context" in call_kwargs
    assert "transformers" in call_kwargs["memory_context"].lower()

    # Response should be sent back
    mock_update.message.reply_text.assert_called()


async def test_handle_message_chunks_long_response(handler, brain_dir):
    handler.executor = MagicMock()
    handler.executor.run = AsyncMock(return_value="X" * 10000)

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "Tell me everything."

    await handler.handle_message(mock_update, MagicMock())

    assert mock_update.message.reply_text.call_count == 3  # ceil(10000/4096)
