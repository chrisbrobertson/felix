"""
Unit tests for slack_client.SlackClient.

Tests api_call, resolve_user, list_channels, post_message, and clear_user_cache.
httpx is mocked at the AsyncClient level throughout.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_client import SlackClient


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_httpx_ctx(status: int, body: dict):
    """Return an async context-manager mock for httpx.AsyncClient that yields a
    fake response with .status_code, .headers, .raise_for_status(), and .json()."""
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    resp.json.return_value = body

    inner = AsyncMock()
    inner.__aenter__ = AsyncMock(return_value=inner)
    inner.__aexit__ = AsyncMock(return_value=False)
    inner.get = AsyncMock(return_value=resp)
    return inner, resp


# ── api_call ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_call_success():
    """200 response with ok:true → parsed dict returned."""
    client = SlackClient(token="xoxp-test")
    inner, _ = _make_httpx_ctx(200, {"ok": True, "data": "value"})

    with patch("httpx.AsyncClient", return_value=inner):
        result = await client.api_call("conversations.list")

    assert result == {"ok": True, "data": "value"}


@pytest.mark.asyncio
async def test_api_call_ok_false_returns_none():
    """`ok: false` in response → None returned, warning logged."""
    client = SlackClient(token="xoxp-test")
    inner, _ = _make_httpx_ctx(200, {"ok": False, "error": "channel_not_found"})

    with patch("httpx.AsyncClient", return_value=inner), \
         patch("slack_client.log") as mock_log:
        result = await client.api_call("conversations.info", {"channel": "CXXX"})

    assert result is None
    mock_log.warning.assert_called_once()


@pytest.mark.asyncio
async def test_api_call_401_returns_none():
    """401 Unauthorized → None returned, error logged."""
    client = SlackClient(token="xoxp-bad")
    resp = MagicMock()
    resp.status_code = 401
    resp.headers = {}

    inner = AsyncMock()
    inner.__aenter__ = AsyncMock(return_value=inner)
    inner.__aexit__ = AsyncMock(return_value=False)
    inner.get = AsyncMock(return_value=resp)

    with patch("httpx.AsyncClient", return_value=inner), \
         patch("slack_client.log") as mock_log:
        result = await client.api_call("auth.test")

    assert result is None
    mock_log.error.assert_called_once()
    assert "401" in mock_log.error.call_args[0][0]


@pytest.mark.asyncio
async def test_api_call_429_retry_after():
    """429 + Retry-After:5 → sleep 5s, retry succeeds."""
    client = SlackClient(token="xoxp-test")
    call_count = [0]

    async def mock_get(*args, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        if call_count[0] == 1:
            resp.status_code = 429
            resp.headers = {"Retry-After": "5"}
        else:
            resp.status_code = 200
            resp.headers = {}
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"ok": True, "result": "ok"}
        return resp

    sleep_calls = []

    async def mock_sleep(secs):
        sleep_calls.append(secs)

    inner = AsyncMock()
    inner.__aenter__ = AsyncMock(return_value=inner)
    inner.__aexit__ = AsyncMock(return_value=False)
    inner.get = mock_get

    with patch("httpx.AsyncClient", return_value=inner), \
         patch("slack_client.asyncio.sleep", side_effect=mock_sleep):
        result = await client.api_call("users.list")

    assert result == {"ok": True, "result": "ok"}
    assert sleep_calls == [5]


@pytest.mark.asyncio
async def test_api_call_429_persistent_returns_none():
    """Two 429s in a row → None returned, error logged."""
    client = SlackClient(token="xoxp-test")

    async def always_429(*args, **kwargs):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "1"}
        return resp

    inner = AsyncMock()
    inner.__aenter__ = AsyncMock(return_value=inner)
    inner.__aexit__ = AsyncMock(return_value=False)
    inner.get = always_429

    with patch("httpx.AsyncClient", return_value=inner), \
         patch("slack_client.asyncio.sleep"), \
         patch("slack_client.log") as mock_log:
        result = await client.api_call("users.list")

    assert result is None
    mock_log.error.assert_called_once()


@pytest.mark.asyncio
async def test_api_call_exception_returns_none():
    """Network error → None returned, warning logged."""
    client = SlackClient(token="xoxp-test")

    async def raises(*args, **kwargs):
        raise ConnectionError("network down")

    inner = AsyncMock()
    inner.__aenter__ = AsyncMock(return_value=inner)
    inner.__aexit__ = AsyncMock(return_value=False)
    inner.get = raises

    with patch("httpx.AsyncClient", return_value=inner), \
         patch("slack_client.log") as mock_log:
        result = await client.api_call("conversations.list")

    assert result is None
    mock_log.warning.assert_called_once()


# ── resolve_user ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_user_returns_real_name():
    """users.info response with real_name → real_name returned."""
    client = SlackClient(token="xoxp-test")

    async def mock_api(method, params=None, **kwargs):
        return {"ok": True, "user": {"real_name": "Alice Smith", "name": "alice"}}

    client.api_call = mock_api  # type: ignore
    name = await client.resolve_user("U001")
    assert name == "Alice Smith"


@pytest.mark.asyncio
async def test_resolve_user_falls_back_to_name():
    """No real_name but name present → name returned."""
    client = SlackClient(token="xoxp-test")

    async def mock_api(method, params=None, **kwargs):
        return {"ok": True, "user": {"name": "alice"}}

    client.api_call = mock_api  # type: ignore
    name = await client.resolve_user("U001")
    assert name == "alice"


@pytest.mark.asyncio
async def test_resolve_user_cache_hit():
    """Second lookup for same user ID → no additional api_call."""
    client = SlackClient(token="xoxp-test")
    call_count = [0]

    async def mock_api(method, params=None, **kwargs):
        call_count[0] += 1
        return {"ok": True, "user": {"real_name": "Bob Jones", "name": "bob"}}

    client.api_call = mock_api  # type: ignore

    name1 = await client.resolve_user("U002")
    name2 = await client.resolve_user("U002")

    assert name1 == name2 == "Bob Jones"
    assert call_count[0] == 1  # only one API call despite two lookups


@pytest.mark.asyncio
async def test_resolve_user_unknown_on_api_failure():
    """api_call returns None → "Unknown User" returned gracefully."""
    client = SlackClient(token="xoxp-test")

    async def mock_api(method, params=None, **kwargs):
        return None

    client.api_call = mock_api  # type: ignore
    name = await client.resolve_user("U999")
    assert name == "Unknown User"


# ── list_channels ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_channels_single_page():
    """Single page of channels → list of (id, name) tuples."""
    client = SlackClient(token="xoxp-test")
    channels_data = [
        {"id": "C001", "name": "engineering", "is_archived": False},
        {"id": "C002", "name": "general", "is_archived": False},
    ]

    async def mock_api(method, params=None, **kwargs):
        assert method == "users.conversations"
        return {"ok": True, "channels": channels_data, "response_metadata": {"next_cursor": ""}}

    client.api_call = mock_api  # type: ignore
    result = await client.list_channels()
    assert result == [("C001", "engineering"), ("C002", "general")]


@pytest.mark.asyncio
async def test_list_channels_pagination():
    """Two pages of results → both pages concatenated."""
    client = SlackClient(token="xoxp-test")
    page = [0]

    async def mock_api(method, params=None, **kwargs):
        page[0] += 1
        if page[0] == 1:
            return {
                "ok": True,
                "channels": [{"id": "C001", "name": "engineering", "is_archived": False}],
                "response_metadata": {"next_cursor": "cursor-abc"},
            }
        else:
            assert params.get("cursor") == "cursor-abc"
            return {
                "ok": True,
                "channels": [{"id": "C002", "name": "general", "is_archived": False}],
                "response_metadata": {"next_cursor": ""},
            }

    client.api_call = mock_api  # type: ignore
    with patch("slack_client.asyncio.sleep"):
        result = await client.list_channels()

    assert len(result) == 2
    assert ("C001", "engineering") in result
    assert ("C002", "general") in result


@pytest.mark.asyncio
async def test_list_channels_skips_archived():
    """Archived channels are excluded from results."""
    client = SlackClient(token="xoxp-test")

    async def mock_api(method, params=None, **kwargs):
        return {
            "ok": True,
            "channels": [
                {"id": "C001", "name": "active", "is_archived": False},
                {"id": "C002", "name": "archived", "is_archived": True},
            ],
            "response_metadata": {"next_cursor": ""},
        }

    client.api_call = mock_api  # type: ignore
    result = await client.list_channels()
    assert result == [("C001", "active")]


@pytest.mark.asyncio
async def test_list_channels_api_failure_returns_empty():
    """api_call returns None → empty list, no crash."""
    client = SlackClient(token="xoxp-test")

    async def mock_api(method, params=None, **kwargs):
        return None

    client.api_call = mock_api  # type: ignore
    result = await client.list_channels()
    assert result == []


# ── post_message ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_message_success():
    """Successful chat.postMessage → True returned."""
    client = SlackClient(token="xoxb-bot")

    async def mock_api(method, params=None, **kwargs):
        assert method == "chat.postMessage"
        assert params["channel"] == "D001"
        assert params["text"] == "Hello!"
        return {"ok": True, "ts": "1712800000.000001"}

    client.api_call = mock_api  # type: ignore
    result = await client.post_message("D001", "Hello!")
    assert result is True


@pytest.mark.asyncio
async def test_post_message_failure_returns_false():
    """api_call returns None (e.g., channel_not_found) → False returned."""
    client = SlackClient(token="xoxb-bot")

    async def mock_api(method, params=None, **kwargs):
        return None

    client.api_call = mock_api  # type: ignore
    result = await client.post_message("D999", "Hello!")
    assert result is False


# ── clear_user_cache ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_user_cache():
    """clear_user_cache() empties the in-memory cache."""
    client = SlackClient(token="xoxp-test")
    client._user_cache["U001"] = "Alice"
    client._user_cache["U002"] = "Bob"

    client.clear_user_cache()

    assert client._user_cache == {}
