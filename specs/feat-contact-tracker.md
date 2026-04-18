---
specmas: 3.0
kind: feature
id: feat-contact-tracker
version: 1.1.0
created: 2026-04-11
status: implemented
shipped_version: "1.3.0"
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-email-scanner
  - feat-zoom-transcript-scanner
  - feat-calendar-scanner
  - feat-slack-scanner
  - feat-proactive-notifications
  - feat-commitment-tracker
---

# Contact Tracker

## Overview

### Problem Statement

Secondbrain accumulates memories about meetings, emails, calendar events, and Slack
threads — all of which involve people. But there is no unified view of those people.
The user cannot ask "when did I last talk to Sarah?", "who do I interact with most on
the product team?", or "show me everything related to Mike Peters." Relationship context
is scattered across individual memory files with no aggregation. The Proactive
Notifications system also needs contact data to assemble pre-meeting briefs.

The Contact Tracker watches for new and updated memory files that contain participants,
upserts a flat-file contact record per person, and exposes contact views through Telegram
commands.

### Scope

**In Scope:**
- Lightweight async daemon loop, running every 5 minutes (`full` role only)
- Watches for new/updated `email_thread`, `meeting_transcript`, `calendar_event`, and
  `slack_thread` memory files (mtime-based, same pattern as Commitment Tracker FR-1)
- One `contact-{name-slug}.md` per person — upserted on each interaction
- Name normalization and email-based deduplication
- Recency-weighted relationship score (no graph database)
- LLM-generated interaction summary (regenerated when interaction count grows by ≥N)
- Telegram commands: `/contacts` (list by recency), `/contact <name>` (detail view)

**Out of Scope:**
- Graph database or network analysis (flat files only)
- Contacts.app or address book import
- Contact creation or editing via Telegram
- Duplicate merging UI (edit the markdown file directly)
- Profile photos or external data enrichment
- Organisation hierarchy or reporting relationships
- Multi-user or shared contact views

### Success Metrics

- Contact file created or updated within one scan cycle of a new memory file
  mentioning a person
- `/contact <name>` returns a result for any person who has appeared in at least one
  memory file
- Relationship scores rank frequent collaborators above one-off contacts
- `/contact <name>` responds within 2 seconds for up to 500 contact files

---

## Functional Requirements

### FR-1: Watch for New and Updated Source Memory Files

Monitor `MEMORIES_DIR` for memory files of configured source types that have been
created or modified since the last scan cycle — the same mtime-based pattern as
`commitment_tracker.py` FR-1.

**Source types:**
```yaml
contact_tracker:
  source_types:
    - email_thread
    - meeting_transcript
    - calendar_event
    - slack_thread
```

**Change detection:**
- Load `DEPLOY_DIR/contact-tracker-state.json` on startup
- State stores `last_scan: ISO timestamp` and `processed: {filename: mtime}` map
- On each cycle: glob `MEMORIES_DIR/*.md`, read frontmatter `type` field (cached header)
- For files matching `source_types`: compare current mtime to `processed[filename]`
- Process files where mtime has changed or file is not in `processed`
- After processing, update `processed[filename] = current_mtime`
- Save state atomically after each file
- Cap at 50 files per cycle

**Participants extraction:**
Each source type stores participants differently in frontmatter:

| Type | Frontmatter field | Format |
|------|------------------|--------|
| `email_thread` | `participants` | `["alice@example.com", "bob@example.com"]` |
| `meeting_transcript` | `participants` | `[{name: "Alice", email: "..."}]` or list of strings |
| `calendar_event` | `participants` | `[{name: "Alice", email: "..."}]` |
| `slack_thread` | `participants` | `[{name: "Alice", slack_id: "..."}]` |

Parse each format; extract name (if available) and email (if available). Rows with
neither name nor email are skipped.

**Validation criteria:**
- New source memory files processed within one scan cycle
- Files of other types (webpage, code_project, commitment) skipped
- Daemon restart does not reprocess already-processed files
- State file write is atomic (temp file + os.rename)

---

### FR-2: Name Normalization and Email-Based Deduplication

Resolve multiple representations of the same person to a single contact file.

**Normalization rules (applied in order):**
1. Strip whitespace and normalize Unicode (NFC)
2. If an email address is present, use it as the primary dedup key
3. Lowercase the email for comparison; store original casing of the first-seen display name
4. If no email: lowercase the full name, remove punctuation, use as dedup key

**Canonical slug** (used in filename):
```python
def _name_to_slug(name: str) -> str:
    # "Sarah Chen" → "sarah-chen"
    # "Dr. Jane O'Brien" → "jane-obrien"
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
```

**Dedup by email:**
- If two interactions have the same email but different display names
  (e.g., "S. Chen" vs "Sarah Chen"), the canonical name is the longest version seen
- Contact file named after the canonical slug
- All known emails stored in `emails[]` frontmatter field
- All known display name variants tracked internally (not surfaced in frontmatter)

**Collision handling:**
- If two people produce the same slug (e.g., "John Smith" x2), the second gets
  `contact-john-smith-2.md`. In practice, email dedup prevents most collisions.

**Validation criteria:**
- Same email with different display names → one contact file
- Same person in email and calendar with the same email → one contact file updated
- Unknown-email participant creates contact keyed by name slug
- Slug is stable — same input always produces same slug

---

### FR-3: Relationship Score Calculation

Compute a numeric relationship score that ranks frequent, recent collaborators above
infrequent or stale ones.

**Formula:**
```python
def _relationship_score(interactions: list[datetime]) -> float:
    now = datetime.utcnow()
    return sum(
        1.0 / max((now - ts).days, 1)
        for ts in interactions
    )
```

This is a recency-weighted interaction count. An interaction yesterday contributes 1.0;
an interaction 10 days ago contributes 0.1; an interaction 30 days ago contributes 0.03.
No external dependencies, no graph database.

**Interaction recording:**
- Each source memory file that mentions a contact counts as one interaction
- Interaction timestamp = `last_message` (email), `meeting_date` (Zoom), `start_time`
  (calendar), or `last_message` (Slack), parsed from frontmatter
- Store up to 100 most recent interaction timestamps per contact in state
  (state file, not in the contact markdown file)

**Score storage:** `relationship_score: 2.47` in frontmatter, rounded to 2 decimal
places. Updated on each upsert.

**Validation criteria:**
- Contact with 5 interactions this week scores higher than contact with 2 interactions
  last month
- Score decreases monotonically as interactions age
- Score of 0.0 for a contact with no interactions in the state window

---

### FR-4: Interaction Summary Generation (LLM)

Generate a human-readable summary of recent interactions for each contact.

**When to regenerate:**
- On first contact file creation
- When `interaction_count` has grown by at least `summary_refresh_threshold` (default 3)
  since the last LLM call
- Regeneration count tracked in `DEPLOY_DIR/contact-tracker-state.json` per contact

**LLM route:** `summarize` (Gemini Flash via LiteLLM)

**Prompt input:**
- Contact name and known emails
- Last 5 interactions: source type, date, source title, and one-sentence context
  (the `summary` field from the source memory file frontmatter)

**Prompt structure:**
```
Summarize the recent interactions with this person in 2-3 sentences.
Focus on recurring topics, open items, and relationship context.

Person: {name}
Known emails: {emails}

Recent interactions (newest first):
1. {date} | {source_type} | {source_title}
   Context: {source summary}
2. ...

Return plain text only (no JSON, no markdown headers).
```

**Validation criteria:**
- LLM not called when `interaction_count` growth < `summary_refresh_threshold`
- Summary replaces previous body on regeneration
- LLM failure → previous summary retained (file not overwritten)
- Summary < 500 characters

---

### FR-5: `/contacts` Telegram Command

List contacts sorted by most recent interaction.

**Usage:** `/contacts [N]`

**Behaviour:**
- Glob `MEMORIES_DIR/contact-*.md`
- Sort by `last_interaction` descending
- Default: top 20; max: 50 (if `N` provided)
- Store result set in `_last_contact_set` (session-scoped, same pattern as `/memories`)

**Reply format:**
```
Contacts (47 total):
1. Sarah Chen — last: 2026-04-10 (email, meeting) — score: 3.42
2. Mike Peters — last: 2026-04-09 (slack) — score: 1.87
3. Alex Wong — last: 2026-04-08 (calendar) — score: 0.95
...

Use /contact <name> or /contact <N> for details.
```

The "source types" shown in parentheses are the distinct types of most recent interactions.

**Validation criteria:**
- Responds within 2 seconds for up to 500 contact files
- Empty list returns friendly message
- Index numbers usable with `/contact N`

---

### FR-6: `/contact <name>` Detail Command

Show a detailed view of a specific contact, including recent interactions and open
commitments involving that person.

**Usage:** `/contact <name or index>`

**Behaviour:**
- If argument is a number: resolve from `_last_contact_set`
- If argument is text: case-insensitive substring match against `name` field across
  all `contact-*.md` files; return closest match
- Load the contact file
- Glob `commitment-*.md` where `owner` or `recipient` matches the contact's name or
  email; filter to `status: active`

**Reply format:**
```
Sarah Chen (sarah@example.com)
Relationship score: 3.42 | 12 interactions

Recent interactions:
• 2026-04-10 — Q4 Planning email thread (email)
• 2026-04-08 — Team Standup (calendar)
• 2026-04-05 — Migration discussion (slack, #engineering)
• 2026-04-03 — 1:1 with Sarah (meeting)

Open commitments:
• [outbound] Send revised budget numbers — due 2026-04-18
• [waiting_on] Waiting for design review — due unknown

Summary:
Sarah and I primarily discuss Q4 planning and the product roadmap. She owes
me a design review and I owe her the revised budget.
```

**Validation criteria:**
- Case-insensitive name match (partial match: "sarah" matches "Sarah Chen")
- No match returns "No contact found for '{name}'. Try /contacts to browse."
- Commitments section omitted if no open commitments found
- `/contact 3` resolves from `_last_contact_set` (same session)

---

### FR-7: Contact File Format

Write one `contact-{name-slug}.md` per person.

**Filename:**
```
contact-{name-slug}.md
```

**File format:**
```markdown
---
source_title: "Sarah Chen"
summary: Sarah is a frequent collaborator on Q4 planning and product roadmap discussions.
tags: [product, planning, q4]
last_scanned: '2026-04-11T14:30:00'
source_url: contact:sarah-chen
type: contact
name: Sarah Chen
emails:
  - sarah.chen@acme.com
last_interaction: '2026-04-10T16:00:00'
interaction_count: 12
relationship_score: 3.42
---

## Recent Interactions

Sarah and I have met frequently over the past week, primarily on Q4 planning and
the product roadmap. She is waiting for my revised budget numbers and has a design
review pending.

## Interaction History

- 2026-04-10 — Q4 Planning (email_thread)
- 2026-04-08 — Team Standup (calendar_event)
- 2026-04-05 — Migration thread (slack_thread)
```

**Frontmatter field order** (`sort_keys=False`):
`source_title`, `summary`, `tags`, `last_scanned`, `source_url`, `type`,
`name`, `emails`, `last_interaction`, `interaction_count`, `relationship_score`

**Write rules:**
- Atomic write via temp file + `os.rename()`
- `source_url` uses `contact:` scheme with the name slug
- `source_title` = canonical display name
- Upsert: if file exists, load, update fields, rewrite; do not overwrite `summary`
  unless regeneration threshold reached (FR-4)
- Interaction history in body: last 10 entries, newest first

**Validation criteria:**
- `type: contact` in all written files
- `source_url` uses `contact:` scheme
- File written atomically
- Existing `summary` not overwritten unless LLM regeneration occurs

---

### FR-8: State Tracking

Persist processed file mtimes, interaction timestamps, and LLM regeneration counters
across daemon restarts.

**State file:** `DEPLOY_DIR/contact-tracker-state.json`

```json
{
  "last_scan": "2026-04-11T14:30:00",
  "processed": {
    "email-thread-q4-planning-abc123.md": 1712860000.0
  },
  "contacts": {
    "sarah-chen": {
      "interaction_timestamps": [
        "2026-04-10T16:00:00",
        "2026-04-08T09:00:00"
      ],
      "last_summary_interaction_count": 9
    }
  }
}
```

- `processed`: filename → mtime, same as commitment tracker
- `contacts[slug].interaction_timestamps`: list of ISO datetimes, capped at 100
- `contacts[slug].last_summary_interaction_count`: the `interaction_count` at the time
  of the last LLM summary generation; used to determine when to regenerate

**Validation criteria:**
- State file created on first run
- `interaction_timestamps` capped at 100 per contact (oldest pruned)
- State file write is atomic
- Daemon restart does not re-extract already-processed files

---

## Config

```yaml
contact_tracker:
  interval_seconds: 300
  source_types:
    - email_thread
    - meeting_transcript
    - calendar_event
    - slack_thread
  summary_refresh_threshold: 3   # new interactions before regenerating LLM summary
  max_files_per_cycle: 50
```

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `specs/feat-contact-tracker.md` | **This spec** |
| `contact_tracker.py` | **Create** — ContactTracker class |
| `daemon.py` | Add ContactTracker to full-role gather (loop TBD — after calendar, before notifications) |
| `chat_handler.py` | Add `/contacts` and `/contact` command handlers |
| `config.yaml.template` | Add `contact_tracker` section |
| `install.sh` | Add `contact_tracker.py` to DAEMON_FILES |
| `CLAUDE.md` | Update loop count and descriptions; document new commands |
| `README.md` | Document contact tracker, new Telegram commands |
| `tests/unit/test_contact_tracker.py` | **Create** |
| `tests/integration/test_pipeline.py` | Add contact tracker integration test |

---

## Unit Tests (`tests/unit/test_contact_tracker.py`)

| Test | Assertion |
|------|-----------|
| `test_upsert_creates_new_contact` | New participant creates contact file |
| `test_upsert_updates_existing_contact` | Second interaction updates `last_interaction` and `interaction_count` |
| `test_name_normalization_lowercase` | "Sarah Chen" and "sarah chen" map to same slug |
| `test_email_dedup_same_email_different_name` | Same email, different name variants → one contact file |
| `test_slug_special_characters` | Punctuation stripped from slug |
| `test_slug_max_length` | Slug capped at 40 characters |
| `test_collision_handling` | Two "John Smith" contacts get distinct filenames |
| `test_relationship_score_recent_higher` | Contact with 3 recent interactions > contact with 5 old interactions |
| `test_relationship_score_decays` | Score lower when all interactions are 30+ days old |
| `test_relationship_score_zero_no_interactions` | Empty interaction list → score 0.0 |
| `test_interaction_timestamps_capped_at_100` | 101st interaction drops oldest |
| `test_summary_not_regenerated_below_threshold` | 2 new interactions → no LLM call |
| `test_summary_regenerated_at_threshold` | 3 new interactions → LLM called |
| `test_summary_retained_on_llm_failure` | LLM error → old summary preserved |
| `test_contact_file_atomic_write` | No .tmp file left after write |
| `test_contact_file_type` | `type: contact` in frontmatter |
| `test_contact_file_field_order` | `source_title` first in frontmatter |
| `test_source_url_scheme` | `source_url` starts with `contact:` |
| `test_existing_summary_not_overwritten` | Upsert below threshold preserves existing body |
| `test_email_participant_extraction` | email_thread participants (strings) parsed |
| `test_calendar_participant_extraction` | calendar_event participants (dicts) parsed |
| `test_slack_participant_extraction` | slack_thread participants (slack_id) parsed |
| `test_cmd_contacts_sorted_by_recency` | `/contacts` returns most recent first |
| `test_cmd_contacts_default_top_20` | Without N argument, returns at most 20 |
| `test_cmd_contact_name_match` | `/contact sarah` matches "Sarah Chen" |
| `test_cmd_contact_case_insensitive` | `/contact SARAH` matches "Sarah Chen" |
| `test_cmd_contact_partial_match` | `/contact che` matches "Sarah Chen" |
| `test_cmd_contact_index_resolution` | `/contact 2` resolves from `_last_contact_set` |
| `test_cmd_contact_not_found` | Unknown name → friendly error message |
| `test_cmd_contact_shows_open_commitments` | Active commitment files for contact shown |
| `test_skip_non_source_types` | webpage and code_project files ignored |
| `test_state_file_persists` | Processed map and interaction timestamps survive restart |

---

## Changelog

### v1.1.0 — 2026-04-11

**New FRs:**

#### FR-9: /people alias for /contacts
**Priority:** Low

Register `/people` as an additional command name that invokes the same handler
as `/contacts`. No behavioural change — purely a discoverability alias.

```python
self.app.add_handler(CommandHandler("people", self.cmd_contacts))
```

The COMMAND_REGISTRY in `chat_handler.py` lists both `people` and `contacts`
with distinct descriptions:
- `people` — "List contacts (alias of /contacts)"
- `contacts` — "List people you've interacted with"
