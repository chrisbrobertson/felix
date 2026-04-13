"""Unit tests for chat_handler.py."""
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
def handler(brain_dir):
    mock_app = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    with patch.object(ch, "BRAIN_DIR", brain_dir), \
         patch("chat_handler.ApplicationBuilder", return_value=mock_builder), \
         patch("chat_handler.SkillExecutor"), \
         patch.dict(os.environ, {"GITHUB_PAT": "", "GITHUB_REPO": ""}, clear=False):
        h = ch.TelegramChatHandler()
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

def test_context_prepends_index_when_present(handler, brain_dir):
    (brain_dir / "index.md").write_text("Weekly index content.")
    ctx = handler._load_context("anything")
    # Command list is always first; index follows
    assert "Available Telegram Commands" in ctx
    assert "Weekly index content." in ctx
    # Index appears after the command block
    assert ctx.index("Memory Index") > ctx.index("Available Telegram Commands")


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
    # Command list is always injected even when no memories or index exist
    assert "Available Telegram Commands" in ctx


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

def test_purge_domain_deletes_matching_memories(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "ex-aaa111", [], "Example Page", source_url="https://example.com/page")
    write_memory(m, "other-bbb222", [], "Other Page", source_url="https://other.com/page")
    count = handler._purge_domain("example.com")
    assert count == 1
    assert not (m / "2026-04-11-ex-aaa111.md").exists()
    assert (m / "2026-04-11-other-bbb222.md").exists()


def test_purge_domain_no_matches(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "other-bbb222", [], "Other Page", source_url="https://other.com/page")
    count = handler._purge_domain("nowhere.com")
    assert count == 0
    assert len(list(m.glob("*.md"))) == 1


def test_purge_domain_skips_files_without_source_url(handler, brain_dir):
    m = brain_dir / "memories"
    p = m / "2026-04-11-no-url-aaa111.md"
    p.write_text("---\ntags: []\nsource_title: No URL File\n---\n\n## Summary\ncontent")
    count = handler._purge_domain("example.com")
    assert count == 0
    assert p.exists()


def test_purge_domain_skips_files_without_frontmatter(handler, brain_dir):
    m = brain_dir / "memories"
    p = m / "2026-04-11-no-fm-aaa111.md"
    p.write_text("## Summary\nJust plain markdown, no frontmatter.")
    count = handler._purge_domain("example.com")
    assert count == 0
    assert p.exists()


def test_purge_domain_deletes_multiple_matches(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "ex1-aaa111", [], "Ex 1", source_url="https://example.com/a")
    write_memory(m, "ex2-bbb222", [], "Ex 2", source_url="https://example.com/b")
    write_memory(m, "other-ccc333", [], "Other", source_url="https://other.com/c")
    count = handler._purge_domain("example.com")
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
    assert "Invalid index" in update.message.reply_text.call_args[0][0]


async def test_cmd_reading_no_results(handler, brain_dir):
    handler._active_list = []
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_reading(update, ctx)
    assert "Invalid index" in update.message.reply_text.call_args[0][0]


async def test_cmd_reading_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999, ["1"])
    await handler.cmd_reading(update, ctx)
    update.message.reply_text.assert_not_called()


# ── /help command ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_help_renders_all_groups(handler):
    update, ctx = _make_update(12345)
    await handler.cmd_help(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    for group in ch.COMMAND_REGISTRY:
        assert group in reply


@pytest.mark.asyncio
async def test_cmd_help_all_registry_commands_listed(handler):
    update, ctx = _make_update(12345)
    await handler.cmd_help(update, ctx)
    # Collect all chunks across multiple calls
    calls = update.message.reply_text.call_args_list
    full_text = "\n".join(c[0][0] for c in calls)
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
async def test_cmd_projects_lists_all(handler, brain_dir):
    m = brain_dir / "memories"
    write_project_memory(m, "alpha")
    write_project_memory(m, "beta")
    update, ctx = _make_update(12345)
    await handler.cmd_projects(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "alpha" in reply
    assert "beta" in reply
    assert len(handler._last_project_set) == 2


@pytest.mark.asyncio
async def test_cmd_projects_filter_by_category(handler, brain_dir):
    m = brain_dir / "memories"
    write_project_memory(m, "codeproj", category="code")
    write_project_memory(m, "workproj", category="work")
    update, ctx = _make_update(12345, ["code"])
    await handler.cmd_projects(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "codeproj" in reply
    assert "workproj" not in reply


@pytest.mark.asyncio
async def test_cmd_projects_default_n_10(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(15):
        write_project_memory(m, f"proj{i:02d}")
    update, ctx = _make_update(12345)
    await handler.cmd_projects(update, ctx)
    assert len(handler._last_project_set) == 10


@pytest.mark.asyncio
async def test_cmd_projects_n_clamped(handler, brain_dir):
    m = brain_dir / "memories"
    for i in range(5):
        write_project_memory(m, f"proj{i}")
    # N=999 clamped to 50 — but only 5 exist so we get 5
    update, ctx = _make_update(12345, ["999"])
    await handler.cmd_projects(update, ctx)
    assert len(handler._last_project_set) == 5
    # N=0 clamped to 1
    update2, ctx2 = _make_update(12345, ["0"])
    await handler.cmd_projects(update2, ctx2)
    assert len(handler._last_project_set) == 1


@pytest.mark.asyncio
async def test_cmd_project_detail_view(handler, brain_dir):
    m = brain_dir / "memories"
    write_project_memory(m, "myrepo", summary="My test repo.")
    handler._last_project_set = [m / "project-myrepo.md"]
    update, ctx = _make_update(12345, ["1"])
    await handler.cmd_project(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "myrepo" in reply
    assert "My test repo" in reply


@pytest.mark.asyncio
async def test_cmd_project_invalid_index(handler):
    handler._last_project_set = []
    update, ctx = _make_update(12345, ["99"])
    await handler.cmd_project(update, ctx)
    assert "Invalid index" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_projects_groups_by_base_name(handler, brain_dir):
    """Projects with same base name from different hosts are grouped."""
    m = brain_dir / "memories"
    write_project_memory(m, "myrepo", hostname="studio", summary="Studio version")
    write_project_memory(m, "myrepo", hostname="laptop", summary="Laptop version")
    update, ctx = _make_update(12345)
    await handler.cmd_projects(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    # Should show "myrepo" once (not twice)
    assert reply.count("myrepo") == 1
    # Should mention both hostnames
    assert "laptop" in reply
    assert "studio" in reply


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
    assert "Invalid index" in update.message.reply_text.call_args[0][0]


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
    assert "Invalid index" in update.message.reply_text.call_args[0][0]


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
    assert "Invalid index" in update.message.reply_text.call_args[0][0]


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


# ── project_scanner migration ─────────────────────────────────────────────────

def test_migrate_legacy_code_project(tmp_path):
    import project_scanner as ps_mod
    from project_scanner import ProjectScanner

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Write a legacy file (both type migration and filename migration will occur)
    legacy = memories_dir / "project-legacy.md"
    legacy.write_text(
        "---\nsource_title: legacy\nsummary: old\ntags: [python]\n"
        "last_scanned: '2026-04-11T10:00:00'\n"
        "source_url: git@github.com:org/legacy.git\ntype: code_project\n"
        "local_path: /tmp/legacy\ndefault_branch: main\n"
        "languages: [python]\nhead_sha: abc123\n---\n\n## Content\n"
    )

    with patch.object(ps_mod, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
        _ = ProjectScanner()

    import yaml as _yaml
    # File will be renamed to project-testhost-legacy.md
    migrated = memories_dir / "project-testhost-legacy.md"
    assert migrated.exists()
    assert not legacy.exists()
    text = migrated.read_text()
    parts = text.split("---", 2)
    fm = _yaml.safe_load(parts[1])
    assert fm["type"] == "project"
    assert fm["category"] == "code"


def test_migrate_idempotent(tmp_path):
    import project_scanner as ps_mod
    from project_scanner import ProjectScanner

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Write an already-migrated file (with hostname)
    migrated = memories_dir / "project-testhost-new.md"
    migrated.write_text(
        "---\nsource_title: new\nsummary: new project\ntags: [python]\n"
        "last_scanned: '2026-04-11T10:00:00'\n"
        "source_url: git@github.com:org/new.git\ntype: project\ncategory: code\n"
        "hostname: testhost\nlocal_path: /tmp/new\ndefault_branch: main\n"
        "languages: [python]\nhead_sha: def456\n---\n\n## Content\n"
    )

    with patch.object(ps_mod, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
        _ = ProjectScanner()

    # File should remain unchanged
    assert migrated.exists()
    import yaml as _yaml
    fm = _yaml.safe_load(migrated.read_text().split("---", 2)[1])
    assert fm["type"] == "project"
    assert fm["category"] == "code"


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
    handler.scanners = {"projects": mock_scanner}
    update, ctx = _make_update(12345, ["projects"])
    with patch("chat_handler.socket.gethostname", return_value="local-host"):
        await handler.cmd_backfill(update, ctx)
    replies = [call[0][0] for call in update.message.reply_text.call_args_list]
    final_reply = replies[-1]
    assert "15 processed" in final_reply
    assert "3 skipped" in final_reply
    assert "1 errors" in final_reply
    assert "Done!" in final_reply
