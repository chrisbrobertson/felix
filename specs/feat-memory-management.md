---
specmas: 3.0
kind: feature
id: feat-memory-management
version: 1.2.0
created: 2026-04-11
status: draft
complexity: low
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-domain-skip-filter
---

# Memory Management via Telegram

## Overview

### Problem Statement

The bot accumulates memory files continuously but provides no way to inspect or
manage individual entries from Telegram. Without list and search commands, the
only way to audit what has been captured is to browse the iCloud Drive directory
manually. Junk captures (uninstall pages, thank-you pages, local API endpoints)
can only be removed by domain-level purge, which may be too coarse when only one
entry from a domain is unwanted.

### Scope

**In Scope:**
- `/memories [N]` — list the N most recent memories (default 10)
- `/search <query>` — keyword search across ALL memory types (all `.md` in `memories/`)
- `/memory <N>` — view full details of a single memory by index
- `/delete <N>` — delete a single memory by index
- `/help` (alias `/commands`) — grouped list of all bot commands, sourced from `COMMAND_REGISTRY`
- `/comms [email|slack] [N]` (aliases `/messages`, `/communications`) — unified list across `email_thread` and `slack_thread` memory files; optional source filter
- `/comm <N>` (aliases `/message`, `/communication`) — detail view of comm N from last `/comms` list
- Session-local index: commands that accept `<N>` operate on the result set
  from the most recent list command for that type

**Out of Scope:**
- Editing memory content (read/delete only)
- Paginating beyond a single result set per command
- Cross-session persistence of result indices (index is in-memory only)
- Bulk delete by keyword (use `/purge <domain>` from feat-domain-skip-filter)
- Full-text search using embeddings or LLM (keyword match only)

### Success Metrics

- `/memories` replies within 1 second for up to 500 files
- `/search` replies within 2 seconds for up to 500 files
- `/memory <N>` and `/delete <N>` correctly resolve indices from the last result set
- No memory files corrupted or partially deleted
- All commands reject unauthorised users silently

---

## Functional Requirements

### FR-1: List recent memories
**Priority:** Critical
**Command:** `/memories [N]`

**Behaviour:**
- Reads `created` and `source_title` from frontmatter of all `.md` files in
  `BRAIN_DIR/memories/`
- Sorts by `created` descending (newest first)
- Returns the first N entries (default 10; maximum 50)
- Sets `self._last_results` to the sorted file paths so `/memory` and `/delete`
  can resolve indices
- Reply format:
  ```
  Your 7 most recent memories:
  1. LiteLLM Router Documentation  (2026-04-11)
  2. ReAct Prompting Guide          (2026-04-11)
  ...
  ```
- If no memories: `"No memories found."`

**Acceptance Criteria:**
- Titles truncated to 60 chars
- Date shown as `YYYY-MM-DD` (from `created` frontmatter field)
- `_last_results` updated after every call

---

### FR-2: Search memories — grouped by knowledge area
**Priority:** Critical
**Command:** `/search <query>` or `/search <type> <query>`

**Behaviour:**

**Default (unfiltered) — `/search <query>`:**
- Keyword intersection scoring via `_score_relevance` against cached 500-char headers, same as before
- Returns up to 50 matches (raised from 10) with score > 0, sorted by score desc then mtime desc
- Sets `self._last_results` to all matching paths in global index order (1-based; `/memory N` still works)
- Results are **grouped by memory type** in the reply, with a fixed display order:
  1. Contacts (`type: contact`)
  2. Commitments (`type: commitment`)
  3. Projects (`type: project`)
  4. Meetings (`type: meeting_transcript`)
  5. Email threads (`type: email_thread`)
  6. Slack threads (`type: slack_thread`)
  7. Calendar events (`type: calendar_event`)
  8. Web memories (everything else / no `type` field)
- Each group shows up to **5 items** with their global index numbers. If a group has more than 5 matches, a hint line is shown: `  … and N more — /search email <query>`
- Global index numbers are assigned across all groups in the fixed group order, not score order within a group. Within each group, items are sorted by score desc then mtime desc.
- Scores are **not shown** in the grouped view (they were noise for users; kept internally for sorting).
- Reply format example:
  ```
  Search results for "tom jones" — 14 matches

  Contacts (1)
    1. Tom Jones — last seen 3 days ago

  Commitments (2)
    2. [outbound] Deliver Q2 proposal to Tom Jones
    3. [waiting] Contract renewal — Tom Jones

  Email threads (4)
    4. Re: Project Alpha — Tom Jones · Apr 8
    5. RE: Q2 Budget — Tom Jones · Apr 2
    6. Intro: Tom Jones & Sarah · Mar 20
    7. Fwd: Contract — Tom Jones · Mar 15
    … and 1 more — /search email tom jones

  Meetings (2)
    8. Q1 Review — Apr 5 — Tom Jones +3 others
    9. Strategy call — Mar 28

  Projects (1)
    10. project-alpha [code] · last commit Apr 8

  Use /memory N for detail on any item.
  ```
- If no matches: `"No memories match '<query>'."`

**Type-filtered — `/search <type> <query>`:**
- First arg is one of: `email`, `slack`, `meeting`, `project`, `commitment`, `event`, `contact`, `web`
- Filters to that memory type only; shows all matches (up to 50) in a flat list (same as old flat format but without scores)
- Uses same `_last_results` so `/memory N` works on filtered results
- "and N more" hints in the grouped view use this syntax to provide easy drill-down

**Type keyword → `type` field mapping:**
| Keyword | `type` value(s) |
|---------|-----------------|
| `email` | `email_thread` |
| `slack` | `slack_thread` |
| `meeting` | `meeting_transcript` |
| `project` | `project` |
| `commitment` | `commitment` |
| `event` | `calendar_event` |
| `contact` | `contact` |
| `web` | no `type` field or unrecognised type |

**Acceptance Criteria:**
- Query with no args returns usage hint
- `_last_results` updated after every call; `/memory N` resolves correctly for grouped results
- Groups with zero matches are omitted from the reply
- Type filter keyword must be the exact first token to trigger filtered mode; ambiguous queries like `/search email` with no second arg return usage hint
- "… and N more" only shown when a group exceeds 5 items
- Scores not shown to the user (internal only)

---

### FR-3: View a single memory
**Priority:** High
**Command:** `/memory <N>`

**Behaviour:**
- Looks up path at 1-based index N in `self._last_results`
- Reads the full file and parses frontmatter via `_parse_frontmatter()`
- Reply format:
  ```
  📄 LiteLLM Router Documentation
  🔗 https://docs.litellm.ai/docs/routing
  📅 2026-04-11
  🏷 litellm, routing, llm
  
  LiteLLM's router supports fallback chains and load balancing across
  providers via a single OpenAI-compatible interface.
  ```
- If N is out of range or `_last_results` is empty:
  `"Invalid index. Run /memories or /search first."`

**Acceptance Criteria:**
- Summary shown from `summary` frontmatter field (not full body)
- Tags shown comma-separated; omitted if empty
- Graceful handling of missing frontmatter fields

---

### FR-4: Delete a single memory
**Priority:** High
**Command:** `/delete <N>`

**Behaviour:**
- Looks up path at 1-based index N in `self._last_results`
- Reads `source_title` from frontmatter for the confirmation message
- Deletes the file
- Removes the entry from `self._last_results` so subsequent indices shift down
- Reply: `"Deleted: LiteLLM Router Documentation"`
- If N is out of range or `_last_results` is empty:
  `"Invalid index. Run /memories or /search first."`

**Acceptance Criteria:**
- File is removed from `BRAIN_DIR/memories/`
- `_last_results` updated to reflect the deletion (subsequent `/delete` calls
  work correctly without re-running `/memories`)
- Handles missing file gracefully (already deleted externally)

---

## Data Model

No new files or schemas. All commands operate on existing memory files in
`BRAIN_DIR/memories/*.md`. The session-local index is held in
`TelegramChatHandler._last_results: list[Path]` (not persisted across restarts).

---

## Implementation Notes

### Frontmatter parsing helper

Extract `_parse_frontmatter(path: Path) -> dict` from the inline parsing that
already exists in `_purge_domain`. Returns parsed YAML dict or `{}` on failure.
Shared by `/memories`, `/memory`, `/delete`.

```python
def _parse_frontmatter(self, path: Path) -> dict:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}
```

### Index resolution helper

```python
def _resolve_index(self, n: str) -> Path | None:
    try:
        idx = int(n) - 1  # 1-based → 0-based
    except ValueError:
        return None
    if 0 <= idx < len(self._last_results):
        return self._last_results[idx]
    return None
```

### Initialisation

Add `self._last_results: list = []` in `TelegramChatHandler.__init__`.

### CommandHandler registration

```python
self.app.add_handler(CommandHandler("memories", self.cmd_memories))
self.app.add_handler(CommandHandler("search",   self.cmd_search))
self.app.add_handler(CommandHandler("memory",   self.cmd_memory))
self.app.add_handler(CommandHandler("delete",   self.cmd_delete))
```

---

## Files Modified

| File | Change |
|------|--------|
| `chat_handler.py` | Add `_parse_frontmatter`, `_resolve_index`, `cmd_memories`, `cmd_search`, `cmd_memory`, `cmd_delete`; add `self._last_results` to `__init__` |
| `README.md` | Extend Telegram Commands table |
| `tests/unit/test_chat_handler.py` | Tests for all 4 commands |
| `tests/integration/test_pipeline.py` | Integration test: list → view → delete flow |

---

## Testing

### Unit tests

| Test | Assertion |
|------|-----------|
| `test_cmd_memories_lists_recent` | Returns N most recent, sets _last_results |
| `test_cmd_memories_custom_count` | `/memories 3` returns 3 entries |
| `test_cmd_memories_empty` | "No memories found." when dir is empty |
| `test_cmd_memories_rejects_unauthorised` | Silent for wrong user_id |
| `test_cmd_search_returns_matches` | Matching files appear in reply |
| `test_cmd_search_no_matches` | Correct empty-state message |
| `test_cmd_search_no_args` | Usage hint returned |
| `test_cmd_search_rejects_unauthorised` | Silent for wrong user_id |
| `test_cmd_memory_shows_details` | Title, URL, summary, date in reply |
| `test_cmd_memory_invalid_index` | Error when index out of range |
| `test_cmd_memory_no_results` | Error when _last_results is empty |
| `test_cmd_memory_rejects_unauthorised` | Silent for wrong user_id |
| `test_cmd_delete_removes_file` | File deleted; confirmation reply |
| `test_cmd_delete_updates_last_results` | _last_results shortened after delete |
| `test_cmd_delete_invalid_index` | Error when index out of range |
| `test_cmd_delete_rejects_unauthorised` | Silent for wrong user_id |

### Integration test

1. Write 3 memory files with distinct source_titles
2. Call `/memories` → assert all 3 appear, `_last_results` has 3 entries
3. Call `/memory 2` → assert second file's details in reply
4. Call `/delete 1` → assert file deleted, `_last_results` has 2 entries
5. Call `/search <keyword from file 2>` → assert correct file appears

### Manual verification

```
/memories          → numbered list of recent memories
/memories 3        → only 3 entries
/search litellm    → matching memories with scores
/memory 1          → full details of entry 1
/delete 1          → confirmation; entry removed from list
```

---

## Changelog

### v1.2.0 — 2026-04-11

**Updated FRs:**

#### FR-2: Search — grouped results + type filter

`/search <query>` now groups results by memory type (contacts, commitments, projects, meetings, emails, slack, events, web) instead of a flat scored list. Up to 5 items per group are shown; a "… and N more — /search <type> <query>" hint appears when a group overflows. Global 1-based index numbering across all groups; `/memory N` still resolves any item.

`/search <type> <query>` is a new filtered mode: first arg must be one of `email`, `slack`, `meeting`, `project`, `commitment`, `event`, `contact`, `web`. Returns a flat list for that type only. The "and N more" hints in grouped view use this syntax for one-tap drill-down.

Result cap raised from 10 to 50. Scores no longer shown to users.

---

### v1.1.0 — 2026-04-11

**New FRs:**

#### FR-5: /help command and COMMAND_REGISTRY
**Priority:** High
**Commands:** `/help`, `/commands`

A module-level constant `COMMAND_REGISTRY` in `chat_handler.py` is the single
source of truth for all available commands and their one-line descriptions.
Structure:

```python
COMMAND_REGISTRY: dict[str, list[tuple[str, str]]] = {
    "Knowledge listings": [
        ("memories",       "List recent web memories"),
        ("search",         "Keyword search across ALL memory types"),
        ("memory",         "Show memory N from last list"),
        ("delete",         "Delete memory N from last list"),
        ("people",         "List contacts (alias of /contacts)"),
        ("contacts",       "List people you've interacted with"),
        ("contact",        "Show contact by name or N"),
        ("projects",       "List code/work/person projects (optional category filter)"),
        ("project",        "Show project N from last list"),
        ("events",         "List recent and upcoming calendar events"),
        ("event",          "Show event N from last list"),
        ("meetings",       "List recent meeting transcripts"),
        ("meeting",        "Show meeting N from last list"),
        ("comms",          "List recent email + slack threads (optional 'email' or 'slack' filter)"),
        ("comm",           "Show comm N from last list"),
        ("messages",       "Alias of /comms"),
        ("communications", "Alias of /comms"),
    ],
    "Commitments": [
        ("commitments", "List active commitments"),
        ("complete",    "Mark commitment N complete"),
        ("dismiss",     "Dismiss commitment N"),
        ("wrong",       "Mark extracted commitment N as a false positive"),
        ("missed",      "Manually add a commitment the bot missed"),
        ("accuracy",    "Show extraction precision per source type"),
    ],
    "Notifications": [
        ("briefing", "Trigger today's briefing now"),
        ("mute",     "Suppress proactive notifications"),
        ("unmute",   "Resume proactive notifications"),
    ],
    "Domain filter": [
        ("skip",     "Add a domain to the ignore list"),
        ("unskip",   "Remove a domain from the ignore list"),
        ("skiplist", "Show currently skipped domains"),
        ("purge",    "Delete all memories for a domain"),
        ("purgeall", "Delete memories for every skipped domain"),
    ],
    "Meta": [
        ("help",     "Show this list"),
        ("commands", "Alias of /help"),
    ],
}
```

**`/help` behaviour:**
- Iterates `COMMAND_REGISTRY` in order
- Renders each section as a bold header followed by `  /cmd — description` lines
- Chunks output into ≤4096-char Telegram messages (same chunking helper used by
  `/commitments`)
- Aliases (e.g. `/messages`) are listed but not repeated under the section header

**Acceptance criteria:**
- Adding a new `CommandHandler` registration without a matching `COMMAND_REGISTRY`
  entry causes a test to fail (registry-completeness test)
- `/help` renders all groups in order, no truncation within a group
- `/commands` replies identically to `/help`

---

#### FR-6: /comms unified communications listing
**Priority:** High
**Commands:** `/comms [email|slack] [N]`, `/messages [...]`, `/communications [...]`

List `email_thread` and `slack_thread` memory files in a single command.

**Behaviour:**
- Globs `BRAIN_DIR/memories/email-thread-*.md` AND `slack-thread-*.md`
- Filters on `type in {"email_thread", "slack_thread"}`
- Optional first-arg filter: `email` → `email_thread` only; `slack` → `slack_thread` only
- If first arg is neither `email` nor `slack` (and not a number), treat it as
  an invalid filter and reply with usage hint
- N (default 10; clamp `[1, 50]`): if first arg is `email`/`slack`, N is second arg;
  otherwise first arg is N
- Sorts by most-recent-activity timestamp: `last_message` for email threads,
  the timestamp component of the filename for slack threads (fallback: file mtime)
- Sets `self._last_comms_set` to displayed paths (for `/comm N`)
- Reply format: `N. [email] subject — sender (date)` or `N. [slack] #channel — opener (date)`
  The `[email]`/`[slack]` source tag is always shown so the user knows where each
  thread came from
- If no results: `"No communications found."` (or type-specific message if filter applied)

**`/comm <N>`** — detail view for comm N from last `/comms` list.

- Resolves index from `self._last_comms_set` via `_resolve_comm_index`
- Routes to email-shaped or slack-shaped formatter based on `type` of resolved file:
  - **email**: subject, participants, `last_message` date, summary
  - **slack**: channel, thread starter, `last_reply` date, summary
- If N out of range or `_last_comms_set` empty: `"Invalid index. Run /comms first."`

**CommandHandler registrations:**

```python
self.app.add_handler(CommandHandler("comms",          self.cmd_comms))
self.app.add_handler(CommandHandler("messages",       self.cmd_comms))
self.app.add_handler(CommandHandler("communications", self.cmd_comms))
self.app.add_handler(CommandHandler("comm",           self.cmd_comm))
self.app.add_handler(CommandHandler("message",        self.cmd_comm))
self.app.add_handler(CommandHandler("communication",  self.cmd_comm))
```

**Acceptance criteria:**
- `/comms` shows mixed email and slack results sorted by recency
- `/comms email` returns only email threads
- `/comms slack` returns only slack threads
- `/comms 5` returns 5 entries (N parsed when no filter)
- `/comms email 5` returns 5 email entries (N parsed after filter)
- `/comm 1` after `/comms` shows correct email or slack detail
- Source tag present on every line in listing

---

## Additional Tests (v1.1.0)

| Test | Assertion |
|------|-----------|
| `test_cmd_help_renders_all_groups` | All group headers in output |
| `test_cmd_help_all_registry_commands_listed` | Every entry in COMMAND_REGISTRY appears in output |
| `test_cmd_help_chunks_at_4096` | Output > 4096 chars split into multiple messages |
| `test_cmd_commands_alias` | `/commands` produces same output as `/help` |
| `test_registry_completeness` | Every CommandHandler registration has a COMMAND_REGISTRY entry |
| `test_cmd_comms_mixed_results` | `/comms` returns both email and slack threads |
| `test_cmd_comms_email_filter` | `/comms email` returns only email_thread files |
| `test_cmd_comms_slack_filter` | `/comms slack` returns only slack_thread files |
| `test_cmd_comms_n_arg_no_filter` | `/comms 5` returns 5 entries |
| `test_cmd_comms_n_arg_with_filter` | `/comms email 5` returns 5 email entries |
| `test_cmd_comms_n_clamped` | N=999 clamped to 50; N=0 clamped to 1 |
| `test_cmd_comms_source_tag_in_reply` | `[email]` or `[slack]` present on every line |
| `test_cmd_comms_sets_last_comms_set` | `_last_comms_set` populated after call |
| `test_cmd_comm_email_detail` | `/comm N` for email file shows subject + summary |
| `test_cmd_comm_slack_detail` | `/comm N` for slack file shows channel + summary |
| `test_cmd_comm_invalid_index` | Out-of-range N → error message |
