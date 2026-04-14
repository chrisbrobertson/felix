---
specmas: 3.0
kind: feature
id: feat-chat-handler
version: 1.0.0
created: 2026-04-13
status: implemented
complexity: moderate
maturity: 2
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-proactive-notifications
  - feat-commitment-tracker
  - feat-contact-tracker
  - feat-code-project-scanner
  - feat-email-scanner
  - feat-zoom-transcript-scanner
  - feat-calendar-scanner
  - feat-slack-scanner
---

# Chat Handler

## Overview

### Problem Statement

The Second Brain daemon accumulates memories from browser history, email threads,
meetings, calendar events, and Slack threads. Users need a conversational interface
to query this knowledge base in natural language, retrieve specific memories, and
manage tracked data (commitments, contacts, events, etc.).

The chat handler provides a Telegram bot that:
- Maintains conversation context across multiple turns
- Routes tool calls to specialized memory retrieval functions
- Handles network failures gracefully with a pending-reply queue
- Supports both natural language queries and slash commands

### Scope

**In Scope:**
- Second async daemon loop, always polling (`full` role only)
- Telegram bot interface with conversation history
- LLM tool-calling for memory retrieval and data browsing
- Pending-reply queue with reconnect detection
- Natural language tool dispatch for `/deliver` and `/discard` actions
- Session-scoped result sets for numbered commands
- Slash commands for all major memory types and management operations

**Out of Scope:**
- Multi-user support (single allowed_user_id only)
- Persistent conversation history across daemon restarts
- Voice message handling
- File/image uploads
- Admin commands or multi-bot orchestration

### Success Metrics

- Query response latency < 3 seconds for simple lookups
- Tool-call dispatch success rate > 95%
- Zero message loss during network outages (pending-reply queue)
- Conversation context correctly maintained for 6+ turn pairs

---

## Functional Requirements

### FR-1: Conversation History Window

Maintain in-memory conversation state per chat_id with a fixed rolling window.

**Window size:** `HISTORY_WINDOW_TURNS = 6` (last 6 user+assistant pairs = 12 messages)

**Data structure:**
```python
_chat_history: dict[int, list[dict]] = {
    chat_id: [
        {"role": "user", "content": "What projects am I working on?"},
        {"role": "assistant", "content": "You have 3 active projects..."},
        ...
    ]
}
```

**Truncation rule:**
When appending a new turn, if `len(turns) > HISTORY_WINDOW_TURNS * 2`, slice to
`turns[-max_msgs:]` to keep only the most recent messages.

**Reset:** `/reset` command clears history for that chat_id.

**Validation criteria:**
- History persists across multiple queries within a session
- Oldest messages dropped when window size exceeded
- History cleared on daemon restart (not persisted to disk)

---

### FR-2: LLM Tool-Calling Interface

Pass a `TOOLS` list to the LLM via `skill_executor.run_with_tools` so the LLM can
retrieve memories and browse data by calling functions.

**Tool list** (from `chat_tools.py`):
- `search_memories` — keyword search across all memory files
- `get_memory` — retrieve full content of a specific memory by name
- `list_projects` — list code/work/person projects, grouped by name
- `list_commitments` — list active commitments and waiting-on items
- `list_events` — list recent and upcoming calendar events
- `list_meetings` — list recent Zoom meeting transcripts
- `list_contacts` — list tracked contacts/people
- `list_comms` — list email threads and Slack threads
- `list_readings` — list recently captured web page memories
- `list_commands` — list all Telegram slash commands
- `deliver_pending_replies` — send queued replies after network recovery
- `discard_pending_replies` — drop queued replies

**Dispatcher:**
`chat_tools.dispatch(name, arguments, handler)` routes tool calls to handler methods.
All dispatcher calls logged at INFO level with entry/exit and result size.

**Validation criteria:**
- LLM can retrieve data without explicit `/` commands from user
- Unknown tool names return clear error (not crash)
- Missing required arguments return error message (not exception)
- Dispatcher logs every call for debugging

---

### FR-3: Pending-Reply Queue (Network Resilience)

When `_send_reply` exhausts retries, queue the response and periodically poll for
network recovery.

**State file:** `DEPLOY_DIR/pending-replies.json`

```json
{
  "12345": {
    "pending": [
      {
        "query": "What commitments do I have?",
        "response": "You have 3 active commitments...",
        "queued_at": "2026-04-13T10:15:00"
      }
    ],
    "summary_sent": false
  }
}
```

**Queue logic:**
- `_send_reply` attempts delivery with exponential backoff (up to 3 retries)
- On final failure: call `_queue_pending_reply(chat_id, query, response)`
- Append to `pending` list, set `summary_sent: false`, save atomically

**Validation criteria:**
- Failed sends persist to `pending-replies.json` atomically
- State file survives daemon crashes (temp file + rename)
- Response text capped at 8192 chars before queueing

---

### FR-4: Reconnect Loop with LLM Context

Poll for Telegram reachability every 30s. When network recovers and pending replies
exist, send a notification *and add it to conversation history* so the LLM can interpret
natural language responses like "yes" or "deliver them".

**Cadence:** 30s (asyncio timeout on `stop_event.wait()`)

**Reachability check:** `bot.get_me()` with 10s timeout

**Notification logic:**
- Load `pending-replies.json`
- For each chat_id with `pending` list and `summary_sent == false`:
  - Send notification message
  - **Append notification to `_chat_history[chat_id]` as assistant turn**
  - Set `summary_sent: true`, save state
  - Log at INFO level

**Notification format:**
```
📬 Network is back. I have N response(s) I couldn't deliver earlier.

• /deliver — send them now
• /discard — drop them
```

**Why add to history:**
When the user replies "yes" or "deliver", the LLM sees the notification in conversation
context and can call the `deliver_pending_replies` tool without requiring a `/deliver`
slash command.

**Validation criteria:**
- Notification sent exactly once per queue recovery (idempotent via `summary_sent`)
- Notification appears in `_chat_history` so subsequent LLM calls have context
- User can reply naturally ("yes", "no") and LLM dispatches correct tool

---

### FR-5: Tool-Based Delivery and Discard

The LLM can call `deliver_pending_replies` or `discard_pending_replies` when the user
responds to a reconnect notification.

**`deliver_pending_replies` behaviour:**
- Load `pending-replies.json`
- For each chat_id with pending items:
  - Attempt to send each queued response via `bot.send_message`
  - On success: append query+response to `_chat_history[chat_id]`
  - On failure: keep in `remaining` list, log warning
  - Update state: set `pending = remaining` or delete entry if empty
- Save state atomically
- Return summary: "Delivered N reply/replies. M remain queued." or "Queue is now empty."

**`discard_pending_replies` behaviour:**
- Load `pending-replies.json`
- Count total queued items across all chats
- Save empty state `{}`
- Return: "Discarded N queued reply/replies."

**Validation criteria:**
- Successful delivery updates conversation history (LLM sees the original query+response)
- Partial failures leave undelivered items in queue
- Discard clears all queued items atomically
- Both tools return clear status messages

---

### FR-6: Slash Commands

**Conversation management:**
- `/reset` — clear conversation history for this chat

**Pending-reply management:**
- `/deliver` — send queued replies (calls `deliver_pending_replies` tool internally)
- `/discard` — drop queued replies (calls `discard_pending_replies` tool internally)

**Data browsing commands:**
(Delegated to other feature specs — listed here for completeness)
- `/projects [category] [N]` — list projects
- `/project <N>` — show project detail
- `/commitments [type]` — list commitments
- `/complete N` — mark commitment complete
- `/dismiss N` — mark commitment dismissed
- `/wrong N` — flag false positive commitment
- `/missed` — add manually missed commitment
- `/accuracy` — show commitment extraction stats
- `/events [N]` — list calendar events
- `/event <N>` — show event detail
- `/meetings [N]` — list meeting transcripts
- `/meeting <N>` — show meeting detail
- `/contacts [N]` — list contacts
- `/contact <name|N>` — show contact detail
- `/comms [email|slack] [N]` — list email/Slack threads
- `/comm <N>` — show comm thread detail
- `/readings [N]` — list web page memories
- `/briefing` — manual morning briefing
- `/mute` — disable proactive notifications
- `/unmute` — re-enable proactive notifications

**Validation criteria:**
- All commands listed in `COMMAND_REGISTRY` (single source of truth)
- Test asserts every `CommandHandler` registration has matching registry entry
- `/help` renders from `COMMAND_REGISTRY`

---

## Config

```yaml
telegram:
  bot_token: "TELEGRAM_BOT_TOKEN"
user:
  telegram_user_id: "12345"
  name: Chris
  timezone: America/Los_Angeles
```

`TELEGRAM_BOT_TOKEN` read from env var on startup (not committed to config.yaml).

---

## Files

| File | Role |
|------|------|
| `specs/feat-chat-handler.md` | **This spec** |
| `chat_handler.py` | TelegramChatHandler class, message routing, conversation history, pending-reply queue |
| `chat_tools.py` | Tool schemas and dispatcher for LLM function-calling |
| `daemon.py` | Add TelegramChatHandler to full-role gather |
| `config.yaml.template` | Add `telegram` section |
| `CLAUDE.md` | Document chat handler loop, conversation window, pending-reply resilience |
| `README.md` | User-facing docs for all Telegram commands |
| `tests/unit/test_chat_handler.py` | Unit tests for conversation history, queue, reconnect |
| `tests/unit/test_chat_tools.py` | Unit tests for tool dispatch and new deliver/discard tools |

---

## Unit Tests

### `tests/unit/test_chat_handler.py`

| Test | Assertion |
|------|-----------|
| `test_score_exact_keyword_match` | Relevance score ≥ 2 when keywords match |
| `test_score_zero_when_no_match` | Zero score when no keywords overlap |
| `test_score_ignores_tokens_under_3_chars` | Tokens < 3 chars excluded from scoring |
| `test_queue_pending_reply_persists_state` | Queued reply written to `pending-replies.json` |
| `test_queue_pending_reply_atomic_write` | Temp file cleaned up after save |
| `test_reconnect_loop_adds_notification_to_chat_history` | Notification appended to `_chat_history` as assistant turn |
| `test_reconnect_loop_sets_summary_sent_flag` | `summary_sent: true` after notification |
| `test_reconnect_loop_idempotent` | Notification sent once per recovery (not repeated) |

### `tests/unit/test_chat_tools.py`

| Test | Assertion |
|------|-----------|
| `test_dispatch_list_projects` | Tool routes to `_list_projects_text` |
| `test_dispatch_list_commitments` | Tool routes to `_list_commitments_text` |
| `test_dispatch_search_memories` | Tool routes to `_search_memories_text` |
| `test_dispatch_get_memory` | Tool routes to `_get_memory_text` |
| `test_dispatch_list_commands` | Tool routes to `_list_commands_text` |
| `test_deliver_pending_replies_sends_queued_and_clears_state` | Queued items sent, history updated, state empty |
| `test_deliver_pending_replies_empty_queue_returns_no_pending` | Returns "No pending replies" when queue empty |
| `test_deliver_pending_replies_partial_failure` | Failed sends remain queued, successful ones delivered |
| `test_discard_pending_replies_clears_state` | State file empty after discard |
| `test_discard_pending_replies_empty_queue` | Returns "No pending replies" when queue empty |
| `test_dispatch_unknown_tool_returns_error_string` | Unknown tool → error message (not exception) |
| `test_dispatch_handler_exception_returns_error_string` | Handler crash → error message returned |
| `test_tools_schema_valid` | Every tool has required fields |
| `test_all_tool_names_in_dispatcher` | Every tool in TOOLS has dispatch case |
| `test_dispatch_logs_success` | Successful dispatch logs entry and exit |

---

## Changelog

### v1.0.0 (2026-04-13)
- Initial spec documenting chat handler conversation history, LLM tool-calling,
  pending-reply queue, and reconnect notification with LLM context
- Added `deliver_pending_replies` and `discard_pending_replies` tools so LLM can
  handle natural language responses to reconnect notifications
