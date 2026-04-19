"""Unit tests for slack_adapter.SlackTransportAdapter._on_message routing."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slack_adapter import SlackTransportAdapter
from command_core import CommandRouter


@pytest.fixture
def router():
    r = CommandRouter()
    return r


@pytest.fixture
def adapter(router):
    a = SlackTransportAdapter(
        router=router,
        bot_token="xoxb-test",
        app_token="xapp-test",
        user_id="U001",
    )
    a._client = MagicMock()
    a._client.post_message = AsyncMock(return_value=True)
    return a


@pytest.mark.asyncio
async def test_unauthorized_user_ignored(adapter):
    """Message from a non-authorized user → no reply sent."""
    event = {"user": "U999", "channel": "D001", "text": "hello"}
    await adapter._on_message(event, say=None)
    adapter._client.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_message_ignored(adapter):
    """bot_id present → ignored."""
    event = {"user": "U001", "bot_id": "B001", "channel": "D001", "text": "hello"}
    await adapter._on_message(event, say=None)
    adapter._client.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_dispatch(adapter, router):
    """!help dispatches to registered handler."""
    replies = []
    async def help_handler(ctx):
        await ctx.reply("Help text")
    router.register("help", help_handler)

    event = {"user": "U001", "channel": "D001", "text": "!help"}
    await adapter._on_message(event, say=None)
    adapter._client.post_message.assert_awaited_once_with("D001", "Help text")


@pytest.mark.asyncio
async def test_command_with_args(adapter, router):
    """!search foo bar → ctx.args == ['foo', 'bar']."""
    received_args = []
    async def search_handler(ctx):
        received_args.extend(ctx.args)
        await ctx.reply("results")
    router.register("search", search_handler)

    event = {"user": "U001", "channel": "D001", "text": "!search foo bar"}
    await adapter._on_message(event, say=None)
    assert received_args == ["foo", "bar"]


@pytest.mark.asyncio
async def test_unknown_command_replies_hint(adapter):
    """Unknown !command → 'Unknown command' reply with hint."""
    event = {"user": "U001", "channel": "D001", "text": "!notacommand"}
    await adapter._on_message(event, say=None)
    call_args = adapter._client.post_message.call_args
    assert "Unknown command" in call_args[0][1] or "Unknown command" in str(call_args)


@pytest.mark.asyncio
async def test_dm_channel_id_cached(adapter):
    """First message caches the DM channel ID for proactive notifications."""
    assert adapter._dm_channel_id is None
    async def noop(ctx): await ctx.reply("ok")
    adapter._router.register("hi", noop)
    event = {"user": "U001", "channel": "D001", "text": "!hi"}
    await adapter._on_message(event, say=None)
    assert adapter._dm_channel_id == "D001"


@pytest.mark.asyncio
async def test_free_text_calls_handle_message(adapter, router):
    """Non-! message → router.handle_message called."""
    called = []
    original = router.handle_message
    async def capture(ctx, text):
        called.append(text)
    router.handle_message = capture

    event = {"user": "U001", "channel": "D001", "text": "what did I work on yesterday?"}
    await adapter._on_message(event, say=None)
    assert called == ["what did I work on yesterday?"]


@pytest.mark.asyncio
async def test_send_text_chunks_long_message(adapter):
    """Messages over 4000 chars are split into multiple post_message calls."""
    long_text = "x" * 8001
    await adapter.send_text("D001", long_text)
    assert adapter._client.post_message.await_count == 3  # 4000 + 4000 + 1


@pytest.mark.asyncio
async def test_max_message_length(adapter):
    assert adapter.max_message_length() == 4000
