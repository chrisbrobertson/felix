---
specmas: 3.0
kind: feature
id: feat-skill-utility-scoring
version: 1.0.0
created: 2026-04-12
status: implemented
shipped_version: "1.4.0"
complexity: low
maturity: 1
parent_system: second-brain
related_specs:
  - feat-skill-optimizer
  - feat-skill-routing
  - feat-skill-creation
---

# Skill Utility Scoring

## Overview

Replace the simple mean `success_rate` with a recency-weighted utility score that surfaces skill health trends and improves optimization prioritization.

## Problem Statement

The current `success_rate` is a simple arithmetic mean of all numeric scores in a skill's execution history. This creates two critical failure modes:

1. **False positives:** A skill that performed well 3 months ago (scores 0.9–1.0) but has degraded recently (scores 0.5–0.6) shows an aggregate `success_rate` of 0.85. The optimizer skips it because it exceeds the underperformance threshold, but the skill is actively declining and needs attention.

2. **False negatives:** A skill that had early bad runs during initial development (scores 0.3–0.5) but has improved after iteration (recent scores 0.8–0.9) remains blocked in the "underperforming" bucket. The low aggregate score triggers unnecessary optimization cycles.

The root cause is temporal weighting: all executions contribute equally regardless of age. A score from 90 days ago has the same influence as a score from today.

The fix is recency-weighted scoring that:
- Gives declining skills more optimizer attention (catches degradation early)
- Gives recently-improved skills credit faster (avoids over-optimization)
- Provides operators visibility into skill health trends via Telegram

## Design Principles

1. **Temporal relevance:** Recent performance matters more than distant history.
2. **Consistency with codebase:** Use the same half-life decay pattern already in `contact_tracker.py` for relationship scoring.
3. **Auditability:** Single closed-form function — no EWMA state, no Bayesian priors.
4. **Backward compatibility:** Keep `success_rate` for raw statistics; introduce `utility_score` alongside it.
5. **Observability:** Surface health trends through a zero-LLM-cost Telegram command.

## Scoring Formula

Weighted mean using half-life decay:

```
weight(days_old) = 1.0 / (1.0 + days_old / half_life_days)

utility_score = sum(score_i × weight_i) / sum(weight_i)
```

Where:
- `score_i` is the numeric score [0.0, 1.0] from execution row `i`
- `days_old = (today - execution_date).days`
- `half_life_days` is the decay parameter (default: 14)

### Weight Decay Examples

For `half_life_days = 14`:
- Score from today (0 days old): weight = 1.0 / (1.0 + 0/14) = **1.00** (full contribution)
- Score from 14 days ago: weight = 1.0 / (1.0 + 14/14) = **0.50** (half contribution)
- Score from 28 days ago: weight = 1.0 / (1.0 + 28/14) = **0.33**
- Score from 60 days ago: weight = 1.0 / (1.0 + 60/14) = **0.19**
- Score from 90 days ago: weight = 1.0 / (1.0 + 90/14) = **0.13**

### Edge Cases

- **Minimum data threshold:** Return `None` if fewer than 3 numeric scores exist. Skills with insufficient history are excluded from optimization gates and shown as "new" in `/skill-health`.
- **Non-numeric scores:** Execution rows with `score: pending` or missing scores are skipped (not counted toward the 3-row minimum).
- **Same-day executions:** All have `days_old = 0`, all receive weight `1.0`.
- **Date parsing:** Execution dates are ISO 8601 strings (`YYYY-MM-DD`). Parse with `datetime.date.fromisoformat()`.

## Trend Detection

Skill health trend is computed by comparing utility scores across two sliding windows:

```python
recent_window    = last 10 execution rows with numeric scores
previous_window  = next 10 execution rows before that (rows 11–20)
```

### Trend Classification

Compute the simple mean of scores in each window. If both windows contain at least 5 numeric scores:

- **improving:** `recent_mean > previous_mean + 0.05`
- **declining:** `recent_mean < previous_mean - 0.05`
- **stable:** `abs(recent_mean - previous_mean) <= 0.05`

If either window has fewer than 5 numeric scores:
- **insufficient-data**

The 0.05 threshold creates a dead zone to avoid trend flapping from minor variance.

### Rationale

Window size of 10 balances responsiveness (small enough to catch recent shifts) and noise resistance (large enough to smooth over single bad executions). Requiring 5 scores per window ensures statistical validity.

## Frontmatter Schema Changes

New fields added to skill file frontmatter (alongside existing `success_rate`, `total_runs`, `last_optimized`, `prev_version_avg_score`):

```yaml
utility_score: 0.84                  # recency-weighted score [0.0, 1.0] or null
utility_score_updated: 2026-04-12    # ISO 8601 date of last recalculation
score_trend: improving               # "improving" | "declining" | "stable" | "insufficient-data"
half_life_days: 14                   # decay parameter (overrides global config if present)
```

### Field Semantics

- **`utility_score`:** Replaces `success_rate` in optimizer gates. Set to `null` if fewer than 3 numeric scores exist. Always a float [0.0, 1.0] if not null.
- **`utility_score_updated`:** ISO 8601 date (`YYYY-MM-DD`) of last recalculation. Allows operators to verify staleness.
- **`score_trend`:** One of four values. Drives priority optimization (FR-4). Always updated atomically with `utility_score`.
- **`half_life_days`:** Per-skill override of global config. If present, takes precedence over `skill_optimizer.half_life_days`. Allows fine-tuning decay for high-frequency vs. low-frequency skills.

### Backward Compatibility

- **`success_rate`:** Still computed and written. Simple arithmetic mean of all numeric scores. Kept for raw record-keeping and historical continuity.
- **`total_runs`:** Still incremented on every execution (includes pending/failed runs).
- **`last_optimized`:** Still written by optimizer.
- **`prev_version_avg_score`:** Still written by optimizer for regression detection.

Migration: Skills without `utility_score` in frontmatter will have it computed on next optimizer run. No manual migration script needed.

## Functional Requirements

### FR-1: Utility Score Calculation Function

New module-level function in `skill_optimizer.py`:

```python
def _compute_utility_score(
    execution_rows: list[dict],
    half_life_days: int = 14
) -> float | None:
    """
    Compute recency-weighted utility score from execution history.
    
    Args:
        execution_rows: List of dicts with 'score' (float | str) and 'date' (str YYYY-MM-DD).
                       Ordered newest-first (same order as Execution History table).
        half_life_days: Decay parameter. Scores from N days ago have weight 0.5.
    
    Returns:
        Float [0.0, 1.0] if at least 3 numeric scores exist, else None.
    """
```

**Implementation notes:**

1. Filter `execution_rows` to those with numeric `score` (float, not "pending" or null).
2. If fewer than 3 numeric rows, return `None`.
3. Parse `date` field with `datetime.date.fromisoformat(row["date"])`.
4. Compute `days_old = (datetime.date.today() - execution_date).days`.
5. Compute `weight = 1.0 / (1.0 + days_old / half_life_days)`.
6. Return `sum(score × weight) / sum(weight)`.

### FR-2: Trend Calculation Function

New module-level function in `skill_optimizer.py`:

```python
def _compute_trend(execution_rows: list[dict]) -> str:
    """
    Classify skill health trend from execution history.
    
    Args:
        execution_rows: List of dicts with 'score' (float | str).
                       Ordered newest-first.
    
    Returns:
        "improving" | "declining" | "stable" | "insufficient-data"
    """
```

**Implementation notes:**

1. Filter to numeric scores.
2. Extract `recent_window = rows[:10]` and `previous_window = rows[10:20]`.
3. If either window has fewer than 5 numeric scores, return `"insufficient-data"`.
4. Compute `recent_mean` and `previous_mean` (simple arithmetic mean).
5. If `recent_mean > previous_mean + 0.05`, return `"improving"`.
6. If `recent_mean < previous_mean - 0.05`, return `"declining"`.
7. Else return `"stable"`.

### FR-3: Update Utility Score After Scoring Pass

In `skill_optimizer.py`, after the judge scores pending execution rows for a skill:

1. Parse the full execution history from the skill file.
2. Call `_compute_utility_score(execution_rows, half_life_days)`.
3. Call `_compute_trend(execution_rows)`.
4. Write `utility_score`, `utility_score_updated` (today's date), and `score_trend` to frontmatter atomically (single file write).
5. Also update `success_rate` (existing logic, simple mean).

**Atomicity:** All frontmatter updates for a skill are written in a single `_write_skill_file()` call. No partial state.

**Half-life resolution:** Read `half_life_days` from skill frontmatter if present, else fall back to `config.yaml` `skill_optimizer.half_life_days` (default: 14).

### FR-4: Optimizer Gates Use Utility Score

Replace all references to `success_rate` in optimization gate logic with `utility_score`:

1. **Underperformance gate:** `if utility_score < underperformance_threshold` (not `success_rate`).
2. **Skip-above-threshold gate:** `if utility_score >= skip_above_threshold` (not `success_rate`).
3. **Regression detection:** Compare post-optimization `utility_score` to pre-optimization `utility_score` (stored as `prev_version_utility_score` in frontmatter, analogous to `prev_version_avg_score`).

**Existing behavior preserved:**
- Skills with `utility_score = None` (insufficient data) are skipped from optimization with INFO log: "Skipping {skill} (insufficient execution history)".
- Evolution log still records both `success_rate` (raw mean) and `utility_score` (weighted mean) for auditability.

**Logging changes:**
- Replace "success_rate=0.65" with "utility_score=0.65" in all optimizer log messages.
- Evolution log entries include both: `success_rate: 0.72 | utility_score: 0.68`.

### FR-5: Priority Optimization for Declining Skills

Introduce a second optimization trigger alongside the underperformance gate:

```python
if score_trend == "declining" and utility_score < 0.80:
    # Optimize even if utility_score >= underperformance_threshold
```

**Rationale:** A skill with `utility_score = 0.76` and `score_trend = "declining"` is on a downward trajectory. Waiting for it to fall below `underperformance_threshold = 0.70` wastes optimizer cycles on more degraded state. Early intervention catches regressions sooner.

**Threshold:** 0.80 chosen to target the top quartile of the [0.7, 1.0] healthy range. Skills below 0.80 with declining trends are candidates for tuning.

**Logging:** `INFO: Optimizing {skill} (declining trend, utility_score=0.76)`.

**Interaction with skip_above_threshold:** The declining-trend gate applies only if `utility_score < 0.80`. If `skip_above_threshold = 0.85`, a skill with `utility_score = 0.82` and `score_trend = "declining"` will still be optimized (because 0.82 < 0.85 and 0.82 < 0.80 is false — wait, that's wrong). Correction:

```python
# Gate 1: Standard underperformance
if utility_score < underperformance_threshold:
    optimize_skill(skill)
    continue

# Gate 2: Declining trend early intervention
if score_trend == "declining" and utility_score < 0.80:
    optimize_skill(skill)
    continue

# Gate 3: Skip high performers
if utility_score >= skip_above_threshold:
    log.info(f"Skipping {skill} (utility_score={utility_score:.2f} >= {skip_above_threshold})")
    continue
```

This sequence ensures declining skills are caught before the high-performer skip gate.

### FR-6: /skill-health Telegram Command

New command handler in `chat_handler.py`:

```python
async def cmd_skill_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show utility scores and health trends for all skill variants."""
```

**Output format:**

```
Skill health — 5 skills

summarize-webpage   ▲ improving  0.87  (47 runs, last opt Apr 8)
summarize-paper     ◆ stable     0.82  (12 runs)
summarize-docs      ▼ declining  0.71  (8 runs) ⚠
summarize-repo      — new        —     (2 runs, need 3+ to score)
chat                ◆ stable     0.91  (203 runs, last opt Mar 15)

⚠ = below underperformance threshold (0.70) or declining
```

**Symbol legend:**
- `▲` improving
- `▼` declining
- `◆` stable
- `—` insufficient-data

**Columns:**
1. Skill name (max 20 chars, truncated with ellipsis if longer)
2. Trend symbol + trend label (10 chars, right-padded)
3. Utility score (4 chars: "0.87" or "—   " if null)
4. Total runs + last optimization date (if `last_optimized` exists in frontmatter)

**Warning indicator:** Append ` ⚠` to the line if:
- `utility_score < underperformance_threshold` (from config, default 0.70), OR
- `score_trend == "declining"`

**Sorting:** Skills sorted by `utility_score` ascending (worst first). Skills with `utility_score = None` appear at the top (sorted by name among themselves).

**Data source:** Read from skill file frontmatter. No LLM calls. Glob `SKILLS_DIR/*.md`, parse YAML frontmatter, render table.

**Telegram formatting:** Use monospace block for alignment:

```python
await update.message.reply_text(
    f"```\nSkill health — {len(skills)} skills\n\n{table_text}\n\n{legend}\n```",
    parse_mode="Markdown"
)
```

**Error handling:** If `SKILLS_DIR` is empty or no skill files exist, reply: "No skills found."

### FR-7: COMMAND_REGISTRY Entry

Add to `chat_handler.py` `COMMAND_REGISTRY`:

```python
COMMAND_REGISTRY = [
    # ... existing entries ...
    ("Skill management", [
        ("skill-health", "Show utility scores and health trends for all skill variants"),
    ]),
]
```

This ensures `/help` output includes the new command and `test_command_registry_matches_handlers` passes.

### FR-8: CommandHandler Registration

In `chat_handler.py` `main()`:

```python
application.add_handler(CommandHandler("skill-health", cmd_skill_health))
```

### FR-9: Half-Life Configuration

Add new config key to `config.yaml.template`:

```yaml
skill_optimizer:
  half_life_days: 14          # Recency decay half-life for utility scoring (days)
  underperformance_threshold: 0.70
  skip_above_threshold: 0.85
  # ... existing keys ...
```

**Resolution order:**
1. Per-skill frontmatter `half_life_days` (if present)
2. Global config `skill_optimizer.half_life_days`
3. Hardcoded default: 14

**Validation:** Must be integer > 0. If invalid, log warning and fall back to default 14.

## Configuration Schema

```yaml
skill_optimizer:
  half_life_days: 14          # Recency decay half-life for utility scoring (days)
  underperformance_threshold: 0.70
  skip_above_threshold: 0.85
  judge_route: judge
  critique_route: optimizer
  rewrite_route: optimizer
```

No changes to existing keys.

## Data Model Changes

### Skill File Frontmatter (Before)

```yaml
---
name: summarize-webpage
success_rate: 0.82
total_runs: 47
last_optimized: 2026-04-08
prev_version_avg_score: 0.78
---
```

### Skill File Frontmatter (After)

```yaml
---
name: summarize-webpage
success_rate: 0.82            # raw mean (kept for backward compat)
utility_score: 0.87           # recency-weighted mean (drives optimization)
utility_score_updated: 2026-04-12
score_trend: improving        # improving | declining | stable | insufficient-data
half_life_days: 14            # optional per-skill override
total_runs: 47
last_optimized: 2026-04-08
prev_version_avg_score: 0.78  # raw mean before last optimization
prev_version_utility_score: 0.81  # utility score before last optimization
---
```

## Files to Create/Modify

| File | Change | Lines (est.) |
|---|---|---|
| `specs/feat-skill-utility-scoring.md` | This spec | 600 |
| `skill_optimizer.py` | Add `_compute_utility_score()`, `_compute_trend()`, update all gate comparisons, add `prev_version_utility_score` tracking | 120 |
| `chat_handler.py` | Add `cmd_skill_health()`, register CommandHandler, add to COMMAND_REGISTRY | 80 |
| `skills/summarize-webpage.md` | Add `utility_score`, `utility_score_updated`, `score_trend`, `half_life_days` to frontmatter | 4 |
| `config.yaml.template` | Add `half_life_days: 14` under `skill_optimizer` | 1 |
| `tests/unit/test_skill_optimizer.py` | Add 9 new test functions for utility scoring and trend detection | 200 |
| `tests/unit/test_chat_handler.py` | Add `test_cmd_skill_health()`, `test_cmd_skill_health_sorts_worst_first()` | 60 |

## Implementation Phases

### Phase 1: Core Scoring (Standalone)

1. Implement `_compute_utility_score()` in `skill_optimizer.py`.
2. Implement `_compute_trend()` in `skill_optimizer.py`.
3. Write unit tests for both functions (7 tests).
4. Run `pytest tests/unit/test_skill_optimizer.py -k utility` — all pass.

**Deliverable:** Scoring functions implemented and tested, but not yet integrated into optimizer gates.

### Phase 2: Optimizer Integration

1. Modify optimizer scoring loop to call `_compute_utility_score()` and `_compute_trend()` after judging pending rows.
2. Write `utility_score`, `utility_score_updated`, `score_trend` to frontmatter.
3. Replace `success_rate` with `utility_score` in all gate comparisons.
4. Add declining-skill priority gate (FR-5).
5. Add `prev_version_utility_score` tracking (analogous to `prev_version_avg_score`).
6. Update Evolution Log format to include both `success_rate` and `utility_score`.
7. Write integration test: `test_optimizer_gates_use_utility_score()`.
8. Write integration test: `test_declining_skill_prioritized()`.

**Deliverable:** Optimizer fully migrated to utility scoring. `pytest tests/unit/test_skill_optimizer.py` passes.

### Phase 3: Telegram Command

1. Implement `cmd_skill_health()` in `chat_handler.py`.
2. Register CommandHandler and add COMMAND_REGISTRY entry.
3. Write unit tests: `test_cmd_skill_health_shows_all_skills()`, `test_cmd_skill_health_sorts_worst_first()`.
4. Run full test suite: `pytest`.

**Deliverable:** `/skill-health` command live. Full test suite passes.

### Phase 4: Config and Documentation

1. Add `half_life_days: 14` to `config.yaml.template`.
2. Update `README.md` section on skill optimization to mention utility scoring.
3. Update all skill files in `skills/` to include new frontmatter fields (run optimizer once to auto-populate).
4. Commit with message: "Add recency-weighted utility scoring to skill optimizer"
5. Deploy with `./install.sh`.

**Deliverable:** Feature complete and deployed.

## Testing Strategy

### Unit Tests

**File:** `tests/unit/test_skill_optimizer.py`

1. **`test_utility_score_weights_recent_more`**
   - Setup: Two scores: 1.0 from today, 0.0 from 30 days ago, `half_life_days=14`.
   - Expected: `utility_score > 0.5` (recent score has more weight).
   - Assert: `0.7 <= result <= 0.9` (rough bounds for validation).

2. **`test_utility_score_weights_old_less`**
   - Setup: Two scores: 0.0 from today, 1.0 from 30 days ago, `half_life_days=14`.
   - Expected: `utility_score < 0.5` (old score has less weight).
   - Assert: `0.1 <= result <= 0.3`.

3. **`test_utility_score_returns_none_under_minimum`**
   - Setup: Two numeric scores (below 3-row threshold).
   - Expected: `None`.

4. **`test_utility_score_skips_pending`**
   - Setup: Five rows total: three with numeric scores, two with `"pending"`.
   - Expected: Only three numeric rows contribute. `utility_score` is computed (not `None`).

5. **`test_trend_improving`**
   - Setup: Recent 10 scores all 0.9, previous 10 scores all 0.8.
   - Expected: `"improving"`.

6. **`test_trend_declining`**
   - Setup: Recent 10 scores all 0.6, previous 10 scores all 0.75.
   - Expected: `"declining"`.

7. **`test_trend_stable`**
   - Setup: Recent 10 scores mean 0.80, previous 10 scores mean 0.81 (within ±0.05).
   - Expected: `"stable"`.

8. **`test_trend_insufficient_data`**
   - Setup: Recent window has 8 scores, previous window has 3 scores (both < 5).
   - Expected: `"insufficient-data"`.

9. **`test_optimizer_gates_use_utility_score`**
   - Setup: Mock skill file with `success_rate=0.65`, `utility_score=0.75`, `underperformance_threshold=0.70`.
   - Expected: Skill is NOT optimized (utility_score is above threshold).
   - Assert: No critique/rewrite calls made.

10. **`test_declining_skill_prioritized`**
    - Setup: Mock skill file with `utility_score=0.75`, `score_trend="declining"`, `underperformance_threshold=0.70`.
    - Expected: Skill IS optimized (declining trend triggers priority gate).
    - Assert: Critique/rewrite calls made. Log message: "Optimizing {skill} (declining trend, utility_score=0.75)".

**File:** `tests/unit/test_chat_handler.py`

11. **`test_cmd_skill_health_shows_all_skills`**
    - Setup: Mock `SKILLS_DIR` with 3 skill files (varying `utility_score`, `score_trend`).
    - Expected: Reply text contains all three skill names.
    - Assert: `assert "summarize-webpage" in reply.text`.

12. **`test_cmd_skill_health_sorts_worst_first`**
    - Setup: Three skills with `utility_score` [0.91, 0.71, 0.87].
    - Expected: Reply text lists 0.71 skill first, 0.91 skill last.
    - Assert: `reply.text.index("skill-low") < reply.text.index("skill-high")`.

### Integration Tests

None required — unit tests cover all new code paths. Existing integration tests for `skill_optimizer.py` (end-to-end optimization loop) will exercise utility scoring via the normal flow.

### Manual Testing

1. Run optimizer: `~/secondbrain/venv/bin/python3 skill_optimizer.py` (standalone mode).
2. Inspect skill file frontmatter — verify `utility_score`, `utility_score_updated`, `score_trend` written.
3. Send `/skill-health` to Telegram bot — verify output formatting, sorting, warning symbols.
4. Add a skill with declining trend + `utility_score=0.75` — verify it gets optimized on next run (check Evolution Log).

## Rollout Plan

### Pre-deployment

1. Run full test suite: `pytest` — all pass.
2. Review Evolution Log format changes — ensure both `success_rate` and `utility_score` are logged.
3. Backup existing skill files (they will be modified with new frontmatter fields).

### Deployment

1. Commit changes: `git commit -m "Add recency-weighted utility scoring to skill optimizer"`
2. Deploy: `./install.sh`
3. Verify daemon restart: `tail -f ~/secondbrain/logs/out.log`

### Post-deployment

1. Wait for next optimizer run (3 AM daily).
2. Check `~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/skills/*.md` — verify `utility_score` fields populated.
3. Send `/skill-health` to bot — verify output.
4. Monitor Evolution Log for 7 days — verify declining skills are caught early.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Half-life too short (e.g., 7 days) causes over-responsiveness to noise | High | Default to 14 days (2-week half-life). Provide per-skill override for tuning. |
| Half-life too long (e.g., 60 days) makes utility score lag behind reality | Medium | Monitor via `/skill-health`. Operator can lower `half_life_days` if skills show stale scores. |
| Trend detection flaps on borderline cases (mean diff near 0.05) | Low | 0.05 threshold creates dead zone. Accept minor flapping as acceptable trade-off for responsiveness. |
| Declining-skill gate triggers too aggressively | Medium | 0.80 threshold chosen conservatively. Log at INFO so operators can track false positives. |
| Migration breaks existing skill files | High | Keep `success_rate` field. Write new fields alongside. No destructive edits. |
| `/skill-health` output too wide for Telegram mobile | Medium | Truncate skill names at 20 chars. Use monospace block for alignment. Test on mobile before deploy. |

## Future Enhancements (Out of Scope)

1. **Per-skill half-life auto-tuning:** Analyze execution frequency and adjust `half_life_days` automatically (high-frequency skills get shorter half-life).
2. **Utility score visualization:** Export skill health history to a time-series chart (PNG sent via Telegram).
3. **Skill health digest:** Weekly Telegram message summarizing skills that crossed threshold boundaries.
4. **Multi-metric utility:** Incorporate latency and token cost alongside accuracy score (weighted composite).
5. **A/B testing framework:** Run two skill variants concurrently, compare utility scores, auto-promote winner.

## Acceptance Criteria

- [ ] `_compute_utility_score()` implemented and tested (4 unit tests pass).
- [ ] `_compute_trend()` implemented and tested (4 unit tests pass).
- [ ] Optimizer writes `utility_score`, `utility_score_updated`, `score_trend` to frontmatter after every scoring pass.
- [ ] Optimizer gates use `utility_score` instead of `success_rate`.
- [ ] Declining-skill priority gate triggers optimization for `score_trend=declining` AND `utility_score < 0.80`.
- [ ] `/skill-health` command shows all skills, sorted worst-first, with trend symbols and warning indicators.
- [ ] `COMMAND_REGISTRY` includes `skill-health` entry.
- [ ] `config.yaml.template` includes `half_life_days: 14`.
- [ ] Full test suite passes: `pytest` (12 tests added, all pass).
- [ ] README.md updated to mention utility scoring in skill optimizer section.
- [ ] Deployed via `./install.sh` and daemon restarted.
- [ ] Manual verification: skill files have new frontmatter fields after next optimizer run.
- [ ] Manual verification: `/skill-health` output renders correctly in Telegram mobile app.

## Appendix A: Example Execution History

Skill file `summarize-webpage.md` before optimization (showing execution history that would trigger declining-skill gate):

```markdown
---
name: summarize-webpage
success_rate: 0.78            # simple mean of all 15 scores
utility_score: 0.71           # recency-weighted (recent scores are lower)
utility_score_updated: 2026-04-12
score_trend: declining        # recent mean 0.65, previous mean 0.85
half_life_days: 14
total_runs: 15
last_optimized: 2026-03-20
prev_version_avg_score: 0.82
prev_version_utility_score: 0.80
---

# Summarize Webpage

...

## Execution History

| Date       | URL | Score | Notes |
|------------|-----|-------|-------|
| 2026-04-12 | https://example.com/page1 | 0.6 | Missing key facts |
| 2026-04-11 | https://example.com/page2 | 0.7 | Good but verbose |
| 2026-04-10 | https://example.com/page3 | 0.65 | Hallucinated a date |
| 2026-04-09 | https://example.com/page4 | 0.7 | Acceptable |
| 2026-04-08 | https://example.com/page5 | 0.6 | Missed main point |
| 2026-04-07 | https://example.com/page6 | 0.75 | Good |
| 2026-04-06 | https://example.com/page7 | 0.65 | Too brief |
| 2026-04-05 | https://example.com/page8 | 0.7 | Acceptable |
| 2026-04-04 | https://example.com/page9 | 0.6 | Poor structure |
| 2026-04-03 | https://example.com/page10 | 0.7 | Acceptable |
| 2026-03-25 | https://example.com/page11 | 0.85 | Excellent |
| 2026-03-24 | https://example.com/page12 | 0.9 | Excellent |
| 2026-03-23 | https://example.com/page13 | 0.85 | Excellent |
| 2026-03-22 | https://example.com/page14 | 0.8 | Good |
| 2026-03-21 | https://example.com/page15 | 0.9 | Excellent |
```

**Analysis:**
- Simple `success_rate`: (0.6+0.7+0.65+0.7+0.6+0.75+0.65+0.7+0.6+0.7+0.85+0.9+0.85+0.8+0.9) / 15 = **0.78**
- Recent 10 mean: (0.6+0.7+0.65+0.7+0.6+0.75+0.65+0.7+0.6+0.7) / 10 = **0.665**
- Previous 5 mean: (0.85+0.9+0.85+0.8+0.9) / 5 = **0.86**
- Trend: 0.665 < 0.86 - 0.05 → **declining**
- Utility score (approximate, with 14-day half-life): Recent scores (0–12 days old) have weight ≈1.0, older scores (18–22 days old) have weight ≈0.4 → weighted mean ≈**0.71**

This skill would trigger the declining-skill gate (`score_trend=declining` AND `utility_score=0.71 < 0.80`) and be optimized, even though `success_rate=0.78` is above `underperformance_threshold=0.70`.

## Appendix B: /skill-health Output Specification

### Column Layout

```
{name:20} {symbol:1} {trend:12} {score:4}  ({total_runs:N} runs{opt_date})
```

Where:
- `{name:20}`: Left-aligned, max 20 chars, truncated with `…` if longer
- `{symbol:1}`: Single Unicode char: `▲` `▼` `◆` `—`
- `{trend:12}`: Left-aligned, one of: `improving  ` `declining  ` `stable     ` `new        `
- `{score:4}`: Right-aligned, `0.87` or `—   ` if null
- `{total_runs:N}`: Integer, no padding
- `{opt_date}`: Empty or `, last opt Apr 8` (abbreviated month, no year)

### Example Output (5 skills)

```
Skill health — 5 skills

summarize-repo      — new         —     (2 runs, need 3+ to score)
summarize-docs      ▼ declining   0.71  (8 runs) ⚠
summarize-paper     ◆ stable      0.82  (12 runs)
summarize-webpage   ▲ improving   0.87  (47 runs, last opt Apr 8)
chat                ◆ stable      0.91  (203 runs, last opt Mar 15)

⚠ = below underperformance threshold (0.70) or declining
```

### Mobile Rendering Constraints

- Max line width: 60 chars (fits on iPhone SE in portrait).
- Use monospace font (Telegram Markdown triple-backtick block).
- Test on narrowest target device before deploy.

### Warning Logic

Append ` ⚠` suffix to line if ANY of:
- `utility_score < config["skill_optimizer"]["underperformance_threshold"]` (default 0.70)
- `score_trend == "declining"`

Do NOT append warning for `score_trend == "insufficient-data"` (that's expected for new skills).

## Appendix C: Evolution Log Format Changes

### Before

```markdown
## Evolution Log

| Date       | Trigger | Prev Avg Score | New Avg Score | Change |
|------------|---------|----------------|---------------|--------|
| 2026-03-20 | underperformance (0.68) | 0.82 | 0.75 | Regression (rolled back) |
```

### After

```markdown
## Evolution Log

| Date       | Trigger | Prev Score (Raw/Util) | New Score (Raw/Util) | Change |
|------------|---------|------------------------|----------------------|--------|
| 2026-04-12 | declining (util=0.71) | 0.78 / 0.71 | 0.82 / 0.79 | Improved |
| 2026-03-20 | underperformance (0.68) | 0.82 / 0.75 | 0.75 / 0.68 | Regression (rolled back) |
```

**Columns:**
- **Date:** ISO 8601 `YYYY-MM-DD`
- **Trigger:** One of: `underperformance (util=0.65)` | `declining (util=0.71)` | `manual`
- **Prev Score:** `{success_rate:.2f} / {utility_score:.2f}` (both values for comparison)
- **New Score:** `{success_rate:.2f} / {utility_score:.2f}` (after optimization)
- **Change:** `Improved` | `Regression (rolled back)` | `No change`

This dual-score logging allows operators to audit the delta between raw mean and weighted mean, surfacing cases where recency weighting changes the optimization decision.

---

**End of Specification**
