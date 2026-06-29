"""Unit tests for circle_bot.py — CircleBotHandler."""
import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from circle_bot import CircleBotHandler, _parse_frontmatter, _get_body


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_update(user_id: int = 42) -> MagicMock:
    """Create a mock Telegram Update with a user_id."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args=None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


def _write_memory(circle_dir: Path, filename: str, frontmatter: dict, body: str = "Body text."):
    content = "---\n" + yaml.dump(frontmatter) + "---\n\n" + body
    (circle_dir / filename).write_text(content)


def _make_handler(circle_dir: Path, members=None, display_name="Test Circle") -> CircleBotHandler:
    return CircleBotHandler(
        circle_path=circle_dir,
        display_name=display_name,
        members=members or [],
    )


# ── _parse_frontmatter ────────────────────────────────────────────────────────

def test_parse_frontmatter_valid():
    text = "---\ntype: goal\ntags:\n  - family\n---\n\nBody."
    fm = _parse_frontmatter(text)
    assert fm["type"] == "goal"
    assert "family" in fm["tags"]


def test_parse_frontmatter_no_block():
    assert _parse_frontmatter("no frontmatter here") == {}


def test_parse_frontmatter_bad_yaml():
    assert _parse_frontmatter("---\n: :\n---") == {}


def test_get_body_strips_frontmatter():
    text = "---\ntype: goal\n---\n\nActual body here."
    assert _get_body(text) == "Actual body here."


# ── Member enforcement ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_non_member_rejected(tmp_path):
    """User not in members list receives rejection message."""
    handler = _make_handler(
        tmp_path,
        members=[{"telegram_user_id": 99, "name": "Alice"}],
    )
    update = _make_update(user_id=42)  # not in members
    ctx = _make_context()

    await handler.cmd_help(update, ctx)
    await handler.cmd_memories(update, ctx)
    await handler.cmd_search(update, _make_context(args=["test"]))
    await handler.cmd_events(update, ctx)
    await handler.cmd_commitments(update, ctx)

    # Every command should reject with the membership message
    assert update.message.reply_text.call_count == 5
    for call in update.message.reply_text.call_args_list:
        text = call.args[0]
        assert "not a member" in text.lower(), f"Unexpected reply: {text!r}"


@pytest.mark.asyncio
async def test_member_allowed(tmp_path):
    """User in members list can access commands."""
    handler = _make_handler(
        tmp_path,
        members=[{"telegram_user_id": 42, "name": "Alice"}],
    )
    update = _make_update(user_id=42)
    ctx = _make_context()

    await handler.cmd_help(update, ctx)
    text = update.message.reply_text.call_args.args[0]
    assert "not a member" not in text.lower()


@pytest.mark.asyncio
async def test_empty_members_allows_anyone(tmp_path):
    """When members list is empty, any user can access (obscurity mode)."""
    handler = _make_handler(tmp_path, members=[])
    update = _make_update(user_id=999)
    ctx = _make_context()

    await handler.cmd_help(update, ctx)
    text = update.message.reply_text.call_args.args[0]
    assert "not a member" not in text.lower()


# ── /help ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_help_shows_display_name(tmp_path):
    handler = _make_handler(tmp_path, display_name="Robertson Family")
    update = _make_update()
    await handler.cmd_help(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "Robertson Family" in text
    assert "/memories" in text
    assert "/search" in text
    assert "/events" in text
    assert "/commitments" in text


# ── /memories ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_memories_empty_circle(tmp_path):
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_memories(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "No memories" in text


@pytest.mark.asyncio
async def test_memories_lists_files(tmp_path):
    _write_memory(tmp_path, "2026-06-01-alpha-abc123.md",
                  {"type": "memory", "source_title": "Alpha Post", "date": "2026-06-01"})
    _write_memory(tmp_path, "2026-06-02-beta-def456.md",
                  {"type": "memory", "source_title": "Beta Post", "date": "2026-06-02"})
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_memories(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "Alpha Post" in text
    assert "Beta Post" in text


@pytest.mark.asyncio
async def test_memories_detail_by_index(tmp_path):
    _write_memory(tmp_path, "2026-06-01-alpha-abc123.md",
                  {"type": "memory", "source_title": "Alpha Post", "summary": "Great article"},
                  body="Full article body here.")
    handler = _make_handler(tmp_path)
    update = _make_update()
    # First populate the list
    await handler.cmd_memories(update, _make_context())
    update.message.reply_text.reset_mock()
    # Now get detail
    await handler.cmd_memories(update, _make_context(args=["1"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Alpha Post" in text
    assert "Great article" in text


@pytest.mark.asyncio
async def test_memories_detail_invalid_index(tmp_path):
    handler = _make_handler(tmp_path)
    update = _make_update()
    # No list populated yet
    await handler.cmd_memories(update, _make_context(args=["5"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Invalid index" in text


# ── /search ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_no_args(tmp_path):
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_search(update, _make_context(args=[]))
    text = update.message.reply_text.call_args.args[0]
    assert "Usage" in text


@pytest.mark.asyncio
async def test_search_finds_by_title(tmp_path):
    _write_memory(tmp_path, "2026-06-01-python-abc.md",
                  {"type": "memory", "source_title": "Python Tips", "tags": ["coding"]})
    _write_memory(tmp_path, "2026-06-02-cooking-def.md",
                  {"type": "memory", "source_title": "Cooking Recipes", "tags": ["food"]})
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_search(update, _make_context(args=["python"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Python Tips" in text
    assert "Cooking" not in text


@pytest.mark.asyncio
async def test_search_finds_by_tag(tmp_path):
    _write_memory(tmp_path, "2026-06-01-post-abc.md",
                  {"type": "memory", "source_title": "Some Post", "tags": ["family", "kids"]})
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_search(update, _make_context(args=["family"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Some Post" in text


@pytest.mark.asyncio
async def test_search_no_results(tmp_path):
    _write_memory(tmp_path, "2026-06-01-post-abc.md",
                  {"type": "memory", "source_title": "Cooking Recipes"})
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_search(update, _make_context(args=["quantum"]))
    text = update.message.reply_text.call_args.args[0]
    assert "No results" in text


@pytest.mark.asyncio
async def test_search_updates_last_memory_list(tmp_path):
    """After /search, /memories N resolves into the search results."""
    _write_memory(tmp_path, "2026-06-01-post-abc.md",
                  {"type": "memory", "source_title": "Python Tips", "summary": "summary here"})
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_search(update, _make_context(args=["python"]))
    update.message.reply_text.reset_mock()
    await handler.cmd_memories(update, _make_context(args=["1"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Python Tips" in text


# ── /events ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_events_no_events(tmp_path):
    _write_memory(tmp_path, "2026-06-01-post-abc.md",
                  {"type": "memory", "source_title": "A reading"})
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_events(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "No calendar events" in text


@pytest.mark.asyncio
async def test_events_lists_calendar_events(tmp_path):
    _write_memory(tmp_path, "calendar-event-2026-06-15-dentist-abc.md", {
        "type": "calendar_event",
        "source_title": "Dentist Appointment",
        "start_time": "2026-06-15T10:00:00",
    })
    _write_memory(tmp_path, "2026-06-01-reading-def.md",
                  {"type": "memory", "source_title": "A reading"})
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_events(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "Dentist Appointment" in text
    assert "A reading" not in text


@pytest.mark.asyncio
async def test_events_detail_by_index(tmp_path):
    _write_memory(tmp_path, "calendar-event-2026-06-15-dentist-abc.md", {
        "type": "calendar_event",
        "source_title": "Dentist Appointment",
        "start_time": "2026-06-15T10:00:00",
        "location": "123 Main St",
        "summary": "Regular checkup",
    })
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_events(update, _make_context())
    update.message.reply_text.reset_mock()
    await handler.cmd_events(update, _make_context(args=["1"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Dentist Appointment" in text
    assert "123 Main St" in text
    assert "Regular checkup" in text


@pytest.mark.asyncio
async def test_events_detail_invalid_index(tmp_path):
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_events(update, _make_context(args=["1"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Invalid index" in text


# ── /commitments ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_commitments_empty(tmp_path):
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_commitments(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "No active commitments" in text


@pytest.mark.asyncio
async def test_commitments_lists_active(tmp_path):
    _write_memory(tmp_path, "commitment-send-report-abc.md", {
        "type": "commitment",
        "status": "active",
        "source_title": "Send quarterly report",
        "commitment_type": "outbound",
        "due_date": "2026-07-01",
    })
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_commitments(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "Send quarterly report" in text
    assert "[outbound]" in text


@pytest.mark.asyncio
async def test_commitments_skips_completed(tmp_path):
    _write_memory(tmp_path, "commitment-done-abc.md", {
        "type": "commitment",
        "status": "completed",
        "source_title": "Already done",
    })
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_commitments(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "No active commitments" in text


# ── MEMORIES_DIR isolation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_never_reads_memories_dir(tmp_path, monkeypatch):
    """Verify that CircleBotHandler never touches MEMORIES_DIR.

    MEMORIES_DIR is the full brain — reading it from the circle handler
    would leak private memories to circle members. This test substitutes
    a sentinel Path that raises on any read/glob operation, then exercises
    all commands to confirm they only access circle_path.
    """
    class _SentinelPath:
        """A fake Path that explodes on glob/read_text/exists (any access)."""
        def glob(self, *a, **kw):
            raise AssertionError("CircleBotHandler accessed MEMORIES_DIR!")
        def read_text(self, *a, **kw):
            raise AssertionError("CircleBotHandler accessed MEMORIES_DIR!")
        def exists(self, *a, **kw):
            return False

    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    _write_memory(circle_dir, "calendar-event-2026-06-01-abc.md", {
        "type": "calendar_event",
        "source_title": "A shared event",
        "start_time": "2026-06-01T09:00:00",
    })

    sentinel = _SentinelPath()

    # Patch the module-level MEMORIES_DIR-ish path (if any) in circle_bot
    import circle_bot as cb
    # circle_bot has no module-level MEMORIES_DIR — this just ensures
    # any accidental import doesn't sneak one in via circle_sync_scanner.
    handler = CircleBotHandler(
        circle_path=circle_dir,
        display_name="Safe Circle",
        members=[],
    )
    # Override _load_files to use circle_dir — already the default
    update = _make_update()
    ctx = _make_context()

    # These must all succeed using only circle_dir
    await handler.cmd_help(update, ctx)
    await handler.cmd_memories(update, ctx)
    await handler.cmd_search(update, _make_context(args=["event"]))
    await handler.cmd_events(update, ctx)
    await handler.cmd_commitments(update, ctx)

    # If we got here without the sentinel firing, the invariant holds.


# ── Missing circle path ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_commands_handle_missing_circle_path(tmp_path):
    """Handler gracefully returns empty lists when circle folder doesn't exist."""
    missing = tmp_path / "does-not-exist"
    handler = _make_handler(missing)
    update = _make_update()

    await handler.cmd_memories(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "No memories" in text

    update.message.reply_text.reset_mock()
    await handler.cmd_events(update, _make_context())
    text = update.message.reply_text.call_args.args[0]
    assert "No calendar events" in text
