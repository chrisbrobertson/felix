import asyncio
import logging
import re
import yaml
from datetime import datetime
from pathlib import Path

from litellm import acompletion

log = logging.getLogger("index-builder")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
INDEX_PATH = BRAIN_DIR / "index.md"
MODEL = "gemini/gemini-2.0-flash"
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
    async def _build(self):
        memory_files = sorted(
            (BRAIN_DIR / "memories").glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        if not memory_files:
            log.info("No memory files yet — skipping index build")
            return

        # Health signal: log the most recent memory mtime per hostname+browser.
        # If a watcher node's memories stop arriving for >1hr during work hours,
        # this log line will show a stale timestamp — iCloud sync stalled or daemon died.
        self._log_watcher_health(memory_files)

        # Concatenate memory files up to input cap
        chunks = []
        budget = MAX_INPUT_CHARS
        for f in memory_files:
            text = f.read_text()
            if budget <= 0:
                break
            chunks.append(text[:budget])
            budget -= len(text)

        combined = "\n\n---\n\n".join(chunks)
        n = len(chunks)
        days_span = (datetime.now() - datetime.fromtimestamp(
            memory_files[-1].stat().st_mtime)).days + 1

        try:
            response = await acompletion(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content":
                        f"Here are {n} memory entries spanning the last {days_span} days:\n\n{combined}"}
                ],
                max_tokens=700
            )
            synthesis = response.choices[0].message.content
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            INDEX_PATH.write_text(
                f"*Last updated: {timestamp} — {n} memories indexed*\n\n{synthesis}\n"
            )
            log.info(f"index.md rebuilt — {n} memories, {days_span} day span")
        except Exception as e:
            log.error(f"Index build failed: {e}")

    def _log_watcher_health(self, memory_files: list):
        """
        Parse frontmatter hostname field from recent files, log last-seen mtime
        per source. A gap >1hr during work hours means a watcher node is silent.
        Reads only the first 300 chars (frontmatter) of the 20 most recent files.
        """
        seen: dict[str, float] = {}  # hostname -> most recent mtime
        for f in memory_files[:20]:
            try:
                header = f.read_text()[:300]
                match = re.search(r'hostname:\s*(\S+)', header)
                hostname = match.group(1) if match else "unknown"
                mtime = f.stat().st_mtime
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
            try:
                config = yaml.safe_load((BRAIN_DIR / "config.yaml").read_text())
                interval = config.get("memory", {}).get("index_rebuild_interval", 3600)
            except Exception:
                interval = 3600
            await self._build()
            await asyncio.sleep(interval)
