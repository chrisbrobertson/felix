"""E2E smoke tests for Review commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_pending_smoke(handler, mk_update, brain_dir):
    update, ctx = mk_update("/pending")
    await handler.cmd_pending(update, ctx)
    update.message.reply_text.assert_called()


async def test_review_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.candidate(brain_dir)
    update, ctx = mk_update("/review")
    await handler.cmd_review(update, ctx)
    update.message.reply_text.assert_called()


async def test_confirm_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.candidate(brain_dir)
    # Populate list
    update, ctx = mk_update("/review")
    await handler.cmd_review(update, ctx)
    # Confirm item 1
    update2, ctx2 = mk_update("/confirm", args=["1"])
    await handler.cmd_confirm(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_reject_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.candidate(brain_dir)
    # Populate list
    update, ctx = mk_update("/review")
    await handler.cmd_review(update, ctx)
    # Reject item 1
    update2, ctx2 = mk_update("/reject", args=["1"])
    await handler.cmd_reject(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_review_purge_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.candidate(brain_dir)
    update, ctx = mk_update("/review_purge", args=["30"])
    await handler.cmd_review_purge(update, ctx)
    update.message.reply_text.assert_called()


async def test_edit_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.candidate(brain_dir)
    # Populate list
    update, ctx = mk_update("/review")
    await handler.cmd_review(update, ctx)
    # Edit item 1
    update2, ctx2 = mk_update("/edit", args=["1", "category=personal"])
    await handler.cmd_edit(update2, ctx2)
    update2.message.reply_text.assert_called()
