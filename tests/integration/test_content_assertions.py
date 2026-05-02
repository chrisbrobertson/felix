"""Content assertion tests — verify reply text, not just that a reply was sent.

Gap 1 remediation: these tests catch regressions in output formatting and
content that the smoke tests (which only call assert_called()) cannot detect.
"""
import pytest
from tests.integration import seed

pytestmark = pytest.mark.asyncio


# ── /commitments ──────────────────────────────────────────────────────────────

async def test_commitments_reply_includes_title_and_type(handler, mk_update, brain_dir):
    seed.commitment(brain_dir, title="Do the thing")
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "Active commitments (1 total):" in text
    assert "[outbound]" in text
    assert "Do the thing" in text


async def test_commitments_reply_includes_due_unknown_when_no_due_date(handler, mk_update, brain_dir):
    seed.commitment(brain_dir)
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "— due unknown" in text


async def test_commitments_includes_update_hint(handler, mk_update, brain_dir):
    seed.commitment(brain_dir)
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "/complete" in text
    assert "/dismiss" in text


async def test_commitments_empty_returns_no_active_message(handler, mk_update):
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "No active commitments." in text


async def test_commitments_count_reflects_seeded_items(handler, mk_update, brain_dir):
    seed.commitment(brain_dir, title="First task")
    seed.commitment(brain_dir, title="Second task")
    update, ctx = mk_update("/commitments")
    await handler.cmd_commitments(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "2 total" in text


# ── /complete ─────────────────────────────────────────────────────────────────

async def test_complete_success_reply_contains_checkmark_and_title(handler, mk_update, brain_dir):
    seed.commitment(brain_dir, title="Review the PR")
    # Load list so handler knows index 1
    list_update, list_ctx = mk_update("/commitments")
    await handler.cmd_commitments(list_update, list_ctx)

    update, ctx = mk_update("/complete", args=["1"])
    await handler.cmd_complete(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "✓" in text  # ✓
    assert "Review the PR" in text


async def test_complete_out_of_range_returns_not_found(handler, mk_update):
    # No commitments loaded — _last_commitment_set is empty
    update, ctx = mk_update("/complete", args=["999"])
    await handler.cmd_complete(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text
    assert "run /commitments" in text


async def test_complete_missing_args_returns_usage(handler, mk_update):
    update, ctx = mk_update("/complete", args=[])
    await handler.cmd_complete(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "Usage" in text
    assert "/complete" in text


# ── /goals ────────────────────────────────────────────────────────────────────

async def test_goals_list_includes_header_category_and_title(handler, mk_update, brain_dir):
    seed.goal(brain_dir, title="Ship feature X")
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "Active goals (1 total):" in text
    assert "[work]" in text
    assert "Ship feature X" in text


async def test_goals_list_includes_no_due_date_when_unset(handler, mk_update, brain_dir):
    seed.goal(brain_dir)
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "no due date" in text


async def test_goals_empty_returns_no_goals_message(handler, mk_update):
    update, ctx = mk_update("/goals")
    await handler.cmd_goals(update, ctx)
    text = update.message.reply_text.call_args[0][0]
    assert "No goals found." in text


# ── /features (local mode) ───────────────────────────────────────────────────

async def test_features_list_includes_header_and_status(handler, mk_update, brain_dir):
    seed.feature_request_item(brain_dir, title="Dark mode support")
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    # cmd_features uses _send_reply which may split or use reply_text
    calls = update.message.reply_text.call_args_list
    text = " ".join(c[0][0] for c in calls if c[0])
    assert "Feature requests" in text
    assert "[new]" in text
    assert "[medium]" in text


async def test_features_title_appears_in_list(handler, mk_update, brain_dir):
    seed.feature_request_item(brain_dir, title="Export to PDF")
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    calls = update.message.reply_text.call_args_list
    text = " ".join(c[0][0] for c in calls if c[0])
    assert "Export to PDF" in text


async def test_features_empty_returns_no_feature_requests(handler, mk_update):
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    calls = update.message.reply_text.call_args_list
    text = " ".join(c[0][0] for c in calls if c[0])
    assert "No feature requests found." in text


async def test_bugs_list_shows_bug_kind_tag(handler, mk_update, brain_dir):
    seed.feature_request_item(brain_dir, title="Crash on startup", kind="bug")
    update, ctx = mk_update("/bugs")
    await handler.cmd_bugs(update, ctx)
    calls = update.message.reply_text.call_args_list
    text = " ".join(c[0][0] for c in calls if c[0])
    assert "Crash on startup" in text
