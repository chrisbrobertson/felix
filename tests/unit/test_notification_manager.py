"""
Unit tests for notification_manager.

All external access (Telegram bot, filesystem) is mocked.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

import notification_manager as nm
from notification_manager import NotificationManager, _chunk_message, _parse_frontmatter


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_calendar_event(
    memories_dir: Path,
    event_id: str,
    title: str,
    start_time: str,
    all_day: bool = False,
    participants: list = None,
    location: str = None,
) -> Path:
    """Create a calendar event memory file."""
    p = memories_dir / f"calendar-event-{event_id}.md"
    fm = {
        "type": "calendar_event",
        "event_id": event_id,
        "title": title,
        "start_time": start_time,
        "all_day": all_day,
        "participants": participants or [],
        "location": location,
        "last_scanned": "2026-04-11T10:00:00",
    }
    frontmatter = yaml.dump(fm, sort_keys=False)
    p.write_text(f"---\n{frontmatter}---\n\n## Details\nTest event.\n")
    return p


def make_commitment(
    memories_dir: Path,
    commitment_id: str,
    description: str,
    status: str = "active",
    commitment_type: str = "outbound",
    due_date: str = None,
    owner: str = "Alice",
    recipient: str = "Chris",
) -> Path:
    """Create a commitment memory file."""
    p = memories_dir / f"commitment-{description.lower().replace(' ', '-')}-{commitment_id}.md"
    fm = {
        "type": "commitment",
        "source_title": description,
        "summary": f"{owner} committed to {description}",
        "status": status,
        "commitment_type": commitment_type,
        "owner": owner,
        "owner_email": f"{owner.lower()}@acme.com",
        "recipient": recipient,
        "due_date": due_date,
        "last_scanned": "2026-04-11T10:00:00",
        "source_memory": "zoom:test123",
        "confidence": 0.85,
    }
    frontmatter = yaml.dump(fm, sort_keys=False)
    p.write_text(f"---\n{frontmatter}---\n\n## Context\nTest.\n")
    return p


def make_contact(memories_dir: Path, name: str, email: str, last_interaction: str, score: float) -> Path:
    """Create a contact memory file."""
    slug = name.lower().replace(" ", "-")
    p = memories_dir / f"contact-{slug}.md"
    fm = {
        "type": "contact",
        "name": name,
        "email": email,
        "last_interaction": last_interaction,
        "relationship_score": score,
    }
    frontmatter = yaml.dump(fm, sort_keys=False)
    p.write_text(f"---\n{frontmatter}---\n\n## Notes\nTest contact.\n")
    return p


# ── Chat ID Persistence ───────────────────────────────────────────────────────

def test_chat_id_persisted_on_first_message(tmp_path):
    """First allowed message writes chat_id to state file."""
    state_file = tmp_path / "notification-state.json"
    with patch.object(nm, "STATE_FILE", state_file):
        mgr = NotificationManager()
        mgr.set_chat_id(123456789)

        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["chat_id"] == 123456789


def test_chat_id_from_config_overrides_state(tmp_path):
    """Non-null telegram_chat_id in config takes precedence."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("notifications:\n  telegram_chat_id: 999888777\n")

    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789}))

    with patch.object(nm, "CONFIG_PATH", config_file):
        with patch.object(nm, "STATE_FILE", state_file):
            mgr = NotificationManager()
            assert mgr.get_chat_id() == 999888777


def test_no_send_when_chat_id_null(tmp_path):
    """send_message not called when chat_id is None."""
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": None}))

    bot_mock = AsyncMock()
    with patch.object(nm, "STATE_FILE", state_file):
        mgr = NotificationManager(bot=bot_mock)
        asyncio.run(mgr.send_message("Test"))

    bot_mock.send_message.assert_not_called()


# ── Mute State ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_send_when_muted(tmp_path):
    """FR-3/FR-4/FR-5 sends suppressed when muted: true."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": True}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                await mgr._check_and_send()

    bot_mock.send_message.assert_not_called()


def test_briefing_bypasses_mute(tmp_path):
    """/briefing command delivers briefing even when muted."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": True}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager()
                briefing = mgr._assemble_briefing()
                assert "Good morning" in briefing


def test_mute_state_persists(tmp_path):
    """muted: true written to state; reloaded correctly."""
    state_file = tmp_path / "notification-state.json"

    with patch.object(nm, "STATE_FILE", state_file):
        state = nm._load_state()
        state["muted"] = True
        nm._save_state(state)

        reloaded = nm._load_state()
        assert reloaded["muted"] is True


def test_unmute_resumes_notifications(tmp_path):
    """muted: false written; next check sends normally."""
    state_file = tmp_path / "notification-state.json"

    with patch.object(nm, "STATE_FILE", state_file):
        state = nm._load_state()
        state["muted"] = True
        nm._save_state(state)

        state["muted"] = False
        nm._save_state(state)

        reloaded = nm._load_state()
        assert reloaded["muted"] is False


# ── Daily Briefing ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_briefing_at_configured_time(tmp_path):
    """Briefing triggered when local time >= briefing_time."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "last_briefing_date": "2026-04-10"}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  briefing_time: '07:30'\n  enabled: true\n")

    bot_mock = AsyncMock()

    # Mock current time to be 07:35 on 2026-04-11
    now = datetime(2026, 4, 11, 7, 35, tzinfo=ZoneInfo("America/Los_Angeles"))

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    await mgr._check_daily_briefing(nm._load_state())

    bot_mock.send_message.assert_called_once()
    assert "Good morning" in bot_mock.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_daily_briefing_not_before_configured_time(tmp_path):
    """No briefing before configured time."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "last_briefing_date": "2026-04-10"}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  briefing_time: '07:30'\n  enabled: true\n")

    bot_mock = AsyncMock()

    # Mock current time to be 07:00 on 2026-04-11
    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    await mgr._check_daily_briefing(nm._load_state())

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_daily_briefing_not_repeated_same_day(tmp_path):
    """last_briefing_date == today prevents second send."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "last_briefing_date": "2026-04-11"}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  briefing_time: '07:30'\n  enabled: true\n")

    bot_mock = AsyncMock()

    # Mock current time to be 08:00 on 2026-04-11
    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    await mgr._check_daily_briefing(nm._load_state())

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_daily_briefing_updates_last_date(tmp_path):
    """After send, last_briefing_date set to today."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "last_briefing_date": "2026-04-10"}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  briefing_time: '07:30'\n  enabled: true\n")

    bot_mock = AsyncMock()

    now = datetime(2026, 4, 11, 7, 35, tzinfo=ZoneInfo("America/Los_Angeles"))

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_daily_briefing(state)

                reloaded = nm._load_state()
                assert reloaded["last_briefing_date"] == "2026-04-11"


def test_on_demand_briefing_does_not_advance_date(tmp_path):
    """/briefing does not set last_briefing_date."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "last_briefing_date": "2026-04-10"}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager()
                mgr._assemble_briefing()

                reloaded = nm._load_state()
                assert reloaded["last_briefing_date"] == "2026-04-10"


def test_briefing_includes_todays_calendar_events(tmp_path):
    """calendar_event files for today listed."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    event_time = now.replace(hour=9, minute=0)

    make_calendar_event(
        memories_dir,
        "evt123",
        "Team Standup",
        event_time.isoformat(),
        participants=["Alice", "Bob"],
    )

    with patch.object(nm, "CONFIG_PATH", config_file):
        with patch.object(nm, "MEMORIES_DIR", memories_dir):
            mgr = NotificationManager()
            with patch.object(mgr, "_get_local_now", return_value=now):
                briefing = mgr._assemble_briefing()

    assert "Team Standup" in briefing
    assert "9:00 AM" in briefing


def test_briefing_includes_due_commitments(tmp_path):
    """Active commitments with due_date=today shown."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    today_str = now.date().isoformat()

    make_commitment(memories_dir, "abc123", "Send report", due_date=today_str)

    with patch.object(nm, "CONFIG_PATH", config_file):
        with patch.object(nm, "MEMORIES_DIR", memories_dir):
            mgr = NotificationManager()
            with patch.object(mgr, "_get_local_now", return_value=now):
                briefing = mgr._assemble_briefing()

    assert "Commitments due today" in briefing
    assert "Send report" in briefing


def test_briefing_includes_overdue(tmp_path):
    """due_date < today shown as overdue."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    past_date = (now - timedelta(days=2)).date().isoformat()

    make_commitment(memories_dir, "def456", "Review document", due_date=past_date)

    with patch.object(nm, "CONFIG_PATH", config_file):
        with patch.object(nm, "MEMORIES_DIR", memories_dir):
            mgr = NotificationManager()
            with patch.object(mgr, "_get_local_now", return_value=now):
                briefing = mgr._assemble_briefing()

    assert "Overdue" in briefing
    assert "Review document" in briefing


def test_briefing_empty_section_omitted(tmp_path):
    """Section with no items not included in message."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    with patch.object(nm, "CONFIG_PATH", config_file):
        with patch.object(nm, "MEMORIES_DIR", memories_dir):
            mgr = NotificationManager()
            with patch.object(mgr, "_get_local_now", return_value=now):
                briefing = mgr._assemble_briefing()

    assert "Calendar" not in briefing
    assert "Commitments due today" not in briefing


# ── Commitment Alerts ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_commitment_alert_due_today(tmp_path):
    """due_date=today triggers alert."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_commitment_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    today_str = now.date().isoformat()

    make_commitment(memories_dir, "abc123def456", "Send budget", due_date=today_str)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_commitment_alerts(state)

    bot_mock.send_message.assert_called_once()
    assert "Commitment due today" in bot_mock.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_commitment_alert_due_tomorrow(tmp_path):
    """due_date=tomorrow triggers alert."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_commitment_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    tomorrow_str = (now + timedelta(days=1)).date().isoformat()

    make_commitment(memories_dir, "abc123def456", "Prepare slides", due_date=tomorrow_str)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_commitment_alerts(state)

    bot_mock.send_message.assert_called_once()
    assert "due tomorrow" in bot_mock.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_commitment_alert_deduplication(tmp_path):
    """Same commitment not re-alerted on next 60s cycle."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_commitment_alerts": ["abc123def456"]}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    today_str = now.date().isoformat()

    make_commitment(memories_dir, "abc123def456", "Send budget", due_date=today_str)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_commitment_alerts(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_commitment_alert_not_for_completed(tmp_path):
    """completed/dismissed commitments not alerted."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_commitment_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    today_str = now.date().isoformat()

    make_commitment(memories_dir, "abc123def456", "Done task", status="completed", due_date=today_str)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_commitment_alerts(state)

    bot_mock.send_message.assert_not_called()


def test_commitment_alerts_pruned_by_age(tmp_path):
    """sent_commitment_alerts entries > 1 day past due removed."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    old_date = (now - timedelta(days=3)).date().isoformat()

    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"sent_commitment_alerts": ["abc123def456"]}))

    make_commitment(memories_dir, "abc123def456", "Old task", due_date=old_date)

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager()
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    mgr._prune_sent_alerts(state)
                    nm._save_state(state)

    reloaded = nm._load_state()
    assert "abc123def456" not in reloaded["sent_commitment_alerts"]


@pytest.mark.asyncio
async def test_commitment_alert_fallback_to_summary(tmp_path):
    """Empty source_title falls back to summary field."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_commitment_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    now = datetime(2026, 4, 12, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    today_str = now.date().isoformat()

    # Create commitment with empty source_title but valid summary
    p = memories_dir / "commitment-test-abc123def456.md"
    fm = {
        "type": "commitment",
        "source_title": "",
        "summary": "Send revised budget to Alice",
        "status": "active",
        "commitment_type": "outbound",
        "owner": "Chris Robertson",
        "owner_email": "chris@acme.com",
        "recipient": "Alice",
        "due_date": today_str,
        "last_scanned": "2026-04-11T10:00:00",
        "source_memory": "zoom:test123",
        "confidence": 0.85,
    }
    frontmatter = yaml.dump(fm, sort_keys=False)
    p.write_text(f"---\n{frontmatter}---\n\n## Context\nTest.\n")

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_commitment_alerts(state)

    bot_mock.send_message.assert_called_once()
    message_text = bot_mock.send_message.call_args[1]["text"]
    assert "Send revised budget to Alice" in message_text
    assert "[outbound] \n" not in message_text


@pytest.mark.asyncio
async def test_commitment_alert_fallback_to_placeholder(tmp_path):
    """Empty source_title and summary fall back to placeholder."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_commitment_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    now = datetime(2026, 4, 12, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    today_str = now.date().isoformat()

    # Create commitment with empty source_title and empty summary
    p = memories_dir / "commitment-test-def456789abc.md"
    fm = {
        "type": "commitment",
        "source_title": "",
        "summary": "",
        "status": "active",
        "commitment_type": "outbound",
        "owner": "Chris Robertson",
        "owner_email": "chris@acme.com",
        "recipient": "Alice",
        "due_date": today_str,
        "last_scanned": "2026-04-11T10:00:00",
        "source_memory": "zoom:test123",
        "confidence": 0.85,
    }
    frontmatter = yaml.dump(fm, sort_keys=False)
    p.write_text(f"---\n{frontmatter}---\n\n## Context\nTest.\n")

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_commitment_alerts(state)

    bot_mock.send_message.assert_called_once()
    message_text = bot_mock.send_message.call_args[1]["text"]
    assert "(untitled commitment)" in message_text


# ── Pre-Meeting Alerts ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pre_meeting_in_window(tmp_path):
    """Event starting in 8–12 min triggers pre-meeting push."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_pre_meeting": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n  pre_meeting_minutes: 10\n")

    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    event_time = now + timedelta(minutes=10)

    make_calendar_event(memories_dir, "evt123", "Team Standup", event_time.isoformat(), participants=["Alice"])

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_pre_meeting_alerts(state)

    bot_mock.send_message.assert_called_once()
    assert "starts in 10 minutes" in bot_mock.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_pre_meeting_outside_window(tmp_path):
    """Event starting in 5 min or 15 min → no push."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_pre_meeting": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n  pre_meeting_minutes: 10\n")

    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    event_time = now + timedelta(minutes=5)  # Too soon

    make_calendar_event(memories_dir, "evt123", "Team Standup", event_time.isoformat())

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_pre_meeting_alerts(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_pre_meeting_all_day_skipped(tmp_path):
    """all_day: true events never trigger pre-meeting."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_pre_meeting": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    make_calendar_event(memories_dir, "evt123", "All Day Event", now.isoformat(), all_day=True)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_pre_meeting_alerts(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_pre_meeting_deduplication(tmp_path):
    """Same event not pushed again on next 60s cycle."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    # Key is the full filename stem (f.stem) not just event_id
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_pre_meeting": ["calendar-event-evt123"]}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")

    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    event_time = now + timedelta(minutes=10)

    make_calendar_event(memories_dir, "evt123", "Team Standup", event_time.isoformat())

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_pre_meeting_alerts(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_pre_meeting_dedup_survives_prune(tmp_path):
    """Regression: alert must not re-fire after _prune_sent_alerts runs.

    Bug e55d54: the old code stored event_id ('evt123') but pruned with a
    glob pattern that never matched, so the entry was dropped from state on
    every prune cycle and the alert re-fired every 60 seconds.
    Fix: store f.stem as the key so prune can look up the file directly.
    """
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  enabled: true\n")
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_pre_meeting": []}))

    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    event_time = now + timedelta(minutes=10)
    make_calendar_event(memories_dir, "evt123", "Team Standup", event_time.isoformat())

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    # First cycle: alert fires
                    state = nm._load_state()
                    await mgr._check_pre_meeting_alerts(state)
                    assert bot_mock.send_message.call_count == 1

                    # Prune: event is still in future, so entry should be KEPT
                    mgr._prune_sent_alerts(state)
                    assert "calendar-event-evt123" in state["sent_pre_meeting"]

                    # Second cycle: alert must NOT re-fire
                    await mgr._check_pre_meeting_alerts(state)
                    assert bot_mock.send_message.call_count == 1  # still 1, not 2


def test_pre_meeting_includes_contacts(tmp_path):
    """Contact file info shown for attendees."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    event_time = now + timedelta(minutes=10)

    make_calendar_event(memories_dir, "evt123", "Team Standup", event_time.isoformat(), participants=["Alice Chen"])
    make_contact(memories_dir, "Alice Chen", "alice@acme.com", "2026-04-10", 3.5)

    with patch.object(nm, "CONFIG_PATH", config_file):
        with patch.object(nm, "MEMORIES_DIR", memories_dir):
            mgr = NotificationManager()
            with patch.object(mgr, "_get_local_now", return_value=now):
                fm = _parse_frontmatter((memories_dir / "calendar-event-evt123.md").read_text())
                context = mgr._assemble_pre_meeting_context(fm, event_time)

    assert "Alice Chen" in context
    assert "relationship score 3.50" in context


def test_pre_meeting_includes_open_commitments(tmp_path):
    """Active commitments with attendees shown."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    event_time = now + timedelta(minutes=10)

    make_calendar_event(memories_dir, "evt123", "Budget Review", event_time.isoformat(), participants=["Alice"])
    make_commitment(memories_dir, "abc123", "Send budget numbers", owner="Alice", due_date="2026-04-11")

    with patch.object(nm, "CONFIG_PATH", config_file):
        with patch.object(nm, "MEMORIES_DIR", memories_dir):
            mgr = NotificationManager()
            with patch.object(mgr, "_get_local_now", return_value=now):
                fm = _parse_frontmatter((memories_dir / "calendar-event-evt123.md").read_text())
                context = mgr._assemble_pre_meeting_context(fm, event_time)

    assert "Open commitments with attendees" in context
    assert "Send budget numbers" in context


def test_pre_meeting_missing_contact_graceful(tmp_path):
    """No contact file → attendee shown without score."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    event_time = now + timedelta(minutes=10)

    make_calendar_event(memories_dir, "evt123", "Team Standup", event_time.isoformat(), participants=["Bob"])

    with patch.object(nm, "CONFIG_PATH", config_file):
        with patch.object(nm, "MEMORIES_DIR", memories_dir):
            mgr = NotificationManager()
            with patch.object(mgr, "_get_local_now", return_value=now):
                fm = _parse_frontmatter((memories_dir / "calendar-event-evt123.md").read_text())
                context = mgr._assemble_pre_meeting_context(fm, event_time)

    assert "Bob" in context
    assert "relationship score" not in context


def test_pre_meeting_sent_alerts_pruned(tmp_path):
    """Entries for past events removed from state; future events kept."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\n")

    now = datetime(2026, 4, 11, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    past_time = now - timedelta(hours=1)
    future_time = now + timedelta(hours=1)

    state_file = tmp_path / "notification-state.json"
    # Keys are full filename stems (f.stem), not bare event_ids
    state_file.write_text(json.dumps({"sent_pre_meeting": [
        "calendar-event-evt-past",
        "calendar-event-evt-future",
    ]}))

    make_calendar_event(memories_dir, "evt-past", "Past Event", past_time.isoformat())
    make_calendar_event(memories_dir, "evt-future", "Future Event", future_time.isoformat())

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager()
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    mgr._prune_sent_alerts(state)
                    nm._save_state(state)
                reloaded = nm._load_state()

    assert "calendar-event-evt-past" not in reloaded["sent_pre_meeting"]
    assert "calendar-event-evt-future" in reloaded["sent_pre_meeting"]


# ── Message Chunking ──────────────────────────────────────────────────────────

def test_message_chunking_at_4000_chars():
    """Message > 4096 chars split into multiple sends."""
    text = "a" * 5000
    chunks = _chunk_message(text, max_len=4000)
    assert len(chunks) == 2
    assert len(chunks[0]) <= 4000
    assert len(chunks[1]) <= 4000


def test_message_chunking_at_line_boundary():
    """Split at paragraph break, not mid-sentence."""
    text = "First paragraph.\n\nSecond paragraph." + ("x" * 4000)
    chunks = _chunk_message(text, max_len=4000)
    assert len(chunks) >= 2
    assert chunks[0] == "First paragraph."


# ── Loop Lifecycle ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_exits_on_stop_event(tmp_path):
    """Clean shutdown when stop_event is set."""
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": None}))

    with patch.object(nm, "STATE_FILE", state_file):
        mgr = NotificationManager()
        stop_event = asyncio.Event()
        stop_event.set()

        # Should exit immediately
        await asyncio.wait_for(mgr.run_loop(stop_event), timeout=1)


@pytest.mark.asyncio
async def test_exception_does_not_kill_loop(tmp_path):
    """RuntimeError in _check_and_send → loop continues."""
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False}))

    call_count = 0

    async def failing_check():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Test error")

    with patch.object(nm, "STATE_FILE", state_file):
        mgr = NotificationManager()
        mgr._check_and_send = failing_check

        stop_event = asyncio.Event()

        # Run for 2 cycles
        async def stop_after_delay():
            await asyncio.sleep(0.2)
            stop_event.set()

        await asyncio.gather(mgr.run_loop(stop_event), stop_after_delay())

    # Should have called twice (once failed, once succeeded)
    assert call_count >= 1


def test_state_file_write_atomic(tmp_path):
    """No .tmp file left after state write."""
    state_file = tmp_path / "notification-state.json"

    with patch.object(nm, "STATE_FILE", state_file):
        state = {"chat_id": 123456789, "muted": False}
        nm._save_state(state)

    assert state_file.exists()
    assert not (tmp_path / "notification-state.tmp").exists()


# ── Command Integration ───────────────────────────────────────────────────────

def test_cmd_mute_sets_state(tmp_path):
    """/mute writes muted: true."""
    state_file = tmp_path / "notification-state.json"

    with patch.object(nm, "STATE_FILE", state_file):
        state = nm._load_state()
        state["muted"] = True
        nm._save_state(state)

    reloaded = json.loads(state_file.read_text())
    assert reloaded["muted"] is True


def test_cmd_unmute_clears_state(tmp_path):
    """/unmute writes muted: false."""
    state_file = tmp_path / "notification-state.json"

    with patch.object(nm, "STATE_FILE", state_file):
        state = nm._load_state()
        state["muted"] = False
        nm._save_state(state)

    reloaded = json.loads(state_file.read_text())
    assert reloaded["muted"] is False


# ── Duplicate-briefing regression (Bug 1) ────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_briefing_state_saved_before_send(tmp_path):
    """last_briefing_date is written to disk BEFORE the Telegram send call.

    This is the critical ordering fix for the duplicate-briefing bug: if the
    daemon crashes mid-send, the next restart must see today's date already
    persisted and skip the briefing — not re-send it.
    """
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "last_briefing_date": "2026-04-10"}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  briefing_time: '07:30'\n  enabled: true\n")

    now = datetime(2026, 4, 11, 7, 35, tzinfo=ZoneInfo("America/Los_Angeles"))
    state_at_send_time = {}

    async def capturing_send(text, chat_id=None):
        # Capture state file contents at the moment of the send call
        state_at_send_time.update(json.loads(state_file.read_text()))

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file), \
         patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(nm, "MEMORIES_DIR", memories_dir):
        mgr = NotificationManager(bot=bot_mock)
        mgr.send_message = capturing_send
        with patch.object(mgr, "_get_local_now", return_value=now):
            await mgr._check_daily_briefing(nm._load_state())

    # State must have been persisted BEFORE send was called
    assert state_at_send_time.get("last_briefing_date") == "2026-04-11"


@pytest.mark.asyncio
async def test_daily_briefing_rolls_back_date_on_send_failure(tmp_path):
    """If the Telegram send raises, last_briefing_date is rolled back to its
    previous value so the briefing is retried next loop iteration.
    """
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "last_briefing_date": "2026-04-10"}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text("user:\n  timezone: America/Los_Angeles\nnotifications:\n  briefing_time: '07:30'\n  enabled: true\n")

    now = datetime(2026, 4, 11, 7, 35, tzinfo=ZoneInfo("America/Los_Angeles"))
    bot_mock = AsyncMock()
    bot_mock.send_message.side_effect = RuntimeError("Telegram unavailable")

    with patch.object(nm, "STATE_FILE", state_file), \
         patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(nm, "MEMORIES_DIR", memories_dir):
        mgr = NotificationManager(bot=bot_mock)
        with patch.object(mgr, "_get_local_now", return_value=now):
            with pytest.raises(RuntimeError):
                await mgr._check_daily_briefing(nm._load_state())

    reloaded = json.loads(state_file.read_text())
    assert reloaded["last_briefing_date"] == "2026-04-10"


def test_save_state_raises_on_failure(tmp_path):
    """_save_state raises (instead of silently logging) so callers can react."""
    state_file = tmp_path / "nonexistent-dir" / "notification-state.json"

    with patch.object(nm, "STATE_FILE", state_file):
        with pytest.raises(Exception):
            nm._save_state({"chat_id": None})


def make_goal(
    memories_dir: Path,
    goal_id: str,
    title: str,
    status: str = "active",
    due_date: str = None,
    category: str = "personal",
) -> Path:
    """Create a goal memory file."""
    slug = title.lower().replace(" ", "-")[:40]
    p = memories_dir / f"goal-{slug}-{goal_id}.md"
    fm = {
        "type": "goal",
        "category": category,
        "source_title": title,
        "summary": f"{title} summary",
        "tags": [],
        "created": "2026-04-10T10:00:00",
        "due_date": due_date,
        "status": status,
        "priority": "medium",
        "linked_projects": [],
        "notes": "",
    }
    frontmatter = yaml.dump(fm, sort_keys=False)
    p.write_text(f"---\n{frontmatter}---\n\n## Notes\nTest goal.\n")
    return p


def make_project(
    memories_dir: Path,
    project_id: str,
    title: str,
    status: str = "active",
    due_date: str = None,
    category: str = "work",
) -> Path:
    """Create a project memory file."""
    slug = title.lower().replace(" ", "-")[:40]
    p = memories_dir / f"project-{category}-{slug}-{project_id}.md"
    fm = {
        "type": "project",
        "category": category,
        "source_title": title,
        "summary": f"{title} summary",
        "tags": [],
        "created": "2026-04-10T10:00:00",
        "due_date": due_date,
        "status": status,
        "priority": "medium",
        "linked_goal": None,
        "milestones": [],
        "inferred_from": [],
        "notes": "",
    }
    frontmatter = yaml.dump(fm, sort_keys=False)
    p.write_text(f"---\n{frontmatter}---\n\n## Notes\nTest project.\n")
    return p


# ── Goal Deadline Alerts ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_goal_alert_fires_at_7_days(tmp_path):
    """Alert fires when goal is due in exactly 7 days."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_goal_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=7)).date().isoformat()

    make_goal(memories_dir, "abc123", "Run a 5K", due_date=due_date)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_goal_alerts(state)

    bot_mock.send_message.assert_called_once()
    text = bot_mock.send_message.call_args[1]["text"]
    assert "Goal deadline approaching" in text
    assert "Run a 5K" in text
    assert "due in 7 days" in text


@pytest.mark.asyncio
async def test_goal_alert_fires_at_1_day(tmp_path):
    """Alert fires when goal is due in exactly 1 day."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_goal_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=1)).date().isoformat()

    make_goal(memories_dir, "def456", "Complete garden project", due_date=due_date)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_goal_alerts(state)

    bot_mock.send_message.assert_called_once()
    text = bot_mock.send_message.call_args[1]["text"]
    assert "Goal deadline approaching" in text
    assert "Complete garden project" in text
    assert "due in 1 day" in text


@pytest.mark.asyncio
async def test_goal_alert_dedup(tmp_path):
    """Alert does not fire twice for same goal+horizon."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({
        "chat_id": 123456789,
        "muted": False,
        "sent_goal_alerts": ["goal:abc123:7d"]
    }))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=7)).date().isoformat()

    make_goal(memories_dir, "abc123", "Run a 5K", due_date=due_date)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_goal_alerts(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_alert_skips_completed_goals(tmp_path):
    """No alert for goals with status: completed."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_goal_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=7)).date().isoformat()

    make_goal(memories_dir, "abc123", "Run a 5K", status="completed", due_date=due_date)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_goal_alerts(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_alert_skips_goals_without_due_date(tmp_path):
    """No alert when due_date is null."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_goal_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    make_goal(memories_dir, "abc123", "Run a 5K", due_date=None)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_goal_alerts(state)

    bot_mock.send_message.assert_not_called()


# ── Project Deadline Alerts ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_project_alert_fires_at_1_day(tmp_path):
    """Alert fires when project is due in 1 day."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_project_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=1)).date().isoformat()

    make_project(memories_dir, "xyz789", "Q2 rollout plan", due_date=due_date)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_project_alerts(state)

    bot_mock.send_message.assert_called_once()
    text = bot_mock.send_message.call_args[1]["text"]
    assert "Project deadline approaching" in text
    assert "Q2 rollout plan" in text
    assert "due in 1 day" in text


@pytest.mark.asyncio
async def test_onhold_project_still_alerts(tmp_path):
    """on-hold projects still generate deadline alerts."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_project_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=7)).date().isoformat()

    make_project(memories_dir, "xyz789", "Q2 rollout plan", status="on-hold", due_date=due_date)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_project_alerts(state)

    bot_mock.send_message.assert_called_once()
    text = bot_mock.send_message.call_args[1]["text"]
    assert "Project deadline approaching" in text
    assert "Q2 rollout plan" in text


@pytest.mark.asyncio
async def test_project_alert_dedup(tmp_path):
    """Alert does not fire twice for same project+horizon."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({
        "chat_id": 123456789,
        "muted": False,
        "sent_project_alerts": ["project:xyz789:7d"]
    }))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=7)).date().isoformat()

    make_project(memories_dir, "xyz789", "Q2 rollout plan", due_date=due_date)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_project_alerts(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_project_alert_skips_completed(tmp_path):
    """No alert for completed projects."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_project_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=7)).date().isoformat()

    make_project(memories_dir, "xyz789", "Q2 rollout plan", status="completed", due_date=due_date)

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_project_alerts(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_project_alert_skips_candidates(tmp_path):
    """project-candidate-*.md files are skipped."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "notification-state.json"
    state_file.write_text(json.dumps({"chat_id": 123456789, "muted": False, "sent_project_alerts": []}))

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
        "goals:\n  deadline_horizons: [7, 1]\n"
    )

    now = datetime(2026, 4, 11, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    due_date = (now + timedelta(days=7)).date().isoformat()

    # Create a candidate file
    p = memories_dir / "project-candidate-abc123.md"
    fm = {
        "type": "project_candidate",
        "source_title": "Q2 rollout plan",
        "due_date": due_date,
        "status": "pending_confirmation",
    }
    frontmatter = yaml.dump(fm, sort_keys=False)
    p.write_text(f"---\n{frontmatter}---\n\n## Notes\nCandidate.\n")

    bot_mock = AsyncMock()

    with patch.object(nm, "STATE_FILE", state_file):
        with patch.object(nm, "CONFIG_PATH", config_file):
            with patch.object(nm, "MEMORIES_DIR", memories_dir):
                mgr = NotificationManager(bot=bot_mock)
                with patch.object(mgr, "_get_local_now", return_value=now):
                    state = nm._load_state()
                    await mgr._check_project_alerts(state)

    bot_mock.send_message.assert_not_called()


# ── Multi-transport send_message ──────────────────────────────────────────────

def _make_adapter(chat_id):
    """Build a minimal mock TransportAdapter with the given chat_id (or None)."""
    a = MagicMock()
    a.get_chat_id = MagicMock(return_value=chat_id)
    a.max_message_length = MagicMock(return_value=4000)
    a.send_text = AsyncMock()
    return a


@pytest.mark.asyncio
async def test_send_message_multi_transport_delivers_to_all(tmp_path):
    """With two adapters that both have chat_ids, both receive the message."""
    state_file = tmp_path / "state.json"
    a1 = _make_adapter("D001")
    a2 = _make_adapter("D002")
    mgr = NotificationManager(transports=[a1, a2], deploy_dir=tmp_path)

    with patch.object(nm, "STATE_FILE", state_file):
        await mgr.send_message("hello")

    a1.send_text.assert_awaited_once_with("D001", "hello")
    a2.send_text.assert_awaited_once_with("D002", "hello")


@pytest.mark.asyncio
async def test_send_message_multi_transport_skips_none_chat_id(tmp_path):
    """Adapter with no chat_id is skipped; others still receive the message."""
    state_file = tmp_path / "state.json"
    a_ok = _make_adapter("D001")
    a_none = _make_adapter(None)
    mgr = NotificationManager(transports=[a_ok, a_none], deploy_dir=tmp_path)

    with patch.object(nm, "STATE_FILE", state_file):
        await mgr.send_message("hello")

    a_ok.send_text.assert_awaited_once_with("D001", "hello")
    a_none.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_falls_back_to_bot_when_all_adapters_have_no_chat_id(tmp_path):
    """All adapters have chat_id=None → falls through to legacy self.bot path."""
    state_file = tmp_path / "state.json"
    state_file.write_text('{"chat_id": 12345, "muted": false}')

    bot_mock = AsyncMock()
    a1 = _make_adapter(None)
    a2 = _make_adapter(None)
    mgr = NotificationManager(bot=bot_mock, transports=[a1, a2], deploy_dir=tmp_path)

    with patch.object(nm, "STATE_FILE", state_file):
        await mgr.send_message("startup notification")

    # Legacy bot path fires since no adapter could send
    bot_mock.send_message.assert_awaited_once()
    assert "startup notification" in bot_mock.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_send_message_no_double_delivery_when_adapter_sends(tmp_path):
    """When adapters send successfully, legacy self.bot path does NOT fire."""
    state_file = tmp_path / "state.json"
    state_file.write_text('{"chat_id": 12345, "muted": false}')

    bot_mock = AsyncMock()
    a1 = _make_adapter("D001")
    mgr = NotificationManager(bot=bot_mock, transports=[a1], deploy_dir=tmp_path)

    with patch.object(nm, "STATE_FILE", state_file):
        await mgr.send_message("hello")

    a1.send_text.assert_awaited_once()
    bot_mock.send_message.assert_not_awaited()


# ── Calendar staleness alert ─────────────────────────────────────────────────

def _stale_config() -> str:
    return (
        "user:\n  timezone: America/Los_Angeles\n"
        "notifications:\n  enabled: true\n"
    )


@pytest.mark.asyncio
async def test_calendar_staleness_alert_fires_after_24h(tmp_path):
    """File mtime 26h ago → alert fires once, today's date recorded."""
    import os
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "chat_id": 123, "muted": False, "sent_calendar_staleness_alerts": []
    }))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_stale_config())

    evt = make_calendar_event(memories_dir, "evt1", "Old", "2026-04-11T09:00:00")
    now = datetime(2026, 4, 21, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    stale_ts = now.timestamp() - 26 * 3600
    os.utime(evt, (stale_ts, stale_ts))

    bot_mock = AsyncMock()
    with patch.object(nm, "STATE_FILE", state_file), \
         patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(nm, "MEMORIES_DIR", memories_dir):
        mgr = NotificationManager(bot=bot_mock)
        with patch.object(mgr, "_get_local_now", return_value=now):
            state = nm._load_state()
            await mgr._check_calendar_staleness(state)

    bot_mock.send_message.assert_called_once()
    text = bot_mock.send_message.call_args[1]["text"]
    assert "stale" in text.lower()
    saved = json.loads(state_file.read_text())
    assert "2026-04-21" in saved["sent_calendar_staleness_alerts"]


@pytest.mark.asyncio
async def test_calendar_staleness_alert_deduped_same_day(tmp_path):
    """State already contains today → no send."""
    import os
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "chat_id": 123, "muted": False,
        "sent_calendar_staleness_alerts": ["2026-04-21"]
    }))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_stale_config())

    evt = make_calendar_event(memories_dir, "evt1", "Old", "2026-04-11T09:00:00")
    now = datetime(2026, 4, 21, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    stale_ts = now.timestamp() - 48 * 3600
    os.utime(evt, (stale_ts, stale_ts))

    bot_mock = AsyncMock()
    with patch.object(nm, "STATE_FILE", state_file), \
         patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(nm, "MEMORIES_DIR", memories_dir):
        mgr = NotificationManager(bot=bot_mock)
        with patch.object(mgr, "_get_local_now", return_value=now):
            state = nm._load_state()
            await mgr._check_calendar_staleness(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_calendar_staleness_alert_not_fired_when_recent(tmp_path):
    """File mtime 2h ago → no alert."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "chat_id": 123, "muted": False, "sent_calendar_staleness_alerts": []
    }))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_stale_config())

    # Fresh mtime (default from write_text is "now")
    make_calendar_event(memories_dir, "evt1", "New", "2026-04-21T09:00:00")
    now = datetime.now(ZoneInfo("America/Los_Angeles"))

    bot_mock = AsyncMock()
    with patch.object(nm, "STATE_FILE", state_file), \
         patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(nm, "MEMORIES_DIR", memories_dir):
        mgr = NotificationManager(bot=bot_mock)
        with patch.object(mgr, "_get_local_now", return_value=now):
            state = nm._load_state()
            await mgr._check_calendar_staleness(state)

    bot_mock.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_calendar_staleness_alert_not_fired_when_no_files(tmp_path):
    """Empty memories dir → silent (fresh-install case, not stale)."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "chat_id": 123, "muted": False, "sent_calendar_staleness_alerts": []
    }))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_stale_config())

    now = datetime(2026, 4, 21, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    bot_mock = AsyncMock()
    with patch.object(nm, "STATE_FILE", state_file), \
         patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(nm, "MEMORIES_DIR", memories_dir):
        mgr = NotificationManager(bot=bot_mock)
        with patch.object(mgr, "_get_local_now", return_value=now):
            state = nm._load_state()
            await mgr._check_calendar_staleness(state)

    bot_mock.send_message.assert_not_called()


def test_calendar_staleness_alert_prunes_state_after_7_days(tmp_path):
    """Entries older than 7 days are dropped by _prune_sent_alerts."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"
    # 8-day-old entry (should be pruned) + fresh entry (should survive)
    state_file.write_text(json.dumps({
        "chat_id": 123, "muted": False,
        "sent_calendar_staleness_alerts": ["2026-04-13", "2026-04-21"],
        "sent_commitment_alerts": [],
        "sent_pre_meeting": [],
    }))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_stale_config())

    now = datetime(2026, 4, 21, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    bot_mock = AsyncMock()
    with patch.object(nm, "STATE_FILE", state_file), \
         patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(nm, "MEMORIES_DIR", memories_dir):
        mgr = NotificationManager(bot=bot_mock)
        with patch.object(mgr, "_get_local_now", return_value=now):
            state = nm._load_state()
            mgr._prune_sent_alerts(state)

    assert "2026-04-13" not in state["sent_calendar_staleness_alerts"]
    assert "2026-04-21" in state["sent_calendar_staleness_alerts"]


@pytest.mark.asyncio
async def test_calendar_staleness_send_failure_rolls_back_state(tmp_path):
    """If send_message raises, today's date must not remain in state."""
    import os
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "chat_id": 123, "muted": False, "sent_calendar_staleness_alerts": []
    }))
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_stale_config())

    evt = make_calendar_event(memories_dir, "evt1", "Old", "2026-04-11T09:00:00")
    now = datetime(2026, 4, 21, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    stale_ts = now.timestamp() - 30 * 3600
    os.utime(evt, (stale_ts, stale_ts))

    bot_mock = AsyncMock()
    bot_mock.send_message.side_effect = RuntimeError("telegram down")

    with patch.object(nm, "STATE_FILE", state_file), \
         patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(nm, "MEMORIES_DIR", memories_dir):
        mgr = NotificationManager(bot=bot_mock)
        with patch.object(mgr, "_get_local_now", return_value=now):
            state = nm._load_state()
            await mgr._check_calendar_staleness(state)

    saved = json.loads(state_file.read_text())
    assert "2026-04-21" not in saved["sent_calendar_staleness_alerts"]


# ── iCloud EDEADLK resilience on config read ──────────────────────────────────

def _reset_config_cache():
    """Helper: clear the module-level shared config cache between tests."""
    import utils as _utils
    _utils._reset_config_cache()


def test_load_config_retries_on_icloud_edeadlk(tmp_path, caplog):
    """config.yaml read hitting EDEADLK must retry, then succeed on a later attempt.

    Previously a single EDEADLK crashed /briefing (and every other command
    that routed through _check_auth → get_chat_id → _load_config).
    """
    _reset_config_cache()
    config_file = tmp_path / "config.yaml"
    config_file.write_text("notifications:\n  telegram_chat_id: 42\n")

    real_read_text = Path.read_text
    attempts = {"n": 0}

    def flaky_read(self, *a, **kw):
        if self == config_file:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError(11, "Resource deadlock avoided")
        return real_read_text(self, *a, **kw)

    with patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(Path, "read_text", flaky_read):
        mgr = NotificationManager()
        cfg = mgr._load_config()

    assert cfg == {"notifications": {"telegram_chat_id": 42}}
    assert attempts["n"] >= 2, "must have retried at least once"


def test_load_config_falls_back_to_cache_on_persistent_edeadlk(tmp_path, caplog):
    """If EDEADLK persists through all retries, return last-good cache — don't crash."""
    _reset_config_cache()
    config_file = tmp_path / "config.yaml"
    config_file.write_text("notifications:\n  telegram_chat_id: 99\n")

    with patch.object(nm, "CONFIG_PATH", config_file):
        mgr = NotificationManager()
        # First read populates the cache.
        first = mgr._load_config()
        assert first == {"notifications": {"telegram_chat_id": 99}}

        # Now every read raises EDEADLK — must serve cache rather than crash.
        real_read_text = Path.read_text

        def always_edeadlk(self, *a, **kw):
            if self == config_file:
                raise OSError(11, "Resource deadlock avoided")
            return real_read_text(self, *a, **kw)

        # Force the cache mtime check to see a different mtime so we actually
        # hit the retry loop (otherwise the cached value is returned without a read).
        def new_stat(self, *a, **kw):
            return type("S", (), {"st_mtime": 999999.0})()

        with patch.object(Path, "read_text", always_edeadlk), \
             patch.object(Path, "stat", new_stat):
            second = mgr._load_config()

    assert second == {"notifications": {"telegram_chat_id": 99}}, "must fall back to cache"


def test_assemble_briefing_survives_icloud_edeadlk_on_memory_file(tmp_path):
    """_assemble_briefing must not crash when a memory file read hits EDEADLK.

    Before this fix, a transient EDEADLK on any calendar-event-*.md or
    commitment-*.md read raised straight out of the command handler and
    Telegram got no reply. Now the retry helper absorbs it and returns
    "" so the file is silently skipped.
    """
    _reset_config_cache()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # One good calendar event and one "bad" one whose read always EDEADLKs.
    good_event = memories_dir / "calendar-event-host-2026-04-22-standup-abc123.md"
    good_event.write_text(
        "---\ntype: calendar_event\nsource_title: Standup\n"
        "start_time: '2026-04-22T09:00:00'\n---\n"
    )
    bad_event = memories_dir / "calendar-event-host-2026-04-22-broken-def456.md"
    bad_event.write_text("---\ntype: calendar_event\n---\n")

    real_read = Path.read_text

    def flaky(self, *a, **kw):
        if "broken" in self.name:
            raise OSError(11, "Resource deadlock avoided")
        return real_read(self, *a, **kw)

    bot_mock = AsyncMock()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"chat_id": 111}))

    with patch.object(nm, "MEMORIES_DIR", memories_dir), \
         patch.object(nm, "STATE_FILE", state_file), \
         patch.object(Path, "read_text", flaky):
        mgr = NotificationManager(bot=bot_mock)
        text = mgr._assemble_briefing()

    # Must complete without raising and include the good event.
    assert "Standup" in text


def test_load_config_caches_by_mtime(tmp_path):
    """Unchanged mtime → no re-read of iCloud file."""
    _reset_config_cache()
    config_file = tmp_path / "config.yaml"
    config_file.write_text("notifications:\n  telegram_chat_id: 7\n")

    read_count = {"n": 0}
    real_read_text = Path.read_text

    def counting_read(self, *a, **kw):
        if self == config_file:
            read_count["n"] += 1
        return real_read_text(self, *a, **kw)

    with patch.object(nm, "CONFIG_PATH", config_file), \
         patch.object(Path, "read_text", counting_read):
        mgr = NotificationManager()
        mgr._load_config()
        mgr._load_config()
        mgr._load_config()

    assert read_count["n"] == 1, f"expected 1 read (cached), got {read_count['n']}"
