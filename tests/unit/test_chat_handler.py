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
         patch("chat_handler.SkillExecutor"):
        h = ch.TelegramChatHandler()
        h.allowed_user_id = 12345
        yield h


def write_memory(memories_dir: Path, slug: str, tags: list, title: str,
                 body: str = "content", source_url: str = "") -> Path:
    path = memories_dir / f"2026-04-11-{slug}.md"
    url_line = f"source_url: {source_url}\n" if source_url else ""
    path.write_text(f"---\n{url_line}tags: {tags}\nsource_title: {title}\n---\n\n## Summary\n{body}")
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


# ── /purge command ────────────────────────────────────────────────────────────

async def test_cmd_purge_deletes_and_reports_count(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "ex-aaa111", [], "Ex", source_url="https://example.com/x")
    update, ctx = _make_update(12345, ["example.com"])
    await handler.cmd_purge(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Deleted 1" in reply
    assert not (m / "2026-04-11-ex-aaa111.md").exists()


async def test_cmd_purge_no_matches(handler, brain_dir):
    update, ctx = _make_update(12345, ["nowhere.com"])
    await handler.cmd_purge(update, ctx)
    assert "No memories found" in update.message.reply_text.call_args[0][0]


async def test_cmd_purge_no_args(handler, brain_dir):
    update, ctx = _make_update(12345, [])
    await handler.cmd_purge(update, ctx)
    assert "Usage" in update.message.reply_text.call_args[0][0]


async def test_cmd_purge_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999, ["example.com"])
    await handler.cmd_purge(update, ctx)
    update.message.reply_text.assert_not_called()


# ── /purgeall command ─────────────────────────────────────────────────────────

async def test_cmd_purgeall_reports_per_domain(handler, brain_dir):
    m = brain_dir / "memories"
    write_memory(m, "g-aaa111", [], "G", source_url="https://google.com/search")
    write_memory(m, "f-bbb222", [], "F", source_url="https://facebook.com/feed")
    update, ctx = _make_update(12345)
    await handler.cmd_purgeall(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "google.com" in reply
    assert "facebook.com" in reply
    assert "1 deleted" in reply
    assert len(list(m.glob("*.md"))) == 0


async def test_cmd_purgeall_empty_skip_list(handler, brain_dir):
    config = yaml.safe_load((brain_dir / "config.yaml").read_text())
    config["browser_watcher"]["skip_domains"] = []
    (brain_dir / "config.yaml").write_text(yaml.dump(config))
    update, ctx = _make_update(12345)
    await handler.cmd_purgeall(update, ctx)
    assert "empty" in update.message.reply_text.call_args[0][0]


async def test_cmd_purgeall_rejects_unauthorised(handler, brain_dir):
    update, ctx = _make_update(99999)
    await handler.cmd_purgeall(update, ctx)
    update.message.reply_text.assert_not_called()
