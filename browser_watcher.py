import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from memory_writer import MemoryWriter
from skill_executor import SkillExecutor
from content_fetcher import fetch_url_content
from skill_router import detect_content_type, get_skill_and_depth
from utils import load_config

log = logging.getLogger("browser-watcher")

DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
CHROME_HISTORY = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
FIREFOX_HISTORY = Path.home() / "Library/Application Support/Firefox/Profiles"
SEEN_URLS_FILE = DEPLOY_DIR / "seen-urls"
CONFIG_PATH = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/config.yaml"

# Security (H4): bound seen_urls to prevent unbounded memory/disk growth
_MAX_SEEN_URLS = 50_000


class BrowserWatcher:
    def __init__(self, role: str = "full"):
        self._executor_pool: dict[str, SkillExecutor] = {}
        self._default_executor = SkillExecutor("summarize-webpage", role=role)
        self.writer = MemoryWriter()
        self.seen_urls: dict = self._load_seen_urls()
        # References set by daemon.py after construction
        self.skill_creator = None   # SkillCreator instance
        self.skill_optimizer = None  # SkillOptimizer instance

    def _load_seen_urls(self) -> dict:
        """Load seen URLs from file, keeping only the last _MAX_SEEN_URLS entries.
        Uses dict (not set) to maintain insertion order for FIFO eviction."""
        if SEEN_URLS_FILE.exists():
            all_lines = SEEN_URLS_FILE.read_text().splitlines()
            # Keep only the last N (most recent) URLs
            recent_lines = all_lines[-_MAX_SEEN_URLS:]
            return {url: None for url in recent_lines}
        return {}

    def _get_executor(self, skill_name: str) -> SkillExecutor:
        """Return cached SkillExecutor for skill_name, creating it if needed.
        Falls back to default summarize-webpage executor on FileNotFoundError."""
        if skill_name in self._executor_pool:
            return self._executor_pool[skill_name]
        try:
            executor = SkillExecutor(skill_name)
            self._executor_pool[skill_name] = executor
            log.debug(f"Created executor for skill: {skill_name}")
            return executor
        except FileNotFoundError:
            log.warning(f"Skill file {skill_name}.md not found, falling back to default")
            # Cache under the original name so we don't retry FileNotFoundError on every URL
            self._executor_pool[skill_name] = self._default_executor
            return self._default_executor

    def save_seen_urls(self):
        """Persist seen URLs to disk, evicting oldest entries if over limit."""
        import os as _os
        # If over limit, keep only the last _MAX_SEEN_URLS (most recent)
        if len(self.seen_urls) > _MAX_SEEN_URLS:
            # dict maintains insertion order; keep last N
            items = list(self.seen_urls.items())[-_MAX_SEEN_URLS:]
            self.seen_urls = dict(items)
            log.info(f"Evicted {len(items)} oldest URLs; keeping {_MAX_SEEN_URLS}")
        # Atomic tmp+rename to prevent partial iCloud sync on crash mid-write
        tmp = SEEN_URLS_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(self.seen_urls.keys()))
        _os.rename(tmp, SEEN_URLS_FILE)
        log.info(f"Persisted {len(self.seen_urls)} seen URLs")

    def _check_heuristics(self, output: str) -> Optional[str]:
        """Fast synchronous quality check. Returns flag reason or None."""
        if len(output) < 100:
            return "too_short"
        error_phrases = ["I cannot", "I'm unable", "Error:", "Failed to", "I don't have access"]
        if any(p in output for p in error_phrases):
            return "error_output"
        has_structure = any(marker in output for marker in ["#", "- ", "**", "1.", "2.", "3."])
        if not has_structure and len(output) < 300:
            return "unstructured"
        # verbatim_copy: unique chars in output that aren't in input is < 15% of output length
        # (Only check if we have input to compare — skip for safety if output is short)
        return None

    def _copy_db(self, src: Path) -> Path:
        # Chrome locks its SQLite DB while running — must copy before reading
        # Use unpredictable temp path to prevent symlink attacks
        fd, tmp_path_str = tempfile.mkstemp(prefix="second-brain-", suffix=f"-{src.name}")
        os.fchmod(fd, 0o600)
        os.close(fd)
        tmp = Path(tmp_path_str)
        try:
            shutil.copy2(src, tmp)
            return tmp
        except Exception:
            # Clean up on copy failure
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _get_firefox_history_db(self):
        profiles = list(FIREFOX_HISTORY.glob("*.default-release/places.sqlite"))
        return profiles[0] if profiles else None

    def _fetch_recent_urls(self, since: datetime) -> list[dict]:
        results = []
        cutoff_chrome = int((since - datetime(1601, 1, 1)).total_seconds() * 1_000_000)

        # Chrome (epoch: 1601-01-01, microseconds)
        if CHROME_HISTORY.exists():
            tmp = None
            try:
                tmp = self._copy_db(CHROME_HISTORY)
                conn = sqlite3.connect(tmp)
                rows = conn.execute("""
                    SELECT url, title, visit_count, last_visit_time
                    FROM urls
                    WHERE last_visit_time > ? AND hidden = 0
                    ORDER BY last_visit_time DESC
                """, (cutoff_chrome,)).fetchall()
                conn.close()
                for url, title, visit_count, _ in rows:
                    results.append({"url": url, "title": title,
                                    "visit_count": visit_count, "browser": "chrome"})
            except Exception as e:
                log.warning(f"Chrome history read failed: {e}")
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass

        # Firefox (epoch: Unix, microseconds)
        ff_db = self._get_firefox_history_db()
        if ff_db:
            cutoff_ff = int(since.timestamp() * 1_000_000)
            tmp = None
            try:
                tmp = self._copy_db(ff_db)
                conn = sqlite3.connect(tmp)
                rows = conn.execute("""
                    SELECT p.url, p.title, p.visit_count
                    FROM moz_places p
                    JOIN moz_historyvisits v ON p.id = v.place_id
                    WHERE v.visit_date > ?
                    GROUP BY p.url
                """, (cutoff_ff,)).fetchall()
                conn.close()
                for url, title, visit_count in rows:
                    results.append({"url": url, "title": title,
                                    "visit_count": visit_count, "browser": "firefox"})
            except Exception as e:
                log.warning(f"Firefox history read failed: {e}")
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass

        return results

    def _should_process(self, entry: dict, config: dict) -> bool:
        url = entry["url"]
        if url in self.seen_urls:
            return False
        if not url.startswith("http"):
            return False
        skip = config.get("browser_watcher", {}).get("skip_domains", [])
        if any(d in url for d in skip):
            return False
        return True

    async def _fetch_content(self, url: str):
        """Fetch page content. Returns cleaned text string or None on failure."""
        _, text = await fetch_url_content(url)
        return text or None

    async def process_url(self, entry: dict):
        content = await self._fetch_content(entry["url"])
        if not content or len(content) < 500:
            log.debug(f"Skipping {entry['url']} — insufficient content")
            return

        # Detect content type and route to appropriate skill
        content_type = detect_content_type(
            url=entry["url"],
            content=content[:3000],
        )
        word_count = len(content.split())
        skill_name, depth = get_skill_and_depth(content_type, word_count)
        executor = self._get_executor(skill_name)

        # If we fell back to default for a non-default type, signal gap to skill_creator
        if content_type != "default" and executor is self._default_executor and self.skill_creator:
            asyncio.create_task(
                self.skill_creator.handle_gap(content_type, entry["url"], content[:500])
            )

        # Add content_type to entry metadata for memory_writer
        entry = {**entry, "content_type": content_type}

        memory_body = await executor.run({
            "url": entry["url"],
            "title": entry["title"],
            "content": content
        })

        if not memory_body:
            return

        # FR-15: Heuristic pre-filter — check quality before writing
        flag = self._check_heuristics(memory_body)
        if flag and self.skill_optimizer:
            self.skill_optimizer.add_to_urgent_queue({
                "skill_name": skill_name,
                "execution_timestamp": datetime.now().isoformat(),
                "flag_reason": flag,
                "input_snippet": content[:200],
                "output_snippet": memory_body[:200],
            })

        # Check if skill is in probation — shadow mode (run but don't write)
        in_probation = False
        if self.skill_creator and skill_name != "summarize-webpage":
            registry = self.skill_creator._load_registry()
            skill_entry = registry.get("skills", {}).get(skill_name, {})
            if skill_entry.get("status") == "probation":
                in_probation = True
                self.skill_creator.increment_probation(skill_name)
                log.debug(f"Shadow execution (probation): {skill_name} for {entry['url'][:60]}")

        if not in_probation:
            await self.writer.write(entry, memory_body, depth=depth)
            self.seen_urls[entry["url"]] = None
            self.save_seen_urls()
            log.info(f"Memory written: {entry['title'][:60]} [{content_type}, {depth}]")
        else:
            # Still mark as seen so we don't re-process on next cycle
            self.seen_urls[entry["url"]] = None
            self.save_seen_urls()

    async def backfill(self, days: int) -> dict:
        """Reprocess URLs from the last N days (max 90). Returns dict with counts."""
        days = min(days, 90)
        since = datetime.now() - timedelta(days=days)

        config = load_config(CONFIG_PATH)

        entries = await asyncio.to_thread(self._fetch_recent_urls, since)

        processed = 0
        skipped = 0
        errors = 0

        for entry in entries:
            url = entry["url"]

            # Remove from seen_urls so _should_process will return True
            if url in self.seen_urls:
                del self.seen_urls[url]

            # Now check if we should process (domain filter, etc.)
            if not self._should_process(entry, config):
                skipped += 1
                continue

            try:
                await self.process_url(entry)
                processed += 1
            except Exception as e:
                log.error(f"Backfill error processing {url}: {e}")
                errors += 1

        self.save_seen_urls()

        return {
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "notes": f"Scanned {days} days of browser history"
        }

    async def run_loop(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            # Re-read config every iteration — picks up skip_domain edits, interval
            # changes, etc. without requiring a daemon restart. It's a tiny YAML file.
            config = load_config(CONFIG_PATH)

            interval = config.get("browser_watcher", {}).get("interval_seconds", 300)

            try:
                since = datetime.now() - timedelta(seconds=interval * 2)
                entries = await asyncio.to_thread(self._fetch_recent_urls, since)
                for entry in entries:
                    if self._should_process(entry, config):
                        await self.process_url(entry)
            except Exception as e:
                log.error(f"Browser watcher loop error: {e}")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
