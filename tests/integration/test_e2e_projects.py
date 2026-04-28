"""E2E smoke tests for Projects commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_addproject_smoke(handler, mk_update):
    update, ctx = mk_update("/addproject", args=["Build", "X"])
    await handler.cmd_addproject(update, ctx)
    update.message.reply_text.assert_called()


async def test_projects_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir)
    update, ctx = mk_update("/projects")
    await handler.cmd_projects(update, ctx)
    update.message.reply_text.assert_called()


async def test_project_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir)
    # Populate list
    update, ctx = mk_update("/projects")
    await handler.cmd_projects(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/project", args=["1"])
    await handler.cmd_project(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_completeproject_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir, status="active")
    # Populate list
    update, ctx = mk_update("/projects")
    await handler.cmd_projects(update, ctx)
    # Complete item 1
    update2, ctx2 = mk_update("/completeproject", args=["1"])
    await handler.cmd_completeproject(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_abandonproject_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir, status="active")
    # Populate list
    update, ctx = mk_update("/projects")
    await handler.cmd_projects(update, ctx)
    # Abandon item 1
    update2, ctx2 = mk_update("/abandonproject", args=["1"])
    await handler.cmd_abandonproject(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_holdproject_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir, status="active")
    # Populate list
    update, ctx = mk_update("/projects")
    await handler.cmd_projects(update, ctx)
    # Put on hold
    update2, ctx2 = mk_update("/holdproject", args=["1"])
    await handler.cmd_holdproject(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_addmilestone_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir)
    # Populate list
    update, ctx = mk_update("/projects")
    await handler.cmd_projects(update, ctx)
    # Add milestone
    update2, ctx2 = mk_update("/addmilestone", args=["1", "Complete", "design"])
    await handler.cmd_addmilestone(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_milestone_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir)
    # Populate list
    update, ctx = mk_update("/projects")
    await handler.cmd_projects(update, ctx)
    # Toggle milestone (requires milestone to exist first, but smoke test just checks handler runs)
    update2, ctx2 = mk_update("/milestone", args=["1", "1"])
    await handler.cmd_milestone(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_linkgoal_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir)
    seed.goal(brain_dir)
    # Populate both lists
    update_p, ctx_p = mk_update("/projects")
    await handler.cmd_projects(update_p, ctx_p)
    update_g, ctx_g = mk_update("/goals")
    await handler.cmd_goals(update_g, ctx_g)
    # Link project 1 to goal 1
    update2, ctx2 = mk_update("/linkgoal", args=["1", "1"])
    await handler.cmd_linkgoal(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_unlinkgoal_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.project(brain_dir)
    # Populate list
    update, ctx = mk_update("/projects")
    await handler.cmd_projects(update, ctx)
    # Unlink
    update2, ctx2 = mk_update("/unlinkgoal", args=["1"])
    await handler.cmd_unlinkgoal(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_changes_smoke(handler, mk_update):
    """Smoke: /changes runs without error even when there are no active projects."""
    from unittest.mock import AsyncMock, MagicMock, patch
    mock_agent = MagicMock()
    mock_agent.generate_change_digest = AsyncMock(return_value=[])
    update, ctx = mk_update("/changes")
    with patch("goal_project_agent.GoalProjectAgent", return_value=mock_agent):
        await handler.cmd_changes(update, ctx)
    update.message.reply_text.assert_called()
