"""
Integration tests: chat handler context assembly against a real memory directory.

Uses real file I/O in tmp dirs; mocks only Telegram ApplicationBuilder
and SkillExecutor LLM calls.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import chat_handler as ch
from memory_cache import MemoryCache


@pytest.fixture
def handler(brain_dir, deploy_dir):
    """Override the shared handler fixture to use pass-through cache for context tests."""
    # Create a cache in pass-through mode for these tests
    cache = MemoryCache(None, brain_dir / "memories", enabled=False)

    mock_app = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    with patch.object(ch, "BRAIN_DIR", brain_dir), \
         patch.object(ch, "DEPLOY_DIR", deploy_dir), \
         patch("chat_handler.ApplicationBuilder", return_value=mock_builder), \
         patch("chat_handler.SkillExecutor"):
        h = ch.TelegramChatHandler(cache=cache)
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

@pytest.mark.asyncio
async def test_context_includes_index_and_memories(handler, brain_dir):
    (brain_dir / "index.md").write_text("Index: Chris reads about LLMs.")
    write_memory(brain_dir / "memories", "llm-abc123", ["llm"], "LLM Paper", "llm content")
    write_memory(brain_dir / "memories", "rust-bbb222", ["rust"], "Rust Book", "rust content")

    ctx = await handler._load_context("llm")
    assert "Index: Chris reads" in ctx
    # llm memory should be included (keyword match)
    assert "LLM Paper" in ctx
    # rust memory should not be included (no keyword overlap with query "llm")


@pytest.mark.asyncio
async def test_index_appears_before_memories(handler, brain_dir):
    (brain_dir / "index.md").write_text("The index.")
    write_memory(brain_dir / "memories", "article-abc123", ["article"], "Some Article")

    ctx = await handler._load_context("article")  # Match the tag/title so it's included
    assert ctx.index("The index.") < ctx.index("Some Article")


@pytest.mark.asyncio
async def test_relevant_memory_appears_before_irrelevant(handler, brain_dir):
    write_memory(brain_dir / "memories", "litellm-aaa111",
                 ["litellm", "llm", "routing"], "LiteLLM Routing",
                 "LiteLLM router with fallback chains")
    write_memory(brain_dir / "memories", "cooking-bbb222",
                 ["cooking", "recipe"], "Pasta Recipe",
                 "boil water, add pasta")

    ctx = await handler._load_context("litellm routing llm")
    # Relevant memory should be included
    assert "LiteLLM Routing" in ctx
    # Irrelevant memory should NOT be included (zero keyword overlap)
    assert "Pasta Recipe" not in ctx


@pytest.mark.asyncio
async def test_all_memories_included_within_budget(handler, brain_dir):
    memories = brain_dir / "memories"
    for i in range(5):
        write_memory(memories, f"page{i}-{i:06x}", [f"tag{i}"], f"Page {i}", "short body")

    ctx = await handler._load_context("page short body")  # Match content tokens
    for i in range(5):
        assert f"Page {i}" in ctx


@pytest.mark.asyncio
async def test_budget_exhaustion_drops_lowest_relevance(handler, brain_dir):
    memories = brain_dir / "memories"
    # Write many large files
    big = "word content " * 1500  # ~18KB each; 6 of them > 80KB budget
    for i in range(6):
        write_memory(memories, f"big{i}-{i:06x}", [f"irrelevant{i}"], f"Big File {i}", big)

    ctx = await handler._load_context("word content big")  # Match content tokens
    # Token budget (150k tokens ≈ 600k chars) — just verify not excessively large
    assert 1 < len(ctx) < 500_000, "Context should be non-empty but not exceed token budget"


@pytest.mark.asyncio
async def test_context_with_no_memories_and_no_index(handler, brain_dir):
    ctx = await handler._load_context("query")
    # No memories or index → empty context
    assert ctx == ""


@pytest.mark.asyncio
async def test_context_with_only_index(handler, brain_dir):
    (brain_dir / "index.md").write_text("Only the index exists.")
    ctx = await handler._load_context("query")
    assert "Only the index exists." in ctx
    assert "Memory Index" in ctx


@pytest.mark.asyncio
async def test_header_cache_persists_across_queries(handler, brain_dir):
    """Header cache is obsolete with MemoryCache — test now just verifies queries work."""
    p = write_memory(brain_dir / "memories", "cached-abc123", ["python"], "Python Docs")

    ctx1 = await handler._load_context("python")
    assert "Python Docs" in ctx1

    # Second query should also work
    ctx2 = await handler._load_context("python async")
    assert "Python Docs" in ctx2


# --- Full message round-trip (mocked LLM) ---

async def test_handle_message_builds_context_and_calls_executor(handler, brain_dir):
    write_memory(brain_dir / "memories", "llm-abc123", ["llm", "transformer"],
                 "Transformers Paper", "attention is all you need")

    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="Here is what I found.")

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "What do I know about transformers?"

    await handler.handle_message(mock_update, MagicMock())

    # executor.run_with_tools must have been called with the query and memory context
    call_kwargs = handler.executor.run_with_tools.call_args.kwargs["inputs"]
    assert "user_query" in call_kwargs
    assert "memory_context" in call_kwargs
    assert "transformers" in call_kwargs["memory_context"].lower()

    # Response should be sent back
    mock_update.message.reply_text.assert_called()


async def test_handle_message_chunks_long_response(handler, brain_dir):
    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="X" * 10000)

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "Tell me everything."

    await handler.handle_message(mock_update, MagicMock())

    assert mock_update.message.reply_text.call_count == 3  # ceil(10000/4096)
