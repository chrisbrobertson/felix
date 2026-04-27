"""E2E smoke tests for Reports commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_reports_smoke(handler, mk_update, deploy_dir):
    from tests.integration import seed
    seed.report_config(deploy_dir)
    update, ctx = mk_update("/reports")
    await handler.cmd_reports(update, ctx)
    update.message.reply_text.assert_called()


async def test_report_smoke(handler, mk_update, deploy_dir):
    from tests.integration import seed
    seed.report_config(deploy_dir)
    # Populate list
    update, ctx = mk_update("/reports")
    await handler.cmd_reports(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/report", args=["1"])
    await handler.cmd_report(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_report_add_smoke(handler, mk_update):
    update, ctx = mk_update("/report_add", args=["weekly", "digest", "email", "meetings"])
    await handler.cmd_report_add(update, ctx)
    update.message.reply_text.assert_called()


async def test_report_remove_smoke(handler, mk_update, deploy_dir):
    from tests.integration import seed
    seed.report_config(deploy_dir)
    # Populate list
    update, ctx = mk_update("/reports")
    await handler.cmd_reports(update, ctx)
    # Remove item 1
    update2, ctx2 = mk_update("/report_remove", args=["1"])
    await handler.cmd_report_remove(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_report_pause_smoke(handler, mk_update, deploy_dir):
    from tests.integration import seed
    seed.report_config(deploy_dir)
    # Populate list
    update, ctx = mk_update("/reports")
    await handler.cmd_reports(update, ctx)
    # Pause item 1
    update2, ctx2 = mk_update("/report_pause", args=["1"])
    await handler.cmd_report_pause(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_report_resume_smoke(handler, mk_update, deploy_dir):
    from tests.integration import seed
    seed.report_config(deploy_dir)
    # Populate list
    update, ctx = mk_update("/reports")
    await handler.cmd_reports(update, ctx)
    # Resume item 1
    update2, ctx2 = mk_update("/report_resume", args=["1"])
    await handler.cmd_report_resume(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_report_run_smoke(handler, mk_update, deploy_dir):
    from tests.integration import seed
    seed.report_config(deploy_dir)
    # Populate list
    update, ctx = mk_update("/reports")
    await handler.cmd_reports(update, ctx)
    # Run item 1
    update2, ctx2 = mk_update("/report_run", args=["1"])
    await handler.cmd_report_run(update2, ctx2)
    update2.message.reply_text.assert_called()
