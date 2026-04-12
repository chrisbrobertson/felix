---
specmas: 3.0
kind: feature
id: feat-feature-tracker
version: 1.0.0
created: 2026-04-12
status: draft
complexity: low
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-memory-management
---

# Feature Request Tracker

## Overview

### Problem Statement

When using Second Brain via Telegram, the user naturally encounters improvement ideas: "I wish I could filter commitments by person", "It would be useful to tag calendar events", "The /search command should support wildcards". These ideas are context-rich (the user is already in the system, experiencing the gap) but ephemeral — without a low-friction capture mechanism, they are lost or written to a notes app where they lose the surrounding context.

The Feature Request Tracker allows the user to capture feature ideas instantly with `/feature <description>`, storing each request as a memory file. The tracker provides lightweight status management (new → planned → in-progress → done | wont-do), priority levels, and note-taking, without becoming a full project management system. All feature requests are stored as memory files in `BRAIN_DIR/memories/` and are automatically indexed by the existing `/search` command.

### Scope

**In Scope:**
- `/feature <description>` — capture a new feature request with automatic tag extraction
- `/features [status]` — list feature requests filtered by status (default: new + planned)
- `/feature-detail <N>` (alias `/fdetail`) — view full content of a request
- `/feature-priority <N> <level>` — update priority
- `/feature-plan <N>`, `/feature-start <N>`, `/feature-done <N>`, `/feature-wont-do <N>` — status transitions
- `/feature-note <N> <text>` — append timestamped note
- Memory file format: `type: feature_request` with structured frontmatter
- Atomic frontmatter updates (status, priority) without corrupting body content
- Session-local result set (`_last_feature_set`) for N-based indexing
- `COMMAND_REGISTRY` entries under "Feature Requests" group
- Automatic searchability via existing `/search` command (no new code)

**Out of Scope:**
- External integrations (GitHub Issues, Linear, Jira)
- Assignment to team members (personal system)
- Due dates or deadlines on feature requests
- Automatic status promotion based on code changes
- Automatic duplicate detection across feature requests
- Browser or web interface
- Voting or ranking of feature requests
- Categorization or grouping of features beyond tags

### Success Metrics

- `/feature` creates a valid memory file within 1 second
- `/features` responds within 1 second for up to 200 feature files
- `/feature-detail N` correctly resolves indices from the last `/features` result set
- Status and priority updates preserve body content exactly (no corruption)
- All commands reject unauthorised users silently
- Feature request files are searchable via `/search` without additional code

---

## Functional Requirements

### FR-1: Feature Request Memory File Format

**Priority:** Critical

**Memory file structure:**

```markdown
---
title: Add configurable reports
type: feature_request
status: new
priority: medium
created: '2026-04-12T10:30:00'
updated: '2026-04-12T10:30:00'
tags: [reporting, automation]
source_url: feature:abc123
---

## Request

I need a way to configure new regular reports delivered via Telegram.

## Context

Captured via /feature command at 2026-04-12 10:30.

## Notes

(empty at creation — populated by /feature-note N <text>)
```

**Frontmatter schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | yes | First 5 words of description, title-cased, max 60 chars |
| `type` | string | yes | Always `feature_request` |
| `status` | string | yes | Lifecycle status (see FR-2) |
| `priority` | string | yes | One of: `low`, `medium`, `high`, `critical` |
| `created` | ISO 8601 | yes | Timestamp of creation |
| `updated` | ISO 8601 | yes | Timestamp of last modification (status/priority change or note append) |
| `tags` | list[string] | yes | Extracted from #hashtags in description; empty list if none |
| `source_url` | string | yes | Stable feature ID (see filename convention) |

**Body structure:**

- `## Request` — user's description text, with #hashtags stripped
- `## Context` — single line: "Captured via /feature command at {timestamp}."
- `## Notes` — timestamped notes appended via `/feature-note`, format:
  ```markdown
  - 2026-04-12 10:45: Discussed with team, planned for Q2
  - 2026-04-13 09:00: Started work on branch `feature/reports`
  ```
  Empty section at creation.

**Filename convention:**
```
feature-request-{slug}-{6char-id}.md
```
- `slug` derived from `title`: lowercase, spaces → hyphens, max 40 chars
- `6char-id` is first 6 chars of SHA-1 hash of `{timestamp}:{description}`
- Example: `feature-request-add-configurable-reports-a1b2c3.md`

**Validation criteria:**
- All frontmatter fields present and correctly typed
- Tags are lowercase, alphanumeric + hyphen only
- `status` is one of the valid lifecycle values (see FR-2)
- `priority` is one of: `low`, `medium`, `high`, `critical`
- Filename collision (same slug + hash) is astronomically unlikely; no dedup check needed
- File is a valid memory file parseable by `_parse_frontmatter()`

---

### FR-2: Status Lifecycle

**Priority:** Critical

**Status values and transitions:**

```
new → planned → in-progress → done
 |                              |
 └────────────────→ wont-do ←──┘
```

| Status | Description | Telegram Commands |
|--------|-------------|-------------------|
| `new` | Just captured, not yet reviewed | Default on creation |
| `planned` | Reviewed and scheduled for future work | `/feature-plan N` |
| `in-progress` | Actively being worked on | `/feature-start N` |
| `done` | Completed and shipped | `/feature-done N [note]` |
| `wont-do` | Decided not to pursue | `/feature-wont-do N [reason]` |

**Validation criteria:**
- Status field in frontmatter is one of the five valid values
- Transition commands update `status` field in-place using atomic write
- `updated` timestamp refreshed on every status change
- Closing commands (`/feature-done`, `/feature-wont-do`) append optional note to `## Notes`

---

### FR-3: `/feature <description>` — Create New Feature Request

**Priority:** Critical

**Command:** `/feature <description>` or `/feature-new <description>` (alias)

**Behaviour:**

1. Call `_check_auth(update)` to validate user
2. Extract description from message text (everything after `/feature `)
3. Parse #hashtags from description (regex: `#(\w+)`); store as tags (lowercase)
4. Strip hashtags from description for body text
5. Generate title from first 5 words of description:
   - Title-case each word
   - Truncate at 60 chars if needed
   - Example: "add dark mode to telegram bot" → "Add Dark Mode To Telegram"
6. Generate stable ID:
   ```python
   import hashlib
   from datetime import datetime, timezone
   timestamp = datetime.now(timezone.utc).isoformat()
   stable_id = hashlib.sha1(f"{timestamp}:{description}".encode()).hexdigest()[:6]
   source_url = f"feature:{stable_id}"
   ```
7. Generate slug from title: lowercase, spaces → hyphens, max 40 chars
8. Generate filename: `feature-request-{slug}-{stable_id}.md`
9. Build frontmatter dict:
   ```python
   metadata = {
       "title": title,
       "type": "feature_request",
       "status": "new",
       "priority": "medium",
       "created": timestamp,
       "updated": timestamp,
       "tags": extracted_tags,
       "source_url": source_url,
   }
   ```
10. Build body content:
    ```markdown
    ## Request
    
    {description_without_hashtags}
    
    ## Context
    
    Captured via /feature command at {timestamp}.
    
    ## Notes
    
    (empty)
    ```
11. Call `write_memory(content=body, metadata=metadata, brain_dir=BRAIN_DIR)`
12. Reply to user:
    ```
    Feature request captured: '{title}' (ID: {stable_id}).
    Use /features to view all.
    ```

**Edge cases:**
- Empty description: reply `"Usage: /feature <description>"`
- Description with no alphabetic characters: generate title "Feature Request"
- No hashtags: tags = `[]`
- Duplicate hashtags: deduplicate tag list

**Validation criteria:**
- Memory file created in `BRAIN_DIR/memories/`
- File parseable by `_parse_frontmatter()`
- Hashtags extracted and stored lowercase
- Description body does not contain hashtag syntax (`#word` removed)
- Reply includes stable ID for reference

---

### FR-4: `/features [status]` — List Feature Requests

**Priority:** Critical

**Command:** `/features [status]`

**Behaviour:**

1. Call `_check_auth(update)` to validate user
2. Parse optional status argument:
   - No arg: filter to `status in [new, planned]` (default)
   - `all`: no filter
   - `new`, `planned`, `in-progress`, `done`, `wont-do`: filter to that status
   - Invalid status: reply `"Unknown status. Use: new, planned, in-progress, done, wont-do, all."`
3. Glob `BRAIN_DIR/memories/feature-request-*.md`
4. Read cached header (first 500 chars) and parse frontmatter for each file
5. Filter to `type == feature_request` and matching status filter
6. Sort by `created` descending (newest first)
7. Store full sorted list in `self._last_feature_set` (list of file paths)
8. Build reply:
   ```
   Feature requests ({status_description}):
   
   1. [new] [medium] Add configurable reports (created: 2026-04-12)
   2. [planned] [high] Dark mode for Telegram (created: 2026-04-11)
   3. [new] [low] Export memories as JSON (created: 2026-04-10)
   
   Use /feature-detail N for full details.
   Use /feature-plan N, /feature-start N, /feature-done N to update status.
   ```
   where `status_description` is:
   - No filter: "new + planned"
   - `all`: "all"
   - Specific status: "{status}"
9. If no matches: `"No feature requests found."`

**Format per line:**
```
{index}. [{status}] [{priority}] {title} (created: {created_date})
```
- `index` is 1-based position in `_last_feature_set`
- `status` is lowercase status value
- `priority` is lowercase priority value
- `title` is truncated to 50 chars if needed
- `created_date` is `YYYY-MM-DD` extracted from `created` field

**Validation criteria:**
- Default filter (no arg) shows only new + planned
- `/features all` shows all statuses
- Sorted newest first within filtered set
- `_last_feature_set` updated after every call
- `/feature-detail N` can resolve indices immediately after `/features`

---

### FR-5: `/feature-detail <N>` — View Full Feature Request

**Priority:** High

**Command:** `/feature-detail <N>` or `/fdetail <N>` (alias)

**Behaviour:**

1. Call `_check_auth(update)` to validate user
2. Parse N as integer
3. Call helper `_resolve_feature_index(n, update)` (see FR-11):
   - Returns file path from `_last_feature_set[n-1]`
   - Sends error reply and returns None if invalid
4. Read full file content
5. Parse frontmatter
6. Build reply:
   ```
   📋 {title}
   
   Status: {status}
   Priority: {priority}
   Created: {created}
   Updated: {updated}
   Tags: {tags joined by ', '}
   ID: {stable_id from source_url}
   
   ## Request
   
   {request body text}
   
   ## Notes
   
   {notes body text, or "(none)" if empty}
   ```

**Edge cases:**
- N out of range: handled by `_resolve_feature_index` (sends error reply)
- `/feature-detail` before `/features`: sends error reply
- Missing frontmatter fields: show "(unknown)" for that field

**Validation criteria:**
- All frontmatter fields displayed
- Full request body shown
- Notes section shown (empty or populated)
- ID extracted from `source_url` field (format: `feature:{id}`)

---

### FR-6: `/feature-priority <N> <level>` — Update Priority

**Priority:** High

**Command:** `/feature-priority <N> <low|medium|high|critical>`

**Behaviour:**

1. Call `_check_auth(update)` to validate user
2. Parse N as integer and level as string
3. Validate level is one of: `low`, `medium`, `high`, `critical`
   - Invalid: reply `"Priority must be: low, medium, high, or critical."`
4. Call `_resolve_feature_index(n, update)` → file path
5. Call `_rewrite_feature_frontmatter(path, {"priority": level})` (see FR-10)
6. Reply: `"Priority updated: '{title}' is now {level}."`

**Validation criteria:**
- Priority field updated in frontmatter
- `updated` timestamp refreshed
- Body content preserved exactly
- File remains parseable

---

### FR-7: `/feature-plan <N>`, `/feature-start <N>`, `/feature-done <N> [note]`, `/feature-wont-do <N> [reason]` — Status Transitions

**Priority:** Critical

**Commands:**
- `/feature-plan <N>` — set status to `planned`
- `/feature-start <N>` — set status to `in-progress`
- `/feature-done <N> [note]` — set status to `done`, optional closing note
- `/feature-wont-do <N> [reason]` — set status to `wont-do`, optional reason

**Behaviour (all four commands):**

1. Call `_check_auth(update)` to validate user
2. Parse N as integer
3. Call `_resolve_feature_index(n, update)` → file path
4. Read full file content
5. Parse frontmatter and body sections
6. Update frontmatter:
   ```python
   updates = {
       "status": new_status,
       "updated": datetime.now(timezone.utc).isoformat(),
   }
   ```
7. If command has optional note/reason argument (everything after `N`):
   - Append to `## Notes` section:
     ```markdown
     - {updated_date} {HH:MM}: {note_text}
     ```
   - Example: `- 2026-04-12 15:30: Completed and shipped in v1.2`
8. Call `_rewrite_feature_frontmatter(path, updates)` with body rewrite for note append
9. Reply:
   - `/feature-plan N`: `"Feature '{title}' marked as planned."`
   - `/feature-start N`: `"Feature '{title}' is now in progress."`
   - `/feature-done N`: `"Feature '{title}' marked as done."`
   - `/feature-wont-do N`: `"Feature '{title}' marked as won't do."`

**Note append format:**
```markdown
- 2026-04-12 15:30: {note_text}
```
- Date is `YYYY-MM-DD` from `updated` timestamp
- Time is `HH:MM` from `updated` timestamp
- Appends to `## Notes` section, creating newlines as needed

**Validation criteria:**
- Status field updated correctly
- `updated` timestamp refreshed
- Optional note appended to `## Notes` section with correct timestamp
- Body content preserved (request text unchanged)
- No duplicate notes if command rerun

---

### FR-8: `/feature-note <N> <text>` — Append Timestamped Note

**Priority:** Medium

**Command:** `/feature-note <N> <text>`

**Behaviour:**

1. Call `_check_auth(update)` to validate user
2. Parse N as integer
3. Extract note text (everything after `N `)
4. Validate note text is non-empty:
   - Empty: reply `"Usage: /feature-note <N> <text>"`
5. Call `_resolve_feature_index(n, update)` → file path
6. Read full file content
7. Parse frontmatter and body sections
8. Generate timestamp: `datetime.now(timezone.utc).isoformat()`
9. Append to `## Notes` section:
   ```markdown
   - {date} {time}: {note_text}
   ```
   where date is `YYYY-MM-DD` and time is `HH:MM` extracted from timestamp
10. Update frontmatter `updated` field to new timestamp
11. Write file atomically (read → modify → temp → rename)
12. Reply: `"Note added to '{title}'."`

**Edge cases:**
- `## Notes` section empty: append first note with proper spacing
- `## Notes` section already has notes: append to end with newline separator
- Very long note text (>500 chars): truncate and append "…"

**Validation criteria:**
- Note appended with correct timestamp
- `updated` field refreshed
- Body content outside `## Notes` unchanged
- Multiple notes accumulate in chronological order

---

### FR-9: `COMMAND_REGISTRY` Entries

**Priority:** Critical

All feature tracker commands must be registered in `COMMAND_REGISTRY` in `chat_handler.py` under a new `"Feature Requests"` group.

**Registry entries:**

```python
COMMAND_REGISTRY = {
    # ... existing groups ...
    
    "Feature Requests": {
        "feature": "Create a new feature request: /feature <description>",
        "feature-new": "Alias for /feature",
        "features": "List feature requests: /features [status]",
        "feature-detail": "View full feature request: /feature-detail <N>",
        "fdetail": "Alias for /feature-detail",
        "feature-priority": "Update priority: /feature-priority <N> <low|medium|high|critical>",
        "feature-plan": "Mark feature as planned: /feature-plan <N>",
        "feature-start": "Mark feature as in-progress: /feature-start <N>",
        "feature-done": "Mark feature as done: /feature-done <N> [note]",
        "feature-wont-do": "Mark feature as won't do: /feature-wont-do <N> [reason]",
        "feature-note": "Add a note: /feature-note <N> <text>",
    },
}
```

**Handler registrations in `ChatHandler.__init__()`:**

```python
app.add_handler(CommandHandler("feature", self.cmd_feature))
app.add_handler(CommandHandler("feature-new", self.cmd_feature))
app.add_handler(CommandHandler("features", self.cmd_features))
app.add_handler(CommandHandler("feature-detail", self.cmd_feature_detail))
app.add_handler(CommandHandler("fdetail", self.cmd_feature_detail))
app.add_handler(CommandHandler("feature-priority", self.cmd_feature_priority))
app.add_handler(CommandHandler("feature-plan", self.cmd_feature_plan))
app.add_handler(CommandHandler("feature-start", self.cmd_feature_start))
app.add_handler(CommandHandler("feature-done", self.cmd_feature_done))
app.add_handler(CommandHandler("feature-wont-do", self.cmd_feature_wont_do))
app.add_handler(CommandHandler("feature-note", self.cmd_feature_note))
```

**Validation criteria:**
- Every handler has a matching `COMMAND_REGISTRY` entry (test enforced)
- `/help` renders the "Feature Requests" group
- All commands call `_check_auth` at entry

---

### FR-10: `_rewrite_feature_frontmatter()` — Atomic Frontmatter Update

**Priority:** Critical

**Function signature:**
```python
def _rewrite_feature_frontmatter(
    self,
    file_path: str,
    frontmatter_updates: dict,
    body_updates: dict | None = None
) -> None:
    """
    Atomically update frontmatter keys in a feature request file.
    
    Args:
        file_path: Absolute path to the feature request file
        frontmatter_updates: Dict of frontmatter keys to update/add
        body_updates: Optional dict of body section updates
                      e.g., {"Notes": new_notes_content}
    
    Raises:
        FileNotFoundError: if file does not exist
        ValueError: if file is not parseable
    """
```

**Behaviour:**

1. Read full file content
2. Split into frontmatter block and body:
   ```python
   lines = content.split("\n")
   assert lines[0] == "---"
   end_idx = lines[1:].index("---") + 1
   frontmatter_lines = lines[1:end_idx]
   body_lines = lines[end_idx+1:]
   ```
3. Parse frontmatter as YAML:
   ```python
   import yaml
   frontmatter = yaml.safe_load("\n".join(frontmatter_lines))
   ```
4. Update frontmatter dict with `frontmatter_updates`
5. Always update `updated` timestamp:
   ```python
   from datetime import datetime, timezone
   frontmatter["updated"] = datetime.now(timezone.utc).isoformat()
   ```
6. If `body_updates` provided:
   - Parse body into sections (regex: `^## (.+)$`)
   - Update specified sections
   - Rebuild body from sections
7. Rebuild frontmatter block:
   ```python
   new_frontmatter = yaml.dump(frontmatter, sort_keys=False, default_flow_style=False)
   ```
8. Rebuild full content:
   ```python
   new_content = f"---\n{new_frontmatter}---\n{new_body}"
   ```
9. Atomic write (temp file → rename):
   ```python
   import tempfile, os
   temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(file_path), suffix=".tmp")
   os.write(temp_fd, new_content.encode("utf-8"))
   os.close(temp_fd)
   os.rename(temp_path, file_path)
   ```

**Validation criteria:**
- Original body content preserved if `body_updates` is None
- Frontmatter keys updated correctly
- `updated` timestamp always refreshed
- No partial writes (atomic rename)
- File remains parseable by `_parse_frontmatter()`
- Body sections (Request, Context, Notes) unchanged unless explicitly updated

---

### FR-11: `_resolve_feature_index()` — Index Resolution Helper

**Priority:** High

**Function signature:**
```python
async def _resolve_feature_index(self, n: int, update: Update) -> str | None:
    """
    Resolve 1-based index N into file path from _last_feature_set.
    
    Args:
        n: 1-based index from user input
        update: Telegram Update object for sending error replies
    
    Returns:
        File path if valid, None if invalid (error reply sent)
    """
```

**Behaviour:**

1. Check if `self._last_feature_set` exists and is non-empty:
   - Not set: send reply `"No feature list loaded. Run /features first."`
   - Return None
2. Check if `n` is in valid range `[1, len(_last_feature_set)]`:
   - Out of range: send reply `"Invalid index. Run /features to see the list."`
   - Return None
3. Return `self._last_feature_set[n-1]`

**Validation criteria:**
- Returns file path for valid indices
- Sends error reply and returns None for invalid indices
- Does not raise exceptions

---

### FR-12: `/search` Compatibility

**Priority:** Medium

**Requirement:**

Feature request memory files must be automatically searchable via the existing `/search` command without any code changes to the search logic.

**Validation criteria:**

1. `/search <query>` includes feature request files in results when query matches
2. Feature requests appear in a dedicated "Feature Requests" group in grouped search results (see feat-memory-management.md FR-2)
3. Group display order (add "Feature Requests" after "Commitments", before "Projects"):
   1. Contacts
   2. Commitments
   3. **Feature Requests** ← new group
   4. Projects
   5. Meetings
   6. Email threads
   7. Slack threads
   8. Calendar events
   9. Web memories
4. Type filter keyword `feature` maps to `type: feature_request`
5. `/search feature <query>` shows only feature request files
6. Keyword relevance scoring uses cached 500-char header (title + request body)
7. Search result line format in grouped view:
   ```
   N. [status] [priority] {title} (created: {date})
   ```

**Implementation note:**

Add to `chat_handler.py` search grouping logic:

```python
# In cmd_search(), group ordering:
GROUP_ORDER = [
    ("contact", "Contacts"),
    ("commitment", "Commitments"),
    ("feature_request", "Feature Requests"),  # ← new
    ("project", "Projects"),
    # ... rest unchanged
]

# In TYPE_KEYWORD_MAP:
TYPE_KEYWORD_MAP = {
    # ... existing entries
    "feature": "feature_request",  # ← new
}
```

**Validation criteria:**
- Test `/search <keyword>` includes feature requests when tags or title match
- Test `/search feature <keyword>` filters to feature requests only
- Test grouped display shows feature requests in correct position with correct format

---

## Non-Goals

1. **External integrations** — no GitHub Issues, Linear, Jira, or other project management tool sync. Feature requests live only in the Second Brain memory system.

2. **Team collaboration** — no assignment to other users, no shared feature backlogs, no voting. This is a personal feature tracker for a single-user system.

3. **Due dates or deadlines** — no `due_date` field on feature requests. The priority field is sufficient for personal prioritisation.

4. **Automatic status updates** — no detection of code changes or commits that mark a feature as done. Status is always set manually via Telegram commands.

5. **Duplicate detection** — no automatic detection of similar or duplicate feature requests. The user is responsible for checking `/features` before creating new requests.

6. **Browser or web interface** — all interaction is via Telegram. No web UI for viewing or managing feature requests.

7. **Voting or ranking** — no upvote/downvote mechanism, no community ranking. Priority is set manually by the user.

8. **Automatic categorization** — no LLM-powered categorization beyond tag extraction. Tags are only from user-provided hashtags in the description.

9. **Dependency tracking** — no "blocked by" or "requires" relationships between features.

10. **Email or calendar integration** — no automatic creation of feature requests from email threads or meeting notes (user can manually create via `/feature` after reading the memory).

---

## Architecture

### Module Changes

**All feature tracker logic lives in `chat_handler.py`.**

No new modules are introduced. All functionality is implemented as new methods in the `ChatHandler` class and registered in `COMMAND_REGISTRY`.

**New methods in `ChatHandler`:**

```python
async def cmd_feature(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feature and /feature-new commands."""
    
async def cmd_features(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /features [status] command."""
    
async def cmd_feature_detail(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feature-detail N and /fdetail N commands."""
    
async def cmd_feature_priority(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feature-priority N <level> command."""
    
async def cmd_feature_plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feature-plan N command."""
    
async def cmd_feature_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feature-start N command."""
    
async def cmd_feature_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feature-done N [note] command."""
    
async def cmd_feature_wont_do(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feature-wont-do N [reason] command."""
    
async def cmd_feature_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /feature-note N <text> command."""

async def _resolve_feature_index(self, n: int, update: Update) -> str | None:
    """Resolve 1-based index to feature file path from _last_feature_set."""

def _rewrite_feature_frontmatter(
    self,
    file_path: str,
    frontmatter_updates: dict,
    body_updates: dict | None = None
) -> None:
    """Atomically update frontmatter and optionally body sections."""
```

**New instance variable in `ChatHandler.__init__()`:**

```python
self._last_feature_set: list[str] = []  # Populated by cmd_features()
```

**Dependencies:**

- `memory_writer.write_memory()` for atomic file creation
- `_parse_frontmatter()` for reading frontmatter from cached headers and full files
- `glob.glob()` for listing feature request files
- `hashlib.sha1()` for stable ID generation
- `yaml.safe_load()` and `yaml.dump()` for frontmatter parsing and serialization
- `tempfile.mkstemp()` and `os.rename()` for atomic writes

**No daemon loop changes:**

The feature tracker is entirely Telegram-command-driven. No background scanning or processing loop is needed.

---

## Testing Strategy

### Unit Tests

**File:** `tests/unit/test_chat_handler.py`

**Test cases:**

1. **`test_feature_creation_basic()`**
   - Mock `_check_auth` to pass
   - Send `/feature add dark mode`
   - Assert file created in `BRAIN_DIR/memories/` with:
     - `type: feature_request`
     - `status: new`
     - `priority: medium`
     - `title: "Add Dark Mode"` (from first 5 words)
     - `tags: []` (no hashtags)
     - Body contains "add dark mode" in `## Request` section
   - Assert reply contains title and ID

2. **`test_feature_creation_with_hashtags()`**
   - Send `/feature improve search performance #search #optimization #performance`
   - Assert file created with:
     - `tags: ["search", "optimization", "performance"]`
     - Body does not contain `#` symbols (hashtags stripped)
   - Assert reply confirms creation

3. **`test_features_list_default_filter()`**
   - Create fixtures:
     - 2 files with `status: new`
     - 2 files with `status: planned`
     - 1 file with `status: done`
   - Send `/features` (no arg)
   - Assert reply includes 4 items (new + planned only)
   - Assert `done` file not in reply
   - Assert `_last_feature_set` contains 4 paths

4. **`test_features_list_all()`**
   - Same fixtures as above
   - Send `/features all`
   - Assert reply includes all 5 items
   - Assert `_last_feature_set` contains 5 paths

5. **`test_features_list_specific_status()`**
   - Same fixtures
   - Send `/features done`
   - Assert reply includes only 1 item
   - Assert `_last_feature_set` contains 1 path

6. **`test_feature_detail_valid_index()`**
   - Create 1 fixture file with known content
   - Send `/features` to populate `_last_feature_set`
   - Send `/feature-detail 1`
   - Assert reply contains:
     - Title
     - Status, priority, created, updated
     - Tags
     - Full request body
     - Notes section

7. **`test_feature_detail_invalid_index()`**
   - Send `/feature-detail 99` without prior `/features`
   - Assert error reply: "No feature list loaded"
   - Create 1 fixture, send `/features`
   - Send `/feature-detail 5` (out of range)
   - Assert error reply: "Invalid index"

8. **`test_feature_priority_update()`**
   - Create fixture with `priority: medium`
   - Send `/features` then `/feature-priority 1 high`
   - Assert file rewritten with `priority: high`
   - Assert `updated` timestamp changed
   - Assert body content unchanged

9. **`test_feature_status_transitions()`**
   - Create fixture with `status: new`
   - Send `/features` then `/feature-plan 1`
   - Assert `status: planned`
   - Send `/feature-start 1`
   - Assert `status: in-progress`
   - Send `/feature-done 1 shipped in v1.2`
   - Assert `status: done`
   - Assert `## Notes` contains "shipped in v1.2" with timestamp

10. **`test_feature_note_append()`**
    - Create fixture
    - Send `/features` then `/feature-note 1 discussed with team`
    - Assert `## Notes` contains timestamped note
    - Send `/feature-note 1 started implementation`
    - Assert two notes in `## Notes` section

11. **`test_frontmatter_rewrite_preserves_body()`**
    - Create fixture with complex body (code blocks, multiple sections, special chars)
    - Send `/feature-priority 1 high`
    - Read file and assert:
      - `priority: high` updated
      - `## Request` content exactly unchanged
      - `## Context` content exactly unchanged

12. **`test_search_includes_features()`**
    - Create feature fixture with `tags: [optimization]` and title "Improve Search"
    - Send `/search optimization`
    - Assert reply includes the feature in "Feature Requests" group
    - Assert group appears in correct position (after Commitments, before Projects)

13. **`test_search_feature_filter()`**
    - Create 1 feature fixture and 1 project fixture, both with "database" keyword
    - Send `/search feature database`
    - Assert reply includes only the feature request, not the project

14. **`test_feature_command_auth()`**
    - Mock `_check_auth` to fail (raise exception or return False)
    - Send `/feature test`, `/features`, `/feature-detail 1`
    - Assert no files created, no replies sent (silent rejection)

15. **`test_feature_filename_collision_resistance()`**
    - Create two features with identical descriptions but 1 second apart
    - Assert two separate files created (different hashes due to timestamp)

### Integration Tests

**File:** `tests/integration/test_feature_workflow.py`

**Test case:**

1. **`test_full_feature_lifecycle()`**
   - Send `/feature add export to JSON #export`
   - Assert file created
   - Send `/features`
   - Assert reply lists the feature
   - Send `/feature-priority 1 high`
   - Send `/feature-plan 1`
   - Send `/feature-note 1 assigned to Q2 roadmap`
   - Send `/feature-start 1`
   - Send `/feature-done 1 shipped in v1.3.0`
   - Send `/features done`
   - Assert reply shows the completed feature
   - Send `/feature-detail 1`
   - Assert reply shows:
     - `status: done`
     - `priority: high`
     - Two notes with timestamps

### Test Fixtures

**Fixture factory for feature requests:**

```python
import tempfile, yaml
from datetime import datetime, timezone

def create_feature_fixture(
    tmp_path,
    title="Test Feature",
    status="new",
    priority="medium",
    tags=None,
    request_text="This is a test feature request.",
    notes_text=""
):
    """Create a feature request memory file for testing."""
    if tags is None:
        tags = []
    
    timestamp = datetime.now(timezone.utc).isoformat()
    slug = title.lower().replace(" ", "-")[:40]
    stable_id = "abc123"
    filename = f"feature-request-{slug}-{stable_id}.md"
    
    frontmatter = {
        "title": title,
        "type": "feature_request",
        "status": status,
        "priority": priority,
        "created": timestamp,
        "updated": timestamp,
        "tags": tags,
        "source_url": f"feature:{stable_id}",
    }
    
    body = f"""## Request

{request_text}

## Context

Captured via test fixture.

## Notes

{notes_text}"""
    
    content = f"---\n{yaml.dump(frontmatter, sort_keys=False)}---\n{body}"
    
    file_path = tmp_path / "memories" / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    
    return str(file_path)
```

---

## Deployment Checklist

1. **Code changes:**
   - [ ] Add new methods to `ChatHandler` class in `chat_handler.py`
   - [ ] Add `_last_feature_set` instance variable
   - [ ] Add `COMMAND_REGISTRY` entries under "Feature Requests" group
   - [ ] Add command handler registrations in `__init__()`
   - [ ] Update search grouping logic to include feature requests

2. **Tests:**
   - [ ] Write 15 unit tests in `tests/unit/test_chat_handler.py`
   - [ ] Write 1 integration test in `tests/integration/test_feature_workflow.py`
   - [ ] Run `pytest` and confirm all tests pass

3. **Documentation:**
   - [ ] Update `README.md` with `/feature` command examples
   - [ ] Add "Feature Requests" section to Telegram commands documentation
   - [ ] Update `CLAUDE.md` if any new patterns introduced

4. **Deployment:**
   - [ ] Commit changes with message referencing this spec
   - [ ] Run `./install.sh` to deploy to `~/secondbrain/`
   - [ ] Reload daemon via launchctl
   - [ ] Test via Telegram: create, list, detail, update status, add note

5. **Validation:**
   - [ ] Create a real feature request via Telegram
   - [ ] Confirm file appears in `BRAIN_DIR/memories/`
   - [ ] Run `/search feature <keyword>` and confirm filtering works
   - [ ] Update priority and status, confirm file updates correctly
   - [ ] Check that body content is never corrupted by frontmatter updates

---

## Changelog

### v1.0.0 — 2026-04-12

Initial spec for Feature Request Tracker.

**Scope:**
- Nine Telegram commands for creating and managing feature requests
- Memory file format: `type: feature_request` with structured frontmatter
- Status lifecycle: new → planned → in-progress → done | wont-do
- Priority levels: low, medium, high, critical
- Tag extraction from hashtags in description
- Atomic frontmatter updates preserving body content
- Session-local result set indexing
- Automatic searchability via existing `/search` command

**Architecture:**
- All logic in `chat_handler.py` (no new modules)
- Two new helpers: `_resolve_feature_index()`, `_rewrite_feature_frontmatter()`
- 15 unit tests + 1 integration test

**Non-goals:**
- External integrations, team collaboration, due dates, automatic status updates, duplicate detection, web UI

---

## Appendix: Example Session

```
User: /feature add configurable morning briefing #telegram #automation

Bot: Feature request captured: 'Add Configurable Morning Briefing' (ID: a1b2c3).
     Use /features to view all.

User: /features

Bot: Feature requests (new + planned):
     
     1. [new] [medium] Add Configurable Morning Briefing (created: 2026-04-12)
     
     Use /feature-detail N for full details.
     Use /feature-plan N, /feature-start N, /feature-done N to update status.

User: /feature-detail 1

Bot: 📋 Add Configurable Morning Briefing
     
     Status: new
     Priority: medium
     Created: 2026-04-12T10:30:00
     Updated: 2026-04-12T10:30:00
     Tags: telegram, automation
     ID: a1b2c3
     
     ## Request
     
     add configurable morning briefing
     
     ## Notes
     
     (none)

User: /feature-priority 1 high

Bot: Priority updated: 'Add Configurable Morning Briefing' is now high.

User: /feature-plan 1

Bot: Feature 'Add Configurable Morning Briefing' marked as planned.

User: /feature-note 1 scheduled for Q2 sprint

Bot: Note added to 'Add Configurable Morning Briefing'.

User: /search telegram

Bot: Search results for "telegram" — 3 matches
     
     Feature Requests (1)
       1. Add Configurable Morning Briefing (created: 2026-04-12)
     
     Commitments (1)
       2. [outbound] Respond to Tom about Telegram bot feature
     
     Web memories (1)
       3. Telegram Bot API documentation (2026-04-10)
     
     Use /memory N for detail on any item.

User: /feature-start 1

Bot: Feature 'Add Configurable Morning Briefing' is now in progress.

User: /feature-done 1 Implemented and tested in v1.4

Bot: Feature 'Add Configurable Morning Briefing' marked as done.

User: /features done

Bot: Feature requests (done):
     
     1. [done] [high] Add Configurable Morning Briefing (created: 2026-04-12)
     
     Use /feature-detail N for full details.
```
