"""E2E smoke tests for Goals commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_addgoal_smoke(handler, mk_update):
    update, ctx = mk_update("/addgoal", args=["Ship", "feature", "X"])
    await handler.cmd_addgoal(update, ctx)
    update.message.reply_text.assert_called()


async def test_goals_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.goal(brain_dir)
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    update.message.reply_text.assert_called()


async def test_goal_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.goal(brain_dir)
    # Populate list
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/goal", args=["1"])
    await handler.cmd_goal(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_completegoal_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.goal(brain_dir, status="active")
    # Populate list
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    # Complete item 1
    update2, ctx2 = mk_update("/completegoal", args=["1"])
    await handler.cmd_completegoal(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_abandongoal_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.goal(brain_dir, status="active")
    # Populate list
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    # Abandon item 1
    update2, ctx2 = mk_update("/abandongoal", args=["1"])
    await handler.cmd_abandongoal(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_goal_note_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.goal(brain_dir)
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    update2, ctx2 = mk_update("/goal_note", args=["1", "Progress", "update"])
    await handler.cmd_goal_note(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_goal_due_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.goal(brain_dir)
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    update2, ctx2 = mk_update("/goal_due", args=["1", "2027-01-01"])
    await handler.cmd_goal_due(update2, ctx2)
    update2.message.reply_text.assert_called()
