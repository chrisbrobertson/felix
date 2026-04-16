# Feature Spec: Goal/Project Agent

**Status:** Implemented  
**Date:** 2026-04-16  
**Loop:** 14th async loop (full role only)

## Overview

The Goal/Project Agent is an autonomous assistant that monitors active goals and projects, discovers related memories, generates reports on progress/blockers, and proposes concrete actions. It operates as the 14th daemon loop, running every 6 hours on full-role nodes.

## Components

### 1. Core Module: `goal_project_agent.py`

**Class:** `GoalProjectAgent`

**Configuration (`_agent_config()`):**
- `enabled` (default: true) — master kill-switch
- `scan_interval_min` (default: 360) — minutes between scans
- `max_items_per_cycle` (default: 0 = unlimited) — cap items processed per scan
- `max_memories_per_item` (default: 20) — cap related memories sent to LLM
- `min_confidence` (default: 0.6) — minimum confidence for action proposals
- `stale_threshold_days` (default: 14) — emit low-urgency report when no activity
- `urgent_cooldown_hours` (default: 24) — rate-limit urgent pings
- `include_on_hold_projects` (default: false) — whether to process on-hold projects

**State (`goal-agent-state.json`):**
```json
{
  "goals": {
    "goal-work-launch-abc123.md": {
      "last_checked": "2026-04-16T10:00:00",
      "last_report_hash": "abc123def456",
      "last_report": "Q2 launch progressing...",
      "last_urgent_ping": "2026-04-15T09:00:00"
    }
  },
  "projects": {...}
}
```

**FR-1: Item Selection (`_select_items()`):**
- Walk `MEMORIES_DIR` for `goal-*.md` and `project-*.md`
- Include if:
  - `type: goal` with `status: active`
  - `type: project` with `status: active` (or `on-hold` if config allows)
  - `category != "code"` (exclude code repos)
  - `agent: false` not set in frontmatter
- Return list of `(path, fm_dict)` tuples

**FR-2: Related Memory Discovery (`_find_related_memories()`):**
- Sources (union):
  1. `inferred_from` frontmatter field (list of filenames)
  2. Tag overlap — any memory with tags intersecting item's tags
  3. Title Jaccard ≥ 0.3
  4. Participant overlap — notes field mentions names in memory's `participants`
  5. Recency filter — `mtime > last_checked`
- Cap at `max_memories_per_item`, sorted by mtime descending
- Return list of `(path, fm_dict)` tuples (headers only, first 500 bytes)

**FR-3: LLM Report Generation (`_generate_report()`):**
- Prompt template includes:
  - Goal/project metadata (title, type, category, status, due date, linked items, notes)
  - Last checked timestamp
  - Count of pending actions already proposed
  - List of related memories (filename, date, title, summary snippet)
  - Allowed action types with signatures
- Returns JSON:
  ```json
  {
    "has_update": true,
    "urgency": "low|medium|high",
    "report": "Brief summary...",
    "actions": [
      {
        "action_type": "add_milestone",
        "target": "project-work-launch-abc123.md",
        "args": {"text": "Complete API testing"},
        "confidence": 0.85,
        "rationale": "Email from Sarah on 2026-04-15...",
        "evidence": ["email-thread-launch-abc123.md"]
      }
    ],
    "evidence": ["email-thread-launch-abc123.md"]
  }
  ```
- Filters actions by `confidence >= min_confidence`
- Validates urgency (high requires len(evidence) ≥ 1)
- Deduplicates by report hash (stored in state)

**FR-4: Action File Writing (`_write_action()`):**
- Filename: `action-{source-slug}-{action-id}.md`
- `action_id` = sha1(f"{item_path.name}:{action_type}:{rationale}")[:6]
- Skip if file already exists (dedup)
- Frontmatter:
  ```yaml
  type: agent_action
  action_id: abc123
  action_type: add_milestone
  status: pending
  target: project-work-launch-abc123.md
  args: {text: "Complete API testing"}
  confidence: 0.85
  rationale: "Email from Sarah mentioned..."
  evidence: [email-thread-launch-abc123.md]
  proposed_at: "2026-04-16T10:00:00"
  approved_at: null
  executed_at: null
  source_goal: goal-work-launch-abc123.md
  ```

**FR-5: Action Execution (`_execute_action()`):**
Dispatch by `action_type`:
- `add_milestone` → `GoalManager.add_milestone(target_path, args["text"])`
- `update_status` → `GoalManager.update_goal_status()` or `update_project_status()`
- `update_due_date` → Atomic YAML rewrite of `due_date` field
- `add_note` → Append timestamped note to `notes` field
- `create_commitment` → `CommitmentTracker._write_commitment()`
- `complete_commitment` → `CommitmentTracker.update_commitment_status(target, "completed")`

Precondition failures → mark action `status: superseded`

**FR-6: Auto-Supersede Check (`_check_superseded_actions()`):**
Run at start of each `_scan()`:
- For each `action-*.md` with `status: pending`:
  - Source goal/project no longer exists → mark superseded
  - `add_milestone`: milestone text already in target → mark superseded
  - `update_status`: target already has proposed status → mark superseded

**FR-7: Process Item (`_process_item()`):**
1. Get/create state entry for this item
2. Find related memories (with recency filter based on `last_checked`)
3. Staleness check: if no related memories AND item older than `stale_threshold_days` → synthesize low-urgency report
4. Call `_generate_report()` if not already synthesized
5. Write action files for each proposed action
6. Send urgent ping if `urgency == "high"` and cooldown clear
7. Update state: `last_checked`, `last_report_hash`, `last_report`, optionally `last_urgent_ping`

**FR-8: Main Scan Loop (`_scan()`):**
1. Prune state for deleted files
2. Check superseded actions
3. Select items
4. Process each item (with exception handling)

**FR-9: Run Loop (`run_loop()`):**
- Role guard: return if `role != "full"`
- Config guard: return if `enabled: false`
- Interval: `scan_interval_min` (default 360 min = 6 hours)
- Exception handling: log and continue

### 2. Skill File: `skills/goal-update.md`

Frontmatter:
```yaml
name: goal-update
version: "1.0"
preferred_model: claude-sonnet-4-6
fallback_model: claude-haiku-4-5-20251001
success_rate: null
total_runs: 0
```

Instructions describe JSON output format and quality standards.

### 3. Daemon Wiring: `daemon.py`

```python
from goal_project_agent import GoalProjectAgent

goal_agent = GoalProjectAgent(role=role)
goal_agent.notification_callback = notification_mgr.send_message

tasks += [goal_agent.run_loop]
```

### 4. Chat Handler: `chat_handler.py`

**COMMAND_REGISTRY:**
```python
"Agent actions": [
    ("actions", "List pending agent-proposed actions (filter: approved, all)"),
    ("action",  "Show full detail for action N"),
    ("run",     "Approve and execute action N"),
    ("drop",    "Reject action N"),
    ("defer",   "Snooze action N for N hours (default 24)"),
],
```

**Session State:**
```python
self._last_action_set: list = []
```

**Helper (`_load_action_set()`):**
- Glob `action-*.md` from `MEMORIES_DIR`
- Parse frontmatter
- Default filter: `status == "pending"` AND (no `defer_until` OR `defer_until <= now`)
- Other filters: `"approved"`, `"all"`, or specific status
- Sort by `proposed_at` descending
- Store in `self._last_action_set`

**Commands:**
- `/actions [filter]` — list numbered actions with type, target, rationale snippet
- `/action N` — show full detail (type, target, args, confidence, rationale, evidence, proposed_at, source_goal, defer_until)
- `/run N` — approve and execute (call `GoalProjectAgent._execute_action()`, mark `status: executed`, handle precondition failures)
- `/drop N` — mark `status: rejected`, append to `rejected-actions.json`
- `/defer N [hours]` — set `defer_until` timestamp (default 24h)

### 5. Notification Manager: `notification_manager.py`

**Briefing Section 1 (Agent Proposals):**
After overdue commitments, before new memory count:
- Load pending actions from last 24h
- Format: `• [{action_type}] {rationale[:80]}`
- Footer: `→ /actions to review, /run N to approve`

**Briefing Section 2 (Goal/Project Updates):**
After agent proposals:
- Load `goal-agent-state.json`
- For each item with `last_checked` in last 24h AND `last_report` non-empty:
  - Format: `• {item_name}: {last_report[:80]}`

## Testing

28 tests total:

**Unit (`test_goal_project_agent.py`):**
1. `test_scan_skips_watcher_role` — role != "full" → no-op
2. `test_scan_skips_disabled_config` — enabled: false → no-op
3. `test_scan_respects_agent_false_frontmatter` — agent: false → skipped
4. `test_scan_skips_completed_goals` — status: completed → skipped
5. `test_related_memories_seed_from_inferred_from` — inferred_from list
6. `test_related_memories_tag_overlap` — tag intersection
7. `test_related_memories_title_jaccard_threshold` — similarity ≥ 0.3
8. `test_related_memories_recency_filter` — mtime > last_checked
9. `test_related_memories_max_cap` — capped at max_memories_per_item
10. `test_llm_dedup_by_report_hash` — same hash → skip
11. `test_llm_confidence_filter` — confidence < min → skip
12. `test_staleness_emits_low_urgency_report` — no related + age ≥ threshold
13. `test_action_file_written_atomically` — tmp + rename
14. `test_urgent_ping_rate_limited` — cooldown enforced
15. `test_state_file_persisted_and_loaded` — atomic write + read
16. `test_archive_superseded_action` — precondition fail → superseded

**Unit (`test_chat_handler.py`):**
17. `test_cmd_actions_lists_pending` — default filter
18. `test_cmd_actions_filter_approved` — filter=approved
19. `test_cmd_action_detail_shows_rationale_evidence` — full detail view
20. `test_cmd_run_approves_and_executes` — status → executed
21. `test_cmd_run_superseded_on_precondition_fail` — error → superseded
22. `test_cmd_drop_marks_rejected_and_logs` — status → rejected, append to JSON
23. `test_cmd_defer_sets_defer_until_and_hides_from_default_list` — defer filter
24. `test_command_registry_parity_still_holds` — auto-covered by existing test

**Integration (`test_goal_agent_flow.py`):**
25. `test_end_to_end_happy_path` — select → find → generate → write → execute
26. `test_end_to_end_rejected_action_logged` — /drop → rejected-actions.json
27. `test_briefing_includes_pending_actions_section` — notification_manager integration
28. `test_briefing_includes_goal_update_section` — state.json integration

## Files

**Created:**
- `goal_project_agent.py`
- `skills/goal-update.md`
- `specs/feat-goal-project-agent.md`
- `tests/unit/test_goal_project_agent.py`
- `tests/integration/test_goal_agent_flow.py`

**Modified:**
- `daemon.py` — wiring
- `chat_handler.py` — COMMAND_REGISTRY, session state, handlers
- `notification_manager.py` — briefing sections
- `CLAUDE.md` — loop 14 entry, deploy dir, role count
- `README.md` — (to be updated with user-facing docs)

## Config Schema

```yaml
goal_agent:
  enabled: true
  scan_interval_min: 360
  max_items_per_cycle: 0  # 0 = unlimited
  max_memories_per_item: 20
  min_confidence: 0.6
  stale_threshold_days: 14
  urgent_cooldown_hours: 24
  include_on_hold_projects: false
```

## Allowed Action Types

1. **add_milestone** — Append milestone to project
   - Target: `project-*.md`
   - Args: `{text: str}`
   
2. **update_status** — Change goal/project status
   - Target: `goal-*.md` or `project-*.md`
   - Args: `{status: "completed"|"abandoned"|"on-hold"|"active"}`
   
3. **update_due_date** — Set new deadline
   - Target: `goal-*.md` or `project-*.md`
   - Args: `{due_date: "YYYY-MM-DD"}`
   
4. **add_note** — Append timestamped note
   - Target: `goal-*.md` or `project-*.md`
   - Args: `{text: str}`
   
5. **create_commitment** — Write new commitment file
   - Target: null
   - Args: `{description, due_date, owner, recipient, commitment_type}`
   
6. **complete_commitment** — Mark existing commitment done
   - Target: `commitment-*.md`
   - Args: `{}`

## State Files

**`goal-agent-state.json`:**
```json
{
  "goals": {
    "goal-{slug}-{id}.md": {
      "last_checked": "ISO datetime",
      "last_report_hash": "12-char sha1",
      "last_report": "Brief summary text",
      "last_urgent_ping": "ISO datetime"
    }
  },
  "projects": {...}
}
```

**`rejected-actions.json`:**
```json
{
  "rejected": [
    {
      "action_id": "abc123",
      "action_type": "add_milestone",
      "rationale": "Email from Sarah...",
      "rejected_at": "ISO datetime"
    }
  ]
}
```

## Telegram Command Flow

```
User: /actions
Bot: Agent-proposed actions (5):
     1. [add_milestone] project-work-launch-abc123.md — Complete API testing
     2. [update_status] goal-work-launch-abc123.md — Mark as on-hold
     ...
     Use /action N for details, /run N to approve and execute.

User: /action 1
Bot: Action 1:
     Type: add_milestone
     Target: project-work-launch-abc123.md
     Args: {text: "Complete API testing"}
     Confidence: 0.85
     Rationale: Email from Sarah on 2026-04-15 mentioned testing is the next blocker
     Evidence: email-thread-launch-planning-abc123.md
     Proposed: 2026-04-16T10:00:00
     Source: goal-work-launch-abc123.md
     
     Use /run 1 to approve and execute, /drop 1 to reject, /defer 1 [hours] to snooze.

User: /run 1
Bot: ✓ Action 1 executed: Added milestone to project-work-launch-abc123.md

User: /defer 2 48
Bot: Action 2 snoozed for 48h (until 2026-04-18 10:00)

User: /drop 3
Bot: ✗ Action 3 rejected.
```

## Edge Cases

1. **Source goal deleted** — auto-supersede pending actions
2. **Milestone already exists** — auto-supersede add_milestone
3. **Status already set** — auto-supersede update_status
4. **Urgent ping spam** — 24h cooldown per item
5. **Deferred actions** — hidden from default /actions list
6. **Report hash unchanged** — skip (no new update)
7. **No related memories + stale** — synthesize low-urgency report
8. **Precondition fail on /run** — mark superseded
9. **agent: false in frontmatter** — respect opt-out
10. **On-hold projects** — only processed if config allows
