---
specmas: 3.0
kind: feature
id: feat-commitment-tracker
version: 1.0.0
created: 2026-04-11
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-email-scanner
  - feat-zoom-transcript-scanner
---

# Commitment Tracker

## Overview

### Problem Statement

The bot accumulates memories of meetings, emails, and conversations, but has no way
to extract and track the commitments embedded in them. After a meeting, "I'll send
you the report by Friday" lives only in the transcript memory file. After an email
thread, "Let me get back to you on that" is buried in a summary. There is no way to
ask the bot "what do I owe people right now?" or "what am I waiting on from Sarah?".

The Commitment Tracker scans newly-written memory files for commitments and waiting-on
items, writes one file per extracted item, and exposes them through Telegram commands
for review and status management.

### Scope

**In Scope:**
- Eighth async daemon loop, running every 5 minutes (`full` role only)
- Scan `meeting_transcript` and `email_thread` memory files for new/updated content
- LLM-powered extraction of commitments (outbound, inbound) and waiting-on items
- One `commitment-{slug}-{id}.md` file per extracted item
- Confidence-based filtering (discard below threshold, flag low-confidence)
- Status management: `active`, `completed`, `dismissed`
- Telegram commands: `/commitments`, `/complete N`, `/dismiss N`
- Extensible `source_types` config for future Slack integration

**Out of Scope:**
- Web page memories (type: `webpage`) — commitments rarely arise from reading articles
- Project memories (type: `code_project`) — no natural-language commitments
- Automatic completion detection (status must be set manually via Telegram)
- Deadline reminders / push notifications (the bot answers questions; it does not push)
- Email or calendar integration for cross-referencing commitment status
- Duplicate detection across source files (same commitment mentioned in email and meeting)
- Multi-user or shared commitment tracking

### Success Metrics

- Commitment extraction precision > 80% (extracted items are real commitments)
- Commitment extraction recall > 70% (real commitments from meetings and emails caught)
- Processing latency < 10 minutes after source memory file is written
- `/commitments` responds within 2 seconds for up to 500 active items

---

## Functional Requirements

### FR-1: Scan New and Updated Source Memory Files

Monitor `MEMORIES_DIR` for memory files of configured source types that have been
created or modified since the last scan cycle.

**Source types (v1):** `meeting_transcript`, `email_thread`
Configured via `commitment_tracker.source_types` in `config.yaml`.

**Change detection:**
- Load `DEPLOY_DIR/commitment-scanner-state.json` on startup
- State stores `last_scan: ISO timestamp` and `processed: {filename: mtime}` map
- On each cycle: glob `MEMORIES_DIR/*.md`, read frontmatter `type` field (cached header)
- For files matching `source_types`: compare current mtime to `processed[filename]`
- Process files where mtime has changed or file is not in `processed`
- After processing, update `processed[filename] = current_mtime`
- Save state after each file (not batch) to survive mid-cycle crashes
- Cap at 30 files per cycle to bound LLM cost

**Validation criteria:**
- New source memory files processed within one scan cycle
- Re-processed when source memory is updated (e.g., email thread gains new messages)
- Files of other types (webpage, code_project) skipped without reading content
- Daemon restart does not reprocess already-processed files

---

### FR-2: LLM Commitment Extraction

Send source memory content to LLM and parse structured commitment/waiting-on items.

**LLM route:** `summarize` (Gemini Flash via LiteLLM)

**Prompt input:**
- Source type and title
- Participants / speakers list
- Summary section from the memory file
- Full `## Messages` or `## Transcript` section (capped at 2000 chars)

**Prompt structure:**
```
Extract commitments and waiting-on items from this {type}.

Source: {source_title}
Participants: {participant list}
Date: {meeting_date or last_message}

Content:
{summary section}

{messages/transcript section, capped at 2000 chars}

Return JSON only:
{
  "commitments": [
    {
      "type": "outbound",
      "description": "Send revised budget numbers",
      "owner": "Sarah Chen",
      "owner_email": "sarah.chen@acme.com",
      "recipient": "Chris",
      "due_date": "2026-04-18",
      "due_date_confidence": "explicit",
      "confidence": 0.85,
      "extracted_text": "Can you commit to having the revised numbers by Friday?"
    }
  ]
}

Commitment types:
- outbound: a promise made by someone to do something for another person
- inbound: a promise someone made to the user (user is recipient)
- waiting_on: the user is waiting for someone else to act or respond

Only include items with confidence >= 0.5. Return [] if none found.
```

**Extraction signals (from EA-AI intelligence engine FR-1/FR-2):**

| Signal | Examples | Confidence |
|--------|----------|------------|
| Explicit promise | "I will send the report", "I'll get back to you" | 0.8–1.0 |
| Action verb + timeline | "I'll have it by Friday", "Expect it next week" | 0.8–0.95 |
| Request acknowledgment | "Sure, I can do that", "Yes, I'll handle it" | 0.6–0.8 |
| Implied obligation | "Let me look into that", "I'll check on that" | 0.5–0.7 |
| Direct question/request (waiting_on) | "Can you send me the data?", "Please review and approve" | 0.8–1.0 |
| Follow-up reference | "Per our discussion, you'll be sending…" | 0.6–0.8 |
| Implicit expectation (waiting_on) | "Let me know your thoughts", "Looking forward to your input" | 0.4–0.6 |

**Validation criteria:**
- Returns valid JSON matching the extraction schema
- JSON parse failure logged (WARNING) and file skipped (not crashed)
- `extracted_text` contains actual quoted text from the source, not a paraphrase
- Empty array returned (not hallucinated items) when no commitments present
- `confidence` values ≥ 0.5 only (prompt instructs LLM to filter; scanner also enforces)

---

### FR-3: One File Per Commitment with Stable Dedup ID

Write one `commitment-{slug}-{id}.md` file per extracted commitment or waiting-on item.

**Stable ID:**
```python
import hashlib
stable_id = hashlib.sha1(
    f"{source_url}:{description.lower().strip()}:{owner.lower().strip()}"
    .encode()
).hexdigest()[:12]
```

**Filename:**
```
commitment-{slug}-{stable-id}.md
```
where `slug` is the description lowercased, spaces → hyphens, max 40 chars.

**File format:**
```markdown
---
source_title: "Send revised budget numbers"
summary: Sarah Chen committed to send revised budget numbers by 2026-04-18 (Friday)
tags: [budget, q4-planning]
last_scanned: '2026-04-11T15:23:00'
source_url: commitment:abc123def456
type: commitment
commitment_type: outbound
owner: Sarah Chen
owner_email: sarah.chen@acme.com
recipient: Chris
due_date: '2026-04-18'
due_date_confidence: explicit
confidence: 0.85
status: active
source_memory: zoom:meeting-uuid-abc123
extracted_text: "Can you commit to having the revised numbers by Friday?"
---

## Context
Extracted from Q4 Planning Review meeting on 2026-04-11 with Sarah Chen and Mike Peters.
```

**Frontmatter field order** (`sort_keys=False`):
`source_title`, `summary`, `tags`, `last_scanned`, `source_url`, `type`,
`commitment_type`, `owner`, `owner_email`, `recipient`, `due_date`,
`due_date_confidence`, `confidence`, `status`, `source_memory`, `extracted_text`

**Write rules:**
- Atomic write via temp file + `os.rename()`
- If file already exists (same stable ID): update `last_scanned` and `status` only if status has changed externally; otherwise skip
- Existing `status` (completed/dismissed) must not be overwritten on re-extraction

**Validation criteria:**
- Stable ID is deterministic — same commitment from two re-runs produces the same filename
- `type: commitment` in all written files
- `source_url` uses `commitment:` scheme
- Completed/dismissed items not reverted to active on re-scan

---

### FR-4: Confidence Scoring and Filtering

Apply thresholds to control which commitments are surfaced.

**Thresholds (from EA-AI intelligence engine FR-6):**

| Confidence | Action |
|------------|--------|
| ≥ 0.7 | Write commitment file with `status: active` |
| 0.5–0.69 | Write commitment file with `tags` including `needs-review` |
| < 0.5 | Discard — do not write file |

The extraction prompt already instructs the LLM to return only items ≥ 0.5, but the
scanner enforces this threshold independently as a safety check.

**Validation criteria:**
- Items below threshold discarded without logging at INFO level (DEBUG only)
- `needs-review` tag visible in `/commitments` output
- Threshold configurable via `commitment_tracker.min_confidence` in `config.yaml`

---

### FR-5: Status Management

Track the lifecycle of each commitment from extraction to completion or dismissal.

**Valid statuses:**
- `active` — item requires attention (default on creation)
- `completed` — user confirmed it is done (set via `/complete N`)
- `dismissed` — false positive, irrelevant, or already handled (set via `/dismiss N`)

**Rules:**
- New extractions always start as `active`
- Status transitions: `active` → `completed` or `active` → `dismissed`
- Completed/dismissed items are not re-set to `active` by the scanner (re-extraction respects existing status)
- Status change writes atomically update the commitment file in-place

**Validation criteria:**
- Status field present in all commitment files
- Status update writes atomically (temp file + rename)
- Completed/dismissed items excluded from `/commitments` default listing

---

### FR-6: `/commitments` Telegram Command

List active commitment files with index numbers for use with `/complete` and `/dismiss`.

**Usage:** `/commitments [type]`

**Optional `type` filter:** `outbound`, `inbound`, `waiting` (matches `waiting_on`)

**Behaviour:**
- Glob `MEMORIES_DIR/commitment-*.md`
- Filter to `status: active` (and optionally by `commitment_type`)
- Sort by `due_date` ascending (nulls last), then `last_scanned` descending
- Store result set in `_last_commitment_set` (same session-scoped pattern as `/memories`)
- Reply with numbered list, up to 20 items; include count of total active

**Reply format:**
```
Active commitments (12 total):
1. [outbound] Send revised budget numbers — Sarah Chen → due 2026-04-18
2. [waiting_on] Waiting for vendor quote from Mike — due unknown
3. [inbound] Alex to share design mockups — due 2026-04-15 ⚠️ needs-review
...

Use /complete N or /dismiss N to update status.
```

Items tagged `needs-review` shown with ⚠️ indicator.

**Validation criteria:**
- Responds within 2 seconds for up to 500 commitment files
- Empty list returns friendly message, not an error
- Index numbers consistent with subsequent `/complete`/`/dismiss` calls within session

---

### FR-7: `/complete N` Telegram Command

Mark a commitment as completed.

**Usage:** `/complete N`

**Behaviour:**
- Look up index N from `_last_commitment_set`
- Read the commitment file
- Set `status: completed`, update `last_scanned`
- Atomic write back
- Reply confirming the update

**Reply format:**
```
✓ Marked complete: "Send revised budget numbers" (Sarah Chen)
```

**Validation criteria:**
- Invalid index returns clear error ("Invalid index. Run /commitments first.")
- `status: completed` written correctly to file
- Idempotent — completing an already-completed item returns success, not error

---

### FR-8: `/dismiss N` Telegram Command

Mark a commitment as dismissed (false positive or no longer relevant).

**Usage:** `/dismiss N`

**Behaviour:**
- Same as FR-7 but sets `status: dismissed`

**Reply format:**
```
✗ Dismissed: "Let me look into that" (Mike Peters)
```

**Validation criteria:**
- Same as FR-7 validation
- Dismissed items not shown in future `/commitments` listings

---

### FR-9: Skip Unchanged Source Files

Avoid re-extracting commitments from source files that have not changed since last processing.

**Mechanism:**
- `processed` map in `DEPLOY_DIR/commitment-scanner-state.json` stores `{filename: mtime}`
- On each cycle, only process files where current `os.stat().st_mtime != processed[filename]`
- Update `processed[filename]` after successful extraction (even if zero commitments found)

**Validation criteria:**
- Unchanged email threads and meeting transcripts not re-extracted
- File updated (e.g., email thread gains new messages) triggers re-extraction
- Re-extraction of an updated file does not duplicate existing commitment files (stable ID check)

---

### FR-10: Extensible Source Types

The `source_types` config controls which memory file types are scanned. Adding Slack or other types in a future feature requires only adding the type string to the config — no code change.

**Config:**
```yaml
commitment_tracker:
  source_types:
    - meeting_transcript
    - email_thread
    # - slack_thread  # add when slack scanner is implemented
```

**Validation criteria:**
- Unknown source types in config logged at WARNING, not crashed
- Removing a source type from config stops scanning that type on next cycle

---

## Config

```yaml
commitment_tracker:
  interval_seconds: 300
  min_confidence: 0.5
  source_types:
    - meeting_transcript
    - email_thread
```

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `specs/feat-commitment-tracker.md` | **This spec** |
| `commitment_tracker.py` | **Create** — CommitmentTracker class + extraction prompt |
| `chat_handler.py` | Add `/commitments`, `/complete`, `/dismiss` command handlers |
| `daemon.py` | Add CommitmentTracker to full-role gather |
| `config.yaml.template` | Add `commitment_tracker` section |
| `CLAUDE.md` | Update to eight async loops, add CommitmentTracker + commands |
| `README.md` | Document commitment tracker, new Telegram commands |
| `tests/unit/test_commitment_tracker.py` | **Create** |
| `tests/integration/test_pipeline.py` | Add commitment tracker integration test |

---

## Unit Tests (`tests/unit/test_commitment_tracker.py`)

| Test | Assertion |
|------|-----------|
| `test_stable_id_deterministic` | Same inputs → same ID across calls |
| `test_stable_id_different_description` | Different description → different ID |
| `test_confidence_filter_discards_below_threshold` | confidence=0.4 → file not written |
| `test_confidence_filter_needs_review_tag` | confidence=0.6 → `needs-review` in tags |
| `test_confidence_filter_auto_accept` | confidence=0.8 → no `needs-review` tag |
| `test_write_commitment_field_order` | `source_title` first, `type: commitment` present |
| `test_write_commitment_atomic` | No temp file left after write |
| `test_write_commitment_preserves_status` | Existing `completed` not overwritten |
| `test_scan_skips_unchanged_mtime` | Same mtime → no LLM call |
| `test_scan_processes_new_mtime` | Updated mtime → LLM call made |
| `test_scan_skips_wrong_type` | `type: webpage` not processed |
| `test_extraction_empty_returns_no_files` | `{"commitments": []}` → no files written |
| `test_extraction_json_parse_error_logs_warning` | Invalid JSON → WARNING, no crash |
| `test_cmd_commitments_returns_active_only` | completed/dismissed items excluded |
| `test_cmd_commitments_filter_outbound` | `/commitments outbound` → only outbound items |
| `test_cmd_commitments_sorted_by_due_date` | Items sorted due-date ascending, nulls last |
| `test_cmd_complete_updates_status` | `/complete 1` → `status: completed` in file |
| `test_cmd_dismiss_updates_status` | `/dismiss 1` → `status: dismissed` in file |
| `test_cmd_complete_invalid_index` | `/complete 99` with empty set → clear error |
| `test_cmd_complete_idempotent` | Completing already-complete item → success |
| `test_needs_review_indicator_in_listing` | needs-review item shows ⚠️ in reply |
| `test_state_file_persists_across_scans` | State file survives daemon restart |
| `test_dedup_same_source_two_runs` | Two scans of same source → no duplicate files |
