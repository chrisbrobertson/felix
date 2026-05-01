"""
circle_sync_scanner.py — Circle Sync Scanner async loop (full role only).

Runs every 5 minutes (configurable). Reads circle ruleset YAML files from
~/secondbrain/circles/, applies rules to MEMORIES_DIR, and atomically syncs
matching files into per-circle iCloud shared folders. Implements deletion
propagation when files no longer match.
"""
import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from circle_ruleset import CircleRuleset, load_ruleset, should_sync
from heartbeat import record_beat

log = logging.getLogger("circle-sync")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))

DEFAULT_ICLOUD_ROOT = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
DEFAULT_CIRCLES_DIR = DEPLOY_DIR / "circles"
DEFAULT_SCAN_INTERVAL = 300
STATE_FILE = DEPLOY_DIR / "circle-sync-state.json"


def _parse_frontmatter(text: str) -> dict:
    """
    Parse YAML frontmatter from markdown text.
    Returns {} on any error.
    """
    try:
        m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not m:
            return {}
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


class CircleSyncScanner:
    """
    Circle sync scanner — rule-based one-way memory file sync to iCloud shared folders.
    """

    def __init__(self, role: str = "full", cache=None):
        self.role = role
        self._config: dict = {}
        self._enabled: bool = False
        self._circles_dir: Path = DEFAULT_CIRCLES_DIR
        self._icloud_root: Path = DEFAULT_ICLOUD_ROOT
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL
        self._state: dict = {}   # {circle_slug: {synced_files: {filename: mtime}, last_run: str}}
        # Cache: MemoryCache instance for queries, or None (defaults to pass-through)
        if cache is None:
            from memory_cache import MemoryCache
            cache = MemoryCache(None, MEMORIES_DIR, enabled=False)
        self._cache = cache

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        """Main async loop — runs every N seconds until stop_event is set."""
        if self.role != "full":
            return

        self._load_config()
        if not self._enabled:
            log.info("Circle sync disabled (circles.enabled: false) — loop exiting")
            return

        self._load_state()

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await self._run_cycle()
            except Exception as exc:
                log.exception("Circle sync cycle error")
                beat_status, beat_error = "error", str(exc)
            record_beat("circle_sync_scanner", beat_status, beat_error)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._scan_interval)
            except asyncio.TimeoutError:
                pass

    def _load_config(self) -> None:
        """Load config from CONFIG_PATH and set instance variables."""
        try:
            self._config = yaml.safe_load(CONFIG_PATH.read_text())
        except Exception as e:
            log.warning("Circle sync: failed to load config: %s", e)
            self._config = {}

        circles_cfg = self._config.get("circles", {})
        self._enabled = circles_cfg.get("enabled", False)

        circles_dir_str = circles_cfg.get("dir", str(DEFAULT_CIRCLES_DIR))
        self._circles_dir = Path(circles_dir_str).expanduser()

        icloud_root_str = circles_cfg.get("icloud_root", str(DEFAULT_ICLOUD_ROOT))
        self._icloud_root = Path(icloud_root_str).expanduser()

        self._scan_interval = circles_cfg.get("scan_interval_seconds", DEFAULT_SCAN_INTERVAL)

    def _load_state(self) -> None:
        """Load state from STATE_FILE. Never raises."""
        try:
            if STATE_FILE.exists():
                self._state = json.loads(STATE_FILE.read_text())
            else:
                self._state = {}
        except Exception as e:
            log.warning("Circle sync: failed to load state: %s", e)
            self._state = {}

    def _save_state(self) -> None:
        """Atomically save state to STATE_FILE."""
        tmp_path = STATE_FILE.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(self._state, indent=2))
            os.rename(str(tmp_path), str(STATE_FILE))
        except Exception as e:
            log.error("Circle sync: failed to save state: %s", e)
            try:
                tmp_path.unlink()
            except OSError:
                pass

    async def _run_cycle(self) -> None:
        """
        One scan cycle:
        1. Load all *.yaml rulesets from circles_dir
        2. Load all memory file frontmatters once
        3. For each circle, sync matching files to iCloud folder
        """
        if not self._circles_dir.exists():
            log.debug("Circles dir not found: %s", self._circles_dir)
            return

        ruleset_files = list(self._circles_dir.glob("*.yaml"))
        if not ruleset_files:
            log.debug("No circle rulesets found in %s", self._circles_dir)
            return

        # Load all memory file frontmatters once
        memory_files = await self._load_memory_frontmatters()

        for ruleset_path in ruleset_files:
            try:
                ruleset = load_ruleset(ruleset_path)
            except ValueError as e:
                log.error("Skipping malformed circle ruleset %s: %s", ruleset_path.name, e)
                continue
            await self._sync_circle(ruleset, memory_files)

        self._save_state()

    async def _load_memory_frontmatters(self) -> dict[str, dict]:
        """
        Load frontmatter from all .md files in MEMORIES_DIR via cache.
        Returns {filename: frontmatter_dict}.
        """
        result = {}
        if not MEMORIES_DIR.exists():
            return result

        all_rows = await self._cache.query_all()
        for row in all_rows:
            try:
                fm = json.loads(row["frontmatter"])
                if fm:
                    result[row["filename"]] = fm
            except Exception as e:
                log.debug("Circle sync: failed to parse frontmatter for %s: %s", row["filename"], e)
                continue

        return result

    async def _sync_circle(self, ruleset: CircleRuleset, memory_files: dict) -> None:
        """
        Sync one circle:
        1. Determine desired files (those matching include rules, not exclude)
        2. Remove stale files (in state but not desired)
        3. Add/update files (desired but not in state, or newer mtime)
        """
        circle_state = self._state.setdefault(
            ruleset.slug,
            {"synced_files": {}, "last_run": None}
        )

        # Resolve iCloud folder path
        icloud_path = self._icloud_root / ruleset.icloud_folder
        if not icloud_path.exists():
            log.warning(
                "Circle '%s': iCloud folder not found: %s — skipping",
                ruleset.slug, icloud_path
            )
            return

        synced: dict = circle_state["synced_files"]

        # Determine desired set: {filename: source_path}
        desired: dict[str, Path] = {}
        for filename, fm in memory_files.items():
            if should_sync(ruleset, fm):
                desired[filename] = MEMORIES_DIR / filename

        # Files in state but not desired → delete
        stale = set(synced.keys()) - set(desired.keys())
        for filename in stale:
            target = icloud_path / filename
            try:
                target.unlink()
                log.info("Circle '%s': removed %s", ruleset.slug, filename)
            except FileNotFoundError:
                log.debug("Circle '%s': already gone: %s", ruleset.slug, filename)
            except OSError as e:
                log.error("Circle '%s': delete error for %s: %s", ruleset.slug, filename, e)
            # Remove from state regardless
            del synced[filename]

        # Files desired but not in state, or mtime newer → sync
        for filename, src_path in desired.items():
            try:
                src_mtime = src_path.stat().st_mtime
            except OSError:
                log.debug("Circle '%s': source gone: %s", ruleset.slug, filename)
                continue

            last_mtime = synced.get(filename)
            if last_mtime is not None and src_mtime <= last_mtime:
                continue  # unchanged

            # Atomic write
            target = icloud_path / filename
            tmp_path = target.with_suffix(".tmp")
            try:
                shutil.copy2(str(src_path), str(tmp_path))
                os.rename(str(tmp_path), str(target))
                synced[filename] = src_mtime
                log.info("Circle '%s': synced %s", ruleset.slug, filename)
            except OSError as e:
                log.error("Circle '%s': write error for %s: %s", ruleset.slug, filename, e)
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        circle_state["last_run"] = datetime.now(timezone.utc).isoformat()
