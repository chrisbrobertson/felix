---
specmas: 3.0
kind: feature
id: feat-goals-projects
version: 1.0.0
created: 2026-04-14
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-commitment-tracker
  - feat-proactive-notifications
  - feat-feature-tracker
---

# Goals and Personal Projects

## Overview

### Problem Statement

The second brain accumulates memories of what the user *has done* (meetings,
emails, browsing) and what they *owe others* (commitments). It has no concept
of what the user *wants to achieve*. There is no way to ask "what are my
goals for this quarter?" or "what personal projects am I running?", no place
to record a milestone like "submit conference talk proposal", and no
notification when a goal deadline is approaching.

The Goals and Personal Projects feature adds two new manually-managed memory
types — `goal` and `project` (category `personal`) — with Telegram commands
for full CRUD, milestone tracking inside project files, and deadline
notifications integrated into the existing notification manager.

Code-repository projects are already covered by `project_scanner.py` and are
out of scope here.

### Scope

**In Scope:**
- New `type: goal` memory files — outcome-oriented, optionally time-bound
- New `type: project` + `category: personal` memory files — effort-oriented,
  can be linked to a goal
- Telegram commands for create, list, detail, update, complete, and abandon
- Inline milestones stored as a YAML list within each project file; toggled via
  Telegram
- Lightweight goal→project linking via a `linked_goal:` frontmatter field
- Deadline alerts (1 day, 1 week before due_date) via `notification_manager.py`
- `/goals` and `/projects personal` unified under the existing `/projects`
  command with a `personal` category filter

**Out of Scope:**
- LLM auto-extraction of goals from meeting or email memories (all entries are
  user-initiated)
- Sub-goals or goal hierarchies (flat list only in v1)
- Shared / collaborative goal tracking
- Progress percentage or burn-down charts
- Integration with external task managers (OmniFocus, Notion, Linear)
- Habit tracking (repeating goals — a distinct concept, separate spec)

### Success Metrics

- `/addgoal` and `/addproject` round-trip (add → `/goals` / `/projects` list
  shows new item) within one Telegram exchange
- `/completegoal N` and `/completeproject N` persist status and item no longer
  appears in the default (active-only) list view
- Deadline notifications fire within 60 seconds of the 09:00 daily briefing
  window when a goal or project `due_date` is ≤ 7 days away
- Full test coverage: unit tests for file write/read helpers; integration test
  for the create → list → complete round-trip

---

## Functional Requirements

### FR-1: Goal Memory File Format

Each goal is a single markdown file in `MEMORIES_DIR`.

**Filename:** `goal-{slug}-{6-char-id}.md`

The slug is derived from the title; the 6-char ID is the first 6 hex chars of
`sha1(title.lower().strip())` for stable deduplication.

**Frontmatter shape:**
```yaml
---
type: goal
source_title: Run a 5K
summary: Run a 5K race by the end of June 2026
tags: [health, fitness]
created: '2026-04-14T09:00:00'
due_date: '2026-06-30'
status: active          # active | completed | abandoned
priority: medium        # low | medium | high | critical
linked_projects: []     # list of project memory filenames (optional)
notes: ''               # free-form text
---

Run a 5K race by the end of June 2026.
```

**Status transitions:**
- `active` → `completed` via `/completegoal N`
- `active` → `abandoned` via `/abandongoal N`
- No transition back from terminal states in v1

**Validation criteria:**
- File written atomically (tmp + rename via `memory_writer.py`)
- Duplicate prevention: if a file with the same 6-char ID already exists, skip
  write and return a "goal already exists" message
- `due_date` must be a valid ISO date string or absent; invalid formats rejected
  with a user-facing error before write

---

### FR-2: Personal Project Memory File Format

Personal projects reuse the existing `type: project` namespace with
`category: personal`, consistent with the generalization documented in
`project_scanner.py` and CLAUDE.md.

**Filename:** `project-personal-{slug}-{6-char-id}.md`

This distinguishes personal projects from hostname-scoped code projects
(`project-{hostname}-{name}.md`).

**Frontmatter shape:**
```yaml
---
type: project
category: personal
source_title: Build home office
summary: Set up a dedicated home office with standing desk and proper lighting
tags: [home, productivity]
created: '2026-04-14T09:00:00'
due_date: '2026-07-01'
status: active          # active | completed | abandoned | on-hold
priority: medium
linked_goal: goal-run-a-5k-ab12cd.md   # optional: one parent goal
milestones:
  - text: "Order standing desk"
    done: false
  - text: "Cable management"
    done: true
notes: ''
---

Set up a dedicated home office with standing desk and proper lighting.
```

**Milestones** are stored inline in the frontmatter as a YAML list of
`{text, done}` objects. They are toggled via `/milestone N M` (project N,
milestone M). There is no separate file per milestone.

**Validation criteria:**
- Milestone list preserved across status updates (only `status` / `done` flags
  mutate on commands; other fields are never touched)
- Atomic file rewrite when toggling milestone `done` flag

---

### FR-3: Telegram Commands — Goals

Add the following commands to `COMMAND_REGISTRY` under a new `"Goals"` group
in `chat_handler.py`:

| Command | Signature | Description |
|---|---|---|
| `/addgoal` | `/addgoal <text> [by <date>]` | Create a new goal |
| `/goals` | `/goals [status]` | List goals (default: active only) |
| `/goal` | `/goal <N>` | Show goal N from last `/goals` list |
| `/completegoal` | `/completegoal <N>` | Mark goal N completed |
| `/abandongoal` | `/abandongoal <N>` | Mark goal N abandoned |

**`/addgoal` parsing:**
- Full text after command is the goal title/description
- Optionally extract `by <date>` suffix as `due_date` (natural language date
  parsing via `dateutil.parser.parse`; reject ambiguous or past dates with a
  clarifying message)
- Remaining text after date extraction becomes `source_title` + body

**`/goals` list format** (mirrors `/commitments`):
```
Goals (4 active):
1. Run a 5K — due Jun 30
2. Learn basic Spanish — no deadline
3. Read 12 books this year — due Dec 31
4. Build home studio — due Sep 1
```
Stores result in `self._last_goal_set` (list of file paths) for index
resolution by `/goal N`, `/completegoal N`, `/abandongoal N`.

**`/goal N` detail format:**
```
Run a 5K
Status: active  Priority: medium  Due: Jun 30, 2026
Tags: health, fitness

Run a 5K race by the end of June 2026.

Linked projects: none
```

**Validation criteria:**
- Empty `/addgoal` (no text) returns usage error
- Past `due_date` returns a warning ("That date is in the past — add anyway?")
  and requires confirmation (`/addgoal confirm`) in next turn
- `/completegoal N` on an already-completed goal returns "already completed"
- Index out of range returns "No goal N in last list — run /goals first"

---

### FR-4: Telegram Commands — Personal Projects

Add the following commands to `COMMAND_REGISTRY` under a new `"Personal
Projects"` group:

| Command | Signature | Description |
|---|---|---|
| `/addproject` | `/addproject <text> [by <date>]` | Create a personal project |
| `/projects` | `/projects [category] [N]` | Existing command; `personal` filter now shows personal projects |
| `/project` | `/project <N>` | Existing command; works for personal projects too |
| `/completeproject` | `/completeproject <N>` | Mark project N completed |
| `/abandonproject` | `/abandonproject <N>` | Mark project N abandoned |
| `/holdproject` | `/holdproject <N>` | Mark project N on-hold |
| `/addmilestone` | `/addmilestone <N> <text>` | Add a milestone to project N |
| `/milestone` | `/milestone <N> <M>` | Toggle milestone M done/undone on project N |

**`/projects personal` integration:**
The existing `cmd_projects` handler already accepts a category filter. It
currently only shows `category: code` files. Extend it to return
`project-personal-*.md` files when `personal` is the filter (or when no
filter is given, show both with a visual separator).

**`/project N` detail format** (personal):
```
Build home office  [personal project]
Status: active  Priority: medium  Due: Jul 1, 2026
Tags: home, productivity

Set up a dedicated home office with standing desk and proper lighting.

Milestones:
  ✅ Cable management
  ⬜ Order standing desk

Linked goal: none
```

**`/addmilestone N <text>`** appends `{text: <text>, done: false}` to the
project's `milestones` list and rewrites the file atomically.

**`/milestone N M`** toggles `milestones[M-1].done` (1-indexed to match how
the detail view numbers them).

**Validation criteria:**
- `/addmilestone` on a non-personal project (e.g., a code project) returns
  "Milestones are only supported on personal projects"
- `/milestone N M` where M is out of range returns a bounds error
- Concurrent milestone toggle (rapid double-tap) is safe because each toggle
  reads-then-rewrites the full file atomically

---

### FR-5: Goal→Project Linking

A personal project may reference one parent goal via `linked_goal:` in its
frontmatter. A goal's `linked_projects:` list is the inverse.

**`/linkgoal <project_N> <goal_M>`:**
- Looks up project N from `_last_project_set` (or `_last_goal_set` for M)
- Writes `linked_goal: <goal_filename>` to the project file
- Appends the project filename to `linked_projects:` in the goal file
- Confirms: "Linked 'Build home office' → goal 'Run a 5K'"

**`/unlinkgoal <project_N>`:**
- Clears `linked_goal:` on the project
- Removes the project filename from the goal's `linked_projects:` list

**Validation criteria:**
- Linking a project that already has a `linked_goal:` prompts "Project already
  linked to '<existing_goal>' — replace? Reply /linkgoal confirm"
- Both file writes succeed or neither (write project first, then goal; if goal
  write fails, revert project)

---

### FR-6: Deadline Notifications

Add `_check_goal_alerts()` and `_check_project_alerts()` methods to
`notification_manager.py`, following the existing `_check_commitment_alerts()`
pattern (lines 397–479).

**Alert trigger:** `due_date` is exactly 7 days or 1 day from today (checked
at the daily 09:00 briefing window). One alert per (file, horizon) pair per
day — deduplicated via `notification-state.json`.

**Alert format:**
```
⏰ Goal deadline approaching:
Run a 5K — due in 7 days (Jun 30)
```
```
🚀 Project deadline tomorrow:
Build home office — due tomorrow (Jul 1)
```

**State tracking:** Extend `notification-state.json` with a
`"goal_alerts_sent"` dict: `{"{filename}:{horizon_days}:{date}": true}`.

**Validation criteria:**
- Alert fires on the 7-day and 1-day horizons only (not every day)
- Completed and abandoned items never generate alerts
- Items with no `due_date` never generate alerts
- Dedup key includes the date so alerts reset the following year for repeating
  deadlines set with future dates

---

### FR-7: Context Loading for Chat Queries

Active goals and personal projects should be included in the LLM's memory
context when the user asks about plans, goals, or current work.

**Change to `chat_handler.py` context loading:**
When loading relevant memory files for a query, include all `type: goal` and
`type: project, category: personal` files with `status: active` unconditionally
(they are always relevant to planning queries) — up to a cap of 5 each, sorted
by `due_date` ascending (soonest first, null last).

This is separate from the keyword-relevance scoring that gates other memory
types. Goals and active personal projects are always pulled into context; the
LLM decides whether they're relevant to the specific query.

**Validation criteria:**
- A query "what should I focus on this week?" includes active goals in the
  LLM context
- Cap of 5 goals + 5 personal projects prevents context overrun
- Completed/abandoned items are excluded

---

## Config

```yaml
# config.yaml additions
goals:
  enabled: true
  deadline_horizons: [7, 1]   # days before due_date to send alert
  max_context_items: 5        # max goals + personal projects each loaded into chat context
```

---

## Files to Create/Modify

| File | Change |
|---|---|
| `goals_tracker.py` | New module: `GoalManager` class. CRUD helpers for `type: goal` files: `create_goal()`, `list_goals()`, `update_status()`. No async loop — pure read/write helpers called from `chat_handler.py`. |
| `chat_handler.py` | Add `cmd_addgoal`, `cmd_goals`, `cmd_goal`, `cmd_completegoal`, `cmd_abandongoal`, `cmd_addproject`, `cmd_completeproject`, `cmd_abandonproject`, `cmd_holdproject`, `cmd_addmilestone`, `cmd_milestone`, `cmd_linkgoal`, `cmd_unlinkgoal`. Update `cmd_projects` to include personal category. Extend context loading to include active goals/personal projects. Update `COMMAND_REGISTRY`. |
| `notification_manager.py` | Add `_check_goal_alerts()` and `_check_project_alerts()`. Call both from `run_notification_loop()`. Extend `notification-state.json` schema. |
| `CLAUDE.md` | Add `goals_tracker.py` to Code Structure section. |
| `README.md` | Document `/addgoal`, `/goals`, `/addproject`, milestone commands, and `/projects personal`. |
| `tests/unit/test_goals_tracker.py` | Unit tests for `GoalManager`: create, list, status update, duplicate prevention, invalid date rejection. |
| `tests/unit/test_chat_handler.py` | Tests for `cmd_addgoal`, `cmd_completegoal`, `cmd_addmilestone`, `cmd_milestone`, milestone out-of-bounds. |
| `tests/integration/test_goals_integration.py` | Round-trip: addgoal → goals list → completegoal → goals list shows empty. |

---

## Unit Tests

| Test | Assertion |
|---|---|
| `test_create_goal_writes_file` | `GoalManager.create_goal("Run 5K")` writes `goal-run-a-5k-*.md` with `type: goal`, `status: active` |
| `test_create_goal_deduplication` | Creating same title twice returns "already exists" without writing a second file |
| `test_create_goal_with_due_date` | `by 2026-06-30` suffix parsed and stored as `due_date: '2026-06-30'` |
| `test_create_goal_rejects_past_date` | `by 2020-01-01` raises `ValueError` or returns warning |
| `test_list_goals_active_only` | Completed/abandoned files excluded from default list |
| `test_list_goals_status_filter` | `/goals completed` returns only completed files |
| `test_complete_goal_updates_status` | `update_status(path, "completed")` rewrites file with `status: completed` atomically |
| `test_complete_goal_already_done` | Re-completing returns "already completed" without rewriting |
| `test_addmilestone_appends` | `/addmilestone 1 "Order desk"` adds `{text: "Order desk", done: false}` to milestones list |
| `test_milestone_toggle` | `/milestone 1 2` flips `milestones[1].done` from false→true and back |
| `test_milestone_out_of_bounds` | `/milestone 1 99` returns bounds error |
| `test_goal_project_link` | `/linkgoal 1 1` sets `linked_goal:` on project, appends project to `linked_projects:` on goal |
| `test_notification_goal_7day_alert` | `due_date` = today + 7d → alert sent; `due_date` = today + 6d → no alert |
| `test_notification_no_alert_for_completed` | `status: completed` goal with due_date tomorrow → no alert |
| `test_notification_dedup` | Alert for same (file, horizon, date) tuple not sent twice |

---

## Changelog

| Version | Date | Notes |
|---|---|---|
| 1.0.0 | 2026-04-14 | Initial draft |
