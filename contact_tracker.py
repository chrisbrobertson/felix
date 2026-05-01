import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from utils import load_config
from heartbeat import record_beat

log = logging.getLogger("contact-tracker")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "contact-tracker-state.json"

MAX_FILES_PER_CYCLE = 50
SUMMARY_REFRESH_THRESHOLD = 3
MAX_INTERACTION_TIMESTAMPS = 100


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> dict:
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _name_to_slug(name: str) -> str:
    """Convert name to filesystem-safe slug, max 40 chars."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:40].rstrip("-")


def _normalize_email(email: str) -> str:
    """Lowercase email for dedup key."""
    return email.lower().strip()


def _normalize_name(name: str) -> str:
    """Normalize name: strip whitespace, NFC unicode."""
    import unicodedata
    return unicodedata.normalize("NFC", name.strip())


def _relationship_score(interactions: list) -> float:
    """Recency-weighted interaction count. interaction_timestamps are ISO strings."""
    now = datetime.now(timezone.utc)
    score = 0.0
    for ts_str in interactions:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            days = max((now - ts).days, 1)
            score += 1.0 / days
        except Exception:
            pass
    return round(score, 2)


# ── ContactTracker ────────────────────────────────────────────────────────────

class ContactTracker:
    def __init__(self, role: str = "full"):
        self.role = role

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        return load_config(CONFIG_PATH)

    def _tracker_config(self) -> dict:
        return self._load_config().get("contact_tracker", {})

    # ── State ─────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {"last_scan": None, "processed": {}, "contacts": {}}

    def _save_state(self, state: dict):
        # Prune stale entries for files that no longer exist — keeps the state file lean
        if "processed" in state:
            state["processed"] = {k: v for k, v in state["processed"].items() if (MEMORIES_DIR / k).exists()}
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(STATE_FILE))
        except Exception as e:
            log.warning("Failed to save contact tracker state: %s", e)

    # ── Participant extraction ────────────────────────────────────────────────

    def _extract_participants(self, fm: dict, source_type: str) -> list:
        """
        Extract list of (name, email) tuples from frontmatter.
        Returns: [(name, email), ...] where name or email may be None.
        """
        participants = fm.get("participants", [])
        results = []

        for p in participants:
            if isinstance(p, str):
                # email_thread: ["alice@example.com", "bob@example.com"]
                if "@" in p:
                    results.append((None, p))
                else:
                    results.append((p, None))
            elif isinstance(p, dict):
                # meeting_transcript / calendar_event: [{name: "Alice", email: "..."}]
                # slack_thread: [{name: "Alice", slack_id: "..."}]
                name = p.get("name")
                email = p.get("email")
                if name or email:
                    results.append((name, email))

        return results

    # ── Deduplication ─────────────────────────────────────────────────────────

    def _find_or_create_contact_slug(
        self, name: str, email: Optional[str], state: dict
    ) -> tuple:
        """
        Find existing contact slug or create new one.
        Returns: (slug, is_new, canonical_name)
        """
        # Email-based dedup (primary key)
        if email:
            norm_email = _normalize_email(email)
            # Search existing contacts for this email
            for slug, contact_state in state.get("contacts", {}).items():
                known_emails = [_normalize_email(e) for e in contact_state.get("emails", [])]
                if norm_email in known_emails:
                    # Update canonical name to longest version seen
                    existing_name = contact_state.get("canonical_name", "")
                    if name and len(name) > len(existing_name):
                        contact_state["canonical_name"] = name
                        return (slug, False, name)
                    return (slug, False, existing_name or name or email)

        # Name-based dedup (fallback)
        canonical_name = _normalize_name(name) if name else email
        if not canonical_name:
            return (None, False, None)  # Skip invalid entries

        base_slug = _name_to_slug(canonical_name)

        # Check if slug already exists
        if base_slug in state.get("contacts", {}):
            existing_name = state["contacts"][base_slug].get("canonical_name", canonical_name)
            # Update to longest name version
            if name and len(name) > len(existing_name):
                state["contacts"][base_slug]["canonical_name"] = name
                return (base_slug, False, name)
            return (base_slug, False, existing_name)

        # Handle collisions (same slug, different people)
        counter = 2
        final_slug = base_slug
        while final_slug in state.get("contacts", {}):
            final_slug = f"{base_slug}-{counter}"
            counter += 1

        return (final_slug, True, canonical_name)

    # ── Contact file write ────────────────────────────────────────────────────

    def _contact_path(self, slug: str) -> Path:
        return MEMORIES_DIR / f"contact-{slug}.md"

    def _upsert_contact(
        self,
        slug: str,
        canonical_name: str,
        email: Optional[str],
        interaction_date: str,
        source_memory: dict,
        state: dict,
        force_summary_refresh: bool = False,
    ):
        """Create or update contact file."""
        contact_path = self._contact_path(slug)
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Load existing contact if it exists
        existing_fm = {}
        existing_body = ""
        if contact_path.exists():
            try:
                existing_text = contact_path.read_text()
                existing_fm = _parse_frontmatter(existing_text)
                # Extract body (everything after frontmatter)
                parts = existing_text.split("---", 2)
                if len(parts) >= 3:
                    existing_body = parts[2]
            except Exception:
                pass

        # Initialize contact state
        contact_state = state.setdefault("contacts", {}).setdefault(slug, {})
        interaction_timestamps = contact_state.setdefault("interaction_timestamps", [])
        last_summary_count = contact_state.get("last_summary_interaction_count", 0)

        # Add new interaction timestamp
        if interaction_date and interaction_date not in interaction_timestamps:
            interaction_timestamps.append(interaction_date)
            # Keep only most recent 100
            interaction_timestamps.sort(reverse=True)
            contact_state["interaction_timestamps"] = interaction_timestamps[:MAX_INTERACTION_TIMESTAMPS]

        # Track emails
        existing_emails = existing_fm.get("emails", [])
        if email and email not in existing_emails:
            existing_emails.append(email)

        # Update canonical name if longer
        if canonical_name and len(canonical_name) > len(existing_fm.get("name", "")):
            contact_state["canonical_name"] = canonical_name

        # Calculate relationship score
        rel_score = _relationship_score(interaction_timestamps)
        interaction_count = len(interaction_timestamps)

        # Determine if we need to regenerate summary
        should_regenerate = (
            not existing_body or
            force_summary_refresh or
            (interaction_count - last_summary_count) >= SUMMARY_REFRESH_THRESHOLD
        )

        # Build frontmatter
        fm = {
            "source_title": canonical_name,
            "summary": existing_fm.get("summary", f"Contact record for {canonical_name}"),
            "tags": existing_fm.get("tags", []),
            "last_scanned": now,
            "source_url": f"contact:{slug}",
            "type": "contact",
            "name": canonical_name,
            "emails": existing_emails,
            "last_interaction": interaction_timestamps[0] if interaction_timestamps else None,
            "interaction_count": interaction_count,
            "relationship_score": rel_score,
        }

        # Build interaction history (last 10)
        history_lines = []
        for i, ts in enumerate(interaction_timestamps[:10]):
            # Format: "- YYYY-MM-DD — Source Title (source_type)"
            date_part = ts[:10] if ts else "unknown"
            # We don't have source metadata here, would need to enhance later
            history_lines.append(f"- {date_part} — Interaction")

        body = existing_body
        if not body:
            body = f"\n\n## Recent Interactions\n\n{canonical_name} is a contact from your secondbrain memories.\n\n## Interaction History\n\n"
            body += "\n".join(history_lines) if history_lines else "No interactions yet."

        # If regeneration needed, call LLM (async context)
        # For now, preserve existing summary - LLM call will be added in integration
        content = f"---\n{yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)}---{body}\n"

        # Atomic write
        tmp_path = contact_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(contact_path))
            log.debug("Wrote contact: %s", contact_path.name)
        except Exception:
            log.exception("Failed to write contact file %s", contact_path.name)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Run loop ──────────────────────────────────────────────────────────────

    async def run_loop(self, stop_event: asyncio.Event):
        tc = self._tracker_config()
        interval = tc.get("interval_seconds", 300)
        log.info("Contact tracker started — scanning every %ds", interval)

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await self._run_scan()
            except Exception as exc:
                log.exception("Uncaught error in contact tracker cycle")
                beat_status, beat_error = "error", str(exc)
            record_beat("contact_tracker", beat_status, beat_error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _run_scan(self):
        tc = self._tracker_config()
        source_types = tc.get("source_types", [
            "email_thread", "meeting_transcript", "calendar_event", "slack_thread"
        ])

        state = self._load_state()
        processed = state.get("processed", {})

        # Collect candidates: source-type files changed since last processed
        candidates = []
        for f in MEMORIES_DIR.glob("*.md"):
            # Skip contact files
            if f.name.startswith("contact-"):
                continue
            try:
                mtime = f.stat().st_mtime
            except Exception:
                continue

            stored_mtime = processed.get(f.name)
            if stored_mtime is not None and abs(mtime - stored_mtime) < 1.0:
                continue  # Unchanged since last scan

            # Check type field from frontmatter header (cached)
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
            log.debug("No new/updated source files to process for contacts")
            return

        log.info(
            "Extracting contacts from %d source file(s)",
            min(len(candidates), MAX_FILES_PER_CYCLE),
        )

        processed_count = 0
        for f, mtime in candidates[:MAX_FILES_PER_CYCLE]:
            try:
                content = f.read_text(encoding="utf-8")
                fm = _parse_frontmatter(content)
                source_type = fm.get("type", "")

                # Extract interaction timestamp
                interaction_date = (
                    fm.get("last_message") or
                    fm.get("meeting_date") or
                    fm.get("start_time") or
                    fm.get("first_message") or
                    ""
                )

                # Extract participants
                participants = self._extract_participants(fm, source_type)

                for name, email in participants:
                    if not name and not email:
                        continue  # Skip invalid entries

                    slug, is_new, canonical_name = self._find_or_create_contact_slug(
                        name, email, state
                    )
                    if not slug:
                        continue

                    # Initialize contact state if new
                    if is_new:
                        state.setdefault("contacts", {})[slug] = {
                            "canonical_name": canonical_name,
                            "emails": [email] if email else [],
                            "interaction_timestamps": [],
                            "last_summary_interaction_count": 0,
                        }

                    # Upsert contact file
                    self._upsert_contact(
                        slug, canonical_name, email, interaction_date,
                        {"filename": f.name, "type": source_type}, state
                    )

                # Mark file as processed
                processed[f.name] = mtime
                state["processed"] = processed
                state["last_scan"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                self._save_state(state)
                processed_count += 1

            except Exception:
                log.exception("Error processing %s for contacts", f.name)

        if processed_count:
            log.info("Contact scan complete — %d source file(s) processed", processed_count)
