"""E2E smoke tests for Agent actions commands."""
import pytest
from unittest.mock import AsyncMock, patch

pytestmark = pytest.mark.asyncio


async def test_actions_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.action(brain_dir)
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    update.message.reply_text.assert_called()


async def test_actions_lists_pending_action(handler, mk_update, brain_dir):
    """After seeding an agent_action, /actions should list it by title."""
    from tests.integration import seed
    seed.action(brain_dir, title="Send followup email")
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert "Send followup email" in reply


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


async def test_action_detail_contains_rationale(handler, mk_update, brain_dir):
    """After /actions, /action 1 detail should include the rationale."""
    from tests.integration import seed
    seed.action(brain_dir, title="Reach out")
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    update2, ctx2 = mk_update("/action", args=["1"])
    await handler.cmd_action(update2, ctx2)
    reply = update2.message.reply_text.call_args[0][0]
    assert "Reach out" in reply or "Test rationale" in reply


async def test_get_action_tool_returns_detail(handler, brain_dir):
    """get_action LLM tool returns full detail after list_actions populates the set."""
    from tests.integration import seed
    seed.action(brain_dir, title="Schedule review")
    # Populate _last_action_set via the list helper
    await handler._list_actions_text()
    result = handler._get_action_text(1)
    assert "Schedule review" in result
    assert "Test rationale" in result


async def test_get_action_tool_empty_list(handler):
    """get_action returns helpful message when no actions have been listed."""
    handler._last_action_set = []
    result = handler._get_action_text(1)
    assert "list_actions" in result.lower() or "no actions" in result.lower()


async def test_run_smoke(handler, mk_update, brain_dir, deploy_dir):
    from tests.integration import seed
    seed.action(brain_dir, status="pending")
    # Populate list
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    # Run item 1
    update2, ctx2 = mk_update("/run", args=["1"])
    await handler.cmd_run(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_drop_smoke(handler, mk_update, brain_dir, deploy_dir):
    from tests.integration import seed
    seed.action(brain_dir, status="pending")
    # Populate list
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    # Drop item 1
    update2, ctx2 = mk_update("/drop", args=["1"])
    await handler.cmd_drop(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_drop_tool_rejects_action(handler, brain_dir, deploy_dir):
    """drop_action tool marks the action as rejected in the file."""
    from tests.integration import seed
    path = seed.action(brain_dir, title="Drop me", status="pending")
    await handler._list_actions_text()
    result = await handler._drop_action_text(1)
    assert "rejected" in result.lower()
    content = path.read_text()
    assert "status: rejected" in content


async def test_defer_tool_sets_defer_until(handler, brain_dir, deploy_dir):
    """defer_action tool writes a defer_until timestamp to the action file."""
    from tests.integration import seed
    path = seed.action(brain_dir, title="Snooze me", status="pending")
    await handler._list_actions_text()
    result = await handler._defer_action_text(1, hours=48)
    assert "48h" in result or "snoozed" in result.lower()
    content = path.read_text()
    assert "defer_until:" in content


async def test_defer_smoke(handler, mk_update, brain_dir, deploy_dir):
    from tests.integration import seed
    seed.action(brain_dir, status="pending")
    # Populate list
    update, ctx = mk_update("/actions")
    await handler.cmd_actions(update, ctx)
    # Defer item 1 for 24 hours
    update2, ctx2 = mk_update("/defer", args=["1", "24"])
    await handler.cmd_defer(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_run_tool_empty_list(handler):
    """run_action returns helpful message when no actions have been listed."""
    handler._last_action_set = []
    result = await handler._run_action_text(1)
    assert "list_actions" in result.lower() or "no actions" in result.lower()


async def test_drop_tool_empty_list(handler):
    """drop_action returns helpful message when no actions have been listed."""
    handler._last_action_set = []
    result = await handler._drop_action_text(1)
    assert "list_actions" in result.lower() or "no actions" in result.lower()


async def test_defer_tool_empty_list(handler):
    """defer_action returns helpful message when no actions have been listed."""
    handler._last_action_set = []
    result = await handler._defer_action_text(1)
    assert "list_actions" in result.lower() or "no actions" in result.lower()


async def test_defer_tool_invalid_hours(handler, brain_dir, deploy_dir):
    """defer_action with hours <= 0 returns an error."""
    from tests.integration import seed
    seed.action(brain_dir, status="pending")
    await handler._list_actions_text()
    result = await handler._defer_action_text(1, hours=0)
    assert "positive" in result.lower() or "error" in result.lower() or "hours" in result.lower()
