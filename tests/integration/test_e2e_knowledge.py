"""E2E smoke tests for Knowledge listings commands."""
import pytest

pytestmark = pytest.mark.asyncio


async def test_readings_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.reading(brain_dir)
    update, ctx = mk_update("/readings")
    await handler.cmd_readings(update, ctx)
    update.message.reply_text.assert_called()


async def test_search_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.reading(brain_dir, title="Test Article")
    update, ctx = mk_update("/search", args=["test"])
    await handler.cmd_search(update, ctx)
    update.message.reply_text.assert_called()


async def test_reading_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.reading(brain_dir)
    # Populate the list first
    update, ctx = mk_update("/readings")
    await handler.cmd_readings(update, ctx)
    # Now access detail
    update2, ctx2 = mk_update("/reading", args=["1"])
    await handler.cmd_reading(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_forget_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.reading(brain_dir)
    # Populate list
    update, ctx = mk_update("/readings")
    await handler.cmd_readings(update, ctx)
    # Forget item 1
    update2, ctx2 = mk_update("/forget", args=["1"])
    await handler.cmd_forget(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_people_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.contact(brain_dir)
    update, ctx = mk_update("/people")
    await handler.cmd_contacts(update, ctx)
    update.message.reply_text.assert_called()


async def test_contacts_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.contact(brain_dir)
    update, ctx = mk_update("/contacts")
    await handler.cmd_contacts(update, ctx)
    update.message.reply_text.assert_called()


async def test_contact_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.contact(brain_dir, name="Alice")
    # Populate list
    update, ctx = mk_update("/contacts")
    await handler.cmd_contacts(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/contact", args=["1"])
    await handler.cmd_contact(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_code_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    # Code memories have type: code
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    (memories / "code-test-host-myrepo.md").write_text(
        "---\ntype: code\nsource_title: myrepo\nsummary: Repo\n"
        "tags: []\nhostname: test-host\nremote_url: git@github.com:user/repo.git\n"
        "---\n\n## Summary\nA repository\n"
    )
    update, ctx = mk_update("/code")
    await handler.cmd_code(update, ctx)
    update.message.reply_text.assert_called()


async def test_events_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.calendar_event(brain_dir)
    update, ctx = mk_update("/events")
    await handler.cmd_events(update, ctx)
    update.message.reply_text.assert_called()


async def test_event_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.calendar_event(brain_dir)
    # Populate list
    update, ctx = mk_update("/events")
    await handler.cmd_events(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/event", args=["1"])
    await handler.cmd_event(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_meetings_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.meeting(brain_dir)
    update, ctx = mk_update("/meetings")
    await handler.cmd_meetings(update, ctx)
    update.message.reply_text.assert_called()


async def test_meeting_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.meeting(brain_dir)
    # Populate list
    update, ctx = mk_update("/meetings")
    await handler.cmd_meetings(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/meeting", args=["1"])
    await handler.cmd_meeting(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_comms_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.email_thread(brain_dir)
    update, ctx = mk_update("/comms")
    await handler.cmd_comms(update, ctx)
    update.message.reply_text.assert_called()


async def test_comm_smoke(handler, mk_update, brain_dir):
    from tests.integration import seed
    seed.email_thread(brain_dir)
    # Populate list
    update, ctx = mk_update("/comms")
    await handler.cmd_comms(update, ctx)
    # Access detail
    update2, ctx2 = mk_update("/comm", args=["1"])
    await handler.cmd_comm(update2, ctx2)
    update2.message.reply_text.assert_called()


async def test_aichat_smoke(handler, mk_update, brain_dir):
    # Create an imported chat memory
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    (memories / "aichat-2026-04-27-test-abc123.md").write_text(
        "---\ntype: aichat\nsource_title: Claude Chat\nsummary: Chat\n"
        "tags: []\nplatform: claude\nimported_at: 2026-04-27T10:00:00\n"
        "---\n\n## Summary\nChat history\n"
    )
    update, ctx = mk_update("/aichat")
    await handler.cmd_aichat(update, ctx)
    update.message.reply_text.assert_called()


async def test_insights_smoke(handler, mk_update, brain_dir):
    # Create a synthesis insight memory
    memories = brain_dir / "memories"
    memories.mkdir(exist_ok=True)
    (memories / "insight-2026-04-27-test.md").write_text(
        "---\ntype: insight\nsource_title: Insight\nsummary: Synthesis\n"
        "tags: []\ngenerated_at: 2026-04-27T10:00:00\n---\n\n## Summary\nInsight\n"
    )
    update, ctx = mk_update("/insights")
    await handler.cmd_insights(update, ctx)
    update.message.reply_text.assert_called()
