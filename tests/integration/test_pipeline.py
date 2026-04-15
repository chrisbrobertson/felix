"""
Integration tests: URL entry → SkillExecutor → MemoryWriter → file on disk.

These tests use real file I/O against tmp directories and mock only the
LLM API call (acompletion) and HTTP fetches.
"""
import json
import os
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

    brain_dir = tmp_path / "brain"
    logs_dir = brain_dir / "logs"
    logs_dir.mkdir(parents=True)

    original_skill = (infra["skills"] / "summarize-webpage.md").read_text()

    with patch.object(se, "SKILLS_DIR", infra["skills"]), \
         patch.object(se, "BRAIN_DIR", brain_dir), \
         patch("skill_executor.acompletion", new=AsyncMock(return_value=mock_resp)):
        executor = se.SkillExecutor("summarize-webpage", role="watcher")
        await executor.run({"url": "u", "title": "t", "content": "c"})

    assert (infra["skills"] / "summarize-webpage.md").read_text() == original_skill
    # Check log file created in logs dir
    log_files = list(logs_dir.glob("*-execution-log.jsonl"))
    assert len(log_files) == 1


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
    """/forget <domain> deletes memories matching the domain and leaves others intact."""
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

    await chat_handler_instance.cmd_forget(mock_update, mock_ctx)

    assert not target.exists()
    assert keeper.exists()
    reply = mock_update.message.reply_text.call_args[0][0]
    assert "Forgotten 1" in reply


async def test_scanner_writes_memory_for_git_repo(tmp_path):
    """CodeScanner scan → code-{name}.md written with correct frontmatter."""
    import subprocess
    import code_scanner as cs
    from code_scanner import CodeScanner

    # Set up a minimal git repo with one commit
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test.com"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True)
    (repo / "README.md").write_text("# My Repo\n\nA test project for the scanner.\n")
    (repo / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial commit"],
                   capture_output=True)

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_content = {
        "code_scanner": {
            "interval_seconds": 300,
            "repo_dirs": [str(tmp_path)],
            "skip_repos": [],
        }
    }

    scanner = CodeScanner(role="full")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("code_scanner._hostname", return_value="testhost"), \
         patch("code_scanner.acompletion", new=AsyncMock(
             return_value=MagicMock(
                 choices=[MagicMock(message=MagicMock(
                     content="SUMMARY: A test project.\nTAGS: python, testing"
                 ))]
             )
         ), create=True):

        import yaml as _yaml
        (tmp_path / "config.yaml").write_text(_yaml.dump(config_content))

        await scanner._run_scan()

    # File is now hostname-scoped and uses code- prefix
    mem = memories_dir / "code-testhost-myrepo.md"
    assert mem.exists(), "Memory file was not created"

    import yaml as _yaml
    text = mem.read_text()
    parts = text.split("---", 2)
    fm = _yaml.safe_load(parts[1])

    assert fm["source_title"] == "myrepo"
    assert fm["type"] == "code"
    assert "category" not in fm
    assert "python" in fm["languages"]
    assert fm["head_sha"] != ""
    assert "## Recent Activity" in text


async def test_scanner_skips_write_when_no_changes(tmp_path):
    """Second scan with same HEAD sha must not modify the memory file."""
    import subprocess
    import code_scanner as cs
    from code_scanner import CodeScanner

    repo = tmp_path / "stable"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], capture_output=True)
    (repo / "main.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_content = {
        "code_scanner": {
            "interval_seconds": 300,
            "repo_dirs": [str(tmp_path)],
            "skip_repos": [],
        }
    }

    scanner = CodeScanner(role="full")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("code_scanner._hostname", return_value="testhost"), \
         patch("code_scanner.acompletion", new=AsyncMock(
             return_value=MagicMock(
                 choices=[MagicMock(message=MagicMock(
                     content="SUMMARY: Stable repo.\nTAGS: python"
                 ))]
             )
         ), create=True):

        import yaml as _yaml
        (tmp_path / "config.yaml").write_text(_yaml.dump(config_content))

        # First scan — writes the file
        await scanner._run_scan()
        mem = memories_dir / "code-testhost-stable.md"
        assert mem.exists()
        first_mtime = mem.stat().st_mtime

        # Second scan — nothing changed, file must not be rewritten
        await scanner._run_scan()
        second_mtime = mem.stat().st_mtime

    assert first_mtime == second_mtime, "Memory file was rewritten despite no git changes"


async def test_email_scanner_writes_memory_for_thread(tmp_path):
    """EmailScanner scan → email-thread-*.md written with correct frontmatter."""
    import sqlite3 as _sqlite3
    import email_scanner as es
    from email_scanner import EmailScanner, EnvelopeIndexSource, CORE_DATA_EPOCH_OFFSET
    from datetime import datetime as _dt

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Build a minimal Envelope Index SQLite database
    db_path = tmp_path / "Envelope Index"
    conn = _sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            conversation_id INTEGER,
            subject INTEGER,
            date_received REAL,
            date_sent REAL,
            snippet TEXT,
            read INTEGER DEFAULT 0,
            flagged INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0,
            sender INTEGER,
            mailbox INTEGER
        );
    """)
    # Insert two threads: 2 messages in thread 1001, 1 message in thread 1002
    def ts(dt):
        return (_dt(*dt) - _dt(1970, 1, 1)).total_seconds() - CORE_DATA_EPOCH_OFFSET

    conn.execute("INSERT INTO subjects VALUES (1, 'API Migration Timeline')")
    conn.execute("INSERT INTO subjects VALUES (2, 'Q3 Budget')")
    conn.execute("INSERT INTO addresses VALUES (1, 'alice@acme.com', 'Alice')")
    conn.execute("INSERT INTO addresses VALUES (2, 'bob@acme.com', 'Bob')")
    conn.execute("INSERT INTO mailboxes VALUES (1, 'mailbox://user@host/INBOX')")
    conn.execute("INSERT INTO messages VALUES (101, 1001, 1, ?, ?, 'Starting migration planning', 0, 0, 0, 1, 1)",
                 (ts((2026, 4, 5, 8, 0, 0)),) * 2)
    conn.execute("INSERT INTO messages VALUES (102, 1001, 1, ?, ?, 'May 15 cutover confirmed', 1, 0, 0, 2, 1)",
                 (ts((2026, 4, 10, 9, 0, 0)),) * 2)
    conn.execute("INSERT INTO messages VALUES (103, 1002, 2, ?, ?, 'Budget numbers attached', 0, 0, 0, 1, 1)",
                 (ts((2026, 4, 8, 10, 0, 0)),) * 2)
    conn.commit()
    conn.close()

    config_content = {
        "email_scanner": {
            "interval_seconds": 300,
            "initial_lookback_days": 30,
            "archive_after_days": 90,
            "skip_mailboxes": ["Trash", "Junk"],
            "full_rescan": False,
        }
    }

    scanner = EmailScanner(role="full")

    with patch.object(es, "MEMORIES_DIR", memories_dir), \
         patch.object(es, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(EnvelopeIndexSource, "_find_db_path", return_value=db_path), \
         patch.object(EnvelopeIndexSource, "_copy_db", return_value=db_path), \
         patch("email_scanner.acompletion", new=AsyncMock(
             return_value=MagicMock(
                 choices=[MagicMock(message=MagicMock(
                     content="SUMMARY: Thread about API migration.\nTAGS: acme, api-migration"
                 ))]
             )
         ), create=True):

        import yaml as _yaml
        (tmp_path / "config.yaml").write_text(_yaml.dump(config_content))

        await scanner._run_scan()

    mem_files = list(memories_dir.glob("email-thread-*.md"))
    assert len(mem_files) == 2, f"Expected 2 memory files, got {[f.name for f in mem_files]}"

    import yaml as _yaml
    for mem in mem_files:
        text = mem.read_text()
        parts = text.split("---", 2)
        fm = _yaml.safe_load(parts[1])
        assert fm["type"] == "email_thread"
        assert fm["conversation_id"] in (1001, 1002)
        assert fm["message_count"] > 0
        assert "## Messages" in text


async def test_email_scanner_skips_write_when_no_new_messages(tmp_path):
    """Second scan with same data must not modify memory files."""
    import sqlite3 as _sqlite3
    import email_scanner as es
    from email_scanner import EmailScanner, EnvelopeIndexSource, CORE_DATA_EPOCH_OFFSET
    from datetime import datetime as _dt

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    db_path = tmp_path / "Envelope Index"
    conn = _sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY, conversation_id INTEGER,
            subject INTEGER, date_received REAL, date_sent REAL,
            snippet TEXT, read INTEGER DEFAULT 0, flagged INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0, sender INTEGER, mailbox INTEGER
        );
    """)

    def ts(dt):
        return (_dt(*dt) - _dt(1970, 1, 1)).total_seconds() - CORE_DATA_EPOCH_OFFSET

    conn.execute("INSERT INTO subjects VALUES (1, 'Status Update')")
    conn.execute("INSERT INTO addresses VALUES (1, 'a@b.com', 'A')")
    conn.execute("INSERT INTO mailboxes VALUES (1, 'mailbox://u@h/INBOX')")
    conn.execute("INSERT INTO messages VALUES (10, 9001, 1, ?, ?, 'Hello', 0, 0, 0, 1, 1)",
                 (ts((2026, 4, 10, 9, 0, 0)),) * 2)
    conn.commit()
    conn.close()

    config_content = {
        "email_scanner": {
            "interval_seconds": 300, "initial_lookback_days": 30,
            "archive_after_days": 90, "skip_mailboxes": [], "full_rescan": False,
        }
    }

    scanner = EmailScanner(role="full")

    with patch.object(es, "MEMORIES_DIR", memories_dir), \
         patch.object(es, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(es, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(EnvelopeIndexSource, "_find_db_path", return_value=db_path), \
         patch.object(EnvelopeIndexSource, "_copy_db", return_value=db_path), \
         patch("email_scanner.acompletion", new=AsyncMock(
             return_value=MagicMock(
                 choices=[MagicMock(message=MagicMock(
                     content="SUMMARY: Status update.\nTAGS: status"
                 ))]
             )
         ), create=True):

        import yaml as _yaml
        (tmp_path / "config.yaml").write_text(_yaml.dump(config_content))

        # First scan — creates the file
        await scanner._run_scan()
        mem_files = list(memories_dir.glob("email-thread-*.md"))
        assert len(mem_files) == 1
        first_mtime = mem_files[0].stat().st_mtime

        # Second scan — same high_water_rowid, no new messages — must not rewrite
        await scanner._run_scan()
        second_mtime = mem_files[0].stat().st_mtime

    assert first_mtime == second_mtime, "Memory file was rewritten with no new messages"


async def test_purgeall_command_clears_all_skip_domain_memories(
    infra, chat_handler_instance
):
    """/forget <domain> can be used to remove memories from skip list domains."""
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
    mock_ctx.args = ["google.com"]

    await chat_handler_instance.cmd_forget(mock_update, mock_ctx)

    assert not g.exists()
    assert keeper.exists()
    reply = mock_update.message.reply_text.call_args[0][0]
    assert "Forgotten 1" in reply


async def test_memories_list_search_view_delete_flow(infra, chat_handler_instance):
    """End-to-end: /readings → /search → /reading → /forget."""
    m = infra["root"] / "memories"

    def mk(slug, title, tags, url, summary):
        p = m / f"2026-04-11-{slug}.md"
        p.write_text(
            f"---\nsource_title: {title}\nsource_url: {url}\n"
            f"summary: {summary}\ntags: {tags}\ncreated: '2026-04-11T10:00:00'\n---\n\n"
            f"## Summary\n{summary}\n"
        )
        return p

    p1 = mk("litellm-aaa", "LiteLLM Router", "['litellm']",
             "https://litellm.ai", "LiteLLM is an LLM router.")
    p2 = mk("react-bbb", "ReAct Prompting", "['react','prompting']",
             "https://promptingguide.ai/react", "ReAct combines reasoning and acting.")
    p3 = mk("cooking-ccc", "Cooking Tips", "['food']",
             "https://cooking.com", "Tips for cooking.")

    u = MagicMock()
    u.effective_user.id = 12345
    u.message = AsyncMock()

    # Step 1: /readings lists all 3
    ctx = MagicMock(); ctx.args = []
    await chat_handler_instance.cmd_readings(u, ctx)
    reply = u.message.reply_text.call_args[0][0]
    assert "LiteLLM" in reply or "ReAct" in reply or "Cooking" in reply
    assert len(chat_handler_instance._last_results) == 3
    assert len(chat_handler_instance._active_list) == 3

    # Step 2: /search finds litellm
    u.message.reset_mock()
    ctx = MagicMock(); ctx.args = ["litellm"]
    await chat_handler_instance.cmd_search(u, ctx)
    reply = u.message.reply_text.call_args[0][0]
    assert "LiteLLM" in reply
    assert len(chat_handler_instance._last_results) == 1
    assert len(chat_handler_instance._active_list) == 1

    # Step 3: /reading 1 shows details of the search result
    u.message.reset_mock()
    ctx = MagicMock(); ctx.args = ["1"]
    await chat_handler_instance.cmd_reading(u, ctx)
    reply = u.message.reply_text.call_args[0][0]
    assert "LiteLLM" in reply
    assert "litellm.ai" in reply

    # Step 4: /forget 1 removes the file
    u.message.reset_mock()
    ctx = MagicMock(); ctx.args = ["1"]
    await chat_handler_instance.cmd_forget(u, ctx)
    assert not p1.exists()
    assert "Forgotten" in u.message.reply_text.call_args[0][0]
    assert len(chat_handler_instance._active_list) == 0


# ── Zoom Scanner integration ───────────────────────────────────────────────────

async def test_zoom_scanner_writes_memory_for_meeting(tmp_path):
    """ZoomScanner._run_scan → meeting-*.md written with correct frontmatter."""
    import zoom_scanner as zs
    from zoom_scanner import ZoomScanner

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "zoom-state.json"

    VTT_CONTENT = (
        "WEBVTT\n\n"
        "1\n00:00:05.000 --> 00:00:10.500\n"
        "Sarah Chen: Good morning everyone, let's get started.\n\n"
        "2\n00:00:11.000 --> 00:00:18.750\n"
        "Mike Peters: Thanks Sarah. I wanted to address the budget concerns.\n\n"
        "3\n00:00:19.000 --> 00:00:25.000\n"
        "Sarah Chen: Can you commit to having the revised numbers by Friday?\n\n"
    )

    RECORDINGS_RESPONSE = {
        "meetings": [{
            "uuid": "test-uuid-abc123",
            "id": "12345678",
            "topic": "Q4 Planning Review",
            "start_time": "2026-04-11T10:00:00Z",
            "duration": 45,
            "recording_files": [{
                "file_type": "TRANSCRIPT",
                "status": "completed",
                "download_url": "https://zoom.us/rec/download/test",
            }],
        }],
        "next_page_token": None,
    }

    PARTICIPANTS_RESPONSE = {
        "participants": [
            {"name": "Sarah Chen", "user_email": "sarah.chen@acme.com"},
            {"name": "Mike Peters", "user_email": "mike.peters@acme.com"},
        ]
    }

    scanner = ZoomScanner(role="full")
    scanner._token = "test-token"
    scanner._token_expiry = 9999999999.0

    import yaml as _yaml

    config_content = {
        "zoom_scanner": {
            "interval_seconds": 300,
            "initial_lookback_days": 30,
        }
    }

    mock_llm_resp = MagicMock()
    mock_llm_resp.choices[0].message.content = (
        '{"summary": "Q4 planning with budget review.", '
        '"tags": ["q4", "budget"], "key_decisions": ["Friday deadline confirmed"]}'
    )

    async def fake_api_get(client, path, params=None, _retry=0):
        if "recordings" in path:
            return RECORDINGS_RESPONSE
        if "participants" in path:
            return PARTICIPANTS_RESPONSE
        return None

    async def fake_download(url):
        return VTT_CONTENT

    with patch.object(zs, "MEMORIES_DIR", memories_dir), \
         patch.object(zs, "STATE_FILE", state_file), \
         patch.object(zs, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(scanner, "_api_get", side_effect=fake_api_get), \
         patch.object(scanner, "_download_transcript", side_effect=fake_download), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_llm_resp)):

        (tmp_path / "config.yaml").write_text(_yaml.dump(config_content))
        await scanner._run_scan()

    mem_files = list(memories_dir.glob("meeting-*.md"))
    assert len(mem_files) == 1, f"Expected 1 meeting memory, got {[f.name for f in mem_files]}"

    text = mem_files[0].read_text()
    import yaml as _yaml2
    parts = text.split("---", 2)
    fm = _yaml2.safe_load(parts[1])

    assert fm["type"] == "meeting_transcript"
    assert fm["source_title"] == "Q4 Planning Review"
    assert fm["source_url"] == "zoom:test-uuid-abc123"
    assert "sarah.chen@acme.com" in fm["participants"]
    assert "## Transcript" in text
    assert "Sarah Chen" in text

    # UUID persisted to state file
    state = json.loads(state_file.read_text())
    assert "test-uuid-abc123" in state["processed_uuids"]


async def test_zoom_scanner_skips_processed_uuid(tmp_path):
    """ZoomScanner does not reprocess a meeting UUID already in state."""
    import zoom_scanner as zs
    from zoom_scanner import ZoomScanner

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "zoom-state.json"

    # Pre-populate state with the UUID we'll see in the API response
    state_file.write_text(json.dumps({
        "processed_uuids": ["already-seen-uuid"],
        "last_poll": "2026-04-11T10:00:00",
    }))

    RECORDINGS_RESPONSE = {
        "meetings": [{
            "uuid": "already-seen-uuid",
            "id": "99999",
            "topic": "Old Meeting",
            "start_time": "2026-04-10T10:00:00Z",
            "duration": 30,
            "recording_files": [{
                "file_type": "TRANSCRIPT",
                "status": "completed",
                "download_url": "https://zoom.us/rec/download/old",
            }],
        }],
        "next_page_token": None,
    }

    scanner = ZoomScanner(role="full")
    scanner._token = "test-token"
    scanner._token_expiry = 9999999999.0

    import yaml as _yaml

    async def fake_api_get(client, path, params=None, _retry=0):
        return RECORDINGS_RESPONSE

    with patch.object(zs, "MEMORIES_DIR", memories_dir), \
         patch.object(zs, "STATE_FILE", state_file), \
         patch.object(zs, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(scanner, "_api_get", side_effect=fake_api_get), \
         patch.object(scanner, "_download_transcript", new=AsyncMock()) as mock_dl:

        (tmp_path / "config.yaml").write_text(_yaml.dump({"zoom_scanner": {"interval_seconds": 300, "initial_lookback_days": 30}}))
        await scanner._run_scan()

    mock_dl.assert_not_called()
    assert list(memories_dir.glob("meeting-*.md")) == []


# ── Commitment Tracker integration ────────────────────────────────────────────

async def test_commitment_tracker_extracts_from_meeting_memory(tmp_path):
    """CommitmentTracker._run_scan → commitment-*.md written from meeting memory."""
    import commitment_tracker as ct
    from commitment_tracker import CommitmentTracker, _parse_frontmatter

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "ct-state.json"

    # Write a meeting transcript memory
    meeting_mem = memories_dir / "meeting-2026-04-11-q4-abc123.md"
    meeting_mem.write_text(
        "---\n"
        "source_title: Q4 Planning Review\n"
        "summary: Sarah committed to sending revised budget numbers by Friday.\n"
        "tags: [q4, budget]\nlast_scanned: '2026-04-11T10:00:00'\n"
        "source_url: zoom:test-uuid-abc123\ntype: meeting_transcript\n"
        "participants: [sarah.chen@acme.com]\nspeakers: [Sarah Chen]\n"
        "duration_minutes: 45\nmeeting_date: '2026-04-11T10:00:00'\n"
        "zoom_meeting_id: '12345678'\n---\n\n"
        "## Transcript\n"
        "- 00:00:19 Sarah Chen: Can you commit to having the revised numbers by Friday?\n"
        "- 00:00:25 Sarah Chen: Yes, I'll have the revised budget numbers to you by Friday.\n\n"
        "## Summary\nSarah committed to sending revised budget numbers by Friday.\n"
    )

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = json.dumps({
        "commitments": [{
            "type": "outbound",
            "description": "Send revised budget numbers",
            "owner": "Sarah Chen",
            "owner_email": "sarah.chen@acme.com",
            "recipient": "Chris",
            "due_date": "2026-04-18",
            "due_date_confidence": "explicit",
            "confidence": 0.9,
            "extracted_text": "Yes, I'll have the revised budget numbers to you by Friday.",
        }]
    })

    tracker = CommitmentTracker(role="full")
    import yaml as _yaml

    with patch.object(ct, "MEMORIES_DIR", memories_dir), \
         patch.object(ct, "STATE_FILE", state_file), \
         patch.object(ct, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):

        (tmp_path / "config.yaml").write_text(_yaml.dump({
            "commitment_tracker": {
                "interval_seconds": 300,
                "min_confidence": 0.5,
                "source_types": ["meeting_transcript"],
            }
        }))
        await tracker._run_scan()

    commitment_files = list(memories_dir.glob("commitment-*.md"))
    assert len(commitment_files) == 1

    import yaml as _yaml2
    text = commitment_files[0].read_text()
    parts = text.split("---", 2)
    fm = _yaml2.safe_load(parts[1])

    assert fm["type"] == "commitment"
    assert fm["commitment_type"] == "outbound"
    assert fm["owner"] == "Sarah Chen"
    assert fm["status"] == "active"
    assert fm["source_url"].startswith("commitment:")
    assert fm["source_memory"] == "zoom:test-uuid-abc123"
    assert "## Context" in text


async def test_commitment_commands_complete_and_dismiss_flow(infra, chat_handler_instance):
    """/commitments → /complete → /dismiss round trip via Telegram commands."""
    import yaml as _yaml
    import commitment_tracker as ct
    from commitment_tracker import _parse_frontmatter, _stable_commitment_id, _slugify

    m = infra["root"] / "memories"

    # Write two active commitment files
    def write_commitment(desc, commitment_type, due_date=None):
        source_url = "zoom:abc"
        stable_id = _stable_commitment_id(source_url, desc, "Alice")
        slug = _slugify(desc)
        p = m / f"commitment-{slug}-{stable_id}.md"
        fm = {
            "source_title": desc,
            "summary": f"Alice committed to {desc.lower()}",
            "tags": [],
            "last_scanned": "2026-04-11T10:00:00",
            "source_url": f"commitment:{stable_id}",
            "type": "commitment",
            "commitment_type": commitment_type,
            "owner": "Alice",
            "owner_email": "alice@acme.com",
            "recipient": "Chris",
            "due_date": due_date,
            "due_date_confidence": "explicit" if due_date else "none",
            "confidence": 0.9,
            "status": "active",
            "source_memory": source_url,
            "extracted_text": "I will do it.",
        }
        p.write_text(f"---\n{_yaml.dump(fm, sort_keys=False)}---\n\n## Context\nTest.\n")
        return p

    p_outbound = write_commitment("Send the report", "outbound", "2026-04-18")
    p_waiting = write_commitment("Waiting for vendor quote", "waiting_on")

    u = MagicMock()
    u.effective_user.id = 12345
    u.message = AsyncMock()

    # Step 1: /commitments lists both
    ctx = MagicMock()
    ctx.args = []
    await chat_handler_instance.cmd_commitments(u, ctx)
    reply = u.message.reply_text.call_args[0][0]
    assert "Send the report" in reply
    assert "Waiting for vendor quote" in reply
    assert len(chat_handler_instance._last_commitment_set) == 2

    # Find index of "Send the report" (sorted by due_date — it has one, so comes first)
    sorted_titles = []
    for f in chat_handler_instance._last_commitment_set:
        fm = _parse_frontmatter(f.read_text())
        sorted_titles.append(fm["source_title"])
    report_idx = sorted_titles.index("Send the report") + 1

    # Step 2: /complete N marks it completed
    u.message.reset_mock()
    ctx = MagicMock()
    ctx.args = [str(report_idx)]

    with patch.object(ct, "MEMORIES_DIR", m):
        await chat_handler_instance.cmd_complete(u, ctx)

    reply = u.message.reply_text.call_args[0][0]
    assert "Marked complete" in reply or "✓" in reply
    fm = _parse_frontmatter(p_outbound.read_text())
    assert fm["status"] == "completed"

    # Step 3: /commitments now shows only 1 (the waiting one)
    u.message.reset_mock()
    ctx = MagicMock()
    ctx.args = []
    await chat_handler_instance.cmd_commitments(u, ctx)
    assert len(chat_handler_instance._last_commitment_set) == 1

    # Step 4: /dismiss 1 marks the remaining one dismissed
    u.message.reset_mock()
    ctx = MagicMock()
    ctx.args = ["1"]
    with patch.object(ct, "MEMORIES_DIR", m):
        await chat_handler_instance.cmd_dismiss(u, ctx)

    reply = u.message.reply_text.call_args[0][0]
    assert "Dismissed" in reply or "✗" in reply
    fm = _parse_frontmatter(p_waiting.read_text())
    assert fm["status"] == "dismissed"


# Skill Optimizer Integration Tests

async def test_optimizer_scores_pending_rows(infra):
    """Seed skill with pending rows → optimizer updates scores."""
    import skill_optimizer as opt
    from unittest.mock import Mock

    skills_dir = infra["skills"]
    memories_dir = infra["memories"]

    # Create skill with pending execution rows
    skill_content = """---
name: test-skill
version: 1
preferred_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
---

## Instructions

Summarize the content.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
| 2026-04-14 | article-test-abc | gemini/gemini-2.0-flash | pending |  |
"""

    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create matching memory file
    mem_file = memories_dir / "2026-04-14-article-test-abc-hash123.md"
    mem_file.write_text("""---
source_url: https://example.com/article
source_title: Test Article
---

## Summary
This is a test article summary.

## Key Points
- Point one
- Point two
""")

    # Mock judge response
    mock_response = Mock()
    mock_response.choices = [Mock(message=Mock(content='{"score": 0.85, "reasoning": "Good quality summary"}'))]

    config = {
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

    with patch.object(opt, "BRAIN_DIR", infra["root"]), \
         patch.object(opt, "SKILLS_DIR", skills_dir), \
         patch.object(opt, "MEMORIES_DIR", memories_dir), \
         patch("skill_optimizer.acompletion", return_value=mock_response):

        import asyncio
        optimizer = opt.SkillOptimizer(config)
        stop_event = asyncio.Event()

        await optimizer._score_pending_rows(skill_path, stop_event)

    # Check that pending row was scored
    updated = skill_path.read_text()
    assert "| pending |" not in updated
    assert "| 0.85 |" in updated
    assert "Good quality" in updated

    # Check frontmatter updated
    fm = yaml.safe_load(updated.split("---")[1])
    assert fm["total_runs"] == 1
    assert fm["success_rate"] == 0.85


async def test_optimizer_rewrites_underperforming_skill(infra):
    """Seed with low scores → Instructions section changed."""
    import skill_optimizer as opt
    from unittest.mock import Mock

    skills_dir = infra["skills"]
    memories_dir = infra["memories"]

    # Create underperforming skill
    skill_content = """---
name: test-skill
version: 1
preferred_model: gemini/gemini-2.0-flash
success_rate: 0.55
total_runs: 15
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: false
---

## Instructions

Old instructions that need improvement.

## Evolution Log

### v1 (2026-04-10) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
| 2026-04-14 | article-1 | gemini | 0.50 | poor quality |
| 2026-04-14 | article-2 | gemini | 0.55 | mediocre |
| 2026-04-14 | article-3 | gemini | 0.60 | acceptable |
"""

    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create meta-skill
    meta_skill = skills_dir / "skill-optimizer.md"
    meta_skill.write_text("""---
name: skill-optimizer
version: 1
preferred_model: claude-sonnet-4-20250514
---

## Instructions

You rewrite skills based on critique. Output the complete skill file with updated Instructions.""")

    # Create memory files
    for i in range(1, 4):
        mem = memories_dir / f"2026-04-14-article-{i}-hash.md"
        mem.write_text(f"---\nsource_url: test\n---\n\n## Summary\nTest content {i}")

    # Mock critique response
    critique_response = Mock()
    critique_response.choices = [Mock(message=Mock(content="""{
        "failure_patterns": ["missing key points", "too verbose"],
        "root_cause": "Instructions lack specificity",
        "suggested_focus": "Add explicit format requirements"
    }"""))]

    # Mock rewrite response
    rewrite_response = Mock()
    rewrite_response.choices = [Mock(message=Mock(content="""---
name: test-skill
version: 1
preferred_model: gemini/gemini-2.0-flash
success_rate: 0.55
total_runs: 15
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: false
---

## Instructions

Improved instructions with explicit format requirements.
- Be concise
- Include all key points
- Follow structured format

## Evolution Log

### v2 (2026-04-15) — improve format specificity
**Critique:** Instructions lack specificity
**Failure patterns:** missing key points, too verbose
**Change:** Added explicit format requirements and structure guidelines
**Pre-optimization avg:** 0.55 | **Post (projected):** pending

### v1 (2026-04-10) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
| 2026-04-14 | article-1 | gemini | 0.50 | poor quality |
| 2026-04-14 | article-2 | gemini | 0.55 | mediocre |
| 2026-04-14 | article-3 | gemini | 0.60 | acceptable |
"""))]

    config = {
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

    with patch.object(opt, "BRAIN_DIR", infra["root"]), \
         patch.object(opt, "SKILLS_DIR", skills_dir), \
         patch.object(opt, "MEMORIES_DIR", memories_dir), \
         patch("skill_optimizer.acompletion", side_effect=[critique_response, rewrite_response]):

        import asyncio
        optimizer = opt.SkillOptimizer(config)
        stop_event = asyncio.Event()

        await optimizer._optimize_skill(skill_path, stop_event)

    # Check Instructions changed
    updated = skill_path.read_text()
    assert "Improved instructions with explicit format requirements" in updated
    assert "Old instructions that need improvement" not in updated

    # Check version incremented
    fm = yaml.safe_load(updated.split("---")[1])
    assert fm["version"] == 2
    assert fm["prev_version_avg_score"] == 0.55
    assert fm["last_optimized"] == "2026-04-15" or fm["last_optimized"] is not None


async def test_optimizer_backup_created(infra):
    """After rewrite → .1 backup exists with prior content."""
    import skill_optimizer as opt

    skills_dir = infra["skills"]
    skill_path = skills_dir / "test-skill.md"
    original_content = "version 1 content"
    skill_path.write_text(original_content)

    config = {
        "skill_optimizer": {
            "max_skill_backups": 5,
            "dry_run": False
        }
    }

    with patch.object(opt, "SKILLS_DIR", skills_dir):
        optimizer = opt.SkillOptimizer(config)
        await optimizer._rotate_backups(skill_path)

    # Check backup created
    backup_path = skill_path.with_suffix(".md.1")
    assert backup_path.exists()
    assert backup_path.read_text() == original_content


async def test_optimizer_rollback_on_regression(infra):
    """Pre-populate prev_version_avg_score > new avg → optimizer restores backup."""
    import skill_optimizer as opt

    skills_dir = infra["skills"]

    # Create skill with regression
    regressed_content = """---
name: test-skill
version: 2
preferred_model: gemini/gemini-2.0-flash
success_rate: 0.55
total_runs: 20
prev_version_avg_score: 0.75
---

## Instructions

New instructions that caused regression.

## Evolution Log

### v2 (2026-04-14) — attempted improvement
### v1 (2026-04-10) — initial version
"""

    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(regressed_content)

    # Create backup with better performance
    backup_content = """---
name: test-skill
version: 1
preferred_model: gemini/gemini-2.0-flash
success_rate: 0.75
total_runs: 15
---

## Instructions

Original good instructions.

## Evolution Log

### v1 (2026-04-10) — initial version
"""

    backup_path = skill_path.with_suffix(".md.1")
    backup_path.write_text(backup_content)

    config = {
        "skill_optimizer": {
            "regression_tolerance": 0.05,
            "dry_run": False
        }
    }

    with patch.object(opt, "SKILLS_DIR", skills_dir):
        optimizer = opt.SkillOptimizer(config)
        rolled_back = await optimizer._check_regression_and_rollback(skill_path)

    # Should have rolled back
    assert rolled_back

    # Check content restored
    restored = skill_path.read_text()
    fm = yaml.safe_load(restored.split("---")[1])
    assert fm["version"] == 1
    assert "Original good instructions" in restored
    assert "New instructions that caused regression" not in restored


async def test_watcher_log_merged_before_scoring(infra):
    """Write JSONL to iCloud logs dir → rows appear in history."""
    import skill_optimizer as opt

    skills_dir = infra["skills"]
    logs_dir = infra["root"] / "logs"
    logs_dir.mkdir()

    # Create skill
    skill_content = """---
name: test-skill
version: 1
preferred_model: gemini/gemini-2.0-flash
---

## Instructions

Test instructions.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
"""

    skill_path = skills_dir / "test-skill.md"
    skill_path.write_text(skill_content)

    # Create watcher JSONL log
    watcher_log = logs_dir / "macbook-pro-execution-log.jsonl"
    record = {
        "date": "2026-04-15",
        "skill": "test-skill",
        "input_slug": "watcher-article",
        "model": "gemini/gemini-2.0-flash",
        "score": "0.80",
        "notes": "from watcher",
        "hostname": "macbook-pro"
    }
    watcher_log.write_text(json.dumps(record))

    config = {
        "skill_optimizer": {
            "run_hour": 3,
            "dry_run": False
        }
    }

    with patch.object(opt, "BRAIN_DIR", infra["root"]), \
         patch.object(opt, "SKILLS_DIR", skills_dir):
        optimizer = opt.SkillOptimizer(config)
        await optimizer._merge_watcher_logs()

    # Check row appeared in skill file
    updated = skill_path.read_text()
    assert "| 2026-04-15 | watcher-article | gemini/gemini-2.0-flash | 0.80 | from watcher |" in updated

    # Check watcher log was renamed
    assert not watcher_log.exists()
    processed_files = list(logs_dir.glob("*-execution-log.processed-*.jsonl"))
    assert len(processed_files) == 1


# ── Slack Scanner integration ─────────────────────────────────────────────────

async def test_slack_scanner_writes_memory_for_thread(tmp_path):
    """SlackScanner._run_scan → slack-thread-*.md written with correct frontmatter."""
    import slack_scanner as ss
    from slack_scanner import SlackScanner
    from commitment_tracker import _parse_frontmatter

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "slack-state.json"

    CHANNELS_RESPONSE = {
        "ok": True,
        "channels": [
            {"id": "C001", "name": "engineering", "is_archived": False}
        ]
    }

    MESSAGES_RESPONSE = {
        "ok": True,
        "messages": [
            {
                "ts": "1712700000.000200",
                "thread_ts": "1712700000.000200",
                "reply_count": 2,
                "text": "Should we migrate to Postgres?",
                "user": "U001"
            }
        ]
    }

    THREAD_RESPONSE = {
        "ok": True,
        "messages": [
            {"ts": "1712700000.000200", "user": "U001", "text": "Should we migrate to Postgres?"},
            {"ts": "1712750000.000000", "user": "U002", "text": "What's the use case?"},
            {"ts": "1712800000.000000", "user": "U001", "text": "Job queuing with pg-boss"}
        ]
    }

    USER_RESPONSE = {
        "ok": True,
        "user": {"real_name": "Alice Smith", "name": "alice"}
    }

    LLM_RESPONSE_TEXT = json.dumps({
        "summary": "Team discussed migrating from SQLite to Postgres for job queuing.",
        "tags": ["postgres", "database", "migration"]
    })

    scanner = SlackScanner(role="full")

    import yaml as _yaml

    async def fake_api_call(client, method, params=None, _retry=0):
        if method == "users.conversations":
            return CHANNELS_RESPONSE
        elif method == "conversations.history":
            return MESSAGES_RESPONSE
        elif method == "conversations.replies":
            return THREAD_RESPONSE
        elif method == "users.info":
            return USER_RESPONSE
        elif method == "auth.test":
            return {"ok": True, "user_id": "U001", "user": "testuser"}
        return None

    mock_acompletion = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = LLM_RESPONSE_TEXT
    mock_acompletion.return_value = mock_resp

    with patch.object(ss, "MEMORIES_DIR", memories_dir), \
         patch.object(ss, "STATE_FILE", state_file), \
         patch.object(ss, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(scanner, "_api_call", side_effect=fake_api_call), \
         patch("litellm.acompletion", mock_acompletion), \
         patch.dict(os.environ, {"SLACK_USER_TOKEN": "xoxp-test"}):

        (tmp_path / "config.yaml").write_text(_yaml.dump({
            "slack_scanner": {
                "interval_seconds": 300,
                "lookback_days": 7,
                "channel_include": [],
                "channel_exclude": [],
                "min_thread_messages": 2
            }
        }))
        await scanner._run_scan()

    # Check memory file written
    files = list(memories_dir.glob("slack-thread-*.md"))
    assert len(files) == 1

    text = files[0].read_text()
    fm = _parse_frontmatter(text)

    assert fm["type"] == "slack_thread"
    assert fm["channel"] == "engineering"
    assert fm["source_url"] == "slack:C001/1712700000.000200"
    assert fm["message_count"] == 3
    assert "## Messages" in text
    assert "Alice Smith" in text

    # State file updated
    state = json.loads(state_file.read_text())
    assert "C001" in state["channels"]
    assert "1712700000.000200" in state["channels"]["C001"]["threads"]


# ── Calendar Scanner integration ──────────────────────────────────────────────

async def test_calendar_scanner_writes_memory_for_event(tmp_path):
    """CalendarScanner._run_scan → calendar-event-*.md written with correct frontmatter."""
    import sqlite3 as _sqlite3
    import calendar_scanner as cs
    from calendar_scanner import CalendarScanner, CalendarCacheSource, _datetime_to_cd
    from datetime import datetime as _dt, timedelta as _td

    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "calendar-state.json"

    # Build a minimal Calendar Cache SQLite database
    db_path = tmp_path / "Calendar Cache"
    conn = _sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE ZCALENDAR (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT
        );
        CREATE TABLE ZCALENDARITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZTITLE TEXT,
            ZSTARTDATE REAL,
            ZENDDATE REAL,
            ZMODIFIEDDATE REAL,
            ZLOCATION TEXT,
            ZNOTES TEXT,
            ZISALLDAY INTEGER,
            ZHASRECURRENCERULES INTEGER,
            ZMYATTENDEESTATUS INTEGER,
            ZEXTERNALIDENTIFIER TEXT,
            ZCALENDAR INTEGER
        );
        CREATE TABLE ZATTENDEE (
            ZCOMMONNAME TEXT,
            ZADDRESS TEXT,
            ZCALENDARITEM INTEGER
        );
    """)
    conn.execute("INSERT INTO ZCALENDAR (Z_PK, ZTITLE) VALUES (1, 'Work')")

    # Insert an event
    now = _dt.now()
    event_start = now + _td(days=1)
    start_cd = _datetime_to_cd(event_start)
    end_cd = _datetime_to_cd(event_start + _td(hours=1))
    modified_cd = _datetime_to_cd(now)

    conn.execute(
        "INSERT INTO ZCALENDARITEM VALUES (1, 'Team Standup', ?, ?, ?, 'Zoom', 'Sprint review items', 0, 1, 0, 'abc123', 1)",
        (start_cd, end_cd, modified_cd)
    )
    conn.execute("INSERT INTO ZATTENDEE VALUES ('Chris Robertson', 'chris@example.com', 1)")
    conn.execute("INSERT INTO ZATTENDEE VALUES ('Sarah Chen', 'sarah@example.com', 1)")
    conn.commit()
    conn.close()

    config_content = {
        "calendar_scanner": {
            "interval_seconds": 300,
            "lookback_days": 7,
            "forward_days": 7,
            "skip_calendars": [],
            "max_events_per_cycle": 50,
        }
    }

    scanner = CalendarScanner(role="full")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "STATE_FILE", state_file), \
         patch.object(cs, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch.object(CalendarCacheSource, "_find_db_path", return_value=db_path), \
         patch.object(CalendarCacheSource, "_copy_db", return_value=db_path), \
         patch("calendar_scanner.acompletion", new=AsyncMock(
             return_value=MagicMock(
                 choices=[MagicMock(message=MagicMock(
                     content='{"summary": "Weekly engineering standup", "tags": ["standup", "engineering"]}'
                 ))]
             )
         ), create=True):

        import yaml as _yaml
        (tmp_path / "config.yaml").write_text(_yaml.dump(config_content))

        await scanner._run_scan()

    mem_files = list(memories_dir.glob("calendar-event-*.md"))
    assert len(mem_files) == 1, f"Expected 1 calendar memory, got {[f.name for f in mem_files]}"

    import yaml as _yaml
    text = mem_files[0].read_text()
    parts = text.split("---", 2)
    fm = _yaml.safe_load(parts[1])

    assert fm["type"] == "calendar_event"
    assert fm["source_title"] == "Team Standup"
    assert fm.get("calendar_names") == ["Work"]
    assert fm["location"] == "Zoom"
    assert fm["recurrence"] is True
    assert len(fm["participants"]) == 2
    assert any(p["email"] == "chris@example.com" for p in fm["participants"])
    assert "## Event Details" in text
    assert "## Context" in text
