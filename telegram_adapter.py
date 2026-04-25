"""Telegram TransportAdapter — wraps TelegramChatHandler.

This module implements the TransportAdapter protocol for Telegram.
In Phase 0, it is a thin wrapper over the existing TelegramChatHandler.
"""
import logging
from typing import TYPE_CHECKING

from transport import CommandContext, TransportAdapter

if TYPE_CHECKING:
    from chat_handler import TelegramChatHandler

log = logging.getLogger("telegram-adapter")

_MAX_CHUNK = 4096  # Telegram message length limit


def _chunk_text(text: str, size: int = _MAX_CHUNK) -> list[str]:
    """Split text into chunks of at most *size* characters."""
    if len(text) <= size:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:size])
        text = text[size:]
    return chunks


class TelegramAdapter:
    """TransportAdapter implementation for Telegram.

    Wraps a TelegramChatHandler instance and exposes the TransportAdapter
    protocol so that NotificationManager and future code can treat Telegram
    as one of several possible transports.

    Phase 0: start/stop/send_text delegate to the existing handler.
    The handler continues to run its own polling loop; this adapter only
    adds the TransportAdapter interface on top.
    """

    def __init__(self, handler: "TelegramChatHandler"):
        self._handler = handler

    # ── TransportAdapter protocol ─────────────────────────────────────────────

    async def start(self) -> None:
        """Start the Telegram polling loop via the underlying handler."""
        await self._handler.start()

    async def stop(self) -> None:
        """Stop the Telegram polling loop."""
        await self._handler.stop()

    async def send_text(self, chat_id: str, text: str) -> None:
        """Send *text* to *chat_id*, chunking if needed."""
        bot = self._bot()
        if bot is None:
            raise RuntimeError("TelegramAdapter.send_text: bot not available")
        for chunk in _chunk_text(text, _MAX_CHUNK):
            await bot.send_message(chat_id=int(chat_id), text=chunk)

    async def send_typing(self, chat_id: str) -> None:
        """Send a typing indicator to *chat_id*."""
        bot = self._bot()
        if bot is None:
            return
        try:
            from telegram.constants import ChatAction
            await bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
        except Exception as e:
            log.debug("TelegramAdapter.send_typing failed: %s", e)

    def max_message_length(self) -> int:
        return _MAX_CHUNK

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _bot(self):
        """Return the underlying telegram.Bot, or None if not yet initialised."""
        handler = self._handler
        app = getattr(handler, "app", None)   # was "_app" — TelegramChatHandler stores self.app
        if app is not None:
            return getattr(app, "bot", None)
        return None

    def get_chat_id(self) -> "str | None":  # str | None requires Python 3.10+; keep as string annotation
        """Return the authorised user's chat_id as a string, or None."""
        # Fall back to notification state (the handler does not expose _chat_id)
        try:
            from notification_manager import _load_state
            state = _load_state()
            cid = state.get("chat_id")
            return str(cid) if cid is not None else None
        except Exception:
            return None
