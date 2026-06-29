"""E2E smoke tests for Feature Requests commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_feature_smoke(handler, mk_update):
    update, ctx = mk_update("/feature", args=["Add", "cool", "feature"])
    await handler.cmd_feature(update, ctx)
    update.message.reply_text.assert_called()


async def test_feature_new_smoke(handler, mk_update):
    # Alias of /feature
    update, ctx = mk_update("/feature_new", args=["Another", "feature"])
    await handler.cmd_feature(update, ctx)
    update.message.reply_text.assert_called()


async def test_bug_smoke(handler, mk_update):
    update, ctx = mk_update("/bug", args=["Fix", "the", "bug"])
    await handler.cmd_bug(update, ctx)
    update.message.reply_text.assert_called()


async def test_bugs_smoke(handler, mk_update, brain_dir):
    # Alias of /features bug
    from tests.integration import seed
    seed.feature_request(brain_dir, kind="bug")
    update, ctx = mk_update("/bugs")
    await handler.cmd_bugs(update, ctx)
    update.message.reply_text.assert_called()


async def test_features_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.feature_request(brain_dir)
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    update.message.reply_text.assert_called()


async def test_feature_detail_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.feature_request(brain_dir)
    # Populate list
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/feature_detail", args=["1"])
    await handler.cmd_feature_detail(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_fdetail_smoke(handler, mk_update, brain_dir):
    # Alias of /feature_detail
    from tests.integration import seed
    seed.feature_request(brain_dir)
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    update2, ctx2 = mk_update("/fdetail", args=["1"])
    await handler.cmd_feature_detail(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_feature_priority_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.feature_request(brain_dir)
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    update2, ctx2 = mk_update("/feature_priority", args=["1", "high"])
    await handler.cmd_feature_priority(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_feature_plan_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.feature_request(brain_dir)
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    update2, ctx2 = mk_update("/feature_plan", args=["1"])
    await handler.cmd_feature_plan(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_feature_start_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.feature_request(brain_dir)
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    update2, ctx2 = mk_update("/feature_start", args=["1"])
    await handler.cmd_feature_start(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_feature_done_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.feature_request(brain_dir)
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    update2, ctx2 = mk_update("/feature_done", args=["1"])
    await handler.cmd_feature_done(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_feature_wont_do_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.feature_request(brain_dir)
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    update2, ctx2 = mk_update("/feature_wont_do", args=["1"])
    await handler.cmd_feature_wont_do(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_feature_note_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.feature_request(brain_dir)
    update, ctx = mk_update("/features")
    await handler.cmd_features(update, ctx)
    update2, ctx2 = mk_update("/feature_note", args=["1", "Some", "note"])
    await handler.cmd_feature_note(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_feature_import_smoke(handler, mk_update):
    update, ctx = mk_update("/feature_import")
    await handler.cmd_feature_import(update, ctx)
    update.message.reply_text.assert_called()


async def test_prs_smoke(handler, mk_update):
    """Without GitHub configured, /prs replies with a 'not configured' message."""
    update, ctx = mk_update("/prs")
    await handler.cmd_prs(update, ctx)
    update.message.reply_text.assert_called()
    reply = update.message.reply_text.call_args[0][0]
    assert "GitHub not configured" in reply
