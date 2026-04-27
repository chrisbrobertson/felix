"""E2E smoke tests for Notifications commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_briefing_smoke(handler, mk_update):
    update, ctx = mk_update("/briefing")
    await handler.cmd_briefing(update, ctx)
    update.message.reply_text.assert_called()


async def test_mute_smoke(handler, mk_update):
    update, ctx = mk_update("/mute")
    await handler.cmd_mute(update, ctx)
    update.message.reply_text.assert_called()


async def test_unmute_smoke(handler, mk_update):
    update, ctx = mk_update("/unmute")
    await handler.cmd_unmute(update, ctx)
    update.message.reply_text.assert_called()
