"""E2E smoke tests for Deduplication commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_dupes_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.dedup_pair(brain_dir)
    update, ctx = mk_update("/dupes")
    await handler.cmd_dupes(update, ctx)
    update.message.reply_text.assert_called()


async def test_merge_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.dedup_pair(brain_dir)
    # Populate list
    update, ctx = mk_update("/dupes")
    await handler.cmd_dupes(update, ctx)
    # Merge pair 1
    update2, ctx2 = mk_update("/merge", args=["1"])
    await handler.cmd_merge(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_keep_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.dedup_pair(brain_dir)
    # Populate list
    update, ctx = mk_update("/dupes")
    await handler.cmd_dupes(update, ctx)
    # Keep pair 1 as distinct
    update2, ctx2 = mk_update("/keep", args=["1"])
    await handler.cmd_keep(update2, ctx2)
    update2.message.reply_text.assert_called()
