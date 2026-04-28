"""Local SQLite read-cache for memory files.

All 14 daemon loops on the full-role machine read memories via this cache
instead of directly from iCloud, eliminating EDEADLK read amplification.

The cache is fully derivative — rm memory-cache.sqlite at any time and it
repopulates lazily. iCloud remains the authoritative store.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Union

import yaml

from utils import read_text_with_retry_async, glob_memories, is_conflict_copy

log = logging.getLogger("memory-cache")


class _FrontmatterEncoder(json.JSONEncoder):
    """JSON encoder that converts YAML-parsed date/datetime objects to ISO strings."""

    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


def _fm_dumps(fm: dict) -> str:
    """Serialize frontmatter dict to JSON, handling date objects from YAML parsing."""
    return json.dumps(fm, cls=_FrontmatterEncoder)


# Schema version for future migrations
_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
  filename       TEXT PRIMARY KEY,
  mtime          REAL NOT NULL,
  size           INTEGER NOT NULL,
  type           TEXT,
  status         TEXT,
  prefix         TEXT,
  frontmatter    TEXT,
  header500      TEXT,
  body           TEXT,
  indexed_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_type   ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_prefix ON memories(prefix);
"""

_PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA mmap_size=268435456;
PRAGMA busy_timeout=5000;
"""


def _parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter from a memory file. Returns {} on any failure."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


def _extract_prefix(filename: str) -> str:
    """Derive prefix from filename.

    Examples:
        calendar-event-macstudio-2026-04-25-slug-abc123.md → calendar-event
        email-thread-slug-abc123.md → email-thread
        2026-04-25-slug-abc123.md → (empty string for browser captures)
    """
    # Known multi-word prefixes
    if filename.startswith("calendar-event-"):
        return "calendar-event"
    if filename.startswith("email-thread-"):
        return "email-thread"
    if filename.startswith("project-candidate-"):
        return "project-candidate"
    if filename.startswith("feature-request-"):
        return "feature-request"
    if filename.startswith("slack-thread-"):
        return "slack-thread"

    # Single-word prefixes
    parts = filename.split("-")
    if parts[0] in ("commitment", "contact", "goal", "project", "action", "code", "meeting"):
        return parts[0]

    # YYYY-MM-DD pattern → browser capture (no prefix)
    if re.match(r'^\d{4}-\d{2}-\d{2}-', filename):
        return ""

    return ""


class MemoryCache:
    """SQLite cache for memory files.

    Pass-through mode (db_path=None or enabled=False): all methods route
    straight to read_text_with_retry_async with no SQLite file created.
    One codepath, one knob, zero branches at call sites.
    """

    def __init__(
        self,
        db_path: Optional[Path],
        memories_dir: Path,
        *,
        enabled: bool = True
    ) -> None:
        self._memories_dir = memories_dir
        self._enabled = enabled and db_path is not None
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

        if not self._enabled:
            log.info("MemoryCache in pass-through mode (cache disabled or db_path=None)")
            return

        # Attempt to open the database; handle corruption by recreating
        try:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

            # Apply PRAGMAs
            for pragma in _PRAGMAS.strip().split("\n"):
                self._conn.execute(pragma)

            # Create schema if needed
            for stmt in _SCHEMA_SQL.strip().split(";"):
                if stmt.strip():
                    self._conn.execute(stmt)
            self._conn.commit()

            log.info(f"MemoryCache initialized at {db_path}")
        except sqlite3.DatabaseError as e:
            log.warning(f"Corrupt cache DB at {db_path}: {e} — recreating")
            self._conn = None
            try:
                db_path.unlink()
            except Exception:
                pass

            # Retry once after unlinking
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            for pragma in _PRAGMAS.strip().split("\n"):
                self._conn.execute(pragma)
            for stmt in _SCHEMA_SQL.strip().split(";"):
                if stmt.strip():
                    self._conn.execute(stmt)
            self._conn.commit()
            log.info(f"MemoryCache recreated at {db_path}")

    async def get(self, filename: str) -> Optional[dict]:
        """Get a single memory file by filename.

        Returns dict with keys: filename, mtime, type, status, prefix,
        frontmatter (as JSON string), header500, body.

        Returns None if file doesn't exist. On cache miss, falls through
        to iCloud read and opportunistically populates the cache.
        """
        if not self._enabled:
            # Pass-through mode
            path = self._memories_dir / filename
            text = await read_text_with_retry_async(path, default=None)
            if text is None:
                return None

            fm = _parse_frontmatter(text)
            stat = path.stat()
            return {
                "filename": filename,
                "mtime": stat.st_mtime,
                "type": fm.get("type"),
                "status": fm.get("status"),
                "prefix": _extract_prefix(filename),
                "frontmatter": _fm_dumps(fm),
                "header500": text[:500],
                "body": text,
            }

        # Try cache first
        row = self._conn.execute(
            "SELECT * FROM memories WHERE filename = ?", (filename,)
        ).fetchone()

        if row:
            return dict(row)

        # Cache miss — fall through to iCloud and populate
        await self.invalidate(filename)

        # Re-query
        row = self._conn.execute(
            "SELECT * FROM memories WHERE filename = ?", (filename,)
        ).fetchone()

        return dict(row) if row else None

    async def query_by_type(
        self, type_: str, *, status: Optional[str] = None
    ) -> list:
        """Query all files of a given type, optionally filtered by status."""
        if not self._enabled:
            # Pass-through mode
            results = []
            for path in glob_memories(self._memories_dir, "*.md"):
                text = await read_text_with_retry_async(path, default=None)
                if text is None:
                    continue
                fm = _parse_frontmatter(text)
                if fm.get("type") != type_:
                    continue
                if status is not None and fm.get("status") != status:
                    continue
                stat = path.stat()
                results.append({
                    "filename": path.name,
                    "mtime": stat.st_mtime,
                    "type": fm.get("type"),
                    "status": fm.get("status"),
                    "prefix": _extract_prefix(path.name),
                    "frontmatter": _fm_dumps(fm),
                    "header500": text[:500],
                    "body": text,
                })
            return results

        # Cache mode
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE type = ?", (type_,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE type = ? AND status = ?",
                (type_, status)
            ).fetchall()

        return [dict(row) for row in rows]

    async def query_by_prefix(self, prefix: str) -> list:
        """Query all files matching a filename prefix (e.g. 'calendar-event-')."""
        if not self._enabled:
            # Pass-through mode
            results = []
            for path in glob_memories(self._memories_dir, f"{prefix}*.md"):
                text = await read_text_with_retry_async(path, default=None)
                if text is None:
                    continue
                fm = _parse_frontmatter(text)
                stat = path.stat()
                results.append({
                    "filename": path.name,
                    "mtime": stat.st_mtime,
                    "type": fm.get("type"),
                    "status": fm.get("status"),
                    "prefix": _extract_prefix(path.name),
                    "frontmatter": _fm_dumps(fm),
                    "header500": text[:500],
                    "body": text,
                })
            return results

        # Cache mode — use the prefix column
        # Strip trailing wildcard and separator so callers can pass either
        # "calendar-event" or "calendar-event-" and get the same result.
        clean_prefix = prefix.rstrip("*-")
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE prefix = ?", (clean_prefix,)
        ).fetchall()
        results = [dict(row) for row in rows]

        # Disk fallback: pick up files written since the last sweep that
        # haven't been indexed yet (e.g. cold start or very first scan).
        # Filter by _extract_prefix to stay semantically identical to the
        # SQL query — avoids broadening results when the prefix is a true
        # prefix of another prefix (e.g. "project" vs "project-candidate").
        cached_names = {r["filename"] for r in results}
        for path in glob_memories(self._memories_dir, f"{clean_prefix}*.md"):
            if path.name in cached_names:
                continue
            if _extract_prefix(path.name) != clean_prefix:
                continue
            text = await read_text_with_retry_async(path, default=None)
            if text is None:
                continue
            fm = _parse_frontmatter(text)
            try:
                stat = path.stat()
                mtime = stat.st_mtime
                size = stat.st_size
            except OSError:
                mtime = 0.0
                size = 0
            results.append({
                "filename": path.name,
                "mtime": mtime,
                "size": size,
                "type": fm.get("type"),
                "status": fm.get("status"),
                "prefix": clean_prefix,
                "frontmatter": _fm_dumps(fm),
                "header500": text[:500],
                "body": text,
                "indexed_at": 0.0,
            })
        return results

    async def query_all(
        self, *, exclude_types: Optional[list] = None
    ) -> list:
        """Return every cached memory row, optionally excluding given type values.

        Used by full-corpus consumers (index_builder, circle_sync_scanner,
        synthesis_scanner, goal_project_agent's related-memory scan). Pass-through
        mode globs MEMORIES_DIR and reads each file directly — no worse than the
        glob+read pattern these consumers do today.
        """
        excluded = set(exclude_types or ())

        if not self._enabled:
            # Pass-through mode
            results = []
            for path in glob_memories(self._memories_dir, "*.md"):
                text = await read_text_with_retry_async(path, default=None)
                if text is None:
                    continue
                fm = _parse_frontmatter(text)
                t = fm.get("type")
                if t in excluded:
                    continue
                stat = path.stat()
                results.append({
                    "filename": path.name,
                    "mtime": stat.st_mtime,
                    "type": t,
                    "status": fm.get("status"),
                    "prefix": _extract_prefix(path.name),
                    "frontmatter": _fm_dumps(fm),
                    "header500": text[:500],
                    "body": text,
                })
            return results

        # Cache mode
        if not excluded:
            rows = self._conn.execute("SELECT * FROM memories").fetchall()
        else:
            placeholders = ",".join("?" * len(excluded))
            rows = self._conn.execute(
                f"SELECT * FROM memories WHERE type IS NULL OR type NOT IN ({placeholders})",
                tuple(excluded),
            ).fetchall()

        return [dict(row) for row in rows]

    async def score_keywords(self, query: str, top_n: int = 50) -> list:
        """Score all memories by keyword intersection against header500.

        Same algorithm as chat_handler._score_relevance: count query tokens
        (3+ chars) found in the first 500 chars of each file.

        Returns [(filename, score), ...] sorted by score descending.
        """
        # Tokenize query: 3+ char words
        tokens = {w for w in re.findall(r'\b\w{3,}\b', query.lower())}
        if not tokens:
            return []

        if not self._enabled:
            # Pass-through mode
            scored = []
            for path in glob_memories(self._memories_dir, "*.md"):
                text = await read_text_with_retry_async(path, default=None)
                if text is None:
                    continue
                header = text[:500].lower()
                score = sum(1 for t in tokens if t in header)
                if score > 0:
                    scored.append((path.name, float(score)))

            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_n]

        # Cache mode — load all header500 in one query
        rows = self._conn.execute(
            "SELECT filename, header500 FROM memories"
        ).fetchall()

        scored = []
        for row in rows:
            header = (row["header500"] or "").lower()
            score = sum(1 for t in tokens if t in header)
            if score > 0:
                scored.append((row["filename"], float(score)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    async def invalidate(self, filename: str) -> None:
        """Re-read a file from iCloud and upsert into cache.

        If the file is missing, delete the row. If iCloud returns EDEADLK
        that persists past retries, log a warning and skip (will retry on
        next sweep).
        """
        if not self._enabled:
            return  # No-op in pass-through mode

        path = self._memories_dir / filename
        text = await read_text_with_retry_async(path, default=None)

        if text is None:
            if path.exists():
                # File is present on disk but unreadable — iCloud EDEADLK most likely.
                # Keep the existing cache row (if any) and let the next sweep retry.
                log.warning("invalidate: %s — exists but unreadable, will retry", filename)
                return
            # File is genuinely gone — remove it from cache.
            self._conn.execute("DELETE FROM memories WHERE filename = ?", (filename,))
            self._conn.commit()
            return

        # Parse frontmatter
        fm = _parse_frontmatter(text)

        try:
            stat = path.stat()
        except OSError as e:
            log.warning(f"invalidate: stat failed for {filename}: {e}")
            return

        # Upsert row
        self._conn.execute(
            """
            INSERT OR REPLACE INTO memories (
                filename, mtime, size, type, status, prefix,
                frontmatter, header500, body, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                filename,
                stat.st_mtime,
                stat.st_size,
                fm.get("type"),
                fm.get("status"),
                _extract_prefix(filename),
                _fm_dumps(fm),
                text[:500],
                text,
                time.time(),
            )
        )
        self._conn.commit()

    async def sweep(self) -> tuple[int, int, int]:
        """Sync cache with MEMORIES_DIR via mtime diff.

        Returns (added, updated, removed) counts.
        """
        if not self._enabled:
            return (0, 0, 0)

        # Load current cache state
        cache_files = {}
        for row in self._conn.execute("SELECT filename, mtime, size FROM memories"):
            cache_files[row["filename"]] = (row["mtime"], row["size"])

        # Scan directory (one syscall via os.scandir)
        disk_files = {}
        try:
            for entry in os.scandir(self._memories_dir):
                if not entry.name.endswith(".md"):
                    continue
                if is_conflict_copy(Path(entry.path)):
                    continue
                try:
                    disk_files[entry.name] = (entry.stat().st_mtime, entry.stat().st_size)
                except OSError:
                    pass
        except OSError as e:
            log.warning(f"sweep: scandir failed: {e}")
            return (0, 0, 0)

        # Find diffs
        to_invalidate = []
        for filename, (mtime, size) in disk_files.items():
            cached = cache_files.get(filename)
            if cached is None:
                # New file
                to_invalidate.append(filename)
            elif cached[0] != mtime or cached[1] != size:
                # Changed file
                to_invalidate.append(filename)

        # Find deleted files
        to_remove = [f for f in cache_files if f not in disk_files]

        # Apply changes
        added = 0
        updated = 0
        for filename in to_invalidate:
            was_in_cache = filename in cache_files
            await self.invalidate(filename)
            if was_in_cache:
                updated += 1
            else:
                added += 1

        for filename in to_remove:
            self._conn.execute("DELETE FROM memories WHERE filename = ?", (filename,))

        if to_remove:
            self._conn.commit()

        removed = len(to_remove)

        if added or updated or removed:
            log.info(f"Sweep complete: {added} added, {updated} updated, {removed} removed")

        return (added, updated, removed)

    async def rebuild(self) -> int:
        """Wipe cache and repopulate from scratch.

        Returns count of indexed files.
        """
        if not self._enabled:
            return 0

        # Wipe table
        self._conn.execute("DELETE FROM memories")
        self._conn.commit()
        log.info("Cache wiped for rebuild")

        # Repopulate
        count = 0
        for path in glob_memories(self._memories_dir, "*.md"):
            await self.invalidate(path.name)
            count += 1

        log.info(f"Rebuild complete: {count} files indexed")
        return count

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
