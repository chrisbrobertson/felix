"""E2E smoke tests for Meta and System commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_help_smoke(handler, mk_update):
    update, ctx = mk_update("/help")
    await handler.cmd_help(update, ctx)
    update.message.reply_text.assert_called()


async def test_commands_smoke(handler, mk_update):
    update, ctx = mk_update("/commands")
    await handler.cmd_help(update, ctx)
    update.message.reply_text.assert_called()


async def test_settings_smoke(handler, mk_update):
    update, ctx = mk_update("/settings")
    await handler.cmd_settings(update, ctx)
    update.message.reply_text.assert_called()


async def test_reset_smoke(handler, mk_update):
    update, ctx = mk_update("/reset")
    await handler.cmd_reset(update, ctx)
    update.message.reply_text.assert_called()


async def test_deliver_smoke(handler, mk_update):
    update, ctx = mk_update("/deliver")
    await handler.cmd_deliver(update, ctx)
    update.message.reply_text.assert_called()


async def test_discard_smoke(handler, mk_update):
    update, ctx = mk_update("/discard")
    await handler.cmd_discard(update, ctx)
    update.message.reply_text.assert_called()


async def test_version_smoke(handler, mk_update):
    update, ctx = mk_update("/version")
    await handler.cmd_version(update, ctx)
    update.message.reply_text.assert_called()


async def test_backfill_smoke(handler, mk_update):
    update, ctx = mk_update("/backfill", args=["readings", "7"])
    await handler.cmd_backfill(update, ctx)
    update.message.reply_text.assert_called()


async def test_remember_smoke(handler, mk_update):
    update, ctx = mk_update("/remember", args=["https://example.com"])
    await handler.cmd_remember(update, ctx)
    update.message.reply_text.assert_called()


async def test_deepen_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.reading(brain_dir)
    update, ctx = mk_update("/readings")
    await handler.cmd_readings(update, ctx)
    update2, ctx2 = mk_update("/deepen", args=["1"])
    await handler.cmd_deepen(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_note_smoke(handler, mk_update):
    update, ctx = mk_update("/note", args=["https://example.com/article"])
    await handler.cmd_note(update, ctx)
    update.message.reply_text.assert_called()


async def test_rebuild_cache_smoke(handler, mk_update):
    update, ctx = mk_update("/rebuild_cache")
    await handler.cmd_rebuild_cache(update, ctx)
    update.message.reply_text.assert_called()
