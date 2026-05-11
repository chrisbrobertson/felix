"""
goal_project_agent.py — 14th async loop (full role only).

Periodically checks all active goals/projects for new related memories, calls
an LLM to generate a report + proposed actions for each, writes proposed actions
as flat files in the memories dir, and sends urgent pings via Telegram.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from llm_routes import resolve
from usage_tracker import record_usage
from utils import load_config
from heartbeat import record_beat

log = logging.getLogger("goal-agent")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter from markdown file content."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _slugify(text: str, max_len: int = 40) -> str:
    """Generate a URL-friendly slug from text."""
    s = text.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s[:max_len].rstrip('-')


def _validate_target_name(target_name: str) -> None:
    """Raise ValueError if target_name looks like a path traversal attempt."""
    if not target_name:
        return
    if ".." in target_name or "/" in target_name or "\\" in target_name:
        raise ValueError(f"Invalid target path: {target_name!r}")


def _title_similarity(title1: str, title2: str) -> float:
    """Compute title similarity as Jaccard index of normalized token sets."""
    tokens1 = set(re.findall(r'[a-z0-9]+', title1.lower()))
    tokens2 = set(re.findall(r'[a-z0-9]+', title2.lower()))
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    return len(intersection) / len(union) if union else 0.0


# ── GoalProjectAgent ──────────────────────────────────────────────────────────

class GoalProjectAgent:
    """Fourteenth async loop: generates reports and actions for active goals/projects."""

    def __init__(self, role: str = "full", cache=None):
        self.role = role
        self.notification_callback = None  # Set by daemon.py
        self.STATE_FILE = DEPLOY_DIR / "goal-agent-state.json"
        self.REJECTED_ACTIONS_FILE = DEPLOY_DIR / "rejected-actions.json"
        # Cache: MemoryCache instance for queries, or None (defaults to pass-through)
        if cache is None:
            from memory_cache import MemoryCache
            cache = MemoryCache(None, MEMORIES_DIR, enabled=False)
        self._cache = cache

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        """Load config from BRAIN_DIR/config.yaml."""
        return load_config(CONFIG_PATH)

    def _agent_config(self) -> dict:
        """Return goal_agent section from config."""
        return self._load_config().get("goal_agent", {})

    # ── State ─────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load state from STATE_FILE."""
        if self.STATE_FILE.exists():
            try:
                return json.loads(self.STATE_FILE.read_text())
            except Exception:
                pass
        return {"goals": {}, "projects": {}}

    def _save_state(self, state: dict) -> None:
        """Save state to STATE_FILE atomically."""
        tmp = self.STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(self.STATE_FILE))
        except Exception as e:
            log.warning("Failed to save goal agent state: %s", e)

    # ── Item selection ────────────────────────────────────────────────────────

    async def _select_items(self) -> list:
        """Walk MEMORIES_DIR for active goals/projects. Returns list of (path, fm_dict)."""
        items = []

        # Goals
        goal_rows = await self._cache.query_by_type("goal", status="active")
        for row in goal_rows:
            fm = json.loads(row["frontmatter"])
            if fm.get("agent") is False:
                continue
            path = MEMORIES_DIR / row["filename"]
            items.append((path, fm))

        # Projects
        ac = self._agent_config()
        include_on_hold = ac.get("include_on_hold_projects", False)

        # Active projects
        active_rows = await self._cache.query_by_type("project", status="active")
        for row in active_rows:
            # Skip candidates by filename prefix
            if row["filename"].startswith("project-candidate-"):
                continue
            fm = json.loads(row["frontmatter"])
            # Exclude code repos (category == "code" is now type == "code")
            if fm.get("category") == "code":
                continue
            if fm.get("agent") is False:
                continue
            path = MEMORIES_DIR / row["filename"]
            items.append((path, fm))

        # On-hold projects if configured
        if include_on_hold:
            onhold_rows = await self._cache.query_by_type("project", status="on-hold")
            for row in onhold_rows:
                if row["filename"].startswith("project-candidate-"):
                    continue
                fm = json.loads(row["frontmatter"])
                if fm.get("category") == "code":
                    continue
                if fm.get("agent") is False:
                    continue
                path = MEMORIES_DIR / row["filename"]
                items.append((path, fm))

        return items

    # ── Related memory discovery ──────────────────────────────────────────────

    async def _find_related_memories(self, item_path: Path, item_fm: dict, last_checked: Optional[str]) -> list:
        """Find memories related to this goal/project. Returns list of (path, fm_dict)."""
        ac = self._agent_config()
        max_memories = ac.get("max_memories_per_item", 20)

        related = []
        item_tags = set(item_fm.get("tags") or [])
        item_title = item_fm.get("source_title", "")
        inferred_from = item_fm.get("inferred_from") or []

        # Parse notes field for participant names (simple word extraction)
        notes_text = item_fm.get("notes", "")
        note_words = set(re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', notes_text))

        # Recency cutoff
        cutoff_ts = None
        if last_checked:
            try:
                cutoff_ts = datetime.fromisoformat(last_checked).timestamp()
            except Exception:
                pass

        # Query all memories excluding action/commitment/goal/project/calendar_event
        exclude_types = ["action", "commitment", "goal", "project", "calendar_event"]
        all_rows = await self._cache.query_all(exclude_types=exclude_types)

        for row in all_rows:
            filename = row["filename"]
            mtime = row["mtime"]

            # Skip self
            if filename == item_path.name:
                continue

            # Recency filter
            if cutoff_ts and mtime <= cutoff_ts:
                continue

            try:
                mem_fm = json.loads(row["frontmatter"])

                # Source 1: inferred_from
                if filename in inferred_from:
                    related.append((row, mem_fm, mtime))
                    continue

                # Source 2: tag overlap
                mem_tags = set(mem_fm.get("tags") or [])
                if item_tags & mem_tags:
                    related.append((row, mem_fm, mtime))
                    continue

                # Source 3: title Jaccard >= 0.3
                mem_title = mem_fm.get("source_title", "")
                if _title_similarity(item_title, mem_title) >= 0.3:
                    related.append((row, mem_fm, mtime))
                    continue

                # Source 4: participant overlap
                mem_participants = set(mem_fm.get("participants") or [])
                if note_words & mem_participants:
                    related.append((row, mem_fm, mtime))
                    continue

            except Exception:
                continue

        # Sort by mtime descending, cap at max
        related_sorted = sorted(related, key=lambda x: x[2], reverse=True)
        related_sorted = related_sorted[:max_memories]

        # Return as list of (path, fm_dict)
        result = []
        for row, fm, _ in related_sorted:
            path = MEMORIES_DIR / row["filename"]
            result.append((path, fm))

        return result

    # ── LLM report generation ─────────────────────────────────────────────────

    async def _generate_report(self, item_path: Path, item_fm: dict, related_memories: list, state_entry: dict) -> Optional[dict]:
        """Call LLM to generate report and actions. Returns parsed dict or None."""
        ac = self._agent_config()
        min_confidence = ac.get("min_confidence", 0.6)

        # Count pending actions already proposed for this item
        source_slug = _slugify(item_fm.get("source_title", item_path.stem))
        action_rows = await self._cache.query_by_prefix(f"action-{source_slug}")
        pending_actions_count = sum(
            1 for row in action_rows
            if json.loads(row["frontmatter"]).get("status") == "pending"
        )

        # Build prompt
        item_type = item_fm.get("type", "goal")
        category = item_fm.get("category", "")
        status = item_fm.get("status", "")
        due_date = item_fm.get("due_date") or "no deadline"
        linked = item_fm.get("linked_goal") or item_fm.get("linked_projects") or []
        if isinstance(linked, list):
            linked = ", ".join(linked) if linked else "none"
        notes = item_fm.get("notes", "")
        last_checked_str = state_entry.get("last_checked") or "never"

        related_summaries = []
        for mem_path, mem_fm in related_memories:
            date_str = mem_fm.get("created") or mem_fm.get("meeting_date") or mem_fm.get("last_message") or ""
            if date_str:
                date_str = str(date_str)[:10]
            title = mem_fm.get("source_title", mem_path.name)
            summary = mem_fm.get("summary", "")[:150]
            related_summaries.append(f"- [{mem_path.name}, {date_str}] {title} — {summary}")

        related_section = "\n".join(related_summaries) if related_summaries else "(none)"

        prompt = (
            f"Goal/Project: {item_fm.get('source_title', item_path.name)}\n"
            f"Type: {item_type} | Category: {category} | Status: {status}\n"
            f"Due: {due_date} | Linked: {linked}\n"
            f"Notes: {notes}\n\n"
            f"Last checked: {last_checked_str}\n"
            f"Pending actions already proposed: {pending_actions_count} (do not re-propose the same action)\n\n"
            f"Recent related memories ({len(related_memories)} files since last check):\n"
            f"{related_section}\n\n"
            "Allowed action_types:\n"
            "- add_milestone(target=<project.md>, args={text: str})\n"
            "- update_status(target=<goal.md|project.md>, args={status: \"completed\"|\"abandoned\"|\"on-hold\"|\"active\"})\n"
            "- update_due_date(target=<goal.md|project.md>, args={due_date: \"YYYY-MM-DD\"})\n"
            "- add_note(target=<goal.md|project.md>, args={text: str})\n"
            "- create_commitment(target=null, args={description, due_date, owner, recipient, commitment_type})\n"
            "- complete_commitment(target=<commitment.md>, args={})\n\n"
            "Return JSON only (no markdown fences):\n"
            "{\n"
            '  "has_update": true,\n'
            '  "urgency": "low|medium|high",\n'
            '  "report": "...",\n'
            '  "actions": [{action_type, target, args, confidence, rationale}],\n'
            '  "evidence": [filenames]\n'
            "}\n"
        )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("chat"),
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            if hasattr(resp, "usage") and resp.usage:
                record_usage(resolve("chat"), resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
            text = resp.choices[0].message.content.strip()
            # Strip markdown fences
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            data = json.loads(text)
        except json.JSONDecodeError as e:
            log.warning("JSON parse error from goal agent LLM for %s: %s", item_path.name, e)
            return None
        except Exception:
            log.exception("LLM call failed for goal agent: %s", item_path.name)
            return None

        # Check has_update
        if not data.get("has_update"):
            return None

        # Filter actions by confidence
        actions = data.get("actions", [])
        filtered_actions = [a for a in actions if float(a.get("confidence", 0)) >= min_confidence]
        data["actions"] = filtered_actions

        # Validate urgency
        urgency = data.get("urgency", "low")
        if urgency == "high":
            evidence = data.get("evidence", [])
            if len(evidence) < 1:
                log.warning("High urgency report for %s has no evidence — downgrading to medium", item_path.name)
                data["urgency"] = "medium"

        # Dedup by report hash
        report_text = data.get("report", "")
        report_hash = hashlib.sha1(report_text.encode()).hexdigest()[:12]
        if state_entry.get("last_report_hash") == report_hash:
            log.debug("Report hash unchanged for %s — skipping", item_path.name)
            return None

        data["report_hash"] = report_hash
        return data

    # ── Action file writing ───────────────────────────────────────────────────

    def _write_action(self, item_path: Path, item_fm: dict, action_dict: dict) -> None:
        """Write an action-*.md file atomically."""
        source_slug = _slugify(item_fm.get("source_title", item_path.stem))
        action_type = action_dict.get("action_type", "")
        rationale = action_dict.get("rationale", "")

        # Validate LLM-supplied target to prevent path traversal
        target = action_dict.get("target")
        if target:
            try:
                _validate_target_name(target)
            except ValueError:
                log.warning("Blocked path traversal in action target: %r — skipping write", target)
                return
            target_path = MEMORIES_DIR / target
            if not target_path.resolve().is_relative_to(MEMORIES_DIR.resolve()):
                log.warning("Blocked path escape in action target: %r — skipping write", target)
                return
            if not target_path.exists():
                log.warning("Action target does not exist: %r — skipping write", target)
                return
        action_id_src = f"{item_path.name}:{action_type}:{rationale}"
        action_id = hashlib.sha1(action_id_src.encode()).hexdigest()[:6]

        filename = f"action-{source_slug}-{action_id}.md"
        action_path = MEMORIES_DIR / filename

        # Skip if already exists
        if action_path.exists():
            log.debug("Action file already exists: %s", filename)
            return

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        fm = {
            "type": "agent_action",
            "action_id": action_id,
            "action_type": action_type,
            "status": "pending",
            "target": action_dict.get("target"),
            "args": action_dict.get("args") or {},
            "confidence": action_dict.get("confidence"),
            "rationale": rationale,
            "evidence": action_dict.get("evidence") or [],
            "proposed_at": now,
            "approved_at": None,
            "executed_at": None,
            "source_goal": item_path.name,
        }

        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        content = f"---\n{frontmatter}---\n\n## Rationale\n{rationale}\n"

        tmp_path = action_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(action_path))
            log.info("Wrote action: %s", filename)
        except Exception:
            log.exception("Failed to write action file %s", filename)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Action execution ──────────────────────────────────────────────────────

    async def _execute_action(self, action_path: Path, action_fm: dict) -> str:
        """Execute an approved action. Returns success message or raises."""
        action_type = action_fm.get("action_type")
        target_name = action_fm.get("target")
        args = action_fm.get("args", {}) or {}

        # Validate target before any path construction
        if target_name:
            _validate_target_name(target_name)
            target_check = MEMORIES_DIR / target_name
            if not target_check.resolve().is_relative_to(MEMORIES_DIR.resolve()):
                raise ValueError(f"Target path escapes memories directory: {target_name!r}")

        if action_type == "add_milestone":
            if not target_name:
                raise ValueError("add_milestone requires target")
            target_path = MEMORIES_DIR / target_name
            if not target_path.exists():
                raise ValueError(f"Target not found: {target_name}")
            target_fm = _parse_frontmatter(target_path.read_text(encoding="utf-8"))
            if target_fm.get("type") != "project":
                raise ValueError(f"Target {target_name} is not a project")
            from goals_tracker import GoalManager
            gm = GoalManager(MEMORIES_DIR, self._load_config())
            gm.add_milestone(target_path, args["text"])
            return f"Added milestone to {target_name}"

        elif action_type == "update_status":
            if not target_name:
                raise ValueError("update_status requires target")
            target_path = MEMORIES_DIR / target_name
            if not target_path.exists():
                raise ValueError(f"Target not found: {target_name}")
            target_fm = _parse_frontmatter(target_path.read_text(encoding="utf-8"))
            target_type = target_fm.get("type")
            new_status = args.get("status")
            from goals_tracker import GoalManager
            gm = GoalManager(MEMORIES_DIR, self._load_config())
            if target_type == "goal":
                gm.update_goal_status(target_path, new_status)
            elif target_type == "project":
                gm.update_project_status(target_path, new_status)
            else:
                raise ValueError(f"Target {target_name} is not a goal or project")
            return f"Updated {target_name} status to {new_status}"

        elif action_type == "update_due_date":
            if not target_name:
                raise ValueError("update_due_date requires target")
            target_path = MEMORIES_DIR / target_name
            if not target_path.exists():
                raise ValueError(f"Target not found: {target_name}")
            # Read and update due_date field
            text = target_path.read_text(encoding="utf-8")
            fm = _parse_frontmatter(text)
            fm["due_date"] = args.get("due_date")
            # Atomic rewrite
            parts = text.split("---", 2)
            new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
            new_content = f"---\n{new_fm}---{parts[2] if len(parts) >= 3 else ''}"
            tmp = target_path.with_suffix(".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            os.rename(str(tmp), str(target_path))
            return f"Updated {target_name} due_date to {args.get('due_date')}"

        elif action_type == "add_note":
            if not target_name:
                raise ValueError("add_note requires target")
            target_path = MEMORIES_DIR / target_name
            if not target_path.exists():
                raise ValueError(f"Target not found: {target_name}")
            text = target_path.read_text(encoding="utf-8")
            fm = _parse_frontmatter(text)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            note_text = f"\n{timestamp}: {args.get('text', '')}"
            current_notes = fm.get("notes", "")
            fm["notes"] = current_notes + note_text
            # Atomic rewrite
            parts = text.split("---", 2)
            new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
            new_content = f"---\n{new_fm}---{parts[2] if len(parts) >= 3 else ''}"
            tmp = target_path.with_suffix(".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            os.rename(str(tmp), str(target_path))
            return f"Added note to {target_name}"

        elif action_type == "create_commitment":
            from commitment_tracker import CommitmentTracker
            ct = CommitmentTracker(role="full", cache=self._cache)
            item = {
                "type": args.get("commitment_type", "outbound"),
                "description": args.get("description"),
                "owner": args.get("owner"),
                "recipient": args.get("recipient"),
                "due_date": args.get("due_date"),
                "due_date_confidence": "explicit" if args.get("due_date") else "none",
                "confidence": 1.0,
                "extracted_text": "",
            }
            source_url = f"agent:{action_fm.get('action_id')}"
            source_title = f"Goal agent action {action_fm.get('action_id')}"
            await ct._write_commitment(item, source_url, source_title, 0.0, "agent_action")
            return f"Created commitment: {args.get('description')}"

        elif action_type == "complete_commitment":
            if not target_name:
                raise ValueError("complete_commitment requires target")
            target_path = MEMORIES_DIR / target_name
            target_row = await self._cache.get(target_name)
            if target_row is None:
                raise ValueError(f"Target not found: {target_name}")
            target_fm = _parse_frontmatter(target_row.get("body") or "")
            if target_fm.get("type") != "commitment":
                raise ValueError(f"Target {target_name} is not a commitment")
            from commitment_tracker import CommitmentTracker
            ct = CommitmentTracker(role="full", cache=self._cache)
            await ct.update_commitment_status(target_path, "completed")
            return f"Marked {target_name} as completed"

        else:
            raise ValueError(f"Unknown action_type: {action_type}")

    # ── Auto-supersede check ──────────────────────────────────────────────────

    def _check_superseded_actions(self) -> None:
        """Mark actions as superseded if their preconditions are no longer met."""
        for action_path in MEMORIES_DIR.glob("action-*.md"):
            try:
                text = action_path.read_text(encoding="utf-8")
                fm = _parse_frontmatter(text)
                if fm.get("status") != "pending":
                    continue

                source_goal = fm.get("source_goal")
                action_type = fm.get("action_type")
                target_name = fm.get("target")
                args = fm.get("args") or {}

                # Check if source goal still exists
                source_path = None
                if source_goal:
                    try:
                        _validate_target_name(source_goal)
                        source_path = MEMORIES_DIR / source_goal
                    except ValueError:
                        log.warning("Invalid source_goal in action file %s: %r", action_path.name, source_goal)
                if source_path and not source_path.exists():
                    self._mark_superseded(action_path, fm, "Source goal/project no longer exists")
                    continue

                # add_milestone: check if milestone text already in target
                if action_type == "add_milestone" and target_name:
                    try:
                        _validate_target_name(target_name)
                    except ValueError:
                        continue
                    target_path = MEMORIES_DIR / target_name
                    if target_path.exists():
                        target_text = target_path.read_text(encoding="utf-8")
                        milestone_text = args.get("text", "")
                        if milestone_text and milestone_text in target_text:
                            self._mark_superseded(action_path, fm, "Milestone already exists")
                            continue

                # update_status: check if target already has proposed status
                if action_type == "update_status" and target_name:
                    try:
                        _validate_target_name(target_name)
                    except ValueError:
                        continue
                    target_path = MEMORIES_DIR / target_name
                    if target_path.exists():
                        target_fm = _parse_frontmatter(target_path.read_text(encoding="utf-8"))
                        if target_fm.get("status") == args.get("status"):
                            self._mark_superseded(action_path, fm, "Status already set")
                            continue

            except Exception:
                log.exception("Error checking superseded status for %s", action_path.name)

    def _mark_superseded(self, action_path: Path, action_fm: dict, reason: str) -> None:
        """Update action file status to superseded with a note."""
        text = action_path.read_text(encoding="utf-8")
        action_fm["status"] = "superseded"
        action_fm["superseded_reason"] = reason
        action_fm["superseded_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        parts = text.split("---", 2)
        new_fm = yaml.dump(action_fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        new_content = f"---\n{new_fm}---{parts[2] if len(parts) >= 3 else ''}"

        tmp = action_path.with_suffix(".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.rename(str(tmp), str(action_path))
        log.info("Marked action %s as superseded: %s", action_path.name, reason)

    # ── Process item ──────────────────────────────────────────────────────────

    async def _process_item(self, item_path: Path, item_fm: dict, state: dict) -> None:
        """Process one goal/project: find related memories, generate report, write actions."""
        ac = self._agent_config()
        stale_threshold_days = ac.get("stale_threshold_days", 14)

        # Get or create state entry
        state_key = "goals" if item_fm.get("type") == "goal" else "projects"
        if state_key not in state:
            state[state_key] = {}
        if item_path.name not in state[state_key]:
            state[state_key][item_path.name] = {}

        state_entry = state[state_key][item_path.name]
        last_checked = state_entry.get("last_checked")

        # Find related memories
        related = await self._find_related_memories(item_path, item_fm, last_checked)

        # Staleness check
        report = None
        if not related:
            # Check if item is stale
            created_str = item_fm.get("created")
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    days_old = (datetime.now() - created).days
                    if days_old >= stale_threshold_days:
                        # Synthesize low-urgency report
                        report = {
                            "has_update": True,
                            "urgency": "low",
                            "report": f"{item_fm.get('source_title', item_path.name)} has had no new related activity in {days_old} days.",
                            "actions": [],
                            "evidence": [],
                            "report_hash": hashlib.sha1(f"stale-{days_old}".encode()).hexdigest()[:12],
                        }
                except Exception:
                    pass

        # Generate report if not already synthesized
        if report is None and related:
            report = await self._generate_report(item_path, item_fm, related, state_entry)

        if report is None:
            # No update
            state_entry["last_checked"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            self._save_state(state)
            return

        # Write action files
        for action_dict in report.get("actions", []):
            # Add evidence from report if not already in action
            if "evidence" not in action_dict:
                action_dict["evidence"] = report.get("evidence", [])
            self._write_action(item_path, item_fm, action_dict)

        # Send urgent ping if needed
        urgency = report.get("urgency", "low")
        if urgency == "high" and self.notification_callback:
            # Check cooldown
            urgent_cooldown_hours = ac.get("urgent_cooldown_hours", 24)
            last_ping_str = state_entry.get("last_urgent_ping")
            send_ping = True
            if last_ping_str:
                try:
                    last_ping = datetime.fromisoformat(last_ping_str)
                    if (datetime.now() - last_ping).total_seconds() < urgent_cooldown_hours * 3600:
                        send_ping = False
                except Exception:
                    pass

            if send_ping:
                msg = (
                    f"🚨 Urgent update for {item_fm.get('source_title', item_path.name)}:\n\n"
                    f"{report.get('report', '')}\n\n"
                    f"Use /actions to review proposed actions."
                )
                try:
                    await self.notification_callback(msg)
                    state_entry["last_urgent_ping"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                    log.info("Sent urgent ping for %s", item_path.name)
                except Exception:
                    log.exception("Failed to send urgent ping for %s", item_path.name)

        # Update state
        state_entry["last_checked"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        state_entry["last_report_hash"] = report.get("report_hash", "")
        state_entry["last_report"] = report.get("report", "")
        self._save_state(state)

    # ── Main scan loop ────────────────────────────────────────────────────────

    async def _scan(self) -> None:
        """Main scan: check superseded actions, then process each item."""
        ac = self._agent_config()
        max_items = ac.get("max_items_per_cycle", 0)

        # Prune state for deleted files
        state = self._load_state()
        for state_key in ["goals", "projects"]:
            if state_key not in state:
                continue
            for filename in list(state[state_key].keys()):
                if not (MEMORIES_DIR / filename).exists():
                    del state[state_key][filename]
        self._save_state(state)

        # Check superseded actions
        self._check_superseded_actions()

        # Select items
        items = await self._select_items()
        if max_items > 0:
            items = items[:max_items]

        if not items:
            log.debug("No active goals/projects to process")
            return

        log.info("Processing %d goal(s)/project(s)", len(items))

        for item_path, item_fm in items:
            try:
                await self._process_item(item_path, item_fm, state)
            except Exception:
                log.exception("Error processing %s", item_path.name)

    async def run_loop(self, stop_event: asyncio.Event):
        """Main async loop: scan every scan_interval_min minutes."""
        if self.role != "full":
            log.debug("Goal/project agent disabled — role is %s (full required)", self.role)
            return

        ac = self._agent_config()
        enabled = ac.get("enabled", True)
        if not enabled:
            log.info("Goal/project agent disabled via config")
            return

        interval = ac.get("scan_interval_min", 360) * 60  # convert minutes to seconds
        log.info("Goal/project agent started — scanning every %ds", interval)

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await self._scan()
            except Exception as exc:
                log.exception("Uncaught error in goal/project agent cycle")
                beat_status, beat_error = "error", str(exc)
            record_beat("goal_project_agent", beat_status, beat_error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ── On-demand change digest ───────────────────────────────────────────────

    async def _generate_digest_summary(
        self,
        title: str,
        item_fm: dict,
        related_memories: list,
        hours: int,
    ) -> str:
        """Return a 2–3 sentence plain-text summary of recent activity for one item."""
        related_snippets = []
        for mem_path, mem_fm in related_memories:
            date_str = (
                mem_fm.get("created")
                or mem_fm.get("meeting_date")
                or mem_fm.get("last_message")
                or ""
            )
            date_str = str(date_str)[:10]
            mem_title = mem_fm.get("source_title", mem_path.name)
            mem_summary = mem_fm.get("summary", "")[:200]
            related_snippets.append(f"- [{date_str}] {mem_title} — {mem_summary}")

        item_type = item_fm.get("type", "goal")
        status = item_fm.get("status", "")
        prompt = (
            f"{item_type.capitalize()}: {title} (status: {status})\n\n"
            f"Related activity in the last {hours}h ({len(related_memories)} items):\n"
            + "\n".join(related_snippets)
            + "\n\nWrite 2–3 sentences of plain text summarising what changed or happened "
            "for this project/goal. Focus on concrete activity, decisions, or progress. "
            "No headers or bullets. Be specific and concise."
        )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("summarize"),
                messages=[{"role": "user", "content": prompt}],
                timeout=20,
            )
            if hasattr(resp, "usage") and resp.usage:
                record_usage(resolve("summarize"), resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
            return resp.choices[0].message.content.strip()
        except Exception as e:
            log.warning("Digest summary LLM call failed for %s: %s", title, e)
            titles = [
                mem_fm.get("source_title", p.name)
                for p, mem_fm in related_memories[:3]
            ]
            return f"{len(related_memories)} related activit{'ies' if len(related_memories) != 1 else 'y'}: {', '.join(titles)}"

    async def generate_change_digest(self, hours: int = 24) -> list:
        """Return activity digest for active goals/projects with changes in the last N hours.

        Each entry is a dict with keys: title, type, summary, memory_count.
        Entries are sorted by memory_count descending (most active first).
        Returns an empty list when no items have recent activity.
        """
        cutoff_iso = (datetime.now() - timedelta(hours=hours)).isoformat()

        items = await self._select_items()
        results = []

        for item_path, item_fm in items:
            related = await self._find_related_memories(
                item_path, item_fm, last_checked=cutoff_iso
            )
            if not related:
                continue

            title = item_fm.get("source_title", item_path.stem)
            summary = await self._generate_digest_summary(title, item_fm, related, hours)
            results.append(
                {
                    "title": title,
                    "type": item_fm.get("type", "goal"),
                    "summary": summary,
                    "memory_count": len(related),
                }
            )

        results.sort(key=lambda x: x["memory_count"], reverse=True)
        return results
