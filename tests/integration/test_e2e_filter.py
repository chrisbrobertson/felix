"""E2E smoke tests for Domain filter commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_skip_smoke(handler, mk_update):
    update, ctx = mk_update("/skip", args=["example.com"])
    await handler.cmd_skip(update, ctx)
    update.message.reply_text.assert_called()


async def test_unskip_smoke(handler, mk_update):
    update, ctx = mk_update("/unskip", args=["example.com"])
    await handler.cmd_unskip(update, ctx)
    update.message.reply_text.assert_called()


async def test_skiplist_smoke(handler, mk_update):
    update, ctx = mk_update("/skiplist")
    await handler.cmd_skiplist(update, ctx)
    update.message.reply_text.assert_called()
