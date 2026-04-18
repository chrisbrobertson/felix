---
specmas: 3.0
kind: feature
id: feat-email-scanner
version: 1.2.0
created: 2026-04-11
status: implemented
shipped_version: "1.3.0"
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-memory-management
  - feat-code-project-scanner
---

# Email Scanner

## Overview

### Problem Statement

The bot knows about web pages the user has read and git repos they are building,
but has no awareness of email correspondence. Decisions, context, and
relationships live in email threads. Without this, the bot cannot answer
questions like "what's the latest on the API migration?", "what did Sarah say
about the Q3 budget?", or "which active threads mention the launch date?".

### Scope

**In Scope:**
- Sixth async daemon loop, running every 5 minutes
- Reads Apple Mail.app data via SQLite (Envelope Index) with AppleScript fallback
- One living memory file per conversation thread, updated in place
- Incremental polling — only processes new messages after initial load
- Initial load: last 30 days (configurable up to 90)
- Excluded mailboxes: Trash, Junk, Spam, Archive, Deleted Messages (configurable)
- LLM-generated summary from message snippets + subjects (not full bodies)
- Change detection via `message_count` + `last_message` — skips write when unchanged
- Thread archival after N days of inactivity (default 90)
- Full rescan on demand via config flag

**Out of Scope:**
- Full email body indexing (privacy + token cost)
- Email sending or modification
- Non-macOS mail clients
- Attachment indexing
- Cross-machine sync beyond what iCloud handles automatically

### Success Metrics

- Active conversation threads produce `email-thread-*.md` memory files within
  the first scan cycle
- Threads with no new messages produce zero file writes on subsequent scans
- Each memory file scannable by the chat bot header cache (source_title, summary,
  tags, last_scanned within first 200 chars)
- LLM called at most once per thread per new message batch

---

## Functional Requirements

### FR-1: Mail data source detection
**Priority:** Critical

On each scan cycle, attempt to open the Envelope Index SQLite database at
`~/Library/Mail/V*/Envelope Index` (glob for highest version number). If the
file is inaccessible (PermissionError — Full Disk Access not granted) or does
not exist, log a clear warning and fall back to AppleScript. The fallback
requires Mail.app to be running; if it is not running, skip the scan cycle and
log a warning.

---

### FR-2: Thread discovery — SQLite path
**Priority:** Critical

Copy the Envelope Index to `/tmp/Envelope Index` before reading (WAL lock
avoidance, same pattern as `browser_watcher.py` for Chrome History). Open
read-only: `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`.

For initial load, query messages where `date_received` > the lookback cutoff.
For incremental polls, query messages where `ROWID` > the stored high-water mark.
Group results by `conversation_id`. Filter out mailboxes whose URL path component
matches the `skip_mailboxes` list (case-insensitive).

Core Data timestamp conversion: SQLite stores seconds since 2001-01-01;
add 978307200 to get Unix epoch.

Key query:
```sql
SELECT m.conversation_id, s.subject, m.date_received, m.date_sent,
       m.snippet, m.read, m.flagged, a.address, a.comment,
       mb.url AS mailbox_url, m.ROWID
FROM messages m
JOIN subjects s  ON m.subject  = s.ROWID
JOIN addresses a ON m.sender   = a.ROWID
JOIN mailboxes mb ON m.mailbox = mb.ROWID
WHERE m.date_received > ? AND m.deleted = 0
ORDER BY m.conversation_id, m.date_received ASC
```

---

### FR-3: Thread discovery — AppleScript fallback
**Priority:** High

When Envelope Index is unavailable, call `osascript` via subprocess
(`timeout=30`). Iterate over all accounts and non-excluded mailboxes. Group
messages by normalized subject (strip leading "RE:", "FW:", "FWD:", "AW:",
"R:", case-insensitive, repeat until clean). Use a deterministic SHA-1 hash of
the normalized subject as the `conversation_id` so filenames remain stable.

---

### FR-4: Incremental polling + state file
**Priority:** Critical

After the initial load, store the maximum ROWID seen in
`DEPLOY_DIR/email-scanner-state.json`. On each subsequent cycle, only query
messages with `ROWID > high_water_rowid`. Update the state file atomically after
each successful scan. The state file also stores `last_scan_time` and
`data_source` ("envelope_index" or "applescript").

When `email_scanner.full_rescan: true` in config, reset the high-water mark to
zero (triggers full lookback), then set `full_rescan` back to false in the
config file after the scan completes.

---

### FR-5: Change detection
**Priority:** Critical

Before writing a memory file, compare the DB's current `message_count` and
`last_message` timestamp for the thread against the values in the existing
memory file's frontmatter. If both match, skip the write. This keeps 5-minute
polls cheap.

---

### FR-6: Summary and tags generation
**Priority:** High

On first scan (no existing memory file) or when `message_count` has increased:
- Build a prompt from: thread subject, participant addresses, and per-message
  `{date} {sender_name}: {snippet}` lines (most recent 10 messages, capped at
  3000 chars total)
- Call LiteLLM `summarize` route (gemini-flash) with a short inline prompt
- Parse `SUMMARY:` and `TAGS:` lines from response

When `message_count` unchanged: reuse `summary` and `tags` from existing
memory file frontmatter. Do not call the LLM.

When LLM call fails: use the thread subject as summary, tags derived from
participant email domains and subject keywords.

---

### FR-7: Memory file write
**Priority:** Critical

Write `BRAIN_DIR/memories/email-thread-{slug}-{conversation_id}.md` atomically
(tmp + rename). Field order in frontmatter:

```
source_title, summary, tags, last_scanned,
source_url, type, participants, message_count,
last_message, first_message, conversation_id
```

`type` is always `email_thread`. `source_url` is `mailto:conversation-{id}`.

---

### FR-8: Thread archival
**Priority:** Medium

Threads with no activity in `archive_after_days` (default 90) stop being
updated — `_needs_update()` returns False regardless of other factors. The
existing memory file is preserved but not rewritten. This prevents stale threads
from consuming scan time.

---

### FR-9: Excluded mailboxes
**Priority:** High

Configurable `skip_mailboxes` list. Defaults: Trash, Junk, Spam, Archive,
Deleted Messages. Comparison is case-insensitive against the final path component
of the `mailboxes.url` field (e.g. `mailbox://user@host/INBOX/Trash` → "Trash").

---

### FR-10: Rate limiting
**Priority:** Medium

Process at most 50 threads per scan cycle. On initial load with many threads,
subsequent cycles catch up by processing the next 50. This prevents a single
long-running cycle that would block the stop event check.

---

### FR-11: Content classification
**Priority:** High

Every new or updated thread is classified into one of five content buckets
during the same LLM call that generates summary + tags:

| label | meaning |
|---|---|
| `human` | Real person-to-person correspondence (colleagues, vendors, family, friends). Default for ambiguous cases. |
| `transactional` | Receipts, order/shipping notifications, account security alerts, calendar invites from services. |
| `marketing` | Newsletters, promotions, sales pitches, product announcements. |
| `automated` | CI/CD alerts, monitoring digests, build reports, OTP codes, password resets. |
| `unknown` | LLM failure or low-confidence response. Treated as `human` by downstream consumers. |

The LLM prompt is extended to return a `CLASSIFICATION:` line alongside
`SUMMARY:` and `TAGS:`. The classifier runs on the same subject +
participants + per-message snippets already available; no full body
access. The AppleScript fallback path has no snippets, so classification
degrades to subject + sender — still useful for obvious marketing.

Downstream consumers that read email memories (`contact_tracker`,
`commitment_tracker`, `chat_handler`) skip any thread whose classification
is `marketing` or `automated` by default. `transactional` is also skipped
by contact/commitment trackers but remains visible in `/comms email`
because users do sometimes care about receipts. `human` and `unknown` are
always processed.

Kill-switch: `email_scanner.classification_enabled: true` in config
disables the classification line (fallback to empty string, treated as
`unknown`/`human`).

---

## Memory File Format

```markdown
---
source_title: "RE: API Migration Timeline"
summary: Sarah Chen and Chris discussed v2 endpoint cutover, settling on May 15.
tags: [acme, api-migration, engineering]
classification: human
last_scanned: '2026-04-11T15:00:00'
source_url: mailto:conversation-12345
type: email_thread
participants: [sarah.chen@acme.com, chris@company.com]
message_count: 4
last_message: '2026-04-10T09:30:00'
first_message: '2026-04-05T14:00:00'
conversation_id: 12345
---

## Messages
- 2026-04-10 Sarah Chen: Confirmed May 15 cutover date, pending QA sign-off
- 2026-04-08 Chris: Proposed May 15 or June 1 options
- 2026-04-07 Sarah Chen: Need to align with QA schedule first
- 2026-04-05 Chris: Starting migration planning, need to pick cutover date

## Context
Discussion about scheduling the v2 API endpoint cutover. Key decision: May 15
cutover date agreed, contingent on QA approval. Next step: Sarah to confirm
with QA team by April 15.
```

---

## Configuration

Add to `config.yaml`:

```yaml
email_scanner:
  interval_seconds: 300          # scan every 5 minutes
  initial_lookback_days: 30      # first scan: how far back (max 90)
  archive_after_days: 90         # stop updating threads inactive this long
  skip_mailboxes:                # mailbox names to exclude (case-insensitive)
    - Trash
    - Junk
    - Spam
    - Archive
    - Deleted Messages
  full_rescan: false             # set true to force full rescan next cycle
```

---

## Implementation Notes

### Module: `email_scanner.py`

```
class MailDataSource:
    detect(cls) -> MailDataSource | None
    get_threads_since(since, excluded) -> list[dict]
    get_threads_updated_since(since, high_water_rowid, excluded) -> list[dict]

class EnvelopeIndexSource(MailDataSource):
    __init__(self, db_path)
    _find_db_path(cls) -> Path | None
    _copy_db(self) -> Path
    _convert_timestamp(self, ts) -> datetime
    _run_query(self, conn, *args) -> list

class AppleScriptSource(MailDataSource):
    _run_osascript(self, script) -> str
    _normalize_subject(self, subject) -> str
    _subject_to_id(self, normalized) -> int

class EmailScanner:
    __init__(self, role)
    async run_loop(self, stop_event)
    async _run_scan(self)
    _scanner_config(self) -> dict
    _load_state(self) -> dict
    _save_state(self, state)
    _get_data_source(self) -> MailDataSource | None
    _needs_update(self, thread, memory_path) -> bool
    async _generate_summary_and_tags(self, thread) -> tuple[str, list]
    _get_existing_summary_and_tags(self, memory_path, thread) -> tuple
    _slugify(self, subject) -> str
    _write_memory(self, thread, summary, tags)
```

### Daemon integration

Import inside the `if role == "full"` block in `daemon.py`. EmailScanner does
not run on watcher nodes (requires ANTHROPIC_API_KEY for summarization).

### FDA requirement

The Envelope Index requires Full Disk Access. Grant it in:
System Settings → Privacy & Security → Full Disk Access → add Terminal (or
the Python executable). The scanner logs a clear message on PermissionError
and falls back to AppleScript automatically.

---

## Files Modified

| File | Change |
|------|--------|
| `email_scanner.py` | **Create** |
| `daemon.py` | Add EmailScanner to full-role gather |
| `config.yaml.template` | Add `email_scanner` section |
| `install.sh` | Add `email_scanner.py` to DAEMON_FILES |
| `README.md` | Document sixth async loop, FDA requirement |
| `CLAUDE.md` | Update loop count and add email_scanner description |
| `tests/unit/test_email_scanner.py` | **Create** |
| `tests/integration/test_pipeline.py` | Add scanner integration test |

---

## Testing

### Unit tests

| Test | Assertion |
|------|-----------|
| `test_find_envelope_index_path` | Returns highest V-number path |
| `test_find_envelope_index_missing` | Returns None when no V-dir |
| `test_convert_core_data_timestamp` | Adds 978307200, correct datetime |
| `test_threads_grouped_by_conversation_id` | Multiple messages → one thread |
| `test_excluded_mailboxes_filtered` | Trash/Junk messages excluded |
| `test_incremental_uses_high_water_rowid` | Only new messages returned |
| `test_needs_update_true_new_messages` | message_count change triggers update |
| `test_needs_update_false_unchanged` | Same counts = no update |
| `test_needs_update_true_no_memory` | Missing file = always update |
| `test_slugify_special_chars` | Clean slug from special chars |
| `test_slugify_truncation` | Long subjects capped at 40 chars |
| `test_write_memory_field_order` | source_title line 2, summary line 3, tags line 4 |
| `test_write_memory_atomic` | No .tmp file after write |
| `test_write_memory_type_email_thread` | type = email_thread |
| `test_applescript_groups_by_subject` | Normalized subject grouping |
| `test_applescript_strips_re_fw` | "RE: FW: RE: Topic" normalizes to "Topic" |
| `test_fda_check_logs_warning` | PermissionError → logs FDA message, returns None |
| `test_state_file_persists_high_water` | State survives across scan cycles |
| `test_full_rescan_resets_high_water` | full_rescan flag → lookback instead of incremental |
| `test_archive_skips_stale_threads` | Threads older than threshold not updated |

### Integration test

1. Create a minimal SQLite DB in `tmp_path` with Envelope Index schema and 3 messages in 2 threads
2. Instantiate `EmailScanner` pointed at the tmp DB
3. Run one scan cycle
4. Assert two `email-thread-*.md` files exist with correct frontmatter
5. Run scan again with same data — assert no file writes (mtime unchanged)

---

## Changelog

### v1.2.0 — 2026-04-13

**Added:** Email content classification (FR-11). Every thread is now classified
into one of five buckets (`human`, `transactional`, `marketing`, `automated`,
`unknown`) during the same LLM call that generates summary + tags. Downstream
consumers (`contact_tracker`, `commitment_tracker`) skip `marketing` and
`automated` by default; `chat_handler`'s `/comms email` hides those two unless
`/comms email all` is used. Kill-switch: `email_scanner.classification_enabled`
config flag (defaults `true`).

### v1.1.0 — 2026-04-11

**Note:** Email threads are surfaced via the unified `/comms` command defined in
`feat-memory-management.md` (v1.1.0). There is no dedicated `/emails` command —
`/comms email` filters to email threads only. The `email_thread` memory type and
frontmatter format are unchanged.
