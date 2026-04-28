import asyncio
import logging
import os
import re
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

from litellm import acompletion
from llm_routes import resolve
from usage_tracker import record_usage
from utils import load_config
from memory_cache import MemoryCache
from heartbeat import record_beat

log = logging.getLogger("index-builder")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
INDEX_PATH = BRAIN_DIR / "index.md"
MAX_INPUT_CHARS = 120_000  # cap input to indexer — summarize summaries

SYSTEM_PROMPT = """You are maintaining a rolling index for a personal second brain.
You will receive a collection of memory file summaries. Write a 400-500 word synthesis covering:
1. Main topics the person has been reading about
2. Recurring themes and emerging patterns
3. Notable connections between separate things they've read
4. Any apparent projects or goals implied by the reading pattern

Be specific — name actual tools, concepts, and ideas. Use present tense.
Do not use headers. Write flowing prose. This will be prepended to every
future conversation the person has with their AI assistant."""


class IndexBuilder:
    def __init__(self, cache: Optional[MemoryCache] = None):
        self._cache = cache if cache is not None else MemoryCache(None, BRAIN_DIR / "memories", enabled=False)

    async def _build(self):
        # Fetch all memory rows from cache
        memory_rows = await self._cache.query_all()

        if not memory_rows:
            log.info("No memory files yet — skipping index build")
            return

        # Sort by mtime descending (most recent first)
        memory_rows = sorted(memory_rows, key=lambda r: r["mtime"], reverse=True)

        # Health signal: log the most recent memory mtime per hostname+browser.
        # If a watcher node's memories stop arriving for >1hr during work hours,
        # this log line will show a stale timestamp — iCloud sync stalled or daemon died.
        self._log_watcher_health(memory_rows)

        # Concatenate memory files up to input cap
        chunks = []
        budget = MAX_INPUT_CHARS
        for row in memory_rows:
            if budget <= 0:
                break
            # Cache already has the body — no need for retries
            text = row["body"]
            chunks.append(text[:budget])
            budget -= len(text)

        combined = "\n\n---\n\n".join(chunks)
        n = len(chunks)
        days_span = (datetime.now() - datetime.fromtimestamp(
            memory_rows[-1]["mtime"])).days + 1

        try:
            response = await acompletion(
                model=resolve("summarize"),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content":
                        f"Here are {n} memory entries spanning the last {days_span} days:\n\n{combined}"}
                ],
                max_tokens=700
            )
            if hasattr(response, "usage") and response.usage:
                record_usage(resolve("summarize"), response.usage.prompt_tokens or 0, response.usage.completion_tokens or 0)
            synthesis = response.choices[0].message.content
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            content = f"*Last updated: {timestamp} — {n} memories indexed*\n\n{synthesis}\n"
            tmp = INDEX_PATH.with_suffix(".tmp")
            tmp.write_text(content)
            os.rename(tmp, INDEX_PATH)
            log.info(f"index.md rebuilt — {n} memories, {days_span} day span")
        except Exception as e:
            log.error(f"Index build failed: {e}")

        # Run deduplication check after index build
        try:
            from dedup_checker import run as dedup_run
            result = dedup_run(BRAIN_DIR / "memories", DEPLOY_DIR)
            if result["auto_merged"]:
                log.info("dedup: auto-merged %d duplicate memories", result["auto_merged"])
        except Exception as e:
            log.warning("dedup_checker failed: %s", e)

    def _log_watcher_health(self, memory_rows: list):
        """
        Parse frontmatter hostname field from recent files, log last-seen mtime
        per source. A gap >1hr during work hours means a watcher node is silent.
        Reads only the first 500 chars (header500) of the 20 most recent rows.
        """
        seen: dict[str, float] = {}  # hostname -> most recent mtime
        for row in memory_rows[:20]:
            try:
                header = row["header500"]
                match = re.search(r'hostname:\s*(\S+)', header)
                hostname = match.group(1) if match else "unknown"
                mtime = row["mtime"]
                if hostname not in seen or mtime > seen[hostname]:
                    seen[hostname] = mtime
            except Exception:
                continue
        for hostname, mtime in seen.items():
            age_min = int((datetime.now().timestamp() - mtime) / 60)
            level = log.warning if age_min > 60 else log.info
            level(f"Health: last memory from {hostname} was {age_min}min ago "
                  f"({datetime.fromtimestamp(mtime).strftime('%H:%M')})")

    async def run_loop(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            config = load_config(BRAIN_DIR / "config.yaml")
            interval = config.get("memory", {}).get("index_rebuild_interval", 3600)
            beat_status, beat_error = "ok", None
            try:
                await self._build()
            except Exception as exc:
                log.error("Index build loop error (will retry next cycle): %s", exc)
                beat_status, beat_error = "error", str(exc)
            record_beat("index_builder", beat_status, beat_error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
