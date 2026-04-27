"""E2E smoke tests for Watchlists commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_watch_smoke(handler, mk_update):
    update, ctx = mk_update("/watch", args=["rust", "async"])
    await handler.cmd_watch(update, ctx)
    update.message.reply_text.assert_called()


async def test_watches_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.watchlist(brain_dir)
    update, ctx = mk_update("/watches")
    await handler.cmd_watches(update, ctx)
    update.message.reply_text.assert_called()


async def test_unwatch_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.watchlist(brain_dir)
    # Populate list
    update, ctx = mk_update("/watches")
    await handler.cmd_watches(update, ctx)
    # Deactivate item 1
    update2, ctx2 = mk_update("/unwatch", args=["1"])
    await handler.cmd_unwatch(update2, ctx2)
    update2.message.reply_text.assert_called()
