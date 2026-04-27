"""E2E smoke tests for Circles commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_circles_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.circle(brain_dir)
    update, ctx = mk_update("/circles")
    await handler.cmd_circles(update, ctx)
    update.message.reply_text.assert_called()


async def test_circle_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.circle(brain_dir)
    # Populate list
    update, ctx = mk_update("/circles")
    await handler.cmd_circles(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/circle", args=["1"])
    await handler.cmd_circle(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_circle_status_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.circle(brain_dir)
    update, ctx = mk_update("/circle_status")
    await handler.cmd_circle_status(update, ctx)
    update.message.reply_text.assert_called()
