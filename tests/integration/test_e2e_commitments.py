"""E2E smoke tests for Commitments commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_commitments_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.commitment(brain_dir, status="active")
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    update.message.reply_text.assert_called()


async def test_complete_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.commitment(brain_dir, status="active")
    # Populate list
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    # Complete item 1
    update2, ctx2 = mk_update("/complete", args=["1"])
    await handler.cmd_complete(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_dismiss_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.commitment(brain_dir, status="active")
    # Populate list
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    # Dismiss item 1
    update2, ctx2 = mk_update("/dismiss", args=["1"])
    await handler.cmd_dismiss(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_wrong_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.commitment(brain_dir, status="active")
    # Populate list
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    # Mark item 1 as false positive
    update2, ctx2 = mk_update("/wrong", args=["1"])
    await handler.cmd_wrong(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_missed_smoke(handler, mk_update):
    update, ctx = mk_update("/missed", args=["Do", "the", "thing"])
    await handler.cmd_missed(update, ctx)
    update.message.reply_text.assert_called()


async def test_accuracy_smoke(handler, mk_update):
    update, ctx = mk_update("/accuracy")
    await handler.cmd_accuracy(update, ctx)
    update.message.reply_text.assert_called()


async def test_quota_smoke(handler, mk_update):
    update, ctx = mk_update("/quota")
    await handler.cmd_quota(update, ctx)
    update.message.reply_text.assert_called()


async def test_todos_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.commitment(brain_dir, status="active")
    update, ctx = mk_update("/todos")
    await handler.cmd_todos(update, ctx)
    update.message.reply_text.assert_called()
async def test_todo_smoke(handler, mk_update):
    update, ctx = mk_update("/todo", args=["Clean", "my", "desk"])
    await handler.cmd_todo(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Clean my desk" in reply
