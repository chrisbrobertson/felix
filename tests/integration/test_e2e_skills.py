"""E2E smoke tests for Skill Management commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_skill_drafts_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.skill_draft(brain_dir)
    update, ctx = mk_update("/skill_drafts")
    await handler.cmd_skill_drafts(update, ctx)
    update.message.reply_text.assert_called()


async def test_skill_draft_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.skill_draft(brain_dir)
    # Populate list
    update, ctx = mk_update("/skill_drafts")
    await handler.cmd_skill_drafts(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/skill_draft", args=["1"])
    await handler.cmd_skill_draft(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_approve_skill_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.skill_draft(brain_dir)
    # Populate list
    update, ctx = mk_update("/skill_drafts")
    await handler.cmd_skill_drafts(update, ctx)
    # Approve item 1
    update2, ctx2 = mk_update("/approve_skill", args=["1"])
    await handler.cmd_approve_skill(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_reject_skill_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.skill_draft(brain_dir)
    # Populate list
    update, ctx = mk_update("/skill_drafts")
    await handler.cmd_skill_drafts(update, ctx)
    # Reject item 1
    update2, ctx2 = mk_update("/reject_skill", args=["1"])
    await handler.cmd_reject_skill(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_skill_approval_smoke(handler, mk_update):
    update, ctx = mk_update("/skill_approval", args=["status"])
    await handler.cmd_skill_approval(update, ctx)
    update.message.reply_text.assert_called()


async def test_skill_health_smoke(handler, mk_update):
    update, ctx = mk_update("/skill_health")
    await handler.cmd_skill_health(update, ctx)
    update.message.reply_text.assert_called()
