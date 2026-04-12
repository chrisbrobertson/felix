---
specmas: 3.0
kind: feature
id: feat-slack-scanner
version: 1.0.0
created: 2026-04-11
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-commitment-tracker
  - feat-contact-tracker
---

# Slack Scanner

## Overview

### Problem Statement

Many team decisions, ad-hoc commitments, and project discussions happen in Slack and
never surface in meetings or email. Without Slack awareness, secondbrain misses a major
communication channel. The user cannot ask "what did we decide in #engineering today?",
"what's the latest on the migration thread?", or "did Mike respond to my question in
Slack yet?" Commitments made in Slack ("I'll send you the PR link by EOD") are
completely invisible to the Commitment Tracker.

The Slack Scanner polls the Slack Web API every 5 minutes, writes one memory file per
active thread in monitored channels, and feeds those files into the Commitment Tracker
and Contact Tracker.

### Scope

**In Scope:**
- Eleventh async daemon loop, running every 5 minutes (`full` role only)
- Polling via Slack Web API bot token (no webhook server required)
- conversations.list to discover channels; conversations.history for messages;
  conversations.replies for thread context
- High-water timestamp per channel for incremental polling
- One `slack-thread-{channel-slug}-{thread-ts}.md` per active thread
- Change detection: skip threads with no new messages since last scan
- LLM-generated summary and tags per thread
- Channel whitelist/blacklist filtering
- User ID → display name resolution (cached per scan cycle)
- `type: slack_thread` in frontmatter — consumed by Commitment Tracker and Contact Tracker
- Graceful exit when `SLACK_BOT_TOKEN` env var is absent

**Out of Scope:**
- Slack Events API / webhook-based real-time capture (polling only)
- Direct messages or multi-party DMs (public and private channels only)
- File and attachment indexing
- Sending Slack messages or posting reactions
- Slack Connect / external workspace channels
- Message edit/delete tracking
- Workspace-level analytics or channel statistics

### Success Metrics

- New threads in monitored channels produce memory files within one scan cycle
- Threads with no new messages produce zero file writes
- Rate limits respected in steady state (no 429 responses without auto-recovery)
- Memory files scannable by header cache (type and title in first 500 chars)

---

## Functional Requirements

### FR-1: Bot Token Authentication and Graceful Startup

Authenticate to the Slack Web API using a bot token. Exit gracefully if credentials
are absent, following the same pattern as `zoom_scanner.py`.

**Required env vars** (set in launchd plist, not config.yaml):
- `SLACK_BOT_TOKEN` — Slack bot OAuth token (starts with `xoxb-`)
- `SLACK_USER_ID` — The user's Slack member ID (e.g., `U01234567`), used to classify
  commitments as inbound vs outbound in the Commitment Tracker

**Required bot token scopes:**
- `channels:read` — list public channels
- `groups:read` — list private channels the bot is in
- `channels:history` — read public channel messages
- `groups:history` — read private channel messages
- `conversations.replies` — read thread replies
- `users:read` — resolve user IDs to display names

**Startup behavior:**
```python
async def run_loop(self, stop_event: asyncio.Event):
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        log.warning("SLACK_BOT_TOKEN not set — Slack scanner disabled")
        return
    ...
```

**Validation criteria:**
- Missing token → WARNING logged, loop exits cleanly (no exception propagated)
- Invalid token → 401 error logged at ERROR, loop exits cleanly
- Valid token → scan cycle proceeds

---

### FR-2: Channel Discovery and Filtering

Discover channels the bot has access to and apply whitelist/blacklist filtering.

**API call:** `conversations.list` with `types=public_channel,private_channel`

**Filtering rules (evaluated in order):**
1. If `channel_include` is non-empty, keep only channels whose names are in the list
2. Otherwise, start with all channels and remove those in `channel_exclude`
3. Always skip archived channels

**Config:**
```yaml
slack_scanner:
  channel_include: []              # whitelist — if non-empty, only these
  channel_exclude:                 # blacklist — applied when include is empty
    - general
    - random
```

**Channel caching:**
- Channel list fetched once per scan cycle (not per message)
- `conversations.list` is paginated; follow `next_cursor` until exhausted
- Result cached for the duration of the cycle

**Validation criteria:**
- Non-empty `channel_include` → only those channels scanned, exclude list ignored
- Empty `channel_include` → all channels minus `channel_exclude` scanned
- Archived channels always skipped
- Unknown channel name in include list → logged at WARNING, not crashed

---

### FR-3: Message Polling with High-Water Timestamp

Fetch only messages newer than the last-seen timestamp for each channel, to avoid
reprocessing the full history on every cycle.

**API call:** `conversations.history` with parameters:
- `channel`: channel ID
- `oldest`: high-water timestamp for this channel (Unix timestamp float as string)
- `limit`: 100 messages per page
- `inclusive`: false (exclude the high-water message itself)

**High-water management:**
- State stores `{"channels": {"C01234567": {"high_water": "1712860000.000100"}}}` per channel
- After a successful cycle for a channel, update `high_water` to the `ts` of the
  newest message seen
- On first run for a channel: set `oldest` = now − `lookback_days` × 86400

**Lookback on first run:**
```yaml
slack_scanner:
  lookback_days: 7
```

**Validation criteria:**
- Second run returns only messages after the high-water timestamp
- First run for a channel fetches up to `lookback_days` of history
- High-water not advanced if the API returns an error for that channel

---

### FR-4: Thread Context Retrieval

Fetch the full reply chain for threaded messages to build coherent memory files.

**Thread detection:** A message is the root of a thread if `reply_count > 0` and
`thread_ts == ts` (it is its own thread parent).

**API call:** `conversations.replies` with:
- `channel`: channel ID
- `ts`: the parent message's `ts`
- `limit`: 100 replies per page

**Grouping strategy:**
- Non-threaded messages (standalone messages in the channel) are skipped in v1
  (they lack the conversational context that makes summaries useful)
- Only messages with `reply_count >= min_thread_messages` config value are scanned

**Thread identity:** thread is identified by `(channel_id, thread_ts)`.
Filename: `slack-thread-{channel-slug}-{thread-ts-numeric}.md`
where `thread-ts-numeric` is the thread `ts` with `.` replaced by `-`.

**Validation criteria:**
- Threads with fewer replies than `min_thread_messages` skipped
- Standalone messages (not part of a thread) skipped
- All replies in a thread included in the memory file body

---

### FR-5: Change Detection

Skip writing a memory file if the thread has not changed since last scan.

**Mechanism:**
- State stores per-thread: `{"message_count": 7, "last_ts": "1712860000.000100"}`
- If `message_count` and `last_ts` both match → skip write (no LLM call)
- If either has changed → re-fetch replies, regenerate summary, overwrite file

**State structure:**
```json
{
  "channels": {
    "C01234567": {
      "high_water": "1712860000.000100",
      "threads": {
        "1712700000.000200": {
          "message_count": 7,
          "last_ts": "1712860000.000100"
        }
      }
    }
  }
}
```

**Validation criteria:**
- Unchanged thread → no API call to conversations.replies, no LLM call, no file write
- New reply in thread → re-fetch, re-summarize, overwrite memory file
- Thread state updated after each write

---

### FR-6: LLM Summary and Tags Generation

Generate a concise summary and tags for each new or changed thread.

**LLM route:** `summarize` (Gemini Flash via LiteLLM)

**Prompt input:**
- Channel name
- Thread participants (resolved display names)
- Messages in chronological order (capped at 3000 chars total)

**Prompt structure:**
```
Summarize this Slack thread in 1-2 sentences. Then provide 3-5 tags.

Channel: #{channel_name}
Participants: {comma-separated display names}

Messages:
{chronological message list: "[HH:MM] Name: text" per line}

Return JSON only:
{
  "summary": "...",
  "tags": ["tag1", "tag2"]
}
```

**Validation criteria:**
- Returns valid JSON; parse failure logged at WARNING, file written with summary = first message text
- Tags are lowercase kebab-case strings
- Summary fits within 280 characters

---

### FR-7: Memory File Write

Write one `slack-thread-{channel-slug}-{thread-ts}.md` per thread.

**Filename:**
```
slack-thread-{channel-name-slug}-{thread-ts-numeric}.md
```
where `{channel-name-slug}` is the channel name lowercased with special chars removed,
and `{thread-ts-numeric}` is the thread `ts` with `.` replaced by `-`
(e.g., `1712700000-000200`).

**File format:**
```markdown
---
source_title: "Thread: should we migrate to Postgres?"
summary: Team debated migrating from SQLite to Postgres, deciding to evaluate pg-boss for job queuing first.
tags: [architecture, database, postgres, decision]
last_scanned: '2026-04-11T14:30:00'
source_url: slack:C01234567/1712700000.000200
type: slack_thread
channel: engineering
channel_id: C01234567
thread_ts: '1712700000.000200'
participants:
  - name: Chris Robertson
    slack_id: U01234567
  - name: Sarah Chen
    slack_id: U09876543
message_count: 7
last_message: '2026-04-11T10:15:00'
---

## Messages

[09:00] Chris Robertson: should we migrate to postgres? sqlite is starting to show limits
[09:15] Sarah Chen: what's the use case? for job queuing specifically?
[09:20] Chris Robertson: yes, pg-boss looks promising for the async queue
[10:15] Sarah Chen: let's evaluate pg-boss first before committing to a full migration

## Context

{LLM summary}
```

**Frontmatter field order** (`sort_keys=False`):
`source_title`, `summary`, `tags`, `last_scanned`, `source_url`, `type`,
`channel`, `channel_id`, `thread_ts`, `participants`, `message_count`, `last_message`

**Write rules:**
- Atomic write via temp file + `os.rename()`
- `source_url` uses `slack:` scheme with `{channel_id}/{thread_ts}`
- `source_title` is "Thread: {first 60 chars of root message}"
- Messages capped at 50 lines in the file body (oldest dropped first)

**Validation criteria:**
- `type: slack_thread` in all written files
- `source_url` uses `slack:` scheme
- File written atomically
- `source_title` derived from root message, not LLM-generated

---

### FR-8: User Identity Resolution

Resolve Slack user IDs (e.g., `U01234567`) to human-readable display names.

**API call:** `users.info` with `user=U01234567`

**Caching:**
- In-memory cache per scan cycle: `{user_id: display_name}`
- Populated lazily on first encounter of each user ID
- Cache not persisted across cycles (user renames are picked up each cycle)

**Fallback:** If `users.info` fails or user is not found, display as "Unknown User"

**`SLACK_USER_ID` usage:**
- When resolving messages, the message whose `user` matches `SLACK_USER_ID` is tagged
  as "me" in the participants list
- This allows the Commitment Tracker to classify commitments correctly:
  messages from `SLACK_USER_ID` are potential outbound commitments; messages to the
  user are potential inbound commitments

**Validation criteria:**
- Second API call for same user ID uses cache, not API
- Unknown user ID falls back to "Unknown User" without crashing
- User matching `SLACK_USER_ID` identified in participants list

---

### FR-9: Rate Limit Compliance

Respect Slack's API rate limits to avoid 429 responses in steady state.

**Slack rate limits (relevant tiers):**
- Tier 2 (conversations.list, users.info): ~20 requests/minute
- Tier 3 (conversations.history, conversations.replies): ~50 requests/minute

**Implementation:**
- Insert a 1-second delay between all API calls (`asyncio.sleep(1)`)
- On 429 response: read `Retry-After` header (seconds), sleep that duration, retry once
- On second consecutive 429: log at ERROR, skip remaining channels for this cycle

**Per-cycle cap:**
- Process at most 20 channels per cycle (controls max API calls)
- Within a channel, process at most 30 threads per cycle
- Remaining channels/threads picked up in the next cycle

**Validation criteria:**
- 429 + Retry-After=5 → sleep 5 seconds, retry
- Two consecutive 429s → skip channel, log ERROR
- 1-second inter-request delay observed between API calls

---

### FR-10: State File Management

Persist channel high-water timestamps and thread change-detection state across restarts.

**State file:** `DEPLOY_DIR/slack-scanner-state.json`

Full schema:
```json
{
  "channels": {
    "C01234567": {
      "name": "engineering",
      "high_water": "1712860000.000100",
      "threads": {
        "1712700000.000200": {
          "message_count": 7,
          "last_ts": "1712860000.000100"
        }
      }
    }
  }
}
```

**Rules:**
- State loaded on scanner init; empty state created if absent
- State saved atomically after each channel is fully processed (not per-thread)
- `threads` map per channel capped at 1000 entries; prune entries whose `last_ts`
  is older than `lookback_days` × 86400
- State file write is atomic (temp file + os.rename)

**Validation criteria:**
- State file created on first run
- High-water advances after successful channel poll
- Thread entries pruned when older than lookback window
- Daemon restart resumes from stored high-water (no full history re-fetch)

---

## Config

```yaml
slack_scanner:
  interval_seconds: 300          # scan every 5 minutes
  lookback_days: 7               # initial history window on first run per channel
  channel_include: []            # whitelist (empty = all channels bot can see)
  channel_exclude:               # blacklist (applied when include is empty)
    - general
    - random
  min_thread_messages: 2         # skip threads with fewer replies than this
  max_channels_per_cycle: 20
  max_threads_per_channel: 30
```

**Env vars** (set in launchd plist, not config.yaml):

| Var | Required | Description |
|-----|----------|-------------|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token (`xoxb-...`) |
| `SLACK_USER_ID` | Yes | User's Slack member ID (`U01234567`) |

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `specs/feat-slack-scanner.md` | **This spec** |
| `slack_scanner.py` | **Create** — SlackScanner class |
| `daemon.py` | Add SlackScanner to full-role gather (loop 11) |
| `config.yaml.template` | Add `slack_scanner` section |
| `install.sh` | Add `slack_scanner.py` to DAEMON_FILES; add `SLACK_BOT_TOKEN` and `SLACK_USER_ID` prompts to plist env var setup |
| `CLAUDE.md` | Update loop count and descriptions |
| `README.md` | Document Slack scanner setup, required bot scopes, how to create a Slack app |
| `tests/unit/test_slack_scanner.py` | **Create** |
| `tests/integration/test_pipeline.py` | Add Slack scanner integration test |

---

## Unit Tests (`tests/unit/test_slack_scanner.py`)

| Test | Assertion |
|------|-----------|
| `test_missing_token_logs_warning_and_exits` | No `SLACK_BOT_TOKEN` → WARNING logged, loop exits cleanly |
| `test_invalid_token_logs_error_and_exits` | 401 response → ERROR logged, loop exits cleanly |
| `test_channel_include_whitelist` | `channel_include: [engineering]` → only that channel scanned |
| `test_channel_include_overrides_exclude` | Non-empty include → exclude list ignored |
| `test_channel_exclude_blacklist` | `channel_exclude: [general]` → general channel skipped |
| `test_archived_channel_always_excluded` | Archived channels skipped regardless of include list |
| `test_high_water_incremental_polling` | Second cycle only requests messages after stored high_water |
| `test_first_run_uses_lookback_days` | No state → `oldest` set to now − lookback_days |
| `test_thread_detection` | `reply_count > 0` and `thread_ts == ts` → thread root |
| `test_min_thread_messages_filter` | Single-reply thread below min_thread_messages → skipped |
| `test_standalone_messages_skipped` | Non-threaded messages → not written as memory files |
| `test_change_detection_unchanged` | Same message_count + last_ts → no write, no LLM call |
| `test_change_detection_new_reply` | New reply → re-fetch, re-summarize, overwrite |
| `test_user_id_resolution_cached` | Second call for same user ID → cache used, no API call |
| `test_user_id_resolution_unknown` | Unknown user ID → "Unknown User", no crash |
| `test_slack_user_id_identified` | Message from SLACK_USER_ID tagged as "me" in participants |
| `test_write_memory_atomic` | No .tmp file left after write |
| `test_write_memory_type` | `type: slack_thread` in frontmatter |
| `test_write_memory_field_order` | `source_title` first in frontmatter |
| `test_source_url_scheme` | `source_url` starts with `slack:` |
| `test_source_title_from_root_message` | Title derived from root message text, max 60 chars |
| `test_messages_capped_at_50_lines` | Thread with 60 messages → 50 in file body |
| `test_rate_limit_retry_after` | 429 + Retry-After=5 → sleep 5s, retry |
| `test_rate_limit_two_consecutive_skips` | Second 429 → channel skipped, ERROR logged |
| `test_inter_request_delay` | 1-second sleep observed between consecutive API calls |
| `test_state_file_created_on_first_run` | No existing state → state file written |
| `test_state_file_persists_high_water` | High-water survives simulated restart |
| `test_thread_state_pruned_by_age` | Thread entries older than lookback_days pruned |
| `test_max_channels_per_cycle` | 25 channels → 20 processed, 5 deferred |
| `test_max_threads_per_channel` | 35 threads in channel → 30 processed |
| `test_watcher_role_skips_slack_scanner` | `role=watcher` → SlackScanner not instantiated |
