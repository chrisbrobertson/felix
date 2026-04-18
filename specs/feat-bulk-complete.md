---
specmas: 3.0
kind: bug
id: feat-bulk-complete
version: 1.0.0
created: 2026-04-17
status: implemented
shipped_version: "1.4.0"
complexity: small
maturity: 1
parent_system: second-brain
related_specs:
  - feat-commitment-tracker
---

# Bulk Commitment Complete / Dismiss

## Overview

### Problem Statement

`/complete N` accepts exactly one index, requiring users to run the command once per commitment. After a busy day with many commitments to close out, this is tedious. Filed as bug `709fce`.

### Scope

**In scope:**
- `/complete N [M P ...]` — accept one or more space-separated indices
- `/dismiss N [M P ...]` — same treatment
- Per-item success/failure reported in a single reply
- "All done" summary line at the end

**Out of scope:**
- `/complete all` (too destructive without explicit confirmation flow)
- Range syntax like `/complete 1-5`
- Bulk-completing goal or project milestones (separate commands)

### Success Metrics

- `/complete 1 3 5` marks three commitments complete and replies with three ✓ lines
- A bad index in a multi-arg call reports an error for that index but still processes the rest

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `/complete` accepts one or more positional args, each an integer index into the last `/commitments` result set |
| FR-2 | `/dismiss` accepts the same multi-arg syntax |
| FR-3 | Each index is resolved independently; failure on one does not abort the rest |
| FR-4 | Reply lists each result on its own line (✓ or ✗ + label), then a summary line |
| FR-5 | If no args are passed, existing `Usage: /complete N` message is shown |
| FR-6 | Duplicate indices are processed once (deduplicated) |

---

## Design

### `cmd_complete` refactor in `chat_handler.py`

```python
async def cmd_complete(self, update, context):
    if not self._check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /complete N [M P ...]")
        return

    from commitment_tracker import CommitmentTracker
    lines = []
    seen = set()
    for arg in context.args:
        if arg in seen:
            continue
        seen.add(arg)
        path = self._resolve_commitment_index(arg)
        if path is None:
            lines.append(f"✗ #{arg}: not found (run /commitments to refresh the list)")
            continue
        fm = self._parse_frontmatter(path)
        title = fm.get("source_title") or "commitment"
        owner = fm.get("owner", "")
        label = f'"{title}"' + (f" ({owner})" if owner else "")
        try:
            CommitmentTracker().update_commitment_status(path, "completed")
            lines.append(f"✓ {label}")
        except Exception as e:
            lines.append(f"✗ {label}: {e}")

    await update.message.reply_text("\n".join(lines))
```

Same pattern applied to `cmd_dismiss` (status `"dismissed"`, symbol `✗`).

---

## Test Plan

**Unit tests in `tests/unit/test_chat_handler.py`:**

1. `test_complete_single_index` — existing single-index behavior unchanged, returns `✓` line
2. `test_complete_multiple_indices` — `/complete 1 2` marks two commitments, reply has two lines
3. `test_complete_partial_failure` — one bad index returns error line, good index still completes
4. `test_complete_deduplicates_args` — `/complete 1 1` only processes index 1 once
5. `test_complete_no_args_shows_usage` — empty args shows usage string
6. `test_dismiss_multiple_indices` — same as complete but for dismiss
