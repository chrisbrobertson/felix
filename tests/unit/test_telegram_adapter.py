"""Unit tests for telegram_adapter.TelegramAdapter."""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_adapter import TelegramAdapter


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_handler(chat_id=None):
    """Return a minimal mock TelegramChatHandler."""
    handler = MagicMock()
    handler._chat_id = chat_id
    handler._app = None  # bot not yet initialised by default
    return handler


def _make_adapter(chat_id=None):
    handler = _make_handler(chat_id=chat_id)
    return TelegramAdapter(handler), handler


# ── max_message_length ────────────────────────────────────────────────────────

def test_max_message_length():
    adapter, _ = _make_adapter()
    assert adapter.max_message_length() == 4096


# ── send_text ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_text_calls_bot_send_message():
    adapter, handler = _make_adapter()
    mock_bot = AsyncMock()
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    handler._app = mock_app

    await adapter.send_text("123", "hello")

    mock_bot.send_message.assert_awaited_once_with(chat_id=123, text="hello")


@pytest.mark.asyncio
async def test_send_text_chunks_at_4096():
    adapter, handler = _make_adapter()
    mock_bot = AsyncMock()
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    handler._app = mock_app

    long_text = "x" * 9000
    await adapter.send_text("1", long_text)

    assert mock_bot.send_message.await_count == 3  # 9000 / 4096 → 3 chunks


@pytest.mark.asyncio
async def test_send_text_no_bot_is_silent():
    """When bot is unavailable, send_text must not raise."""
    adapter, handler = _make_adapter()
    handler._app = None  # no bot

    await adapter.send_text("1", "hello")  # should not raise


@pytest.mark.asyncio
async def test_send_text_bot_error_is_silent():
    """A bot.send_message exception must be swallowed, not propagated."""
    adapter, handler = _make_adapter()
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = RuntimeError("network error")
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    handler._app = mock_app

    await adapter.send_text("1", "hello")  # should not raise


# ── send_typing ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_typing_calls_send_chat_action():
    # ChatAction is imported inside send_typing from the `telegram` package,
    # so we patch it at its source module.
    adapter, handler = _make_adapter()
    mock_bot = AsyncMock()
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    handler._app = mock_app

    with patch("telegram.constants.ChatAction") as mock_action:
        mock_action.TYPING = "typing"
        await adapter.send_typing("42")

    mock_bot.send_chat_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_typing_no_bot_is_silent():
    adapter, handler = _make_adapter()
    handler._app = None
    await adapter.send_typing("1")  # should not raise


# ── get_chat_id ───────────────────────────────────────────────────────────────

def test_get_chat_id_from_handler_attribute():
    adapter, _ = _make_adapter(chat_id=99)
    assert adapter.get_chat_id() == "99"


def test_get_chat_id_none_when_no_chat_id_anywhere():
    # _load_state is imported inside get_chat_id from notification_manager
    adapter, _ = _make_adapter(chat_id=None)
    with patch("notification_manager._load_state", return_value={}):
        result = adapter.get_chat_id()
    assert result is None


def test_get_chat_id_falls_back_to_notification_state():
    adapter, _ = _make_adapter(chat_id=None)
    with patch("notification_manager._load_state", return_value={"chat_id": 777}):
        result = adapter.get_chat_id()
    assert result == "777"


def test_get_chat_id_handler_attribute_takes_priority_over_state():
    # _chat_id on the handler takes priority; _load_state is never called
    adapter, _ = _make_adapter(chat_id=42)
    result = adapter.get_chat_id()
    assert result == "42"


# ── _bot helper ───────────────────────────────────────────────────────────────

def test_bot_returns_none_when_app_is_none():
    adapter, handler = _make_adapter()
    handler._app = None
    assert adapter._bot() is None


def test_bot_returns_none_when_app_has_no_bot():
    adapter, handler = _make_adapter()
    mock_app = MagicMock(spec=[])  # spec with no attributes
    handler._app = mock_app
    assert adapter._bot() is None


def test_bot_returns_bot_from_app():
    adapter, handler = _make_adapter()
    mock_bot = MagicMock()
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    handler._app = mock_app
    assert adapter._bot() is mock_bot
