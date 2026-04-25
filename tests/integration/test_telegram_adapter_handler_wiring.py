"""Integration test: TelegramAdapter._bot() reads from TelegramChatHandler.app."""
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def test_adapter_bot_matches_handler_app_bot():
    """TelegramAdapter._bot() must return the bot from handler.app, not handler._app."""
    from telegram_adapter import TelegramAdapter
    mock_bot = MagicMock()
    mock_app = MagicMock()
    mock_app.bot = mock_bot

    # Use spec=["app"] so that accessing handler._app raises AttributeError
    # (getattr with default catches it and returns None)
    handler = MagicMock(spec=["app"])
    handler.app = mock_app

    adapter = TelegramAdapter(handler)
    assert adapter._bot() is mock_bot


def test_adapter_bot_returns_none_when_handler_has_no_app():
    """When handler has no app attribute, _bot() returns None."""
    from telegram_adapter import TelegramAdapter

    # spec=[] means no attributes at all
    handler = MagicMock(spec=[])
    adapter = TelegramAdapter(handler)
    assert adapter._bot() is None
