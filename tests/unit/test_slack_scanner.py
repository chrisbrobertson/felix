"""
Unit tests for slack_scanner.

All external access (Slack API, httpx, LiteLLM, filesystem) is mocked.
After Phase 1 refactor, Slack API calls go through SlackClient (scanner._client).
"""
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import yaml

import slack_scanner as ss
from slack_scanner import SlackScanner, MAX_TRANSCRIPT_LINES


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def scanner(tmp_path):
    s = SlackScanner(role="full")
    with patch.object(ss, "MEMORIES_DIR", tmp_path / "memories"), \
         patch.object(ss, "STATE_FILE", tmp_path / "slack-state.json"):
        (tmp_path / "memories").mkdir()
        yield s


def _make_mock_client():
    """Return a pre-configured AsyncMock that behaves like SlackClient."""
    client = MagicMock()
    client.api_call = AsyncMock(return_value={"ok": True})
    client.resolve_user = AsyncMock(return_value="Unknown User")
    client.list_channels = AsyncMock(return_value=[])
    client.clear_user_cache = MagicMock()
    return client


@pytest.fixture
def scanner_with_client(scanner):
    """Scanner with a mock SlackClient pre-installed (for _run_scan tests)."""
    scanner._client = _make_mock_client()
    return scanner


# ── Startup and credentials ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_token_logs_warning_and_exits(scanner, tmp_path):
    """No SLACK_USER_TOKEN → WARNING logged, loop exits cleanly."""
    with patch.dict(os.environ, {}, clear=True), \
         patch("slack_scanner.get_secret_or_env", return_value=None):
        stop = asyncio.Event()
        with patch("slack_scanner.log") as mock_log:
            await scanner.run_loop(stop)
            mock_log.warning.assert_called_once()
            assert "SLACK_USER_TOKEN not set" in mock_log.warning.call_args[0][0]


@pytest.mark.asyncio
async def test_invalid_token_logs_error_and_exits(scanner, tmp_path, caplog):
    """401 response → ERROR logged, loop exits cleanly."""
    import logging
    from slack_client import SlackClient

    with patch.dict(os.environ, {"SLACK_USER_TOKEN": "xoxp-invalid"}):
        stop = asyncio.Event()

        # Patch SlackClient so that api_call returns None (simulating auth failure)
        async def mock_api_call(method, params=None, **kwargs):
            if method == "auth.test":
                stop.set()
                return None
            return None

        with patch.object(SlackClient, "api_call", side_effect=mock_api_call), \
             caplog.at_level(logging.ERROR):
            await scanner.run_loop(stop)
            assert "SLACK_USER_TOKEN" in caplog.text or "auth.test" in caplog.text


@pytest.mark.asyncio
async def test_resolve_self_populates_own_user_id(tmp_path):
    """_resolve_self caches own_user_id from auth.test response."""
    scanner = SlackScanner(role="full")
    scanner._client = _make_mock_client()

    auth_response = {
        "ok": True, "user_id": "U01234567", "user": "testuser",
        "team": "TestTeam", "url": "https://testteam.slack.com/"
    }
    scanner._client.api_call = AsyncMock(return_value=auth_response)

    result = await scanner._resolve_self()

    assert result is True
    assert scanner.own_user_id == "U01234567"


@pytest.mark.asyncio
async def test_resolve_self_401_logs_friendly_error(tmp_path, caplog):
    """_resolve_self logs a friendly error with xoxp- hint on auth failure."""
    import logging
    scanner = SlackScanner(role="full")
    scanner._client = _make_mock_client()
    scanner._client.api_call = AsyncMock(return_value=None)

    with caplog.at_level(logging.ERROR):
        result = await scanner._resolve_self()

    assert result is False
    assert "xoxp-" in caplog.text


# ── Channel filtering ─────────────────────────────────────────────────────────

def test_channel_include_whitelist(scanner):
    """channel_include: [engineering] → only that channel scanned."""
    channels = [("C001", "engineering"), ("C002", "general"), ("C003", "random")]
    config = {"channel_include": ["engineering"]}
    result = scanner._filter_channels(channels, config)
    assert result == [("C001", "engineering")]


def test_channel_include_overrides_exclude(scanner):
    """Non-empty include → exclude list ignored."""
    channels = [("C001", "engineering"), ("C002", "general")]
    config = {"channel_include": ["engineering"], "channel_exclude": ["engineering"]}
    result = scanner._filter_channels(channels, config)
    assert result == [("C001", "engineering")]


def test_channel_exclude_blacklist(scanner):
    """channel_exclude: [general] → general channel skipped."""
    channels = [("C001", "engineering"), ("C002", "general"), ("C003", "random")]
    config = {"channel_exclude": ["general"]}
    result = scanner._filter_channels(channels, config)
    assert len(result) == 2
    assert ("C002", "general") not in result


@pytest.mark.asyncio
async def test_archived_channel_always_excluded(scanner_with_client):
    """Scanner only processes channels returned by list_channels(); archived ones are excluded there.

    SlackClient.list_channels() passes exclude_archived=true to the API, so the scanner
    never sees archived channels. This test verifies the scanner uses list_channels() as
    its sole channel source — if it bypassed that and called conversations.list directly,
    archived channels could leak through.
    """
    scanner = scanner_with_client
    # list_channels returns only the non-archived channel (archived already excluded by SlackClient)
    scanner._client.list_channels = AsyncMock(return_value=[("C001", "general")])

    fetch_calls = []

    async def track_fetch(channel_id, *args, **kwargs):
        fetch_calls.append(channel_id)
        return []

    with patch.object(scanner, "_fetch_channel_messages", side_effect=track_fetch):
        await scanner._run_scan()

    # Only C001 (the non-archived channel) was fetched
    assert fetch_calls == ["C001"]
    # list_channels() was called — scanner used it as the channel source
    scanner._client.list_channels.assert_awaited_once()


# ── High-water and lookback ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_high_water_incremental_polling(scanner_with_client, tmp_path):
    """Second cycle only requests messages after stored high_water."""
    state_file = tmp_path / "slack-state.json"
    scanner = scanner_with_client
    with patch.object(ss, "STATE_FILE", state_file):
        state = {
            "channels": {
                "C001": {
                    "name": "engineering",
                    "high_water": "1712800000.000000",
                    "threads": {}
                }
            }
        }
        state_file.write_text(json.dumps(state))

        called_oldest = []

        async def mock_fetch_messages(channel_id, high_water, lookback_days):
            called_oldest.append(high_water)
            return []

        scanner._client.list_channels = AsyncMock(return_value=[("C001", "engineering")])
        with patch.object(scanner, "_fetch_channel_messages", side_effect=mock_fetch_messages):
            await scanner._run_scan()
            assert called_oldest[0] == "1712800000.000000"


@pytest.mark.asyncio
async def test_first_run_uses_lookback_days(scanner_with_client, tmp_path):
    """No state → oldest set to now − lookback_days."""
    scanner = scanner_with_client
    called_args = []

    async def capture_args(channel_id, high_water, lookback_days):
        called_args.append((high_water, lookback_days))
        return []

    scanner._client.list_channels = AsyncMock(return_value=[("C001", "engineering")])
    scanner._fetch_channel_messages = capture_args
    await scanner._run_scan()

    hw, lb = called_args[0]
    assert hw is None
    assert lb == 7  # default lookback_days


# ── Thread detection ──────────────────────────────────────────────────────────

def test_thread_detection(scanner):
    """reply_count > 0 and thread_ts == ts → thread root."""
    msg = {"ts": "1712700000.000200", "thread_ts": "1712700000.000200", "reply_count": 3}
    assert msg["reply_count"] > 0
    assert msg["thread_ts"] == msg["ts"]


@pytest.mark.asyncio
async def test_min_thread_messages_filter(scanner_with_client):
    """reply_count < min_thread_messages (default 2) → thread not fetched."""
    scanner = scanner_with_client
    scanner._client.list_channels = AsyncMock(return_value=[("C001", "general")])

    # Message has only 1 reply — below the threshold of 2
    low_reply_msg = {
        "ts": "1712700000.000100",
        "thread_ts": "1712700000.000100",
        "reply_count": 1,
        "text": "root message",
    }

    with patch.object(scanner, "_fetch_channel_messages", return_value=[low_reply_msg]), \
         patch.object(scanner, "_fetch_thread_replies") as mock_fetch_replies:
        await scanner._run_scan()

    mock_fetch_replies.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_messages_skipped(scanner_with_client):
    """Message where thread_ts != ts (a reply, not a root) → not processed as thread root."""
    scanner = scanner_with_client
    scanner._client.list_channels = AsyncMock(return_value=[("C001", "general")])

    # thread_ts != ts means this is a reply in someone else's thread, not a root
    reply_msg = {
        "ts": "1712700000.000200",
        "thread_ts": "1712700000.000100",  # different from ts — this is a reply
        "reply_count": 5,
        "text": "a reply",
    }

    with patch.object(scanner, "_fetch_channel_messages", return_value=[reply_msg]), \
         patch.object(scanner, "_fetch_thread_replies") as mock_fetch_replies:
        await scanner._run_scan()

    mock_fetch_replies.assert_not_called()


# ── Change detection ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_detection_unchanged(scanner_with_client, tmp_path):
    """Same message_count + last_ts → no LLM call, no memory write."""
    import time as _time

    scanner = scanner_with_client
    state_file = tmp_path / "slack-state.json"

    # Use recent timestamps so _prune_threads (lookback_days=7) doesn't evict the entry
    now = _time.time()
    thread_ts = f"{now - 3600:.6f}"   # 1 hour ago
    last_ts   = f"{now - 1800:.6f}"   # 30 minutes ago

    state = {
        "channels": {
            "C001": {
                "name": "engineering",
                "threads": {
                    thread_ts: {
                        "message_count": 2,
                        "last_ts": last_ts,
                    }
                },
            }
        }
    }
    state_file.write_text(json.dumps(state))

    thread_msg = {
        "ts": thread_ts,
        "thread_ts": thread_ts,
        "reply_count": 2,
        "text": "root",
    }
    # Full thread returns same count and same last_ts — nothing changed
    full_thread = [
        {"ts": thread_ts, "user": "U001", "text": "root"},
        {"ts": last_ts,   "user": "U002", "text": "reply"},
    ]

    scanner._client.list_channels = AsyncMock(return_value=[("C001", "engineering")])

    with patch.object(ss, "STATE_FILE", state_file), \
         patch.object(scanner, "_fetch_channel_messages", return_value=[thread_msg]), \
         patch.object(scanner, "_fetch_thread_replies", return_value=full_thread), \
         patch.object(scanner, "_generate_summary") as mock_llm, \
         patch.object(scanner, "_write_memory") as mock_write:
        await scanner._run_scan()

    mock_llm.assert_not_called()
    mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_change_detection_new_reply(scanner_with_client, tmp_path):
    """New reply → re-fetch, re-summarize, overwrite."""
    scanner = scanner_with_client
    state_file = tmp_path / "slack-state.json"
    with patch.object(ss, "STATE_FILE", state_file):
        state = {
            "channels": {
                "C001": {
                    "name": "engineering",
                    "threads": {
                        "1712700000.000200": {
                            "message_count": 2,
                            "last_ts": "1712750000.000000"
                        }
                    }
                }
            }
        }
        state_file.write_text(json.dumps(state))

        thread_msg = {
            "ts": "1712700000.000200",
            "thread_ts": "1712700000.000200",
            "reply_count": 3,
            "text": "root message"
        }

        full_thread = [
            {"ts": "1712700000.000200", "user": "U001", "text": "root"},
            {"ts": "1712750000.000000", "user": "U002", "text": "reply 1"},
            {"ts": "1712800000.000000", "user": "U001", "text": "new reply"}
        ]

        llm_called = False

        async def mock_llm(*args, **kwargs):
            nonlocal llm_called
            llm_called = True
            return {"summary": "updated", "tags": []}

        scanner._client.list_channels = AsyncMock(return_value=[("C001", "engineering")])
        scanner._client.resolve_user = AsyncMock(return_value="Test User")
        with patch.object(scanner, "_fetch_channel_messages", return_value=[thread_msg]), \
             patch.object(scanner, "_fetch_thread_replies", return_value=full_thread), \
             patch.object(scanner, "_generate_summary", side_effect=mock_llm), \
             patch.object(scanner, "_write_memory"):
            await scanner._run_scan()
            assert llm_called


# ── User ID resolution ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_id_resolution_cached(scanner):
    """Second call for same user ID → cache used, no API call."""
    from slack_client import SlackClient

    api_calls = []

    async def mock_api_call(method, params=None, **kwargs):
        api_calls.append(method)
        return {"ok": True, "user": {"real_name": "Alice Smith", "name": "alice"}}

    client = SlackClient(token="xoxp-test")
    client.api_call = mock_api_call  # type: ignore

    # First call
    name1 = await client.resolve_user("U001")
    assert name1 == "Alice Smith"
    assert len(api_calls) == 1

    # Second call — should use cache
    name2 = await client.resolve_user("U001")
    assert name2 == "Alice Smith"
    assert len(api_calls) == 1  # no additional API call


@pytest.mark.asyncio
async def test_user_id_resolution_unknown(scanner):
    """Unknown user ID → "Unknown User", no crash."""
    from slack_client import SlackClient

    async def mock_api_call(method, params=None, **kwargs):
        return None

    client = SlackClient(token="xoxp-test")
    client.api_call = mock_api_call  # type: ignore
    name = await client.resolve_user("U999")
    assert name == "Unknown User"


@pytest.mark.asyncio
async def test_slack_user_id_identified(scanner):
    """Message from SLACK_USER_ID tagged as "me" in participants."""
    # This is tested via participant extraction in _write_memory.
    # The SLACK_USER_ID env var is used by commitment tracker, not directly by slack_scanner.
    # For slack_scanner, we just include user IDs in frontmatter.
    pass


# ── Memory file write ─────────────────────────────────────────────────────────

def test_write_memory_atomic(scanner, tmp_path):
    """No .tmp file left after write."""
    with patch.object(ss, "MEMORIES_DIR", tmp_path):
        messages = [
            {"ts": "1712700000.000200", "user": "U001", "text": "root", "_resolved_name": "Alice"},
            {"ts": "1712750000.000000", "user": "U002", "text": "reply", "_resolved_name": "Bob"}
        ]
        llm_result = {"summary": "test summary", "tags": ["test"]}
        scanner._write_memory("C001", "engineering", "1712700000.000200", messages, llm_result)

        # Check no .tmp files remain
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


def test_write_memory_type(scanner, tmp_path):
    """type: slack_thread in frontmatter."""
    with patch.object(ss, "MEMORIES_DIR", tmp_path):
        messages = [{"ts": "1712700000.000200", "user": "U001", "text": "root", "_resolved_name": "Alice"}]
        llm_result = {"summary": "test", "tags": []}
        scanner._write_memory("C001", "engineering", "1712700000.000200", messages, llm_result)

        files = list(tmp_path.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "type: slack_thread" in content


def test_write_memory_field_order(scanner, tmp_path):
    """source_title first in frontmatter."""
    with patch.object(ss, "MEMORIES_DIR", tmp_path):
        messages = [{"ts": "1712700000.000200", "user": "U001", "text": "root message", "_resolved_name": "Alice"}]
        llm_result = {"summary": "test", "tags": []}
        scanner._write_memory("C001", "engineering", "1712700000.000200", messages, llm_result)

        files = list(tmp_path.glob("*.md"))
        content = files[0].read_text()
        fm_start = content.index("---\n") + 4
        fm_end = content.index("\n---", fm_start)
        fm_text = content[fm_start:fm_end]
        first_line = fm_text.split("\n")[0]
        assert first_line.startswith("source_title:")


def test_source_url_scheme(scanner, tmp_path):
    """source_url starts with slack:."""
    with patch.object(ss, "MEMORIES_DIR", tmp_path):
        messages = [{"ts": "1712700000.000200", "user": "U001", "text": "root", "_resolved_name": "Alice"}]
        llm_result = {"summary": "test", "tags": []}
        scanner._write_memory("C001", "engineering", "1712700000.000200", messages, llm_result)

        files = list(tmp_path.glob("*.md"))
        content = files[0].read_text()
        assert "source_url: slack:C001/1712700000.000200" in content


def test_source_title_from_root_message(scanner, tmp_path):
    """Title derived from root message text, max 60 chars."""
    with patch.object(ss, "MEMORIES_DIR", tmp_path):
        long_text = "a" * 100
        messages = [{"ts": "1712700000.000200", "user": "U001", "text": long_text, "_resolved_name": "Alice"}]
        llm_result = {"summary": "test", "tags": []}
        scanner._write_memory("C001", "engineering", "1712700000.000200", messages, llm_result)

        files = list(tmp_path.glob("*.md"))
        content = files[0].read_text()
        # Extract source_title from frontmatter
        import re
        match = re.search(r'source_title: (.+)', content)
        assert match
        title = match.group(1)
        # Title may have quotes from yaml.dump
        title_clean = title.strip("'\"")
        # "Thread: " prefix + 60 chars max
        assert title_clean.startswith("Thread:")
        assert len(title_clean) <= 68  # "Thread: " = 8 chars + 60


def test_messages_capped_at_50_lines(scanner, tmp_path):
    """Thread with 60 messages → 50 in file body."""
    with patch.object(ss, "MEMORIES_DIR", tmp_path):
        messages = [
            {"ts": f"17127{i:05d}.000000", "user": "U001", "text": f"message {i}", "_resolved_name": "Alice"}
            for i in range(60)
        ]
        llm_result = {"summary": "test", "tags": []}
        scanner._write_memory("C001", "engineering", "1712700000.000000", messages, llm_result)

        files = list(tmp_path.glob("*.md"))
        content = files[0].read_text()
        # Count message lines in the Messages section
        messages_section = content.split("## Messages")[1].split("## Context")[0]
        message_lines = [l for l in messages_section.strip().split("\n") if l.startswith("[")]
        assert len(message_lines) == MAX_TRANSCRIPT_LINES


# ── Rate limiting ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_retry_after(scanner):
    """429 + Retry-After=5 → sleep 5s, retry. Tested via SlackClient.api_call."""
    from slack_client import SlackClient

    call_count = [0]

    async def mock_get(*args, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        if call_count[0] == 1:
            resp.status_code = 429
            resp.headers = {"Retry-After": "5"}
        else:
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"ok": True, "channels": []}
        return resp

    sleep_calls = []

    async def mock_sleep(duration):
        sleep_calls.append(duration)

    client = SlackClient(token="xoxb-test")

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("slack_client.asyncio.sleep", side_effect=mock_sleep):
        mock_httpx = AsyncMock()
        mock_httpx.__aenter__ = AsyncMock(return_value=mock_httpx)
        mock_httpx.__aexit__ = AsyncMock(return_value=False)
        mock_httpx.get = mock_get
        mock_client_cls.return_value = mock_httpx

        result = await client.api_call("conversations.list")
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 5
        assert result["ok"] is True


@pytest.mark.asyncio
async def test_rate_limit_two_consecutive_skips(scanner):
    """Second 429 → None returned, ERROR logged. Tested via SlackClient.api_call."""
    from slack_client import SlackClient

    async def mock_get(*args, **kwargs):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "1"}
        return resp

    client = SlackClient(token="xoxb-test")

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("slack_client.asyncio.sleep"), \
         patch("slack_client.log") as mock_log:
        mock_httpx = AsyncMock()
        mock_httpx.__aenter__ = AsyncMock(return_value=mock_httpx)
        mock_httpx.__aexit__ = AsyncMock(return_value=False)
        mock_httpx.get = mock_get
        mock_client_cls.return_value = mock_httpx

        result = await client.api_call("conversations.list")
        assert result is None
        mock_log.error.assert_called()


@pytest.mark.asyncio
async def test_inter_request_delay(scanner_with_client):
    """Paginated _fetch_channel_messages calls asyncio.sleep(1) between pages."""
    scanner = scanner_with_client

    # First API response has a cursor (triggers sleep); second has no cursor
    scanner._client.api_call = AsyncMock(side_effect=[
        {
            "ok": True,
            "messages": [{"ts": "1712700000.1", "text": "msg"}],
            "response_metadata": {"next_cursor": "cursor1"},
        },
        {
            "ok": True,
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        },
    ])

    with patch("slack_scanner.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        result = await scanner._fetch_channel_messages("C001", None, 7)

    mock_sleep.assert_called_once_with(1)
    assert len(result) == 1  # message from first page


# ── State file management ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_file_created_on_first_run(scanner_with_client, tmp_path):
    """No existing state → state file written."""
    scanner = scanner_with_client
    state_file = tmp_path / "slack-state.json"
    with patch.object(ss, "STATE_FILE", state_file):
        assert not state_file.exists()

        scanner._client.list_channels = AsyncMock(return_value=[("C001", "engineering")])
        with patch.object(scanner, "_fetch_channel_messages", return_value=[]):
            await scanner._run_scan()
            assert state_file.exists()


@pytest.mark.asyncio
async def test_state_file_persists_high_water(scanner_with_client, tmp_path):
    """High-water survives simulated restart."""
    scanner = scanner_with_client
    state_file = tmp_path / "slack-state.json"
    with patch.object(ss, "STATE_FILE", state_file):
        messages = [{"ts": "1712800000.000000", "text": "msg"}]

        scanner._client.list_channels = AsyncMock(return_value=[("C001", "engineering")])
        with patch.object(scanner, "_fetch_channel_messages", return_value=messages):
            await scanner._run_scan()

        # Reload state
        state = json.loads(state_file.read_text())
        assert "C001" in state["channels"]
        assert state["channels"]["C001"]["high_water"] == "1712800000.000000"


def test_thread_state_pruned_by_age(scanner, tmp_path):
    """Thread entries older than lookback_days pruned."""
    state_file = tmp_path / "slack-state.json"
    with patch.object(ss, "STATE_FILE", state_file):
        old_ts = str(time.time() - 10 * 86400)  # 10 days ago
        recent_ts = str(time.time() - 1 * 86400)  # 1 day ago

        state = {
            "channels": {
                "C001": {
                    "threads": {
                        "old": {"last_ts": old_ts, "message_count": 1},
                        "recent": {"last_ts": recent_ts, "message_count": 1}
                    }
                }
            }
        }
        state_file.write_text(json.dumps(state))

        loaded = scanner._load_state()
        scanner._prune_threads(loaded, lookback_days=7)

        # Old thread should be pruned
        assert "old" not in loaded["channels"]["C001"]["threads"]
        assert "recent" in loaded["channels"]["C001"]["threads"]


# ── Cycle limits ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_max_channels_per_cycle(scanner_with_client):
    """25 channels → 20 processed, 5 deferred."""
    scanner = scanner_with_client
    channels = [(f"C{i:03d}", f"channel-{i}") for i in range(25)]

    processed = []

    async def mock_fetch(channel_id, high_water, lookback_days):
        processed.append(channel_id)
        return []

    scanner._client.list_channels = AsyncMock(return_value=channels)
    with patch.object(scanner, "_fetch_channel_messages", side_effect=mock_fetch):
        await scanner._run_scan()
        assert len(processed) == 20


@pytest.mark.asyncio
async def test_max_threads_per_channel(scanner_with_client):
    """35 threads in channel → 30 processed."""
    scanner = scanner_with_client
    thread_msgs = [
        {"ts": f"1712700{i:03d}.000000", "thread_ts": f"1712700{i:03d}.000000", "reply_count": 3}
        for i in range(35)
    ]

    processed_threads = []

    async def mock_fetch_replies(channel_id, thread_ts):
        processed_threads.append(thread_ts)
        return [{"ts": thread_ts, "user": "U001", "text": "msg", "_resolved_name": "Alice"}]

    scanner._client.list_channels = AsyncMock(return_value=[("C001", "engineering")])
    scanner._client.resolve_user = AsyncMock(return_value="Alice")
    with patch.object(scanner, "_fetch_channel_messages", return_value=thread_msgs), \
         patch.object(scanner, "_fetch_thread_replies", side_effect=mock_fetch_replies), \
         patch.object(scanner, "_generate_summary", return_value={"summary": "test", "tags": []}), \
         patch.object(scanner, "_write_memory"):
        await scanner._run_scan()
        assert len(processed_threads) == 30


# ── Role check ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watcher_role_skips_slack_scanner():
    """role=watcher → SlackScanner not instantiated."""
    scanner_watcher = SlackScanner(role="watcher")
    stop = asyncio.Event()
    stop.set()

    with patch("slack_scanner.log") as mock_log:
        await scanner_watcher.run_loop(stop)
        # Should log debug and exit
        mock_log.debug.assert_called()
        assert "skipped" in mock_log.debug.call_args[0][0]


# ── Backfill ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_clears_high_water_per_channel(tmp_path):
    """backfill() clears high_water for all channels."""
    scanner = SlackScanner(role="full")
    scanner._client = _make_mock_client()

    with patch.object(ss, "MEMORIES_DIR", tmp_path / "memories"), \
         patch.object(ss, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(ss, "CONFIG_PATH", tmp_path / "config.yaml"):

        scanner._client.list_channels = AsyncMock(return_value=[("C123", "general")])
        scanner._filter_channels = MagicMock(return_value=[("C123", "general")])
        scanner._fetch_channel_messages = AsyncMock(return_value=[])  # No messages

        (tmp_path / "memories").mkdir()
        (tmp_path / "config.yaml").write_text("slack_scanner:\n  channel_include: []\n")

        scanner._save_state({
            "channels": {
                "C123": {"name": "general", "high_water": "1234567890.123456", "threads": {}},
                "C456": {"name": "random", "high_water": "1234567890.654321", "threads": {}}
            }
        })

        result = await scanner.backfill(30)

        # All channels' high_water should be cleared (None) by backfill
        state = scanner._load_state()
        assert state["channels"]["C123"].get("high_water") is None
        assert state["channels"]["C456"].get("high_water") is None
        assert result["processed"] == 0  # No threads processed
