---
specmas: 3.0
kind: feature
id: feat-domain-skip-filter
version: 1.0.0
created: 2026-04-11
status: draft
complexity: low
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
---

# Domain Skip Filter

## Overview

### Problem Statement

The browser watcher has a static `skip_domains` list in `config.yaml` that must be
edited manually in iCloud Drive. There is no way to update the list or remove
accumulated memories from a domain without leaving the Telegram interface. Additionally,
even after adding a domain to the skip list, previously captured memories from that
domain remain in the memory store and surface in chat responses.

### Scope

**In Scope:**
- Telegram slash commands to add/remove domains from the skip list
- Telegram command to list the current skip list
- Telegram command to purge all existing memories from a given domain
- Atomic config.yaml writes to prevent iCloud sync corruption
- Domain changes picked up by the browser watcher without daemon restart

**Out of Scope:**
- Automatic memory purge when a domain is added to the skip list (explicit purge only)
- Wildcard or regex domain patterns (substring match only)
- Per-machine skip lists (the config.yaml is shared via iCloud — applies to all nodes)
- Undo / restore of purged memories

### Success Metrics

- `/skip`, `/unskip`, `/skiplist`, `/purge` commands all reply within 2 seconds
- Config changes reflected in browser watcher within one poll cycle (≤5 min)
- `/purge` removes all and only memories whose `source_url` contains the domain
- No memory files corrupted or partially written during purge
- Existing `skip_domains` manual edits continue to work unchanged

---

## Functional Requirements

### FR-1: Add domain to skip list
**Priority:** Critical  
**Command:** `/skip <domain>`

**Behaviour:**
- Reads `config.yaml` from iCloud (`BRAIN_DIR / "config.yaml"`)
- Appends `<domain>` to `browser_watcher.skip_domains` if not already present
- Writes config back atomically (tmp file + `os.rename`)
- Replies: `"Added example.com to skip list. Browser watcher will ignore it within 5 minutes."`
- If already present: `"example.com is already on the skip list."`

**Acceptance Criteria:**
- `config.yaml` updated on disk after command
- Running `/skiplist` immediately after shows the new domain
- Browser watcher skips URLs containing the domain on next poll cycle

---

### FR-2: Remove domain from skip list
**Priority:** High  
**Command:** `/unskip <domain>`

**Behaviour:**
- Removes `<domain>` from `browser_watcher.skip_domains` if present
- Writes config back atomically
- Replies: `"Removed example.com from skip list."`
- If not present: `"example.com was not on the skip list."`

---

### FR-3: List skipped domains
**Priority:** High  
**Command:** `/skiplist`

**Behaviour:**
- Reads current `browser_watcher.skip_domains` from `config.yaml`
- Replies with a numbered list of all domains, or `"Skip list is empty."` if none

---

### FR-4: Purge memories for a domain
**Priority:** High  
**Command:** `/purge <domain>`

**Behaviour:**
- Globs all `.md` files in `BRAIN_DIR / "memories"`
- For each file, reads the first 500 characters and parses the YAML frontmatter
- Checks whether `source_url` contains `<domain>` as a substring
- Deletes all matching files
- Replies: `"Deleted 7 memories from example.com."` or `"No memories found for example.com."`

**Acceptance Criteria:**
- Only files whose `source_url` contains the domain string are deleted
- Files without a `source_url` field are skipped (not deleted)
- Reply count matches actual files deleted
- Command is independent of the skip list — can purge a domain that is not skipped

---

### FR-5: Purge memories for all skipped domains
**Priority:** Medium  
**Command:** `/purgeall`

**Behaviour:**
- Reads current `browser_watcher.skip_domains` from `config.yaml`
- Runs the purge logic (FR-4) for each domain in the list
- Replies with a summary: one line per domain showing count deleted, e.g.:
  ```
  Purge complete:
  • google.com — 12 deleted
  • facebook.com — 3 deleted
  • twitter.com — 0 found
  ```
- If skip list is empty: `"Skip list is empty — nothing to purge."`

---

## Data Model

No new files or schemas. All changes are to existing structures:

| Location | Field | Change |
|----------|-------|--------|
| `config.yaml` → `browser_watcher.skip_domains` | `list[str]` | Written by `/skip` and `/unskip`; unchanged format |
| Memory files `memories/*.md` | `source_url` frontmatter field | Read-only during purge; files deleted if matched |

---

## Implementation Notes

### Config editor

Add `_edit_skip_domains(action: str, domain: str)` to `TelegramChatHandler`:

```python
def _edit_skip_domains(self, action: str, domain: str) -> str:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    domains = config.setdefault("browser_watcher", {}).setdefault("skip_domains", [])
    if action == "add":
        if domain in domains:
            return f"{domain} is already on the skip list."
        domains.append(domain)
    elif action == "remove":
        if domain not in domains:
            return f"{domain} was not on the skip list."
        domains.remove(domain)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(yaml.dump(config, default_flow_style=False))
    os.rename(tmp, CONFIG_PATH)
    return None  # caller constructs success message
```

### Memory purge

Add `_purge_domain(domain: str) -> int` to `TelegramChatHandler`:

```python
def _purge_domain(self, domain: str) -> int:
    deleted = 0
    for f in (BRAIN_DIR / "memories").glob("*.md"):
        header = f.read_text()[:500]
        m = re.match(r'^---\n(.*?)\n---', header, re.DOTALL)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1))
        except Exception:
            continue
        if domain in (fm.get("source_url") or ""):
            f.unlink()
            deleted += 1
    return deleted
```

### Command registration

Use `CommandHandler` (not `MessageHandler`) so commands only fire on explicit `/` prefixed messages. Register alongside the existing `MessageHandler` in `__init__`:

```python
from telegram.ext import CommandHandler
self.app.add_handler(CommandHandler("skip", self.cmd_skip))
self.app.add_handler(CommandHandler("unskip", self.cmd_unskip))
self.app.add_handler(CommandHandler("skiplist", self.cmd_skiplist))
self.app.add_handler(CommandHandler("purge", self.cmd_purge))
self.app.add_handler(CommandHandler("purgeall", self.cmd_purgeall))
```

All command handlers must check `user_id != self.allowed_user_id` and return silently for unauthorised users, matching the existing `handle_message` pattern.

---

## Files Modified

| File | Change |
|------|--------|
| `chat_handler.py` | Add 5 command handlers + `_edit_skip_domains` + `_purge_domain` |
| `config.yaml.template` | Add comment: skip_domains manageable via /skip Telegram command |
| `README.md` | Add "Telegram Commands" section |
| `tests/unit/test_chat_handler.py` | Tests for all 5 commands |
| `tests/integration/test_pipeline.py` | Integration test: /skip persists; /purge removes correct files |

---

## Testing

### Unit tests (`tests/unit/test_chat_handler.py`)

| Test | Assertion |
|------|-----------|
| `test_skip_adds_domain` | config.yaml updated; correct reply |
| `test_skip_already_present` | No duplicate added; correct reply |
| `test_unskip_removes_domain` | Domain removed from config |
| `test_unskip_not_present` | No change; correct reply |
| `test_skiplist_shows_domains` | Reply contains all domains |
| `test_skiplist_empty` | Correct empty-state reply |
| `test_purge_deletes_matching_memories` | Only files with matching source_url deleted |
| `test_purge_skips_files_without_source_url` | Non-matching files untouched |
| `test_purge_no_matches` | Zero-count reply |
| `test_purgeall_deletes_all_skip_domains` | Summary reply with per-domain counts |
| `test_commands_reject_unauthorised_user` | All 5 commands silent for wrong user_id |

### Integration test (`tests/integration/test_pipeline.py`)

1. Write a sample `config.yaml` with empty skip_domains
2. Call `/skip example.com` — assert config updated
3. Run browser watcher loop with a URL containing `example.com` — assert URL not processed
4. Write two memory files: one with `source_url: https://example.com/page`, one with `source_url: https://other.com/page`
5. Call `/purge example.com` — assert only the first file deleted, second intact

### Manual verification

```
/skiplist              → current list
/skip test.com         → "Added test.com..."
/skiplist              → includes test.com
/unskip test.com       → "Removed test.com..."
/purge example.com     → "Deleted N memories..."
/purgeall              → per-domain summary
```
