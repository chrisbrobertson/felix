---
specmas: 3.0
kind: feature
id: feat-slack-interface
version: 2.0.0
status: implemented
created: 2026-04-12
updated: 2026-04-19
complexity: high
maturity: 5
parent_system: second-brain
related_specs:
  - feat-slack-scanner
  - feat-chat-handler
---

# Slack Interface (Bidirectional)

## Overview

### Problem Statement

The second brain's interactive interface was Telegram-only. Users who spend their day in Slack had to context-switch to Telegram to run `/commitments`, `/search`, `/briefing`, or any other command. Additionally, `chat_handler.py` was tightly coupled to `python-telegram-bot`, making it impossible to add a second transport without duplicating ~6000 lines of command logic.

### Goal

1. **Adapter architecture** — introduce a `TransportAdapter` protocol and `CommandRouter` so future transports can be added by implementing a single interface.
2. **Shared Slack infrastructure** — extract common Slack API helpers (`SlackClient`) from the scanner into a shared module.
3. **Slack chat interface** — a new `slack_adapter.py` delivers the full command surface in a Slack DM via Socket Mode (no public URL required).

---

## Architecture

### Layer Overview

```
┌──────────────────────────────────────────────────────────┐
│               TransportAdapter protocol                   │
│   telegram_adapter.py        slack_adapter.py      ...   │
│       (TelegramAdapter)    (SlackTransportAdapter)  MCP  │
└──────────────┬───────────────────────┬────────────────────┘
               │  CommandContext        │  CommandContext
               ▼                       ▼
┌──────────────────────────────────────────────────────────┐
│          command_core.py (CommandRouter)                  │
│   COMMAND_REGISTRY, dispatch_command(), handle_message() │
│   Handlers registered at startup via register_with_router│
└──────────────────────────────────────────────────────────┘
               │  delegates to
               ▼
┌──────────────────────────────────────────────────────────┐
│          chat_handler.py (TelegramChatHandler)            │
│   All cmd_* methods — called via bridge from any adapter  │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│              slack_client.py (SlackClient)                │
│  api_call(), resolve_user(), list_channels(), post_msg() │
│  Used by: slack_scanner.py + slack_adapter.py            │
└──────────────────────────────────────────────────────────┘
```

### Design Decision: Bridge Pattern (not individual command migration)

Rather than individually migrating all 90+ `cmd_*` methods to `CommandContext` signatures, `TelegramChatHandler.register_with_router(router)` registers them all via a bridge:

- For each `cmd_*` method, a wrapper closure creates lightweight fake Telegram `Update` and `Context` objects wired to `ctx.reply` and `ctx.args`.
- `update.effective_user.id` is set to `self.allowed_user_id` so `_check_auth()` always passes — auth is enforced at the adapter boundary before the router is called.
- `update.message.reply_text(text)` delegates to `await ctx.reply(text)`.
- `update.message.set_reaction()`, `context.bot.send_chat_action()`, and similar Telegram-specific calls are no-ops.
- Free-text messages register a `__message__` handler that delegates to `TelegramChatHandler.handle_message()` via the same fake objects; `CommandContext.raw_text` carries the original message text.

This approach delivers full command parity over Slack without touching the existing `cmd_*` implementations. The trade-off is that `chat_handler.py` retains its internal Telegram dependency.

---

### `transport.py` — Adapter Protocol

```python
@dataclass
class CommandContext:
    """Passed from every adapter into CommandRouter for each user interaction."""
    args: list[str]                                # Parsed command arguments
    user_id: str                                   # Transport-specific user identifier
    reply: Callable[[str], Awaitable[None]]        # Send text back to user
    send_typing: Callable[[], Awaitable[None]]     # Show typing indicator (no-op ok)
    raw_text: str = ""                             # Original free-text message (non-command)

@runtime_checkable
class TransportAdapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: str, text: str) -> None: ...
    async def send_typing(self, chat_id: str) -> None: ...
    def max_message_length(self) -> int: ...
```

---

### `command_core.py` — Command Router

A thin routing layer. Does **not** hold command implementations — those remain in `TelegramChatHandler`.

- **`COMMAND_REGISTRY`** — moved here; single source of truth for all command names and descriptions
- **`CommandRouter`** class:
  - `register(name, handler)` / `register_all(mapping)` — register command handlers
  - `dispatch_command(ctx, command) -> bool` — routes to registered handler; returns `False` if unknown
  - `handle_message(ctx, text)` — delegates to `__message__` handler if registered
  - `format_help(use_markdown)` — renders `COMMAND_REGISTRY` as text or Markdown

Handlers are registered at daemon startup by `TelegramChatHandler.register_with_router(router)`.

---

### `telegram_adapter.py` — Telegram Adapter

Wraps `TelegramChatHandler` to implement `TransportAdapter`:

- `start()` / `stop()` delegate to handler's polling lifecycle
- `send_text(chat_id, text)` → `bot.send_message()`, chunked at 4096
- `send_typing(chat_id)` → `ChatAction.TYPING`
- `get_chat_id()` returns the known DM chat ID (from handler or notification state)

`chat_handler.py` is **not** a shim — it is still the primary implementation. `TelegramAdapter` adds the `TransportAdapter` interface on top.

---

### `slack_client.py` — Shared Slack API Client

Single `SlackClient` class used by both the scanner (user token) and the chat adapter (bot token). Backed by raw `httpx` (no `slack_sdk` dependency added).

- `api_call(method, params) -> Optional[dict]` — rate-limit-aware (429 retry with `Retry-After`), 401 detection, `"ok": false` handling
- `resolve_user(user_id) -> str` — display name lookup with in-memory `_user_cache`
- `list_channels() -> list[tuple[str, str]]` — paginated `users.conversations` enumeration
- `post_message(channel, text) -> bool` — `chat.postMessage` for the adapter
- `clear_user_cache()` — clears the in-memory cache

Token is provided at init: scanner uses `SLACK_USER_TOKEN` (`xoxp-`), adapter uses `slack.bot_token` (`xoxb-`).

**`slack_scanner.py` after refactor:**
- Replaces `self._api_call()`, `self._resolve_user()`, `self._list_channels()` with `self._client.*`
- No change to state file format, memory output, or scan logic

---

### `slack_adapter.py` — Slack Chat Adapter

```python
class SlackTransportAdapter:
    """TransportAdapter implementation for Slack DM via Socket Mode."""

    def __init__(self, router: CommandRouter, bot_token: str, app_token: str, user_id: str):
        self._router = router
        self._client = SlackClient(token=bot_token)
        self._authorized_user_id = user_id
        self._dm_channel_id: Optional[str] = None  # discovered on first message

    async def start(self):
        # Lazy import of slack_bolt — graceful degradation if not installed
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        await self._handler.start_async()

    async def _on_message(self, event, say):
        if event.get("bot_id") or event.get("subtype"):
            return
        if event.get("user") != self._authorized_user_id:
            return  # auth check at adapter boundary

        channel = event["channel"]
        text = event.get("text", "").strip()
        if self._dm_channel_id is None:
            self._dm_channel_id = channel  # cache for proactive notifications

        if text.startswith("!"):
            parts = text[1:].split()
            command, args = parts[0].lower(), parts[1:]
            ctx = CommandContext(args=args, user_id=self._authorized_user_id,
                                 reply=lambda t: self.send_text(channel, t),
                                 send_typing=noop)
            handled = await self._router.dispatch_command(ctx, command)
            if not handled:
                await ctx.reply(f"Unknown command: !{command}\nSend !help for a list.")
        else:
            ctx = CommandContext(args=[], user_id=self._authorized_user_id,
                                 reply=lambda t: self.send_text(channel, t),
                                 send_typing=noop, raw_text=text)
            await self._router.handle_message(ctx, text)
```

**Command prefix:** `!command args` in Slack DMs. Avoids Slack's built-in slash command system, which requires a public Request URL even for Socket Mode interactivity.

**`get_chat_id()`** returns `self._dm_channel_id` — set on first inbound message; used by `NotificationManager` for proactive delivery.

---

### `notification_manager.py` — Multi-Transport Notifications

`__init__` now accepts `transports: list[TransportAdapter]` alongside the legacy `bot=` parameter.

`send_message(text)` iterates all active transports:
```python
for adapter in self._transports:
    chat_id = adapter.get_chat_id()
    if chat_id:
        for chunk in _chunk(text, adapter.max_message_length()):
            await adapter.send_text(chat_id, chunk)
```

Falls back to the legacy `self.bot` path when `_transports` is empty (watcher role, no Slack configured).

---

### `daemon.py` — Wiring

At startup, `full` role:

1. `_build_slack_adapter(config, chat)` — reads `chat.transports` config; if `"slack"` is listed and credentials are present, builds `SlackTransportAdapter` and calls `chat.register_with_router(router)` to populate the router with all commands.
2. `TelegramAdapter(chat)` is always constructed.
3. Both adapters are passed to `NotificationManager(transports=[tg_adapter, slack_adapter])`.
4. `slack_adapter.start` is added to the async task list.

---

## Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR-1 | Free-text messages in a Slack DM produce LLM-backed responses | ✅ |
| FR-2 | `!command args` dispatches to the same command logic as Telegram | ✅ |
| FR-3 | Proactive notifications are delivered to Slack when configured | ✅ |
| FR-4 | With `transports: [telegram, slack]`, both receive all notifications simultaneously | ✅ |
| FR-5 | Slack and Telegram share the same `chat-history.json` (keyed by Telegram `allowed_user_id`) | ✅ (shared; not per-transport isolated) |
| FR-6 | Core extraction produces zero behavior change — all existing tests pass | ✅ |
| FR-7 | `slack_scanner.py` behavior is unchanged after migration to `SlackClient` | ✅ |
| FR-8 | Unauthorized Slack users (not `slack.user_id`) receive no response | ✅ |

> **Note on FR-5:** Chat history is shared across transports rather than isolated per-transport. Both Telegram and Slack conversations write to and read from the same history, keyed by `TelegramChatHandler.allowed_user_id`. This is simpler and maintains continuity when switching between interfaces.

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
  # Previously a string "telegram" — now a list to support multiple simultaneous transports.
  # Backward compatible: "telegram" (string) is accepted and treated as ["telegram"].
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

---

## Phased Delivery

### Phase 0 — Core extraction + adapter protocol ✅
**New files:** `transport.py`, `command_core.py`, `telegram_adapter.py`
**Modified:** `notification_manager.py` (accepts `transports` list)
**What shipped:** `TransportAdapter` protocol, `CommandContext` dataclass, `CommandRouter` (routing only — no command implementations), `COMMAND_REGISTRY` as single source of truth, `TelegramAdapter` wrapper, multi-transport `send_message`.
**What was NOT done:** `chat_handler.py` was not turned into a shim; no individual commands were migrated to `CommandContext` signatures.

### Phase 1 — Shared Slack client + scanner migration ✅
**New files:** `slack_client.py`
**Modified:** `slack_scanner.py`
**Dependencies:** No new packages — `SlackClient` uses `httpx` (already present), not `slack_sdk`.
**What shipped:** `SlackClient` with `api_call`, `resolve_user`, `list_channels`, `post_message`, `clear_user_cache`; `slack_scanner.py` migrated to use `self._client` for all API calls.

### Phase 2 — Slack Socket Mode adapter ✅
**New files:** `slack_adapter.py`
**Modified:** `daemon.py`, `install.sh`, `requirements.txt`, `config.yaml.template`
**New dependency:** `slack_bolt[async]>=1.18` (lazy import — graceful if missing)
**What shipped:** `SlackTransportAdapter` with Socket Mode, `!command` dispatch stub, DM channel discovery for proactive notifications.

### Phase 3 — Command bridge ✅
**Modified:** `chat_handler.py`, `transport.py`, `slack_adapter.py`, `daemon.py`
**What shipped:** `TelegramChatHandler.register_with_router(router)` — registers all 90+ `cmd_*` methods via fake Telegram object bridge; `CommandContext.raw_text` field for free-text delegation; `daemon.py` calls `register_with_router` at startup.

### Phase 4 — Multi-transport notifications ✅
**Modified:** `daemon.py`
**What shipped:** `daemon.py` builds `TelegramAdapter` + `SlackTransportAdapter` (if configured) and passes both to `NotificationManager(transports=[...])`. Briefings, alerts, and goal notifications are delivered to all active transports simultaneously.

---

## Files Created / Modified

| File | Change |
|------|--------|
| `transport.py` | New — `TransportAdapter` protocol, `CommandContext` dataclass (with `raw_text`) |
| `command_core.py` | New — `CommandRouter` (routing only), `COMMAND_REGISTRY` |
| `telegram_adapter.py` | New — `TelegramAdapter` wrapping `TelegramChatHandler` |
| `slack_client.py` | New — `SlackClient` with `api_call`, `resolve_user`, `list_channels`, `post_message` |
| `slack_adapter.py` | New — `SlackTransportAdapter` using Socket Mode + `SlackClient` |
| `slack_scanner.py` | Refactor — replace raw `httpx` with `SlackClient`; no functional change |
| `chat_handler.py` | Extended — `register_with_router()` bridge method added; otherwise unchanged |
| `notification_manager.py` | Refactor — accepts `list[TransportAdapter]`, dispatches to all |
| `daemon.py` | Modified — builds adapters from `chat.transports` config, passes to NotificationManager |
| `install.sh` | Modified — added `transport.py`, `command_core.py`, `telegram_adapter.py`, `slack_client.py`, `slack_adapter.py` to `DAEMON_FILES` |
| `requirements.txt` | Modified — added `slack_bolt[async]>=1.18` |

---

## Out of Scope

- **Slack slash commands** — require a public Request URL; use `!command` prefix instead
- **Rich Slack UI** (Block Kit buttons, modals) — plain text only
- **Multi-user / workspace-wide** deployment — single authorized user
- **Migrating `slack_scanner.py` to `slack_bolt`** — scanner uses `SlackClient` (raw `httpx`), not `slack_bolt`
- **Threaded Slack replies** — all responses go to the DM as flat messages
- **MCP / REST adapter** — architecture supports it via `TransportAdapter` but not implemented
- **Per-transport chat history isolation** — both transports share the same history
- **Individual `cmd_*` migration to `CommandContext`** — commands use the bridge pattern instead; `chat_handler.py` retains its Telegram-specific internals

---

## Testing

- `tests/unit/test_command_core.py` — `CommandRouter` routing logic
- `tests/unit/test_slack_client.py` — `SlackClient` with mocked `httpx` (17 tests)
- `tests/unit/test_slack_scanner.py` — scanner behavior unchanged after `SlackClient` migration
- `tests/unit/test_slack_adapter.py` — `_on_message` routing, auth, chunking, channel caching (9 tests)
- `tests/unit/test_chat_handler.py` — `register_with_router` bridge: completeness, `__message__` handler, args forwarding, auth bypass (5 tests)
- `tests/integration/test_pipeline.py` — end-to-end scanner memory write with mocked `SlackClient`
