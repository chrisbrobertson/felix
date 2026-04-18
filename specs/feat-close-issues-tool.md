---
specmas: 3.0
kind: bug
id: feat-close-issues-tool
version: 1.0.0
created: 2026-04-17
status: implemented
shipped_version: "1.4.0"
complexity: small
maturity: 1
parent_system: second-brain
related_specs:
  - feat-feature-tracker
---

# Close Issues via Conversation

## Overview

### Problem Statement

`chat_tools.py` exposes `add_bug` and `add_feature` so the agent can file issues via conversation. But there is no `close_issue` tool — the agent cannot mark a bug or feature as done, won't_do, or in_progress via natural language. Users must use the `/close` slash command directly. Filed as bug `6d364b`.

### Scope

**In scope:**
- New `close_issue` tool in `chat_tools.py`
- Accepts a `short_id` (6-char hash shown in `/features` and `/bugs` listings) or a partial title string
- Sets `status` field in the YAML frontmatter of the matching file
- Valid status values: `done`, `wont_do`, `in_progress`
- Reuses the same iCloud memories directory scan as the existing `/close` command

**Out of scope:**
- Bulk-closing multiple issues in one call
- Deleting issues (status change only)
- Changing `priority` or `kind` via tool

### Success Metrics

- "Mark bug 6d364b as done" results in the agent calling `close_issue` and the file's `status` flipping to `done`
- Title-based search ("close the PDF bug") finds and closes the right file

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `close_issue` tool accepts `short_id` (exact 6-char match) or `title` (case-insensitive substring search) |
| FR-2 | `status` parameter accepts `done`, `wont_do`, `in_progress` (default `done`) |
| FR-3 | Searches all `feature-request-*.md` files in the memories directory |
| FR-4 | If `short_id` is given and matches, updates that file; if no match, returns a "not found" message |
| FR-5 | If `title` search matches exactly one file, updates it; if multiple matches, lists them and asks for clarification |
| FR-6 | Returns confirmation including the file's title and new status |
| FR-7 | Status update uses atomic write (tmp → rename) to avoid partial iCloud sync |

---

## Design

### New tool schema in `chat_tools.py`

```python
{
    "type": "function",
    "function": {
        "name": "close_issue",
        "description": (
            "Close, resolve, or update the status of a bug or feature request. "
            "Use when the user says something like 'mark that bug as done', "
            "'close feature 6d364b', or 'that issue is fixed'. "
            "Provide either short_id (the 6-char hash) or a title substring."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "short_id": {
                    "type": "string",
                    "description": "6-character hash ID shown in /features or /bugs listings",
                },
                "title": {
                    "type": "string",
                    "description": "Partial title to search for when short_id is unknown",
                },
                "status": {
                    "type": "string",
                    "description": "New status to set (default: done)",
                    "enum": ["done", "wont_do", "in_progress"],
                },
            },
        },
    },
}
```

### Dispatch implementation in `chat_tools.py` `_call()`

```python
if name == "close_issue":
    return handler._close_issue_text(
        short_id=arguments.get("short_id"),
        title=arguments.get("title"),
        status=arguments.get("status", "done"),
    )
```

### `_close_issue_text()` in `chat_handler.py`

```python
def _close_issue_text(self, short_id=None, title=None, status="done") -> str:
    memories = list((BRAIN_DIR / "memories").glob("feature-request-*.md"))
    match = None

    if short_id:
        for f in memories:
            fm = self._parse_frontmatter(f)
            if fm.get("short_id") == short_id:
                match = f
                break
        if not match:
            return f"No issue found with ID {short_id!r}."

    elif title:
        hits = [f for f in memories
                if title.lower() in self._parse_frontmatter(f).get("title", "").lower()]
        if not hits:
            return f"No issue found matching {title!r}."
        if len(hits) > 1:
            lines = [f"Multiple matches — be more specific:"]
            for h in hits[:5]:
                fm = self._parse_frontmatter(h)
                lines.append(f"• [{fm.get('short_id')}] {fm.get('title', '')[:60]}")
            return "\n".join(lines)
        match = hits[0]

    else:
        return "Provide either short_id or title."

    # Atomic status update
    text = match.read_text()
    updated = re.sub(r'^status:\s*\S+', f'status: {status}', text, flags=re.MULTILINE)
    tmp = match.with_suffix(".tmp")
    tmp.write_text(updated)
    os.rename(tmp, match)

    fm = self._parse_frontmatter(match)
    return f"Closed [{fm.get('short_id')}] {fm.get('title', '')[:60]} → {status}"
```

---

## Test Plan

**Unit tests in `tests/unit/test_chat_tools.py` and `tests/unit/test_chat_handler.py`:**

1. `test_close_issue_by_short_id` — matching short_id updates status to `done`
2. `test_close_issue_by_title` — single title match updates status
3. `test_close_issue_title_ambiguous` — multiple matches returns disambiguation list
4. `test_close_issue_not_found` — missing ID returns "not found" message
5. `test_close_issue_custom_status` — `status: wont_do` written correctly
6. `test_close_issue_tool_in_tools_list` — `close_issue` present in TOOLS constant
7. `test_agent_can_dispatch_close_issue` — dispatch routes correctly
