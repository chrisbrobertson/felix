"""
project_inference_scanner.py — 13th async loop (full role only).

Scans email_thread, meeting_transcript, and slack_thread memory files for
newly-created or updated content, calls LLM to infer what projects the user
is working on, and writes project-candidate-*.md files for human confirmation.
"""
import asyncio
import errno
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
from memory_cache import MemoryCache

log = logging.getLogger("project-inference")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))

MAX_FILES_PER_CYCLE = 20

UNMANGLE_SENTINEL_NAME = ".project-candidate-unmangle-v1.done"
# Extracts the canonical `candidate-{slug}-{id}` suffix from a possibly-
# hostname-mangled stem like `project-Chriss-MacBook-Air-Chriss-Air-candidate-foo-abc123`.
_CANDIDATE_TAIL_RE = re.compile(r"(candidate-.+)$")


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


def _stable_id(title: str, source_path: str) -> str:
    """Generate a stable 6-character ID from title + source path."""
    key = f"{title.lower().strip()}:{source_path}"
    return hashlib.sha1(key.encode()).hexdigest()[:6]


def _title_similarity(title1: str, title2: str) -> float:
    """Compute title similarity as Jaccard index of normalized token sets."""
    tokens1 = set(re.findall(r'[a-z0-9]+', title1.lower()))
    tokens2 = set(re.findall(r'[a-z0-9]+', title2.lower()))
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    return len(intersection) / len(union) if union else 0.0


# ── ProjectInferenceScanner ───────────────────────────────────────────────────

class ProjectInferenceScanner:
    """Thirteenth async loop: infers projects from comms memories."""

    def __init__(self, role: str = "full", cache: Optional[MemoryCache] = None):
        self.role = role
        self.SCAN_INTERVAL = 900  # 15 minutes
        self.SOURCE_TYPES = ["email_thread", "meeting_transcript", "slack_thread"]
        self.STATE_FILE = DEPLOY_DIR / "project-inference-state.json"
        self.REJECTED_FILE = DEPLOY_DIR / "rejected-candidates.json"
        self._cache = cache if cache is not None else MemoryCache(None, MEMORIES_DIR, enabled=False)
        self._unmangle_candidate_filenames()

    def _unmangle_candidate_filenames(self):
        """One-shot cleanup of candidate files mangled by code_scanner's greedy migration.

        Before v1.6.2, ``code_scanner._migrate_project_filenames`` globbed
        ``project-*.md`` and re-prefixed any file that didn't start with the
        current hostname — including candidate files owned by this scanner.
        Combined with macOS's unstable ``socket.gethostname()`` (which flips
        between values like ``Chriss-Air`` and ``Chriss-MacBook-Air``), this
        stacked 1–3 hostname segments onto every ``project-candidate-*.md``
        filename. April 2026: 474 of 474 candidate files on disk were
        mangled; zero canonical remained.

        This is a one-shot sentinel-gated cleanup. On first run it extracts
        the canonical ``candidate-{slug}-{id}`` tail via regex, renames to
        ``project-{tail}.md``, and resolves collisions by keeping the newer
        (by frontmatter ``created`` field, falling back to mtime). Wraps all
        file I/O in try/except for OSError to survive iCloud transient
        errors (EDEADLK on placeholder reads) — any file that fails this
        cycle simply retries on the next scanner boot.

        After the first successful pass the sentinel is written and
        subsequent calls return immediately.
        """
        sentinel = self.STATE_FILE.parent / UNMANGLE_SENTINEL_NAME
        if sentinel.exists():
            return
        if not MEMORIES_DIR.exists():
            try:
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.touch()
            except Exception:
                log.exception("Failed to write project-candidate unmangle sentinel")
            return

        renamed = 0
        deleted_dups = 0
        skipped = 0
        transient = 0

        # Match only mangled forms: `project-{something}-candidate-*.md`. The
        # canonical `project-candidate-*.md` shape is excluded by the negative
        # lookahead on the first segment.
        for path in MEMORIES_DIR.glob("project-*-candidate-*.md"):
            try:
                stem = path.stem  # e.g. "project-Chriss-MacBook-Air-candidate-foo-abc123"
                rest = stem[len("project-"):]
                m = _CANDIDATE_TAIL_RE.search(rest)
                if not m:
                    log.warning(
                        "Project-candidate unmangle: no candidate- pattern in %s, skipping",
                        path.name,
                    )
                    skipped += 1
                    continue
                canonical_tail = m.group(1)
                canonical_name = f"project-{canonical_tail}.md"
                canonical_path = path.parent / canonical_name

                if path.name == canonical_name:
                    # Already canonical — glob shouldn't match this, but guard.
                    continue

                if canonical_path.exists():
                    # Collision: keep whichever is "newer" by frontmatter
                    # `created` field (falling back to mtime if absent). The
                    # `status` field also matters — a `confirmed` or
                    # `rejected` record trumps `pending_confirmation`.
                    winner, loser = self._pick_candidate_winner(path, canonical_path)
                    if loser.exists():
                        loser.unlink()
                        deleted_dups += 1
                    if winner != canonical_path:
                        # Winner is the mangled path; rename it onto canonical.
                        winner.rename(canonical_path)
                        renamed += 1
                    continue

                path.rename(canonical_path)
                renamed += 1
            except OSError as e:
                # iCloud EDEADLK (errno 11) and EAGAIN are transient — skip
                # this file and let the next scanner boot retry. Do NOT treat
                # as a hard failure that would crash migration.
                if e.errno in (errno.EDEADLK, errno.EAGAIN):
                    log.debug(
                        "Project-candidate unmangle: transient OSError on %s (%s), retry next boot",
                        path.name, e,
                    )
                    transient += 1
                else:
                    log.exception("Project-candidate unmangle failed for %s", path)
            except Exception:
                log.exception("Project-candidate unmangle failed for %s", path)

        if renamed or deleted_dups:
            log.info(
                "Project-candidate unmangle: renamed=%d deleted_duplicates=%d skipped=%d transient=%d",
                renamed, deleted_dups, skipped, transient,
            )

        # Only stamp the sentinel if there are no transient errors left —
        # otherwise we want the next boot to retry those files.
        if transient == 0:
            try:
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.touch()
            except Exception:
                log.exception("Failed to write project-candidate unmangle sentinel")

    def _pick_candidate_winner(self, a: Path, b: Path) -> tuple:
        """Return (winner, loser) tuple for two competing candidate paths.

        Precedence (highest → lowest):
        1. Any file with `status: confirmed` or `status: rejected` beats
           `status: pending_confirmation` (user intent is authoritative).
        2. Newer frontmatter `created` timestamp.
        3. Newer file mtime (fallback when `created` is missing/malformed).
        """
        def _signal(path: Path) -> tuple:
            try:
                # Use cache if available; fallback to direct read for unmangle-time access
                # Note: unmangle runs in __init__ before async context, so we must use sync read
                text = path.read_text()
                fm = _parse_frontmatter(text)
            except OSError:
                fm = {}
            status = str(fm.get("status", "pending_confirmation"))
            status_rank = 1 if status in ("confirmed", "rejected") else 0
            created = str(fm.get("created", "")) or ""
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            return (status_rank, created, mtime)

        if _signal(a) >= _signal(b):
            return a, b
        return b, a

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        """Load config from BRAIN_DIR/config.yaml."""
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

    def _inference_config(self) -> dict:
        """Return project_inference section from config."""
        return self._load_config().get("project_inference", {})

    def _goal_categories(self) -> list:
        """Return configured goal categories for LLM prompt."""
        return self._load_config().get("goals", {}).get(
            "categories",
            ["personal", "work", "family", "learning", "other"]
        )

    # ── State ─────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load state from STATE_FILE."""
        if self.STATE_FILE.exists():
            try:
                return json.loads(self.STATE_FILE.read_text())
            except Exception:
                pass
        return {"last_scan": None, "processed": {}}

    def _save_state(self, state: dict) -> None:
        """Save state to STATE_FILE atomically."""
        # Prune stale entries for files that no longer exist — keeps the state file lean
        if "processed" in state:
            state["processed"] = {k: v for k, v in state["processed"].items() if (MEMORIES_DIR / k).exists()}
        tmp = self.STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state, indent=2))
            os.rename(str(tmp), str(self.STATE_FILE))
        except Exception as e:
            log.warning("Failed to save project inference state: %s", e)

    # ── Rejected candidates tracking ──────────────────────────────────────────

    def _load_rejected(self) -> dict:
        """Load rejected-candidates.json."""
        if self.REJECTED_FILE.exists():
            try:
                return json.loads(self.REJECTED_FILE.read_text())
            except Exception:
                pass
        return {"rejected": []}

    def _is_rejected(self, source_path: str) -> bool:
        """Check if source_path appears in any rejected candidate evidence list."""
        rejected_data = self._load_rejected()
        for entry in rejected_data.get("rejected", []):
            if source_path in entry.get("evidence", []):
                return True
        return False

    # ── Deduplication ─────────────────────────────────────────────────────────

    async def _is_duplicate(self, title: str) -> bool:
        """Check if title is too similar to an existing project or candidate."""
        # Query both project and project-candidate prefixes via cache
        # Note: cache.query_by_prefix("project-") returns both "project" and "project-candidate"
        # since _extract_prefix returns "project-candidate" for project-candidate-*.md files
        # We need to query both separately
        project_rows = await self._cache.query_by_prefix("project-")
        candidate_rows = await self._cache.query_by_prefix("project-candidate-")

        # Combine and deduplicate by filename
        seen = set()
        all_rows = []
        for row in project_rows + candidate_rows:
            if row["filename"] not in seen:
                seen.add(row["filename"])
                all_rows.append(row)

        for row in all_rows:
            try:
                fm = _parse_frontmatter(row["header500"])
                existing_title = fm.get("source_title", "")
                if _title_similarity(title, existing_title) >= 0.8:
                    return True
            except Exception:
                continue

        return False

    # ── LLM extraction ────────────────────────────────────────────────────────

    async def _extract_projects(self, path: Path, fm: dict) -> list:
        """Call LLM to extract project candidates from memory file."""
        source_type = fm.get("type", "")
        source_title = fm.get("source_title", path.name)
        summary = fm.get("summary", "")

        # Extract date field
        date_str = (
            fm.get("meeting_date") or
            fm.get("last_message") or
            fm.get("first_message") or
            "unknown"
        )
        if date_str != "unknown":
            date_str = str(date_str)[:19]  # truncate to YYYY-MM-DDTHH:MM:SS

        # Load full content and extract main section (capped at 2000 chars)
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            content = ""

        body_section = ""
        for marker in ("## Transcript", "## Messages", "## Thread"):
            if marker in content:
                section = content.split(marker, 1)[1]
                # Stop at next heading
                section = section.split("##", 1)[0] if "##" in section else section
                body_section = section.strip()[:2000]
                break

        # Build LLM prompt
        categories = self._goal_categories()
        prompt = (
            f"Given the following {source_type} content, identify any projects this person "
            f"appears to be working on. A project is any distinct effort that spans multiple "
            f"tasks or interactions — could be work, personal, family, learning, or any other domain.\n\n"
            f"Source: {source_title}\n"
            f"Date: {date_str}\n"
            f"Content:\n{summary}\n\n{body_section}\n\n"
            f"Configured project categories: {', '.join(categories)}\n\n"
            f"Return JSON only — no markdown fences:\n"
            "{\n"
            '  "projects": [\n'
            "    {\n"
            '      "title": "Q2 rollout plan",\n'
            '      "category_guess": "work",\n'
            '      "summary": "Coordinating Q2 product launch across eng, design, marketing",\n'
            '      "confidence": 0.85,\n'
            '      "due_date_guess": "2026-07-01",\n'
            '      "evidence_quote": "Can you have the Q2 launch checklist ready by EOQ?"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Only include items with confidence >= 0.7. Return [] if no projects detected."
        )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("summarize"),
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            text = resp.choices[0].message.content.strip()
            # Strip markdown fences if present
            text = re.sub(r'^```(?:json)?\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
            data = json.loads(text)
            raw_items = data.get("projects", [])
        except json.JSONDecodeError as e:
            log.warning(
                "JSON parse error extracting projects from %s: %s",
                path.name, e
            )
            return []
        except Exception:
            log.exception("LLM call failed for project extraction: %s", path.name)
            return []

        # Filter by confidence threshold
        min_confidence = self._inference_config().get("confidence_threshold", 0.7)
        return [item for item in raw_items if float(item.get("confidence", 0)) >= min_confidence]

    # ── Candidate cleanup ─────────────────────────────────────────────────────

    async def _cleanup_stale_candidates(self) -> int:
        """Delete pending_confirmation candidates exceeding TTL or max cap.

        Uses cache queries (not glob) to avoid disk fan-out.
        Returns number of files deleted.
        """
        ic = self._inference_config()
        ttl_days = ic.get("candidate_ttl_days", 30)
        max_pending = ic.get("max_pending_candidates", 200)

        now = datetime.now()
        cutoff = now - timedelta(days=ttl_days)

        rows = await self._cache.query_by_prefix("project-candidate-")

        pending = []  # (created_dt, path)
        for row in rows:
            try:
                fm = json.loads(row["frontmatter"])
            except Exception:
                continue
            if fm.get("status") != "pending_confirmation":
                continue
            created_str = str(fm.get("created", ""))
            try:
                created_dt = datetime.fromisoformat(created_str)
            except Exception:
                created_dt = datetime.fromtimestamp(row["mtime"])
            pending.append((created_dt, MEMORIES_DIR / row["filename"]))

        pending.sort(key=lambda x: x[0])  # oldest first

        to_delete: set = set()
        for created_dt, path in pending:
            if created_dt < cutoff:
                to_delete.add(path)

        remaining = [(dt, p) for dt, p in pending if p not in to_delete]
        excess = len(remaining) - max_pending
        if excess > 0:
            for _, path in remaining[:excess]:
                to_delete.add(path)

        deleted = 0
        for path in to_delete:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                log.debug("Could not delete candidate %s", path.name)

        if deleted:
            log.info(
                "Candidate cleanup: removed %d stale/excess pending (ttl=%dd, cap=%d)",
                deleted, ttl_days, max_pending,
            )
        return deleted

    # ── Candidate file write ──────────────────────────────────────────────────

    async def _write_candidate(self, item: dict, source_path: Path) -> None:
        """Write a project-candidate-*.md file for a discovered project."""
        title = item.get("title", "").strip()
        if not title:
            return

        # Dedup checks
        if self._is_rejected(source_path.name):
            log.debug("Skipping project from rejected source: %s", source_path.name)
            return

        if await self._is_duplicate(title):
            log.debug("Skipping duplicate project: %s", title)
            return

        # Generate filename
        stable_id = _stable_id(title, source_path.name)
        slug = _slugify(title)
        filename = f"project-candidate-{slug}-{stable_id}.md"
        candidate_path = MEMORIES_DIR / filename

        # Skip if already exists
        if candidate_path.exists():
            return

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Build frontmatter
        fm = {
            "type": "project_candidate",
            "candidate_type": "project",
            "category_guess": item.get("category_guess", "other"),
            "source_title": f"{title} (candidate)",
            "summary": item.get("summary", ""),
            "confidence": item.get("confidence", 0.0),
            "evidence": [source_path.name],
            "extracted_fields": {
                "title": title,
                "due_date": item.get("due_date_guess") or None,
            },
            "status": "pending_confirmation",
            "created": now,
        }

        # Build content
        frontmatter_yaml = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        body = f"## Evidence\n- {source_path.name}\n\n## Quote\n{item.get('evidence_quote', '')}\n"
        content = f"---\n{frontmatter_yaml}---\n\n{body}"

        # Atomic write
        tmp_path = candidate_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(candidate_path))
            log.info("Wrote candidate: %s (confidence=%.2f)", candidate_path.name, fm["confidence"])
        except Exception:
            log.exception("Failed to write candidate file %s", candidate_path.name)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Scan loop ─────────────────────────────────────────────────────────────

    async def _scan(self) -> None:
        """Scan comms memories for project candidates."""
        state = self._load_state()
        processed = state.get("processed", {})

        # Collect candidates: source-type files changed since last processed
        # Query for each source type via cache
        candidates = []
        for source_type in self.SOURCE_TYPES:
            rows = await self._cache.query_by_type(source_type)
            for row in rows:
                filename = row["filename"]
                mtime = row["mtime"]

                # Skip candidate files themselves (shouldn't happen with type query, but guard)
                if filename.startswith("project-candidate-"):
                    continue

                stored_mtime = processed.get(filename)
                if stored_mtime is not None and abs(mtime - stored_mtime) < 1.0:
                    continue  # Unchanged since last scan

                candidates.append((row, mtime))

        if not candidates:
            log.debug("No new/updated source files to process for project inference")
            return

        log.info(
            "Inferring projects from %d source file(s)",
            min(len(candidates), MAX_FILES_PER_CYCLE),
        )

        processed_count = 0
        for row, mtime in candidates[:MAX_FILES_PER_CYCLE]:
            try:
                # Parse frontmatter from the cached body
                fm = json.loads(row["frontmatter"])
                # Reconstruct a minimal Path object for _extract_projects compatibility
                filename = row["filename"]
                f = MEMORIES_DIR / filename

                items = await self._extract_projects(f, fm)
                for item in items:
                    await self._write_candidate(item, f)

                # Persist state after each file to survive mid-cycle crashes
                processed[filename] = mtime
                state["processed"] = processed
                state["last_scan"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                self._save_state(state)
                processed_count += 1

            except Exception:
                log.exception("Error processing %s for project inference", filename)

        await self._cleanup_stale_candidates()

        if processed_count:
            log.info("Project inference scan complete — %d source file(s) processed", processed_count)

    async def run_loop(self, stop_event: asyncio.Event):
        """Main async loop: scan every SCAN_INTERVAL seconds."""
        if self.role != "full":
            log.debug("Project inference scanner disabled — role is %s (full required)", self.role)
            return

        ic = self._inference_config()
        enabled = ic.get("enabled", True)
        if not enabled:
            log.info("Project inference scanner disabled via config")
            return

        interval = ic.get("scan_interval_min", 15) * 60  # convert minutes to seconds
        log.info("Project inference scanner started — scanning every %ds", interval)

        while not stop_event.is_set():
            try:
                await self._scan()
            except Exception:
                log.exception("Uncaught error in project inference cycle")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
