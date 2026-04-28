import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from llm_routes import resolve
from usage_tracker import record_usage
from utils import load_config

log = logging.getLogger("commitment-tracker")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "commitment-scanner-state.json"
CORRECTIONS_FILE = DEPLOY_DIR / "commitment-corrections.jsonl"
ACCURACY_FILE = DEPLOY_DIR / "commitment-accuracy.json"

MAX_FILES_PER_CYCLE = 30
MIN_CONFIDENCE_DEFAULT = 0.5
NEEDS_REVIEW_THRESHOLD = 0.7
MAX_CORRECTIONS_IN_PROMPT = 20
MAX_CORRECTIONS_CHARS = 1000


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _stable_commitment_id(source_url: str, description: str, owner: str) -> str:
    """Generate stable commitment ID using SHA-256 (was SHA-1 through 2026-04-19)."""
    key = f"{source_url}:{description.lower().strip()}:{owner.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _slugify(text: str, max_len: int = 40) -> str:
    s = text.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s[:max_len].rstrip('-')


def _load_corrections(max_count: int = MAX_CORRECTIONS_IN_PROMPT) -> list:
    """Load the last N corrections from the corrections JSONL file."""
    if not CORRECTIONS_FILE.exists():
        return []
    try:
        lines = CORRECTIONS_FILE.read_text().strip().split("\n")
        corrections = []
        for line in lines:
            if line.strip():
                corrections.append(json.loads(line))
        return corrections[-max_count:]
    except Exception as e:
        log.warning("Failed to load corrections: %s", e)
        return []


def _build_corrections_prompt_section(corrections: list) -> str:
    """Build few-shot examples section from corrections, capped at MAX_CORRECTIONS_CHARS."""
    if not corrections:
        return ""

    false_positives = [c for c in corrections if c.get("correction_type") == "false_positive"]
    missed = [c for c in corrections if c.get("correction_type") == "missed"]

    if not false_positives and not missed:
        return ""

    lines = ["Learning from previous corrections:", ""]

    if false_positives:
        lines.append("Do NOT extract items like these (previously marked as false positives):")
        for c in false_positives[:10]:  # Limit to avoid excessive length
            desc = c.get("description", "")[:80]
            owner = c.get("owner", "")
            source_type = c.get("source_type", "")
            lines.append(f'- "{desc}" ({owner}, {source_type}) — too vague, not a real commitment')

    if missed:
        if false_positives:
            lines.append("")
        lines.append("DO extract items like these (previously missed):")
        for c in missed[:10]:
            desc = c.get("description", "")[:80]
            lines.append(f'- "{desc}" — a commitment that was missed')

    lines.append("")
    section = "\n".join(lines)

    # Cap at MAX_CORRECTIONS_CHARS
    if len(section) > MAX_CORRECTIONS_CHARS:
        section = section[:MAX_CORRECTIONS_CHARS] + "\n[truncated]\n"

    return section


def _load_accuracy() -> dict:
    """Load accuracy JSON file."""
    if not ACCURACY_FILE.exists():
        return {"by_source_type": {}, "last_updated": None}
    try:
        return json.loads(ACCURACY_FILE.read_text())
    except Exception:
        return {"by_source_type": {}, "last_updated": None}


def _save_accuracy(data: dict):
    """Atomically save accuracy JSON file."""
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    tmp = ACCURACY_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.rename(str(tmp), str(ACCURACY_FILE))
    except Exception as e:
        log.warning("Failed to save accuracy file: %s", e)


def _increment_extracted_count(source_type: str):
    """Increment the extracted count for a source type."""
    data = _load_accuracy()
    by_type = data["by_source_type"]
    if source_type not in by_type:
        by_type[source_type] = {"extracted": 0, "false_positives": 0, "missed": 0}
    by_type[source_type]["extracted"] += 1
    _save_accuracy(data)


def _record_false_positive(source_type: str):
    """Increment the false_positives count for a source type."""
    data = _load_accuracy()
    by_type = data["by_source_type"]
    if source_type not in by_type:
        by_type[source_type] = {"extracted": 0, "false_positives": 0, "missed": 0}
    by_type[source_type]["false_positives"] += 1
    _save_accuracy(data)


def _record_missed(source_type: str):
    """Increment the missed count for a source type."""
    data = _load_accuracy()
    by_type = data["by_source_type"]
    if source_type not in by_type:
        by_type[source_type] = {"extracted": 0, "false_positives": 0, "missed": 0}
    by_type[source_type]["missed"] += 1
    _save_accuracy(data)


# ── CommitmentTracker ─────────────────────────────────────────────────────────

class CommitmentTracker:
    def __init__(self, role: str = "full"):
        self.role = role

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        return load_config(CONFIG_PATH)

    def _tracker_config(self) -> dict:
        return self._load_config().get("commitment_tracker", {})

    # ── State ─────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {"last_scan": None, "processed": {}}

    def _save_state(self, state: dict):
        # Prune stale entries for files that no longer exist — keeps the state file lean
        if "processed" in state:
            state["processed"] = {k: v for k, v in state["processed"].items() if (MEMORIES_DIR / k).exists()}
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(STATE_FILE))
        except Exception as e:
            log.warning("Failed to save commitment tracker state: %s", e)

    # ── LLM extraction ────────────────────────────────────────────────────────

    async def _extract_commitments(
        self,
        memory_path: Path,
        fm: dict,
        content: str,
    ) -> list:
        source_type = fm.get("type", "")
        source_title = fm.get("source_title", memory_path.name)
        participants = fm.get("participants") or fm.get("speakers") or []
        summary = fm.get("summary", "")
        meeting_date = (
            fm.get("meeting_date")
            or fm.get("last_message")
            or fm.get("first_message")
            or ""
        )

        # Extract the main body section (Transcript or Messages)
        body_section = ""
        for marker in ("## Transcript", "## Messages"):
            if marker in content:
                section = content.split(marker, 1)[1]
                # Stop at next heading
                section = section.split("##", 1)[0] if "##" in section else section
                body_section = section.strip()[:2000]
                break

        participant_str = (
            ", ".join(str(p) for p in participants[:15]) if participants else "unknown"
        )
        date_str = str(meeting_date)[:19] if meeting_date else "unknown"

        # Load and prepend corrections as few-shot examples (FR-13)
        corrections = _load_corrections()
        corrections_section = _build_corrections_prompt_section(corrections)

        prompt = (
            f"Extract commitments and waiting-on items from this {source_type}.\n\n"
            + (corrections_section + "\n" if corrections_section else "")
            + f"Source: {source_title}\n"
            f"Participants: {participant_str}\n"
            f"Date: {date_str}\n\n"
            f"Content:\n{summary}\n\n{body_section}\n\n"
            "Return JSON only:\n"
            "{\n"
            '  "commitments": [\n'
            "    {\n"
            '      "type": "outbound",\n'
            '      "description": "Send revised budget numbers",\n'
            '      "owner": "Sarah Chen",\n'
            '      "owner_email": "sarah.chen@acme.com",\n'
            '      "recipient": "Chris",\n'
            '      "due_date": "2026-04-18",\n'
            '      "due_date_confidence": "explicit",\n'
            '      "confidence": 0.85,\n'
            '      "extracted_text": "Can you commit to having the revised numbers by Friday?"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Commitment types:\n"
            "- outbound: a promise made by someone to do something for another person\n"
            "- inbound: a promise someone made to the user (user is recipient)\n"
            "- waiting_on: the user is waiting for someone else to act or respond\n\n"
            "Only include items with confidence >= 0.5. Return [] if none found."
        )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("summarize"),
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            if hasattr(resp, "usage") and resp.usage:
                record_usage(resolve("summarize"), resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
            text = resp.choices[0].message.content.strip()
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            data = json.loads(text)
            raw_items = data.get("commitments", [])
        except json.JSONDecodeError as e:
            log.warning(
                "JSON parse error extracting commitments from %s: %s",
                memory_path.name, e
            )
            return []
        except Exception:
            log.exception("LLM call failed for commitment extraction: %s", memory_path.name)
            return []

        # Enforce minimum confidence (LLM instructed to filter, but we enforce too)
        return [item for item in raw_items if float(item.get("confidence", 0)) >= 0.5]

    # ── Commitment file write ─────────────────────────────────────────────────

    def _commitment_path(self, description: str, stable_id: str) -> Path:
        slug = _slugify(description)
        return MEMORIES_DIR / f"commitment-{slug}-{stable_id}.md"

    def _write_commitment(
        self,
        item: dict,
        source_memory_url: str,
        source_title: str,
        min_confidence: float,
        source_type: str = "",
    ):
        confidence = float(item.get("confidence", 0))
        if confidence < min_confidence:
            log.debug(
                "Discarding low-confidence commitment (%.2f): %s",
                confidence,
                item.get("description"),
            )
            return

        description = (item.get("description") or "").strip()
        owner = (item.get("owner") or "").strip()
        stable_id = _stable_commitment_id(source_memory_url, description, owner)
        commitment_path = self._commitment_path(description, stable_id)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # If file already exists, preserve completed/dismissed status
        if commitment_path.exists():
            try:
                existing_text = commitment_path.read_text()
                existing_fm = _parse_frontmatter(existing_text)
                existing_status = existing_fm.get("status", "active")
                if existing_status in ("completed", "dismissed"):
                    # Update last_scanned only — do not revert status
                    new_text = re.sub(
                        r"(last_scanned: ')([^']+)(')",
                        f"\\g<1>{now}\\3",
                        existing_text,
                    )
                    if new_text != existing_text:
                        tmp = commitment_path.with_suffix(".tmp")
                        tmp.write_text(new_text, encoding="utf-8")
                        os.rename(str(tmp), str(commitment_path))
                    return
            except Exception:
                pass  # Fall through and rewrite

        tags = list(item.get("tags") or [])
        if confidence < NEEDS_REVIEW_THRESHOLD and "needs-review" not in tags:
            tags.append("needs-review")

        due_date = item.get("due_date") or None
        owner_email = item.get("owner_email") or None
        recipient = item.get("recipient") or None

        fm = {
            "source_title": description,
            "summary": (
                f"{owner} committed to {description.lower()}"
                + (f" by {due_date}" if due_date else "")
            ),
            "tags": tags,
            "last_scanned": now,
            "source_url": f"commitment:{stable_id}",
            "type": "commitment",
            "commitment_type": item.get("type", "outbound"),
            "owner": owner,
            "owner_email": owner_email,
            "recipient": recipient,
            "due_date": due_date,
            "due_date_confidence": item.get("due_date_confidence", "none"),
            "confidence": confidence,
            "status": "active",
            "source_memory": source_memory_url,
            "extracted_text": item.get("extracted_text", ""),
        }
        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

        context = (
            f"Extracted from {source_title}.\n"
            f"Extracted text: {item.get('extracted_text', '')}"
        )
        content = f"---\n{frontmatter}---\n\n## Context\n{context}\n"

        tmp_path = commitment_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(commitment_path))
            log.debug("Wrote %s", commitment_path.name)
            # Increment extracted count in accuracy tracking (FR-14)
            if source_type:
                _increment_extracted_count(source_type)
        except Exception:
            log.exception("Failed to write commitment file %s", commitment_path.name)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Status update (used by Telegram commands) ─────────────────────────────

    def update_commitment_status(self, commitment_path: Path, new_status: str):
        """Atomically update status field of a commitment file."""
        text = commitment_path.read_text()
        fm = _parse_frontmatter(text)
        fm["status"] = new_status
        fm["last_scanned"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        parts = text.split("---", 2)
        if len(parts) >= 3:
            new_content = f"---\n{new_fm}---{parts[2]}"
        else:
            new_content = f"---\n{new_fm}---\n"

        tmp = commitment_path.with_suffix(".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.rename(str(tmp), str(commitment_path))

    def create_manual_commitment(
        self,
        commitment_type: str,
        description: str,
        owner: str,
        due_date: Optional[str],
        source_note: str,
        force_unique: bool = False,
        recipient: Optional[str] = None,
    ) -> Path:
        """Create a commitment file from manual /missed or /todo command.

        force_unique=True generates a fresh UUID-based ID each call so repeated
        todos with the same description create new files instead of overwriting
        prior state (e.g. a completed "Take vitamins" entry).
        """
        if force_unique:
            unique_token = uuid.uuid4().hex[:12]
            source_url = f"manual:{unique_token}"
            stable_id = unique_token
        else:
            # SHA-256 for commitment IDs (was SHA-1 through 2026-04-19)
            source_url = f"manual:{hashlib.sha256(description.encode()).hexdigest()[:12]}"
            stable_id = _stable_commitment_id(source_url, description, owner)
        commitment_path = self._commitment_path(description, stable_id)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        fm = {
            "source_title": description,
            "summary": f"{owner} committed to {description.lower()}",
            "tags": [],
            "last_scanned": now,
            "source_url": f"commitment:{stable_id}",
            "type": "commitment",
            "commitment_type": commitment_type,
            "owner": owner,
            "owner_email": None,
            "recipient": recipient,
            "due_date": due_date if due_date and due_date != "unknown" else None,
            "due_date_confidence": "explicit" if due_date and due_date != "unknown" else "none",
            "confidence": 1.0,
            "status": "active",
            "source_memory": source_url,
            "extracted_text": "",
            "correction_type": "manual_add",
        }
        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

        context = f"Manually added via /missed command.\nSource note: {source_note}"
        content = f"---\n{frontmatter}---\n\n## Context\n{context}\n"

        tmp_path = commitment_path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.rename(str(tmp_path), str(commitment_path))

        return commitment_path

    # ── Run loop ──────────────────────────────────────────────────────────────

    async def run_loop(self, stop_event: asyncio.Event):
        tc = self._tracker_config()
        interval = tc.get("interval_seconds", 300)
        log.info("Commitment tracker started — scanning every %ds", interval)

        while not stop_event.is_set():
            try:
                await self._run_scan()
            except Exception:
                log.exception("Uncaught error in commitment tracker cycle")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _run_scan(self):
        tc = self._tracker_config()
        min_confidence = float(tc.get("min_confidence", MIN_CONFIDENCE_DEFAULT))
        source_types = tc.get("source_types", ["meeting_transcript", "email_thread"])

        known_types = {"meeting_transcript", "email_thread"}
        for st in source_types:
            if st not in known_types:
                log.warning(
                    "Unknown source_type in commitment_tracker config: %s — will attempt anyway", st
                )

        state = self._load_state()
        processed = state.get("processed", {})

        # Collect candidates: source-type files changed since last processed
        candidates = []
        for f in MEMORIES_DIR.glob("*.md"):
            # Skip commitment files
            if f.name.startswith("commitment-"):
                continue
            try:
                mtime = f.stat().st_mtime
            except Exception:
                continue

            stored_mtime = processed.get(f.name)
            if stored_mtime is not None and abs(mtime - stored_mtime) < 1.0:
                continue  # Unchanged since last scan

            # Check type field from frontmatter header (avoid full read when possible)
            try:
                header = f.read_text(encoding="utf-8")[:500]
            except Exception:
                continue

            fm_type = ""
            fm_classification = ""
            for line in header.split("\n"):
                stripped = line.strip()
                if stripped.startswith("type:"):
                    fm_type = stripped[5:].strip().strip('"').strip("'")
                elif stripped.startswith("classification:"):
                    fm_classification = stripped[15:].strip().strip('"').strip("'")

            if fm_type not in source_types:
                continue

            # Skip email threads with marketing, automated, or transactional classification
            if fm_type == "email_thread" and fm_classification in {"marketing", "automated", "transactional"}:
                continue

            candidates.append((f, mtime))

        if not candidates:
            log.debug("No new/updated source files to process for commitments")
            return

        log.info(
            "Extracting commitments from %d source file(s)",
            min(len(candidates), MAX_FILES_PER_CYCLE),
        )

        processed_count = 0
        for f, mtime in candidates[:MAX_FILES_PER_CYCLE]:
            try:
                content = f.read_text(encoding="utf-8")
                fm = _parse_frontmatter(content)
                source_url = fm.get("source_url") or f"file:{f.name}"
                source_title = fm.get("source_title") or f.name
                source_type = fm.get("type", "")

                items = await self._extract_commitments(f, fm, content)
                for item in items:
                    self._write_commitment(item, source_url, source_title, min_confidence, source_type)

                # Persist state after each file to survive mid-cycle crashes
                processed[f.name] = mtime
                state["processed"] = processed
                state["last_scan"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                self._save_state(state)
                processed_count += 1

            except Exception:
                log.exception("Error processing %s for commitments", f.name)

        if processed_count:
            log.info("Commitment scan complete — %d source file(s) processed", processed_count)
