---
specmas: 3.0
kind: feature
id: feat-zoom-transcript-scanner
version: 1.0.0
created: 2026-04-11
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-commitment-tracker
---

# Zoom Transcript Scanner

## Overview

### Problem Statement

Meetings are where decisions are made, commitments are spoken, and action items are
assigned — yet this information evaporates once the meeting ends. Zoom cloud recordings
include VTT transcripts with full speaker attribution, but they sit unused. Without
ingesting these transcripts, the bot cannot answer questions like "what did we decide in
the Q4 planning call?", "what did Sarah commit to in Tuesday's meeting?", or "who was on
the API migration call last week?".

The Zoom Transcript Scanner polls the Zoom Cloud Recordings API, downloads VTT
transcripts, parses speaker-attributed segments, and writes one memory file per meeting
— making meeting context searchable through the Telegram bot.

### Scope

**In Scope:**
- Seventh async daemon loop, running every 5 minutes (`full` role only)
- Zoom Server-to-Server OAuth (M2M) authentication
- Poll `GET /v2/users/me/recordings` for completed transcripts
- VTT transcript download and parsing with speaker attribution
- Participant extraction and speaker-to-email matching
- LLM-generated summary and tags per meeting
- One `meeting-{date}-{slug}-{meeting-id}.md` memory file per meeting
- Deduplication via state file (`zoom-scanner-state.json`)
- Rate limit compliance with exponential backoff

**Out of Scope:**
- Video/audio download (transcripts only)
- Webhook endpoint (polling only — no inbound server)
- Calendar event correlation (no calendar connector in secondbrain v1)
- Multi-account Zoom support
- Live meeting detection or real-time transcription
- Zoom Chat messages
- Meeting scheduling or creation (read-only)

### Success Metrics

- Transcript retrieval success rate > 95% for recorded meetings
- Processing latency < 15 minutes after recording becomes available
- Speaker attribution accuracy > 85%
- Zero duplicate memory files for the same meeting

---

## Functional Requirements

### FR-1: Zoom Server-to-Server OAuth Authentication

Acquire and cache a bearer token using Zoom's Server-to-Server OAuth flow (M2M).

**Credentials (from env vars via launchd plist):**
- `ZOOM_ACCOUNT_ID`
- `ZOOM_CLIENT_ID`
- `ZOOM_CLIENT_SECRET`

**Token acquisition:**
```
POST https://zoom.us/oauth/token
Authorization: Basic base64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded
Body: grant_type=account_credentials&account_id={account_id}
```

Response: `{ "access_token": "...", "expires_in": 3600 }`

**Token caching:**
- Cache token in memory with expiry timestamp
- Refresh when fewer than 5 minutes remain
- On startup, acquire token immediately; abort scanner loop with WARNING if credentials missing

**Required Zoom app scopes:**
- `recording:read:admin` — download cloud recordings and transcripts
- `meeting:read:admin` — read meeting metadata
- `user:read:admin` — read participant email addresses

**Validation criteria:**
- Token acquired on scanner startup
- Token refreshed automatically before expiry
- Missing credentials logged clearly (WARNING) and loop exits gracefully
- Token not written to disk (memory only)

---

### FR-2: Poll Recordings API for Completed Transcripts

Poll `GET /v2/users/me/recordings` periodically to discover new meetings with available transcripts.

**API endpoint:**
```
GET https://api.zoom.us/v2/users/me/recordings
    ?from={YYYY-MM-DD}
    &page_size=100
Authorization: Bearer {token}
```

**Polling strategy:**
- `from` date = `(now - initial_lookback_days)` on first run; `(now - interval_seconds * 2)` on subsequent runs
- Paginate via `next_page_token` until exhausted
- For each `RecordingFile` with `file_type=TRANSCRIPT` and `status=completed`:
  - Skip if `meeting_uuid` already in `zoom-scanner-state.json`
  - Otherwise queue for processing
- Cap at 20 new meetings per scan cycle to bound LLM cost

**State file (`DEPLOY_DIR/zoom-scanner-state.json`):**
```json
{
  "processed_uuids": ["abc123", "def456"],
  "last_poll": "2026-04-11T15:00:00"
}
```

**Validation criteria:**
- New meetings processed within one poll cycle of transcript becoming available
- Already-processed meetings skipped without API calls
- Pagination handled correctly for users with many recordings
- 404 (no recordings) handled without error

---

### FR-3: VTT Download and Parsing with Speaker Attribution

Download the VTT transcript file and parse it into structured segments.

**Download:**
```
GET {download_url}?access_token={token}
```
Zoom requires the token as a query parameter for recording downloads.

**VTT format:**
```
WEBVTT

1
00:00:05.000 --> 00:00:10.500
Sarah Chen: Good morning everyone, let's get started.

2
00:00:11.000 --> 00:00:18.750
Mike Peters: Thanks Sarah. I wanted to address the budget concerns.
```

**Parsing rules:**
- Skip the `WEBVTT` header line
- Numeric-only lines are segment indices (ignore)
- Lines matching `HH:MM:SS.mmm --> HH:MM:SS.mmm` are timestamp boundaries
- Content lines with `Speaker Name: text` → extract speaker and text
- Content lines without a colon → continuation of previous segment's text
- Empty lines → end of segment
- Segments with no text discarded

**Output per segment:**
```python
{
    "index": int,
    "start_time": "HH:MM:SS",   # truncated from HH:MM:SS.mmm
    "speaker": str | None,
    "text": str,
}
```

**Full parse output:**
```python
{
    "segments": [...],
    "speakers": ["Sarah Chen", "Mike Peters"],   # unique, in order of first appearance
    "raw_text": "full concatenated text",        # for LLM summarization
    "duration_ms": int,                          # end time of last segment in ms
}
```

**Validation criteria:**
- VTT parsed correctly for standard Zoom output format
- Speaker names extracted from `Name: text` pattern
- Multi-line segments (continuation lines) concatenated correctly
- Malformed or empty transcripts return empty segment list, not an exception

---

### FR-4: Participant Extraction and Speaker Matching

Retrieve participant list from Zoom API and match transcript speaker names to email addresses.

**Participant API:**
```
GET https://api.zoom.us/v2/past_meetings/{meeting_uuid}/participants
Authorization: Bearer {token}
```

Response fields used: `name`, `user_email` (may be empty for external participants).

**Matching strategy:**
1. Exact name match (case-insensitive) → confidence 1.0
2. First-name match → confidence 0.7
3. No match → speaker listed with no email

**Output:**
```python
[
    {"name": "Sarah Chen", "email": "sarah.chen@acme.com", "confidence": 1.0},
    {"name": "Mike Peters", "email": "mike.peters@acme.com", "confidence": 0.7},
    {"name": "External Guest", "email": None, "confidence": 0.0},
]
```

**Validation criteria:**
- 404 from participants endpoint (data expired after 30 days) handled gracefully → empty list
- Participants with no email still appear in the `participants` list
- Speaker names not in the participant list included as-is (no email)

---

### FR-5: LLM Summary Generation

Generate a natural-language summary and tags for each meeting using the `summarize` LiteLLM route.

**Prompt input (snippets only, no full transcript):**
- Meeting topic
- Date and duration
- Speaker list with emails where available
- First 100 words of raw transcript text (capped to avoid token cost on long meetings)
- Up to 20 representative transcript lines (one per speaker turn, evenly sampled)

**Prompt structure:**
```
Summarize this Zoom meeting for a personal knowledge base.

Meeting: {topic}
Date: {date}
Duration: {duration_minutes} minutes
Speakers: {speaker list}

Transcript excerpt:
{excerpt}

Return JSON:
{
  "summary": "2-3 sentence summary of key topics and outcomes",
  "tags": ["tag1", "tag2"],
  "key_decisions": ["decision 1", "decision 2"]
}
```

**Validation criteria:**
- Summary ≤ 200 words
- Tags are lowercase, hyphenated slugs
- LLM failure logged (WARNING) and scan continues with a fallback summary of the meeting topic
- JSON parse error falls back to raw LLM text as summary

---

### FR-6: Memory File Write

Write one `meeting-{date}-{slug}-{zoom-id}.md` per processed meeting.

**Filename convention:**
```
meeting-{YYYY-MM-DD}-{title-slug}-{6-char-meeting-id-hash}.md
```
where `title-slug` is the meeting topic lowercased, spaces → hyphens, max 40 chars.

**File format:**
```markdown
---
source_title: "Q4 Planning Review"
summary: Discussed Q4 budget timeline. Sarah committed to revised numbers by Friday. Mike to follow up on vendor contracts.
tags: [q4-planning, budget, review]
last_scanned: '2026-04-11T15:23:00'
source_url: zoom:meeting-uuid-abc123def456
type: meeting_transcript
participants: [sarah.chen@acme.com, mike.peters@acme.com]
speakers: [Sarah Chen, Mike Peters]
duration_minutes: 45
meeting_date: '2026-04-11T10:00:00'
zoom_meeting_id: '12345678'
---

## Transcript
- 00:00:05 Sarah Chen: Good morning everyone, let's get started with the Q4 review.
- 00:00:11 Mike Peters: Thanks Sarah. I wanted to first address the budget concerns.
- 00:00:19 Sarah Chen: Yes, can you commit to having the revised numbers by Friday?
...

## Summary
{LLM-generated summary}

## Key Decisions
- {decision 1}
- {decision 2}
```

**Write rules:**
- Atomic write via temp file + `os.rename()` (same pattern as `memory_writer.py`)
- `yaml.dump(..., sort_keys=False)` to preserve field order
- Transcript section: at most 50 lines (one per segment); truncate with `(... {N} more lines)` if longer
- Always overwrite if file exists (re-scan after config change)

**Validation criteria:**
- File created in `MEMORIES_DIR` (iCloud Drive)
- `type: meeting_transcript` in frontmatter
- `source_url` uses `zoom:` scheme with meeting UUID
- No temp files left after write

---

### FR-7: Deduplication via State File

Prevent reprocessing already-written meetings across daemon restarts.

**State file:** `DEPLOY_DIR/zoom-scanner-state.json`

**Operations:**
- Load on scanner startup (create empty `{"processed_uuids": [], "last_poll": null}` if missing)
- After successful memory file write, append `meeting_uuid` to `processed_uuids`
- Save state after each individual meeting (not batch) to survive mid-cycle crashes
- `processed_uuids` list capped at 10,000 entries (trim oldest on overflow)

**Validation criteria:**
- Restarting the daemon does not duplicate memory files
- Partial-cycle crash (after N of M meetings) resumes from the unprocessed meetings

---

### FR-8: Rate Limit Handling

Respect Zoom API rate limits and back off on 429 responses.

**Rate limit response:**
```
HTTP 429 Too Many Requests
Retry-After: 30
```

**Handling:**
- On 429: log WARNING, sleep `Retry-After` seconds (default 60 if header absent), retry once
- Limit API calls per cycle: max 1 recordings list call + 20 participant calls + 20 transcript downloads
- On persistent 429 (3+ in one cycle), skip remaining meetings and log WARNING

**Validation criteria:**
- 429 does not crash the scanner loop
- Retry-After header respected
- Meetings skipped due to rate limits retried on next poll cycle (not in state file)

---

### FR-9: Watcher Role Exclusion

Zoom scanner runs only on the `full` daemon role.

**Validation criteria:**
- `watcher` role daemon does not import or instantiate `ZoomScanner`
- `zoom_scanner.py` not deployed on watcher nodes (installer skips if role=watcher)
- Missing `ZOOM_*` env vars on a `full` node: log WARNING once at startup, skip loop

---

## Config

```yaml
zoom_scanner:
  interval_seconds: 300
  initial_lookback_days: 30
```

`ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` are set as environment variables in the launchd plist — not in `config.yaml` (which syncs via iCloud).

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `specs/feat-zoom-transcript-scanner.md` | **This spec** |
| `zoom_scanner.py` | **Create** — ZoomScanner class + VTT parser + OAuth client |
| `daemon.py` | Add ZoomScanner to full-role gather |
| `config.yaml.template` | Add `zoom_scanner` section |
| `install.sh` | Add `zoom_scanner.py` to DAEMON_FILES; add ZOOM_* env var prompts to plist section |
| `CLAUDE.md` | Update to seven async loops, add ZoomScanner description |
| `README.md` | Document Zoom scanner setup, required scopes, app registration steps |
| `tests/unit/test_zoom_scanner.py` | **Create** |
| `tests/integration/test_pipeline.py` | Add zoom scanner integration test |

---

## Unit Tests (`tests/unit/test_zoom_scanner.py`)

| Test | Assertion |
|------|-----------|
| `test_parse_vtt_basic` | Segments extracted with speaker and text |
| `test_parse_vtt_speaker_attribution` | `Speaker: text` pattern extracted correctly |
| `test_parse_vtt_continuation_lines` | Multi-line segment text concatenated |
| `test_parse_vtt_no_speakers` | Segments without `Name:` prefix have `speaker=None` |
| `test_parse_vtt_empty` | Empty VTT returns empty segment list, no exception |
| `test_parse_timestamp_ms` | `01:23:45.678` → correct milliseconds |
| `test_match_speakers_exact` | Exact name match → confidence 1.0, correct email |
| `test_match_speakers_first_name` | First-name-only match → confidence 0.7 |
| `test_match_speakers_no_match` | Unknown speaker → email None, confidence 0.0 |
| `test_token_cache_hit` | Cached token returned without API call |
| `test_token_cache_refresh` | Expired token triggers new acquisition |
| `test_missing_credentials_logs_warning` | No ZOOM_CLIENT_ID → WARNING logged, returns None |
| `test_dedup_skips_processed_uuid` | UUID in state file → recording skipped |
| `test_state_file_persists_uuid` | After write, UUID in state file |
| `test_state_file_created_if_missing` | Scanner starts cleanly when state file absent |
| `test_rate_limit_respects_retry_after` | 429 with Retry-After → sleep called with correct value |
| `test_write_memory_field_order` | `source_title` first, `type: meeting_transcript` present |
| `test_write_memory_atomic` | No temp file left after write |
| `test_transcript_truncated_at_50_lines` | >50 segments → truncation marker in file |
| `test_watcher_role_skips_zoom_scanner` | role=watcher → ZoomScanner not instantiated |

---

## Zoom App Setup (for README)

1. Go to [Zoom Marketplace](https://marketplace.zoom.us/) → Develop → Build App → Server-to-Server OAuth
2. Add scopes: `recording:read:admin`, `meeting:read:admin`, `user:read:admin`
3. Copy Account ID, Client ID, Client Secret
4. Add to launchd plist `EnvironmentVariables`: `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`
5. Enable cloud recording with transcription in Zoom account settings (Account Management → Recording)
6. Run `./install.sh` — it will detect the new env vars and add them to the plist
