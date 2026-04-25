"""Unit tests for command_core.CommandRouter and COMMAND_REGISTRY."""
import pytest
from unittest.mock import AsyncMock

from command_core import CommandRouter, COMMAND_REGISTRY
from transport import CommandContext


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _noop_typing():
    pass


def _make_ctx(args=None, reply=None):
    replies = []

    async def _reply(text):
        replies.append(text)

    return CommandContext(
        args=args or [],
        user_id="U1",
        reply=reply or _reply,
        send_typing=_noop_typing,
    ), replies


# ── register / dispatch ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_known_command_calls_handler():
    router = CommandRouter()
    called = []

    async def handler(ctx):
        called.append(ctx)

    router.register("ping", handler)
    ctx, _ = _make_ctx()
    result = await router.dispatch_command(ctx, "ping")

    assert result is True
    assert len(called) == 1


@pytest.mark.asyncio
async def test_dispatch_unknown_command_returns_false():
    router = CommandRouter()
    ctx, _ = _make_ctx()
    result = await router.dispatch_command(ctx, "nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_dispatch_is_case_insensitive():
    router = CommandRouter()
    called = []

    async def handler(ctx):
        called.append(True)

    router.register("Ping", handler)
    ctx, _ = _make_ctx()
    await router.dispatch_command(ctx, "PING")
    assert called


@pytest.mark.asyncio
async def test_dispatch_handler_exception_replies_error_and_returns_true():
    """A handler that raises must not propagate — router catches, replies, returns True."""
    router = CommandRouter()

    async def bad_handler(ctx):
        raise ValueError("boom")

    router.register("explode", bad_handler)
    ctx, replies = _make_ctx()
    result = await router.dispatch_command(ctx, "explode")

    assert result is True
    assert any("Internal error" in r for r in replies)


@pytest.mark.asyncio
async def test_register_all_registers_multiple_handlers():
    router = CommandRouter()
    called = {}

    async def h_a(ctx):
        called["a"] = True

    async def h_b(ctx):
        called["b"] = True

    router.register_all({"alpha": h_a, "beta": h_b})

    ctx_a, _ = _make_ctx()
    ctx_b, _ = _make_ctx()
    await router.dispatch_command(ctx_a, "alpha")
    await router.dispatch_command(ctx_b, "beta")

    assert called == {"a": True, "b": True}


# ── handle_message ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_message_calls_message_handler():
    router = CommandRouter()
    called = []

    async def msg_handler(ctx):
        called.append(ctx)

    router.register("__message__", msg_handler)
    ctx, _ = _make_ctx()
    await router.handle_message(ctx, "hello")

    assert len(called) == 1


@pytest.mark.asyncio
async def test_handle_message_noop_when_no_message_handler():
    """handle_message must not raise when __message__ is not registered."""
    router = CommandRouter()
    ctx, _ = _make_ctx()
    await router.handle_message(ctx, "hello")  # should not raise


# ── format_help ───────────────────────────────────────────────────────────────

def test_format_help_plain_text_contains_all_groups():
    router = CommandRouter()
    text = router.format_help(use_markdown=False)
    for group in COMMAND_REGISTRY:
        assert group in text


def test_format_help_plain_text_contains_slash_commands():
    router = CommandRouter()
    text = router.format_help(use_markdown=False)
    assert "/help" in text
    assert "/search" in text


def test_format_help_markdown_bolds_group_names():
    router = CommandRouter()
    text = router.format_help(use_markdown=True)
    for group in COMMAND_REGISTRY:
        assert f"*{group}*" in text


def test_format_help_no_trailing_whitespace():
    router = CommandRouter()
    text = router.format_help(use_markdown=False)
    assert text == text.rstrip()


# ── COMMAND_REGISTRY ──────────────────────────────────────────────────────────

def test_command_registry_has_expected_groups():
    expected = {
        "Knowledge listings", "Commitments", "Goals", "Projects",
        "Review", "Agent actions", "Notifications", "Domain filter",
        "Deduplication", "Watchlists", "Import", "Feature Requests",
        "Skill Management", "Reports", "Circles", "Meta", "System",
    }
    assert expected == set(COMMAND_REGISTRY.keys())


def test_command_registry_entries_are_two_tuples():
    for group, entries in COMMAND_REGISTRY.items():
        for entry in entries:
            assert len(entry) == 2, f"Entry in {group!r} is not a 2-tuple: {entry}"
            cmd, desc = entry
            assert isinstance(cmd, str) and cmd
            assert isinstance(desc, str) and desc


def test_command_registry_help_is_registered():
    all_cmds = {cmd for entries in COMMAND_REGISTRY.values() for cmd, _ in entries}
    assert "help" in all_cmds
    assert "search" in all_cmds
    assert "readings" in all_cmds
