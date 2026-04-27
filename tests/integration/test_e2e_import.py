"""E2E smoke tests for Import commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_import_chats_smoke(handler, mk_update):
    # Note: This command expects a file attachment, but smoke test just checks handler runs
    update, ctx = mk_update("/import_chats")
    await handler.cmd_import_chats(update, ctx)
    update.message.reply_text.assert_called()
