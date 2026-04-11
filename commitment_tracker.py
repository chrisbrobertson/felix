import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("commitment-tracker")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "commitment-scanner-state.json"

MAX_FILES_PER_CYCLE = 30
MIN_CONFIDENCE_DEFAULT = 0.5
NEEDS_REVIEW_THRESHOLD = 0.7


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
    key = f"{source_url}:{description.lower().strip()}:{owner.lower().strip()}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _slugify(text: str, max_len: int = 40) -> str:
    s = text.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s[:max_len].rstrip('-')


# ── CommitmentTracker ─────────────────────────────────────────────────────────

class CommitmentTracker:
    def __init__(self, role: str = "full"):
        self.role = role

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

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

        prompt = (
            f"Extract commitments and waiting-on items from this {source_type}.\n\n"
            f"Source: {source_title}\n"
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
                model="summarize",
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
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
            for line in header.split("\n"):
                stripped = line.strip()
                if stripped.startswith("type:"):
                    fm_type = stripped[5:].strip().strip('"').strip("'")
                    break

            if fm_type not in source_types:
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

                items = await self._extract_commitments(f, fm, content)
                for item in items:
                    self._write_commitment(item, source_url, source_title, min_confidence)

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
