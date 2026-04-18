---
specmas: 3.0
kind: feature
id: feat-zoom-transcript-scanner
version: 1.2.0
created: 2026-04-11
status: implemented
shipped_version: "1.3.0"
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-commitment-tracker
  - feat-zoom-ai-companion
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
- Speech-to-text from local audio files (local recordings without `closed_caption.vtt` are skipped)
- AI Companion meeting summaries (see `feat-zoom-ai-companion`)

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

Zoom scanner runs only on the `full` daemon role for **cloud recordings**.
Local recording scanning (FR-11 through FR-19) runs on all roles including `watcher`,
since local recordings exist on the machine that made the recording.

**Validation criteria:**
- `watcher` role daemon does not import or instantiate `ZoomScanner` for cloud path
- `zoom_scanner.py` not deployed on watcher nodes unless `local_recordings_enabled: true`
- Missing `ZOOM_*` env vars on a `full` node: log WARNING once at startup, skip loop

---

### FR-11: Local Recording Directory Scan

Scan a configurable local directory for Zoom meeting folders and process any VTT
transcripts found within them.

**Default directory:** `~/Documents/Zoom/` (Zoom's standard macOS recording output path).

**Folder discovery:**
- Walk the directory looking for subdirectories whose names match the pattern:
  `YYYY-MM-DD HH.MM.SS <Meeting Topic>`
  Example: `2026-04-15 14.30.22 Weekly Standup`
- Ignore folders that do not match this pattern (version directories, tmp, etc.)
- Runs on both `full` and `watcher` roles

**Validation criteria:**
- Folders matching the date-prefix pattern are discovered
- Non-matching subdirectories silently ignored
- Directory missing or inaccessible: log WARNING once, skip local scan

---

### FR-12: VTT Detection and Skipping

For each local recording folder, check for a `closed_caption.vtt` file. Skip folders
that do not have one.

**Background:** Local Zoom recordings include a `closed_caption.vtt` **only** when
"Save Closed Caption as a VTT file" is enabled in Zoom account/user settings AND live
captions were active during the meeting. Cloud recordings always include a transcript.

**Validation criteria:**
- Folder with `closed_caption.vtt` → queued for processing
- Folder without `closed_caption.vtt` → skipped, logged at DEBUG (not WARNING)
- Future phase: optional Whisper-based audio transcription for meetings without VTT
  (out of scope for v1 — adds a heavy dependency)

---

### FR-13: Metadata Extraction from Folder Name

Parse the Zoom folder name to extract meeting date and topic without an API call.

**Folder name format:** `YYYY-MM-DD HH.MM.SS <Meeting Topic>`

**Extraction:**
```python
# Example: "2026-04-15 14.30.22 Weekly Standup"
date_str = "2026-04-15"
time_str = "14.30.22"   # dots as separators (not colons, which are illegal in macOS filenames)
topic    = "Weekly Standup"
meeting_date = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H.%M.%S").isoformat()
```

**Validation criteria:**
- `meeting_date` extracted as ISO 8601 string
- `source_title` set to the meeting topic portion of the folder name
- Folder name with no topic portion (only timestamp) uses the full folder name as `source_title`

---

### FR-14: VTT Parsing for Local Recordings

Use the existing `_parse_vtt()` method unchanged — Zoom's `closed_caption.vtt` uses
the same WebVTT standard as cloud transcript downloads.

**No new code required.** The VTT format is identical:
```
WEBVTT

1
00:00:05.000 --> 00:00:10.500
Sarah Chen: Good morning everyone.
```

**Duration:** derived from the last segment's start timestamp (already done by `_parse_vtt`
via `duration_ms`). No audio file metadata parsing needed.

**Validation criteria:**
- `_parse_vtt()` produces correct segments from a local `closed_caption.vtt`
- Duration correctly derived from last VTT timestamp

---

### FR-15: LLM Summary for Local Recordings

Generate meeting summary and tags using the same `summarize-transcript` skill as the
cloud recording path.

**Watcher role note:** watcher nodes currently do not run LLM summarisation. For local
recordings on a watcher node, two options exist:
- **Option A (v1):** Run the `summarize` model call on the watcher node using the
  existing `skill_executor.py`. The watcher already has `GEMINI_API_KEY` available.
- **Option B:** Write a "pending summarisation" stub file that the full node picks up
  and summarises on its next scan.

Spec recommends **Option A** for simplicity. The watcher's LLM call for meeting
summaries is a low-frequency, short-prompt operation (< 1000 tokens).

**Validation criteria:**
- Summary and tags generated for local recording VTT
- LLM failure falls back to meeting topic as summary (same as cloud path)

---

### FR-16: Memory File Write for Local Recordings

Write `meeting-{date}-{slug}-{hash}.md` for each processed local recording.

**Differences from cloud recording files:**

| Field | Cloud recording | Local recording |
|---|---|---|
| `source_url` | `zoom:{meeting_uuid}` | `local:{8-char-folder-hash}` |
| `zoom_meeting_id` | Numeric ID from API | Absent (not available) |
| `participants` | Emails from Participants API | Empty list `[]` |
| `speakers` | Names from VTT | Names from VTT (same) |
| `duration_minutes` | From Zoom API | Derived from last VTT timestamp |

**`source_url` scheme:** `local:` prefix with an 8-char SHA1 of the full folder path,
e.g. `local:a3f2c1b8`. This provides a stable, unique, human-readable dedup key
without exposing the full local filesystem path in iCloud.

**All other fields and write mechanics** (atomic rename, YAML frontmatter, transcript
section truncation at 50 lines) are identical to the cloud path.

**Validation criteria:**
- `type: meeting_transcript` in frontmatter (downstream consumers pick it up)
- `source_url` uses `local:` scheme
- `participants: []` and speakers list populated from VTT
- No temp files left after write

---

### FR-17: Deduplication for Local Recordings

Prevent reprocessing already-handled local recording folders.

**State file extension (`zoom-scanner-state.json`):**
```json
{
  "processed_uuids": ["..."],
  "processed_local": ["a3f2c1b8", "7d91e4f2"],
  "last_poll": "..."
}
```

`processed_local` stores the 8-char folder path hashes used in `source_url`. Capped at
10,000 entries, trimmed from front.

Backward compat: state files without `processed_local` key initialise it to `[]`.

**Validation criteria:**
- Folder already in `processed_local` skipped without re-reading the VTT
- State saved after each individual meeting

---

### FR-18: Participant Handling for Local Recordings

Participant email addresses are unavailable for local recordings (no Zoom API call).

**Memory file behaviour:**
- `participants: []` (empty list)
- `speakers: [...]` populated from VTT speaker names as usual

**Downstream impact:**
- Commitment tracker uses `participants` with fallback to `speakers` — no breakage
- Contact tracker will record interactions by speaker name only (no email dedup)
- Project inference does not use participants/speakers — no impact

**Future option:** a `local_speaker_emails` mapping in `config.yaml` (e.g.
`"Sarah Chen": sarah@example.com`) to resolve local recording speakers to emails.
Out of scope for v1.

**Validation criteria:**
- `participants` field present in frontmatter as empty list
- `speakers` field populated from VTT

---

### FR-19: Duplicate Detection Between Local and Cloud Recordings

The same meeting may be recorded both locally and in Zoom cloud. Both the cloud and
local scanners would independently discover it and create separate memory files.

**v1 behaviour:** Allow both memory files to exist. Downstream consumers (commitment
tracker, project inference) will see both and may extract commitments twice. This is
acceptable for v1 given the low overlap in practice.

**Future dedup approach:** Match by (`meeting_date` ± 30 min) + (`source_title`
fuzzy match ≥ 0.8). If a cloud recording file already exists for the same meeting,
skip the local recording. Out of scope for v1.

**Validation criteria:**
- Scanner does not crash or error when both a cloud and local file exist for the same meeting
- No attempt to merge or overwrite the cloud memory file with local data

---

## Config

```yaml
zoom_scanner:
  interval_seconds: 300
  initial_lookback_days: 30
  local_recordings_enabled: false          # opt-in; set true to scan ~/Documents/Zoom/
  local_recordings_path: ~/Documents/Zoom  # override if recordings saved elsewhere
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
| `test_cmd_meetings_lists_recent` | `/meetings` returns N most recent meeting files |
| `test_cmd_meetings_default_n_10` | Without N arg, returns at most 10 |
| `test_cmd_meetings_custom_n` | `/meetings 5` returns 5 entries |
| `test_cmd_meetings_n_clamped` | N=999 clamped to 50; N=0 clamped to 1 |
| `test_cmd_meetings_sets_last_meeting_set` | `_last_meeting_set` populated after call |
| `test_cmd_meeting_detail_view` | `/meeting 1` shows date, attendees, summary |
| `test_cmd_meeting_invalid_index` | `/meeting 99` without prior list → error message |
| `test_local_folder_discovered` | Folder matching `YYYY-MM-DD HH.MM.SS *` pattern is found |
| `test_local_folder_without_vtt_skipped` | Folder without `closed_caption.vtt` → skipped at DEBUG level |
| `test_local_vtt_parsed` | Local `closed_caption.vtt` parsed identically to cloud VTT |
| `test_local_metadata_from_folder_name` | `meeting_date` and `source_title` extracted from folder name |
| `test_local_duration_from_vtt` | Duration derived from last VTT timestamp when no API metadata |
| `test_local_source_url_scheme` | `source_url` uses `local:` prefix with 8-char folder hash |
| `test_local_participants_empty` | `participants: []`, `speakers` populated from VTT names |
| `test_local_dedup` | Folder in `processed_local` skipped without re-reading VTT |
| `test_local_state_backwards_compat` | State file without `processed_local` → initialised to `[]` |
| `test_local_disabled_by_default` | `local_recordings_enabled: false` → directory not scanned |
| `test_local_missing_directory_logs_warning` | Missing `local_recordings_path` → WARNING once, no crash |

---

## Zoom App Setup (for README)

1. Go to [Zoom Marketplace](https://marketplace.zoom.us/) → Develop → Build App → Server-to-Server OAuth
2. Add scopes: `recording:read:admin`, `meeting:read:admin`, `user:read:admin`
3. Copy Account ID, Client ID, Client Secret
4. Add to launchd plist `EnvironmentVariables`: `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`
5. Enable cloud recording with transcription in Zoom account settings (Account Management → Recording)
6. Run `./install.sh` — it will detect the new env vars and add them to the plist

---

## Changelog

### v1.2.0 — 2026-04-16

Added local recording support (FR-11 through FR-19):
- Scan `~/Documents/Zoom/` for meeting folders with `closed_caption.vtt`
- Parse meeting date and topic from folder name
- Reuse `_parse_vtt()` unchanged — same WebVTT format
- `source_url: local:{hash}` scheme; `participants: []`
- Dedup via `processed_local` set in state file
- Off by default (`local_recordings_enabled: false`)
- Runs on watcher role (local recordings exist on the recording machine)

### v1.1.0 — 2026-04-11

**New FRs:**

#### FR-10: /meetings and /meeting Telegram commands
**Priority:** High

**`/meetings [N]`** — list meeting transcripts from memory files.

- Globs `BRAIN_DIR/memories/meeting-*.md`
- Filters on `type == "meeting_transcript"`
- Sorts by `start_time` (or `created`) descending — most recent first
- Default N=10; clamp `[1, 50]`
- Sets `self._last_meeting_set` to displayed paths (for `/meeting N`)
- Reply format: `N. [YYYY-MM-DD] title — M participants`
- If no results: `"No meeting transcripts found."`

**`/meeting <N>`** — show full detail for meeting N from last `/meetings` list.

- Resolves index from `self._last_meeting_set` via `_resolve_meeting_index`
- Reply includes: title, date, duration, participants, summary
- If N out of range or `_last_meeting_set` empty: `"Invalid index. Run /meetings first."`

**CommandHandler registrations:**

```python
self.app.add_handler(CommandHandler("meetings", self.cmd_meetings))
self.app.add_handler(CommandHandler("meeting",  self.cmd_meeting))
```
