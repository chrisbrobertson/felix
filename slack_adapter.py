"""Slack chat adapter — TransportAdapter implementation using Socket Mode.

Uses slack_bolt[async] + SlackClient. Receives DMs via WebSocket (no public URL).
Command prefix: !command args  (avoids Slack's built-in slash registration).
"""
import asyncio
import logging
from typing import Optional

from slack_client import SlackClient
from transport import CommandContext, TransportAdapter

log = logging.getLogger("slack-adapter")

_MAX_CHUNK = 4000  # Slack recommends < 4000 chars per message


def _chunk_text(text: str, size: int = _MAX_CHUNK) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:size])
        text = text[size:]
    return chunks


class SlackTransportAdapter:
    """TransportAdapter for Slack DM via Socket Mode.

    Requires:
      bot_token  — xoxb- bot token (chat:write, im:history, im:read, im:write)
      app_token  — xapp- app-level token (connections:write scope)
      user_id    — Slack user ID of the authorized user (e.g. U12345678)
      router     — CommandRouter instance (from command_core)
    """

    def __init__(self, router, bot_token: str, app_token: str, user_id: str):
        self._router = router
        self._bot_token = bot_token
        self._app_token = app_token
        self._authorized_user_id = user_id
        self._client = SlackClient(token=bot_token)
        self._dm_channel_id: Optional[str] = None  # set on first message
        self._app = None
        self._handler = None

    # ── TransportAdapter protocol ─────────────────────────────────────────────

    async def start(self) -> None:
        """Start the Slack Socket Mode connection."""
        try:
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        except ImportError:
            log.error(
                "slack_bolt is not installed — Slack adapter disabled. "
                "Run: pip install slack_bolt"
            )
            return

        self._app = AsyncApp(token=self._bot_token)
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)

        # Register DM message handler
        @self._app.event("message")
        async def on_message(event, say):
            await self._on_message(event, say)

        log.info("Slack adapter starting Socket Mode connection")
        await self._handler.start_async()

    async def stop(self) -> None:
        """Close the Socket Mode WebSocket."""
        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception as e:
                log.debug("Slack adapter stop error: %s", e)

    async def send_text(self, chat_id: str, text: str) -> None:
        """Send text to a Slack channel/DM, chunking at 4000 chars."""
        for chunk in _chunk_text(text):
            ok = await self._client.post_message(chat_id, chunk)
            if not ok:
                log.warning("Slack send_text failed for channel %s", chat_id)

    async def send_typing(self, chat_id: str) -> None:
        """No-op — Slack DMs have no typing indicator API."""
        pass

    def max_message_length(self) -> int:
        return _MAX_CHUNK

    def get_chat_id(self) -> Optional[str]:
        """Return the DM channel ID discovered on first message, or None."""
        return self._dm_channel_id

    # ── Message handling ──────────────────────────────────────────────────────

    async def _on_message(self, event: dict, say) -> None:
        """Handle incoming DM events from Slack."""
        # Ignore bot messages and messages from other users
        if event.get("bot_id") or event.get("subtype"):
            return
        if event.get("user") != self._authorized_user_id:
            return

        channel = event.get("channel", "")
        text = event.get("text", "").strip()

        # Cache the DM channel ID for proactive notifications
        if self._dm_channel_id is None:
            self._dm_channel_id = channel
            log.info("Slack adapter: discovered DM channel %s", channel)

        async def reply(msg: str) -> None:
            await self.send_text(channel, msg)

        async def noop_typing() -> None:
            pass

        ctx = CommandContext(
            args=[],
            user_id=self._authorized_user_id,
            reply=reply,
            send_typing=noop_typing,
        )

        if text.startswith("!"):
            # !command args  →  dispatch_command
            parts = text[1:].split()
            if not parts:
                return
            command = parts[0].lower()
            ctx.args = parts[1:]
            handled = await self._router.dispatch_command(ctx, command)
            if not handled:
                await reply(
                    f"Unknown command: !{command}\n"
                    "Send !help for a list of commands."
                )
        else:
            # Free-text → LLM chat
            await self._router.handle_message(ctx, text)
