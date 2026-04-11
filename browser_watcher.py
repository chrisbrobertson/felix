import asyncio
import logging
import shutil
import sqlite3
import yaml
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from memory_writer import MemoryWriter
from skill_executor import SkillExecutor

log = logging.getLogger("browser-watcher")

CHROME_HISTORY = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
FIREFOX_HISTORY = Path.home() / "Library/Application Support/Firefox/Profiles"
SEEN_URLS_FILE = Path.home() / ".second-brain-seen-urls"
CONFIG_PATH = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/config.yaml"


class BrowserWatcher:
    def __init__(self, role: str = "full"):
        self.executor = SkillExecutor("summarize-webpage", role=role)
        self.writer = MemoryWriter()
        self.seen_urls: set = self._load_seen_urls()

    def _load_seen_urls(self) -> set:
        if SEEN_URLS_FILE.exists():
            return set(SEEN_URLS_FILE.read_text().splitlines())
        return set()

    def save_seen_urls(self):
        # Called on shutdown — persists seen set so restarts don't reprocess
        SEEN_URLS_FILE.write_text("\n".join(self.seen_urls))
        log.info(f"Persisted {len(self.seen_urls)} seen URLs")

    def _copy_db(self, src: Path) -> Path:
        # Chrome locks its SQLite DB while running — must copy before reading
        tmp = Path("/tmp") / src.name
        shutil.copy2(src, tmp)
        return tmp

    def _get_firefox_history_db(self) -> Path | None:
        profiles = list(FIREFOX_HISTORY.glob("*.default-release/places.sqlite"))
        return profiles[0] if profiles else None

    def _fetch_recent_urls(self, since: datetime) -> list[dict]:
        results = []
        cutoff_chrome = int((since - datetime(1601, 1, 1)).total_seconds() * 1_000_000)

        # Chrome (epoch: 1601-01-01, microseconds)
        if CHROME_HISTORY.exists():
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

        # Firefox (epoch: Unix, microseconds)
        ff_db = self._get_firefox_history_db()
        if ff_db:
            cutoff_ff = int(since.timestamp() * 1_000_000)
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

    async def _fetch_content(self, url: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "lxml")
                    # Remove noise elements
                    for tag in soup(["script", "style", "nav", "footer",
                                     "header", "aside", "form"]):
                        tag.decompose()
                    text = soup.get_text(separator=" ", strip=True)
                    return text[:8000]
        except Exception as e:
            log.debug(f"Content fetch failed for {url}: {e}")
        return None

    async def process_url(self, entry: dict):
        content = await self._fetch_content(entry["url"])
        if not content or len(content) < 500:
            log.debug(f"Skipping {entry['url']} — insufficient content")
            return

        memory_body = await self.executor.run({
            "url": entry["url"],
            "title": entry["title"],
            "content": content
        })

        if memory_body:
            await self.writer.write(entry, memory_body)
            self.seen_urls.add(entry["url"])
            # Persist after every successful write — survive restarts
            self.save_seen_urls()
            log.info(f"Memory written: {entry['title'][:60]}")

    async def run_loop(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            # Re-read config every iteration — picks up skip_domain edits, interval
            # changes, etc. without requiring a daemon restart. It's a tiny YAML file.
            try:
                config = yaml.safe_load(CONFIG_PATH.read_text())
            except Exception as e:
                log.warning(f"Config read failed, using defaults: {e}")
                config = {}

            interval = config.get("browser_watcher", {}).get("interval_seconds", 300)

            try:
                since = datetime.now() - timedelta(seconds=interval * 2)
                entries = self._fetch_recent_urls(since)
                for entry in entries:
                    if self._should_process(entry, config):
                        await self.process_url(entry)
            except Exception as e:
                log.error(f"Browser watcher loop error: {e}")

            await asyncio.sleep(interval)
