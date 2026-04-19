"""Unit tests for transport.CommandContext dataclass."""
import asyncio
import pytest

from transport import CommandContext


async def _noop_typing():
    pass


@pytest.mark.asyncio
async def test_command_context_required_fields():
    replies = []

    async def reply(text):
        replies.append(text)

    ctx = CommandContext(args=["a", "b"], user_id="U1", reply=reply, send_typing=_noop_typing)
    assert ctx.args == ["a", "b"]
    assert ctx.user_id == "U1"


def test_command_context_raw_text_defaults_to_empty_string():
    ctx = CommandContext(
        args=[],
        user_id="U1",
        reply=AsyncMock_stub,
        send_typing=_noop_typing,
    )
    assert ctx.raw_text == ""


def test_command_context_raw_text_can_be_set():
    ctx = CommandContext(
        args=[],
        user_id="U1",
        reply=AsyncMock_stub,
        send_typing=_noop_typing,
        raw_text="hello world",
    )
    assert ctx.raw_text == "hello world"


@pytest.mark.asyncio
async def test_command_context_reply_is_callable():
    calls = []

    async def reply(text):
        calls.append(text)

    ctx = CommandContext(args=[], user_id="U1", reply=reply, send_typing=_noop_typing)
    await ctx.reply("hi")
    assert calls == ["hi"]


@pytest.mark.asyncio
async def test_command_context_send_typing_is_callable():
    called = []

    async def typing():
        called.append(True)

    ctx = CommandContext(args=[], user_id="U1", reply=AsyncMock_stub, send_typing=typing)
    await ctx.send_typing()
    assert called


# Minimal async stub (avoids importing unittest.mock at module level)
async def AsyncMock_stub(text=""):
    pass
