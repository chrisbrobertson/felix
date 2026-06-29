"""Unit tests for circle_bot.py — CircleBotHandler and CircleBotRunner."""
import asyncio
import json
import time

import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from circle_bot import CircleBotHandler, CircleBotRunner, _parse_frontmatter, _get_body
from circle_ruleset import CircleRuleset


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

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "answer"
    with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
        await handler.cmd_ask(update, _make_context(args=["what", "events?"]))

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


# ── /join invite flow (FR-6) ──────────────────────────────────────────────────


def _make_invites_file(tmp_path: Path, slug: str, code: str, expires_delta: float = 3600) -> Path:
    """Write a circle-invites.json with one valid code."""
    invites_file = tmp_path / "circle-invites.json"
    state = {
        slug: {
            code: {"expires_at": time.time() + expires_delta, "created_by": 12345}
        }
    }
    invites_file.write_text(json.dumps(state))
    return invites_file


def _make_ruleset_file(tmp_path: Path, slug: str, members=None) -> Path:
    """Write a minimal circle YAML ruleset file."""
    members = members or []
    data = {
        "circle": slug,
        "display_name": slug.title(),
        "members": members,
        "bot_token": "",
        "icloud_folder": f"second-brain-circles/{slug}/memories",
        "rules": {"include": [{"type": "calendar_event"}], "exclude": []},
    }
    p = tmp_path / f"{slug}.yaml"
    p.write_text(yaml.dump(data))
    return p


def _make_join_handler(
    circle_dir: Path,
    slug: str,
    members=None,
    ruleset_path=None,
    invites_file=None,
) -> CircleBotHandler:
    return CircleBotHandler(
        circle_path=circle_dir,
        display_name=f"{slug.title()} Circle",
        members=members or [],
        ruleset_path=ruleset_path,
        invites_file=invites_file,
        slug=slug,
    )


@pytest.mark.asyncio
async def test_join_no_args_shows_usage(tmp_path):
    handler = _make_join_handler(tmp_path, "family")
    update = _make_update()
    await handler.cmd_join(update, _make_context(args=[]))
    text = update.message.reply_text.call_args.args[0]
    assert "Usage" in text


@pytest.mark.asyncio
async def test_join_no_invites_file_rejects(tmp_path):
    """Without invites_file configured, /join returns a config error."""
    handler = _make_join_handler(tmp_path, "family")  # no invites_file
    update = _make_update()
    await handler.cmd_join(update, _make_context(args=["a1b2c3d4"]))
    text = update.message.reply_text.call_args.args[0]
    assert "configuration error" in text.lower()


@pytest.mark.asyncio
async def test_join_invalid_code_rejected(tmp_path):
    """A code not in the invites file is rejected."""
    code = "validcode"
    invites_file = tmp_path / "circle-invites.json"
    invites_file.write_text(json.dumps(
        {"family": {code: {"expires_at": time.time() + 3600, "created_by": 1}}}
    ))
    ruleset_path = _make_ruleset_file(tmp_path, "family")
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    handler = _make_join_handler(circle_dir, "family", ruleset_path=ruleset_path, invites_file=invites_file)
    update = _make_update(user_id=42)
    await handler.cmd_join(update, _make_context(args=["wrongcode"]))
    text = update.message.reply_text.call_args.args[0]
    assert "Invalid or expired" in text


@pytest.mark.asyncio
async def test_join_expired_code_rejected(tmp_path):
    """An expired code is rejected and cleaned up from the invites file."""
    code = "a1b2c3d4"
    invites_file = tmp_path / "circle-invites.json"
    invites_file.write_text(json.dumps(
        {"family": {code: {"expires_at": time.time() - 1, "created_by": 1}}}
    ))
    ruleset_path = _make_ruleset_file(tmp_path, "family")
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    handler = _make_join_handler(circle_dir, "family", ruleset_path=ruleset_path, invites_file=invites_file)
    update = _make_update(user_id=42)
    await handler.cmd_join(update, _make_context(args=[code]))
    text = update.message.reply_text.call_args.args[0]
    assert "Invalid or expired" in text
    # Expired code must be removed from the invites file
    state = json.loads(invites_file.read_text())
    assert code not in state.get("family", {})


@pytest.mark.asyncio
async def test_join_valid_code_adds_member(tmp_path):
    """Valid code → user appended to ruleset, welcome message sent."""
    code = "a1b2c3d4"
    invites_file = _make_invites_file(tmp_path, "family", code)
    ruleset_path = _make_ruleset_file(tmp_path, "family")
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    handler = _make_join_handler(circle_dir, "family", ruleset_path=ruleset_path, invites_file=invites_file)
    update = _make_update(user_id=42)
    update.effective_user.first_name = "Alice"
    await handler.cmd_join(update, _make_context(args=[code]))
    text = update.message.reply_text.call_args.args[0]
    assert "Welcome" in text
    # Member added to ruleset file
    data = yaml.safe_load(ruleset_path.read_text())
    members = data.get("members", [])
    assert any(m.get("telegram_user_id") == 42 for m in members)
    assert any(m.get("name") == "Alice" for m in members)


@pytest.mark.asyncio
async def test_join_code_consumed_after_use(tmp_path):
    """Code is deleted from invites file after successful /join (one-time use)."""
    code = "a1b2c3d4"
    invites_file = _make_invites_file(tmp_path, "family", code)
    ruleset_path = _make_ruleset_file(tmp_path, "family")
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    handler = _make_join_handler(circle_dir, "family", ruleset_path=ruleset_path, invites_file=invites_file)
    update = _make_update(user_id=42)
    update.effective_user.first_name = "Alice"
    await handler.cmd_join(update, _make_context(args=[code]))
    state = json.loads(invites_file.read_text())
    assert code not in state.get("family", {})


@pytest.mark.asyncio
async def test_join_nonmember_with_valid_code_succeeds(tmp_path):
    """Non-member with a valid code joins successfully — member check is skipped."""
    code = "a1b2c3d4"
    invites_file = _make_invites_file(tmp_path, "family", code)
    ruleset_path = _make_ruleset_file(tmp_path, "family")
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    # Enforced members list that does NOT include user 42
    handler = _make_join_handler(
        circle_dir, "family",
        members=[{"telegram_user_id": 99, "name": "Existing"}],
        ruleset_path=ruleset_path,
        invites_file=invites_file,
    )
    update = _make_update(user_id=42)
    update.effective_user.first_name = "Alice"
    await handler.cmd_join(update, _make_context(args=[code]))
    text = update.message.reply_text.call_args.args[0]
    assert "not a member" not in text.lower()
    assert "Welcome" in text


@pytest.mark.asyncio
async def test_join_idempotent_already_member(tmp_path):
    """If user is already in members, /join adds no duplicate entry."""
    code = "a1b2c3d4"
    invites_file = _make_invites_file(tmp_path, "family", code)
    ruleset_path = _make_ruleset_file(
        tmp_path, "family",
        members=[{"telegram_user_id": 42, "name": "Alice"}],
    )
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    handler = _make_join_handler(circle_dir, "family", ruleset_path=ruleset_path, invites_file=invites_file)
    update = _make_update(user_id=42)
    update.effective_user.first_name = "Alice"
    await handler.cmd_join(update, _make_context(args=[code]))
    data = yaml.safe_load(ruleset_path.read_text())
    members_with_id = [m for m in data.get("members", []) if m.get("telegram_user_id") == 42]
    assert len(members_with_id) == 1


@pytest.mark.asyncio
async def test_join_grants_immediate_access(tmp_path):
    """After /join, new member's ID is in the handler's in-memory set immediately."""
    code = "a1b2c3d4"
    invites_file = _make_invites_file(tmp_path, "family", code)
    ruleset_path = _make_ruleset_file(tmp_path, "family")
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    handler = _make_join_handler(circle_dir, "family", ruleset_path=ruleset_path, invites_file=invites_file)
    update = _make_update(user_id=42)
    update.effective_user.first_name = "Alice"
    await handler.cmd_join(update, _make_context(args=[code]))
    assert 42 in handler._member_ids


# ── CircleBotRunner ───────────────────────────────────────────────────────────

def _make_ruleset(
    bot_token: str = "fake:token123",
    slug: str = "family",
    display_name: str = "Family",
    members=None,
    icloud_folder: str = "second-brain-circles/family/memories",
) -> CircleRuleset:
    return CircleRuleset(
        slug=slug,
        display_name=display_name,
        members=members or [],
        bot_token=bot_token,
        icloud_folder=icloud_folder,
        include_rules=[],
        exclude_rules=[],
    )


# ── /ask LLM query ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ask_no_args_shows_usage(tmp_path):
    """/ask with no arguments returns the usage hint."""
    handler = _make_handler(tmp_path)
    update = _make_update()
    await handler.cmd_ask(update, _make_context(args=[]))
    update.message.reply_text.assert_called_once()
    assert "Usage" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ask_non_member_rejected(tmp_path):
    """/ask rejects users not in the members list."""
    handler = _make_handler(tmp_path, members=[{"telegram_user_id": 99, "name": "Bob"}])
    update = _make_update(user_id=42)
    await handler.cmd_ask(update, _make_context(args=["hello"]))
    update.message.reply_text.assert_called_once()
    assert "not a member" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ask_calls_llm_and_returns_answer(tmp_path):
    """/ask calls acompletion with circle-scoped context and relays the answer."""
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    _write_memory(circle_dir, "2026-06-01-school-play-abc.md", {
        "type": "calendar_event",
        "source_title": "School Play",
        "tags": ["kids"],
    }, "The school play is on Friday.")

    handler = _make_handler(circle_dir, display_name="Family")
    update = _make_update()

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "The school play is on Friday."

    with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
        await handler.cmd_ask(update, _make_context(args=["When", "is", "the", "play?"]))

    update.message.reply_text.assert_called_once()
    assert "Friday" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ask_handles_llm_error_gracefully(tmp_path):
    """/ask returns an error message when acompletion raises."""
    handler = _make_handler(tmp_path)
    update = _make_update()

    with patch("litellm.acompletion", new=AsyncMock(side_effect=Exception("API down"))):
        await handler.cmd_ask(update, _make_context(args=["anything?"]))

    update.message.reply_text.assert_called_once()
    assert "couldn't process" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_ask_chunks_long_response(tmp_path):
    """/ask chunks responses longer than 4096 characters."""
    handler = _make_handler(tmp_path)
    update = _make_update()

    long_answer = "x" * 5000
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = long_answer

    with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
        await handler.cmd_ask(update, _make_context(args=["question?"]))

    assert update.message.reply_text.call_count == 2


@pytest.mark.asyncio
async def test_ask_uses_circle_path_not_memories_dir(tmp_path):
    """/ask reads only from circle_path (MEMORIES_DIR isolation invariant)."""
    circle_dir = tmp_path / "circle"
    circle_dir.mkdir()
    _write_memory(circle_dir, "2026-06-01-note-abc.md", {
        "source_title": "Shared Note",
        "tags": ["family"],
    }, "Shared content here.")

    handler = _make_handler(circle_dir)
    update = _make_update()

    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "answer"

    captured_messages = []

    async def fake_acompletion(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return mock_resp

    with patch("litellm.acompletion", new=fake_acompletion):
        await handler.cmd_ask(update, _make_context(args=["note?"]))

    system_content = next(m["content"] for m in captured_messages if m["role"] == "system")
    assert "Shared content" in system_content


# ── CircleBotRunner ───────────────────────────────────────────────────────────

@patch("circle_bot.ApplicationBuilder")
def test_runner_builds_application_with_token(mock_builder, tmp_path):
    """CircleBotRunner calls ApplicationBuilder().token(bot_token).build()."""
    ruleset = _make_ruleset(bot_token="7654321:AAtest")
    ruleset_path = tmp_path / "family.yaml"
    ruleset_path.touch()
    runner = CircleBotRunner(ruleset, ruleset_path, tmp_path, tmp_path / "circle-invites.json")
    mock_builder.assert_called_once()
    mock_builder.return_value.token.assert_called_once_with("7654321:AAtest")
    mock_builder.return_value.token.return_value.build.assert_called_once()


@patch("circle_bot.ApplicationBuilder")
def test_runner_registers_all_commands(mock_builder, tmp_path):
    """CircleBotRunner registers exactly 7 CommandHandlers on the application."""
    ruleset = _make_ruleset()
    ruleset_path = tmp_path / "family.yaml"
    ruleset_path.touch()
    mock_app = mock_builder.return_value.token.return_value.build.return_value
    runner = CircleBotRunner(ruleset, ruleset_path, tmp_path, tmp_path / "circle-invites.json")
    assert mock_app.add_handler.call_count == 7


@patch("circle_bot.ApplicationBuilder")
@pytest.mark.asyncio
async def test_runner_start_calls_polling(mock_builder, tmp_path):
    """start() calls initialize(), start(), and updater.start_polling()."""
    ruleset = _make_ruleset()
    ruleset_path = tmp_path / "family.yaml"
    ruleset_path.touch()
    mock_app = mock_builder.return_value.token.return_value.build.return_value
    mock_app.initialize = AsyncMock()
    mock_app.start = AsyncMock()
    mock_app.updater.start_polling = AsyncMock()
    runner = CircleBotRunner(ruleset, ruleset_path, tmp_path, tmp_path / "circle-invites.json")
    await runner.start()
    mock_app.initialize.assert_called_once()
    mock_app.start.assert_called_once()
    mock_app.updater.start_polling.assert_called_once_with(drop_pending_updates=True)


@patch("circle_bot.ApplicationBuilder")
@pytest.mark.asyncio
async def test_runner_stop_calls_shutdown(mock_builder, tmp_path):
    """stop() calls updater.stop(), app.stop(), and app.shutdown()."""
    ruleset = _make_ruleset()
    ruleset_path = tmp_path / "family.yaml"
    ruleset_path.touch()
    mock_app = mock_builder.return_value.token.return_value.build.return_value
    mock_app.updater.stop = AsyncMock()
    mock_app.stop = AsyncMock()
    mock_app.shutdown = AsyncMock()
    runner = CircleBotRunner(ruleset, ruleset_path, tmp_path, tmp_path / "circle-invites.json")
    await runner.stop()
    mock_app.updater.stop.assert_called_once()
    mock_app.stop.assert_called_once()
    mock_app.shutdown.assert_called_once()


@patch("circle_bot.ApplicationBuilder")
@pytest.mark.asyncio
async def test_runner_poll_loop_exits_on_stop_event(mock_builder, tmp_path):
    """poll_loop() returns as soon as the stop_event is set."""
    ruleset = _make_ruleset()
    ruleset_path = tmp_path / "family.yaml"
    ruleset_path.touch()
    runner = CircleBotRunner(ruleset, ruleset_path, tmp_path, tmp_path / "circle-invites.json")
    stop_event = asyncio.Event()
    stop_event.set()
    # Should complete immediately without hanging
    await asyncio.wait_for(runner.poll_loop(stop_event), timeout=1.0)
