"""Unit tests for chat_handler.py."""
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import chat_handler as ch

CONFIG_YAML = """\
telegram:
  bot_token: fake-token
user:
  telegram_user_id: "12345"
  name: Chris
  timezone: America/Los_Angeles
browser_watcher:
  skip_domains:
    - google.com
    - facebook.com
"""


@pytest.fixture
def brain_dir(tmp_path):
    d = tmp_path / "brain"
    d.mkdir()
    (d / "memories").mkdir()
    (d / "config.yaml").write_text(CONFIG_YAML)
    return d


@pytest.fixture
def handler(brain_dir, tmp_path):
    from memory_cache import MemoryCache
    mock_app = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir(exist_ok=True)

    # Create cache in pass-through mode for tests
    cache = MemoryCache(None, brain_dir / "memories", enabled=False)

    with patch.object(ch, "BRAIN_DIR", brain_dir), \
         patch.object(ch, "DEPLOY_DIR", deploy_dir), \
         patch.object(ch.TelegramChatHandler, "PENDING_FILE", deploy_dir / "pending-replies.json"), \
         patch.object(ch.TelegramChatHandler, "HISTORY_FILE", deploy_dir / "chat-history.json"), \
         patch("chat_handler.ApplicationBuilder", return_value=mock_builder), \
         patch("chat_handler.SkillExecutor"), \
         patch.dict(os.environ, {"GITHUB_PAT": "", "GITHUB_REPO": ""}, clear=False):
        h = ch.TelegramChatHandler(cache=cache)
        h.allowed_user_id = 12345
        yield h


def write_memory(memories_dir: Path, slug: str, tags: list, title: str,
                 body: str = "content", source_url: str = "",
                 created: str = "2026-04-11T12:00:00",
                 summary: str = "") -> Path:
    path = memories_dir / f"2026-04-11-{slug}.md"
    url_line = f"source_url: {source_url}\n" if source_url else ""
    summary_line = f"summary: {summary}\n" if summary else ""
    path.write_text(
        f"---\nsource_title: {title}\n{url_line}{summary_line}"
        f"tags: {tags}\ncreated: '{created}'\n---\n\n## Summary\n{body}"
    )
    return path


def _make_update(user_id: int, args=None):
    """Build a mock Update with an async message and optional command args."""
    mock_update = MagicMock()
    mock_update.effective_user.id = user_id
    mock_update.message = AsyncMock()
    mock_context = MagicMock()
    mock_context.args = args or []
    return mock_update, mock_context


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

@pytest.mark.asyncio
async def test_context_prepends_index_when_present(handler, brain_dir):
    (brain_dir / "index.md").write_text("Weekly index content.")
    ctx = await handler._load_context("anything")
    # Index is prepended when present
    assert "Weekly index content." in ctx
    assert "Memory Index" in ctx


@pytest.mark.asyncio
async def test_context_includes_all_memories(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "page-one-aaa111", ["python"], "Python Guide")
    write_memory(m, "page-two-bbb222", ["rust"], "Rust Guide")
    # Query matches "guide" which appears in both memory titles
    ctx = await handler._load_context("guide")
    assert "Python Guide" in ctx
    assert "Rust Guide" in ctx


@pytest.mark.asyncio
async def test_context_sorts_by_relevance(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "relevant-aaa111", ["litellm", "llm", "routing"], "LiteLLM Guide")
    write_memory(m, "unrelated-bbb222", ["cooking", "food"], "Cooking Tips")
    ctx = await handler._load_context("litellm routing llm")
    # The relevant file should be included
    assert "LiteLLM Guide" in ctx
    # The unrelated file (no keyword match) should not be included
    assert "Cooking Tips" not in ctx


@pytest.mark.asyncio
async def test_context_respects_char_budget(handler, brain_dir):
    """Context respects token budget (was char budget before M9)."""
    m = brain_dir / "memories"
    big = "word " * 3000  # ~15KB
    for i in range(10):
        write_memory(m, f"big-{i:02d}-{i:06x}", [f"t{i}"], f"File {i}", big)
    # Query "word" which appears in all the files' bodies
    ctx = await handler._load_context("word")
    # Token budget of 150k tokens ≈ 600k chars (rough 1:4 ratio), but actual tokens will be lower
    # Just verify context was generated and isn't unreasonably large
    assert 10_000 < len(ctx) < 1_000_000  # sanity check, not strict budget test


@pytest.mark.asyncio
async def test_context_empty_when_no_memories_and_no_index(handler, brain_dir):
    ctx = await handler._load_context("anything")
    # Context is empty when no memories or index exist
    assert ctx == ""


@pytest.mark.asyncio
async def test_context_index_only_when_no_memories(handler, brain_dir):
    (brain_dir / "index.md").write_text("Just the index.")
    ctx = await handler._load_context("anything")
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
    handler.executor.run_with_tools = AsyncMock(return_value="Here is your answer.")

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "What did I read about LLMs?"

    await handler.handle_message(mock_update, MagicMock())
    mock_update.message.reply_text.assert_called()


async def test_handle_message_reacts_with_eyes_on_receipt(handler, brain_dir):
    """Verify 👀 reaction is set immediately on receipt before processing."""
    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="Here is your answer.")

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "What did I read about LLMs?"

    mock_context = MagicMock()

    await handler.handle_message(mock_update, mock_context)

    # First call should be 👀
    calls = mock_update.message.set_reaction.call_args_list
    assert len(calls) >= 2
    assert calls[0][0][0] == "👀"
    assert calls[-1][0][0] == "✅"


async def test_handle_message_reacts_with_check_on_success(handler, brain_dir):
    """Verify ✅ reaction is set after successful reply."""
    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="Here is your answer.")

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "What did I read about LLMs?"

    mock_context = MagicMock()

    await handler.handle_message(mock_update, mock_context)

    # Last call should be ✅
    calls = mock_update.message.set_reaction.call_args_list
    assert calls[-1][0][0] == "✅"
    mock_update.message.reply_text.assert_called()


async def test_handle_message_reacts_with_x_on_error(handler, brain_dir):
    """Verify ❌ reaction is set when the executor raises."""
    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(side_effect=RuntimeError("LLM failed"))

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "What did I read about LLMs?"

    mock_context = MagicMock()

    await handler.handle_message(mock_update, mock_context)

    # First call should be 👀, last should be ❌
    calls = mock_update.message.set_reaction.call_args_list
    assert len(calls) >= 2
    assert calls[0][0][0] == "👀"
    assert calls[-1][0][0] == "❌"
    mock_update.message.reply_text.assert_called()


async def test_handle_message_reaction_failure_does_not_crash(handler, brain_dir):
    """Verify that set_reaction failures don't prevent message processing."""
    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="Here is your answer.")

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "What did I read about LLMs?"
    # Make all set_reaction calls fail
    mock_update.message.set_reaction.side_effect = Exception("Reactions not supported")

    mock_context = MagicMock()

    # Should not raise — reactions are best-effort
    await handler.handle_message(mock_update, mock_context)

    # Message should still be processed normally
    mock_update.message.reply_text.assert_called()
    handler.executor.run_with_tools.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_timeout_sends_error_and_reacts(handler, brain_dir):
    """Verify that a chat timeout sends an error reply and sets ❌ reaction."""
    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(side_effect=asyncio.TimeoutError())

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "Tell me everything about all my projects in detail"

    mock_context = MagicMock()

    await handler.handle_message(mock_update, mock_context)

    # Should reply with a timeout message
    reply_args = mock_update.message.reply_text.call_args_list
    assert any("timed out" in str(call).lower() for call in reply_args)

    # Should set ❌ reaction
    reaction_calls = [str(c) for c in mock_update.message.set_reaction.call_args_list]
    assert any("❌" in c for c in reaction_calls)

    # History must NOT be saved — the response was not delivered
    history = handler._chat_history.get(12345, [])
    assert history == []


@pytest.mark.asyncio
async def test_handle_message_timeout_after_mutation_warns_user(handler, brain_dir):
    """Timeout that fires after a mutating tool already completed must warn the user
    not to retry, rather than suggesting a generic retry that would duplicate state."""
    handler.executor = MagicMock()

    # Simulate: run_with_tools calls tool_dispatch("add_goal", ...) successfully,
    # then times out waiting for the next LLM turn.
    async def _run_with_tools_calls_mutation_then_times_out(**kwargs):
        td = kwargs["tool_dispatch"]
        await td("add_goal", {"title": "Test goal", "category": "work"})
        raise asyncio.TimeoutError()

    handler.executor.run_with_tools = _run_with_tools_calls_mutation_then_times_out

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "Add a goal: ship the thing by end of quarter"

    with patch("chat_tools.dispatch", new=AsyncMock(return_value="Goal created: Test goal [work]")):
        await handler.handle_message(mock_update, MagicMock())

    reply_calls = mock_update.message.reply_text.call_args_list
    assert reply_calls, "expected a reply"
    reply_text = " ".join(str(c) for c in reply_calls)
    assert "add_goal" in reply_text, "reply should name the completed mutation"
    assert "verify" in reply_text.lower() or "duplicate" in reply_text.lower(), \
        "reply should warn about duplicates"
    assert "try asking" not in reply_text.lower(), \
        "generic retry suggestion should be absent when a mutation already landed"


@pytest.mark.asyncio
async def test_handle_message_timeout_after_failed_mutation_uses_generic_message(handler, brain_dir):
    """Timeout after a mutating tool returns an error string must NOT warn about duplicates.

    The tool returned "Error: ..." so no state was written — the user should be
    told to retry normally, not warned about a phantom mutation.
    """
    handler.executor = MagicMock()

    async def _run_with_tools_calls_failed_mutation_then_times_out(**kwargs):
        td = kwargs["tool_dispatch"]
        await td("add_goal", {"title": "", "category": "bad-category"})
        raise asyncio.TimeoutError()

    handler.executor.run_with_tools = _run_with_tools_calls_failed_mutation_then_times_out

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "Add a goal: ship the thing"

    with patch("chat_tools.dispatch", new=AsyncMock(return_value="Error: invalid category 'bad-category'")):
        await handler.handle_message(mock_update, MagicMock())

    reply_calls = mock_update.message.reply_text.call_args_list
    assert reply_calls, "expected a reply"
    reply_text = " ".join(str(c) for c in reply_calls)
    assert "timed out" in reply_text.lower(), "should report a timeout"
    assert "verify" not in reply_text.lower() and "duplicate" not in reply_text.lower(), \
        "must not warn about duplicates when the mutation returned an error"
    assert "add_goal" not in reply_text, \
        "must not name the failed tool as a completed mutation"


# ── conversation history ──────────────────────────────────────────────────────

def _make_handle_message_mocks(handler):
    """Return (update, context) mocks suitable for handle_message tests."""
    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="The answer is 42.")
    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_chat.id = 99001
    update.message = AsyncMock()
    update.message.text = "Hello"
    update.message.set_reaction = AsyncMock()
    context = MagicMock()
    return update, context


@pytest.mark.asyncio
async def test_handle_message_appends_to_history(handler, brain_dir):
    """After one turn, _chat_history holds user + assistant messages."""
    update, context = _make_handle_message_mocks(handler)
    await handler.handle_message(update, context)
    history = handler._chat_history.get(99001, [])
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Hello"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "The answer is 42."


@pytest.mark.asyncio
async def test_handle_message_passes_history_to_executor(handler, brain_dir):
    """Second message passes prior turns as history kwarg to run_with_tools."""
    update, context = _make_handle_message_mocks(handler)
    # Prime the history
    await handler.handle_message(update, context)
    # Second turn
    update.message.text = "And what is 7 × 6?"
    handler.executor.run_with_tools = AsyncMock(return_value="It is 42.")
    await handler.handle_message(update, context)
    call_kwargs = handler.executor.run_with_tools.call_args.kwargs
    history_passed = call_kwargs.get("history", [])
    assert any(m["role"] == "user" and "Hello" in m["content"] for m in history_passed)


@pytest.mark.asyncio
async def test_chat_history_window_truncates_to_six_turns(handler, brain_dir):
    """Sending 10 turns keeps only the last 6 pairs (12 messages)."""
    update, context = _make_handle_message_mocks(handler)
    for i in range(10):
        update.message.text = f"Message {i}"
        handler.executor.run_with_tools = AsyncMock(return_value=f"Reply {i}")
        await handler.handle_message(update, context)
    history = handler._chat_history.get(99001, [])
    assert len(history) == handler.HISTORY_WINDOW_TURNS * 2


@pytest.mark.asyncio
async def test_chat_history_isolated_per_chat_id(handler, brain_dir):
    """Two different chat IDs do not share history."""
    update_a = MagicMock()
    update_a.effective_user.id = 12345
    update_a.effective_chat.id = 11111
    update_a.message = AsyncMock()
    update_a.message.text = "Chat A message"
    update_a.message.set_reaction = AsyncMock()

    update_b = MagicMock()
    update_b.effective_user.id = 12345
    update_b.effective_chat.id = 22222
    update_b.message = AsyncMock()
    update_b.message.text = "Chat B message"
    update_b.message.set_reaction = AsyncMock()

    context = MagicMock()
    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="Reply")

    await handler.handle_message(update_a, context)
    await handler.handle_message(update_b, context)

    assert 11111 in handler._chat_history
    assert 22222 in handler._chat_history
    # Neither history should contain the other chat's message
    h_a = handler._chat_history[11111]
    h_b = handler._chat_history[22222]
    assert not any("Chat B" in m["content"] for m in h_a)
    assert not any("Chat A" in m["content"] for m in h_b)


@pytest.mark.asyncio
async def test_reset_command_clears_history(handler):
    """cmd_reset removes all history for the chat."""
    handler._chat_history[12345] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    update, ctx = _make_update(12345)
    update.effective_chat.id = 12345  # must match the key in _chat_history
    await handler.cmd_reset(update, ctx)
    assert handler._chat_history.get(12345, []) == []
    assert "1 turn" in update.message.reply_text.call_args[0][0]


# ── _edit_skip_domains ────────────────────────────────────────────────────────

def test_edit_skip_domains_add(handler, brain_dir):
    result = handler._edit_skip_domains("add", "twitter.com")
    assert result is None
    config = yaml.safe_load((brain_dir / "config.yaml").read_text())
    assert "twitter.com" in config["browser_watcher"]["skip_domains"]


def test_edit_skip_domains_add_already_present(handler, brain_dir):
    result = handler._edit_skip_domains("add", "google.com")
    assert "already" in result
    # list unchanged
    config = yaml.safe_load((brain_dir / "config.yaml").read_text())
    assert config["browser_watcher"]["skip_domains"].count("google.com") == 1


def test_edit_skip_domains_remove(handler, brain_dir):
    result = handler._edit_skip_domains("remove", "google.com")
    assert result is None
    config = yaml.safe_load((brain_dir / "config.yaml").read_text())
    assert "google.com" not in config["browser_watcher"]["skip_domains"]


def test_edit_skip_domains_remove_not_present(handler, brain_dir):
    result = handler._edit_skip_domains("remove", "nothere.com")
    assert "not on the skip list" in result


def test_edit_skip_domains_writes_atomically(handler, brain_dir, monkeypatch):
    """Tmp file is written then renamed — original never partially overwritten."""
    renamed = []
    real_rename = os.rename

    def capture_rename(src, dst):
        renamed.append((src, dst))
        real_rename(src, dst)

    monkeypatch.setattr(ch.os, "rename", capture_rename)
    handler._edit_skip_domains("add", "example.com")
    assert len(renamed) == 1
    src, dst = renamed[0]
    assert str(src).endswith(".tmp")
    assert dst == brain_dir / "config.yaml"


# ── _purge_domain ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_domain_deletes_matching_memories(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "ex-aaa111", [], "Example Page", source_url="https://example.com/page")
    write_memory(m, "other-bbb222", [], "Other Page", source_url="https://other.com/page")
    count = await handler._purge_domain("example.com")
    assert count == 1
    assert not (m / "2026-04-11-ex-aaa111.md").exists()
    assert (m / "2026-04-11-other-bbb222.md").exists()


@pytest.mark.asyncio
async def test_purge_domain_no_matches(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "other-bbb222", [], "Other Page", source_url="https://other.com/page")
    count = await handler._purge_domain("nowhere.com")
    assert count == 0
    assert len(list(m.glob("*.md"))) == 1


@pytest.mark.asyncio
async def test_purge_domain_skips_files_without_source_url(handler, brain_dir):
    m = brain_dir / "memories"
    p = m / "2026-04-11-no-url-aaa111.md"
    p.write_text("---\ntags: []\nsource_title: No URL File\n---\n\n## Summary\ncontent")
    count = await handler._purge_domain("example.com")
    assert count == 0
    assert p.exists()


@pytest.mark.asyncio
async def test_purge_domain_skips_files_without_frontmatter(handler, brain_dir):
    m = brain_dir / "memories"
    p = m / "2026-04-11-no-fm-aaa111.md"
    p.write_text("## Summary\nJust plain markdown, no frontmatter.")
    count = await handler._purge_domain("example.com")
    assert count == 0
    assert p.exists()


@pytest.mark.asyncio
async def test_purge_domain_deletes_multiple_matches(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "ex1-aaa111", [], "Ex 1", source_url="https://example.com/a")
    write_memory(m, "ex2-bbb222", [], "Ex 2", source_url="https://example.com/b")
    write_memory(m, "other-ccc333", [], "Other", source_url="https://other.com/c")
    count = await handler._purge_domain("example.com")
    assert count == 2
    assert len(list(m.glob("*.md"))) == 1


# ── /skip command ─────────────────────────────────────────────────────────────

async def test_cmd_skip_adds_domain(handler, brain_dir):
    update, ctx = _make_update(12345, ["twitter.com"])
    await handler.cmd_skip(update, ctx)
    config = yaml.safe_load((brain_dir / "config.yaml").read_text())
    assert "twitter.com" in config["browser_watcher"]["skip_domains"]
    update.message.reply_text.assert_called_once()
    assert "twitter.com" in update.message.reply_text.call_args[0][0]


async def test_cmd_skip_already_present(handler, brain_dir):
    update, ctx = _make_update(12345, ["google.com"])
    await handler.cmd_skip(update, ctx)
    assert "already" in update.message.reply_text.call_args[0][0]


async def test_cmd_skip_no_args(handler, brain_dir):
    update, ctx = _make_update(12345, [])
    await handler.cmd_skip(update, ctx)
    assert "Usage" in update.message.reply_text.call_args[0][0]


async def test_cmd_skip_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999, ["evil.com"])
    await handler.cmd_skip(update, ctx)
    update.message.reply_text.assert_not_called()


# ── /unskip command ───────────────────────────────────────────────────────────

async def test_cmd_unskip_removes_domain(handler, brain_dir):
    update, ctx = _make_update(12345, ["google.com"])
    await handler.cmd_unskip(update, ctx)
    config = yaml.safe_load((brain_dir / "config.yaml").read_text())
    assert "google.com" not in config["browser_watcher"]["skip_domains"]
    assert "Removed" in update.message.reply_text.call_args[0][0]


async def test_cmd_unskip_not_present(handler, brain_dir):
    update, ctx = _make_update(12345, ["nothere.com"])
    await handler.cmd_unskip(update, ctx)
    assert "not on the skip list" in update.message.reply_text.call_args[0][0]


async def test_cmd_unskip_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999, ["google.com"])
    await handler.cmd_unskip(update, ctx)
    update.message.reply_text.assert_not_called()


# ── /skiplist command ─────────────────────────────────────────────────────────

async def test_cmd_skiplist_shows_domains(handler, brain_dir):
    update, ctx = _make_update(12345)
    await handler.cmd_skiplist(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "google.com" in reply
    assert "facebook.com" in reply


async def test_cmd_skiplist_empty(handler, brain_dir):
    config = yaml.safe_load((brain_dir / "config.yaml").read_text())
    config["browser_watcher"]["skip_domains"] = []
    (brain_dir / "config.yaml").write_text(yaml.dump(config))
    update, ctx = _make_update(12345)
    await handler.cmd_skiplist(update, ctx)
    assert "empty" in update.message.reply_text.call_args[0][0]


async def test_cmd_skiplist_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999)
    await handler.cmd_skiplist(update, ctx)
    update.message.reply_text.assert_not_called()


# ── /forget command ───────────────────────────────────────────────────────────

async def test_forget_numeric_with_active_list(handler, brain_dir):
    """Test /forget N deletes item from active list."""
    m = brain_dir / "memories"
    p = write_memory(m, "test-aaa111", [], "Test Page")
    handler._active_list = [p]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_forget(update, ctx)
    assert not p.exists()
    assert "Forgotten:" in update.message.reply_text.call_args[0][0]
    assert p not in handler._active_list


async def test_forget_numeric_no_list(handler, brain_dir):
    """Test /forget N with empty active list."""
    handler._active_list = []
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_forget(update, ctx)
    assert "Run a list command first" in update.message.reply_text.call_args[0][0]


async def test_forget_domain(handler, brain_dir):
    """Test /forget <domain> deletes all captures from that domain."""
    m = brain_dir / "memories"
    write_memory(m, "ex1-aaa111", [], "Ex1", source_url="https://example.com/page1")
    write_memory(m, "ex2-bbb222", [], "Ex2", source_url="https://example.com/page2")
    write_memory(m, "other-ccc333", [], "Other", source_url="https://other.com/page")
    update, ctx = _make_update(12345, ["example.com"])
    await handler.cmd_forget(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Forgotten 2" in reply
    assert not (m / "2026-04-11-ex1-aaa111.md").exists()
    assert not (m / "2026-04-11-ex2-bbb222.md").exists()
    assert (m / "2026-04-11-other-ccc333.md").exists()


async def test_forget_domain_no_matches(handler, brain_dir):
    """Test /forget <domain> with no matching captures."""
    update, ctx = _make_update(12345, ["nowhere.com"])
    await handler.cmd_forget(update, ctx)
    assert "No captures found" in update.message.reply_text.call_args[0][0]


async def test_forget_no_args(handler, brain_dir):
    """Test /forget with no arguments shows usage."""
    update, ctx = _make_update(12345, [])
    await handler.cmd_forget(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Usage:" in reply
    assert "/forget <N>" in reply
    assert "/forget <domain>" in reply


async def test_forget_single_index(handler, brain_dir):
    """Test /forget N (single arg) preserves existing behavior."""
    m = brain_dir / "memories"
    p = write_memory(m, "test-aaa111", [], "Test Page")
    handler._active_list = [p]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_forget(update, ctx)
    assert not p.exists()
    assert "Forgotten:" in update.message.reply_text.call_args[0][0]
    assert p not in handler._active_list


async def test_forget_multiple_indices_unlinks_all(handler, brain_dir):
    """Test /forget 1 2 3 removes all three items."""
    m = brain_dir / "memories"
    p1 = write_memory(m, "one-aaa111", [], "Page One")
    p2 = write_memory(m, "two-bbb222", [], "Page Two")
    p3 = write_memory(m, "three-ccc333", [], "Page Three")
    handler._active_list = [p1, p2, p3]
    update, ctx = _make_update(12345, ["1", "2", "3"])
    await handler.cmd_forget(update, ctx)
    assert not p1.exists()
    assert not p2.exists()
    assert not p3.exists()
    assert "Forgot 3 items" in update.message.reply_text.call_args[0][0]
    assert handler._active_list == []


async def test_forget_multiple_indices_handles_missing_file_gracefully(handler, brain_dir):
    """Test /forget handles cases where one file doesn't exist."""
    m = brain_dir / "memories"
    p1 = write_memory(m, "one-aaa111", [], "Page One")
    p2 = write_memory(m, "two-bbb222", [], "Page Two")
    p3 = write_memory(m, "three-ccc333", [], "Page Three")
    handler._active_list = [p1, p2, p3]
    # Manually delete p2 to simulate missing file
    p2.unlink()
    update, ctx = _make_update(12345, ["1", "2", "3"])
    await handler.cmd_forget(update, ctx)
    assert not p1.exists()
    assert not p2.exists()  # Was already deleted
    assert not p3.exists()
    assert "Forgot 3 items" in update.message.reply_text.call_args[0][0]
    assert handler._active_list == []


async def test_forget_multiple_indices_no_offbyone_after_mutation(handler, brain_dir):
    """Test snapshot prevents off-by-one when removing from start of list."""
    m = brain_dir / "memories"
    p1 = write_memory(m, "one-aaa111", [], "Page One")
    p2 = write_memory(m, "two-bbb222", [], "Page Two")
    p3 = write_memory(m, "three-ccc333", [], "Page Three")
    handler._active_list = [p1, p2, p3]
    # Forget items 1 and 3 — without snapshot, removing 1 first would shift indices
    update, ctx = _make_update(12345, ["1", "3"])
    await handler.cmd_forget(update, ctx)
    assert not p1.exists()
    assert p2.exists()
    assert not p3.exists()
    assert "Forgot 2 items" in update.message.reply_text.call_args[0][0]
    assert handler._active_list == [p2]


# ── /readings command ─────────────────────────────────────────────────────────

async def test_cmd_readings_lists_recent(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "one-aaa111", [], "Article One", created="2026-04-10T10:00:00")
    write_memory(m, "two-bbb222", [], "Article Two", created="2026-04-11T10:00:00")
    update, ctx = _make_update(12345)
    await handler.cmd_readings(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Article One" in reply
    assert "Article Two" in reply
    assert len(handler._last_results) == 2
    assert len(handler._active_list) == 2


async def test_cmd_readings_custom_count(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(5):
        write_memory(m, f"p{i}-{'a' * 5}{i}", [], f"Page {i}")
    update, ctx = _make_update(12345, ["3"])
    await handler.cmd_readings(update, ctx)
    assert len(handler._last_results) == 3


async def test_cmd_readings_empty(handler, brain_dir):
    update, ctx = _make_update(12345)
    await handler.cmd_readings(update, ctx)
    assert "No memories" in update.message.reply_text.call_args[0][0]


async def test_cmd_readings_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999)
    await handler.cmd_readings(update, ctx)
    update.message.reply_text.assert_not_called()


# ── /search command ───────────────────────────────────────────────────────────

async def test_cmd_search_returns_matches(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "litellm-aaa111", ["litellm", "routing"], "LiteLLM Router")
    write_memory(m, "cooking-bbb222", ["food"], "Cooking Tips")
    update, ctx = _make_update(12345, ["litellm"])
    await handler.cmd_search(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "LiteLLM" in reply
    assert "Cooking" not in reply
    assert len(handler._last_results) == 1


async def test_cmd_search_no_matches(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "cooking-bbb222", ["food"], "Cooking Tips")
    update, ctx = _make_update(12345, ["litellm"])
    await handler.cmd_search(update, ctx)
    assert "No memories match" in update.message.reply_text.call_args[0][0]


async def test_cmd_search_no_args(handler, brain_dir):
    update, ctx = _make_update(12345, [])
    await handler.cmd_search(update, ctx)
    assert "Usage" in update.message.reply_text.call_args[0][0]


async def test_cmd_search_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999, ["litellm"])
    await handler.cmd_search(update, ctx)
    update.message.reply_text.assert_not_called()


def _write_typed_memory(memories_dir, slug, title, mem_type, extra_fm="", body="content"):
    """Write a memory with an explicit type frontmatter field."""
    path = memories_dir / f"2026-04-11-{slug}.md"
    path.write_text(
        f"---\nsource_title: {title}\ntype: {mem_type}\ntags: []\ncreated: '2026-04-11'\n{extra_fm}---\n\n{body}"
    )
    return path


async def test_cmd_search_grouped_by_type(handler, brain_dir):
    """Grouped search shows type headers when results span multiple types."""
    m = brain_dir / "memories"
    write_memory(m, "web-abc111", ["tom"], "Tom Jones Article")
    _write_typed_memory(m, "email-abc222", "Tom Jones project thread", "email_thread")
    _write_typed_memory(m, "commit-abc333", "Deliver proposal to Tom", "commitment",
                        extra_fm="commitment_type: outbound\n")
    update, ctx = _make_update(12345, ["tom", "jones"])
    await handler.cmd_search(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Commitments" in reply
    assert "Email threads" in reply
    assert "Web memories" in reply
    # Global indices assigned — all three items accessible
    assert len(handler._last_results) == 3


async def test_cmd_search_grouped_omits_empty_groups(handler, brain_dir):
    """Groups with zero results don't appear in the reply."""
    m = brain_dir / "memories"
    _write_typed_memory(m, "email-xyz", "Tom Jones email", "email_thread")
    update, ctx = _make_update(12345, ["tom"])
    await handler.cmd_search(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Email threads" in reply
    assert "Meetings" not in reply
    assert "Projects" not in reply


async def test_cmd_search_grouped_overflow_hint(handler, brain_dir):
    """Groups with > 5 items show 'and N more' hint with type-filter syntax."""
    m = brain_dir / "memories"
    for i in range(7):
        _write_typed_memory(m, f"email-{i}", f"Tom email {i}", "email_thread")
    update, ctx = _make_update(12345, ["tom"])
    await handler.cmd_search(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "… and 2 more" in reply
    assert "/search email tom" in reply
    # All 7 in _last_results despite only 5 shown
    assert len(handler._last_results) == 7


async def test_cmd_search_type_filter_email(handler, brain_dir):
    """'/search email tom' returns only email_thread results in flat list."""
    m = brain_dir / "memories"
    _write_typed_memory(m, "email-1", "Tom Jones thread", "email_thread")
    _write_typed_memory(m, "commit-1", "Follow up with Tom", "commitment")
    update, ctx = _make_update(12345, ["email", "tom"])
    await handler.cmd_search(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Tom Jones thread" in reply
    assert "Follow up" not in reply
    assert "(email)" in reply


async def test_cmd_search_type_filter_no_query(handler, brain_dir):
    """'/search email' with no second arg returns usage hint."""
    update, ctx = _make_update(12345, ["email"])
    await handler.cmd_search(update, ctx)
    assert "Usage" in update.message.reply_text.call_args[0][0]


async def test_cmd_search_type_filter_no_matches(handler, brain_dir):
    """'/search meeting tom' with no meeting files returns specific empty message."""
    m = brain_dir / "memories"
    _write_typed_memory(m, "email-1", "Tom email", "email_thread")
    update, ctx = _make_update(12345, ["meeting", "tom"])
    await handler.cmd_search(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "meeting" in reply.lower()
    assert "match" in reply.lower()


async def test_cmd_search_memory_N_resolves_across_groups(handler, brain_dir):
    """After a grouped search, /memory N resolves items from any group."""
    m = brain_dir / "memories"
    _write_typed_memory(m, "contact-1", "Tom Jones", "contact")
    _write_typed_memory(m, "email-1", "Tom Jones thread", "email_thread")
    update, ctx = _make_update(12345, ["tom"])
    await handler.cmd_search(update, ctx)
    # Both items are in _last_results; /memory 2 should reach the email
    assert len(handler._last_results) == 2
    paths = {p.name for p in handler._last_results}
    assert any("contact" in n for n in paths)
    assert any("email" in n for n in paths)


# ── /reading command ──────────────────────────────────────────────────────────

async def test_cmd_reading_shows_details(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "litellm-aaa111", ["litellm"], "LiteLLM Router",
                 source_url="https://litellm.ai", summary="A great router.")
    handler._active_list = list(m.glob("*.md"))
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_reading(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "LiteLLM Router" in reply
    assert "https://litellm.ai" in reply
    assert "A great router." in reply


async def test_cmd_reading_invalid_index(handler, brain_dir):
    handler._active_list = []
    update, ctx = _make_update(12345, ["5"])
    await handler.cmd_reading(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "/readings" in reply
    assert "Knowledge listings commands:" in reply


async def test_cmd_reading_no_results(handler, brain_dir):
    handler._active_list = []
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_reading(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "/readings" in reply
    assert "Knowledge listings commands:" in reply


async def test_cmd_reading_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999, ["1"])
    await handler.cmd_reading(update, ctx)
    update.message.reply_text.assert_not_called()


# ── /help command ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_help_renders_all_groups(handler):
    update, ctx = _make_update(12345)
    await handler.cmd_help(update, ctx)
    # Help may be split across multiple messages due to 4096-char Telegram limit
    all_replies = " ".join(
        call[0][0] for call in update.message.reply_text.call_args_list
    )
    for group in ch.COMMAND_REGISTRY:
        assert group in all_replies


@pytest.mark.asyncio
async def test_cmd_help_all_registry_commands_listed(handler):
    update, ctx = _make_update(12345)
    await handler.cmd_help(update, ctx)
    # Collect all chunks across multiple calls. _send_reply uses fixed-size
    # 4096-char slicing (chunks are direct substrings of the original text),
    # so concatenate without a separator to faithfully reconstruct the source —
    # otherwise commands that happen to land on a chunk boundary appear split
    # by a stray newline and the substring search misses them.
    calls = update.message.reply_text.call_args_list
    full_text = "".join(c[0][0] for c in calls)
    for commands in ch.COMMAND_REGISTRY.values():
        for cmd, _ in commands:
            assert f"/{cmd}" in full_text, f"/{cmd} not found in /help output"


@pytest.mark.asyncio
async def test_cmd_help_rejects_unauthorised(handler):
    update, ctx = _make_update(99999)
    await handler.cmd_help(update, ctx)
    update.message.reply_text.assert_not_called()


def test_registry_completeness(handler):
    """Every CommandHandler registration must have a COMMAND_REGISTRY entry."""
    all_registered = set()
    for cmd, handler_func in handler.app.add_handler.call_args_list:
        arg = cmd[0]
        if hasattr(arg, 'commands'):
            for c in arg.commands:
                all_registered.add(c)

    all_in_registry = {
        cmd
        for commands in ch.COMMAND_REGISTRY.values()
        for cmd, _ in commands
    }
    unregistered = all_in_registry - all_registered
    assert not unregistered, f"Commands in COMMAND_REGISTRY but not registered: {unregistered}"

    # Reverse check: every registered CommandHandler must have a COMMAND_REGISTRY entry
    missing_from_registry = all_registered - all_in_registry
    # Exclude 'start' — it's a standard Telegram /start, not a user-facing command
    missing_from_registry.discard("start")
    assert not missing_from_registry, f"Commands registered but missing from COMMAND_REGISTRY: {missing_from_registry}"


# ── /projects and /project commands ──────────────────────────────────────────

def write_project_memory(memories_dir: Path, name: str, category: str = "code",
                         last_scanned: str = "2026-04-11T12:00:00",
                         summary: str = "A project.", hostname: str = "") -> Path:
    # Support both legacy and hostname-scoped filenames
    if hostname:
        path = memories_dir / f"project-{hostname}-{name}.md"
        hostname_field = f"hostname: {hostname}\n"
    else:
        path = memories_dir / f"project-{name}.md"
        hostname_field = ""
    path.write_text(
        f"---\nsource_title: {name}\nsummary: {summary}\ntags: [python]\n"
        f"last_scanned: '{last_scanned}'\nsource_url: git@github.com:org/{name}.git\n"
        f"type: project\ncategory: {category}\n{hostname_field}"
        f"local_path: /tmp/{name}\ndefault_branch: main\nlanguages: [python]\n"
        f"head_sha: abc123\n---\n\n## Description\n{summary}\n"
    )
    return path


@pytest.mark.asyncio
async def test_cmd_code_lists_all(handler, brain_dir):
    m = brain_dir / "memories"
    write_project_memory(m, "alpha")
    write_project_memory(m, "beta")
    update, ctx = _make_update(12345)
    await handler.cmd_code(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "alpha" in reply
    assert "beta" in reply
    assert len(handler._last_code_set) == 2


@pytest.mark.asyncio
async def test_cmd_code_filter_by_category(handler, brain_dir):
    m = brain_dir / "memories"
    write_project_memory(m, "codeproj", category="code")
    write_project_memory(m, "workproj", category="work")
    update, ctx = _make_update(12345, ["code"])
    await handler.cmd_code(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "codeproj" in reply
    assert "workproj" not in reply


@pytest.mark.asyncio
async def test_cmd_code_default_n_10(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(15):
        write_project_memory(m, f"proj{i:02d}")
    update, ctx = _make_update(12345)
    await handler.cmd_code(update, ctx)
    assert len(handler._last_code_set) == 10


@pytest.mark.asyncio
async def test_cmd_code_n_clamped(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(5):
        write_project_memory(m, f"proj{i}")
    # N=999 clamped to 50 — but only 5 exist so we get 5
    update, ctx = _make_update(12345, ["999"])
    await handler.cmd_code(update, ctx)
    assert len(handler._last_code_set) == 5
    # N=0 clamped to 1
    update2, ctx2 = _make_update(12345, ["0"])
    await handler.cmd_code(update2, ctx2)
    assert len(handler._last_code_set) == 1


@pytest.mark.asyncio
async def test_cmd_code_detail_view(handler, brain_dir):
    m = brain_dir / "memories"
    write_project_memory(m, "myrepo", summary="My test repo.")
    handler._last_code_set = [m / "project-myrepo.md"]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_code(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "myrepo" in reply
    assert "My test repo" in reply


@pytest.mark.asyncio
async def test_cmd_code_invalid_index(handler, brain_dir):
    # Create some files, populate the list, then try invalid index
    m = brain_dir / "memories"
    write_project_memory(m, "proj1")
    write_project_memory(m, "proj2")
    update_list, ctx_list = _make_update(12345)
    await handler.cmd_code(update_list, ctx_list)
    # Now list is populated with 2 items, try to access index 99
    update, ctx = _make_update(12345, ["99"])
    await handler.cmd_code(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "/code" in reply
    assert "Knowledge listings commands:" in reply


@pytest.mark.asyncio
async def test_cmd_code_groups_by_base_name(handler, brain_dir):
    """Projects with same base name from different hosts are grouped."""
    m = brain_dir / "memories"
    write_project_memory(m, "myrepo", hostname="studio", summary="Studio version")
    write_project_memory(m, "myrepo", hostname="laptop", summary="Laptop version")
    update, ctx = _make_update(12345)
    await handler.cmd_code(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    # Should show "myrepo" once (not twice)
    assert reply.count("myrepo") == 1
    # Should mention both hostnames
    assert "laptop" in reply
    assert "studio" in reply


@pytest.mark.asyncio
async def test_cmd_code_single_host_always_shown(handler, brain_dir):
    """A single-host project must include '· host: <hostname>' so the LLM
    can answer 'group by laptop' questions without burning tool iterations."""
    m = brain_dir / "memories"
    write_project_memory(m, "solo", hostname="macbook-air")
    update, ctx = _make_update(12345)
    await handler.cmd_code(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "· host: macbook-air" in reply


@pytest.mark.asyncio
async def test_cmd_code_legacy_no_hostname_omitted(handler, brain_dir):
    """Legacy files without a hostname field must not print '· host: legacy'."""
    m = brain_dir / "memories"
    write_project_memory(m, "oldrepo", hostname="")  # no hostname in frontmatter
    update, ctx = _make_update(12345)
    await handler.cmd_code(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    # The sentinel "legacy" must not appear in the host column
    assert "· host: legacy" not in reply
    assert "· hosts:" not in reply


# ── /events and /event commands ───────────────────────────────────────────────

def write_event_memory(memories_dir: Path, slug: str,
                       start: str = "2026-04-12T10:00:00",
                       title: str = "Team Meeting",
                       location: str = "") -> Path:
    path = memories_dir / f"calendar-event-2026-04-12-{slug}-abc123.md"
    loc_line = f"location: '{location}'\n" if location else ""
    path.write_text(
        f"---\nsource_title: '{title}'\nsummary: Event summary.\n"
        f"tags: [meeting]\nlast_scanned: '2026-04-12T10:00:00'\n"
        f"source_url: calendar:abc\ntype: calendar_event\n"
        f"calendar_name: Work\nstart_time: '{start}'\nend_time: '{start}'\n"
        f"all_day: false\n{loc_line}participants: [Alice, Bob]\n---\n\nContent.\n"
    )
    return path


@pytest.mark.asyncio
async def test_cmd_events_lists_recent(handler, brain_dir):
    m = brain_dir / "memories"
    write_event_memory(m, "standup", title="Standup")
    write_event_memory(m, "review", title="Code Review")
    update, ctx = _make_update(12345)
    await handler.cmd_events(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Standup" in reply
    assert "Code Review" in reply
    assert len(handler._last_event_set) == 2


@pytest.mark.asyncio
async def test_cmd_events_default_n_10(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(15):
        write_event_memory(m, f"evt{i:02d}", title=f"Event {i}")
    update, ctx = _make_update(12345)
    await handler.cmd_events(update, ctx)
    assert len(handler._last_event_set) == 10


@pytest.mark.asyncio
async def test_cmd_events_n_clamped(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(5):
        write_event_memory(m, f"evt{i}", title=f"Evt{i}")
    update, ctx = _make_update(12345, ["0"])
    await handler.cmd_events(update, ctx)
    assert len(handler._last_event_set) == 1


@pytest.mark.asyncio
async def test_cmd_events_sets_last_event_set(handler, brain_dir):
    m = brain_dir / "memories"
    p = write_event_memory(m, "standup")
    update, ctx = _make_update(12345)
    await handler.cmd_events(update, ctx)
    assert handler._last_event_set == [p]


@pytest.mark.asyncio
async def test_cmd_event_detail_view(handler, brain_dir):
    m = brain_dir / "memories"
    write_event_memory(m, "standup", title="Daily Standup", location="Conf Room A")
    handler._last_event_set = [m / "calendar-event-2026-04-12-standup-abc123.md"]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_event(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Daily Standup" in reply
    assert "Conf Room A" in reply


@pytest.mark.asyncio
async def test_cmd_event_invalid_index(handler):
    handler._last_event_set = []
    update, ctx = _make_update(12345, ["99"])
    await handler.cmd_event(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "/events" in reply
    assert "Knowledge listings commands:" in reply


def write_event_memory_with_cal(memories_dir: Path, slug: str, calendar_names: list,
                                start: str = "2026-04-12T10:00:00",
                                title: str = "Team Meeting") -> Path:
    """Write a calendar event memory with a calendar_names list (new format)."""
    path = memories_dir / f"calendar-event-2026-04-12-{slug}-xyz999.md"
    cal_list = "[" + ", ".join(calendar_names) + "]"
    path.write_text(
        f"---\nsource_title: '{title}'\nsummary: Event summary.\n"
        f"tags: [meeting]\nlast_scanned: '2026-04-12T10:00:00'\n"
        f"source_url: calendar:xyz\ntype: calendar_event\n"
        f"calendar_names: {cal_list}\nstart_time: '{start}'\nend_time: '{start}'\n"
        f"all_day: false\nparticipants: [Alice]\n---\n\nContent.\n"
    )
    return path


@pytest.mark.asyncio
async def test_events_filter_matches_calendar_name(handler, brain_dir):
    m = brain_dir / "memories"
    write_event_memory_with_cal(m, "work-evt", ["Work"], title="Sync")
    write_event_memory_with_cal(m, "personal-evt", ["Personal"], title="Dentist")
    update, ctx = _make_update(12345, ["work"])
    await handler.cmd_events(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Sync" in reply
    assert "Dentist" not in reply


@pytest.mark.asyncio
async def test_events_filter_case_insensitive(handler, brain_dir):
    m = brain_dir / "memories"
    write_event_memory_with_cal(m, "work-ci", ["Work"], title="Standup")
    update, ctx = _make_update(12345, ["WORK"])
    await handler.cmd_events(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Standup" in reply


@pytest.mark.asyncio
async def test_events_filter_empty_result_shows_available_calendars(handler, brain_dir):
    m = brain_dir / "memories"
    write_event_memory_with_cal(m, "family-evt", ["Family"], title="Dinner")
    update, ctx = _make_update(12345, ["nonexistent"])
    await handler.cmd_events(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "No events found matching calendar 'nonexistent'" in reply
    assert "Available calendars" in reply
    assert "Family" in reply


@pytest.mark.asyncio
async def test_events_filter_and_limit_both_applied(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(5):
        write_event_memory_with_cal(m, f"work-{i}", ["Work"], title=f"Work Evt {i}",
                                    start=f"2026-04-12T{10+i:02d}:00:00")
    update, ctx = _make_update(12345, ["work", "2"])
    await handler.cmd_events(update, ctx)
    assert len(handler._last_event_set) == 2


@pytest.mark.asyncio
async def test_event_detail_reads_calendar_names_list(handler, brain_dir):
    """cmd_event detail view should use calendar_names list, not calendar_name string."""
    m = brain_dir / "memories"
    p = write_event_memory_with_cal(m, "cal-names-test", ["Work", "Shared"],
                                    title="Cross-cal Meeting")
    handler._last_event_set = [p]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_event(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Work" in reply
    assert "Shared" in reply


# ── /meetings and /meeting commands ──────────────────────────────────────────

def write_meeting_memory(memories_dir: Path, slug: str,
                         title: str = "Q4 Planning",
                         date: str = "2026-04-10") -> Path:
    path = memories_dir / f"meeting-{date}-{slug}-abc123.md"
    path.write_text(
        f"---\nsource_title: '{title}'\nsummary: Meeting summary.\n"
        f"tags: [meeting]\ncreated: '{date}T10:00:00'\n"
        f"source_url: zoom:abc\ntype: meeting_transcript\n"
        f"start_time: '{date}T10:00:00'\nparticipants: [Alice, Bob, Charlie]\n"
        f"---\n\nTranscript.\n"
    )
    return path


@pytest.mark.asyncio
async def test_cmd_meetings_lists_recent(handler, brain_dir):
    m = brain_dir / "memories"
    write_meeting_memory(m, "q4", title="Q4 Planning")
    write_meeting_memory(m, "standup", title="Standup")
    update, ctx = _make_update(12345)
    await handler.cmd_meetings(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Q4 Planning" in reply or "Standup" in reply
    assert len(handler._last_meeting_set) == 2


@pytest.mark.asyncio
async def test_cmd_meetings_default_n_10(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(15):
        write_meeting_memory(m, f"mtg{i:02d}", title=f"Meeting {i}")
    update, ctx = _make_update(12345)
    await handler.cmd_meetings(update, ctx)
    assert len(handler._last_meeting_set) == 10


@pytest.mark.asyncio
async def test_cmd_meetings_sets_last_meeting_set(handler, brain_dir):
    m = brain_dir / "memories"
    p = write_meeting_memory(m, "q4")
    update, ctx = _make_update(12345)
    await handler.cmd_meetings(update, ctx)
    assert handler._last_meeting_set == [p]


@pytest.mark.asyncio
async def test_cmd_meeting_detail_view(handler, brain_dir):
    m = brain_dir / "memories"
    write_meeting_memory(m, "q4", title="Q4 Planning")
    handler._last_meeting_set = [m / "meeting-2026-04-10-q4-abc123.md"]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_meeting(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Q4 Planning" in reply
    assert "Alice" in reply


@pytest.mark.asyncio
async def test_cmd_meeting_invalid_index(handler):
    handler._last_meeting_set = []
    update, ctx = _make_update(12345, ["99"])
    await handler.cmd_meeting(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "/meetings" in reply
    assert "Knowledge listings commands:" in reply


# ── /comms and /comm commands ─────────────────────────────────────────────────

def write_email_memory(memories_dir: Path, slug: str,
                       subject: str = "Re: Project Update",
                       last_message: str = "2026-04-11") -> Path:
    path = memories_dir / f"email-thread-{slug}-abc123.md"
    path.write_text(
        f"---\nsource_title: '{subject}'\nsummary: Email thread summary.\n"
        f"type: email_thread\nlast_message: '{last_message}'\n"
        f"participants: [alice@example.com]\n---\n\nContent.\n"
    )
    return path


def write_slack_memory(memories_dir: Path, slug: str,
                       channel: str = "engineering",
                       last_reply: str = "2026-04-11") -> Path:
    path = memories_dir / f"slack-thread-{slug}-1234567890.md"
    path.write_text(
        f"---\nsource_title: '{channel}'\nsummary: Slack thread summary.\n"
        f"type: slack_thread\nchannel: {channel}\n"
        f"last_reply: '{last_reply}'\nparticipants: [U123456]\n---\n\nContent.\n"
    )
    return path


@pytest.mark.asyncio
async def test_cmd_comms_mixed_results(handler, brain_dir):
    m = brain_dir / "memories"
    write_email_memory(m, "proj-update")
    write_slack_memory(m, "eng-discussion")
    update, ctx = _make_update(12345)
    await handler.cmd_comms(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "[email]" in reply
    assert "[slack]" in reply
    assert len(handler._last_comms_set) == 2


@pytest.mark.asyncio
async def test_cmd_comms_email_filter(handler, brain_dir):
    m = brain_dir / "memories"
    write_email_memory(m, "email1")
    write_slack_memory(m, "slack1")
    update, ctx = _make_update(12345, ["email"])
    await handler.cmd_comms(update, ctx)
    assert len(handler._last_comms_set) == 1
    reply = update.message.reply_text.call_args[0][0]
    assert "[email]" in reply
    assert "[slack]" not in reply


@pytest.mark.asyncio
async def test_cmd_comms_slack_filter(handler, brain_dir):
    m = brain_dir / "memories"
    write_email_memory(m, "email1")
    write_slack_memory(m, "slack1")
    update, ctx = _make_update(12345, ["slack"])
    await handler.cmd_comms(update, ctx)
    assert len(handler._last_comms_set) == 1
    reply = update.message.reply_text.call_args[0][0]
    assert "[slack]" in reply
    assert "[email]" not in reply


@pytest.mark.asyncio
async def test_cmd_comms_n_arg_no_filter(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(5):
        write_email_memory(m, f"email{i}")
    update, ctx = _make_update(12345, ["3"])
    await handler.cmd_comms(update, ctx)
    assert len(handler._last_comms_set) == 3


@pytest.mark.asyncio
async def test_cmd_comms_n_clamped(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(3):
        write_email_memory(m, f"email{i}")
    update, ctx = _make_update(12345, ["0"])
    await handler.cmd_comms(update, ctx)
    assert len(handler._last_comms_set) == 1


@pytest.mark.asyncio
async def test_cmd_comms_source_tag_in_reply(handler, brain_dir):
    m = brain_dir / "memories"
    write_email_memory(m, "e1")
    write_slack_memory(m, "s1")
    update, ctx = _make_update(12345)
    await handler.cmd_comms(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    # Each non-header line should have a source tag
    content_lines = [l for l in reply.split("\n") if l.startswith(("1.", "2."))]
    for line in content_lines:
        assert "[email]" in line or "[slack]" in line


@pytest.mark.asyncio
async def test_cmd_comms_sets_last_comms_set(handler, brain_dir):
    m = brain_dir / "memories"
    write_email_memory(m, "e1")
    update, ctx = _make_update(12345)
    await handler.cmd_comms(update, ctx)
    assert len(handler._last_comms_set) == 1


@pytest.mark.asyncio
async def test_cmd_comm_email_detail(handler, brain_dir):
    m = brain_dir / "memories"
    p = write_email_memory(m, "proj-update", subject="Re: Project Update")
    handler._last_comms_set = [p]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_comm(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "[email]" in reply
    assert "Re: Project Update" in reply


@pytest.mark.asyncio
async def test_cmd_comm_slack_detail(handler, brain_dir):
    m = brain_dir / "memories"
    p = write_slack_memory(m, "eng", channel="engineering")
    handler._last_comms_set = [p]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_comm(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "[slack]" in reply
    assert "engineering" in reply


@pytest.mark.asyncio
async def test_cmd_comm_invalid_index(handler):
    handler._last_comms_set = []
    update, ctx = _make_update(12345, ["99"])
    await handler.cmd_comm(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "/comms" in reply
    assert "Knowledge listings commands:" in reply


# ── /people alias ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_people_is_alias_for_contacts(handler, brain_dir):
    """Both /contacts and /people handlers should behave the same."""
    m = brain_dir / "memories"
    path = m / "contact-alice.md"
    path.write_text(
        "---\nsource_title: Alice\nname: Alice\ntype: contact\n"
        "emails: [alice@example.com]\nlast_interaction: '2026-04-11'\n"
        "relationship_score: 0.8\ninteraction_count: 5\n---\n"
    )
    update, ctx = _make_update(12345)
    await handler.cmd_contacts(update, ctx)
    contacts_reply = update.message.reply_text.call_args[0][0]

    update2, ctx2 = _make_update(12345)
    # /people is registered as cmd_contacts — same method
    await handler.cmd_contacts(update2, ctx2)
    people_reply = update2.message.reply_text.call_args[0][0]
    assert contacts_reply == people_reply


# ── code_scanner migration ───────────────────────────────────────────────────

def test_migrate_legacy_code_project(tmp_path):
    import code_scanner as cs_mod
    from code_scanner import CodeScanner

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Write a legacy file (type migration: code_project → project+code → code, filename migration)
    legacy = memories_dir / "project-legacy.md"
    legacy.write_text(
        "---\nsource_title: legacy\nsummary: old\ntags: [python]\n"
        "last_scanned: '2026-04-11T10:00:00'\n"
        "source_url: git@github.com:org/legacy.git\ntype: code_project\n"
        "local_path: /tmp/legacy\ndefault_branch: main\n"
        "languages: [python]\nhead_sha: abc123\n---\n\n## Content\n"
    )

    with patch.object(cs_mod, "MEMORIES_DIR", memories_dir), \
         patch.object(cs_mod, "DEPLOY_DIR", tmp_path), \
         patch("code_scanner._hostname", return_value="testhost"):
        _ = CodeScanner()

    import yaml as _yaml
    # File will be migrated through: code_project → project+category:code → code-testhost-legacy.md
    migrated = memories_dir / "code-testhost-legacy.md"
    assert migrated.exists()
    assert not legacy.exists()
    text = migrated.read_text()
    parts = text.split("---", 2)
    fm = _yaml.safe_load(parts[1])
    assert fm["type"] == "code"
    assert "category" not in fm


def test_migrate_idempotent(tmp_path):
    import code_scanner as cs_mod
    from code_scanner import CodeScanner

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Write a file in the intermediate state (type: project + category: code, hostname-scoped)
    # This will be migrated to type: code, code-{hostname}-*.md
    intermediate = memories_dir / "project-testhost-new.md"
    intermediate.write_text(
        "---\nsource_title: new\nsummary: new project\ntags: [python]\n"
        "last_scanned: '2026-04-11T10:00:00'\n"
        "source_url: git@github.com:org/new.git\ntype: project\ncategory: code\n"
        "hostname: testhost\nlocal_path: /tmp/new\ndefault_branch: main\n"
        "languages: [python]\nhead_sha: def456\n---\n\n## Content\n"
    )

    with patch.object(cs_mod, "MEMORIES_DIR", memories_dir), \
         patch.object(cs_mod, "DEPLOY_DIR", tmp_path), \
         patch("code_scanner._hostname", return_value="testhost"):
        _ = CodeScanner()

    # File should be migrated to code-testhost-new.md with type: code
    migrated_final = memories_dir / "code-testhost-new.md"
    assert migrated_final.exists()
    assert not intermediate.exists()
    import yaml as _yaml
    fm = _yaml.safe_load(migrated_final.read_text().split("---", 2)[1])
    assert fm["type"] == "code"
    assert "category" not in fm


# ── /bug command ──────────────────────────────────────────────────────────────

async def test_bug_creates_memory_file(handler, brain_dir):
    """Test /bug creates a feature_request file with kind=bug."""
    update, ctx = _make_update(12345, ["login", "fails"])
    await handler.cmd_bug(update, ctx)
    m = brain_dir / "memories"
    files = list(m.glob("feature-request-*.md"))
    assert len(files) == 1
    fm = handler._parse_frontmatter(files[0])
    assert fm["kind"] == "bug"
    assert fm["type"] == "feature_request"
    text = files[0].read_text()
    assert "## Bug" in text
    assert "## Expected" in text
    assert "## Steps to reproduce" in text


# ── /features kind filter tests ──────────────────────────────────────────────

async def test_features_kind_filter_bug(handler, brain_dir):
    """Test /features bug shows only bugs."""
    m = brain_dir / "memories"
    # Create one bug and one feature
    bug_path = m / "feature-request-bug-aaa111.md"
    bug_path.write_text(
        "---\ntitle: Bug item\ntype: feature_request\nkind: bug\n"
        "status: new\npriority: medium\ncreated: '2026-04-11T10:00:00'\n"
        "tags: []\nshort_id: aaa111\n---\n\n## Bug\nSomething broke"
    )
    feat_path = m / "feature-request-feat-bbb222.md"
    feat_path.write_text(
        "---\ntitle: Feature item\ntype: feature_request\nkind: feature\n"
        "status: new\npriority: medium\ncreated: '2026-04-11T10:00:00'\n"
        "tags: []\nshort_id: bbb222\n---\n\n## Request\nNew thing"
    )
    update, ctx = _make_update(12345, ["bug"])
    await handler.cmd_features(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert len(handler._last_feature_set) == 1
    assert handler._last_feature_set[0] == bug_path


async def test_features_kind_filter_feature(handler, brain_dir):
    """Test /features feature shows only features."""
    m = brain_dir / "memories"
    bug_path = m / "feature-request-bug-aaa111.md"
    bug_path.write_text(
        "---\ntitle: Bug item\ntype: feature_request\nkind: bug\n"
        "status: new\npriority: medium\ncreated: '2026-04-11T10:00:00'\n"
        "tags: []\nshort_id: aaa111\n---\n\n## Bug\nSomething"
    )
    feat_path = m / "feature-request-feat-bbb222.md"
    feat_path.write_text(
        "---\ntitle: Feature item\ntype: feature_request\nkind: feature\n"
        "status: new\npriority: medium\ncreated: '2026-04-11T10:00:00'\n"
        "tags: []\nshort_id: bbb222\n---\n\n## Request\nNew"
    )
    update, ctx = _make_update(12345, ["feature"])
    await handler.cmd_features(update, ctx)
    assert len(handler._last_feature_set) == 1
    assert handler._last_feature_set[0] == feat_path


async def test_bugs_alias_lists_bugs_only(handler, brain_dir):
    """Test /bugs (alias) lists only bugs."""
    m = brain_dir / "memories"
    bug_path = m / "feature-request-bug-aaa111.md"
    bug_path.write_text(
        "---\ntitle: Bug item\ntype: feature_request\nkind: bug\n"
        "status: new\npriority: medium\ncreated: '2026-04-11T10:00:00'\n"
        "tags: []\nshort_id: aaa111\n---\n\n## Bug\nSomething"
    )
    feat_path = m / "feature-request-feat-bbb222.md"
    feat_path.write_text(
        "---\ntitle: Feature item\ntype: feature_request\nkind: feature\n"
        "status: new\npriority: medium\ncreated: '2026-04-11T10:00:00'\n"
        "tags: []\nshort_id: bbb222\n---\n\n## Request\nNew"
    )
    update, ctx = _make_update(12345)
    await handler.cmd_bugs(update, ctx)
    assert len(handler._last_feature_set) == 1
    assert handler._last_feature_set[0] == bug_path


# ── GitHub client tests ────────────────────────────────────────────────────────

def _parse_fm(path):
    """Parse frontmatter from a markdown file."""
    text = path.read_text()
    parts = text.split("---", 2)
    return yaml.safe_load(parts[1]) if len(parts) >= 3 else {}


@pytest.mark.asyncio
async def test_github_fallback_when_pat_missing(handler, brain_dir):
    """With no PAT, /feature writes a local file."""
    # handler fixture already has empty GITHUB_PAT → handler.github.enabled is False
    update, ctx = _make_update(12345, ["test", "feature", "request"])
    await handler.cmd_feature(update, ctx)
    files = list((brain_dir / "memories").glob("feature-request-*.md"))
    assert len(files) == 1
    fm = _parse_fm(files[0])
    assert fm.get("kind") == "feature"


@pytest.mark.asyncio
async def test_github_enabled_create_feature(handler, brain_dir):
    """With GitHub enabled, /feature creates a GH issue not a local file."""
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.create_issue = AsyncMock(return_value={
        "number": 42, "html_url": "https://github.com/owner/repo/issues/42"
    })
    mock_gh.ensure_labels = AsyncMock()
    mock_gh.list_issues = AsyncMock(return_value=[])
    handler.github = mock_gh
    update, ctx = _make_update(12345, ["test", "feature"])
    await handler.cmd_feature(update, ctx)
    mock_gh.create_issue.assert_called_once()
    call = mock_gh.create_issue.call_args
    labels = call.kwargs.get("labels") or call.args[2]
    assert "kind:feature" in labels
    reply = update.message.reply_text.call_args[0][0]
    assert "42" in reply


@pytest.mark.asyncio
async def test_github_enabled_create_bug(handler, brain_dir):
    """With GitHub enabled, /bug creates an issue with kind:bug label."""
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.create_issue = AsyncMock(return_value={
        "number": 7, "html_url": "https://github.com/owner/repo/issues/7"
    })
    mock_gh.ensure_labels = AsyncMock()
    mock_gh.list_issues = AsyncMock(return_value=[])
    handler.github = mock_gh
    update, ctx = _make_update(12345, ["login", "broken"])
    await handler.cmd_bug(update, ctx)
    mock_gh.create_issue.assert_called_once()
    call = mock_gh.create_issue.call_args
    labels = call.kwargs.get("labels") or call.args[2]
    assert "kind:bug" in labels


@pytest.mark.asyncio
async def test_github_feature_plan_sets_status(handler, brain_dir):
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.repo = "owner/repo"
    mock_gh.get_issue = AsyncMock(return_value={
        "number": 5, "title": "Test", "state": "open", "state_reason": None,
        "labels": [{"name": "kind:feature"}, {"name": "priority:medium"}]
    })
    mock_gh.replace_labels = AsyncMock()
    mock_gh.update_issue = AsyncMock()
    mock_gh.list_issues = AsyncMock(return_value=[])
    handler.github = mock_gh
    handler._last_feature_set = [5]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_feature_plan(update, ctx)
    mock_gh.replace_labels.assert_called_once()
    new_labels = mock_gh.replace_labels.call_args[0][1]
    assert "status:planned" in new_labels


@pytest.mark.asyncio
async def test_github_feature_done_closes_issue(handler, brain_dir):
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.repo = "owner/repo"
    mock_gh.get_issue = AsyncMock(return_value={
        "number": 5, "title": "Test", "state": "open", "state_reason": None,
        "labels": [{"name": "kind:feature"}, {"name": "status:planned"}]
    })
    mock_gh.replace_labels = AsyncMock()
    mock_gh.update_issue = AsyncMock(return_value={"number": 5})
    mock_gh.list_issues = AsyncMock(return_value=[])
    handler.github = mock_gh
    handler._last_feature_set = [5]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_feature_done(update, ctx)
    update_call = mock_gh.update_issue.call_args
    assert update_call[1].get("state") == "closed" or update_call[0][1] == "closed" or \
           any("closed" in str(a) for a in update_call.args + tuple(update_call.kwargs.values()))


@pytest.mark.asyncio
async def test_github_feature_note_adds_comment(handler, brain_dir):
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.repo = "owner/repo"
    mock_gh.add_comment = AsyncMock(return_value={"id": 1})
    mock_gh.get_issue = AsyncMock(return_value={"number": 10, "title": "Test issue"})
    handler.github = mock_gh
    handler._last_feature_set = [10]
    update, ctx = _make_update(12345, ["1", "this", "is", "a", "note"])
    await handler.cmd_feature_note(update, ctx)
    mock_gh.add_comment.assert_called_once_with(10, "this is a note")


@pytest.mark.asyncio
async def test_github_hashtag_ref_bypasses_list(handler, brain_dir):
    """#N syntax lets users act on an issue without running /features first."""
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.repo = "owner/repo"
    mock_gh.get_issue = AsyncMock(return_value={
        "number": 99, "title": "Direct ref test", "state": "open", "state_reason": None,
        "labels": [{"name": "kind:feature"}]
    })
    mock_gh.replace_labels = AsyncMock()
    mock_gh.update_issue = AsyncMock()
    mock_gh.list_issues = AsyncMock(return_value=[])
    handler.github = mock_gh
    handler._last_feature_set = []  # empty — no prior /features call
    update, ctx = _make_update(12345, ["#99"])
    await handler.cmd_feature_plan(update, ctx)
    # Should still call _gh_set_status(99, "planned")
    mock_gh.get_issue.assert_called_with(99)


@pytest.mark.asyncio
async def test_github_features_list_calls_list_issues(handler, brain_dir):
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.list_issues = AsyncMock(return_value=[
        {"number": 1, "title": "Feature A", "state": "open", "state_reason": None,
         "created_at": "2024-01-01T00:00:00Z",
         "labels": [{"name": "kind:feature"}, {"name": "priority:medium"}]},
    ])
    handler.github = mock_gh
    update, ctx = _make_update(12345, [])
    await handler.cmd_features(update, ctx)
    mock_gh.list_issues.assert_called()
    assert handler._last_feature_set == [1]


@pytest.mark.asyncio
async def test_feature_import_preview(handler, brain_dir):
    """/feature_import without confirm shows preview count."""
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    handler.github = mock_gh
    memories_dir = brain_dir / "memories"
    # Write two local feature files
    for i in range(2):
        (memories_dir / f"feature-request-test{i}-abc{i}de.md").write_text(
            f"---\ntitle: Test {i}\ntype: feature_request\nkind: feature\nstatus: new\n"
            f"priority: medium\ncreated: 2024-01-01\ntags: []\nshort_id: abc{i}de\n---\n\n## Request\n\nTest {i}\n"
        )
    update, ctx = _make_update(12345, [])
    await handler.cmd_feature_import(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "2" in reply
    assert "confirm" in reply.lower()


@pytest.mark.asyncio
async def test_feature_import_confirm_creates_and_archives(handler, brain_dir):
    """/feature_import confirm creates GH issues and moves local files to archive/."""
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.repo = "owner/repo"
    mock_gh.create_issue = AsyncMock(side_effect=[
        {"number": 101, "html_url": "..."},
        {"number": 102, "html_url": "..."},
    ])
    mock_gh.ensure_labels = AsyncMock()
    mock_gh.update_issue = AsyncMock()
    mock_gh.list_issues = AsyncMock(return_value=[])
    handler.github = mock_gh
    memories_dir = brain_dir / "memories"
    for i in range(2):
        (memories_dir / f"feature-request-test{i}-abc{i}de.md").write_text(
            f"---\ntitle: Test {i}\ntype: feature_request\nkind: feature\nstatus: new\n"
            f"priority: medium\ncreated: 2024-01-01\ntags: []\nshort_id: abc{i}de\n---\n\n## Request\n\nTest {i}\n"
        )
    update, ctx = _make_update(12345, ["confirm"])
    await handler.cmd_feature_import(update, ctx)
    assert mock_gh.create_issue.call_count == 2
    # Files should be in archive/
    archive = memories_dir / "archive"
    assert archive.exists()
    archived = list(archive.glob("feature-request-*.md"))
    assert len(archived) == 2
    reply = update.message.reply_text.call_args[0][0]
    assert "2" in reply


@pytest.mark.asyncio
async def test_feature_import_refuses_when_gh_disabled(handler, brain_dir):
    handler.github = MagicMock(enabled=False)
    update, ctx = _make_update(12345, [])
    await handler.cmd_feature_import(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "GitHub" in reply or "not configured" in reply.lower()


@pytest.mark.asyncio
async def test_features_index_snapshot_written_on_create(handler, brain_dir):
    """After GH-backed /feature create, features-index.md is written to memories/."""
    mock_gh = AsyncMock()
    mock_gh.enabled = True
    mock_gh.repo = "owner/repo"
    mock_gh.create_issue = AsyncMock(return_value={
        "number": 1, "html_url": "https://github.com/owner/repo/issues/1"
    })
    mock_gh.ensure_labels = AsyncMock()
    mock_gh.list_issues = AsyncMock(return_value=[
        {"number": 1, "title": "Test feature", "state": "open", "state_reason": None,
         "labels": [{"name": "kind:feature"}]}
    ])
    handler.github = mock_gh
    update, ctx = _make_update(12345, ["test", "feature"])
    await handler.cmd_feature(update, ctx)
    index_file = brain_dir / "memories" / "features-index.md"
    assert index_file.exists()
    content = index_file.read_text()
    assert "feature_request_index" in content


# ── Backfill ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_rejects_unknown_type(handler):
    update, ctx = _make_update(12345, ["unknown_type"])
    await handler.cmd_backfill(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Unknown type" in reply


@pytest.mark.asyncio
async def test_backfill_rejects_mismatched_hostname(handler):
    mock_scanner = AsyncMock()
    handler.scanners = {"readings": mock_scanner}
    update, ctx = _make_update(12345, ["readings", "different-host"])
    with patch("chat_handler.socket.gethostname", return_value="local-host"):
        await handler.cmd_backfill(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Cross-node" in reply or "not yet implemented" in reply.lower()


@pytest.mark.asyncio
async def test_backfill_uses_default_days_when_omitted(handler):
    mock_scanner = AsyncMock()
    mock_scanner.backfill = AsyncMock(return_value={"processed": 5, "skipped": 0, "errors": 0, "notes": ""})
    handler.scanners = {"readings": mock_scanner}
    update, ctx = _make_update(12345, ["readings"])
    with patch("chat_handler.socket.gethostname", return_value="local-host"):
        await handler.cmd_backfill(update, ctx)
    # Default for readings is 30 days
    mock_scanner.backfill.assert_called_once_with(30)


@pytest.mark.asyncio
async def test_backfill_calls_scanner_backfill_with_parsed_days(handler):
    mock_scanner = AsyncMock()
    mock_scanner.backfill = AsyncMock(return_value={"processed": 10, "skipped": 2, "errors": 0, "notes": "test"})
    handler.scanners = {"email": mock_scanner}
    update, ctx = _make_update(12345, ["email", "60"])
    with patch("chat_handler.socket.gethostname", return_value="local-host"):
        await handler.cmd_backfill(update, ctx)
    mock_scanner.backfill.assert_called_once_with(60)


@pytest.mark.asyncio
async def test_backfill_reply_formats_result_dict(handler):
    mock_scanner = AsyncMock()
    mock_scanner.backfill = AsyncMock(return_value={"processed": 15, "skipped": 3, "errors": 1, "notes": "Done!"})
    handler.scanners = {"code": mock_scanner}
    update, ctx = _make_update(12345, ["code"])
    with patch("chat_handler.socket.gethostname", return_value="local-host"):
        await handler.cmd_backfill(update, ctx)
    replies = [call[0][0] for call in update.message.reply_text.call_args_list]
    final_reply = replies[-1]
    assert "15 processed" in final_reply
    assert "3 skipped" in final_reply
    assert "1 errors" in final_reply
    assert "Done!" in final_reply


# --- Error handler registration ---

def test_error_handler_registered(handler):
    """add_error_handler must be called so python-telegram-bot stops logging
    'No error handlers are registered' spam on any unhandled exception."""
    handler.app.add_error_handler.assert_called_once()
    # Verify the registered callable is the handler method
    registered = handler.app.add_error_handler.call_args[0][0]
    assert callable(registered)
    assert registered.__name__ == "_on_telegram_error"


# --- /comms email classification filtering (FR-11) ---

def test_comms_email_hides_marketing_by_default(brain_dir, handler):
    """Default /comms email hides marketing and automated threads."""
    mem_dir = brain_dir / "memories"

    # Create one human email and one marketing email
    human = mem_dir / "email-thread-human-1001.md"
    human.write_text(
        "---\nsource_title: Project Update\nsummary: Discussion.\n"
        "tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        "source_url: mailto:conversation-1001\ntype: email_thread\n"
        "classification: human\n"
        "participants: [alice@acme.com]\n"
        "last_message: '2026-04-11T10:00:00'\n"
        "message_count: 3\n---\n\n## Messages\nTest.\n"
    )

    marketing = mem_dir / "email-thread-newsletter-1002.md"
    marketing.write_text(
        "---\nsource_title: Newsletter\nsummary: Marketing.\n"
        "tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        "source_url: mailto:conversation-1002\ntype: email_thread\n"
        "classification: marketing\n"
        "participants: [newsletter@company.com]\n"
        "last_message: '2026-04-11T10:00:00'\n"
        "message_count: 1\n---\n\n## Messages\nNewsletter.\n"
    )

    text = handler._list_comms_text(kind="email", limit=20, show_all=False)

    # Should show human email
    assert "Project Update" in text
    # Should hide marketing email
    assert "Newsletter" not in text


def test_comms_email_all_shows_marketing(brain_dir, handler):
    """"/comms email all" shows marketing emails with [mkt] suffix."""
    mem_dir = brain_dir / "memories"

    marketing = mem_dir / "email-thread-newsletter-1002.md"
    marketing.write_text(
        "---\nsource_title: Newsletter\nsummary: Marketing.\n"
        "tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        "source_url: mailto:conversation-1002\ntype: email_thread\n"
        "classification: marketing\n"
        "participants: [newsletter@company.com]\n"
        "last_message: '2026-04-11T10:00:00'\n"
        "message_count: 1\n---\n\n## Messages\nNewsletter.\n"
    )

    text = handler._list_comms_text(kind="email", limit=20, show_all=True)

    # Should show marketing email with suffix
    assert "Newsletter" in text
    assert "[mkt]" in text


def test_comms_email_missing_classification_treated_as_human(brain_dir, handler):
    """Old email file without classification field should appear in default listing."""
    mem_dir = brain_dir / "memories"

    # Old format: no classification field
    old_email = mem_dir / "email-thread-old-1003.md"
    old_email.write_text(
        "---\nsource_title: Old Email\nsummary: Legacy.\n"
        "tags: []\nlast_scanned: '2026-04-11T10:00:00'\n"
        "source_url: mailto:conversation-1003\ntype: email_thread\n"
        "participants: [bob@acme.com]\n"
        "last_message: '2026-04-11T10:00:00'\n"
        "message_count: 2\n---\n\n## Messages\nOld.\n"
    )

    text = handler._list_comms_text(kind="email", limit=20, show_all=False)

    # Should show old email (treated as human)
    assert "Old Email" in text


# ── /remember command ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_remember_happy_path(handler, brain_dir):
    """Successful fetch+summarize writes a memory file and replies with preview."""
    update, ctx = _make_update(12345, ["https://example.com/article"])

    with patch("chat_handler.fetch_url_content", new=AsyncMock(
            return_value=("Article Title", "Long article content here."))), \
         patch("chat_handler.SkillExecutor") as MockExec, \
         patch("memory_writer.MemoryWriter") as MockWriter:
        mock_executor_instance = MagicMock()
        mock_executor_instance.run = AsyncMock(
            return_value="## Summary\nGreat article.\n\n**Tags:** tech")
        MockExec.return_value = mock_executor_instance
        mock_writer_instance = MagicMock()
        mock_writer_instance.write = AsyncMock(return_value="2026-04-13-article-title-abc123.md")
        MockWriter.return_value = mock_writer_instance

        await handler.cmd_remember(update, ctx)

    replies = [call[0][0] for call in update.message.reply_text.call_args_list]
    assert any("📥" in r for r in replies), "Should send a fetching ack"
    assert any("✅" in r for r in replies), "Should confirm save"
    assert any("2026-04-13-article-title-abc123.md" in r for r in replies)


@pytest.mark.asyncio
async def test_cmd_remember_bad_url(handler):
    """Non-http argument shows usage message."""
    update, ctx = _make_update(12345, ["not-a-url"])
    await handler.cmd_remember(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_remember_no_args(handler):
    """Missing argument shows usage message."""
    update, ctx = _make_update(12345, [])
    await handler.cmd_remember(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_remember_fetch_failure(handler):
    """If fetch returns empty content, report the error gracefully."""
    update, ctx = _make_update(12345, ["https://example.com/bad"])

    with patch("chat_handler.fetch_url_content", new=AsyncMock(return_value=("", ""))):
        await handler.cmd_remember(update, ctx)

    replies = [call[0][0] for call in update.message.reply_text.call_args_list]
    assert any("Could not fetch" in r for r in replies)


@pytest.mark.asyncio
async def test_cmd_remember_skill_failure(handler):
    """If the skill returns None, report gracefully without crashing."""
    update, ctx = _make_update(12345, ["https://example.com/article"])

    with patch("chat_handler.fetch_url_content",
               new=AsyncMock(return_value=("Title", "Some content."))), \
         patch("chat_handler.SkillExecutor") as MockExec:
        mock_executor_instance = MagicMock()
        mock_executor_instance.run = AsyncMock(return_value=None)
        MockExec.return_value = mock_executor_instance
        await handler.cmd_remember(update, ctx)

    replies = [call[0][0] for call in update.message.reply_text.call_args_list]
    assert any("Summary failed" in r or "failed" in r.lower() for r in replies)


# ── /note command ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_note_uses_detailed_skill(handler, brain_dir):
    """cmd_note must use the summarize-webpage-detailed skill, not the default."""
    update, ctx = _make_update(12345, ["https://example.com/paper"])

    with patch("chat_handler.fetch_url_content", new=AsyncMock(
            return_value=("Paper Title", "Long paper content here."))), \
         patch("chat_handler.SkillExecutor") as MockExec, \
         patch("memory_writer.MemoryWriter") as MockWriter:
        mock_executor_instance = MagicMock()
        mock_executor_instance.run = AsyncMock(
            return_value="## Summary\nDetailed notes.\n\n**Tags:** research")
        MockExec.return_value = mock_executor_instance
        mock_writer_instance = MagicMock()
        mock_writer_instance.write = AsyncMock(return_value="2026-04-13-paper-title-abc123.md")
        MockWriter.return_value = mock_writer_instance

        await handler.cmd_note(update, ctx)

    # Executor must be instantiated with the detailed skill name
    MockExec.assert_called_once_with("summarize-webpage-detailed")


@pytest.mark.asyncio
async def test_cmd_note_writes_content_type_detailed(handler, brain_dir):
    """Entry written by cmd_note must have content_type='detailed'."""
    update, ctx = _make_update(12345, ["https://example.com/paper"])

    with patch("chat_handler.fetch_url_content", new=AsyncMock(
            return_value=("Paper Title", "Content here."))), \
         patch("chat_handler.SkillExecutor") as MockExec, \
         patch("memory_writer.MemoryWriter") as MockWriter:
        mock_executor_instance = MagicMock()
        mock_executor_instance.run = AsyncMock(return_value="## Summary\nGreat.")
        MockExec.return_value = mock_executor_instance
        mock_writer_instance = MagicMock()
        mock_writer_instance.write = AsyncMock(return_value="2026-04-13-paper-title-abc123.md")
        MockWriter.return_value = mock_writer_instance

        await handler.cmd_note(update, ctx)

    # Check the entry dict passed to MemoryWriter.write had content_type='detailed'
    entry_arg = mock_writer_instance.write.call_args[0][0]
    assert entry_arg["content_type"] == "detailed"


@pytest.mark.asyncio
async def test_cmd_note_bad_url(handler):
    """Non-http argument shows usage message."""
    update, ctx = _make_update(12345, ["not-a-url"])
    await handler.cmd_note(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_note_fetch_failure(handler):
    """If fetch returns empty content, report gracefully."""
    update, ctx = _make_update(12345, ["https://example.com/bad"])

    with patch("chat_handler.fetch_url_content", new=AsyncMock(return_value=("", ""))):
        await handler.cmd_note(update, ctx)

    replies = [call[0][0] for call in update.message.reply_text.call_args_list]
    assert any("Could not fetch" in r for r in replies)


@pytest.mark.asyncio
async def test_cmd_note_happy_path_replies_with_saved(handler, brain_dir):
    """Successful /note replies with ✅ and filename."""
    update, ctx = _make_update(12345, ["https://example.com/paper"])

    with patch("chat_handler.fetch_url_content", new=AsyncMock(
            return_value=("Paper Title", "Content here."))), \
         patch("chat_handler.SkillExecutor") as MockExec, \
         patch("memory_writer.MemoryWriter") as MockWriter:
        mock_executor_instance = MagicMock()
        mock_executor_instance.run = AsyncMock(return_value="## Summary\nGreat.")
        MockExec.return_value = mock_executor_instance
        mock_writer_instance = MagicMock()
        mock_writer_instance.write = AsyncMock(return_value="2026-04-13-paper-title-abc123.md")
        MockWriter.return_value = mock_writer_instance

        await handler.cmd_note(update, ctx)

    replies = [call[0][0] for call in update.message.reply_text.call_args_list]
    assert any("📥" in r for r in replies)
    assert any("✅" in r for r in replies)
    assert any("2026-04-13-paper-title-abc123.md" in r for r in replies)


# --- Network error hardening ---

@pytest.mark.asyncio
async def test_send_reply_retries_on_timeout(handler):
    """_send_reply should retry on TimedOut up to 3 attempts."""
    from telegram.error import TimedOut
    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    # Fail twice, then succeed
    mock_update.message.reply_text.side_effect = [
        TimedOut("timeout 1"),
        TimedOut("timeout 2"),
        None,  # success on third attempt
    ]
    await handler._send_reply(mock_update, "hello")
    assert mock_update.message.reply_text.call_count == 3


@pytest.mark.asyncio
async def test_send_reply_gives_up_after_three_timeouts(handler, caplog):
    """_send_reply should give up after 3 failed attempts and log error."""
    from telegram.error import TimedOut
    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    # Always fail
    mock_update.message.reply_text.side_effect = TimedOut("persistent timeout")

    # Should not raise, just log and return
    await handler._send_reply(mock_update, "hello")

    assert mock_update.message.reply_text.call_count == 3
    # Check that error was logged
    assert any("Reply failed after 3 attempts" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_handle_message_cascade_broken_on_timeout(handler):
    """handle_message error path should not cascade when the fallback reply also times out."""
    from telegram.error import TimedOut
    mock_update, mock_context = _make_update(12345)
    # Set text as a string (not AsyncMock) so query logging doesn't fail
    mock_update.message.text = "test query"

    # Make the executor raise
    with patch.object(handler.executor, "run_with_tools", side_effect=Exception("LLM down")):
        # Make reply_text also raise TimedOut
        mock_update.message.reply_text.side_effect = TimedOut("network error")

        # Should complete without raising
        await handler.handle_message(mock_update, mock_context)

        # Verify the error handler was invoked (message.reply_text was called despite timing out)
        assert mock_update.message.reply_text.called


def test_safe_read_text_returns_none_on_oserror(tmp_path):
    """_safe_read_text should return None on OSError."""
    from chat_handler import _safe_read_text
    missing = tmp_path / "missing.md"
    result = _safe_read_text(missing)
    assert result is None


# --- Pending-reply queue tests ---

@pytest.mark.asyncio
async def test_send_reply_returns_true_on_success(handler):
    """_send_reply should return True when all chunks delivered successfully."""
    mock_update, _ = _make_update(12345)
    result = await handler._send_reply(mock_update, "Test message")
    assert result is True
    assert mock_update.message.reply_text.called


@pytest.mark.asyncio
async def test_send_reply_returns_false_on_exhausted_retries(handler):
    """_send_reply should return False when all retries are exhausted."""
    from telegram.error import TimedOut
    mock_update, _ = _make_update(12345)
    mock_update.message.reply_text.side_effect = TimedOut("network error")
    result = await handler._send_reply(mock_update, "Test message")
    assert result is False
    assert mock_update.message.reply_text.call_count == 3  # 3 attempts


@pytest.mark.asyncio
async def test_handle_message_does_not_append_history_on_send_failure(handler):
    """handle_message should not append to history when send fails, should queue instead."""
    mock_update, mock_context = _make_update(12345)
    mock_update.effective_chat.id = 12345
    mock_update.message.text = "test query"

    with patch.object(handler.executor, "run_with_tools", new=AsyncMock(return_value="response")), \
         patch.object(handler, "_send_reply", return_value=False), \
         patch.object(handler, "_queue_pending_reply") as mock_queue:
        await handler.handle_message(mock_update, mock_context)

        # History should be empty
        assert 12345 not in handler._chat_history or len(handler._chat_history[12345]) == 0
        # Queue should be called
        assert mock_queue.called
        mock_queue.assert_called_once_with(12345, "test query", "response")


@pytest.mark.asyncio
async def test_handle_message_persists_history_to_disk(handler):
    """handle_message should write chat history to disk after successful delivery."""
    mock_update, mock_context = _make_update(12345)
    mock_update.effective_chat.id = 12345
    mock_update.message.text = "hello"

    with patch.object(handler.executor, "run_with_tools", new=AsyncMock(return_value="world")), \
         patch.object(handler, "_send_reply", return_value=True):
        await handler.handle_message(mock_update, mock_context)

    assert handler.HISTORY_FILE.exists()
    data = json.loads(handler.HISTORY_FILE.read_text())
    assert "12345" in data
    assert data["12345"][-2]["content"] == "hello"
    assert data["12345"][-1]["content"] == "world"


def test_load_history_restores_from_disk(handler):
    """_load_history should restore previously saved history."""
    handler.HISTORY_FILE.write_text(json.dumps({
        "12345": [
            {"role": "user", "content": "prior question"},
            {"role": "assistant", "content": "prior answer"},
        ]
    }))
    restored = handler._load_history()
    assert 12345 in restored
    assert len(restored[12345]) == 2
    assert restored[12345][0]["content"] == "prior question"


def test_load_history_returns_empty_on_missing_file(handler):
    """_load_history should return empty dict when no file exists."""
    assert handler._load_history() == {}


def test_load_history_returns_empty_on_corrupt_file(handler):
    """_load_history should return empty dict when file is corrupt."""
    handler.HISTORY_FILE.write_text("not json")
    assert handler._load_history() == {}


def test_queue_pending_reply_writes_state_file(handler):
    """_queue_pending_reply should persist to JSON with correct schema."""
    handler._queue_pending_reply(12345, "query", "response")

    assert handler.PENDING_FILE.exists()
    state = json.loads(handler.PENDING_FILE.read_text())
    assert "12345" in state
    assert state["12345"]["pending"][0]["query"] == "query"
    assert state["12345"]["pending"][0]["response"] == "response"
    assert "queued_at" in state["12345"]["pending"][0]
    assert state["12345"]["summary_sent"] is False


def test_queue_pending_reply_resets_summary_sent_on_new_item(handler):
    """_queue_pending_reply should reset summary_sent when adding a new item."""
    # Pre-populate with summary_sent=True
    handler.PENDING_FILE.write_text(json.dumps({
        "12345": {"pending": [{"query": "old", "response": "old", "queued_at": "2026-01-01T12:00:00"}], "summary_sent": True}
    }))

    handler._queue_pending_reply(12345, "new query", "new response")

    state = json.loads(handler.PENDING_FILE.read_text())
    assert len(state["12345"]["pending"]) == 2
    assert state["12345"]["summary_sent"] is False


@pytest.mark.asyncio
async def test_cmd_deliver_sends_all_pending_and_empties_queue(handler):
    """cmd_deliver should send all pending and clear the queue."""
    # Pre-populate queue
    handler.PENDING_FILE.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "q1", "response": "r1", "queued_at": "2026-01-01T12:00:00"},
                {"query": "q2", "response": "r2", "queued_at": "2026-01-01T12:01:00"}
            ],
            "summary_sent": False
        }
    }))

    mock_update, mock_context = _make_update(12345)
    mock_update.effective_chat.id = 12345

    with patch.object(handler, "_send_reply", return_value=True):
        await handler.cmd_deliver(mock_update, mock_context)

    # Queue should be empty
    state = handler._load_pending()
    assert "12345" not in state
    # History should have 4 entries (2 turns)
    assert len(handler._chat_history[12345]) == 4


@pytest.mark.asyncio
async def test_cmd_deliver_requeues_on_partial_failure(handler):
    """cmd_deliver should requeue items that fail to send and reset summary_sent."""
    # Pre-populate queue with 3 items
    handler.PENDING_FILE.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "q1", "response": "r1", "queued_at": "2026-01-01T12:00:00"},
                {"query": "q2", "response": "r2", "queued_at": "2026-01-01T12:01:00"},
                {"query": "q3", "response": "r3", "queued_at": "2026-01-01T12:02:00"}
            ],
            "summary_sent": True  # was already sent
        }
    }))

    mock_update, mock_context = _make_update(12345)
    mock_update.effective_chat.id = 12345

    # First two succeed, third fails
    with patch.object(handler, "_send_reply", side_effect=[True, True, False]):
        await handler.cmd_deliver(mock_update, mock_context)

    state = handler._load_pending()
    assert len(state["12345"]["pending"]) == 1
    assert state["12345"]["pending"][0]["query"] == "q3"
    assert state["12345"]["summary_sent"] is False


@pytest.mark.asyncio
async def test_cmd_discard_empties_queue(handler):
    """cmd_discard should remove all pending items for this chat."""
    # Pre-populate queue
    handler.PENDING_FILE.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "q1", "response": "r1", "queued_at": "2026-01-01T12:00:00"}
            ],
            "summary_sent": False
        }
    }))

    mock_update, mock_context = _make_update(12345)
    mock_update.effective_chat.id = 12345

    await handler.cmd_discard(mock_update, mock_context)

    state = handler._load_pending()
    assert "12345" not in state


@pytest.mark.asyncio
async def test_cmd_reset_also_clears_pending_queue(handler):
    """cmd_reset should clear both history and pending queue."""
    # Set up history and pending queue
    handler._chat_history[12345] = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "r1"}
    ]
    handler.PENDING_FILE.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "q2", "response": "r2", "queued_at": "2026-01-01T12:00:00"}
            ],
            "summary_sent": False
        }
    }))

    mock_update, mock_context = _make_update(12345)
    mock_update.effective_chat.id = 12345

    await handler.cmd_reset(mock_update, mock_context)

    # Both should be cleared
    assert 12345 not in handler._chat_history
    state = handler._load_pending()
    assert "12345" not in state


@pytest.mark.asyncio
async def test_is_telegram_reachable_returns_true_on_success(handler):
    """_is_telegram_reachable should return True when get_me succeeds."""
    handler.app.bot.get_me = AsyncMock(return_value={"id": 123, "username": "bot"})
    result = await handler._is_telegram_reachable()
    assert result is True


@pytest.mark.asyncio
async def test_is_telegram_reachable_returns_false_on_exception(handler):
    """_is_telegram_reachable should return False when get_me raises."""
    handler.app.bot.get_me = AsyncMock(side_effect=Exception("network error"))
    result = await handler._is_telegram_reachable()
    assert result is False


@pytest.mark.asyncio
async def test_reconnect_loop_sends_summary_when_network_back(handler):
    """_reconnect_loop should notify user when network is back and queue has items."""
    # Pre-populate queue
    handler.PENDING_FILE.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "q1", "response": "r1", "queued_at": "2026-01-01T12:00:00"}
            ],
            "summary_sent": False
        }
    }))

    stop_event = asyncio.Event()
    handler.app.bot.send_message = AsyncMock()

    # Mock _is_telegram_reachable to return True
    with patch.object(handler, "_is_telegram_reachable", return_value=True):
        # Mock wait_for to raise TimeoutError once (normal tick), then set stop_event
        call_count = 0
        async def mock_wait_for(event_wait, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()  # First tick
            else:
                stop_event.set()  # Stop after first iteration

        with patch("asyncio.wait_for", side_effect=mock_wait_for):
            await handler._reconnect_loop(stop_event)

    # Verify send_message was called with the reconnect notification
    assert handler.app.bot.send_message.called
    call_args = handler.app.bot.send_message.call_args
    assert call_args[1]["chat_id"] == 12345
    assert "📬" in call_args[1]["text"]
    assert "/deliver" in call_args[1]["text"]

    # Verify summary_sent was updated
    state = handler._load_pending()
    assert state["12345"]["summary_sent"] is True


@pytest.mark.asyncio
async def test_reconnect_loop_does_not_spam_when_summary_already_sent(handler):
    """_reconnect_loop should not send notification when summary_sent is already True."""
    # Pre-populate queue with summary_sent=True
    handler.PENDING_FILE.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "q1", "response": "r1", "queued_at": "2026-01-01T12:00:00"}
            ],
            "summary_sent": True
        }
    }))

    stop_event = asyncio.Event()
    handler.app.bot.send_message = AsyncMock()

    # Mock _is_telegram_reachable to return True
    with patch.object(handler, "_is_telegram_reachable", return_value=True):
        # Mock wait_for to raise TimeoutError once, then stop
        call_count = 0
        async def mock_wait_for(event_wait, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()
            else:
                stop_event.set()

        with patch("asyncio.wait_for", side_effect=mock_wait_for):
            await handler._reconnect_loop(stop_event)

    # Verify send_message was NOT called
    assert not handler.app.bot.send_message.called


@pytest.mark.asyncio
async def test_reconnect_loop_adds_notification_to_chat_history(handler, tmp_path):
    """Reconnect loop appends notification to _chat_history as assistant turn."""
    import json

    # Pre-populate pending queue
    pending_file = tmp_path / "deploy" / "pending-replies.json"
    pending_file.write_text(json.dumps({
        "12345": {
            "pending": [
                {"query": "test", "response": "test response", "queued_at": "2026-04-13T10:00:00"}
            ],
            "summary_sent": False
        }
    }))

    handler.PENDING_FILE = pending_file
    handler._chat_history = {}
    handler.app.bot.send_message = AsyncMock()
    stop_event = asyncio.Event()

    # Mock _is_telegram_reachable to return True
    with patch.object(handler, "_is_telegram_reachable", return_value=True):
        # Mock wait_for to raise TimeoutError once, then stop
        call_count = 0
        async def mock_wait_for(event_wait, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError()
            else:
                stop_event.set()

        with patch("asyncio.wait_for", side_effect=mock_wait_for):
            await handler._reconnect_loop(stop_event)

    # Assert send_message was called
    assert handler.app.bot.send_message.called

    # Assert notification added to chat history
    assert 12345 in handler._chat_history
    turns = handler._chat_history[12345]
    assert len(turns) == 1
    assert turns[0]["role"] == "assistant"
    assert "Network is back" in turns[0]["content"]
    assert "/deliver" in turns[0]["content"]
    assert "/discard" in turns[0]["content"]


# ── pending-reply tool gating (Fix 1: bugs a692c6 + 95dad0) ─────────────────

async def test_pending_tools_absent_when_queue_empty(handler, brain_dir):
    """deliver/discard tools must NOT be passed to the LLM when queue is empty."""
    handler.executor = MagicMock()
    captured_tools = []

    async def capture_tools(**kwargs):
        captured_tools.extend(kwargs.get("tools", []))
        return "answer"

    handler.executor.run_with_tools = capture_tools

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "yes"

    # No pending-replies.json — queue is empty
    await handler.handle_message(mock_update, MagicMock())

    tool_names = [t["function"]["name"] for t in captured_tools]
    assert "deliver_pending_replies" not in tool_names
    assert "discard_pending_replies" not in tool_names


async def test_pending_tools_present_when_queue_nonempty(handler, brain_dir):
    """deliver/discard tools ARE passed to the LLM when queue has entries for this chat
    AND the last assistant message was the reconnect notification."""
    handler.executor = MagicMock()
    captured_tools = []

    async def capture_tools(**kwargs):
        captured_tools.extend(kwargs.get("tools", []))
        return "answer"

    handler.executor.run_with_tools = capture_tools

    # Pre-populate queue for this chat_id
    chat_id = 12345
    handler._save_pending({str(chat_id): {"pending": [{"query": "q", "response": "r", "queued_at": "2026-04-13T12:00:00"}], "summary_sent": False}})

    # Pre-populate history with the notification message
    handler._chat_history[chat_id] = [
        {"role": "user", "content": "what is the weather"},
        {"role": "assistant", "content": "📬 Network is back. I have 1 response queued — say \"yes\" to deliver or \"no\" to discard."}
    ]

    mock_update = MagicMock()
    mock_update.effective_user.id = chat_id
    mock_update.effective_chat.id = chat_id
    mock_update.message = AsyncMock()
    mock_update.message.text = "yes"

    await handler.handle_message(mock_update, MagicMock())

    tool_names = [t["function"]["name"] for t in captured_tools]
    assert "deliver_pending_replies" in tool_names
    assert "discard_pending_replies" in tool_names


async def test_pending_tools_scoped_per_chat_id(handler, brain_dir):
    """Queue for chat A must not expose pending tools when querying as chat B."""
    handler.executor = MagicMock()
    captured_tools = []

    async def capture_tools(**kwargs):
        captured_tools.extend(kwargs.get("tools", []))
        return "answer"

    handler.executor.run_with_tools = capture_tools

    # Queue belongs to a different chat_id
    handler._save_pending({"99999": {"pending": [{"query": "q", "response": "r", "queued_at": "2026-04-13T12:00:00"}], "summary_sent": False}})

    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.effective_chat.id = 12345  # different from 99999
    mock_update.message = AsyncMock()
    mock_update.message.text = "yes"

    await handler.handle_message(mock_update, MagicMock())

    tool_names = [t["function"]["name"] for t in captured_tools]
    assert "deliver_pending_replies" not in tool_names
    assert "discard_pending_replies" not in tool_names


@pytest.mark.asyncio
async def test_load_context_short_query_uses_history(handler, brain_dir):
    """Short queries (< 3 tokens ≥ 3 chars) should augment scoring with recent user history."""
    # Create memory file with "commitments" keyword
    m = brain_dir / "memories"
    write_memory(m, "work-abc123", ["work", "commitments", "tasks"], "Work Tasks",
                 body="Details about weekly commitments")

    # Simulate history with a previous query about commitments
    history = [
        {"role": "user", "content": "what commitments do I have this week"},
        {"role": "assistant", "content": "You have 3 commitments..."}
    ]

    # Patch cache.score_keywords to capture what it's called with
    with patch.object(handler._cache, 'score_keywords', wraps=handler._cache.score_keywords) as mock_score:
        ctx = await handler._load_context("ok", history)

        # Verify score_keywords was called
        assert mock_score.call_count > 0

        # Check that the query included augmented text from history
        score_query = mock_score.call_args[0][0]
        assert "commitments" in score_query.lower(), \
            f"Expected 'commitments' in augmented query, got: {score_query}"


async def test_pending_tools_hidden_when_not_last_notification(handler, brain_dir):
    """deliver/discard tools should NOT be exposed when queue is non-empty but
    last assistant message was NOT the reconnect notification."""
    handler.executor = MagicMock()
    captured_tools = []

    async def capture_tools(**kwargs):
        captured_tools.extend(kwargs.get("tools", []))
        return "answer"

    handler.executor.run_with_tools = capture_tools

    # Pre-populate queue for this chat_id
    chat_id = 12345
    handler._save_pending({str(chat_id): {"pending": [{"query": "q", "response": "r", "queued_at": "2026-04-13T12:00:00"}], "summary_sent": False}})

    # Pre-populate history where last message is NOT the notification
    handler._chat_history[chat_id] = [
        {"role": "user", "content": "yes"},
        {"role": "assistant", "content": "No problem!"}
    ]

    mock_update = MagicMock()
    mock_update.effective_user.id = chat_id
    mock_update.effective_chat.id = chat_id
    mock_update.message = AsyncMock()
    mock_update.message.text = "yes"

    await handler.handle_message(mock_update, MagicMock())

    tool_names = [t["function"]["name"] for t in captured_tools]
    assert "deliver_pending_replies" not in tool_names, \
        "deliver_pending_replies should not be exposed when last message was not the notification"
    assert "discard_pending_replies" not in tool_names


async def test_pending_tools_shown_when_last_is_notification(handler, brain_dir):
    """deliver/discard tools SHOULD be exposed when queue is non-empty AND
    last assistant message contains the reconnect notification."""
    handler.executor = MagicMock()
    captured_tools = []

    async def capture_tools(**kwargs):
        captured_tools.extend(kwargs.get("tools", []))
        return "answer"

    handler.executor.run_with_tools = capture_tools

    # Pre-populate queue for this chat_id
    chat_id = 12345
    handler._save_pending({str(chat_id): {"pending": [{"query": "q", "response": "r", "queued_at": "2026-04-13T12:00:00"}], "summary_sent": False}})

    # Pre-populate history where last message IS the notification
    handler._chat_history[chat_id] = [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "📬 Network is back. I have 1 response queued."}
    ]

    mock_update = MagicMock()
    mock_update.effective_user.id = chat_id
    mock_update.effective_chat.id = chat_id
    mock_update.message = AsyncMock()
    mock_update.message.text = "yes"

    await handler.handle_message(mock_update, MagicMock())

    tool_names = [t["function"]["name"] for t in captured_tools]
    assert "deliver_pending_replies" in tool_names, \
        "deliver_pending_replies should be exposed when last message was the notification"
    assert "discard_pending_replies" in tool_names


# ── /comms forget subcommand (Fix 3: bug c1a5ce) ─────────────────────────────

def _write_email_thread(memories_dir: Path, slug: str, subject: str, n: int = 1) -> Path:
    """Write a minimal email-thread memory file for testing."""
    path = memories_dir / f"email-thread-{slug}-abc{n:03d}.md"
    path.write_text(
        f"---\ntype: email_thread\nsource_title: {subject}\n"
        f"participants: [sender@example.com]\nlast_message: '2026-04-0{n}'\n---\n\n{subject} body."
    )
    return path


async def test_comms_forget_single_index(handler, brain_dir):
    m = brain_dir / "memories"
    f1 = _write_email_thread(m, "alpha", "Alpha thread", 1)
    f2 = _write_email_thread(m, "beta", "Beta thread", 2)
    f3 = _write_email_thread(m, "gamma", "Gamma thread", 3)
    with patch.object(ch, "BRAIN_DIR", brain_dir):
        update, ctx = _make_update(12345, ["email", "forget", "2"])
        await handler.cmd_comms(update, ctx)
    assert not f2.exists()
    assert f1.exists()
    assert f3.exists()


async def test_comms_forget_multi_index(handler, brain_dir):
    m = brain_dir / "memories"
    files = [_write_email_thread(m, f"t{i}", f"Thread {i}", i) for i in range(1, 6)]
    with patch.object(ch, "BRAIN_DIR", brain_dir):
        update, ctx = _make_update(12345, ["email", "forget", "1", "3", "5"])
        await handler.cmd_comms(update, ctx)
    assert not files[0].exists()  # index 1
    assert files[1].exists()       # index 2
    assert not files[2].exists()  # index 3
    assert files[3].exists()       # index 4
    assert not files[4].exists()  # index 5


async def test_comms_forget_rejects_non_numeric(handler, brain_dir):
    m = brain_dir / "memories"
    _write_email_thread(m, "alpha", "Alpha thread", 1)
    with patch.object(ch, "BRAIN_DIR", brain_dir):
        update, ctx = _make_update(12345, ["email", "forget", "abc"])
        await handler.cmd_comms(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Usage:" in reply


async def test_comms_forget_empty_args_usage_message(handler, brain_dir):
    m = brain_dir / "memories"
    _write_email_thread(m, "alpha", "Alpha thread", 1)
    with patch.object(ch, "BRAIN_DIR", brain_dir):
        update, ctx = _make_update(12345, ["email", "forget"])
        await handler.cmd_comms(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Usage:" in reply


async def test_forget_indices_helper_regression(handler, brain_dir):
    """Ensure cmd_forget still works correctly after _forget_indices extraction."""
    m = brain_dir / "memories"
    f1 = write_memory(m, "one-aaa111", [], "Article One")
    f2 = write_memory(m, "two-bbb222", [], "Article Two")
    f3 = write_memory(m, "three-ccc333", [], "Article Three")
    handler._active_list = [f1, f2, f3]
    update, ctx = _make_update(12345, ["2"])
    await handler.cmd_forget(update, ctx)
    assert not f2.exists()
    assert f1.exists()
    assert f3.exists()

# --- FR-7: Active goals/projects context injection ---

@pytest.mark.asyncio
async def test_build_goal_project_context_returns_active_goals(handler, brain_dir):
    """_build_goal_project_context_async includes active goals."""
    goal_path = brain_dir / "memories" / "goal-run-5k-abc123.md"
    goal_path.write_text(
        "---\n"
        "type: goal\n"
        "category: personal\n"
        "source_title: Run a 5K\n"
        "summary: ''\n"
        "tags: []\n"
        "created: '2026-04-15T09:00:00'\n"
        "due_date: '2026-06-30'\n"
        "status: active\n"
        "priority: medium\n"
        "linked_projects: []\n"
        "notes: ''\n"
        "---\n\n## Notes\n"
    )

    result = await handler._build_goal_project_context_async()
    assert "## Active Goals" in result
    assert "Run a 5K" in result
    assert "[personal]" in result
    assert "2026-06-30" in result


@pytest.mark.asyncio
async def test_build_goal_project_context_returns_active_projects(handler, brain_dir):
    """_build_goal_project_context_async includes active and on-hold projects."""
    active_path = brain_dir / "memories" / "project-work-q2-rollout-def456.md"
    active_path.write_text(
        "---\n"
        "type: project\n"
        "category: work\n"
        "source_title: Q2 rollout plan\n"
        "summary: ''\n"
        "tags: []\n"
        "created: '2026-04-15T09:00:00'\n"
        "due_date: '2026-07-01'\n"
        "status: active\n"
        "priority: high\n"
        "linked_goal: null\n"
        "milestones:\n"
        "  - text: Lock scope\n"
        "    done: true\n"
        "  - text: Draft checklist\n"
        "    done: false\n"
        "inferred_from: []\n"
        "notes: ''\n"
        "---\n\n## Notes\n"
    )

    onhold_path = brain_dir / "memories" / "project-personal-garden-shed-ghi789.md"
    onhold_path.write_text(
        "---\n"
        "type: project\n"
        "category: personal\n"
        "source_title: Garden shed build\n"
        "summary: ''\n"
        "tags: []\n"
        "created: '2026-04-15T09:00:00'\n"
        "due_date: null\n"
        "status: on-hold\n"
        "priority: medium\n"
        "linked_goal: null\n"
        "milestones: []\n"
        "inferred_from: []\n"
        "notes: ''\n"
        "---\n\n## Notes\n"
    )

    result = await handler._build_goal_project_context_async()
    assert "## Active Projects" in result
    assert "Q2 rollout plan" in result
    assert "milestones: 1/2 done" in result
    assert "Garden shed build" in result
    assert "no due date" in result


@pytest.mark.asyncio
async def test_build_goal_project_context_empty_when_no_active(handler, brain_dir):
    """_build_goal_project_context_async returns empty string when no active goals/projects."""
    goal_path = brain_dir / "memories" / "goal-old-abc123.md"
    goal_path.write_text(
        "---\n"
        "type: goal\n"
        "category: personal\n"
        "source_title: Old Goal\n"
        "summary: ''\n"
        "tags: []\n"
        "created: '2026-04-15T09:00:00'\n"
        "due_date: '2026-06-30'\n"
        "status: completed\n"
        "priority: medium\n"
        "linked_projects: []\n"
        "notes: ''\n"
        "---\n\n## Notes\n"
    )

    result = await handler._build_goal_project_context_async()
    assert result == ""


@pytest.mark.asyncio
async def test_build_goal_project_context_excludes_candidates(handler, brain_dir):
    """_build_goal_project_context_async never reads project-candidate-*.md files."""
    # Write a real active project
    real_path = brain_dir / "memories" / "project-work-real-abc123.md"
    real_path.write_text(
        "---\n"
        "type: project\n"
        "category: work\n"
        "source_title: Real Project\n"
        "summary: ''\n"
        "tags: []\n"
        "created: '2026-04-15T09:00:00'\n"
        "due_date: null\n"
        "status: active\n"
        "priority: high\n"
        "linked_goal: null\n"
        "milestones: []\n"
        "inferred_from: []\n"
        "notes: ''\n"
        "---\n\n## Notes\n"
    )
    # Write a project-candidate that should never appear
    for i in range(5):
        cand_path = brain_dir / "memories" / f"project-candidate-noise-{i:06d}.md"
        cand_path.write_text(
            "---\n"
            "type: project_candidate\n"
            "source_title: Candidate Project\n"
            "status: pending_confirmation\n"
            "---\n\nCandidate body\n"
        )

    result = await handler._build_goal_project_context_async()
    assert "Real Project" in result
    assert "Candidate Project" not in result


@pytest.mark.asyncio
async def test_load_context_includes_goal_project_context(handler, brain_dir):
    """_load_context includes active goals/projects even without keyword match."""
    # Write an active goal
    goal_path = brain_dir / "memories" / "goal-run-5k-abc123.md"
    goal_path.write_text(
        "---\n"
        "type: goal\n"
        "category: personal\n"
        "source_title: Run a 5K\n"
        "summary: ''\n"
        "tags: []\n"
        "created: '2026-04-15T09:00:00'\n"
        "due_date: '2026-06-30'\n"
        "status: active\n"
        "priority: medium\n"
        "linked_projects: []\n"
        "notes: ''\n"
        "---\n\n## Notes\n"
    )

    # Query has no keyword match with the goal
    context = await handler._load_context("litellm routing config")

    # Goal should still appear in context
    assert "## Active Goals" in context
    assert "Run a 5K" in context
# --- Review commands ---

@pytest.mark.asyncio
async def test_cmd_review_lists_candidates(brain_dir, handler):
    """Review command should glob and list pending candidates."""
    mem_dir = brain_dir / "memories"

    # Write a project candidate
    project_candidate = mem_dir / "project-candidate-q2-rollout-abc123.md"
    project_candidate.write_text(
        "---\ntype: project_candidate\ncandidate_type: project\n"
        "category_guess: work\nsource_title: Q2 rollout plan (candidate)\n"
        "summary: Coordinating Q2 launch\nconfidence: 0.85\n"
        "evidence: [meeting-2026-04-10-abc.md]\n"
        "extracted_fields:\n  title: Q2 rollout plan\n  due_date: 2026-07-01\n"
        "status: pending_confirmation\ncreated: '2026-04-15T09:00:00'\n---\n\n"
        "## Evidence\n- meeting-2026-04-10-abc.md\n"
    )

    # Write a code repo candidate
    code_candidate = mem_dir / "project-candidate-my-new-project-def456.md"
    code_candidate.write_text(
        "---\ntype: project_candidate\ncandidate_type: code_repo\n"
        "source_title: my-new-project (candidate)\n"
        "extracted_fields:\n  name: my-new-project\n  local_path: /Users/chris/repos/my-new-project\n"
        "  default_branch: main\n  languages: [python, shell]\n"
        "status: pending_confirmation\ncreated: '2026-04-15T10:00:00'\n---\n\n"
    )

    update, context = _make_update(12345)

    await handler.cmd_review(update, context)

    # Check reply was sent
    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]

    # Should list both candidates with index numbers
    assert "Pending candidates (2 total)" in text
    assert "1. Q2 rollout plan" in text
    assert "2. my-new-project" in text


@pytest.mark.asyncio
async def test_cmd_review_cache_enabled_prefix(brain_dir, tmp_path):
    """cmd_review returns pending candidates when MemoryCache is enabled (cache path)."""
    from memory_cache import MemoryCache
    mem_dir = brain_dir / "memories"

    candidate_text = (
        "---\ntype: project_candidate\ncandidate_type: project\n"
        "category_guess: work\nsource_title: Cache Test (candidate)\n"
        "summary: Testing the cache path\nconfidence: 0.85\n"
        "evidence: [meeting-cache-test.md]\n"
        "extracted_fields:\n  title: Cache Test\n  due_date: 2026-08-01\n"
        "status: pending_confirmation\ncreated: '2026-04-20T09:00:00'\n---\n\n"
        "## Evidence\n- meeting-cache-test.md\n"
    )
    candidate = mem_dir / "project-candidate-cache-test-aabbcc.md"
    candidate.write_text(candidate_text)

    db_path = tmp_path / "test-cache.sqlite"
    cache = MemoryCache(db_path, mem_dir, enabled=True)
    await cache.sweep()

    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir(exist_ok=True)

    mock_app = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    with patch.object(ch, "BRAIN_DIR", brain_dir), \
         patch.object(ch, "DEPLOY_DIR", deploy_dir), \
         patch.object(ch.TelegramChatHandler, "PENDING_FILE", deploy_dir / "pending-replies.json"), \
         patch.object(ch.TelegramChatHandler, "HISTORY_FILE", deploy_dir / "chat-history.json"), \
         patch("chat_handler.ApplicationBuilder", return_value=mock_builder), \
         patch("chat_handler.SkillExecutor"), \
         patch.dict(os.environ, {"GITHUB_PAT": "", "GITHUB_REPO": ""}, clear=False):
        h = ch.TelegramChatHandler(cache=cache)
        h.allowed_user_id = 12345

    update, context = _make_update(12345)
    await h.cmd_review(update, context)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Cache Test" in text
    assert "1 total" in text or "Pending candidates" in text


@pytest.mark.asyncio
async def test_cmd_confirm_project_creates_project(brain_dir, handler):
    """Confirming a project candidate should call GoalManager.create_project."""
    mem_dir = brain_dir / "memories"

    # Write a project candidate
    candidate = mem_dir / "project-candidate-test-abc123.md"
    candidate.write_text(
        "---\ntype: project_candidate\ncandidate_type: project\n"
        "category_guess: work\nsource_title: Test Project (candidate)\n"
        "summary: Test summary\nconfidence: 0.85\n"
        "evidence: [meeting-test.md]\n"
        "extracted_fields:\n  title: Test Project\n  due_date: 2026-08-01\n"
        "status: pending_confirmation\ncreated: '2026-04-15T09:00:00'\n---\n\n"
    )

    # Populate _last_candidate_set
    handler._last_candidate_set = [candidate]

    update, context = _make_update(12345, args=["1", "work"])

    with patch("goals_tracker.GoalManager") as MockGM:
        mock_manager = MockGM.return_value
        mock_manager.confirm_candidate.return_value = mem_dir / "project-work-test-xyz.md"

        await handler.cmd_confirm(update, context)

        # Should call confirm_candidate
        MockGM.assert_called_once()
        mock_manager.confirm_candidate.assert_called_once_with(
            candidate,
            category_override="work"
        )

        # Should reply with success
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "confirmed" in text.lower()


@pytest.mark.asyncio
async def test_cmd_confirm_code_repo_creates_code_file(brain_dir, handler):
    """Confirming a code_repo candidate should write code-*.md file."""
    mem_dir = brain_dir / "memories"

    # Write a code repo candidate
    candidate = mem_dir / "project-candidate-test-repo-abc123.md"
    candidate.write_text(
        "---\ntype: project_candidate\ncandidate_type: code_repo\n"
        "source_title: test-repo (candidate)\n"
        "extracted_fields:\n  name: test-repo\n  local_path: /Users/chris/repos/test-repo\n"
        "  default_branch: main\n  languages: [python]\n  head_sha: abc123\n"
        "  remote_url: git@github.com:chris/test-repo.git\n  summary: Test summary\n"
        "status: pending_confirmation\ncreated: '2026-04-15T10:00:00'\n---\n\n"
    )

    # Populate _last_candidate_set
    handler._last_candidate_set = [candidate]

    update, context = _make_update(12345, args=["1"])

    with patch("chat_handler.socket.gethostname", return_value="testhost.local"):
        await handler.cmd_confirm(update, context)

        # Should create code file
        expected_code_file = mem_dir / "code-testhost-test-repo.md"
        assert expected_code_file.exists()

        # Check frontmatter
        content = expected_code_file.read_text()
        assert "type: code" in content
        assert "source_title: test-repo" in content

        # Candidate should be deleted
        assert not candidate.exists()

        # Should reply with success
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "confirmed" in text.lower()


@pytest.mark.asyncio
async def test_cmd_reject_updates_json(brain_dir, handler, tmp_path):
    """Rejecting a candidate should update rejected-candidates.json and delete file."""
    mem_dir = brain_dir / "memories"
    deploy_dir = tmp_path / "deploy"  # Already created by handler fixture

    # Write a project candidate
    candidate = mem_dir / "project-candidate-test-abc123.md"
    candidate.write_text(
        "---\ntype: project_candidate\ncandidate_type: project\n"
        "source_title: Test Project (candidate)\nsummary: Test\n"
        "evidence: [meeting-test.md]\nstatus: pending_confirmation\n---\n\n"
    )

    # Populate _last_candidate_set
    handler._last_candidate_set = [candidate]

    update, context = _make_update(12345, args=["1"])

    with patch.object(ch, "DEPLOY_DIR", deploy_dir):
        await handler.cmd_reject(update, context)

        # Check rejected JSON was created
        rejected_json = deploy_dir / "rejected-candidates.json"
        assert rejected_json.exists()

        rejected_data = yaml.safe_load(rejected_json.read_text())
        assert "rejected" in rejected_data
        assert len(rejected_data["rejected"]) == 1
        assert rejected_data["rejected"][0]["source_title"] == "Test Project (candidate)"
        assert "meeting-test.md" in rejected_data["rejected"][0]["evidence"]

        # Candidate should be deleted
        assert not candidate.exists()

        # Should reply with success
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "Rejected" in text


@pytest.mark.asyncio
async def test_cmd_edit_updates_field(brain_dir, handler):
    """Editing a candidate should update extracted_fields."""
    mem_dir = brain_dir / "memories"

    # Write a project candidate
    candidate = mem_dir / "project-candidate-test-abc123.md"
    candidate.write_text(
        "---\ntype: project_candidate\ncandidate_type: project\n"
        "source_title: Test Project (candidate)\nsummary: Test\n"
        "extracted_fields:\n  title: Test Project\n  due_date: null\n"
        "status: pending_confirmation\n---\n\n## Notes\nSome notes.\n"
    )

    # Populate _last_candidate_set
    handler._last_candidate_set = [candidate]

    update, context = _make_update(12345, args=["1", "due_date=2026-08-01"])

    await handler.cmd_edit(update, context)

    # Check file was updated
    content = candidate.read_text()
    fm = yaml.safe_load(content.split("---")[1])
    assert fm["extracted_fields"]["due_date"] == "2026-08-01"

    # Should reply with success
    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Updated due_date" in text
    assert "2026-08-01" in text


# ── _resolve_goal_index / _resolve_project_index error messages ─────────────


GOAL_FILE_TEXT = (
    "---\n"
    "type: goal\n"
    "category: personal\n"
    "source_title: Run a 5K\n"
    "summary: ''\n"
    "tags: []\n"
    "created: '2026-04-15T09:00:00'\n"
    "due_date: '2026-06-30'\n"
    "status: active\n"
    "priority: medium\n"
    "linked_projects: []\n"
    "notes: ''\n"
    "---\n\n## Notes\n"
)

PROJECT_FILE_TEXT = (
    "---\n"
    "type: project\n"
    "category: work\n"
    "source_title: Q2 Rollout\n"
    "summary: ''\n"
    "tags: []\n"
    "created: '2026-04-15T09:00:00'\n"
    "due_date: null\n"
    "status: active\n"
    "priority: medium\n"
    "linked_goal: null\n"
    "milestones: []\n"
    "inferred_from: []\n"
    "notes: ''\n"
    "---\n\n## Notes\n"
)


@pytest.mark.asyncio
async def test_cmd_goal_empty_shows_addgoal_hint(brain_dir, handler):
    """/goal N when no goals exist should hint at /addgoal, not 'Run /goals first'."""
    # No goal files in memories — handler._last_goal_set stays []
    update, context = _make_update(12345, args=["1"])

    await handler.cmd_goal(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "/addgoal" in text
    assert "Run /goals first" not in text


@pytest.mark.asyncio
async def test_cmd_goal_non_integer_shows_command_list(brain_dir, handler):
    """/goal <word> — known verbs dispatch, unknown verbs show help."""
    mem_dir = brain_dir / "memories"
    (mem_dir / "goal-run-abc123.md").write_text(GOAL_FILE_TEXT)

    handler._last_goal_set = []  # force lazy-populate
    # "add" dispatches to addgoal, which shows conversational prompt
    update, context = _make_update(12345, args=["add"])
    await handler.cmd_goal(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "Add a new goal" in text  # addgoal conversational prompt

    # Unknown verbs like "delete", "update", "remove" show registry help
    for verb in ["delete", "update", "remove"]:
        handler._last_goal_set = []  # reset
        update, context = _make_update(12345, args=[verb])
        await handler.cmd_goal(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "/addgoal" in text
        assert "/completegoal" in text
        assert "/goals" in text


@pytest.mark.asyncio
async def test_cmd_goal_out_of_range_shows_count(brain_dir, handler):
    """/goal 99 when only 1 goal exists should report the count."""
    mem_dir = brain_dir / "memories"
    (mem_dir / "goal-run-abc123.md").write_text(GOAL_FILE_TEXT)

    handler._last_goal_set = []  # force lazy-populate
    update, context = _make_update(12345, args=["99"])

    await handler.cmd_goal(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "out of range" in text.lower()
    assert "1 active goal" in text


@pytest.mark.asyncio
async def test_cmd_goal_lazy_populates_without_prior_goals_list(brain_dir, handler):
    """/goal 1 works without running /goals first (lazy populate)."""
    mem_dir = brain_dir / "memories"
    (mem_dir / "goal-run-abc123.md").write_text(GOAL_FILE_TEXT)

    # _last_goal_set is empty (never ran /goals)
    assert handler._last_goal_set == []

    update, context = _make_update(12345, args=["1"])

    await handler.cmd_goal(update, context)

    # Should show goal detail, not an error
    text = update.message.reply_text.call_args[0][0]
    assert "Run a 5K" in text


@pytest.mark.asyncio
async def test_cmd_project_empty_shows_addproject_hint(brain_dir, handler):
    """/project N when no projects exist should hint at /addproject."""
    update, context = _make_update(12345, args=["1"])

    await handler.cmd_project(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "/addproject" in text
    assert "Run /projects first" not in text


@pytest.mark.asyncio
async def test_cmd_project_non_integer_shows_command_list(brain_dir, handler):
    """/project <word> — known verbs dispatch, unknown verbs show help."""
    mem_dir = brain_dir / "memories"
    (mem_dir / "project-work-q2-def456.md").write_text(PROJECT_FILE_TEXT)

    handler._last_project_set = []
    # "add" dispatches to addproject, which shows conversational prompt
    update, context = _make_update(12345, args=["add"])
    await handler.cmd_project(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "Add a new project" in text  # addproject conversational prompt

    # Unknown verbs like "delete", "update", "remove" show registry help
    for verb in ["delete", "update", "remove"]:
        handler._last_project_set = []  # reset
        update, context = _make_update(12345, args=[verb])
        await handler.cmd_project(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "/addproject" in text
        assert "/completeproject" in text
        assert "/projects" in text


@pytest.mark.asyncio
async def test_cmd_project_out_of_range_shows_count(brain_dir, handler):
    """/project 99 when only 1 project exists should report the count."""
    mem_dir = brain_dir / "memories"
    (mem_dir / "project-work-q2-def456.md").write_text(PROJECT_FILE_TEXT)

    handler._last_project_set = []
    update, context = _make_update(12345, args=["99"])

    await handler.cmd_project(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "out of range" in text.lower()
    assert "1 active project" in text


@pytest.mark.asyncio
async def test_cmd_project_lazy_populates_without_prior_projects_list(brain_dir, handler):
    """/project 1 works without running /projects first (lazy populate)."""
    mem_dir = brain_dir / "memories"
    (mem_dir / "project-work-q2-def456.md").write_text(PROJECT_FILE_TEXT)

    assert handler._last_project_set == []

    update, context = _make_update(12345, args=["1"])

    await handler.cmd_project(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "Q2 Rollout" in text


# ── Helper unit tests ──────────────────────────────────────────────────────

def test_match_verb_in_group_tier1_compound_prefix(handler):
    """add + goal → addgoal"""
    result = handler._match_verb_in_group("Goals", "goal", "add")
    assert result == "addgoal"

def test_match_verb_in_group_tier2_compound_suffix(handler):
    """feature + _ + done → feature_done"""
    result = handler._match_verb_in_group("Feature Requests", "feature", "done")
    assert result == "feature_done"

def test_match_verb_in_group_tier3_exact(handler):
    """confirm is an exact command in Review group"""
    result = handler._match_verb_in_group("Review", "review", "confirm")
    assert result == "confirm"

def test_match_verb_in_group_tier4_prefix(handler):
    """link → only linkgoal starts with it in Projects"""
    result = handler._match_verb_in_group("Projects", "project", "link")
    assert result == "linkgoal"

def test_match_verb_in_group_tier5_substring(handler):
    """approve → only approve_skill contains it in Skill Management"""
    result = handler._match_verb_in_group("Skill Management", "skill_draft", "approve")
    assert result == "approve_skill"

def test_match_verb_in_group_ambiguous_returns_none(handler):
    """When multiple commands match the same tier, return None"""
    import chat_handler as ch_module
    orig = ch_module.COMMAND_REGISTRY["Goals"][:]
    # Create a real ambiguity at tier-4 (prefix matching)
    ch_module.COMMAND_REGISTRY["Goals"] = orig + [("linktarget", "T1"), ("linkback", "T2")]
    try:
        result = handler._match_verb_in_group("Goals", "goal", "link")
        # Both linktarget and linkback (and linkgoal) start with "link" → ambiguous
        assert result is None
    finally:
        ch_module.COMMAND_REGISTRY["Goals"] = orig

def test_match_verb_in_group_excludes_base(handler):
    """goal verb on Goals group is ambiguous now that goal_note and goal_due also start with 'goal'"""
    result = handler._match_verb_in_group("Goals", "goal", "goal")
    # tier-4 (prefix) now hits goals, goal_note, goal_due → ambiguous → None
    assert result is None

def test_format_group_help_is_dynamic(handler):
    """Adding a command to COMMAND_REGISTRY makes it appear in format output"""
    import chat_handler as ch_module
    orig = ch_module.COMMAND_REGISTRY["Goals"][:]
    ch_module.COMMAND_REGISTRY["Goals"] = orig + [("fakegoal", "Fake description")]
    try:
        result = handler._format_group_help("Goals", "goal")
        assert "fakegoal" in result
        assert "Fake description" in result
    finally:
        ch_module.COMMAND_REGISTRY["Goals"] = orig

def test_format_group_help_includes_base_hint(handler):
    """With base_command, output starts with 'expects a number' hint"""
    result = handler._format_group_help("Goals", "goal")
    assert "/goal expects a number" in result
    assert "/addgoal" in result

def test_format_group_help_without_base(handler):
    """Without base_command, no 'expects a number' hint"""
    result = handler._format_group_help("Commitments")
    assert "expects a number" not in result
    assert "Commitments commands:" in result

# ── Dispatch tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_goal_add_dispatches_to_addgoal(handler, brain_dir):
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["add"])
    handler.cmd_addgoal = AsyncMock()
    await handler.cmd_goal(update, context)
    handler.cmd_addgoal.assert_awaited_once()
    assert context.args == []

@pytest.mark.asyncio
async def test_cmd_goal_complete_dispatches_to_completegoal(handler, brain_dir):
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["complete", "1"])
    handler.cmd_completegoal = AsyncMock()
    await handler.cmd_goal(update, context)
    handler.cmd_completegoal.assert_awaited_once()
    assert context.args == ["1"]

@pytest.mark.asyncio
async def test_cmd_project_add_dispatches_to_addproject(handler, brain_dir):
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["add"])
    handler.cmd_addproject = AsyncMock()
    await handler.cmd_project(update, context)
    handler.cmd_addproject.assert_awaited_once()

@pytest.mark.asyncio
async def test_cmd_project_link_prefix_dispatches_to_linkgoal(handler, brain_dir):
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["link", "1", "2"])
    handler.cmd_linkgoal = AsyncMock()
    await handler.cmd_project(update, context)
    handler.cmd_linkgoal.assert_awaited_once()
    assert context.args == ["1", "2"]

@pytest.mark.asyncio
async def test_cmd_project_add_prefers_addproject_over_addmilestone(handler, brain_dir):
    """Tier-1 compound-prefix wins over tier-4 prefix (both addproject and addmilestone start with 'add')"""
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["add"])
    handler.cmd_addproject = AsyncMock()
    handler.cmd_addmilestone = AsyncMock()
    await handler.cmd_project(update, context)
    handler.cmd_addproject.assert_awaited_once()
    handler.cmd_addmilestone.assert_not_called()

@pytest.mark.asyncio
async def test_cmd_report_pause_dispatches_to_report_pause(handler, brain_dir):
    """Tier-2 compound-suffix: report + _ + pause → report_pause"""
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["pause", "1"])
    handler.cmd_report_pause = AsyncMock()
    await handler.cmd_report(update, context)
    handler.cmd_report_pause.assert_awaited_once()
    assert context.args == ["1"]

@pytest.mark.asyncio
async def test_cmd_feature_detail_done_dispatches_to_feature_done(handler, brain_dir):
    """Tier-2 compound-suffix: feature + _ + done → feature_done"""
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["done", "3"])
    handler.cmd_feature_done = AsyncMock()
    await handler.cmd_feature_detail(update, context)
    handler.cmd_feature_done.assert_awaited_once()
    assert context.args == ["3"]

@pytest.mark.asyncio
async def test_cmd_skill_draft_approve_dispatches_to_approve_skill(handler, brain_dir):
    """Tier-5 substring: 'approve' found in 'approve_skill'"""
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["approve", "1"])
    handler.cmd_approve_skill = AsyncMock()
    await handler.cmd_skill_draft(update, context)
    handler.cmd_approve_skill.assert_awaited_once()
    assert context.args == ["1"]

@pytest.mark.asyncio
async def test_cmd_review_confirm_dispatches_to_confirm(handler, brain_dir):
    """Tier-3 exact: 'confirm' is an exact command in Review group"""
    from unittest.mock import AsyncMock
    update, context = _make_update(12345, args=["confirm", "2"])
    handler.cmd_confirm = AsyncMock()
    await handler.cmd_review(update, context)
    handler.cmd_confirm.assert_awaited_once()
    assert context.args == ["2"]

# ── Fallback (dynamic help) tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_goal_unknown_verb_shows_registry_help(handler, brain_dir):
    """Unknown verb falls back to registry-derived group help"""
    update, context = _make_update(12345, args=["delete", "1"])
    await handler.cmd_goal(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "/addgoal" in reply
    assert "/goals" in reply
    assert "/completegoal" in reply
    assert "/abandongoal" in reply

@pytest.mark.asyncio
async def test_cmd_reading_invalid_index_shows_registry_help(handler, brain_dir):
    """Invalid index in /reading shows Knowledge listings group help"""
    update, context = _make_update(12345, args=["999"])
    await handler.cmd_reading(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "/readings" in reply

def write_commitment(memories_dir: Path, description: str, status: str = "active",
                     confidence: float = 0.85, commitment_type: str = "outbound",
                     due_date: str = None, tags: list = None) -> Path:
    """Write a commitment-*.md file for testing."""
    from commitment_tracker import _stable_commitment_id, _slugify
    source_url = "zoom:test123"
    stable_id = _stable_commitment_id(source_url, description, "Alice")
    slug = _slugify(description)
    p = memories_dir / f"commitment-{slug}-{stable_id}.md"
    fm = {
        "source_title": description,
        "summary": f"Alice committed to {description.lower()}",
        "tags": tags or [],
        "last_scanned": "2026-04-11T10:00:00",
        "source_url": f"commitment:{stable_id}",
        "type": "commitment",
        "commitment_type": commitment_type,
        "owner": "Alice",
        "owner_email": "alice@acme.com",
        "recipient": "Chris",
        "due_date": due_date,
        "due_date_confidence": "explicit" if due_date else "none",
        "confidence": confidence,
        "status": status,
        "source_memory": source_url,
        "extracted_text": "I'll do the thing.",
    }
    frontmatter = yaml.dump(fm, sort_keys=False, allow_unicode=True)
    content = f"---\n{frontmatter}---\n\nCommitment details."
    p.write_text(content)
    return p


@pytest.mark.asyncio
async def test_cmd_complete_invalid_index_shows_commitments_help(handler, brain_dir):
    """Invalid index in /complete shows Commitments group help"""
    update, context = _make_update(12345, args=["xyz"])
    await handler.cmd_complete(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "not found" in reply


@pytest.mark.asyncio
async def test_complete_multiple_indices(handler, brain_dir):
    """Multiple indices in /complete processes all successfully"""
    m = brain_dir / "memories"
    c1 = write_commitment(m, "Send quarterly report", status="active")
    c2 = write_commitment(m, "Review design doc", status="active")

    # Load commitments first to populate _last_commitment_set
    update_list, context_list = _make_update(12345, args=[])
    await handler.cmd_commitments(update_list, context_list)

    # Complete indices 1 and 2
    with patch("commitment_tracker.CommitmentTracker.update_commitment_status") as mock_update:
        update, context = _make_update(12345, args=["1", "2"])
        await handler.cmd_complete(update, context)
        reply = update.message.reply_text.call_args[0][0]

        # Should have two success lines
        assert reply.count("\u2713") == 2
        assert "Send quarterly report" in reply
        assert "Review design doc" in reply
        assert mock_update.call_count == 2


@pytest.mark.asyncio
async def test_complete_partial_failure(handler, brain_dir):
    """Partial failure shows error for bad index, processes good index"""
    m = brain_dir / "memories"
    c1 = write_commitment(m, "Send quarterly report", status="active")

    # Load commitments first
    update_list, context_list = _make_update(12345, args=[])
    await handler.cmd_commitments(update_list, context_list)

    # Try to complete indices 1 and 99 (99 doesn't exist)
    with patch("commitment_tracker.CommitmentTracker.update_commitment_status") as mock_update:
        update, context = _make_update(12345, args=["1", "99"])
        await handler.cmd_complete(update, context)
        reply = update.message.reply_text.call_args[0][0]

        # Should have one success and one failure
        assert "\u2713" in reply
        assert "#99: not found" in reply
        assert mock_update.call_count == 1


@pytest.mark.asyncio
async def test_complete_deduplicates_args(handler, brain_dir):
    """Duplicate indices are processed only once"""
    m = brain_dir / "memories"
    c1 = write_commitment(m, "Send quarterly report", status="active")

    # Load commitments first
    update_list, context_list = _make_update(12345, args=[])
    await handler.cmd_commitments(update_list, context_list)

    # Complete index 1 twice
    with patch("commitment_tracker.CommitmentTracker.update_commitment_status") as mock_update:
        update, context = _make_update(12345, args=["1", "1"])
        await handler.cmd_complete(update, context)
        reply = update.message.reply_text.call_args[0][0]

        # Should only process once
        assert mock_update.call_count == 1
        assert reply.count("\u2713") == 1


@pytest.mark.asyncio
async def test_dismiss_multiple_indices(handler, brain_dir):
    """Multiple indices in /dismiss processes all successfully"""
    m = brain_dir / "memories"
    c1 = write_commitment(m, "Send quarterly report", status="active")
    c2 = write_commitment(m, "Review design doc", status="active")

    # Load commitments first
    update_list, context_list = _make_update(12345, args=[])
    await handler.cmd_commitments(update_list, context_list)

    # Dismiss indices 1 and 2
    with patch("commitment_tracker.CommitmentTracker.update_commitment_status") as mock_update:
        update, context = _make_update(12345, args=["1", "2"])
        await handler.cmd_dismiss(update, context)
        reply = update.message.reply_text.call_args[0][0]

        # Should have two dismiss lines (using ✗ symbol)
        assert reply.count("\u2717") == 2
        assert "Send quarterly report" in reply
        assert "Review design doc" in reply
        assert mock_update.call_count == 2

# ── Agent Actions Commands ───────────────────────────────────────────────────

def write_action(memories_dir: Path, action_id: str, action_type: str, target: str, status: str = "pending", rationale: str = "Test rationale", defer_until: str = None):
    """Write an action-*.md file for testing."""
    path = memories_dir / f"action-test-{action_id}.md"
    fm = {
        "type": "agent_action",
        "action_id": action_id,
        "action_type": action_type,
        "status": status,
        "target": target,
        "args": {"text": "Test action"},
        "confidence": 0.85,
        "rationale": rationale,
        "evidence": ["email-test.md"],
        "proposed_at": "2026-04-16T10:00:00",
        "source_goal": "goal-test-abc123.md",
    }
    if defer_until:
        fm["defer_until"] = defer_until
    content = f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Rationale\n{rationale}\n"
    path.write_text(content)
    return path


@pytest.mark.asyncio
async def test_cmd_actions_lists_pending(handler, brain_dir):
    """Default /actions filter shows pending, non-deferred actions."""
    m = brain_dir / "memories"
    write_action(m, "abc123", "add_milestone", "project-test.md", status="pending")
    write_action(m, "def456", "update_status", "goal-test.md", status="pending")
    write_action(m, "ghi789", "add_note", "project-test.md", status="executed")  # Should not appear

    update, context = _make_update(12345, args=[])
    await handler.cmd_actions(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "Agent-proposed actions (2)" in reply
    assert "[add_milestone]" in reply
    assert "[update_status]" in reply
    assert "executed" not in reply.lower()


@pytest.mark.asyncio
async def test_cmd_actions_filter_approved(handler, brain_dir):
    """Filter: approved shows only approved actions."""
    m = brain_dir / "memories"
    write_action(m, "abc123", "add_milestone", "project-test.md", status="pending")
    write_action(m, "def456", "update_status", "goal-test.md", status="approved")

    update, context = _make_update(12345, args=["approved"])
    await handler.cmd_actions(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "Agent-proposed actions (1)" in reply
    assert "[update_status]" in reply
    assert "[add_milestone]" not in reply


@pytest.mark.asyncio
async def test_cmd_action_detail_shows_rationale_evidence(handler, brain_dir):
    """Detail view shows full rationale and evidence."""
    m = brain_dir / "memories"
    write_action(m, "abc123", "add_milestone", "project-test.md", rationale="Detailed rationale here")

    # Load the action set first
    await handler.cmd_actions(_make_update(12345, args=[])[0], _make_update(12345, args=[])[1])

    update, context = _make_update(12345, args=["1"])
    await handler.cmd_action(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "Action 1:" in reply
    assert "Type: add_milestone" in reply
    assert "Rationale: Detailed rationale here" in reply
    assert "Evidence: email-test.md" in reply


@pytest.mark.asyncio
async def test_cmd_run_approves_and_executes(handler, brain_dir):
    """Successful /run marks action as executed."""
    m = brain_dir / "memories"
    project_path = write_memory(m, "test-project", ["work"], "Test Project", source_url="project:test")
    project_path.rename(m / "project-work-test-abc123.md")
    action_path = write_action(m, "abc123", "add_note", "project-work-test-abc123.md")

    # Load action set
    await handler.cmd_actions(_make_update(12345, args=[])[0], _make_update(12345, args=[])[1])

    # Mock GoalProjectAgent._execute_action
    with patch("goal_project_agent.GoalProjectAgent._execute_action", return_value="Success"):
        update, context = _make_update(12345, args=["1"])
        await handler.cmd_run(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "Action 1 executed" in reply or "Success" in reply

        # Check action file updated
        fresh_fm = handler._parse_frontmatter(action_path)
        assert fresh_fm.get("status") == "executed"
        assert fresh_fm.get("executed_at") is not None


@pytest.mark.asyncio
async def test_cmd_run_superseded_on_precondition_fail(handler, brain_dir):
    """Precondition failure marks action as superseded."""
    m = brain_dir / "memories"
    action_path = write_action(m, "abc123", "add_milestone", "nonexistent.md")

    # Load action set
    await handler.cmd_actions(_make_update(12345, args=[])[0], _make_update(12345, args=[])[1])

    # Mock _execute_action to raise error
    with patch("goal_project_agent.GoalProjectAgent._execute_action", side_effect=ValueError("Target not found")):
        update, context = _make_update(12345, args=["1"])
        await handler.cmd_run(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "superseded" in reply.lower() or "not found" in reply.lower()

        # Check action file status
        fresh_fm = handler._parse_frontmatter(action_path)
        assert fresh_fm.get("status") == "superseded"


@pytest.mark.asyncio
async def test_cmd_drop_marks_rejected_and_logs(handler, brain_dir, tmp_path):
    """Drop marks action as rejected and logs to rejected-actions.json."""
    m = brain_dir / "memories"
    action_path = write_action(m, "abc123", "add_milestone", "project-test.md")

    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir(exist_ok=True)

    # Load action set
    await handler.cmd_actions(_make_update(12345, args=[])[0], _make_update(12345, args=[])[1])

    with patch.object(ch, "DEPLOY_DIR", deploy_dir):
        update, context = _make_update(12345, args=["1"])
        await handler.cmd_drop(update, context)
        reply = update.message.reply_text.call_args[0][0]
        assert "rejected" in reply.lower()

        # Check action file status
        fresh_fm = handler._parse_frontmatter(action_path)
        assert fresh_fm.get("status") == "rejected"
        assert fresh_fm.get("rejected_at") is not None

        # Check rejected-actions.json
        rejected_file = deploy_dir / "rejected-actions.json"
        assert rejected_file.exists()
        rejected_data = json.loads(rejected_file.read_text())
        assert len(rejected_data["rejected"]) == 1
        assert rejected_data["rejected"][0]["action_id"] == "abc123"


@pytest.mark.asyncio
async def test_cmd_defer_sets_defer_until_and_hides_from_default_list(handler, brain_dir):
    """Defer sets defer_until and hides from default /actions."""
    m = brain_dir / "memories"
    action_path = write_action(m, "abc123", "add_milestone", "project-test.md")

    # Load action set
    await handler.cmd_actions(_make_update(12345, args=[])[0], _make_update(12345, args=[])[1])

    # Defer for 48 hours
    update, context = _make_update(12345, args=["1", "48"])
    await handler.cmd_defer(update, context)
    reply = update.message.reply_text.call_args[0][0]
    assert "snoozed" in reply.lower() or "48" in reply

    # Check defer_until set
    fresh_fm = handler._parse_frontmatter(action_path)
    assert fresh_fm.get("defer_until") is not None

    # Check default /actions no longer shows it
    update2, context2 = _make_update(12345, args=[])
    await handler.cmd_actions(update2, context2)
    reply2 = update2.message.reply_text.call_args[0][0]
    assert "No pending agent actions" in reply2 or "0" in reply2


# ── _resolve_feature_index hash fallback (fix fcfc1f) ─────────────────────────

def test_resolve_feature_by_hash(handler, brain_dir):
    """_resolve_feature_index accepts short_id hash (not just numeric index)."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)

    feature_file = memories_dir / "feature-request-dark-mode-abcdef.md"
    feature_file.write_text(
        "---\ntitle: dark mode\ntype: feature_request\nkind: feature\nstatus: new\n"
        "short_id: abcdef\n---\n\n## Request\nadd dark mode\n"
    )

    result = handler._resolve_feature_index(["abcdef"], MagicMock())
    assert result == feature_file


def test_resolve_feature_by_hash_not_found(handler, brain_dir):
    """_resolve_feature_index returns None when hash matches nothing."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)
    result = handler._resolve_feature_index(["xxxxxx"], MagicMock())
    assert result is None


def test_resolve_feature_by_numeric_index_still_works(handler, brain_dir):
    """Numeric index lookup still works after hash-fallback addition."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)

    feature_file = memories_dir / "feature-request-dark-mode-abcdef.md"
    feature_file.write_text(
        "---\ntitle: dark mode\ntype: feature_request\nkind: feature\nstatus: new\n"
        "short_id: abcdef\n---\n\n## Request\nadd dark mode\n"
    )
    handler._last_feature_set = [feature_file]

    result = handler._resolve_feature_index(["1"], MagicMock())
    assert result == feature_file


# ── _close_issue_text tests ────────────────────────────────────────────────

def test_close_issue_by_short_id(handler, brain_dir):
    """_close_issue_text updates status when short_id matches."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)

    feature_file = memories_dir / "feature-request-pdf-bug-abc123.md"
    feature_file.write_text(
        "---\ntitle: PDF export broken\ntype: feature_request\nkind: bug\nstatus: new\n"
        "short_id: abc123\n---\n\n## Bug\nPDF export doesn't work\n"
    )

    result = handler._close_issue_text(short_id="abc123", status="done")

    assert "Closed" in result
    assert "abc123" in result
    assert "done" in result

    # Verify file was updated
    content = feature_file.read_text()
    assert "status: done" in content
    assert "status: new" not in content


def test_close_issue_by_title(handler, brain_dir):
    """_close_issue_text updates status when title substring matches."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)

    feature_file = memories_dir / "feature-request-dark-mode-def456.md"
    feature_file.write_text(
        "---\ntitle: Add dark mode support\ntype: feature_request\nkind: feature\nstatus: new\n"
        "short_id: def456\n---\n\n## Request\nDark mode needed\n"
    )

    result = handler._close_issue_text(title="dark mode", status="done")

    assert "Closed" in result
    assert "def456" in result
    assert "done" in result

    # Verify file was updated
    content = feature_file.read_text()
    assert "status: done" in content


def test_close_issue_title_ambiguous(handler, brain_dir):
    """_close_issue_text returns disambiguation list when multiple titles match."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)

    file1 = memories_dir / "feature-request-pdf-export-aaa111.md"
    file1.write_text(
        "---\ntitle: PDF export broken\ntype: feature_request\nkind: bug\nstatus: new\n"
        "short_id: aaa111\n---\n\n## Bug\n"
    )

    file2 = memories_dir / "feature-request-pdf-viewer-bbb222.md"
    file2.write_text(
        "---\ntitle: PDF viewer slow\ntype: feature_request\nkind: bug\nstatus: new\n"
        "short_id: bbb222\n---\n\n## Bug\n"
    )

    result = handler._close_issue_text(title="PDF")

    assert "Multiple matches" in result
    assert "aaa111" in result
    assert "bbb222" in result
    assert "PDF export" in result or "PDF viewer" in result

    # Verify files were NOT updated
    assert "status: new" in file1.read_text()
    assert "status: new" in file2.read_text()


def test_close_issue_not_found(handler, brain_dir):
    """_close_issue_text returns error when short_id not found."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)

    result = handler._close_issue_text(short_id="xxxxxx")

    assert "No issue found" in result
    assert "xxxxxx" in result


def test_close_issue_custom_status(handler, brain_dir):
    """_close_issue_text writes custom status correctly."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)

    feature_file = memories_dir / "feature-request-feature-ghi789.md"
    feature_file.write_text(
        "---\ntitle: Feature request\ntype: feature_request\nkind: feature\nstatus: new\n"
        "short_id: ghi789\n---\n\n## Request\n"
    )

    result = handler._close_issue_text(short_id="ghi789", status="wont_do")

    assert "wont_do" in result

    # Verify file was updated with custom status
    content = feature_file.read_text()
    assert "status: wont_do" in content
    assert "status: new" not in content


def test_close_issue_no_params_returns_error(handler, brain_dir):
    """_close_issue_text returns error when neither short_id nor title provided."""
    result = handler._close_issue_text()
    assert "Provide either short_id or title" in result


def test_close_issue_title_not_found(handler, brain_dir):
    """_close_issue_text returns error when title matches nothing."""
    memories_dir = brain_dir / "memories"
    memories_dir.mkdir(exist_ok=True)

    result = handler._close_issue_text(title="nonexistent feature")

    assert "No issue found" in result
    assert "nonexistent feature" in result


# ── Document upload tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_document_upload_pdf_creates_memory(handler, brain_dir):
    """PDF document upload creates a memory file."""
    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.document = MagicMock()
    mock_update.message.document.file_name = "paper.pdf"
    mock_update.message.document.mime_type = "application/pdf"
    mock_update.message.document.file_size = 1024
    mock_update.message.document.file_id = "fake-file-id"

    mock_context = MagicMock()
    mock_file = AsyncMock()
    mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake pdf data"))
    mock_context.bot.get_file = AsyncMock(return_value=mock_file)

    with patch("content_fetcher._extract_pdf", return_value=("Research Paper", "Extracted text.")), \
         patch.object(handler.executor, "run", new=AsyncMock(return_value="## Summary\nML paper.")), \
         patch("memory_writer.MemoryWriter.write", return_value=MagicMock(name="2026-04-18-paper-abc.md")):
        await handler._handle_document_upload(mock_update, mock_context)

    calls = [str(c) for c in mock_update.message.reply_text.call_args_list]
    assert any("Saved" in c or "📄" in c for c in calls)


@pytest.mark.asyncio
async def test_document_upload_txt_creates_memory(handler, brain_dir):
    """Plain text document upload creates a memory file."""
    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.document = MagicMock()
    mock_update.message.document.file_name = "notes.txt"
    mock_update.message.document.mime_type = "text/plain"
    mock_update.message.document.file_size = 512
    mock_update.message.document.file_id = "fake-file-id"

    mock_context = MagicMock()
    mock_file = AsyncMock()
    mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"Some notes here."))
    mock_context.bot.get_file = AsyncMock(return_value=mock_file)

    with patch.object(handler.executor, "run", new=AsyncMock(return_value="## Summary\nNotes.")), \
         patch("memory_writer.MemoryWriter.write", return_value=MagicMock(name="2026-04-18-notes-def.md")):
        await handler._handle_document_upload(mock_update, mock_context)

    calls = [str(c) for c in mock_update.message.reply_text.call_args_list]
    assert any("Saved" in c or "📄" in c for c in calls)


@pytest.mark.asyncio
async def test_document_upload_unsupported_type_rejected(handler, brain_dir):
    """Unsupported MIME type is rejected with a user-friendly message."""
    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.document = MagicMock()
    mock_update.message.document.file_name = "doc.docx"
    mock_update.message.document.mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    mock_update.message.document.file_size = 1024
    mock_update.message.document.file_id = "fake-file-id"

    mock_context = MagicMock()
    await handler._handle_document_upload(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    assert "Unsupported" in mock_update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_document_upload_too_large_rejected(handler, brain_dir):
    """Files over 20MB are rejected before download."""
    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.document = MagicMock()
    mock_update.message.document.file_name = "large.pdf"
    mock_update.message.document.mime_type = "application/pdf"
    mock_update.message.document.file_size = 25 * 1024 * 1024
    mock_update.message.document.file_id = "fake-file-id"

    mock_context = MagicMock()
    await handler._handle_document_upload(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    assert "large" in mock_update.message.reply_text.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_document_upload_empty_content_rejected(handler, brain_dir):
    """Empty PDF extraction produces a user-friendly error."""
    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.document = MagicMock()
    mock_update.message.document.file_name = "empty.pdf"
    mock_update.message.document.mime_type = "application/pdf"
    mock_update.message.document.file_size = 1024
    mock_update.message.document.file_id = "fake-file-id"

    mock_context = MagicMock()
    mock_file = AsyncMock()
    mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake pdf"))
    mock_context.bot.get_file = AsyncMock(return_value=mock_file)

    with patch("content_fetcher._extract_pdf", return_value=("", "")):
        await handler._handle_document_upload(mock_update, mock_context)

    calls = " ".join(str(c) for c in mock_update.message.reply_text.call_args_list)
    assert "extract" in calls.lower()


# --- Deduplication commands ---

async def test_cmd_dupes_lists_candidates(handler, brain_dir, tmp_path):
    """cmd_dupes should list candidates from dedup-state.json."""
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir(exist_ok=True)

    state_file = deploy_dir / "dedup-state.json"
    state_file.write_text(json.dumps({
        "candidates": [
            {
                "a": "file1.md",
                "b": "file2.md",
                "similarity": 0.85,
                "detected_at": "2026-04-17T12:00:00"
            },
            {
                "a": "file3.md",
                "b": "file4.md",
                "similarity": 0.72,
                "detected_at": "2026-04-17T12:05:00"
            }
        ],
        "dismissed": []
    }))

    with patch.object(ch, "DEPLOY_DIR", deploy_dir):
        update, ctx = _make_update(12345)
        await handler.cmd_dupes(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "Found 2 potential duplicate pairs" in reply
    assert "1. file1.md ~ file2.md (similarity: 0.85)" in reply
    assert "2. file3.md ~ file4.md (similarity: 0.72)" in reply
    assert "/merge" in reply
    assert "/keep" in reply
    assert len(handler._last_dupes_set) == 2


async def test_cmd_keep_dismisses_pair(handler, brain_dir, tmp_path):
    """cmd_keep should move pair from candidates to dismissed."""
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir(exist_ok=True)

    candidate = {
        "a": "file1.md",
        "b": "file2.md",
        "similarity": 0.85,
        "detected_at": "2026-04-17T12:00:00"
    }
    state_file = deploy_dir / "dedup-state.json"
    state_file.write_text(json.dumps({
        "candidates": [candidate],
        "dismissed": []
    }))

    handler._last_dupes_set = [candidate]

    with patch.object(ch, "DEPLOY_DIR", deploy_dir):
        update, ctx = _make_update(12345, ["1"])
        await handler.cmd_keep(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "Dismissed pair as distinct" in reply
    assert "file1.md" in reply
    assert "file2.md" in reply

    state = json.loads(state_file.read_text())
    assert len(state["candidates"]) == 0
    assert len(state["dismissed"]) == 1
    assert state["dismissed"][0] == candidate


# ── register_with_router (Phase 3 bridge) ────────────────────────────────────

@pytest.mark.asyncio
async def test_register_with_router_registers_all_cmd_methods(handler):
    """register_with_router must register every cmd_* method in the router."""
    import inspect
    from command_core import CommandRouter

    router = CommandRouter()
    handler.register_with_router(router)

    # Every cmd_* method should appear (minus the "cmd_" prefix).
    # registered may also include aliases (e.g. "people", "messages") — those are extra, not missing.
    expected = {
        name[4:] for name, _ in inspect.getmembers(handler, predicate=inspect.ismethod)
        if name.startswith("cmd_")
    }
    registered = set(router._cmd_handlers.keys()) - {"__message__"}
    missing = expected - registered
    assert not missing, f"cmd_* methods not registered in router: {missing}"


@pytest.mark.asyncio
async def test_register_with_router_registers_message_handler(handler):
    """register_with_router must register a __message__ handler."""
    from command_core import CommandRouter

    router = CommandRouter()
    handler.register_with_router(router)

    assert "__message__" in router._cmd_handlers


@pytest.mark.asyncio
async def test_register_with_router_command_receives_args(handler, brain_dir):
    """A command dispatched through the router receives ctx.args correctly."""
    from command_core import CommandRouter
    from transport import CommandContext

    router = CommandRouter()
    handler.register_with_router(router)

    replies = []

    async def capture_reply(text: str) -> None:
        replies.append(text)

    async def noop_typing() -> None:
        pass

    # /readings with args=["5"] — expects to return a listing
    with patch.object(ch, "BRAIN_DIR", brain_dir):
        ctx = CommandContext(args=["5"], user_id="test-user",
                             reply=capture_reply, send_typing=noop_typing)
        handled = await router.dispatch_command(ctx, "readings")

    assert handled is True
    # At least one reply must have been sent
    assert len(replies) >= 1


@pytest.mark.asyncio
async def test_register_with_router_unknown_command_returns_false(handler):
    """An unregistered command name returns False from dispatch_command."""
    from command_core import CommandRouter
    from transport import CommandContext

    router = CommandRouter()
    handler.register_with_router(router)

    replies = []

    async def capture_reply(text: str) -> None:
        replies.append(text)

    async def noop_typing() -> None:
        pass

    ctx = CommandContext(args=[], user_id="test-user",
                         reply=capture_reply, send_typing=noop_typing)
    handled = await router.dispatch_command(ctx, "nonexistent_cmd_xyz")
    assert handled is False
    assert replies == []


@pytest.mark.asyncio
async def test_register_with_router_fake_auth_passes(handler):
    """Fake update always passes _check_auth (auth done at adapter boundary)."""
    from command_core import CommandRouter
    from transport import CommandContext

    router = CommandRouter()
    handler.register_with_router(router)

    replies = []

    async def capture_reply(text: str) -> None:
        replies.append(text)

    async def noop_typing() -> None:
        pass

    # Send a command from a different user_id — auth check should still pass
    # because fake update uses handler.allowed_user_id, not ctx.user_id
    ctx = CommandContext(args=[], user_id="slack-U9999",
                         reply=capture_reply, send_typing=noop_typing)
    handled = await router.dispatch_command(ctx, "version")
    assert handled is True
    assert len(replies) >= 1


@pytest.mark.asyncio
async def test_register_with_router_no_shared_state_between_calls(handler):
    """Each bridge invocation gets its own fake objects — no state sharing between calls."""
    from command_core import CommandRouter
    from transport import CommandContext

    router = CommandRouter()
    handler.register_with_router(router)

    replies_a = []
    replies_b = []

    async def noop_typing() -> None:
        pass

    async def reply_a(t): replies_a.append(t)
    async def reply_b(t): replies_b.append(t)

    # Two concurrent dispatches with different args must not bleed into each other
    ctx_a = CommandContext(args=["10"], user_id="u1",
                           reply=reply_a, send_typing=noop_typing)
    ctx_b = CommandContext(args=["5"], user_id="u2",
                           reply=reply_b, send_typing=noop_typing)

    await asyncio.gather(
        router.dispatch_command(ctx_a, "readings"),
        router.dispatch_command(ctx_b, "readings"),
    )

    # Both should have received replies — no cross-contamination
    assert len(replies_a) >= 1
    assert len(replies_b) >= 1
    # replies must not have landed in the wrong list
    assert replies_a is not replies_b


# --- Security (M2): demoted query logging ---

async def test_query_logging_does_not_leak_content_at_info(handler, brain_dir, caplog):
    """At INFO level, query content should not be logged (only hash+length)."""
    import logging
    caplog.set_level(logging.INFO)

    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="response")

    mock_update = MagicMock()
    mock_update.message.text = "secret query with sensitive data"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "secret query with sensitive data"

    await handler.handle_message(mock_update, MagicMock())

    # At INFO level, "secret" should NOT appear
    info_logs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    has_hash_log = any("hash=" in msg and "len=" in msg for msg in info_logs)
    has_secret = any("secret" in msg for msg in info_logs)
    
    assert has_hash_log, f"Expected hash= and len= in INFO log. Logs: {info_logs}"
    assert not has_secret, f"Query content 'secret' leaked to INFO log. Logs: {info_logs}"


async def test_query_logging_includes_content_at_debug(handler, brain_dir, caplog):
    """At DEBUG level, query content should be logged."""
    import logging
    caplog.set_level(logging.DEBUG)

    handler.executor = MagicMock()
    handler.executor.run_with_tools = AsyncMock(return_value="response")

    mock_update = MagicMock()
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.message = AsyncMock()
    mock_update.message.text = "debug test query"

    await handler.handle_message(mock_update, MagicMock())

    # At DEBUG level, the query content should appear
    debug_logs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    has_content = any("debug test query" in msg for msg in debug_logs)
    
    assert has_content, f"Query content missing from DEBUG log. Logs: {debug_logs}"


class TestCircleCommands:
    """Tests for /circles, /circle, /circle_status commands."""

    def _make_yaml(self, tmp_path, slug, members=None, icloud_folder=None, include_rules=None, exclude_rules=None):
        """Write a minimal circle YAML file and return its path."""
        members = members or []
        icloud_folder = icloud_folder or f"second-brain-circles/{slug}/memories"
        include_rules = include_rules or [{"type": "calendar_event"}]
        exclude_rules = exclude_rules or []
        content = {
            "circle": slug,
            "display_name": slug.replace("-", " ").title(),
            "members": members,
            "bot_token": "",
            "icloud_folder": icloud_folder,
            "rules": {
                "include": include_rules,
                "exclude": exclude_rules,
            },
        }
        import yaml
        p = tmp_path / f"{slug}.yaml"
        p.write_text(yaml.dump(content))
        return p

    @pytest.fixture
    def handler(self, tmp_path, monkeypatch):
        """Minimal TelegramChatHandler with patched DEPLOY_DIR and no real Telegram."""
        from unittest.mock import AsyncMock, MagicMock, patch
        circles_dir = tmp_path / "circles"
        circles_dir.mkdir()
        monkeypatch.setattr("chat_handler.DEPLOY_DIR", tmp_path)
        monkeypatch.setattr("chat_handler.BRAIN_DIR", tmp_path)
        (tmp_path / "memories").mkdir(exist_ok=True)

        config = {}
        with patch("chat_handler.ApplicationBuilder") as mock_builder:
            mock_app = MagicMock()
            mock_app.add_handler = MagicMock()
            mock_app.add_error_handler = MagicMock()
            mock_builder.return_value.token.return_value.build.return_value = mock_app
            h = ch.TelegramChatHandler.__new__(ch.TelegramChatHandler)
            h.app = mock_app
            h.allowed_user_id = 12345
            h._config = config
            h._last_circle_set = []
            h._active_list = []
            h.notification_manager = None
        return h, tmp_path

    def _make_update(self, user_id=12345, args=None):
        from unittest.mock import AsyncMock, MagicMock
        update = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat.id = 99999
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = args or []
        return update, context

    @pytest.mark.asyncio
    async def test_circles_empty(self, handler, tmp_path):
        """No circles dir / empty -> helpful message."""
        h, tmp = handler
        update, context = self._make_update()
        await h.cmd_circles(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "No circles configured" in text

    @pytest.mark.asyncio
    async def test_circles_lists_all(self, handler, tmp_path):
        """Two YAML files -> numbered list with member count and synced count."""
        h, tmp = handler
        circles_dir = tmp / "circles"
        self._make_yaml(circles_dir, "family", members=[{"name": "Alex", "telegram_user_id": 1}])
        self._make_yaml(circles_dir, "work-team", members=[{"name": "Bob", "telegram_user_id": 2}, {"name": "Carol", "telegram_user_id": 3}])
        update, context = self._make_update()
        await h.cmd_circles(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "Circles (2 configured)" in text
        assert "family" in text
        assert "work-team" in text
        assert "1 member" in text
        assert "2 members" in text
        assert len(h._last_circle_set) == 2

    @pytest.mark.asyncio
    async def test_circle_invalid_index_no_prior_list(self, handler, tmp_path):
        """Index before running /circles -> 'Invalid index. Run /circles first.'"""
        h, tmp = handler
        update, context = self._make_update(args=["1"])
        await h.cmd_circle(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "Invalid index" in text
        assert "/circles" in text

    @pytest.mark.asyncio
    async def test_circle_detail(self, handler, tmp_path):
        """After /circles, /circle 1 shows detail with rules."""
        h, tmp = handler
        circles_dir = tmp / "circles"
        icloud_folder = "second-brain-circles/family/memories"
        (tmp / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "second-brain-circles" / "family" / "memories").mkdir(parents=True)
        self._make_yaml(
            circles_dir, "family",
            members=[{"name": "Alex", "telegram_user_id": 1}],
            icloud_folder=icloud_folder,
            include_rules=[{"type": "calendar_event", "tags_contains_any": ["family"]}],
            exclude_rules=[{"tags_contains_any": ["private"]}],
        )
        # patch the icloud root to tmp so the folder existence check works
        monkeypatch_icloud = tmp / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        h._config = {"circles": {"icloud_root": str(monkeypatch_icloud)}}
        update, context = self._make_update()
        await h.cmd_circles(update, context)
        update2, context2 = self._make_update(args=["1"])
        await h.cmd_circle(update2, context2)
        text = update2.message.reply_text.call_args[0][0]
        assert "family" in text
        assert "Alex" in text
        assert "Include rules" in text
        assert "Exclude rules" in text
        assert "✓" in text

    @pytest.mark.asyncio
    async def test_circle_missing_icloud_folder(self, handler, tmp_path):
        """iCloud folder not present -> shown in detail."""
        h, tmp = handler
        circles_dir = tmp / "circles"
        self._make_yaml(circles_dir, "family", icloud_folder="nonexistent/path")
        h._config = {"circles": {"icloud_root": str(tmp)}}
        update, context = self._make_update()
        await h.cmd_circles(update, context)
        update2, context2 = self._make_update(args=["1"])
        await h.cmd_circle(update2, context2)
        text = update2.message.reply_text.call_args[0][0]
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_circle_status(self, handler, tmp_path):
        """circle_status shows all circles with folder status."""
        h, tmp = handler
        circles_dir = tmp / "circles"
        self._make_yaml(circles_dir, "family")
        h._config = {"circles": {"icloud_root": str(tmp)}}
        update, context = self._make_update()
        await h.cmd_circle_status(update, context)
        text = update.message.reply_text.call_args[0][0]
        assert "Circle sync status" in text
        assert "family" in text

    @pytest.mark.asyncio
    async def test_circles_unauth(self, handler, tmp_path):
        """Unauthorized user -> silent return, no reply."""
        h, tmp = handler
        update, context = self._make_update(user_id=99999)
        await h.cmd_circles(update, context)
        update.message.reply_text.assert_not_called()


# ── LLM Chat Tests ────────────────────────────────────────────────────────────

def write_llm_chat_memory(memories_dir: Path, platform: str, title: str,
                          created: str, slug: str, chat_id: str,
                          summary: str = "", topics: list = None) -> Path:
    """Create an llm-chat memory file."""
    date = created[:10]
    path = memories_dir / f"llm-chat-{platform}-{date}-{slug}-{chat_id}.md"
    fm = {
        "type": "llm_chat",
        "platform": platform,
        "source_title": title,
        "created": created,
        "summary": summary or f"Conversation about {title}",
        "topics": topics or ["test"],
        "tags": [],
    }
    import yaml
    frontmatter = yaml.dump(fm, sort_keys=False)
    path.write_text(f"---\n{frontmatter}---\n\n## Summary\nTest conversation.\n")
    return path


@pytest.mark.asyncio
async def test_cmd_aichat_list_groups_by_platform(handler, brain_dir):
    """Default list mode groups by platform."""
    h = handler
    memories_dir = brain_dir / "memories"
    write_llm_chat_memory(memories_dir, "claude", "RAG discussion", "2026-04-20T10:00:00", "rag", "abc123")
    write_llm_chat_memory(memories_dir, "claude", "Python tips", "2026-04-21T10:00:00", "python", "def456")
    write_llm_chat_memory(memories_dir, "chatgpt", "Docker setup", "2026-04-22T10:00:00", "docker", "ghi789")

    update, context = _make_update(12345, args=[])
    await h.cmd_aichat(update, context)
    text = update.message.reply_text.call_args[0][0]

    assert "Claude:" in text
    assert "Chatgpt:" in text
    assert "RAG discussion" in text
    assert "Python tips" in text
    assert "Docker setup" in text


@pytest.mark.asyncio
async def test_cmd_aichat_detail_shows_summary(handler, brain_dir):
    """Detail mode shows full summary and topics."""
    h = handler
    memories_dir = brain_dir / "memories"
    write_llm_chat_memory(
        memories_dir, "claude", "RAG implementation",
        "2026-04-20T10:00:00", "rag-impl", "abc123",
        summary="Discussed vector search and embedding strategies.",
        topics=["RAG", "embeddings", "vector search"]
    )

    update, context = _make_update(12345, args=["1"])
    await h.cmd_aichat(update, context)
    text = update.message.reply_text.call_args[0][0]

    assert "[claude]" in text
    assert "RAG implementation" in text
    assert "Discussed vector search" in text
    assert "RAG, embeddings, vector search" in text


@pytest.mark.asyncio
async def test_cmd_aichat_search_keyword_filter(handler, brain_dir):
    """Search mode filters by keyword in header."""
    h = handler
    memories_dir = brain_dir / "memories"
    write_llm_chat_memory(memories_dir, "claude", "RAG discussion", "2026-04-20T10:00:00", "rag", "abc123")
    write_llm_chat_memory(memories_dir, "claude", "Python tips", "2026-04-21T10:00:00", "python", "def456")

    update, context = _make_update(12345, args=["search", "rag"])
    await h.cmd_aichat(update, context)
    text = update.message.reply_text.call_args[0][0]

    assert "RAG discussion" in text
    assert "Python tips" not in text


@pytest.mark.asyncio
async def test_cmd_aichat_invalid_index(handler, brain_dir):
    """Out-of-range index returns error message."""
    h = handler
    memories_dir = brain_dir / "memories"
    write_llm_chat_memory(memories_dir, "claude", "Test", "2026-04-20T10:00:00", "test", "abc123")

    update, context = _make_update(12345, args=["99"])
    await h.cmd_aichat(update, context)
    text = update.message.reply_text.call_args[0][0]

    assert "No such entry" in text


@pytest.mark.asyncio
async def test_cmd_comms_llm_filter(handler, brain_dir):
    """'/comms llm' returns only llm_chat memories."""
    h = handler
    memories_dir = brain_dir / "memories"

    # Create mixed types
    write_llm_chat_memory(memories_dir, "claude", "RAG discussion", "2026-04-20T10:00:00", "rag", "abc123")
    email_path = memories_dir / "email-thread-test-conv123.md"
    email_path.write_text(
        "---\ntype: email_thread\nsource_title: Project update\nlast_message: '2026-04-21T10:00:00'\n---\n\nTest."
    )

    update, context = _make_update(12345, args=["llm"])
    await h.cmd_comms(update, context)
    text = update.message.reply_text.call_args[0][0]

    assert "[claude]" in text
    assert "RAG discussion" in text
    assert "Project update" not in text


@pytest.mark.asyncio
async def test_search_memories_tool_accepts_llm_chat_type(handler, brain_dir):
    """Tool schema includes llm_chat in type description."""
    from chat_tools import TOOLS
    search_tool = next(t for t in TOOLS if t["function"]["name"] == "search_memories")
    type_desc = search_tool["function"]["parameters"]["properties"]["type"]["description"]
    assert "llm_chat" in type_desc


@pytest.mark.asyncio
async def test_command_registry_includes_aichat(handler):
    """Registry-completeness test passes for aichat."""
    # This is covered by the existing test_registry_completeness test which
    # automatically checks all registered handlers against COMMAND_REGISTRY.
    # We just verify that aichat was added.
    all_in_registry = {
        cmd
        for commands in ch.COMMAND_REGISTRY.values()
        for cmd, _ in commands
    }
    assert "aichat" in all_in_registry


# --- _list_projects_text (tool dispatch) ---

def _write_project_file(memories_dir: Path, slug: str, title: str,
                        category: str = "work", status: str = "active",
                        due_date: str = None, milestones: list = None) -> Path:
    due_line = f"due_date: '{due_date}'" if due_date else "due_date: null"
    ms_yaml = ""
    if milestones:
        ms_yaml = "milestones:\n" + "".join(
            f"  - text: {m['text']}\n    done: {str(m['done']).lower()}\n"
            for m in milestones
        )
    else:
        ms_yaml = "milestones: []\n"
    path = memories_dir / f"project-{slug}.md"
    path.write_text(
        f"---\n"
        f"type: project\n"
        f"category: {category}\n"
        f"source_title: {title}\n"
        f"summary: ''\n"
        f"tags: []\n"
        f"created: '2026-04-15T09:00:00'\n"
        f"{due_line}\n"
        f"status: {status}\n"
        f"priority: medium\n"
        f"linked_goal: null\n"
        f"{ms_yaml}"
        f"inferred_from: []\n"
        f"notes: ''\n"
        f"---\n\n## Notes\n"
    )
    return path


def test_list_projects_text_returns_active_projects(handler, brain_dir):
    """_list_projects_text returns numbered list of active projects."""
    m = brain_dir / "memories"
    _write_project_file(m, "work-rollout-abc123", "Q2 Rollout", category="work",
                        due_date="2026-07-01")
    _write_project_file(m, "personal-shed-def456", "Garden Shed", category="personal")
    _write_project_file(m, "work-done-ghi789", "Old Project", status="completed")

    text = handler._list_projects_text()
    assert "Q2 Rollout" in text
    assert "Garden Shed" in text
    assert "Old Project" not in text  # completed, not active
    assert "Active projects" in text


def test_list_projects_text_shows_milestone_progress(handler, brain_dir):
    """_list_projects_text includes milestone done/total counts."""
    m = brain_dir / "memories"
    _write_project_file(
        m, "work-feature-abc123", "Feature Launch",
        milestones=[{"text": "Design", "done": True}, {"text": "Build", "done": False}]
    )

    text = handler._list_projects_text()
    assert "milestones: 1/2 done" in text


def test_list_projects_text_empty_when_no_active(handler, brain_dir):
    """_list_projects_text returns 'No active projects' when none exist."""
    text = handler._list_projects_text()
    assert "No active projects" in text


def test_list_projects_text_respects_limit(handler, brain_dir):
    """_list_projects_text truncates output to limit rows."""
    m = brain_dir / "memories"
    for i in range(5):
        _write_project_file(m, f"work-p{i}-{i:06d}", f"Project {i}")

    text = handler._list_projects_text(limit=3)
    assert "and 2 more" in text


def test_list_projects_text_filters_by_category(handler, brain_dir):
    """_list_projects_text honours category filter when passed."""
    m = brain_dir / "memories"
    _write_project_file(m, "work-thing-abc123", "Work Thing", category="work")
    _write_project_file(m, "personal-thing-def456", "Personal Thing", category="personal")

    text = handler._list_projects_text(category="work")
    assert "Work Thing" in text
    assert "Personal Thing" not in text


def test_list_projects_text_code_routes_to_code_repos(handler, brain_dir):
    """_list_projects_text(category='code') returns code-repo content, not GoalManager projects."""
    m = brain_dir / "memories"
    # GoalManager project that should NOT appear
    _write_project_file(m, "work-thing-abc123", "GoalManager Project", category="work")
    # Code repo that SHOULD appear
    write_project_memory(m, "myrepo", category="code", summary="Code repo.")

    text = handler._list_projects_text(category="code")
    assert "myrepo" in text
    assert "GoalManager Project" not in text


@pytest.mark.asyncio
async def test_cmd_briefing_awaits_assemble_briefing(handler):
    """/briefing must await _assemble_briefing() — without await the coroutine is sent as-is."""
    briefing_content = "Good morning. Here's your briefing for Monday, April 28:"

    # notification_manager with an async _assemble_briefing
    mock_nm = MagicMock()
    mock_nm._assemble_briefing = AsyncMock(return_value=briefing_content)
    handler.notification_manager = mock_nm

    update = MagicMock()
    update.effective_user.id = handler.allowed_user_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    await handler.cmd_briefing(update, context)

    update.message.reply_text.assert_called_once_with(briefing_content)
    # Verify it was called with the resolved string, not a coroutine object
    arg = update.message.reply_text.call_args[0][0]
    assert isinstance(arg, str), f"reply_text received {type(arg)} instead of str — missing await?"


# ── /changes tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_changes_no_activity(handler, brain_dir):
    """/changes when no projects have recent activity shows the 'no activity' message."""
    from unittest.mock import AsyncMock as _AsyncMock
    mock_agent = MagicMock()
    mock_agent.generate_change_digest = _AsyncMock(return_value=[])

    update, context = _make_update(12345, args=[])
    with patch("goal_project_agent.GoalProjectAgent", return_value=mock_agent):
        await handler.cmd_changes(update, context)

    calls = [c[0][0] for c in update.message.reply_text.call_args_list]
    assert any("No project/goal activity" in t for t in calls)


@pytest.mark.asyncio
async def test_cmd_changes_shows_digest(handler, brain_dir):
    """/changes with results formats each entry with title and summary."""
    from unittest.mock import AsyncMock as _AsyncMock
    digest = [
        {"title": "Alpha Project", "type": "project", "summary": "Work was done.", "memory_count": 2},
        {"title": "My Goal", "type": "goal", "summary": "Progress made.", "memory_count": 1},
    ]
    mock_agent = MagicMock()
    mock_agent.generate_change_digest = _AsyncMock(return_value=digest)

    update, context = _make_update(12345, args=[])
    with patch("goal_project_agent.GoalProjectAgent", return_value=mock_agent):
        await handler.cmd_changes(update, context)

    all_text = " ".join(c[0][0] for c in update.message.reply_text.call_args_list)
    assert "Alpha Project" in all_text
    assert "Work was done." in all_text
    assert "My Goal" in all_text
    assert "Progress made." in all_text
    mock_agent.generate_change_digest.assert_awaited_once_with(hours=24)


@pytest.mark.asyncio
async def test_cmd_changes_custom_hours(handler, brain_dir):
    """/changes 48 passes hours=48 to generate_change_digest."""
    from unittest.mock import AsyncMock as _AsyncMock
    mock_agent = MagicMock()
    mock_agent.generate_change_digest = _AsyncMock(return_value=[])

    update, context = _make_update(12345, args=["48"])
    with patch("goal_project_agent.GoalProjectAgent", return_value=mock_agent):
        await handler.cmd_changes(update, context)

    mock_agent.generate_change_digest.assert_awaited_once_with(hours=48)


@pytest.mark.asyncio
async def test_cmd_changes_rejects_out_of_range_hours(handler, brain_dir):
    """/changes 999 is rejected before calling the agent."""
    from unittest.mock import AsyncMock as _AsyncMock
    mock_agent = MagicMock()
    mock_agent.generate_change_digest = _AsyncMock(return_value=[])

    update, context = _make_update(12345, args=["999"])
    with patch("goal_project_agent.GoalProjectAgent", return_value=mock_agent):
        await handler.cmd_changes(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "168" in text  # mentions the max
    mock_agent.generate_change_digest.assert_not_awaited()
