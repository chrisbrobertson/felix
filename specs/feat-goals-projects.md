---
specmas: 3.0
kind: feature
id: feat-goals-projects
version: 1.0.0
created: 2026-04-15
status: draft
complexity: high
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-commitment-tracker
  - feat-proactive-notifications
  - feat-code-project-scanner
---

# Goals, Projects, and Code-Repo Namespace Rename

## Overview

### Problem Statement

The system captures what the user reads, says, and commits to — but has no way to
represent what the user is *working toward*. "I want to run a 5K by June", "build a
garden shed this summer", "coordinate the Q2 product launch", and "learn Spanish" are
all real things the user is doing, but they live nowhere in the brain. The chat agent
has no awareness of them, no notifications fire when deadlines approach, and there is
no way to ask "what am I working on right now?".

Additionally, the existing `type: project` + `category: code` namespace — used by the
Project Scanner to represent auto-discovered git repositories — blocks the broader
meaning. The word "project" must mean *any* effort (personal, work, family, learning),
not just code repositories. The code-repo namespace must move to its own top-level type
(`type: code`) before projects can be generalised.

This spec defines two new top-level types (`goal`, `project`), the complete CRUD and
lifecycle management surface for both, LLM-inferred project discovery from comms
memories, a human-confirmation flow for discovered candidates, and the one-shot
migration that renames the existing code-repo namespace.

### Scope

**In Scope:**
- Two new memory types: `goal` (outcome) and `project` (any effort)
- Configurable categories: `[personal, work, family, learning, other]` via `config.yaml`
- CRUD Telegram commands for goals and projects
- Inline milestones on projects
- Goal↔project linking
- Deadline notifications at 7-day and 1-day horizons
- Active goals/projects always injected into chat-handler LLM context
- LLM tools (`add_goal`, `add_project`) enabling natural-language creation via chat
- Thirteenth async loop: `project_inference_scanner.py` — infers projects from comms
  memories (`email_thread`, `meeting_transcript`, `slack_thread`) with confidence ≥ 0.7
- `project-candidate-*.md` files with `status: pending_confirmation`
- `/review`, `/confirm`, `/reject`, `/edit` commands for candidate management
- `rejected-candidates.json` dedup list
- **FR-13: Code-repo namespace rename** — `type: project` + `category: code` →
  `type: code`; filename `project-{hostname}-{name}.md` → `code-{hostname}-{name}.md`;
  module `project_scanner.py` → `code_scanner.py`; command `/projects` (code repos) →
  `/code`; one-shot migration on daemon startup
- CLAUDE.md and README updates to reflect the rename

**Out of Scope:**
- LLM auto-extraction of *goals* from comms (goals remain user-initiated)
- Sub-goals, goal hierarchies, progress percentages, burn-down charts
- Shared / collaborative tracking across users
- Integration with external task managers (OmniFocus, Notion, Linear, etc.)
- Habit tracking (repeating goals — separate spec)
- Automatic milestone generation from comms (milestones remain manual)

### Success Metrics

- Goal and project CRUD round-trips complete in < 2 seconds
- Inference loop discovers ≥ 60% of active projects from comms within 24 hours
- Candidate confirmation flow completes in < 5 Telegram messages
- Migration runs on daemon startup without data loss and is idempotent
- Active goals/projects appear in chat context within one index rebuild cycle

---

## Functional Requirements

### FR-1: Goal File Format and Validation

Write one `goal-{slug}-{6-char-id}.md` per goal.

**Filename:**
```
goal-{slug}-{6-char-id}.md
```
where `slug` is `source_title` lowercased, spaces → hyphens, max 40 chars;
`6-char-id` is the first 6 hex chars of `sha1(source_title + created_iso)`.

**Frontmatter shape:**
```yaml
---
type: goal
category: personal       # must be in configured goals.categories list
source_title: Run a 5K
summary: Run a 5K race by the end of June 2026
tags: [health, fitness]
created: '2026-04-15T09:00:00'
due_date: '2026-06-30'
status: active           # active | completed | abandoned
priority: medium         # low | medium | high | critical
linked_projects: []      # list of project filenames
notes: ''
---
```

**Write rules:**
- Atomic write via temp file + `os.rename()`
- `category` validated against `config["goals"]["categories"]` — invalid category
  raises `ValueError` with the configured list in the message
- `due_date` must be `YYYY-MM-DD` format or `null`; invalid format raises `ValueError`
- Stable ID dedup: if a file with the same ID already exists, skip write and return
  existing filename
- Status transitions: `active` → `completed` or `active` → `abandoned` only; no other
  transitions permitted

**Validation criteria:**
- `type: goal` present in all written files
- Atomic write leaves no temp file on crash
- Invalid category rejected before any file I/O
- Stable ID produces the same filename for two calls with the same title + timestamp
- Completed/abandoned goals not reverted to active by any command

---

### FR-2: Project File Format and Milestone Shape

Write one `project-{category}-{slug}-{6-char-id}.md` per project.

**Filename:**
```
project-{category}-{slug}-{6-char-id}.md
```
Same slug and ID derivation as FR-1 but includes `category` in the filename for
easy glob filtering.

**Frontmatter shape:**
```yaml
---
type: project
category: work           # must be in configured goals.categories list (not "code")
source_title: Q2 rollout plan
summary: Coordinate Q2 launch across eng, design, and marketing
tags: [q2, launch]
created: '2026-04-15T09:00:00'
due_date: '2026-07-01'
status: active           # active | completed | abandoned | on-hold
priority: high
linked_goal: goal-q2-targets-ab12cd.md  # null if not linked
milestones:
  - text: "Lock feature scope"
    done: false
  - text: "Draft launch checklist"
    done: true
inferred_from: []        # source memory filenames if LLM-discovered
notes: ''
---
```

**Milestone rules:**
- Stored inline in project frontmatter (no separate milestone files)
- Each milestone is `{text: str, done: bool}`
- Adding a milestone appends to the list; toggling flips `done` in-place
- Milestone index references are 1-based (matching Telegram command convention)

**Write rules:**
- Same atomic write + validation rules as FR-1
- `category` must not be `"code"` — code repos have their own `type: code` namespace
- `status: on-hold` allowed on projects (not on goals)

**Validation criteria:**
- `type: project` present in all written files
- `category: code` rejected at the `GoalManager` layer before file I/O
- Milestone list preserved on status-only writes (partial frontmatter update forbidden)
- `inferred_from` field present and non-null in all files written by the inference loop

---

### FR-3: Telegram Commands — Goals

Expose full goal lifecycle management via Telegram.

**Commands:**

| Command | Behaviour |
|---------|-----------|
| `/addgoal` | Bot prompts for title, category, due date (optional), priority (optional); creates goal file |
| `/goals [category\|status]` | List active goals; filter by category or status if provided |
| `/goal N` | Show full detail of goal N from last `/goals` result set |
| `/completegoal N` | Set `status: completed` on goal N |
| `/abandongoal N` | Set `status: abandoned` on goal N |

**List format:**
```
Active goals (4 total):
1. [personal] Run a 5K — due 2026-06-30
2. [work] Q2 OKR alignment — due 2026-04-30 ⚠️ 7 days
3. [learning] Finish Spanish A2 course — no due date
4. [family] Plan summer camping trip — due 2026-07-15
```

**Session result set:** `_last_goal_set` (same pattern as `_last_commitment_set` in
`chat_handler.py:1060-1122`) — populated by `/goals`, consumed by `/goal N`,
`/completegoal N`, `/abandongoal N`.

**Validation criteria:**
- Invalid index returns "Invalid index. Run /goals first."
- Empty list returns friendly message, not error
- `/goals status:completed` shows only completed goals
- `/goals category:work` shows only work-category goals
- Category filter validates against configured list

---

### FR-4: Telegram Commands — Projects

Expose full project lifecycle management including milestones.

**Commands:**

| Command | Behaviour |
|---------|-----------|
| `/addproject` | Bot prompts for title, category, due date (optional), linked goal (optional); creates project file |
| `/projects [category\|status]` | List active projects; filter by category or status if provided |
| `/project N` | Show full detail of project N including milestone list |
| `/completeproject N` | Set `status: completed` |
| `/abandonproject N` | Set `status: abandoned` |
| `/holdproject N` | Set `status: on-hold` |
| `/addmilestone N <text>` | Append milestone with `done: false` to project N |
| `/milestone N M` | Toggle `done` on milestone M of project N |

**Session result sets:**
- `/projects` populates `_last_project_set`
- `/project N` detail shows milestones with 1-based indices for `/milestone N M`

**Project detail format:**
```
Q2 rollout plan [work] — active
Due: 2026-07-01 · Priority: high
Linked goal: Q2 OKR alignment

Milestones:
  ✓ Lock feature scope
  ○ Draft launch checklist
  ○ Stakeholder sign-off

Use /milestone 1 M to toggle a milestone.
```

**Validation criteria:**
- `/milestone N M` with invalid M returns "Invalid milestone index."
- `/holdproject N` not available on goals (project-only status)
- Milestone text truncated to 200 chars if longer

---

### FR-5: Goal↔Project Linking

Allow a project to be linked to a parent goal. Both files updated atomically.

**Commands:**

| Command | Behaviour |
|---------|-----------|
| `/linkgoal <project_N> <goal_M>` | Set `linked_goal` on project N to goal M filename; add project filename to `linked_projects` on goal M |
| `/unlinkgoal <project_N>` | Clear `linked_goal` on project N; remove from `linked_projects` on goal M |

**Mechanism:**
- Read both files, update both in memory, write goal file first (temp + rename), then
  project file (temp + rename). If goal write succeeds but project write fails, re-read
  goal and remove the stale project reference — rollback rather than leaving inconsistent state.

**Validation criteria:**
- Both files updated or neither (rollback on partial failure)
- Linking a project to a non-existent goal returns clear error
- Unlinking a project that has no linked goal is a no-op with a friendly message
- `linked_projects` on the goal is a list — multiple projects can link to one goal

---

### FR-6: Deadline Notifications

Push proactive Telegram alerts when goal or project deadlines approach.

**Mechanism:**
- Add `_check_goal_alerts()` and `_check_project_alerts()` to `notification_manager.py`,
  following the same pattern as `_check_commitment_alerts()` at lines 397–479.
- Call from `run_notification_loop()` on every 60-second tick.
- Dedup via `notification-state.json` using key `goal_alert:{filename}:{horizon}` and
  `project_alert:{filename}:{horizon}`.

**Alert horizons:** 7 days and 1 day before `due_date`.

**Conditions for alert:**
- `status: active` (goals) or `status: active` or `status: on-hold` (projects)
- `due_date` is set and non-null
- Alert for this filename+horizon not already in `notification-state.json`

**Alert format:**
```
⏰ Goal deadline approaching: "Run a 5K" — due in 7 days (2026-06-30)
```

**Validation criteria:**
- Alert fires once per horizon per item (deduped)
- Items without `due_date` silently skipped
- Completed/abandoned items silently skipped
- Missing `notification-state.json` treated as empty (first run)

---

### FR-7: Active Context Injection

Always include active goals and projects in the chat-handler LLM context, bypassing
keyword relevance scoring.

**Mechanism:**
- In `chat_handler.py` context-load path, after loading keyword-relevant memories,
  prepend up to 5 active goals and 5 active projects (sorted by `due_date` ascending,
  nulls last).
- These items are loaded by filename glob (`goal-*.md`, `project-*.md`) and frontmatter
  filter (`status: active`), not keyword intersection.
- Cap: 5 goals + 5 projects. If more exist, closest deadlines win.

**Context block format:**
```
## Active Goals
- Run a 5K [personal] — due 2026-06-30
- Q2 OKR alignment [work] — due 2026-04-30

## Active Projects
- Q2 rollout plan [work] — due 2026-07-01 (milestones: 1/3 done)
- Garden shed build [personal] — no due date
```

**Validation criteria:**
- Goals/projects appear even when query has no keyword overlap with their content
- Cap (5+5) enforced — 6th item not included
- On-hold projects included (active status filter is `active` OR `on-hold`)
- Completed/abandoned items excluded

---

### FR-8: LLM Tools for Natural-Language Creation

Allow the chat agent to create goals and projects from natural language without
requiring the user to invoke slash commands explicitly.

**New tools in `chat_tools.TOOLS`:**

```python
add_goal(
    title: str,
    category: str,               # must be in configured list
    due_date: str | None = None, # YYYY-MM-DD
    priority: str = "medium",    # low | medium | high | critical
) -> str  # returns created filename

add_project(
    title: str,
    category: str,
    due_date: str | None = None,
    linked_goal: str | None = None,  # goal filename
) -> str  # returns created filename
```

**Dispatch:** tools call the same `GoalManager.create_goal()` and
`GoalManager.create_project()` helpers used by the slash commands — no duplicated logic.

**Example trigger:** User says "I want to run a 5K by end of June." The chat agent
calls `add_goal(title="Run a 5K", category="personal", due_date="2026-06-30")` and
replies "Goal created: Run a 5K (personal) — due 2026-06-30."

**Validation criteria:**
- Invalid category passed to tool returns tool error string (not a Python exception)
- Tool result message confirms creation with filename
- Tool does not create duplicate if user rephrases the same goal in one session
  (dedup check in `GoalManager` via stable ID)

---

### FR-9: Project Inference from Comms Memories

New thirteenth async loop (`project_inference_scanner.py`, `full` role only) that
scans comms memory files and infers what projects the user is working on.

**Loop cadence:** Every 15 minutes.

**Source types:** `email_thread`, `meeting_transcript`, `slack_thread`

**Change detection:** mtime-based, same pattern as `commitment_tracker.py:499`.
State file: `DEPLOY_DIR/project-inference-state.json`.

**LLM prompt:**
```
Given the following {source_type} content, identify any projects this person appears
to be working on. A project is any distinct effort that spans multiple tasks or
interactions — could be work, personal, family, learning, or any other domain.

Source: {source_title}
Date: {date}
Content: {summary + messages/transcript, capped at 2000 chars}

Configured project categories: {goals.categories from config}

Return JSON only:
{
  "projects": [
    {
      "title": "Q2 rollout plan",
      "category_guess": "work",
      "summary": "Coordinating Q2 product launch across eng, design, marketing",
      "confidence": 0.85,
      "due_date_guess": "2026-07-01",
      "evidence_quote": "Can you have the Q2 launch checklist ready by EOQ?"
    }
  ]
}

Only include items with confidence >= 0.7. Return [] if no projects detected.
```

**LLM route:** `summarize` (Gemini Flash)

**Dedup logic:**
- Before writing a candidate, glob `MEMORIES_DIR/project-*.md` and
  `MEMORIES_DIR/project-candidate-*.md`
- Compute title similarity (lowercase, strip punctuation, compare token sets)
- If similarity ≥ 0.8 with an existing project or candidate, skip (already known)
- Also skip if source file is listed in `rejected-candidates.json` under any entry

**Validation criteria:**
- Items below 0.7 confidence discarded (prompt instructs LLM; scanner enforces)
- Duplicate detection prevents re-proposing confirmed projects
- Missing state file treated as empty (first run)
- Cap at 20 source files per cycle to bound LLM cost

---

### FR-10: Code-Repo Discovery via Confirmation Flow

On first discovery of a new git repository, `code_scanner.py` writes a
`project-candidate-*.md` file instead of directly writing a `code-{hostname}-*.md`
file, gated by a config flag.

**Config flag:** `code_scanner.require_confirmation: true` (default `true` for new
installs; existing installs that upgrade default to `false` to avoid retroactively
quarantining all known repos).

**Candidate shape for code repos:**
```yaml
---
type: project_candidate
category_guess: null     # code repos don't map to the goals category list
source_title: my-new-repo (code repository)
summary: LLM-inferred from README
confidence: 1.0          # scanner discovery is deterministic
evidence:
  - code-discovery:{hostname}:{repo-name}
extracted_fields:
  title: my-new-repo
  local_path: /Users/chris/repos/my-new-repo
  default_branch: main
  languages: [python]
  head_sha: abc123
candidate_type: code_repo   # distinguishes from project candidates
status: pending_confirmation
created: '2026-04-15T09:00:00'
---
```

**On confirmation** (via `/confirm N`): write a real `code-{hostname}-{name}.md` file
with `type: code` frontmatter (see FR-13).

**On rejection** (via `/reject N`): delete candidate; add the `local_path` to
`rejected-candidates.json` so the scanner never re-proposes it.

**On subsequent scans of already-confirmed repos:** continue the existing auto-update
flow (update `head_sha`, `last_scanned`) without re-entering the confirmation flow.

**Validation criteria:**
- With `require_confirmation: false`, scanner writes `code-*.md` directly (existing behaviour)
- With `require_confirmation: true`, first discovery writes candidate; re-scan of known
  repo updates in-place
- Rejected `local_path` never re-proposed

---

### FR-11: Review and Confirmation Commands

Allow the user to inspect, confirm, reject, and edit pending project candidates.

**Project candidate file format:**
```yaml
---
type: project_candidate
category_guess: work
source_title: Q2 rollout plan (candidate)
summary: LLM-inferred from 3 meetings and 2 emails
confidence: 0.82
evidence:
  - meeting-2026-04-10-q2-planning-abc123.md
  - email-thread-q2-launch-def456.md
extracted_fields:
  title: Q2 rollout plan
  due_date: '2026-07-01'
  participants: [alice@acme.com, bob@acme.com]
candidate_type: project    # or "code_repo" for FR-10 candidates
status: pending_confirmation
created: '2026-04-15T09:00:00'
---
```

**Commands:**

| Command | Behaviour |
|---------|-----------|
| `/review` | List all `project-candidate-*.md` files grouped by `candidate_type` |
| `/review N` | Show detail of candidate N including evidence filenames |
| `/confirm N [category]` | Promote candidate to real project or code entry; `category` overrides `category_guess` |
| `/reject N` | Delete candidate; add evidence to `rejected-candidates.json` |
| `/edit N field=value` | Update a field on candidate N before confirming |

**Session result set:** `_last_candidate_set` — populated by `/review`, consumed by
`/review N`, `/confirm N`, `/reject N`, `/edit N`.

**`/confirm N` promotion logic:**
- If `candidate_type: project`: call `GoalManager.create_project()` with `extracted_fields`
  (category from `category_guess` or override), set `inferred_from: [evidence list]`,
  delete candidate file.
- If `candidate_type: code_repo`: write `code-{hostname}-{name}.md` (FR-13 format),
  delete candidate file.

**`rejected-candidates.json` format:**
```json
{
  "rejected": [
    {
      "source_title": "Q2 rollout plan",
      "evidence": ["meeting-abc.md", "email-def.md"],
      "rejected_at": "2026-04-15T09:00:00"
    }
  ]
}
```

**Validation criteria:**
- `/confirm N work` overrides `category_guess` with `"work"`
- Invalid category in `/confirm` returns error with configured list
- `/reject N` removes candidate file and updates `rejected-candidates.json` atomically
- `/edit N due_date=2026-08-01` updates `extracted_fields.due_date` in the candidate file
- Empty review list returns "No pending candidates."

---

### FR-12: Configurable Categories

`config.yaml` `goals.categories` is the single source of truth for all goal and
project category values.

**Config:**
```yaml
goals:
  categories:
    - personal
    - work
    - family
    - learning
    - other
```

**Default list:** `[personal, work, family, learning, other]`

**Rules:**
- `code` is never in this list — code repos use `type: code`, not `type: project`
- All commands accepting a `category` argument validate against this list
- Inference loop's category-guess prompt includes the configured list
- Users extend the list by editing config and restarting the daemon
- Unknown category values in config entries are logged at WARNING, not crashed

**Validation criteria:**
- Adding a new category to config makes it valid for subsequent commands without code change
- `code` category rejected even if a user adds it to the config list (hard-coded exclusion)
- Invalid category error message includes the current configured list

---

### FR-13: Code-Repo Namespace Rename and Migration

Rename the existing `type: project` + `category: code` namespace to `type: code`.
This is a one-shot migration that runs on `CodeScanner.__init__` startup, mirroring
the precedent set when `type: code_project` was migrated to `type: project` +
`category: code` (see `project_scanner.py` `__init__` and CLAUDE.md
"Project type generalization" note).

**Rename table:**

| Old | New |
|-----|-----|
| `type: project` + `category: code` | `type: code` |
| Filename `project-{hostname}-{name}.md` | `code-{hostname}-{name}.md` |
| Module `project_scanner.py` | `code_scanner.py` |
| Class `ProjectScanner` | `CodeScanner` |
| Telegram command `/projects` (code repos) | `/code` |
| Telegram command `/project N` (code repo detail) | `/code N` |
| Config key `project_scanner.*` | `code_scanner.*` |

**Code-repo frontmatter after rename:**
```yaml
---
type: code               # was: type: project, category: code
source_title: secondbrain
summary: Personal knowledge system — async daemon, flat-file iCloud storage, Telegram bot
tags: [python, llm]
hostname: Chriss-MacBook-Air
local_path: /Users/chris/repos/secondbrain
default_branch: main
languages: [python, shell]
head_sha: abc123def456
last_scanned: '2026-04-15T09:00:00'
---
```

**Migration algorithm (runs in `CodeScanner.__init__`):**
1. Glob `MEMORIES_DIR/project-*.md`
2. For each file, read frontmatter; skip if `type != "project"` or `category != "code"`
3. Remove `category` field; set `type: code`
4. Derive new filename: replace `project-{hostname}-` prefix with `code-{hostname}-`
5. Write updated frontmatter to new filename (atomic temp + rename)
6. Delete old filename
7. Log each migrated file at INFO level
8. If new filename already exists (prior partial migration), skip old file and delete it
   (idempotency)

**Migration is idempotent:** Re-running on already-migrated files is safe — step 2
skips files that are no longer `type: project` + `category: code`.

**CLAUDE.md updates required:**
- Code Structure: rename `project_scanner.py` → `code_scanner.py` entry
- Architecture loop 5: update description to use `code_scanner.py`, `type: code`,
  `code-{hostname}-*.md`, `/code` command
- Key Design Decisions: add follow-up note to "Project type generalization" entry
  describing this rename and the date it was applied

**README updates required:**
- Rename `/projects` (code repos) → `/code` in command reference
- Add note about the one-shot migration for users upgrading

**Validation criteria:**
- After migration, no `project-{hostname}-*.md` files remain in MEMORIES_DIR
- After migration, all former code-repo files have `type: code` in frontmatter
- Re-running migration on already-migrated files is a no-op (no errors, no data loss)
- New repos discovered after migration go directly to `code-{hostname}-*.md` format
- `/code` command lists code repos; `/projects` command is reassigned to the new
  generic project listing (FR-4) — these are now different commands

---

## Config

```yaml
goals:
  categories:
    - personal
    - work
    - family
    - learning
    - other
  deadline_horizons:
    - 7   # days before due_date to send first alert
    - 1   # days before due_date to send final alert
  max_context_items: 5   # max goals and max projects injected into chat context

project_inference:
  enabled: true
  scan_interval_min: 15
  confidence_threshold: 0.7
  source_types:
    - email_thread
    - meeting_transcript
    - slack_thread

code_scanner:             # renamed from project_scanner
  interval_seconds: 300
  require_confirmation: false   # true for new installs; false preserves existing behaviour
```

---

## Files to Create / Modify / Rename

| File | Change |
|------|--------|
| `specs/feat-goals-projects.md` | **This spec** |
| `goals_tracker.py` | **Create** — `GoalManager` class; `create_goal`, `list_goals`, `update_goal_status`, `create_project`, `list_projects`, `update_project_status`, `toggle_milestone`, `link_goal_to_project`, `confirm_candidate`, `reject_candidate` |
| `project_inference_scanner.py` | **Create** — 13th async loop; scans comms memories, writes `project-candidate-*.md`; state in `project-inference-state.json` |
| `project_scanner.py` → `code_scanner.py` | **Rename** — class `ProjectScanner` → `CodeScanner`; runs one-shot migration in `__init__`; adds `require_confirmation` flow (FR-10) |
| `chat_handler.py` | Add `cmd_addgoal`, `cmd_goals`, `cmd_goal`, `cmd_completegoal`, `cmd_abandongoal`; `cmd_addproject`, `cmd_projects`, `cmd_project`, `cmd_completeproject`, `cmd_abandonproject`, `cmd_holdproject`, `cmd_addmilestone`, `cmd_milestone`; `cmd_linkgoal`, `cmd_unlinkgoal`; `cmd_review`, `cmd_confirm`, `cmd_reject`, `cmd_edit`; rename `cmd_projects` (code) → `cmd_code`, `cmd_project` (code) → `cmd_code_detail`; extend context-load for FR-7 |
| `chat_tools.py` | Add `add_goal`, `add_project`, `confirm_project_candidate` tool schemas + dispatch |
| `notification_manager.py` | Add `_check_goal_alerts()`, `_check_project_alerts()`, optional `_check_pending_candidates()` (weekly nudge); wire into `run_notification_loop()` |
| `daemon.py` | Import `code_scanner` (was `project_scanner`); register `project_inference_scanner` under `full` role guard |
| `config.yaml` template | Add `goals`, `project_inference`, `code_scanner` sections; remove `project_scanner` section |
| `CLAUDE.md` | Update Code Structure, loop 5 description, Key Design Decisions |
| `README.md` | Document `/goals`, `/projects`, `/code`, `/review`; add migration note |
| `tests/unit/test_goals_tracker.py` | **Create** — unit tests for `GoalManager` |
| `tests/unit/test_project_inference_scanner.py` | **Create** — unit tests for inference loop |
| `tests/unit/test_code_scanner.py` | **Create** (was `test_project_scanner.py`) — includes migration tests |

### Filename Migration Table

| Old filename pattern | New filename pattern | Trigger |
|----------------------|----------------------|---------|
| `project-{hostname}-{name}.md` (type:project, category:code) | `code-{hostname}-{name}.md` | `CodeScanner.__init__` one-shot migration |

---

## Migration Plan

### Step-by-step migration (runs automatically on first daemon startup after deploy)

1. `CodeScanner.__init__` is called by `daemon.py` during startup.
2. Glob `MEMORIES_DIR/project-*.md`.
3. For each matched file:
   a. Parse YAML frontmatter.
   b. If `type != "project"` or `category != "code"`: skip.
   c. Remove `category` key; set `type: "code"`.
   d. Compute new filename: `code-{hostname}-{name}.md` (same hostname and name components).
   e. If new filename already exists: delete old file only (previous partial migration).
   f. Otherwise: write updated content to `{new_filename}.tmp`, `os.rename()` to new
      filename, then `os.unlink()` old filename.
   g. Log `INFO: migrated {old} → {new}`.
4. Proceed with normal scan loop.

### Idempotency

After migration, `project-*.md` files with `type: project` + `category: code` no
longer exist. Step 3b skips all other `project-*.md` files (future `type: project`
generic files created by FR-2 will have a different category and will not be touched).
Re-running is safe.

### Rollback

No automated rollback. In the unlikely event of a failed migration, the original files
are only deleted *after* the new file is successfully written (step 3f). A crash between
the rename and unlink leaves both files present; the next startup re-runs and skips via
step 3e.

---

## Unit Tests

### `tests/unit/test_goals_tracker.py`

| Test | Assertion |
|------|-----------|
| `test_create_goal_writes_file` | File written with `type: goal`, correct frontmatter fields |
| `test_create_goal_invalid_category` | Category not in configured list → `ValueError` |
| `test_create_goal_invalid_due_date` | Bad date format → `ValueError` |
| `test_create_goal_stable_id_dedup` | Same title+timestamp → same filename, no overwrite |
| `test_create_goal_atomic_write` | No temp file left after write |
| `test_update_goal_status_active_to_completed` | `status: completed` written correctly |
| `test_update_goal_status_active_to_abandoned` | `status: abandoned` written correctly |
| `test_update_goal_status_invalid_transition` | `completed` → `active` raises error |
| `test_create_project_code_category_rejected` | `category: code` → `ValueError` |
| `test_create_project_milestone_inline` | Milestones stored in frontmatter |
| `test_toggle_milestone_flips_done` | Milestone 1 `done: false` → `done: true` |
| `test_toggle_milestone_invalid_index` | Index out of range → `ValueError` |
| `test_link_goal_to_project_dual_write` | Both files updated; `linked_goal` + `linked_projects` set |
| `test_link_goal_rollback_on_project_write_failure` | Goal write succeeds, project write fails → goal reverted |
| `test_list_goals_filter_by_category` | Category filter returns only matching goals |
| `test_list_goals_sorted_by_due_date` | Nulls last, soonest deadline first |
| `test_confirm_candidate_project_creates_project` | `GoalManager.create_project()` called with evidence |
| `test_confirm_candidate_code_creates_code_file` | `type: code` file written on code_repo candidate confirm |
| `test_reject_candidate_updates_rejected_json` | Evidence added to `rejected-candidates.json` |

### `tests/unit/test_project_inference_scanner.py`

| Test | Assertion |
|------|-----------|
| `test_skips_unchanged_mtime` | Same mtime → no LLM call |
| `test_processes_new_mtime` | Updated mtime → LLM call made |
| `test_confidence_filter_below_threshold` | confidence=0.6 → no candidate written |
| `test_confidence_filter_above_threshold` | confidence=0.75 → candidate written |
| `test_dedup_existing_project_by_title` | Similar title to existing project → skipped |
| `test_dedup_rejected_evidence` | Evidence in `rejected-candidates.json` → skipped |
| `test_candidate_file_format` | Written file has `type: project_candidate`, required fields |
| `test_cap_20_files_per_cycle` | 25 changed files → only 20 processed |

### `tests/unit/test_code_scanner.py` (migration tests)

| Test | Assertion |
|------|-----------|
| `test_migration_renames_file` | `project-host-foo.md` → `code-host-foo.md` on init |
| `test_migration_updates_type` | `type: code` in migrated file; `category` removed |
| `test_migration_idempotent` | Re-running migration on already-migrated files → no-op |
| `test_migration_skips_non_code_projects` | Generic `type: project` + `category: work` → untouched |
| `test_migration_partial_recovery` | New filename exists on init → old file deleted, new kept |
| `test_require_confirmation_false_writes_directly` | New repo with flag=false → `code-*.md` written |
| `test_require_confirmation_true_writes_candidate` | New repo with flag=true → `project-candidate-*.md` written |
| `test_known_repo_rescan_updates_inplace` | Already-confirmed repo → `code-*.md` updated, no new candidate |

---

## Changelog

### v1.0.0 (2026-04-15)
- Initial spec covering FR-1 through FR-13
- Two new top-level types: `goal` and `project`
- Configurable categories sourced from `config.yaml goals.categories`
- Full CRUD + milestones + linking + deadline notifications + context injection
- Dual creation path: slash commands + LLM tools
- Thirteenth async loop: `project_inference_scanner.py`
- Project candidate confirmation flow with `/review`, `/confirm`, `/reject`, `/edit`
- FR-13: one-shot migration renames `type: project` + `category: code` → `type: code`;
  renames `project_scanner.py` → `code_scanner.py`; reassigns Telegram commands
