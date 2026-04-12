---
specmas: 3.0
kind: feature
id: feat-skill-optimizer
version: 1.1.0
created: 2026-04-11
updated: 2026-04-12
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
---

# Skill Optimizer

## Overview

### Problem Statement

The daemon learns from every page it summarizes, but the prompt templates it uses
never improve. After 1,000 runs of `summarize-webpage`, any systematic failure mode
(missing entities, tags too generic, summaries that exceed the 2 KB target) persists
indefinitely. The execution history table records evidence of these failures, but
nothing reads it.

The Skill Optimizer closes this loop: once daily it scores pending executions,
identifies failure patterns in low-scoring runs, rewrites the skill's Instructions
section to address them, and maintains a versioned backup trail for rollback.

### Design Principles

Three patterns were evaluated and selected from the prompt optimization literature
(DSPy, OPRO, TextGrad/ProTeGi, PromptBreeder, Constitutional AI):

| Pattern | Borrowed Idea | Implementation Cost |
|---------|--------------|---------------------|
| **TextGrad/ProTeGi** | Two-call Critique-then-Edit: diagnose failure patterns first, then rewrite | +1 LLM call per optimized skill |
| **OPRO** | Show the optimizer its own past rewrites + resulting scores (the Evolution Log trajectory) | Zero — prompt construction |
| **DSPy** | Auto-inject top-scoring execution traces as few-shot exemplars in the Instructions | Zero — file manipulation |

**Excluded:** PromptBreeder (candidate dry-runs too costly), Constitutional AI
(cumulative Principles list adds maintenance overhead; the critique captures the same
information per-cycle), ReAct (runtime execution pattern, not applicable to batch
optimization).

### Scope

**In Scope:**
- Fourth async daemon loop, running once daily at configured hour (`full` role only)
- LLM-as-judge scoring of `pending` execution history rows
- Critique-then-Edit optimization of underperforming skills
- Rolling backup files (`.1` through `.N`) with automatic regression rollback
- Watcher-node execution log merging via iCloud-synced JSONL files
- Auto-exemplar injection (top-N scoring traces as few-shot examples)
- Execution history pruning to bound file growth
- Dry-run mode for safe testing

**Out of Scope:**
- Multi-objective optimization (accuracy vs. latency vs. cost)
- A/B testing of candidate rewrites before committing (PromptBreeder pattern)
- Cross-skill learning (each skill optimized independently)
- Automatic skill creation (the optimizer only improves existing skills)
- Online learning or reinforcement learning approaches (RLHF)

### Success Metrics

- Optimizer runs to completion nightly without crashing
- Pending execution rows scored within the daily run
- Optimized skills show measurable score improvement within 10 subsequent runs
- No skill regression persists beyond one optimization cycle (rollback fires correctly)
- No skill file corruption (atomic writes, backup before every rewrite)

---

## Functional Requirements

### FR-1: Scheduled Daily Run

The optimizer must run once per day at the configured hour, not on a fixed 1-hour
poll interval (the current stub's behaviour).

**Mechanism:**
- On each `run_loop` iteration, calculate seconds until the next occurrence of
  `run_hour` (accounting for whether it has already passed today)
- Sleep exactly that duration using `asyncio.wait_for(stop_event.wait(), timeout=seconds)`
- After waking, run the full optimization pass, then loop back to calculate the next
  scheduled wake time

**Scheduling logic (pseudocode):**
```python
now = datetime.now()
next_run = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)
if now >= next_run:
    next_run += timedelta(days=1)
sleep_seconds = (next_run - now).total_seconds()
```

**Config:** `skill_optimizer.run_hour` (default: `3`)

**Validation criteria:**
- Optimizer runs within 60 seconds of the scheduled hour
- Stopping the daemon mid-sleep does not cause a crash or missed next run
- If the daemon starts after the scheduled hour, the next run is the following day

---

### FR-2: Watcher Node Execution Log Merging

Watcher nodes write execution records locally (to avoid iCloud sync conflicts on
simultaneous writes to the shared skill file). The full node must ingest these records
and append them to the appropriate skill's `## Execution History` table before scoring.

**Watcher log format** (JSONL, one record per line):
```json
{"date": "2026-04-14", "skill": "summarize-webpage", "input_slug": "ai-news-abc12",
 "model": "gemini/gemini-2.0-flash", "score": "pending", "notes": "",
 "hostname": "macbook-pro"}
```

**Watcher log path** (written by `skill_executor.py` on watcher nodes):
```
BRAIN_DIR/logs/{hostname}-execution-log.jsonl
```

This path is in iCloud (`BRAIN_DIR`), not the local deploy dir, so the file syncs
to the full node automatically. Note: this changes the watcher log location from
the current `DEPLOY_DIR/execution-log.jsonl` — see Files section.

**Merge procedure (`_merge_watcher_logs`):**
1. Glob `BRAIN_DIR / "logs" / "*-execution-log.jsonl"`
2. For each file, read all JSONL records
3. Group records by `skill` name
4. For each skill, append records as pipe-delimited rows to that skill file's
   `## Execution History` table (using the same append logic as `skill_executor.py`)
5. Rename processed file to `{hostname}-execution-log.processed-{date}.jsonl`
   to prevent double-ingestion; delete processed files older than 30 days
6. Log count of merged records per skill

**Validation criteria:**
- Merged records appear in the skill file's Execution History table
- No double-ingestion on subsequent runs (processed files renamed)
- Missing or malformed JSONL lines skipped with WARNING, not crashed
- Works with zero watcher log files (no-op, not an error)

---

### FR-3: LLM-as-Judge Scoring of Pending Runs

Before deciding whether to optimize a skill, score all execution history rows where
`score` is `pending`. Scoring happens in a batch at the start of the daily run.

**Judge model:** `judge` route (Claude Haiku 4.5) — fast and cheap while remaining
in the Anthropic family for consistent instruction-following. Configurable via
`judge_model` config key.

**Judge prompt per execution row:**
```
You are evaluating the quality of a skill output.

Skill: {skill_name}
Skill instructions (what a good output should look like):
{skill_instructions}

Execution date: {date}
Input: {input_slug}
Output:
{output_text}

Rate this output on a scale of 0.0 to 1.0 using this rubric:
- 1.0: Excellent — fully satisfies all instructions, well-structured, complete
- 0.7: Acceptable — minor gaps or format issues, still useful
- 0.5: Weak — missing key content or poorly structured
- 0.0: Failed — junk, empty, or seriously wrong output

Respond with JSON only:
{"score": 0.0, "reasoning": "one sentence explanation"}
```

**Sourcing output text for scoring:**
The execution history row stores only an `input_slug` (first 20 chars of the input,
not the full output). The optimizer must locate the actual output from the memory
file written by that execution. Memory files are matched by `input_slug` prefix
against the filename slug component. If no match is found, the row is left as
`pending` and a WARNING is logged (the memory file may have been deleted).

**Scoring output:**
- Parse JSON response; on parse failure, log WARNING and skip row
- Write score and truncated reasoning (max 120 chars) back into the execution history
  row, replacing `pending`
- Atomic write for the skill file update

**Frontmatter update:**
After scoring, recalculate and write `success_rate` (mean of all non-zero numeric
scores) and `total_runs` (count of numeric score rows) to the skill's frontmatter.

**Validation criteria:**
- All `pending` rows scored within one daily run
- `success_rate` and `total_runs` in frontmatter reflect current execution history
- Judge failures (parse error, no matching memory file) leave the row as `pending`
  and emit a WARNING — they do not halt the pass
- Scoring loop respects `stop_event` (checks between rows)

---

### FR-4: Optimization Gates

Before optimizing a skill, check three conditions. Proceed only if all pass.

| Gate | Condition | Config key |
|------|-----------|------------|
| Minimum runs | At least N numeric scores in execution history | `min_runs_before_optimize` (default: 10) |
| Underperforming | `success_rate` < threshold | `underperformance_threshold` (default: 0.70) |
| Not excellent | `success_rate` < ceiling | `skip_above_threshold` (default: 0.90) |

**Additional gate:** If the number of runs since `last_optimized` is fewer than
`min_runs_before_optimize`, skip (not enough new data since the last rewrite).

**`skill-optimizer.md` itself is always skipped** — the meta-skill is managed manually.

**Logging:**
- Gate pass/fail logged at INFO level for each skill, with the reason
- "Skipped (success_rate=0.87, above skip_above_threshold=0.90)" is a useful log

**Validation criteria:**
- Skills with fewer than `min_runs_before_optimize` numeric scores are not optimized
- Skills with `success_rate >= skip_above_threshold` are not optimized
- `skill-optimizer.md` is never optimized
- Log output shows which gate triggered the skip

---

### FR-5: Critique Generation

First of the two LLM calls per optimized skill. The critique identifies failure
patterns from low-scoring runs and proposes what should change.

**Input to the critique call:**
- Current skill `## Instructions` text
- Evolution Log (all prior versions — OPRO trajectory pattern)
- Low-scoring execution rows (score < `underperformance_threshold`), including the
  associated output text for each (looked up from memory files)
- High-scoring rows for contrast (top 5 by score)

**Model:** `optimizer` route (Claude Sonnet)

**System prompt for the critique call:**
```
You are analyzing a prompt template to identify why some executions score poorly.

Your output must be a JSON object:
{
  "failure_patterns": ["pattern 1", "pattern 2"],
  "root_cause": "one sentence summary of the core issue",
  "suggested_focus": "what the rewrite should specifically address"
}

Be specific. Cite evidence from the execution examples. Avoid generic observations.
```

**Validation criteria:**
- Returns valid JSON with the three required keys
- `failure_patterns` is a non-empty list (at least one pattern identified)
- JSON parse failure logged at WARNING, optimization of this skill skipped
- Critique stored in the Evolution Log entry (see FR-10)

---

### FR-6: Instruction Rewrite

Second LLM call per optimized skill. Uses the critique from FR-5 plus the evolution
trajectory to rewrite only the `## Instructions` section.

**Input to the rewrite call:**
- System prompt: the `skill-optimizer.md` Instructions (the meta-skill)
- Current skill file content (full, including Evolution Log for OPRO trajectory)
- Critique JSON from FR-5

**Model:** `optimizer` route (Claude Sonnet)

**The meta-skill (`skill-optimizer.md` Instructions, to be updated — see FR-13)
must instruct the LLM to:**
- Output the complete updated skill file (frontmatter + all sections)
- Change ONLY the `## Instructions` section
- Append a new Evolution Log entry (see FR-10 format)
- Do NOT modify Execution History, Top Examples, or frontmatter (except `version` bump)
- Be conservative: only change what the evidence directly supports

**Post-processing:**
- Extract the `## Instructions` section from the LLM response
- Verify it is non-empty and differs from the current version
- If identical, log INFO ("No change proposed — skipping write") and abort

**Validation criteria:**
- Only the Instructions section changes in the written file
- Frontmatter `version` is incremented by 1
- Rewrite that produces identical Instructions is a no-op (no file write, no backup)
- LLM returning a malformed skill file structure (missing sections) → WARNING, no write

---

### FR-7: Auto-Exemplar Selection

Inject the top-scoring execution traces as few-shot examples in the skill file.
This implements the DSPy bootstrapping pattern: the LLM sees concrete examples of
its best past outputs, improving consistency.

**Eligibility:** Only skills with `exemplar_eligible: true` in frontmatter.
Set manually per skill. Default: `false`.

**Selection:** Top N executions by score (ties broken by most recent), where N =
`max_exemplars` (default: 2). Requires the output text to be locatable in a
memory file.

**Format** (written to `## Top Examples` section, created if absent):
```markdown
## Top Examples
<!-- Auto-managed by optimizer. Do not edit manually. -->
### Example 1 (score: 0.95, 2026-04-13)
**Input:** url=https://example.com, title=Example Article
**Output:**
## Summary
Two to three sentence summary of the example article...

**Key Points**
- Point one
...
```

**Rules:**
- Section is fully replaced on each optimizer run (not appended)
- If fewer than 2 examples are available with score ≥ 0.70, the section is omitted
- Examples appear immediately after `## Instructions` and before `## Evolution Log`

**Validation criteria:**
- `## Top Examples` section present only for skills with `exemplar_eligible: true`
- Section contains exactly `max_exemplars` examples (or fewer if insufficient data)
- Section is replaced, not appended, on each optimization pass
- No exemplars added if all scores are below 0.70

---

### FR-8: Rolling Skill File Backups and Regression Rollback

**Backup rotation** (logrotate-style) before every rewrite:

1. Delete `{skill}.md.{max_skill_backups}` if it exists
2. For N from `max_skill_backups - 1` down to 1: rename `{skill}.md.{N}` → `{skill}.md.{N+1}`
3. Copy current `{skill}.md` → `{skill}.md.1`

`{skill}.md.1` is always the most recent backup; `{skill}.md.{max_skill_backups}` is
the oldest. Backups live in the same iCloud `skills/` directory as the skill file.

**Config:** `max_skill_backups` (default: 5)

**Regression detection:**
- After a rewrite, store the pre-optimization `success_rate` as `prev_version_avg_score`
  in the skill's frontmatter
- On the next optimization pass, before writing any new rewrite:
  - If current `success_rate` < `prev_version_avg_score - regression_tolerance`, the
    current version has regressed
  - Rollback: copy `{skill}.md.1` over `{skill}.md` (restoring the prior version),
    reverse-rotate backups (`.2` → `.1`, `.3` → `.2`, etc.), log rollback in Evolution Log
  - Then re-run optimization gates — the rollback may have restored a version that
    now passes the gates and should be re-optimized with the new data

**Config:** `regression_tolerance` (default: 0.05)

**Validation criteria:**
- `{skill}.md.1` exists after every successful rewrite
- Backup count never exceeds `max_skill_backups`
- Rollback restores byte-identical content to the `.1` backup
- Rollback event logged in Evolution Log with pre/post scores
- If `.1` is missing (first run ever), rollback logs WARNING and skips (no crash)

---

### FR-9: Execution History Pruning

Prevent execution history tables from growing unboundedly. On each optimization
pass, after scoring and before rewriting, prune each skill's history to the N most
recent rows.

**Config:** `max_history_rows` (default: 100)

**Rules:**
- Keep the newest `max_history_rows` rows (most recently appended, by position in file)
- Never delete rows where `score = pending` (they haven't been scored yet and would
  be lost before they contribute to the success rate)
- The header row (`| date | input_slug | ...`) is always preserved
- Pruning is atomic (temp file + rename)

**Validation criteria:**
- Execution History table never exceeds `max_history_rows + pending_count` rows
- Pending rows are never deleted by pruning
- Pruning does not trigger if row count is within limit (no unnecessary write)

---

### FR-10: Evolution Log Maintenance

Each optimization pass appends a structured entry to `## Evolution Log`.

**Entry format:**

```markdown
### v3 (2026-04-15) — {one-line description of the change}
**Critique:** {root_cause from the critique JSON, ≤ 100 chars}
**Failure patterns:** {comma-joined failure_patterns list}
**Change:** {one sentence describing what was changed in the Instructions}
**Pre-optimization avg:** {prev_version_avg_score} | **Post (projected):** pending
```

On the *following* pass, if the skill was not rolled back, update the entry's
`Post (projected)` with the actual new `success_rate`.

**Rollback entry format:**
```markdown
### v3 → v2 rollback (2026-04-22)
**Reason:** success_rate dropped from 0.71 to 0.63 (regression_tolerance=0.05)
**Action:** Restored from summarize-webpage.md.1
```

**Validation criteria:**
- New entry appended (not prepended) after the most recent entry
- Entry contains all required fields
- Version number in entry matches frontmatter `version`

---

### FR-11: Atomic File Writes

All skill file mutations must use temp-file + `os.rename()` to prevent partial writes
from corrupting the skill file (which would break all executions using that skill).

**Pattern (consistent with `memory_writer.py` and `zoom_scanner.py`):**
```python
tmp = skill_path.with_suffix(".tmp")
tmp.write_text(new_content)
os.rename(tmp, skill_path)
```

**Scope:** Applies to all write operations: scoring updates, exemplar section updates,
full rewrites, backup copies, watcher log truncation.

**Validation criteria:**
- No `.tmp` files left after a successful write
- Interrupted write (test by raising exception between write and rename) leaves
  `.tmp` file, not corrupted `.md` file

---

### FR-12: Dry-Run Mode

When `dry_run: true` in config, the optimizer logs all proposed changes without
writing any files. Useful for verifying the optimizer's behaviour before the first
real run.

**Dry-run output:**
- Log at INFO: "DRY RUN: Would score N pending rows for {skill}"
- Log at INFO: "DRY RUN: Would optimize {skill} — critique: {root_cause}"
- Log at INFO: "DRY RUN: Would backup {skill}.md → {skill}.md.1"
- Log at INFO: "DRY RUN: Proposed new Instructions: {first 200 chars}..."

**Validation criteria:**
- No files written when `dry_run: true`
- Log output is sufficient to understand what would have changed
- Config hot-reload: changing `dry_run` in `config.yaml` takes effect on the next
  daily run without daemon restart

---

### FR-13: `skill-optimizer.md` Meta-Skill Rewrite

The current `skill-optimizer.md` Instructions are a single-shot prompt ("identify
patterns, rewrite"). The Critique-then-Edit architecture requires two separate
calls with different prompts. The meta-skill drives the second call (the rewrite).

**Updated Instructions must:**
1. Expect as input: current skill file + structured critique JSON from the first call
2. Follow OPRO trajectory protocol: read the Evolution Log to understand prior attempts
3. Output the complete skill file with only `## Instructions` changed
4. Append a new Evolution Log entry in the FR-10 format
5. Increment `version` in frontmatter by 1
6. Be conservative: if the critique shows only one pattern, fix only that pattern
7. Preserve all other sections byte-identical

**Validation criteria:**
- Meta-skill Instructions reference the critique JSON input format
- Meta-skill Instructions reference the Evolution Log as context
- Output from the meta-skill is a valid, parseable skill file

---

### FR-14: Skill Executor Reload-on-Modify

`SkillExecutor._load()` caches skill instructions at `__init__` time. When the
optimizer rewrites a skill at 3 AM, running executor instances (held in memory by
the daemon) continue serving the old prompt until the daemon restarts.

**Fix:** Add a `_reload_if_modified()` check that compares `skill_path.stat().st_mtime`
against a stored timestamp before each `run()` call. If the file has been modified,
reload `self._skill`.

**Note:** This is recorded as the v0.2 backlog item in the main spec
(`second-brain-spec-v1.0.md` §Future Work). It belongs in `skill_executor.py`.

**Validation criteria:**
- `SkillExecutor.run()` uses the updated instructions after the optimizer writes a new version
- No daemon restart required to pick up the new prompt
- mtime check adds < 1 ms overhead per execution (one `stat()` call)

---

### FR-15: Heuristic Pre-filter (Real-time, Synchronous)

After every skill execution, run a fast synchronous quality check BEFORE writing
the memory file. No LLM call — pure heuristics on the output text.

**Heuristic rules:**
- **`too_short`**: Output length < 100 chars
- **`error_output`**: Output contains any of: "I cannot", "I'm unable", "Error:", 
  "Failed to", "I don't have access"
- **`unstructured`**: Output has no markdown structure (no `#`, no `-`, no `**`, 
  no numbered list) AND length < 300
- **`verbatim_copy`**: Output is >90% identical to the input (copy-paste, not 
  summarization). Quick char-level check: if `len(set(output_chars) - set(input_chars)) / len(output) < 0.15`

**Action on flag:**
- Append execution to an in-memory `_urgent_queue` list (managed by `SkillOptimizer` instance)
- Queue entry structure: `{skill_name, execution_timestamp, flag_reason, input_snippet (first 200 chars), output_snippet (first 200 chars)}`
- Do NOT suppress the memory write — still write the memory file (don't break the pipeline)

**Queue capping:**
- Cap the queue at 20 entries total
- Once capped, only add entries for skills already in the queue (don't add new skills)
- This prevents a bad URL from flooding the queue

**Validation criteria:**
- Flagged executions appear in `_urgent_queue` within 1 second of execution
- Memory file is still written after flag (pipeline not broken)
- Queue never exceeds 20 entries
- After cap, new skills are not added (only existing skills accumulate more entries)

---

### FR-16: Urgent Rewrite Queue

An in-memory list `_urgent_queue` on the `SkillOptimizer` instance, populated by FR-15.

**Structure per entry:**
```python
{
  "skill_name": str,
  "execution_timestamp": str,  # ISO format
  "flag_reason": str,          # too_short | error_output | unstructured | verbatim_copy
  "input_snippet": str,        # first 200 chars
  "output_snippet": str        # first 200 chars
}
```

**Capacity:** 20 entries total

**Validation criteria:**
- Queue is accessible to both the heuristic pre-filter (append) and the hourly processor (read/clear)
- Queue persists across hourly ticks (not cleared between runs)
- Entries are timestamped for later analysis

---

### FR-17: Hourly Queue Processor

`SkillOptimizer` gains a second async loop `run_urgent_loop()` that runs every 60 minutes
(not just nightly).

**Processing logic per tick:**
1. If `_urgent_queue` is empty: skip
2. Group queue entries by skill name
3. For skills with ≥2 flagged executions in the queue: trigger an immediate rewrite cycle
   (same path as the nightly rewrite, just scoped to those skills)
4. For skills with exactly 1 flagged execution: check if that same execution pattern 
   appeared in the last nightly run's history too — if so, trigger rewrite; otherwise wait
5. After processing, clear processed entries from `_urgent_queue`
6. Rewrite is capped at 3 skills per hourly tick to avoid LLM cost spikes

**Scheduling:**
- Sleep exactly 60 minutes using `asyncio.wait_for(stop_event.wait(), timeout=3600)`
- On stop event, terminate gracefully without waiting for the full hour
- First tick runs 60 minutes after `daemon.py` startup (not immediately)

**Validation criteria:**
- Hourly loop runs every 60 minutes ± 10 seconds
- Skills with ≥2 flagged executions are rewritten within one hourly tick
- No more than 3 skills rewritten per hourly tick (cost cap)
- Processed entries removed from queue after successful rewrite
- Loop respects `stop_event` (can be stopped mid-sleep)

---

### FR-18: Optional Per-execution Judge Call

Config flag `optimizer.realtime_judge: false` (default off). When enabled, after a
heuristic flag (FR-15), additionally call the `judge` model route with a short prompt
asking "Is this a good summary? Rate 1-5." on the flagged output.

**Judge prompt (real-time variant):**
```
You are evaluating the quality of a skill output.

Skill: {skill_name}
Output:
{output_text}

Rate this output on a scale of 1 to 5:
- 5: Excellent — well-structured, complete, useful
- 4: Good — minor issues, still useful
- 3: Acceptable — some problems but salvageable
- 2: Poor — missing key content or badly structured
- 1: Failed — junk, empty, or seriously wrong

Respond with JSON only:
{"score": 1, "reasoning": "one sentence explanation"}
```

**Judge call behaviour:**
- If judge score ≤ 2: immediately add to urgent queue even if this is the first failure
- If judge score ≥ 4: remove the heuristic flag (it was a false positive), do not add to queue
- If judge score = 3: keep the heuristic flag, add to queue using normal rules (FR-15)
- Judge call is async (fire-and-forget, not blocking the BrowserWatcher loop) — use `asyncio.create_task`
- Cap judge calls at 5/hour to limit API cost

**Cost control:**
- Track judge call count per hour using a sliding window
- If 5 calls already made in the last 60 minutes: skip the judge call, use heuristic flag only
- Log WARNING when judge calls are rate-limited

**Validation criteria:**
- Judge call does not block the BrowserWatcher loop (fire-and-forget)
- Judge score ≤ 2 → immediate queue add
- Judge score ≥ 4 → heuristic flag removed, no queue add
- No more than 5 judge calls per hour
- Judge call failure (timeout, parse error) logs WARNING and falls back to heuristic flag

---

### FR-19: Reflection Metadata in Execution History

The execution history table appended to skill files (already defined in v1.0) gains two
new columns: `heuristic_flag` and `judge_score`. Both are empty string for normal executions.
Populated when FR-15/FR-18 fire.

**Updated table format:**
```markdown
| date | input_slug | model | score | heuristic_flag | judge_score | notes |
|------|-----------|-------|-------|----------------|-------------|-------|
| 2026-04-14 | great-article-abc12 | gemini-2.0-flash | 0.95 | | | excellent |
| 2026-04-13 | news-item-def34 | gemini-2.0-flash | 0.55 | too_short | 2 | missing content |
```

**Columns:**
- `heuristic_flag`: one of `too_short`, `error_output`, `unstructured`, `verbatim_copy`, 
  or empty string if no flag
- `judge_score`: integer 1-5 if FR-18 fired, empty string otherwise

**Backward compatibility:**
- Existing skill files without these columns: append columns with empty string defaults
- Parser must handle both old (5 columns) and new (7 columns) table formats

**Validation criteria:**
- New executions write 7 columns (including the new metadata columns)
- Flagged executions have `heuristic_flag` populated
- Real-time judge calls (if enabled) populate `judge_score`
- Parser handles old 5-column format without crashing

---

### FR-20: `daemon.py` Integration

`daemon.py` must start the `run_urgent_loop()` coroutine alongside the existing nightly
`run_loop()`. Both are gathered in the same `asyncio.gather()` call that starts all loops.

**Guard:** Only start urgent loop if `optimizer.enabled: true` (same guard as nightly).

**Updated daemon.py gather call structure:**
```python
if daemon_role == "full":
    skill_optimizer = SkillOptimizer(...)
    tasks.append(skill_optimizer.run_loop())      # nightly
    tasks.append(skill_optimizer.run_urgent_loop())  # hourly
```

**Validation criteria:**
- Both loops run when `optimizer.enabled: true`
- Neither loop runs when `optimizer.enabled: false`
- Stopping the daemon (Ctrl+C) terminates both loops gracefully
- Hourly loop does not interfere with nightly loop (separate sleep cycles)

---

## Config

Full `skill_optimizer` section for `config.yaml.template`:

```yaml
skill_optimizer:
  enabled: true                     # enable optimizer (both nightly and hourly loops)
  run_hour: 3                       # hour to run daily (0–23, local time)
  min_runs_before_optimize: 10      # minimum numeric scores before optimizing
  underperformance_threshold: 0.70  # optimize if success_rate < this
  skip_above_threshold: 0.90        # skip if success_rate >= this (working well)
  regression_tolerance: 0.05        # rollback if new avg < old avg - this
  max_exemplars: 2                  # top-N scoring traces to inject as examples
  max_history_rows: 100             # prune execution history beyond this
  max_skill_backups: 5              # rolling backup files to keep (.1 through .N)
  judge_model: judge                # LiteLLM route for judge calls (Haiku 4.5)
  dry_run: false                    # log changes without writing files
  realtime_judge: false             # enable optional per-execution judge calls (FR-18)
```

---

## LLM Call Budget

Per daily run (N skills total, M skills needing optimization):

| Call type | Model route | Count | Notes |
|-----------|-------------|-------|-------|
| Judge scoring | `judge` (Haiku 4.5) | ~20–30 | One per pending execution row |
| Critique | `optimizer` (Sonnet) | M | Typically 0–2 per day |
| Rewrite | `optimizer` (Sonnet) | M | Typically 0–2 per day |
| **Total** | | **~24–34** | Mostly cheap Flash calls |

For a system with 10 skills running ~20 total executions per day:
- Scoring: 20 Flash calls ≈ $0.002
- Critique + rewrite (2 skills): 4 Sonnet calls ≈ $0.01
- **Daily total: ~$0.012**

**v1.1.0 real-time reflection additions:**
- Heuristic pre-filter: zero cost (synchronous checks)
- Optional real-time judge calls: 5/hour max = 120/day ≈ $0.012 (if enabled)
- Hourly urgent rewrites: ~3 skills/day = 6 Sonnet calls ≈ $0.015

**Total with real-time reflection enabled: ~$0.039/day**

---

## Module Design

### Real-time Reflection Architecture (v1.1.0)

Data flow from skill execution through real-time reflection to urgent rewrite:

```
BrowserWatcher.run()
      ↓
SkillExecutor.run() → output
      ↓
Heuristic Check (FR-15)
      ├─ too_short / error_output / unstructured / verbatim_copy?
      │       ↓ YES
      │  [Optional] Async Judge Call (FR-18, if enabled)
      │       ↓
      │  Add to SkillOptimizer._urgent_queue (FR-16)
      │       ↓
      └─ NO → continue
      ↓
Write memory file (normal pipeline)

[Every 60 minutes]
SkillOptimizer.run_urgent_loop() (FR-17)
      ↓
Group _urgent_queue by skill
      ↓
Skills with ≥2 flagged executions → immediate rewrite
Skills with 1 flagged execution + nightly history match → rewrite
      ↓
Critique + Rewrite (same as nightly, scoped to flagged skills)
      ↓
Clear processed entries from _urgent_queue
```

**Key design decisions:**
1. **Heuristics first, judge optional**: Fast synchronous checks catch obvious failures 
   without blocking the BrowserWatcher loop. LLM judge only fires if enabled.
2. **Fire-and-forget judge**: Judge call is `asyncio.create_task()`, not `await` — 
   never blocks memory write.
3. **Queue capping prevents flood**: 20-entry cap + existing-skills-only rule prevents 
   a single bad URL from triggering infinite rewrites.
4. **Hourly not instant**: Rewrite cycle is expensive; batching at 60-min intervals 
   balances responsiveness vs. cost.
5. **Reuse nightly rewrite logic**: Hourly processor calls the same critique+edit path, 
   just scoped to flagged skills — no code duplication.

---

## Updated Skill File Format

Full template showing all sections, in order:

```markdown
---
name: summarize-webpage
version: 2
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
success_rate: 0.82
total_runs: 47
last_optimized: 2026-04-15
prev_version_avg_score: 0.71
exemplar_eligible: true
---

## Instructions

(current prompt text — the only section the optimizer rewrites)

## Top Examples
<!-- Auto-managed by optimizer. Do not edit manually. -->
### Example 1 (score: 0.95, 2026-04-13)
**Input:** url=https://example.com/article, title=Example Article
**Output:**
## Summary
Two-sentence summary of the article.

**Key Points**
- Point one extracted from content
- Point two extracted from content

**Entities**
- ExampleCorp (company), Jane Smith (author)

**Tags:** ai, research, 2026

### Example 2 (score: 0.92, 2026-04-12)
...

## Evolution Log

### v2 (2026-04-15) — improve entity extraction
**Critique:** Summaries consistently miss company names mentioned in body text
**Failure patterns:** missing-entities, tags-too-generic
**Change:** Added explicit instruction to scan for organization, product, and person names
**Pre-optimization avg:** 0.71 | **Post (projected):** 0.82

### v1 (2026-04-11) — initial version

## Execution History

| date | input_slug | model | score | heuristic_flag | judge_score | notes |
|------|-----------|-------|-------|----------------|-------------|-------|
| 2026-04-14 | great-article-abc12 | gemini-2.0-flash | 0.95 | | | excellent |
| 2026-04-13 | news-item-def34 | gemini-2.0-flash | 0.55 | too_short | 2 | missing entity ExampleCorp |
| 2026-04-12 | old-post-ghi78 | gemini-2.0-flash | 0.88 | | | |
```

**Backup files** (iCloud `skills/` directory, alongside the skill file):
```
summarize-webpage.md         ← current version
summarize-webpage.md.1       ← most recent backup (before last rewrite)
summarize-webpage.md.2       ← backup before that
...
summarize-webpage.md.5       ← oldest backup kept
```

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `specs/feat-skill-optimizer.md` | **This spec** |
| `skill_optimizer.py` | Replace stub with full implementation (all 14 FRs) |
| `skill_executor.py` | (1) Change watcher log path to `BRAIN_DIR/logs/{hostname}-execution-log.jsonl`; (2) add `_reload_if_modified()` (FR-14) |
| `skills/skill-optimizer.md` | Rewrite meta-skill Instructions with Critique-Edit protocol (FR-13) |
| `skills/summarize-webpage.md` | Add `exemplar_eligible: true`, `prev_version_avg_score: null`, `last_optimized: null` to frontmatter |
| `config.yaml.template` | Add new config keys to `skill_optimizer` section |
| `CLAUDE.md` | Update skill optimizer description (no longer a stub) |
| `README.md` | Document daily optimization pass, backup files, dry-run mode |
| `tests/unit/test_skill_optimizer.py` | **Create** — unit test suite |
| `tests/integration/test_pipeline.py` | Add optimizer integration tests |

---

## Unit Tests (`tests/unit/test_skill_optimizer.py`)

| Test | Assertion |
|------|-----------|
| `test_scheduling_calculates_correct_sleep` | Sleep duration brings wakeup to run_hour |
| `test_scheduling_next_day_if_past_run_hour` | If past run_hour, schedules for tomorrow |
| `test_merge_watcher_logs_appends_rows` | JSONL records appear in skill history table |
| `test_merge_watcher_logs_renames_processed` | Processed JSONL file renamed after merge |
| `test_merge_watcher_logs_skips_missing_skill` | Record for unknown skill: WARNING, no crash |
| `test_score_pending_updates_row` | `pending` row replaced with numeric score |
| `test_score_pending_updates_frontmatter` | `success_rate` and `total_runs` recalculated |
| `test_score_no_memory_file_leaves_pending` | No matching memory file: row stays pending |
| `test_gates_min_runs_not_met` | Fewer than min_runs numeric scores → skipped |
| `test_gates_above_skip_threshold` | success_rate ≥ skip_above_threshold → skipped |
| `test_gates_above_underperformance` | success_rate ≥ underperformance_threshold → skipped |
| `test_gates_meta_skill_always_skipped` | skill-optimizer.md never optimized |
| `test_backup_rotation` | .1 → .2, .2 → .3, current → .1 |
| `test_backup_rotation_max_reached` | .N deleted before rotating |
| `test_regression_triggers_rollback` | New avg < old avg - tolerance → .1 restored |
| `test_regression_no_rollback_within_tolerance` | Drop within tolerance → no rollback |
| `test_regression_no_backup_logs_warning` | Missing .1 backup → WARNING, no crash |
| `test_critique_json_parse_failure_skips` | Malformed critique JSON → WARNING, skip skill |
| `test_rewrite_identical_instructions_noop` | Same Instructions → no backup, no write |
| `test_rewrite_updates_version_in_frontmatter` | version incremented by 1 |
| `test_exemplars_injected_for_eligible_skill` | Top examples in ## Top Examples section |
| `test_exemplars_not_injected_for_ineligible` | exemplar_eligible: false → no section |
| `test_exemplars_section_replaced_not_appended` | Second run replaces, not appends |
| `test_history_pruning_keeps_newest` | After pruning, oldest rows removed |
| `test_history_pruning_preserves_pending` | Pending rows not pruned |
| `test_history_pruning_noop_under_limit` | No write if count within limit |
| `test_evolution_log_entry_format` | Entry has all required fields |
| `test_atomic_write_no_tmp_left` | No .tmp file after successful write |
| `test_dry_run_no_files_written` | dry_run: true → no file modifications |
| `test_dry_run_logs_proposed_changes` | dry_run: true → INFO logs show what would change |
| `test_executor_reload_on_modify` | SkillExecutor picks up new Instructions after optimizer write |
| `test_heuristic_too_short_flags` | Output < 100 chars → flagged as too_short |
| `test_heuristic_error_output_flags` | Output contains "I cannot" → flagged as error_output |
| `test_heuristic_unstructured_flags` | No markdown + < 300 chars → flagged as unstructured |
| `test_heuristic_verbatim_copy_flags` | >90% char overlap → flagged as verbatim_copy |
| `test_heuristic_adds_to_urgent_queue` | Flag → entry added to _urgent_queue |
| `test_heuristic_does_not_suppress_write` | Flagged execution still writes memory file |
| `test_urgent_queue_caps_at_20` | Queue never exceeds 20 entries |
| `test_urgent_queue_capped_only_existing_skills` | After cap, only skills already in queue can add entries |
| `test_hourly_loop_schedules_60min` | run_urgent_loop() sleeps ~3600 seconds |
| `test_hourly_loop_processes_2plus_flags` | Skill with ≥2 flags → immediate rewrite |
| `test_hourly_loop_single_flag_plus_history` | 1 flag + nightly history match → rewrite |
| `test_hourly_loop_caps_3_skills_per_tick` | No more than 3 skills rewritten per hourly tick |
| `test_hourly_loop_clears_processed_entries` | After rewrite, processed entries removed from queue |
| `test_realtime_judge_score_2_immediate_add` | Judge score ≤ 2 → immediate queue add |
| `test_realtime_judge_score_4_removes_flag` | Judge score ≥ 4 → heuristic flag removed, no queue add |
| `test_realtime_judge_rate_limited_5_per_hour` | 6th judge call in 60 min → skipped, WARNING logged |
| `test_realtime_judge_async_no_blocking` | Judge call does not block BrowserWatcher loop |
| `test_execution_history_7_columns` | New executions write 7 columns (including heuristic_flag, judge_score) |
| `test_execution_history_backward_compat` | Parser handles old 5-column format |
| `test_daemon_starts_both_loops` | daemon.py starts both run_loop() and run_urgent_loop() |
| `test_daemon_guard_enabled_false` | optimizer.enabled: false → neither loop starts |

---

## Integration Tests (additions to `tests/integration/test_pipeline.py`)

| Test | Assertion |
|------|-----------|
| `test_optimizer_scores_pending_rows` | Seed skill with pending rows → optimizer updates scores |
| `test_optimizer_rewrites_underperforming_skill` | Seed with low scores → Instructions section changed |
| `test_optimizer_backup_created` | After rewrite → `.1` backup exists with prior content |
| `test_optimizer_rollback_on_regression` | Pre-populate prev_version_avg_score > new avg → optimizer restores backup |
| `test_watcher_log_merged_before_scoring` | Write JSONL to iCloud logs dir → rows appear in history |
| `test_heuristic_flags_trigger_hourly_rewrite` | Seed _urgent_queue with 2 flags → hourly loop rewrites skill |
| `test_realtime_judge_prevents_false_positive` | Heuristic flag + judge score 4 → no queue add |

---

## Changelog

### v1.1.0 (2026-04-12) — Real-time Reflection

Adds real-time quality checks and hourly urgent rewrites to catch systematic failures
faster than the nightly batch cycle.

**New functional requirements:**
- **FR-15**: Heuristic pre-filter — synchronous quality checks after every execution 
  (too_short, error_output, unstructured, verbatim_copy) with zero LLM cost
- **FR-16**: Urgent rewrite queue — in-memory list of flagged executions, capped at 
  20 entries to prevent flood
- **FR-17**: Hourly queue processor — second async loop that rewrites skills with ≥2 
  flagged executions, capped at 3 skills/hour for cost control
- **FR-18**: Optional per-execution judge call — async LLM call to validate or override 
  heuristic flags, rate-limited to 5/hour
- **FR-19**: Reflection metadata in execution history — two new columns (`heuristic_flag`, 
  `judge_score`) create training signal for nightly optimizer
- **FR-20**: daemon.py integration — starts both nightly and hourly loops under the 
  same `optimizer.enabled` guard

**Architecture changes:**
- Moved real-time scoring from "Out of Scope" to in-scope with nuanced heuristic-first approach
- Added Real-time Reflection Architecture diagram showing data flow from execution → 
  heuristic → judge → queue → rewrite
- Updated skill file format to 7-column execution history table (backward compatible)

**Cost impact:**
- Heuristics: zero cost
- Real-time judge (if enabled): ~$0.012/day (120 calls max)
- Hourly urgent rewrites: ~$0.015/day (3 skills × 2 calls)
- Total daily cost with reflection enabled: ~$0.039 (3.25× the v1.0 baseline, still < $0.04)

**Testing additions:**
- 21 new unit tests covering heuristic checks, queue management, hourly loop, judge rate limiting
- 2 new integration tests for end-to-end reflection pipeline

### v1.0.0 (2026-04-11) — Initial Spec

Nightly batch optimizer with LLM-as-judge scoring, OPRO-style critique-edit rewrite,
regression rollback, and auto-exemplar injection.
