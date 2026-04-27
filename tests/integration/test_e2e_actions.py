"""E2E smoke tests for Agent actions commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_actions_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.action(brain_dir)
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    update.message.reply_text.assert_called()


async def test_action_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.action(brain_dir)
    # Populate list
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/action", args=["1"])
    await handler.cmd_action(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_run_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.action(brain_dir, status="pending")
    # Populate list
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    # Run item 1
    update2, ctx2 = mk_update("/run", args=["1"])
    await handler.cmd_run(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_drop_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.action(brain_dir, status="pending")
    # Populate list
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    # Drop item 1
    update2, ctx2 = mk_update("/drop", args=["1"])
    await handler.cmd_drop(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_defer_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.action(brain_dir, status="pending")
    # Populate list
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    # Defer item 1 for 24 hours
    update2, ctx2 = mk_update("/defer", args=["1", "24"])
    await handler.cmd_defer(update2, ctx2)
    update2.message.reply_text.assert_called()
