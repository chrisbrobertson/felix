---
specmas: 3.0
kind: feature
id: feat-communication-watchlists
version: 1.0.0
created: 2026-04-17
status: implemented
shipped_version: "1.4.0"
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - feat-email-scanner
  - feat-slack-scanner
  - feat-zoom-transcript-scanner
  - feat-proactive-notifications
---

# Communication Watchlists

## Overview

### Problem Statement

Users want Felix to monitor incoming communications for specific people or topics and send an alert when a match arrives. Example: "let me know when Sarah replies about the API deadline." Currently the system passively stores all communications with no active monitoring. Filed as feature `33b6ce`.

### Scope

**In scope:**
- Watchlist entries stored as `watchlist-{slug}-{id}.md` memory files
- Match criteria: `topic` (keyword list, AND logic), optional `person` (name or email substring)
- Communication sources checked: email threads, Slack threads, Zoom/meeting transcripts
- Match check runs inside each scanner after a new memory is written
- Alert delivered via Telegram to the configured user chat_id
- Status lifecycle: `active` → `triggered` (one-shot) or `recurring` (re-alerts every match)
- Telegram commands: `/watch`, `/watches`, `/unwatch N`
- Agent can create watches via a new `add_watch` tool in `chat_tools.py`

**Out of scope:**
- Calendar event monitoring (use notification_manager for meeting alerts)
- Regex or semantic matching (keyword substring only, case-insensitive)
- Multi-user watchlists
- Retroactive matching against existing memories (new arrivals only)

### Success Metrics

- `/watch "API deadline" from:sarah` creates a watchlist entry
- Next email from Sarah mentioning "API deadline" triggers a Telegram alert within one scan cycle
- `/watches` lists all active watches with indices
- `/unwatch 1` deactivates a watch

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `watchlist-{slug}-{id}.md` stored in memories/ with frontmatter: `type: watchlist`, `topic_keywords: [...]`, `person: str|null`, `source_types: [email, slack, meeting]`, `mode: one_shot|recurring`, `status: active|triggered|expired`, `created`, `short_id` |
| FR-2 | `/watch "<topic>" [from:<person>] [--recurring]` creates a watchlist entry |
| FR-3 | `/watches` lists all non-expired watchlists with index, topic, person, status |
| FR-4 | `/unwatch N` sets status to `expired` on item N from last `/watches` |
| FR-5 | `watchlist_checker.py` provides `check_new_memory(memory_path, memories_dir, notify_fn)` — checks all active watchlists against the given memory file |
| FR-6 | Match condition: all `topic_keywords` appear (case-insensitive) in the memory's title + body, AND `person` (if set) appears in the memory's `participants` or `source_title` |
| FR-7 | On match: send Telegram notification ("🔔 Watch triggered: [topic] — [memory title]"), update `status: triggered` for one_shot watches |
| FR-8 | Email scanner, Slack scanner, and Zoom scanner call `check_new_memory()` after each new memory write |
| FR-9 | `add_watch` tool added to `chat_tools.py` for agent-triggered watch creation |
| FR-10 | Watches older than 30 days auto-expire if still `active` and never triggered |

---

## Design

### Memory file format (`watchlist-{slug}-{id}.md`)

```yaml
---
type: watchlist
short_id: abc123
topic_keywords:
  - API deadline
  - milestone
person: sarah
source_types:
  - email
  - slack
mode: one_shot
status: implemented
shipped_version: "1.4.0"
created: 2026-04-17T10:00:00
triggered_at: null
---

## Watch

Alert me when Sarah mentions the API deadline.
```

### `watchlist_checker.py`

```python
async def check_new_memory(memory_path: Path, memories_dir: Path, notify_fn) -> None:
    """Check all active watchlists against a newly written memory file."""
    watchlists = list(memories_dir.glob("watchlist-*.md"))
    if not watchlists:
        return

    try:
        body = memory_path.read_text().lower()
    except OSError:
        return

    for wl_path in watchlists:
        fm = _parse_frontmatter(wl_path)
        if fm.get("status") != "active":
            continue
        if _matches(fm, body, memory_path):
            await _trigger(fm, wl_path, memory_path, notify_fn)

def _matches(fm, body_lower, memory_path) -> bool:
    keywords = [k.lower() for k in fm.get("topic_keywords", [])]
    if not all(kw in body_lower for kw in keywords):
        return False
    person = (fm.get("person") or "").lower()
    if person and person not in body_lower:
        return False
    return True
```

### Scanner integration

In each scanner, after `self._write_memory(...)`:

```python
from watchlist_checker import check_new_memory
await check_new_memory(written_path, BRAIN_DIR / "memories", self._notify)
```

### Telegram commands in `chat_handler.py`

- `/watch` → parse args, create watchlist file, reply with confirmation + short_id
- `/watches` → list all `watchlist-*.md`, populate `_last_watch_set`
- `/unwatch N` → resolve index, set status expired

---

## Test Plan

**Unit tests in `tests/unit/test_watchlist_checker.py`:**

1. `test_keyword_match_triggers_notify` — all keywords in body → notify called
2. `test_keyword_and_person_match` — both keyword + person in body → match
3. `test_partial_keyword_no_match` — only some keywords present → no notify
4. `test_person_mismatch_no_trigger` — keyword matches but person doesn't → no notify
5. `test_one_shot_status_flips_to_triggered` — after match, status updated to triggered
6. `test_recurring_stays_active` — recurring mode does not flip status
7. `test_expired_watch_skipped` — status=expired never matches
8. `test_expired_by_age` — 31-day-old active watch auto-expires on check

**Unit tests in `tests/unit/test_chat_handler.py`:**

9. `test_cmd_watch_creates_file` — `/watch "topic"` creates watchlist-*.md
10. `test_cmd_watch_with_person` — `from:sarah` sets person field
11. `test_cmd_watches_lists_active` — `/watches` returns active entries
12. `test_cmd_unwatch_expires_entry` — `/unwatch 1` sets status=expired
