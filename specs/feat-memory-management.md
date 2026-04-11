---
specmas: 3.0
kind: feature
id: feat-memory-management
version: 1.0.0
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
- `/search <query>` — keyword search across all memories
- `/memory <N>` — view full details of a single memory by index
- `/delete <N>` — delete a single memory by index
- Session-local index: commands that accept `<N>` operate on the result set
  from the most recent `/memories` or `/search` call

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

### FR-2: Search memories
**Priority:** Critical
**Command:** `/search <query>`

**Behaviour:**
- Reuses `_score_relevance(path, query)` for keyword intersection scoring
  against the cached 500-char file headers
- Returns up to 10 results with score > 0, sorted by score descending then
  mtime descending as tiebreaker
- Sets `self._last_results` to the matching paths in reply order
- Reply format (same as `/memories` but with score):
  ```
  Search results for "react prompting":
  1. ReAct Prompting | Prompt Engineering Guide  (2026-04-11) [score: 3]
  2. ...
  ```
- If no matches: `"No memories match '<query>'."`

**Acceptance Criteria:**
- Query with no args returns usage hint
- Score shown as integer (word-match count)
- `_last_results` updated after every call

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
