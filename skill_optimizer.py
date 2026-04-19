import asyncio
import json
import logging
import os
import re

import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from litellm import acompletion

from llm_routes import resolve

log = logging.getLogger("skill-optimizer")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
SKILLS_DIR = BRAIN_DIR / "skills"
MEMORIES_DIR = BRAIN_DIR / "memories"
DEPLOY_DIR = Path.home() / "secondbrain"


class SkillOptimizer:
    """
    Daily pass: reads each skill's execution history, scores pending runs using
    LLM-as-judge, identifies failure patterns in low-scoring runs, rewrites the
    Instructions section to address them, maintains rolling backups, and appends
    to the Evolution Log.

    Implements TextGrad Critique-then-Edit (two LLM calls), OPRO trajectory via
    Evolution Log, DSPy auto-exemplars, and regression-triggered rollback.
    """

    def __init__(self, config: dict):
        self.config = config.get("skill_optimizer", {})
        self.run_hour = self.config.get("run_hour", 3)
        self.min_runs = self.config.get("min_runs_before_optimize", 10)
        self.underperformance_threshold = self.config.get("underperformance_threshold", 0.70)
        self.skip_above_threshold = self.config.get("skip_above_threshold", 0.90)
        self.regression_tolerance = self.config.get("regression_tolerance", 0.05)
        self.max_exemplars = self.config.get("max_exemplars", 2)
        self.max_history_rows = self.config.get("max_history_rows", 100)
        self.max_skill_backups = self.config.get("max_skill_backups", 5)
        self.judge_model = self.config.get("judge_model", "judge")
        self.dry_run = self.config.get("dry_run", False)
        self.half_life_days = self.config.get("half_life_days", 14)

        # FR-15 through FR-20: Real-time reflection
        self.realtime_judge = self.config.get("realtime_judge", False)
        self._urgent_queue: list[dict] = []  # populated by browser_watcher heuristic pre-filter
        self._judge_calls_this_hour: list[float] = []  # timestamps of recent judge calls
        self._skill_creator = None  # set by daemon.py if available

    async def run_loop(self, stop_event: asyncio.Event):
        """Main loop: schedule daily optimization passes at run_hour."""
        while not stop_event.is_set():
            # Reload config on each iteration to pick up changes
            try:
                config = yaml.safe_load((BRAIN_DIR / "config.yaml").read_text())
                self.__init__(config)
            except Exception as e:
                log.warning(f"Config reload failed: {e}")

            # Check if today's pass was missed (e.g. daemon restarted after run_hour)
            state_file = DEPLOY_DIR / "skill-optimizer-state.json"
            try:
                state = json.loads(state_file.read_text()) if state_file.exists() else {}
                last_pass = state.get("last_pass_date")
                today = datetime.now().strftime("%Y-%m-%d")
                now_hour = datetime.now().hour
                if last_pass != today and now_hour >= self.run_hour:
                    log.info(f"Missed pass detected (last={last_pass}) — running now")
                    await self._run_daily_pass(stop_event)
                    state["last_pass_date"] = today
                    state_file.write_text(json.dumps(state))
                    if stop_event.is_set():
                        break
            except Exception as e:
                log.warning(f"Missed-pass check failed: {e}")

            # Calculate sleep duration until next run_hour
            now = datetime.now()
            next_run = now.replace(hour=self.run_hour, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            sleep_seconds = (next_run - now).total_seconds()

            log.info(f"Next optimization pass scheduled for {next_run.isoformat()} "
                     f"(sleeping {sleep_seconds:.0f}s)")

            # Sleep until scheduled time or stop_event
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_seconds)
                # If we get here, stop_event was set
                break
            except asyncio.TimeoutError:
                # Woke up on schedule
                pass

            # Run the optimization pass
            if stop_event.is_set():
                break
            await self._run_daily_pass(stop_event)

    async def _run_daily_pass(self, stop_event: asyncio.Event):
        """One full optimization pass: merge logs, score, optimize eligible skills."""
        log.info("Starting daily optimization pass")

        # FR-2: Merge watcher node execution logs
        await self._merge_watcher_logs()

        if stop_event.is_set():
            return

        # Glob all skill files
        if not SKILLS_DIR.exists():
            log.warning(f"Skills directory not found: {SKILLS_DIR}")
            return

        skill_files = sorted(SKILLS_DIR.glob("*.md"))
        log.info(f"Found {len(skill_files)} skill files")

        for skill_path in skill_files:
            if stop_event.is_set():
                break

            skill_name = skill_path.stem

            # FR-4: Always skip skill-optimizer.md itself
            if skill_name == "skill-optimizer":
                log.debug(f"Skipped {skill_name} (meta-skill)")
                continue

            try:
                # FR-3: Score pending rows
                await self._score_pending_rows(skill_path, stop_event)

                # FR-9: Prune execution history
                await self._prune_execution_history(skill_path)

                # FR-8: Check for regression and rollback if needed
                rolled_back = await self._check_regression_and_rollback(skill_path)

                # FR-4: Check optimization gates
                should_optimize, reason = await self._check_optimization_gates(skill_path)

                if not should_optimize:
                    log.info(f"Skipped {skill_name}: {reason}")
                    continue

                log.info(f"Optimizing {skill_name}: {reason}")

                # FR-5, FR-6, FR-7: Critique, rewrite, auto-exemplars
                await self._optimize_skill(skill_path, stop_event)

            except Exception as e:
                log.error(f"Error processing {skill_name}: {e}", exc_info=True)
                continue

        log.info("Daily optimization pass complete")

        # Persist last_pass_date for missed-pass recovery
        try:
            state_file = DEPLOY_DIR / "skill-optimizer-state.json"
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
            state["last_pass_date"] = datetime.now().strftime("%Y-%m-%d")
            state_file.write_text(json.dumps(state))
        except Exception as e:
            log.warning(f"Could not save optimizer state: {e}")

        # Trigger probation graduation check if skill_creator is wired in
        if self._skill_creator is not None:
            try:
                await self._skill_creator.run_probation_check(self)
            except Exception as e:
                log.error(f"Probation check failed: {e}")

    async def _merge_watcher_logs(self):
        """FR-2: Merge watcher node JSONL logs into skill execution history tables."""
        logs_dir = BRAIN_DIR / "logs"
        if not logs_dir.exists():
            return

        jsonl_files = list(logs_dir.glob("*-execution-log.jsonl"))
        if not jsonl_files:
            log.debug("No watcher logs to merge")
            return

        log.info(f"Merging {len(jsonl_files)} watcher log files")

        for jsonl_file in jsonl_files:
            try:
                hostname = jsonl_file.stem.replace("-execution-log", "")
                records = []

                # Read all JSONL records
                for line_no, line in enumerate(jsonl_file.read_text().splitlines(), 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        log.warning(f"Skipping malformed JSONL line {line_no} in {jsonl_file.name}: {e}")
                        continue

                if not records:
                    # Empty file, just rename it
                    date_suffix = datetime.now().strftime("%Y-%m-%d")
                    processed_name = f"{hostname}-execution-log.processed-{date_suffix}.jsonl"
                    processed_path = logs_dir / processed_name
                    jsonl_file.rename(processed_path)
                    continue

                # Group by skill
                by_skill = {}
                for rec in records:
                    skill_name = rec.get("skill")
                    if not skill_name:
                        log.warning(f"Record missing 'skill' field: {rec}")
                        continue
                    by_skill.setdefault(skill_name, []).append(rec)

                # Append to each skill's execution history
                for skill_name, skill_records in by_skill.items():
                    skill_path = SKILLS_DIR / f"{skill_name}.md"
                    if not skill_path.exists():
                        log.warning(f"Skill file not found for watcher log records: {skill_name}")
                        continue

                    text = skill_path.read_text()

                    # Build rows
                    rows = []
                    for rec in skill_records:
                        row = f"| {rec['date']} | {rec['input_slug']} | {rec['model']} | {rec['score']} | {rec.get('notes', '')} |\n"
                        rows.append(row)

                    # Append to execution history
                    if "## Execution History" not in text:
                        text += (f"\n## Execution History\n\n"
                                 f"| date | input_slug | model | score | notes |\n"
                                 f"|------|-----------|-------|-------|-------|\n")
                        text += "".join(rows)
                    else:
                        lines = text.splitlines(keepends=True)
                        # Find last row in the execution history table
                        insert_at = len(lines)
                        for i in range(len(lines) - 1, -1, -1):
                            if lines[i].strip().startswith("|"):
                                insert_at = i + 1
                                break
                        for row in rows:
                            lines.insert(insert_at, row)
                            insert_at += 1
                        text = "".join(lines)

                    # Atomic write
                    self._atomic_write(skill_path, text)
                    log.info(f"Merged {len(skill_records)} records from {hostname} into {skill_name}")

                # Rename processed file
                date_suffix = datetime.now().strftime("%Y-%m-%d")
                processed_name = f"{hostname}-execution-log.processed-{date_suffix}.jsonl"
                processed_path = logs_dir / processed_name
                jsonl_file.rename(processed_path)

                # Delete processed files older than 30 days
                cutoff = datetime.now() - timedelta(days=30)
                for old_file in logs_dir.glob("*-execution-log.processed-*.jsonl"):
                    try:
                        # Extract date from filename
                        date_match = re.search(r'processed-(\d{4}-\d{2}-\d{2})\.jsonl$', old_file.name)
                        if date_match:
                            file_date = datetime.strptime(date_match.group(1), "%Y-%m-%d")
                            if file_date < cutoff:
                                old_file.unlink()
                                log.debug(f"Deleted old processed log: {old_file.name}")
                    except Exception as e:
                        log.warning(f"Error cleaning up old log {old_file.name}: {e}")

            except Exception as e:
                log.error(f"Error merging watcher log {jsonl_file.name}: {e}", exc_info=True)

    async def _score_pending_rows(self, skill_path: Path, stop_event: asyncio.Event):
        """FR-3: Score all pending execution history rows using LLM-as-judge."""
        text = skill_path.read_text()
        skill_name = skill_path.stem

        # Extract skill instructions
        instructions = self._extract_section(text, "## Instructions")
        if not instructions:
            log.warning(f"No Instructions section in {skill_name}")
            return

        # Find pending rows
        history_section = self._extract_section(text, "## Execution History")
        if not history_section:
            return

        lines = history_section.splitlines()
        pending_rows = []
        for i, line in enumerate(lines):
            if "| pending |" in line or line.strip().endswith("| pending |"):
                pending_rows.append((i, line))

        if not pending_rows:
            log.debug(f"No pending rows to score in {skill_name}")
            return

        # Chat skill responses are streamed to Telegram — no memory-file output to score against.
        # Mark pending rows as n/a to prevent permanent backlog growth.
        if skill_name == "chat":
            for row_idx, row_text in pending_rows:
                lines[row_idx] = row_text.replace("| pending |", "| n/a |")
            new_history = "\n".join(lines)
            text = self._replace_section(text, "## Execution History", new_history)
            self._atomic_write(skill_path, text)
            log.info(f"Marked {len(pending_rows)} chat rows as n/a (no memory-file output)")
            return

        if self.dry_run:
            log.info(f"DRY RUN: Would score {len(pending_rows)} pending rows for {skill_name}")
            return

        log.info(f"Scoring {len(pending_rows)} pending rows in {skill_name}")

        # Build memory lookup once per skill — avoids O(n*m) file reads
        # (503 rows × 1000 files = 500k reads through iCloud without this)
        memory_index = self._build_memory_index()

        # Score each pending row
        scored_count = 0
        for row_idx, row_text in pending_rows:
            if stop_event.is_set():
                break

            # Parse row
            parts = [p.strip() for p in row_text.split("|")]
            if len(parts) < 6:
                continue

            date_str = parts[1]
            input_slug = parts[2]
            model = parts[3]
            notes = parts[5] if len(parts) > 5 else ""

            # Find output by matching memory file (uses pre-built index)
            output_text = self._find_output_in_index(input_slug, memory_index)
            if not output_text:
                log.warning(f"No memory file found for {input_slug} in {skill_name} — leaving pending")
                continue

            # Call judge model
            try:
                score, reasoning = await self._call_judge(skill_name, instructions, input_slug,
                                                          output_text, date_str)

                # Update the row
                new_row = f"| {date_str} | {input_slug} | {model} | {score:.2f} | {reasoning[:120]} |"
                lines[row_idx] = new_row
                scored_count += 1

            except Exception as e:
                log.warning(f"Judge call failed for {input_slug}: {e}")
                continue

        if scored_count == 0:
            return

        # Rebuild execution history section
        new_history = "\n".join(lines)
        text = self._replace_section(text, "## Execution History", new_history)

        # Recalculate frontmatter stats
        text = await self._update_frontmatter_stats(text)

        # Atomic write
        self._atomic_write(skill_path, text)
        log.info(f"Scored {scored_count} rows in {skill_name}")

    async def _find_output_by_slug(self, input_slug: str) -> Optional[str]:
        """Find memory file matching input_slug and return its body.

        Matching strategy:
        1. Hash-based (primary): new-format slugs end with a 6-char hex url hash
           (e.g. "article-title-a1b2c3") — match against the hash suffix in memory
           filenames, which use the same SHA1(url)[:6] hash from memory_writer.py.
        2. URL-prefix (legacy): old-format slugs are the first 15 chars of the raw URL
           (e.g. "https://sfbay.c") — search memory file frontmatter for source_url
           that starts with the slug value.
        3. Filename substring fallback: original startswith/contains check on the
           filename slug component.
        """
        if not MEMORIES_DIR.exists():
            return None

        # Check whether input_slug ends with a 6-char hex hash (new format)
        hash_match = re.search(r'([0-9a-f]{6})$', input_slug)

        # Check whether input_slug looks like a URL prefix (legacy format)
        is_url_prefix = input_slug.startswith(("https://", "http://"))

        for mem_file in MEMORIES_DIR.glob("*.md"):
            try:
                content = mem_file.read_text()
            except Exception:
                continue

            matched = False

            # Strategy 1: hash-based matching (new slugs with url hash suffix)
            if hash_match:
                name_parts = mem_file.stem.split("-", 3)
                if len(name_parts) >= 4:
                    file_hash = re.search(r'([0-9a-f]{6})$', name_parts[3])
                    if file_hash and file_hash.group(1) == hash_match.group(1):
                        matched = True

            # Strategy 2: URL-prefix matching against frontmatter source_url (legacy)
            if not matched and is_url_prefix:
                url_match = re.search(r'source_url:\s*(\S+)', content)
                if url_match and url_match.group(1).startswith(input_slug):
                    matched = True

            # Strategy 3: filename substring fallback
            if not matched:
                name_parts = mem_file.stem.split("-", 3)
                if len(name_parts) >= 4:
                    file_slug = name_parts[3]
                    if file_slug.startswith(input_slug) or input_slug in file_slug:
                        matched = True

            if matched:
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()

        return None

    def _build_memory_index(self) -> list[dict]:
        """Pre-scan all memory files once, returning a list of dicts with slug, url, and body.

        Calling _find_output_by_slug per row scans the full MEMORIES_DIR on every call —
        O(n*m) file reads through iCloud. Building the index once reduces it to O(m) reads
        regardless of how many pending rows need scoring.
        """
        index = []
        if not MEMORIES_DIR.exists():
            return index
        for mem_file in MEMORIES_DIR.glob("*.md"):
            try:
                content = mem_file.read_text()
            except Exception:
                continue
            # Extract body (after frontmatter)
            fm_parts = content.split("---", 2)
            body = fm_parts[2].strip() if len(fm_parts) >= 3 else ""
            if not body:
                continue
            # Extract filename slug component
            name_parts = mem_file.stem.split("-", 3)
            file_slug = name_parts[3] if len(name_parts) >= 4 else ""
            # Extract source_url from frontmatter for URL-prefix matching
            url_match = re.search(r'source_url:\s*(\S+)', content)
            source_url = url_match.group(1) if url_match else ""
            index.append({"file_slug": file_slug, "source_url": source_url, "body": body})
        return index

    def _find_output_in_index(self, input_slug: str, index: list[dict]) -> Optional[str]:
        """Match input_slug against pre-built memory index. Three strategies (same as _find_output_by_slug):
        1. Hash-based: slug ends with 6-char hex hash → match against file_slug hash suffix
        2. URL-prefix: slug starts with http → match against source_url prefix
        3. Substring: fallback to startswith/contains on file_slug
        """
        hash_match = re.search(r'([0-9a-f]{6})$', input_slug)
        is_url_prefix = input_slug.startswith(("https://", "http://"))

        for entry in index:
            matched = False
            if hash_match:
                file_hash = re.search(r'([0-9a-f]{6})$', entry["file_slug"])
                if file_hash and file_hash.group(1) == hash_match.group(1):
                    matched = True
            if not matched and is_url_prefix and entry["source_url"].startswith(input_slug):
                matched = True
            if not matched and entry["file_slug"] and (
                entry["file_slug"].startswith(input_slug) or input_slug in entry["file_slug"]
            ):
                matched = True
            if matched:
                return entry["body"]
        return None

    async def _call_judge(self, skill_name: str, instructions: str, input_slug: str,
                          output_text: str, date_str: str) -> tuple[float, str]:
        """Call judge model to score an execution."""
        prompt = f"""You are evaluating the quality of a skill output.

Skill: {skill_name}
Skill instructions (what a good output should look like):
{instructions}

Execution date: {date_str}
Input: {input_slug}
Output:
{output_text}

Rate this output on a scale of 0.0 to 1.0 using this rubric:
- 1.0: Excellent — fully satisfies all instructions, well-structured, complete
- 0.7: Acceptable — minor gaps or format issues, still useful
- 0.5: Weak — missing key content or poorly structured
- 0.0: Failed — junk, empty, or seriously wrong output

Respond with JSON only:
{{"score": 0.0, "reasoning": "one sentence explanation"}}"""

        response = await asyncio.wait_for(
            acompletion(
                model=resolve(self.judge_model),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
            ),
            timeout=30
        )

        result_text = response.choices[0].message.content.strip()

        # Parse JSON
        # Try to extract JSON from markdown code block if present
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0].strip()
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0].strip()

        result = json.loads(result_text)
        score = float(result["score"])
        reasoning = result.get("reasoning", "")

        return score, reasoning

    # ── Utility scoring (feat-skill-utility-scoring) ──────────────────────────

    def _parse_history_rows(self, history_section: str) -> list[dict]:
        """Extract [{date, score}] from execution history table (non-pending rows only)."""
        rows = []
        for line in history_section.splitlines():
            if not line.strip().startswith("|") or "| date |" in line or "|---" in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue
            date_str = parts[1]
            score_str = parts[4]
            if score_str in ("pending", "n/a") or not score_str:
                continue
            try:
                score = float(score_str)
                if score >= 0:
                    rows.append({"date": date_str, "score": score})
            except ValueError:
                continue
        return rows

    def _compute_utility_score(
        self, rows: list[dict], half_life_days: float = 14.0
    ) -> Optional[float]:
        """Recency-weighted mean using half-life decay. Returns None if < 3 numeric scores."""
        if len(rows) < 3:
            return None
        today = datetime.now().date()
        total_weight = 0.0
        weighted_sum = 0.0
        for row in rows:
            try:
                row_date = datetime.strptime(row["date"][:10], "%Y-%m-%d").date()
                days_old = max(0, (today - row_date).days)
            except (ValueError, KeyError):
                days_old = 0
            weight = 1.0 / (1.0 + days_old / half_life_days)
            weighted_sum += row["score"] * weight
            total_weight += weight
        if total_weight == 0:
            return None
        return round(weighted_sum / total_weight, 2)

    def _compute_trend(self, rows: list[dict]) -> str:
        """Compare recent (last 10) vs previous (11-20) windows of numeric scores."""
        if len(rows) < 5:
            return "insufficient-data"
        # rows are ordered oldest-first from history table; reverse for recency
        recent_rows = rows[-10:]
        previous_rows = rows[-20:-10] if len(rows) >= 11 else []
        if len(recent_rows) < 5 or len(previous_rows) < 5:
            return "insufficient-data"
        recent_mean = sum(r["score"] for r in recent_rows) / len(recent_rows)
        previous_mean = sum(r["score"] for r in previous_rows) / len(previous_rows)
        diff = recent_mean - previous_mean
        if diff > 0.05:
            return "improving"
        if diff < -0.05:
            return "declining"
        return "stable"

    # ── Frontmatter stats ─────────────────────────────────────────────────────

    async def _update_frontmatter_stats(self, text: str) -> str:
        """Recalculate success_rate, utility_score, and score_trend from execution history."""
        history_section = self._extract_section(text, "## Execution History")
        if not history_section:
            return text

        rows = self._parse_history_rows(history_section)
        scores = [r["score"] for r in rows]

        # Update frontmatter
        parts = text.split("---", 2)
        if len(parts) < 3:
            return text

        fm = yaml.safe_load(parts[1])

        # Backwards-compat: keep success_rate (simple mean)
        fm["total_runs"] = len(scores)
        fm["success_rate"] = round(sum(scores) / len(scores), 2) if scores else None

        # Recency-weighted utility score and trend (new)
        half_life = fm.get("half_life_days", self.half_life_days)
        utility = self._compute_utility_score(rows, half_life_days=half_life)
        trend = self._compute_trend(rows)
        if utility is not None:
            fm["utility_score"] = utility
            fm["utility_score_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            fm["score_trend"] = trend
        elif "utility_score" not in fm:
            # Leave existing utility_score alone if rows < 3 but field already present
            fm["score_trend"] = trend

        parts[1] = yaml.dump(fm, sort_keys=False)
        return "---".join(parts)

    async def _prune_execution_history(self, skill_path: Path):
        """FR-9: Prune execution history to max_history_rows, keeping pending rows."""
        text = skill_path.read_text()
        history_section = self._extract_section(text, "## Execution History")
        if not history_section:
            return

        lines = history_section.splitlines()

        # Separate header, pending, and scored rows
        header_lines = []
        pending_rows = []
        scored_rows = []

        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            if "| date |" in line or "|---" in line:
                header_lines.append(line)
            elif "| pending |" in line or stripped.endswith("| pending |"):
                pending_rows.append(line)
            else:
                scored_rows.append(line)

        # Keep only newest max_history_rows scored rows
        if len(scored_rows) > self.max_history_rows:
            scored_rows = scored_rows[-self.max_history_rows:]

            # Rebuild section
            new_lines = header_lines + scored_rows + pending_rows
            new_history = "\n".join(new_lines)
            text = self._replace_section(text, "## Execution History", new_history)

            if self.dry_run:
                log.info(f"DRY RUN: Would prune {skill_path.stem} execution history to {self.max_history_rows} rows")
                return

            self._atomic_write(skill_path, text)
            log.info(f"Pruned {skill_path.stem} execution history to {self.max_history_rows} rows")

    async def _check_regression_and_rollback(self, skill_path: Path) -> bool:
        """FR-8: Check for regression and rollback if needed. Returns True if rolled back."""
        text = skill_path.read_text()
        parts = text.split("---", 2)
        if len(parts) < 3:
            return False

        fm = yaml.safe_load(parts[1])
        # Prefer utility_score for regression comparison; fall back to success_rate
        current_rate = fm.get("utility_score") or fm.get("success_rate")
        prev_rate = fm.get("prev_version_avg_score")

        if current_rate is None or prev_rate is None:
            return False

        # Check for regression
        if current_rate < (prev_rate - self.regression_tolerance):
            log.warning(f"Regression detected in {skill_path.stem}: "
                        f"{current_rate:.2f} < {prev_rate:.2f} - {self.regression_tolerance}")

            if self.dry_run:
                log.info(f"DRY RUN: Would rollback {skill_path.stem} from backup")
                return True

            # Rollback from .1 backup
            backup_path = skill_path.with_suffix(skill_path.suffix + ".1")
            if not backup_path.exists():
                log.warning(f"Cannot rollback {skill_path.stem}: no .1 backup found")
                return False

            # Restore backup
            backup_content = backup_path.read_text()
            self._atomic_write(skill_path, backup_content)

            # Reverse-rotate backups (.2 → .1, .3 → .2, etc.)
            for i in range(2, self.max_skill_backups + 1):
                src = skill_path.with_suffix(f"{skill_path.suffix}.{i}")
                dst = skill_path.with_suffix(f"{skill_path.suffix}.{i-1}")
                if src.exists():
                    src.rename(dst)

            # Append rollback entry to Evolution Log
            text = skill_path.read_text()
            version = self._get_version(text)
            rollback_entry = f"\n### v{version} → v{version-1} rollback ({datetime.now().strftime('%Y-%m-%d')})\n"
            rollback_entry += f"**Reason:** score dropped from {prev_rate:.2f} to {current_rate:.2f} (regression_tolerance={self.regression_tolerance})\n"
            rollback_entry += f"**Action:** Restored from {skill_path.name}.1\n"

            text = self._append_to_evolution_log(text, rollback_entry)
            self._atomic_write(skill_path, text)

            log.info(f"Rolled back {skill_path.stem} to version {version-1}")
            return True

        return False

    async def _check_optimization_gates(self, skill_path: Path) -> tuple[bool, str]:
        """FR-4: Check if skill should be optimized. Returns (should_optimize, reason)."""
        text = skill_path.read_text()
        parts = text.split("---", 2)
        if len(parts) < 3:
            return False, "invalid frontmatter"

        # Skip probation skills — their executions are the training signal, not noise
        if "status: probation" in text or "status: failed" in text:
            return False, "skill is in probation or failed state"

        fm = yaml.safe_load(parts[1])
        success_rate = fm.get("success_rate")
        utility_score = fm.get("utility_score")
        score_trend = fm.get("score_trend", "")
        total_runs = fm.get("total_runs", 0)
        last_optimized = fm.get("last_optimized")

        # Use utility_score for thresholds; fall back to success_rate if not yet computed
        effective_score = utility_score if utility_score is not None else success_rate

        # Priority gate: declining skills bypass min_runs and cadence checks
        if score_trend == "declining" and utility_score is not None and utility_score < 0.80:
            return True, f"declining skill prioritised (utility_score={utility_score:.2f})"

        # Gate 1: Minimum runs
        if total_runs < self.min_runs:
            return False, f"total_runs={total_runs} < min_runs={self.min_runs}"

        # Gate 2: Not excellent (skip if performing well)
        if effective_score is not None and effective_score >= self.skip_above_threshold:
            score_label = "utility_score" if utility_score is not None else "success_rate"
            return False, f"{score_label}={effective_score:.2f} >= skip_above_threshold={self.skip_above_threshold}"

        # Gate 3: Underperforming (only optimise if below threshold)
        if effective_score is not None and effective_score >= self.underperformance_threshold:
            score_label = "utility_score" if utility_score is not None else "success_rate"
            return False, f"{score_label}={effective_score:.2f} >= underperformance_threshold={self.underperformance_threshold}"

        # Gate 4: Enough runs since last optimization
        if last_optimized:
            # Count runs since last_optimized date
            history_section = self._extract_section(text, "## Execution History")
            if history_section:
                runs_since = 0
                for line in history_section.splitlines():
                    if not line.strip().startswith("|") or "| date |" in line or "|---" in line:
                        continue
                    parts_list = [p.strip() for p in line.split("|")]
                    if len(parts_list) < 2:
                        continue
                    date_str = parts_list[1]
                    if date_str > last_optimized:
                        runs_since += 1

                if runs_since < self.min_runs:
                    return False, f"runs_since_last_optimized={runs_since} < min_runs={self.min_runs}"

        score_label = "utility_score" if utility_score is not None else "success_rate"
        score_val = effective_score if effective_score is not None else "unknown"
        return True, f"{score_label}={score_val} < {self.underperformance_threshold}"

    async def _optimize_skill(self, skill_path: Path, stop_event: asyncio.Event):
        """FR-5, FR-6, FR-7: Critique, rewrite, and update skill with auto-exemplars."""
        text = skill_path.read_text()
        skill_name = skill_path.stem

        # FR-8: Backup before optimization
        if not self.dry_run:
            await self._rotate_backups(skill_path)

        # FR-5: Generate critique
        critique = await self._generate_critique(skill_path)
        if not critique:
            log.warning(f"Critique generation failed for {skill_name}")
            return

        if self.dry_run:
            log.info(f"DRY RUN: Would optimize {skill_name} — critique: {critique.get('root_cause', 'N/A')}")

        # FR-6: Rewrite instructions
        new_text = await self._rewrite_skill(skill_path, critique)
        if not new_text:
            log.info(f"No change proposed for {skill_name} — skipping write")
            return

        if self.dry_run:
            new_instructions = self._extract_section(new_text, "## Instructions")
            log.info(f"DRY RUN: Proposed new Instructions: {new_instructions[:200]}...")
            return

        # FR-7: Add auto-exemplars if eligible
        new_text = await self._add_auto_exemplars(skill_path, new_text)

        # Store prev_version_avg_score for regression detection
        parts = text.split("---", 2)
        fm = yaml.safe_load(parts[1])
        prev_rate = fm.get("success_rate")

        new_parts = new_text.split("---", 2)
        new_fm = yaml.safe_load(new_parts[1])
        new_fm["prev_version_avg_score"] = prev_rate
        new_fm["last_optimized"] = datetime.now().strftime("%Y-%m-%d")
        new_parts[1] = yaml.dump(new_fm, sort_keys=False)
        new_text = "---".join(new_parts)

        # Write
        self._atomic_write(skill_path, new_text)
        log.info(f"Optimized {skill_name} — new version {new_fm['version']}")

    async def _rotate_backups(self, skill_path: Path):
        """FR-8: Rotate backup files logrotate-style."""
        if self.dry_run:
            log.info(f"DRY RUN: Would backup {skill_path.name} → {skill_path.name}.1")
            return

        # Delete oldest backup
        oldest = skill_path.with_suffix(f"{skill_path.suffix}.{self.max_skill_backups}")
        if oldest.exists():
            oldest.unlink()

        # Rotate existing backups
        for i in range(self.max_skill_backups - 1, 0, -1):
            src = skill_path.with_suffix(f"{skill_path.suffix}.{i}")
            dst = skill_path.with_suffix(f"{skill_path.suffix}.{i + 1}")
            if src.exists():
                src.rename(dst)

        # Copy current to .1
        backup_path = skill_path.with_suffix(f"{skill_path.suffix}.1")
        backup_path.write_text(skill_path.read_text())
        log.debug(f"Backed up {skill_path.name} to {backup_path.name}")

    async def _generate_critique(self, skill_path: Path) -> Optional[dict]:
        """FR-5: Generate critique using optimizer model."""
        text = skill_path.read_text()
        instructions = self._extract_section(text, "## Instructions")
        evolution_log = self._extract_section(text, "## Evolution Log") or ""

        # Get low-scoring and high-scoring examples
        low_examples = await self._get_execution_examples(skill_path, max_score=self.underperformance_threshold)
        high_examples = await self._get_execution_examples(skill_path, min_score=0.80, limit=5)

        if not low_examples:
            log.warning(f"No low-scoring examples found for {skill_path.stem}")
            return None

        prompt = f"""You are analyzing a prompt template to identify why some executions score poorly.

Current skill instructions:
{instructions}

Evolution log (prior optimization attempts):
{evolution_log}

Low-scoring executions (score < {self.underperformance_threshold}):
{self._format_examples(low_examples)}

High-scoring executions for contrast:
{self._format_examples(high_examples)}

Your output must be a JSON object:
{{
  "failure_patterns": ["pattern 1", "pattern 2"],
  "root_cause": "one sentence summary of the core issue",
  "suggested_focus": "what the rewrite should specifically address"
}}

Be specific. Cite evidence from the execution examples. Avoid generic observations."""

        try:
            response = await asyncio.wait_for(
                acompletion(
                    model=resolve("optimizer"),
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=500,
                ),
                timeout=60
            )

            result_text = response.choices[0].message.content.strip()

            # Extract JSON
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            critique = json.loads(result_text)

            # Validate required fields
            if not critique.get("failure_patterns") or not critique.get("root_cause"):
                log.warning(f"Critique missing required fields: {critique}")
                return None

            return critique

        except Exception as e:
            log.warning(f"Critique generation failed: {e}")
            return None

    async def _get_execution_examples(self, skill_path: Path, min_score: float = 0.0,
                                      max_score: float = 1.0, limit: int = 10) -> list[dict]:
        """Get execution examples with scores in the given range."""
        text = skill_path.read_text()
        history_section = self._extract_section(text, "## Execution History")
        if not history_section:
            return []

        examples = []
        for line in history_section.splitlines():
            if not line.strip().startswith("|") or "| date |" in line or "|---" in line:
                continue

            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue

            score_str = parts[4]
            if score_str == "pending":
                continue

            try:
                score = float(score_str)
                if min_score <= score <= max_score:
                    input_slug = parts[2]
                    output = await self._find_output_by_slug(input_slug)
                    if output:
                        examples.append({
                            "date": parts[1],
                            "input_slug": input_slug,
                            "score": score,
                            "output": output[:500]  # Truncate to keep prompt manageable
                        })
            except ValueError:
                continue

        # Return most recent examples
        return examples[-limit:] if examples else []

    def _format_examples(self, examples: list[dict]) -> str:
        """Format execution examples for prompt."""
        if not examples:
            return "(none)"

        formatted = []
        for ex in examples:
            formatted.append(f"Date: {ex['date']}, Score: {ex['score']:.2f}\n"
                             f"Input: {ex['input_slug']}\n"
                             f"Output: {ex['output']}\n")
        return "\n".join(formatted)

    async def _rewrite_skill(self, skill_path: Path, critique: dict) -> Optional[str]:
        """FR-6: Rewrite skill using meta-skill and critique."""
        text = skill_path.read_text()
        current_instructions = self._extract_section(text, "## Instructions")

        # Load meta-skill
        meta_skill_path = SKILLS_DIR / "skill-optimizer.md"
        if not meta_skill_path.exists():
            log.error("skill-optimizer.md not found")
            return None

        meta_text = meta_skill_path.read_text()
        meta_instructions = self._extract_section(meta_text, "## Instructions")

        # Build rewrite prompt
        critique_json = json.dumps(critique, indent=2)
        user_msg = f"""Current skill file:
{text}

Critique from analysis:
{critique_json}

Please rewrite the skill file following the meta-skill instructions."""

        try:
            response = await asyncio.wait_for(
                acompletion(
                    model=resolve("optimizer"),
                    messages=[
                        {"role": "system", "content": meta_instructions},
                        {"role": "user", "content": user_msg}
                    ],
                    max_tokens=2000,
                ),
                timeout=60
            )

            new_skill_text = response.choices[0].message.content.strip()

            # Extract if wrapped in markdown
            if "```markdown" in new_skill_text:
                new_skill_text = new_skill_text.split("```markdown")[1].split("```")[0].strip()
            elif "```" in new_skill_text:
                # Try to extract the skill file from code block
                parts = new_skill_text.split("```")
                if len(parts) >= 3:
                    new_skill_text = parts[1].strip()

            # Validate structure
            new_instructions = self._extract_section(new_skill_text, "## Instructions")
            if not new_instructions:
                log.warning(f"Rewritten skill missing Instructions section — "
                            f"response preview: {new_skill_text[:300]!r}")
                return None

            # Check if instructions actually changed
            if new_instructions.strip() == current_instructions.strip():
                return None

            # Increment version in frontmatter
            parts = new_skill_text.split("---", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1])
                fm["version"] = fm.get("version", 1) + 1
                parts[1] = yaml.dump(fm, sort_keys=False)
                new_skill_text = "---".join(parts)

            return new_skill_text

        except Exception as e:
            log.error(f"Rewrite failed: {e}", exc_info=True)
            return None

    async def _add_auto_exemplars(self, skill_path: Path, text: str) -> str:
        """FR-7: Add Top Examples section if skill is exemplar_eligible."""
        parts = text.split("---", 2)
        if len(parts) < 3:
            return text

        fm = yaml.safe_load(parts[1])
        if not fm.get("exemplar_eligible", False):
            return text

        # Get top-scoring examples
        top_examples = await self._get_execution_examples(
            skill_path,
            min_score=0.70,
            limit=self.max_exemplars
        )

        # Sort by score descending
        top_examples.sort(key=lambda x: x["score"], reverse=True)
        top_examples = top_examples[:self.max_exemplars]

        if len(top_examples) < 1:
            # Not enough examples, don't add section
            return text

        # Build Top Examples section content (without header)
        section_lines = ["<!-- Auto-managed by optimizer. Do not edit manually. -->"]

        for i, ex in enumerate(top_examples, 1):
            section_lines.append(f"### Example {i} (score: {ex['score']:.2f}, {ex['date']})")
            section_lines.append(f"**Input:** {ex['input_slug']}")
            section_lines.append("**Output:**")
            section_lines.append(ex["output"])
            section_lines.append("")

        new_section_content = "\n".join(section_lines)

        # Replace or insert section after Instructions
        if "## Top Examples" in text:
            text = self._replace_section(text, "## Top Examples", new_section_content)
        else:
            new_section = "## Top Examples\n" + new_section_content
            # Insert after Instructions section
            instructions_end = text.find("## Instructions")
            if instructions_end >= 0:
                # Find next section or end
                next_section = text.find("\n## ", instructions_end + len("## Instructions"))
                if next_section >= 0:
                    text = text[:next_section] + "\n" + new_section + "\n" + text[next_section:]
                else:
                    text = text + "\n" + new_section + "\n"

        return text

    def _extract_section(self, text: str, section_header: str) -> Optional[str]:
        """Extract a markdown section by header."""
        pattern = rf'^{re.escape(section_header)}\n(.*?)(?=\n## |\Z)'
        match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        return match.group(1).strip() if match else None

    def _replace_section(self, text: str, section_header: str, new_content: str) -> str:
        """Replace a markdown section's content."""
        pattern = rf'^({re.escape(section_header)}\n).*?((?=\n## )|\Z)'
        replacement = rf'\1{new_content}\n\2'
        return re.sub(pattern, replacement, text, flags=re.MULTILINE | re.DOTALL)

    def _append_to_evolution_log(self, text: str, entry: str) -> str:
        """Append an entry to the Evolution Log section."""
        if "## Evolution Log" not in text:
            text += f"\n## Evolution Log\n{entry}\n"
        else:
            # Find the end of Evolution Log section
            evo_start = text.find("## Evolution Log")
            next_section = text.find("\n## ", evo_start + len("## Evolution Log"))

            if next_section >= 0:
                text = text[:next_section] + "\n" + entry + "\n" + text[next_section:]
            else:
                text = text + "\n" + entry + "\n"

        return text

    def _get_version(self, text: str) -> int:
        """Extract version number from frontmatter."""
        parts = text.split("---", 2)
        if len(parts) < 3:
            return 1
        fm = yaml.safe_load(parts[1])
        return fm.get("version", 1)

    def add_to_urgent_queue(self, entry: dict):
        """Called by BrowserWatcher after heuristic pre-filter flags an output.

        entry keys: skill_name, execution_timestamp, flag_reason, input_snippet, output_snippet
        """
        if len(self._urgent_queue) >= 20:
            # Only add entries for skills already in the queue
            existing_skills = {e["skill_name"] for e in self._urgent_queue}
            if entry["skill_name"] not in existing_skills:
                log.debug(f"Urgent queue capped, skipping new skill: {entry['skill_name']}")
                return

        self._urgent_queue.append(entry)
        log.info(f"Urgent queue: {entry['skill_name']} flagged as {entry['flag_reason']} "
                 f"(queue size: {len(self._urgent_queue)})")

        # Optionally fire judge call
        if self.realtime_judge:
            import asyncio
            asyncio.create_task(self._realtime_judge_call(entry))

    async def run_urgent_loop(self, stop_event: asyncio.Event):
        """Hourly processor: rewrites skills with ≥2 flagged executions in the urgent queue."""
        while not stop_event.is_set():
            # Sleep 60 minutes, but wake on stop_event
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3600)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass

            if stop_event.is_set():
                break

            await self._process_urgent_queue(stop_event)

    async def _process_urgent_queue(self, stop_event: asyncio.Event):
        """Process skills in the urgent queue, rewriting those with ≥2 flags."""
        if not self._urgent_queue:
            return

        log.info(f"Processing urgent queue: {len(self._urgent_queue)} entries")

        # Group by skill name
        by_skill: dict[str, list[dict]] = {}
        for entry in self._urgent_queue:
            by_skill.setdefault(entry["skill_name"], []).append(entry)

        rewrote_count = 0
        processed_skills = []

        for skill_name, entries in by_skill.items():
            if stop_event.is_set():
                break
            if rewrote_count >= 3:
                log.info("Urgent queue: rewrite cap (3 skills/tick) reached")
                break

            skill_path = SKILLS_DIR / f"{skill_name}.md"
            if not skill_path.exists():
                log.warning(f"Urgent queue: skill file not found: {skill_name}")
                processed_skills.append(skill_name)
                continue

            # Check if skill is in probation — skip probation skills
            try:
                text = skill_path.read_text()
                if "status: probation" in text:
                    log.debug(f"Urgent queue: skipping probation skill {skill_name}")
                    processed_skills.append(skill_name)
                    continue
            except Exception:
                pass

            should_rewrite = len(entries) >= 2

            if not should_rewrite:
                # Single entry: only rewrite if the same flag appeared in nightly history
                flag = entries[0]["flag_reason"]
                should_rewrite = self._appeared_in_nightly_history(skill_path, flag)

            if should_rewrite:
                try:
                    log.info(f"Urgent rewrite: {skill_name} ({len(entries)} flags: "
                             f"{[e['flag_reason'] for e in entries]})")
                    await self._optimize_skill(skill_path, stop_event)
                    rewrote_count += 1
                except Exception as e:
                    log.error(f"Urgent rewrite failed for {skill_name}: {e}")

            processed_skills.append(skill_name)

        # Remove processed entries from queue
        self._urgent_queue = [
            e for e in self._urgent_queue
            if e["skill_name"] not in processed_skills
        ]
        log.info(f"Urgent queue processed: {rewrote_count} rewrites, "
                 f"{len(self._urgent_queue)} entries remaining")

    def _appeared_in_nightly_history(self, skill_path: Path, flag_reason: str) -> bool:
        """Check if this flag_reason appeared in the skill's nightly score history.
        Returns True if the skill has low-scored executions in its history."""
        try:
            text = skill_path.read_text()
            # Check execution history for low scores — proxy for the same failure pattern
            history_match = re.search(r'## Execution History.*?(?=\n##|\Z)', text, re.DOTALL)
            if not history_match:
                return False
            history = history_match.group(0)
            # Find rows with score < 0.5
            low_score_rows = re.findall(r'\|\s*[\d-]+\s*\|[^|]+\|[^|]+\|\s*(0\.[0-4]\d*)\s*\|', history)
            return len(low_score_rows) >= 1
        except Exception:
            return False

    async def _realtime_judge_call(self, entry: dict):
        """Optional async judge call for flagged executions. Rate-limited to 5/hour."""
        import time
        now = time.time()
        # Prune old timestamps
        self._judge_calls_this_hour = [t for t in self._judge_calls_this_hour if now - t < 3600]

        if len(self._judge_calls_this_hour) >= 5:
            log.warning(f"Realtime judge rate limit reached (5/hour), skipping for {entry['skill_name']}")
            return

        self._judge_calls_this_hour.append(now)

        prompt = f"""You are evaluating the quality of a skill output.

Skill: {entry['skill_name']}
Output:
{entry['output_snippet']}

Rate this output on a scale of 1 to 5:
- 5: Excellent — well-structured, complete, useful
- 4: Good — minor issues, still useful
- 3: Acceptable — some problems but salvageable
- 2: Poor — missing key content or badly structured
- 1: Failed — junk, empty, or seriously wrong

Respond with JSON only: {{"score": 1, "reasoning": "one sentence"}}"""

        try:
            response = await asyncio.wait_for(
                acompletion(
                    model=resolve(self.judge_model),
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=100,
                ),
                timeout=30
            )
            text = response.choices[0].message.content.strip()
            data = json.loads(text)
            score = int(data.get("score", 3))

            log.info(f"Realtime judge: {entry['skill_name']} score={score} ({data.get('reasoning', '')})")

            if score <= 2:
                # Force add to queue (bypass the normal rules)
                if len(self._urgent_queue) < 20:
                    self._urgent_queue.append(entry)
                    log.info(f"Judge score {score} — forced queue add: {entry['skill_name']}")
            elif score >= 4:
                # False positive — remove from queue if present
                self._urgent_queue = [
                    e for e in self._urgent_queue
                    if not (e["skill_name"] == entry["skill_name"] and
                            e["execution_timestamp"] == entry["execution_timestamp"])
                ]
                log.info(f"Judge score {score} — false positive, removed from queue: {entry['skill_name']}")

        except Exception as e:
            log.warning(f"Realtime judge call failed for {entry['skill_name']}: {e}")

    def _atomic_write(self, path: Path, content: str):
        """FR-11: Atomic file write using temp file + rename."""
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content)
        os.rename(tmp_path, path)
