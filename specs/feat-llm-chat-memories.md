---
specmas: 3.0
kind: feature
id: feat-llm-chat-memories
version: 1.0.0
created: 2026-04-25
status: implemented
shipped_version: "1.8.0"
complexity: simple
maturity: 0
parent_system: second-brain
related_specs:
  - feat-llm-chat-import
  - feat-chat-handler
  - feat-proactive-notifications
---

# LLM Chat Memories — Refresh Nudge & Integration

## Overview

### Problem Statement

`feat-llm-chat-import` shipped in v1.4.0 with `/import_chats <platform>` for one-shot import of Claude and ChatGPT export files. In practice the feature is unused: there's no reminder to re-export periodically, and once memories exist there is no first-class way to browse them. They are invisible inside `/comms` (FR-12 of the original spec was never implemented), invisible to `/code`/`/meetings`-style list commands, and the `search_memories` tool has no awareness of `type: llm_chat`. The result is a write-only graveyard.

This sub-spec closes that loop with three additions: a periodic refresh nudge, a dedicated `/aichat` browse command, and integration into existing `/comms` and `search_memories` surfaces. **Out of scope: any browser scraping, DOM capture, or attempt to bypass the manual export step.** The user still re-exports manually; we just remind them and surface the result.

### Scope

**In scope:**
- Daily refresh-nudge check inside `notification_manager` ("Last LLM chat import was N days ago — re-export and run `/import_chats`.") with cooldown.
- `/aichat [N]` command — list view of imported chats (most-recent 20, grouped by platform); detail view (`/aichat 3`) shows summary and key topics.
- `/aichat search <query>` — keyword filter against cached headers.
- `/comms llm` filter — closes FR-12 from `feat-llm-chat-import`.
- `search_memories` tool extension — accepts `type: llm_chat` so the `chat` skill can pull llm_chat memories naturally into context.
- `/help` and `COMMAND_REGISTRY` updates per CLAUDE.md convention.
- Three config keys with safe defaults and a kill-switch.

**Out of scope:**
- Re-import automation (no API exists; manual export step is unchanged).
- Per-message granularity (conversation-level memories only).
- Browser DOM scraping or local-file readers for either vendor.
- Cross-conversation analytics ("what topics dominate my LLM use") — separate future spec if it proves valuable.
- Automatic export-file detection in iCloud or `~/Downloads`.

### Success Metrics

- User receives at most one nudge per 7 days when last `llm-chat-*.md` mtime is older than `refresh_interval_days`.
- `/aichat` lists imported chats indexed by recency, grouped by platform.
- A free-form chat query about a topic discussed in an imported llm_chat surfaces the memory in context (via `search_memories`).

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `notification_manager` exposes `_check_llm_chat_refresh(state)`; reads newest `llm-chat-*.md` mtime; if `now - mtime > llm_chat.refresh_interval_days` (default 14) and cooldown elapsed, sends the nudge during the tick and stamps `last_llm_chat_nudge` on `state` |
| FR-2 | Nudge cooldown stored in `notification-state.json` as `last_llm_chat_nudge` (ISO timestamp); minimum `llm_chat.nudge_cooldown_days` (default 7) between nudges |
| FR-3 | Nudge respects existing `is_muted` state and existing per-day briefing window logic |
| FR-4 | Nudge text identifies stalest platform(s) and includes the literal `/import_chats <platform>` hint inline |
| FR-5 | `/aichat` (no args) lists most-recent 20 imported chats grouped by platform; format `N. [platform] YYYY-MM-DD title (Ndays ago)` |
| FR-6 | `/aichat <N>` opens detail view: title, platform, created, summary, key topics, tags |
| FR-7 | `/aichat search <query>` keyword-filters by header (reuse the `/comms search` pattern already in `chat_handler`) |
| FR-8 | `/comms llm` filter shows only `type: llm_chat` memories; mirrors existing `/comms email` and `/comms slack` shape |
| FR-9 | `search_memories` tool accepts `type=llm_chat`; the `chat` skill prompt mentions it as an available type |
| FR-10 | `COMMAND_REGISTRY` entry for `/aichat`; `/help` renders it; the registry-vs-handlers test in `test_chat_handler.py` passes |
| FR-11 | Config keys: `llm_chat.refresh_interval_days` (default 14), `llm_chat.nudge_cooldown_days` (default 7), `llm_chat.nudge_enabled` (default true) — all overridable per machine |

---

## Design

### `notification_manager.py` — refresh-nudge check

Add `_check_llm_chat_refresh(state: dict)` to the 60-sec tick alongside the existing `_check_daily_briefing(state)` pattern. Note the parameter-passing style: state is loaded once per tick by the caller, mutated in place by each `_check_xxx`, and persisted once at the end. There is no `self._state` instance attribute and no `self._is_muted()` method — both are reads against the passed-in `state` dict.

```python
async def _check_llm_chat_refresh(self, state: dict) -> None:
    cfg = self.config.get("llm_chat", {})
    if not cfg.get("nudge_enabled", True):
        return
    if state.get("muted", False):
        return

    interval_days = cfg.get("refresh_interval_days", 14)
    cooldown_days = cfg.get("nudge_cooldown_days", 7)

    last_nudge = state.get("last_llm_chat_nudge")
    if last_nudge and (now() - parse(last_nudge)).days < cooldown_days:
        return

    # Helper introduced by this spec — globs MEMORIES_DIR/llm-chat-*.md and
    # parses the {platform} segment from the filename (per llm_chat_importer.py:201).
    # Filename parse avoids the cost of opening every memory to read frontmatter.
    newest = self._newest_llm_chat_mtime_by_platform()  # -> dict[platform, datetime]
    stale = [p for p, t in newest.items() if (now() - t).days > interval_days]
    missing = [p for p in ("claude", "chatgpt") if p not in newest]
    targets = stale + missing
    if not targets:
        return

    msg = (
        f"📥 LLM chat history is going stale ({', '.join(targets)}). "
        f"Re-export and run `/import_chats {targets[0]}` when you have a moment."
    )
    await self.send_message(msg)
    state["last_llm_chat_nudge"] = now().isoformat()
    # Caller persists state once per tick — do not write here.
```

### `chat_handler.py` — `/aichat` command

The existing `cmd_xxx` pattern in `chat_handler.py` (e.g. `cmd_comms` at line 4610) builds reply text inline and calls `update.message.reply_text(text)` directly — there are no `_reply_list`/`_reply_detail` helpers. This spec follows the same shape and introduces three small new format helpers (named with the `_format_aichat_*` prefix to make their newness obvious) plus one memory-loader helper.

```python
async def cmd_aichat(self, update, context):
    args = context.args or []
    # New helper this spec adds: globs MEMORIES_DIR/llm-chat-*.md, parses
    # frontmatter once per file (cached by mtime), returns a list sorted by
    # most-recent first. Each item: {"path", "platform", "title", "created",
    # "summary", "topics", "tags", "header"}.
    memories = self._llm_chat_memories()

    if args and args[0] == "search":
        query = " ".join(args[1:]).lower()
        memories = [m for m in memories if query in m["header"].lower()]
        await update.message.reply_text(self._format_aichat_list(memories[:20]))
        return

    if args and args[0].isdigit():
        idx = int(args[0]) - 1
        if not 0 <= idx < len(memories):
            await update.message.reply_text("No such entry.")
            return
        await update.message.reply_text(self._format_aichat_detail(memories[idx]))
        return

    # Default: list grouped by platform, most-recent 20.
    await update.message.reply_text(self._format_aichat_list_grouped(memories[:20]))
```

`COMMAND_REGISTRY` entry — registered under the `command_core.COMMAND_REGISTRY` dict (see `command_core.py:20`), in the `"Knowledge listings"` group alongside `/readings`, `/code`, `/meetings`:

```python
("aichat", "Browse imported Claude/ChatGPT history. /aichat | /aichat <N> | /aichat search <q>"),
```

### `chat_handler.py` — `/comms llm` filter

`cmd_comms` today (`chat_handler.py:4620`) checks the filter argument inline:

```python
if args and args[0].lower() in ("email", "slack"):
    type_filter = args[0].lower()
    args = args[1:]
```

Extend that tuple to `("email", "slack", "llm")` and add an `llm`-branch in the existing filter-application logic that maps `llm → type: llm_chat`. The hide-marketing-and-automated logic remains email-specific and does not apply to `llm`. There is no `ALLOWED_FILTERS` constant or `TYPE_FOR_FILTER` dict — both are inline today, and this spec keeps that style.

### `search_memories` tool extension

The tool schema lives in `chat_tools.py`, not `chat_handler.py`. Today `type` is declared as a free-form string with a description that enumerates allowed values:

```python
"type": {"type": "string",
         "description": "Optional type filter: email, slack, meeting, project, "
                        "commitment, event, contact, web"}
```

Add `llm_chat` to the description string. (Optional future hardening: tighten this to a JSON-Schema enum — out of scope here, called out in Open Questions.)

The `chat` skill's tool-usage guidance lives in the deployed skill file at `~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/skills/chat.md` (the source-of-truth skills directory; the repo's `skills/` is what gets deployed there). The implementation PR should update that file to mention "use `type=llm_chat` to find prior Claude or ChatGPT conversations the user has imported."

### Config (`config.yaml`)

```yaml
llm_chat:
  refresh_interval_days: 14
  nudge_cooldown_days: 7
  nudge_enabled: true
```

### README.md

- Add `/aichat` to the Telegram-commands section, between `/import_chats` and `/comms`.
- Document the nudge in the proactive-notifications section alongside briefing/commitment alerts.
- Document the three new config keys.

---

## Test Plan

**Unit tests in `tests/unit/test_notification_manager.py`:**

1. `test_llm_chat_nudge_fires_when_chats_stale` — fixture: latest `llm-chat-*.md` mtime 30 days old, no prior nudge → `send_message` called once with platform name in message; `state["last_llm_chat_nudge"]` stamped.
2. `test_llm_chat_nudge_respects_cooldown` — `state["last_llm_chat_nudge"]` set 3 days ago → `send_message` not called.
3. `test_llm_chat_nudge_respects_mute` — `state["muted"]=True`, stalest 30 days → `send_message` not called.
4. `test_llm_chat_nudge_disabled_via_config` — `nudge_enabled: false` → `send_message` not called regardless of staleness.
5. `test_llm_chat_nudge_when_no_chats_ever_imported` — empty memories dir → `send_message` called listing both platforms as missing.

**Unit tests in `tests/unit/test_chat_handler.py`:**

6. `test_cmd_aichat_list_groups_by_platform` — fixtures with 3 claude + 2 chatgpt → list groups them under platform headings.
7. `test_cmd_aichat_detail_shows_summary` — `/aichat 1` → reply contains summary and key-topics from the memory.
8. `test_cmd_aichat_search_keyword_filter` — `/aichat search rag` → only memories whose header contains "rag" are listed.
9. `test_cmd_aichat_invalid_index` — `/aichat 99` → "No such entry." reply, no exception.
10. `test_cmd_comms_llm_filter` — `/comms llm` → returns only `type: llm_chat` memories, mixed-type fixture proves filter.
11. `test_search_memories_tool_accepts_llm_chat_type` — tool schema validation passes; mock execution returns only matching type.
12. `test_command_registry_includes_aichat` — existing registry-vs-handlers test green for the new command.

---

## Open Questions

- Should `/aichat` support tag filtering (`/aichat tag rag`) on day one? Lean: no — start with platform grouping and search; add tag filter only if the corpus grows large enough to need it.
- Should the nudge also fire on the watcher role? Lean: no — notifications are full-role only per existing pattern.
