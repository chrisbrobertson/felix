---
specmas: 3.0
kind: feature
id: feat-slack-interface
version: 1.0.0
status: open
created: 2026-04-12
updated: 2026-04-18
complexity: high
maturity: 2
parent_system: second-brain
related_specs:
  - feat-slack-scanner
  - feat-chat-handler
---

# Slack Interface (Bidirectional)

## Overview

### Problem Statement

The second brain's interactive interface is currently Telegram-only. Users who spend their day in Slack must context-switch to Telegram to run `/commitments`, `/search`, `/briefing`, or any other command. For users where Slack is the primary communication surface, this friction reduces the second brain's utility.

Additionally, the current `chat_handler.py` is tightly coupled to `python-telegram-bot`, making it impossible to add a second transport without duplicating ~6000 lines of command logic. The Slack scanner (`slack_scanner.py`) and any future Slack chat adapter would also duplicate Slack API infrastructure (rate limiting, user resolution, token management) if built independently.

### Goal

1. **Adapter architecture** — extract the command logic from `chat_handler.py` into a transport-agnostic core (`command_core.py`), then implement Telegram and Slack as thin adapters over it. Future transports (MCP server, REST API) can be added by implementing a single `TransportAdapter` protocol.

2. **Shared Slack infrastructure** — extract common Slack API helpers (`SlackClient`) from the scanner into a shared module consumed by both the scanner and the chat adapter.

3. **Slack chat interface** — a new `slack_adapter.py` delivers the full command surface in a Slack DM using Socket Mode (no public URL required).

---

## Architecture

### Layer Overview

```
┌─────────────────────────────────────────────────┐
│              TransportAdapter protocol           │
│  telegram_adapter.py     slack_adapter.py  ...  │
│       (Telegram)             (Slack)       MCP  │
└──────────────────┬──────────────────────────────┘
                   │  CommandContext
┌──────────────────▼──────────────────────────────┐
│           command_core.py (CommandRouter)        │
│   COMMAND_REGISTRY, _load_context(), cmd_*,      │
│   handle_message(), LLM tool dispatch            │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│              slack_client.py (SlackClient)       │
│  api_call(), resolve_user(), list_channels()     │
│  Used by: slack_scanner.py + slack_adapter.py   │
└─────────────────────────────────────────────────┘
```

---

### `transport.py` — Adapter Protocol (new)

```python
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, runtime_checkable

@dataclass
class CommandContext:
    """Passed from every adapter into CommandRouter for each user interaction."""
    args: list[str]              # Parsed command arguments (empty list for free-text)
    user_id: str                 # Transport-specific user identifier
    reply: Callable[[str], Awaitable[None]]  # Send text back to user
    send_typing: Callable[[], Awaitable[None]]  # Show typing indicator

@runtime_checkable
class TransportAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: str, text: str) -> None: ...
    async def send_typing(self, chat_id: str) -> None: ...
    def max_message_length(self) -> int: ...
```

---

### `command_core.py` — Transport-Agnostic Command Core (new)

Extracted from `chat_handler.py`:

- **`COMMAND_REGISTRY`** — moved here; single source of truth for all command names and descriptions
- **`CommandRouter`** class:
  - Holds `SkillExecutor`, `GoalManager`, scanner refs, notification callback, chat history
  - `handle_message(ctx: CommandContext, text: str)` — free-text LLM chat
  - `dispatch_command(ctx: CommandContext, command: str)` — routes to `cmd_*`
  - All `cmd_*` methods accept `CommandContext` instead of `Update`
  - `_load_context(query, history)` — memory context assembly (unchanged)
  - LLM tool dispatch via `SkillExecutor.run_with_tools()` (unchanged)
- Chat history keyed by `"{transport_prefix}:{user_id}"` to isolate per-transport context

**Migration approach**: Commands are migrated incrementally. The compatibility shim in `chat_handler.py` continues forwarding to the old Telegram-coupled implementations until each command is ported to `CommandContext`. Tests run against `CommandRouter` directly, without any transport.

---

### `telegram_adapter.py` — Telegram Adapter (new, refactored from `chat_handler.py`)

- Wraps `python-telegram-bot` `ApplicationBuilder`, `Update`, `CommandHandler`, `MessageHandler`
- Translates `Update` → `CommandContext` → calls `CommandRouter`
- `send_text` → `bot.send_message()`, chunked at 4096 chars
- `send_typing` → `ChatAction.TYPING`
- `_check_auth()` validates `update.effective_user.id` against `config.user.telegram_user_id`
- `start()` / `stop()` manage `Application.run_polling()` lifecycle
- `chat_handler.py` becomes a backward-compatible shim importing from here

---

### `slack_client.py` — Shared Slack API Client (new, extracted from `slack_scanner.py`)

Single `SlackClient` class used by both the scanner (user token) and the chat adapter (bot token). Accepts token at construction — same interface regardless of token type.

**Extracted from `SlackScanner`:**
- `api_call(method, params) -> Optional[dict]` — rate-limit-aware (429 retry with `Retry-After`), 401 detection, `"ok": false` handling
- `resolve_user(user_id) -> str` — display name lookup with in-memory `_user_cache`
- `list_channels() -> list[tuple[str, str]]` — paginated `users.conversations` enumeration

**New in `SlackClient`:**
- Backed by `slack_sdk.web.async_client.AsyncWebClient` instead of raw `httpx`
- `post_message(channel, text) -> bool` — `chat_postMessage` for the adapter
- Token provided at init: scanner uses `SLACK_USER_TOKEN` (xoxp-), adapter uses `slack.bot_token` (xoxb-)

**`slack_scanner.py` after refactor:**
- Replaces all direct `httpx` `GET` calls with `self._client.api_call(...)`
- Replaces `self._user_cache` / `self._resolve_user()` with `self._client.resolve_user()`
- Replaces `self._list_channels()` with `self._client.list_channels()`
- No change to state file format, memory output, or scan logic
- All existing `test_slack_scanner.py` tests pass unchanged

---

### `slack_adapter.py` — Slack Chat Adapter (new)

```python
class SlackTransportAdapter:
    """TransportAdapter implementation for Slack DM via Socket Mode."""

    def __init__(self, router: CommandRouter, bot_token: str, app_token: str, user_id: str):
        self._router = router
        self._client = SlackClient(token=bot_token)  # shared infrastructure
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._authorized_user_id = user_id

    async def start(self): await self._handler.start_async()
    async def stop(self): await self._handler.close_async()

    async def send_text(self, chat_id: str, text: str):
        for chunk in self._chunk(text):
            await self._client.post_message(chat_id, chunk)

    def max_message_length(self) -> int: return 4000

    def _chunk(self, text: str) -> list[str]: ...  # split at 4000

    async def _on_message(self, event, say):
        """Handles all DM messages — dispatches to CommandRouter."""
        if event.get("user") != self._authorized_user_id:
            return  # auth check
        text = event.get("text", "").strip()
        channel = event["channel"]

        ctx = CommandContext(
            args=[],
            user_id=self._authorized_user_id,
            reply=lambda t: self.send_text(channel, t),
            send_typing=lambda: None,  # Slack has no typing indicator in DMs
        )

        if text.startswith("!"):
            # Command dispatch: "!commitments 3" → command="commitments", args=["3"]
            parts = text[1:].split()
            command, ctx.args = parts[0].lower(), parts[1:]
            await self._router.dispatch_command(ctx, command)
        else:
            await self._router.handle_message(ctx, text)
```

**Command prefix:** `!command args` in Slack DMs. Avoids Slack's built-in slash command system, which requires a public Request URL even for Socket Mode interactivity. Users type `!search quantum computing` rather than `/search quantum computing`.

---

### `notification_manager.py` — Multi-Transport Notifications (refactored)

- `__init__` accepts `transports: list[TransportAdapter]` instead of `telegram.Bot`
- `send_message(text)` iterates over all active transports and calls `send_text(chat_id, text)` on each
- Each adapter stores its own `chat_id` (Telegram stores the user's chat_id from state; Slack stores the DM channel ID discovered on first message)
- `_chunk_message()` calls each adapter's `max_message_length()` during dispatch

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | Free-text messages in a Slack DM with the bot produce LLM-backed responses identical to Telegram |
| FR-2 | `!command args` in a Slack DM dispatches to the same command logic as Telegram |
| FR-3 | Proactive notifications (briefings, commitment alerts, pre-meeting context) are delivered to Slack when Slack is an active transport |
| FR-4 | When `chat.transports: [telegram, slack]`, both receive all notifications simultaneously |
| FR-5 | Chat history is maintained per-transport so Slack and Telegram conversations don't bleed into each other |
| FR-6 | Phase 0 (core extraction) produces zero behavior change — all 912 existing tests pass |
| FR-7 | `slack_scanner.py` behavior is unchanged after migration to `SlackClient` |
| FR-8 | Unauthorized Slack users (not `slack.user_id`) receive no response |

---

## Auth / Token Model

| Token | Type | Used by | Config key |
|-------|------|---------|------------|
| `xoxp-…` | User token | `slack_scanner.py` (read channels) | `SLACK_USER_TOKEN` env var (existing) |
| `xoxb-…` | Bot token | `slack_adapter.py` (send/receive DMs) | `slack.bot_token` in config.yaml |
| `xapp-…` | App-level token | Socket Mode WebSocket | `slack.app_token` in config.yaml |

**Slack App setup required:**
1. Create a Slack App at api.slack.com with Socket Mode enabled
2. Bot token scopes: `chat:write`, `im:history`, `im:read`, `im:write`
3. App-level token scopes: `connections:write`
4. Install app to workspace → add bot to DM with yourself

---

## Config Schema

```yaml
chat:
  # Previously a string "telegram" — now a list to support multiple simultaneous transports
  transports: [telegram]    # any combo of: telegram, slack

slack:
  bot_token: xoxb-...       # Slack bot token for chat adapter
  app_token: xapp-...       # Slack app-level token (Socket Mode)
  user_id: U12345678        # Authorized Slack user ID for chat (not scanner)

# Existing section — unchanged:
slack_scanner:
  channel_include: []
  channel_exclude: []
  lookback_days: 7
  max_thread_age_days: 30
  interval_seconds: 300
```

**Backward compatibility:** `chat.transport: telegram` (old string form) is accepted and treated as `chat.transports: [telegram]`.

---

## Phased Delivery

### Phase 0 — Core extraction + adapter protocol
**Scope:** Transport-agnostic foundation. No behavior change.
**New files:** `transport.py`, `command_core.py`, `telegram_adapter.py`
**Modified:** `chat_handler.py` (becomes shim), `notification_manager.py` (accepts `TransportAdapter`)
**Migrated commands (10):** `search`, `readings`, `reading`, `commitments`, `complete`, `dismiss`, `briefing`, `goals`, `projects`, `help`
**Acceptance:** All 912 existing tests pass. `TelegramChatHandler` in `chat_handler.py` still importable.

### Phase 1 — Shared Slack client + scanner migration
**Scope:** Extract `SlackClient`, migrate scanner off raw `httpx`.
**New files:** `slack_client.py`
**Modified:** `slack_scanner.py`
**New dependency:** `slack_sdk>=3.19`
**Acceptance:** All existing `test_slack_scanner.py` tests pass. No change to memory files or state.

### Phase 2 — Slack Socket Mode adapter
**Scope:** New `slack_adapter.py` connects and can receive/send DMs.
**New files:** `slack_adapter.py`
**Modified:** `daemon.py`, `install.sh`, `requirements.txt`
**New dependency:** `slack_bolt[async]>=1.18`
**Config additions:** `slack.bot_token`, `slack.app_token`, `slack.user_id`, `chat.transports`
**Acceptance:** Sending `!help` in Slack DM returns the help text. Free-text chat works.

### Phase 3 — Complete command migration
**Scope:** Port remaining ~70 commands to `CommandContext`.
**Modified:** `command_core.py` (add commands), `chat_handler.py` (shrinks toward removal)
**Acceptance:** Every command listed in `COMMAND_REGISTRY` works over both Telegram and Slack.

### Phase 4 — Notifications via all transports
**Scope:** `NotificationManager` dispatches to all active transports.
**Modified:** `notification_manager.py`
**Acceptance:** With `transports: [telegram, slack]`, briefings arrive on both platforms.

---

## Out of Scope

- **Slack slash commands** (e.g., `/secondbrain`) — require a public Request URL even with Socket Mode interactivity; use `!command` prefix instead
- **Rich Slack UI** (Block Kit buttons, modals) — plain text first
- **Multi-user / workspace-wide** deployment — single authorized user only
- **Migrating `slack_scanner.py` to `slack_bolt`** — scanner uses `SlackClient` (wrapping `slack_sdk`), not the full `slack_bolt` event framework
- **Threaded replies in Slack** — all responses go to the DM as flat messages, matching Telegram behavior
- **MCP / REST adapter implementation** — architecture supports it via `TransportAdapter` but not specified here
- **Slack file uploads** — `/import_chats` over Slack not in scope for initial delivery

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `transport.py` | New — `TransportAdapter` protocol, `CommandContext` dataclass |
| `command_core.py` | New — `CommandRouter`, all `cmd_*` methods, `COMMAND_REGISTRY` |
| `telegram_adapter.py` | New — Telegram `TransportAdapter` extracted from `chat_handler.py` |
| `slack_client.py` | New — `SlackClient` with `api_call`, `resolve_user`, `list_channels`, `post_message` |
| `slack_adapter.py` | New — `SlackTransportAdapter` using Socket Mode + `SlackClient` |
| `slack_scanner.py` | Refactor — replace raw `httpx` with `SlackClient`; no functional change |
| `chat_handler.py` | Refactor — thin shim delegating to `telegram_adapter` + `command_core` |
| `notification_manager.py` | Refactor — accept `list[TransportAdapter]`, dispatch to all |
| `daemon.py` | Modify — instantiate adapters from `chat.transports` config |
| `install.sh` | Modify — add `transport.py`, `command_core.py`, `telegram_adapter.py`, `slack_client.py`, `slack_adapter.py` to `DAEMON_FILES` |
| `requirements.txt` | Modify — add `slack_sdk>=3.19`, `slack_bolt[async]>=1.18` |

---

## Testing Strategy

- **Phase 0**: New `tests/unit/test_command_core.py` — test `CommandRouter` commands via `CommandContext` with a mock `reply` callable. No Telegram or Slack imports needed.
- **Phase 1**: Existing `tests/unit/test_slack_scanner.py` must pass unchanged. New `tests/unit/test_slack_client.py` tests `SlackClient` with mocked `AsyncWebClient`.
- **Phase 2**: New `tests/unit/test_slack_adapter.py` tests `_on_message` routing using mocked `CommandRouter`.
- **Phase 4**: Integration test verifies `NotificationManager` calls `send_text` on both a mock Telegram and mock Slack adapter.
