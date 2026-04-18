---
specmas: 3.0
kind: feature
id: feat-zoom-ai-companion
version: 1.0.0
created: 2026-04-16
status: implemented
shipped_version: "1.4.0"
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-zoom-transcript-scanner
  - feat-commitment-tracker
  - feat-goal-project-agent
---

# Zoom AI Companion Meeting Summary Integration

## Overview

### Problem Statement

The existing Zoom scanner processes cloud recordings by downloading VTT transcripts and
running an LLM to generate summaries. Two gaps remain:

1. **LLM cost and latency on every meeting** — Zoom's AI Companion already generates a
   high-quality summary, overview, and action items for each meeting. The scanner
   discards this and pays for its own LLM call.

2. **Blind to AI-only meetings** — Many meetings have AI Companion enabled but cloud
   recording disabled (the host didn't hit Record). These meetings produce no VTT,
   so the scanner never processes them — their decisions, commitments, and action
   items are lost.

The AI Companion Meeting Summary API (`GET /v2/meetings/{meetingId}/meeting_summary`)
exposes Zoom-generated summaries via the same Server-to-Server OAuth already in use.
Integrating it eliminates redundant LLM calls and expands meeting coverage to
AI-Companion-only meetings.

### Scope

**In Scope:**
- Polling `GET /v2/meetings/meeting_summaries` to discover meetings with AI Companion summaries
- Fetching individual summaries and parsing `summary_content` into structured sections
- When both VTT and AI Companion summary exist: use AI Companion summary, skip LLM call
- When only AI Companion summary exists (no VTT): create a memory file from it
- `summary_source` frontmatter field for traceability
- `## Action Items` body section parsed from AI Companion response
- Deduplication via extended `zoom-scanner-state.json`
- Graceful degradation if scopes not granted (continue with VTT-only path)

**Out of Scope:**
- Zoom AI Companion Whiteboard or Clips summaries (meetings only)
- `meeting.summary_completed` webhook (Phase B — polling matches existing pattern)
- Structured access to smart chapters or per-participant attribution within summaries
  (Zoom API does not expose these as separate fields)
- Retroactive reprocessing of meetings beyond the 30-day Zoom retention window

### Success Metrics

- LLM calls eliminated for meetings with AI Companion summaries
- Memory files created for AI-Companion-only meetings within one poll cycle
- Zero changes required to downstream consumers (commitment tracker, project inference)
- Graceful no-op on accounts without paid Zoom Workplace plan or AI Companion enabled

---

## Functional Requirements

### FR-1: Poll AI Companion Summaries

Discover meetings with AI Companion summaries in each scan cycle alongside existing
recordings polling.

**API endpoint:**
```
GET https://api.zoom.us/v2/meetings/meeting_summaries
    ?from={YYYY-MM-DD}
    &to={YYYY-MM-DD}
    &page_size=100
Authorization: Bearer {token}
```

**Polling strategy:**
- Call once per scan cycle after the existing recordings poll
- `from` = `(now - initial_lookback_days)` on first run; `(now - interval_seconds * 2)` on subsequent
- Paginate via `next_page_token` until exhausted
- Returns list of `{ meeting_id, meeting_uuid, meeting_topic, summary_created_time, ... }`
- Merge with recordings result by numeric `meeting_id`: some meetings appear in both sets,
  some only in one

**Validation criteria:**
- Summaries discovered within one scan cycle of `summary_created_time`
- Pagination handled correctly
- Empty result (no AI Companion meetings) handled without error

---

### FR-2: Fetch Individual Meeting Summary

Retrieve full summary content for each meeting discovered in FR-1.

**API endpoint:**
```
GET https://api.zoom.us/v2/meetings/{meetingId}/meeting_summary
Authorization: Bearer {token}
```

**Important implementation notes:**
- Use the **numeric meeting ID** (e.g. `84481630996`), not the UUID. Multiple developers
  have reported error code 3001 / `entity not exist` when using a UUID here.
- If the UUID contains `//`, it must be double URL-encoded on other endpoints — but
  this endpoint uses the numeric ID and avoids that issue entirely.
- The meeting must be past (completed). Scheduled/upcoming meetings return 3001.

**Response fields consumed:**
```json
{
  "meeting_host_email": "host@example.com",
  "meeting_uuid": "...",
  "meeting_id": 12345678,
  "meeting_topic": "Q4 Planning Review",
  "meeting_start_time": "2026-04-11T10:00:00Z",
  "meeting_end_time": "2026-04-11T10:45:00Z",
  "summary_created_time": "2026-04-11T10:52:00Z",
  "summary_title": "Q4 Planning Review",
  "summary_content": "<html>...</html>"
}
```

**Validation criteria:**
- Numeric meeting ID used in request path
- 3001 (meeting not found) logged at DEBUG, not WARNING (expected for old meetings)
- 403 (missing scopes) triggers graceful degradation (see FR-8)
- `summary_content` absent or empty: log DEBUG, skip this meeting

---

### FR-3: Parse `summary_content` into Sections

Extract a clean overview, action items, and next steps from the `summary_content` blob.

**Format uncertainty:** Zoom developer forums report `summary_content` is returned as HTML
on some accounts, plain text on others. Parse both:

```python
def _parse_summary_content(content: str) -> dict:
    """Returns {"overview": str, "action_items": [str], "next_steps": [str]}"""
```

**HTML path:**
1. Strip HTML tags with a simple regex or `html.parser`
2. Look for section markers: `Overview`, `Action Items`, `Next Steps`
3. Split on those markers; treat everything before the first marker as overview

**Plain-text path:**
- Same section-marker logic on raw text
- Fallback: treat the entire content as `overview` with empty action_items and next_steps

**Output example:**
```python
{
    "overview": "The team discussed Q4 budget targets and timeline...",
    "action_items": [
        "Sarah to send revised numbers by Friday",
        "Mike to follow up on vendor contracts",
    ],
    "next_steps": ["Schedule follow-up for next Tuesday"],
}
```

**Validation criteria:**
- HTML tags stripped cleanly (no `<p>`, `<li>`, etc. in output)
- Missing sections return empty list/string (not an exception)
- Malformed or empty content returns empty dict without raising

---

### FR-4: Merge AI Companion Summary with VTT Transcript

When a meeting has both a cloud recording VTT and an AI Companion summary, produce a
single memory file using the best data from each source.

**Merge rules:**
- `## Summary` section: use AI Companion overview (skip `acompletion` LLM call)
- `## Transcript` section: keep VTT speaker-attributed segments (richer detail for
  downstream LLM extraction by commitment tracker and project inference)
- `## Action Items` section: add from AI Companion (new section, not present in VTT-only files)
- `## Key Decisions` section: keep if LLM previously generated it; omit for merged files
  (AI Companion overview is the source of truth)
- Frontmatter `summary`: AI Companion overview text (truncated to 300 chars if needed)
- Frontmatter `summary_source: ai_companion`

**Cost benefit:** LLM call to `summarize-transcript` skill eliminated for these meetings.

**Validation criteria:**
- `acompletion` not called when AI Companion summary available
- Memory file includes both `## Transcript` and `## Action Items` sections
- `summary_source: ai_companion` in frontmatter
- Downstream consumers (commitment tracker) can parse the merged file correctly

---

### FR-5: AI-Companion-Only Meeting Memory Files

When a meeting has an AI Companion summary but no cloud recording (no VTT), create a
memory file using the AI Companion data alone — expanding coverage beyond cloud-recorded
meetings.

**Memory file format (AI-Companion-only):**
```markdown
---
source_title: "Weekly Standup"
summary: "Team discussed sprint progress and blockers..."
tags: [standup, sprint, blockers]
last_scanned: '2026-04-11T10:52:00'
source_url: zoom:84481630996
type: meeting_transcript
participants: []
speakers: []
duration_minutes: 22
meeting_date: '2026-04-11T10:00:00'
zoom_meeting_id: '84481630996'
summary_source: ai_companion
---

## Summary
Team discussed sprint progress and blockers for the current sprint cycle.
Sarah confirmed the API integration is on track. Mike flagged a dependency
on the DevOps team for the deployment pipeline.

## Action Items
- Mike to follow up with DevOps team by Thursday
- Sarah to share integration test results before EOD

## Next Steps
- Review sprint board in Friday's retrospective
```

**Notes:**
- `participants` and `speakers` are empty lists (no VTT, no participants API call)
- `source_url` uses numeric meeting ID (`zoom:{meeting_id}`) since no UUID available
  from the summary-only API response
- LLM tags generation still runs (brief prompt using `summary_content` overview text)
  since AI Companion does not return tags

**Validation criteria:**
- Memory file created in `MEMORIES_DIR` with correct frontmatter
- `type: meeting_transcript` enables downstream pipeline pickup
- Commitment tracker and project inference parse the file successfully
- No VTT-specific sections (`## Transcript`) included

---

### FR-6: Deduplication

Prevent reprocessing already-fetched AI Companion summaries across daemon restarts.

**State file extension (`zoom-scanner-state.json`):**
```json
{
  "processed_uuids": ["uuid1", "uuid2"],
  "processed_summaries": ["84481630996", "73390741885"],
  "last_poll": "2026-04-11T15:00:00"
}
```

`processed_summaries` stores numeric meeting IDs (strings) of meetings whose AI Companion
summary has been fetched and processed. Capped at 10,000 entries, trimmed from front.

Backward compat: state files without `processed_summaries` key initialise it to `[]`.

**Validation criteria:**
- Already-processed meeting IDs skipped without an API call
- State saved after each individual meeting (not batch) to survive mid-cycle crashes

---

### FR-7: `summary_source` Frontmatter Field

All meeting memory files include a `summary_source` field for traceability.

| Scenario | `summary_source` value |
|---|---|
| AI Companion summary used | `ai_companion` |
| LLM summary generated from VTT | `llm` |

Backward compat: existing files without `summary_source` are implicitly `llm` — no
migration needed. Downstream consumers must not require this field.

---

### FR-8: Graceful Degradation

If AI Companion scopes are not granted or the feature is disabled on the account,
the scanner must fall back to the existing VTT-only path silently.

**Degradation triggers:**
- `403 Forbidden` from `GET /v2/meetings/meeting_summaries` or individual summary endpoint
- `zoom_scanner.ai_companion_enabled: false` in config

**Behaviour:**
- Log one WARNING per daemon startup if degraded (not every scan cycle)
- Continue processing cloud recordings and VTT transcripts as before
- `summary_source: llm` set on all memory files in degraded mode

**Zoom prerequisite (for README):**
The account admin must enable "Meeting summary with AI Companion" at
`zoom.us` → Account Management → Account Settings → AI Companion
before the `meeting:read:list_summaries:admin` and `meeting:read:summary:admin`
scopes appear in the Marketplace Server-to-Server OAuth app builder.

**Validation criteria:**
- 403 does not crash scan loop
- Warning logged at most once per daemon restart
- Existing VTT-based memory files unaffected
- Config toggle `ai_companion_enabled: false` immediately disables summary polling

---

### FR-9: Rate Limit Handling

Reuse existing `_api_get` helper with 429/Retry-After handling.

**Additional calls per cycle:**
- 1 × `GET /v2/meetings/meeting_summaries` (paginated — typically 1-2 pages)
- Up to 20 × `GET /v2/meetings/{id}/meeting_summary`

**Fits within existing rate budget** — Zoom rate limits are per-endpoint, and the
existing scanner already uses at most ~22 API calls per cycle for recordings + participants.

**Validation criteria:**
- 429 from summary endpoints handled with same retry logic as recording endpoints
- Meetings skipped due to rate limits are not added to `processed_summaries` (retried next cycle)

---

## Config

```yaml
zoom_scanner:
  interval_seconds: 300
  initial_lookback_days: 30
  ai_companion_enabled: true    # set false to disable summary polling
  prefer_ai_summary: true       # use AI Companion summary over LLM when both available
```

New scopes to add to existing Zoom Server-to-Server OAuth app:
- `meeting:read:list_summaries:admin`
- `meeting:read:summary:admin`

---

## Memory File Changes

**New frontmatter field:** `summary_source: ai_companion | llm`

**New optional body section:**
```markdown
## Action Items
- {line per action item from AI Companion}
```

**No changes to `type: meeting_transcript`** — downstream pipeline picks up all
meeting files without modification.

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `specs/feat-zoom-ai-companion.md` | **This spec** |
| `zoom_scanner.py` | Add `_list_meeting_summaries()`, `_get_meeting_summary()`, `_parse_summary_content()`, merge logic in `_process_meeting()`, extend state schema |
| `tests/unit/test_zoom_scanner.py` | Add tests per table below |
| `README.md` | Add AI Companion scopes + admin setup to Zoom App Setup section |
| `CHANGELOG.md` | Entry under `[Unreleased]` on implementation |

---

## Unit Tests

| Test | Assertion |
|------|-----------|
| `test_ai_summary_replaces_llm_when_both_exist` | VTT + AI Companion → `summary_source: ai_companion`, no `acompletion` call |
| `test_ai_summary_only_creates_memory` | AI Companion only (no VTT) → valid `meeting-*.md` with correct frontmatter |
| `test_vtt_only_unchanged` | VTT, no AI Companion → `summary_source: llm`, `acompletion` called |
| `test_summary_content_html_parsed` | HTML `summary_content` → clean overview + action_items list |
| `test_summary_content_plain_text_parsed` | Plain-text `summary_content` → overview extracted |
| `test_summary_content_empty_returns_empty` | Empty `summary_content` → empty dict, no exception |
| `test_action_items_section_written` | `## Action Items` section present in merged memory file |
| `test_dedup_processed_summaries` | Meeting ID in `processed_summaries` → API call skipped |
| `test_graceful_degradation_403` | 403 from summary endpoint → WARNING once, VTT-only path continues |
| `test_ai_companion_disabled_config` | `ai_companion_enabled: false` → no summary API calls |
| `test_summary_source_frontmatter_ai` | `summary_source: ai_companion` when AI Companion used |
| `test_summary_source_frontmatter_llm` | `summary_source: llm` when VTT-only path used |
| `test_downstream_commitment_parseable` | AI Companion memory file parseable by commitment tracker logic |
| `test_state_file_backwards_compat` | State file without `processed_summaries` initialised to `[]` |
| `test_numeric_meeting_id_used` | Summary fetched using numeric ID, not UUID |

---

## Open Questions

1. **`summary_content` format stability** — Zoom developer forums report inconsistent
   formatting (HTML vs plain text) across accounts and API versions. The parser must
   handle both, but the section-marker text (`Overview`, `Action Items`, etc.) may
   also vary by locale or Zoom version. Validate against real `summary_content` before
   finalising the parser.

2. **Webhook vs polling** — Zoom emits a `meeting.summary_completed` webhook event
   when AI Companion finishes generating a summary (~5-10 min after meeting ends).
   The webhook would reduce latency and eliminate unnecessary polling. Requires an
   inbound HTTP server, which the daemon currently does not have. Phase B.

3. **Action item overlap with commitment tracker** — AI Companion action items and
   commitment tracker extractions will often describe the same commitments. Two
   options: (a) let both run and rely on dedup within the commitment tracker; or
   (b) add `summary_source: ai_companion` awareness to the commitment tracker to
   skip its own LLM extraction when AI Companion already identified the action items.
   Option (a) is simpler and correct for v1.

4. **30-day retention** — Zoom auto-deletes AI Companion summaries after 30 days.
   The scanner must process meetings within that window. If `initial_lookback_days`
   is set > 30, summaries for older meetings will simply return 404 (not an error).

5. **Zoom plan tier** — AI Companion requires a paid Zoom Workplace plan (Pro or above).
   On free accounts, the API returns no summaries (not an error). Should the scanner
   detect and warn at startup? Low priority — the degradation is silent and harmless.

---

## Changelog

### v1.0.0 — 2026-04-16

Initial spec.
