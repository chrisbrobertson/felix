# Second Brain — System Spec v1.0

**Target:** Working, usable system by end of day tomorrow  
**Philosophy:** Karpathy flat-file pattern. No vector DB, no graph, no embedding model. Files + LLM = database.  
**Author:** Chris Robertson  
**Date:** 2026-04-11  
**Changelog:**
- v1.0 — released. Seven review rounds, zero blocking issues remaining.
- v0.6 — section numbering fixed, signal handler moved after setup, full-node imports guarded inside role block
- v0.5 — role gate in daemon (watcher/full), guarded finally block, local execution logging on watcher nodes, atomic memory writes, health signal in index_builder
- v0.4 — header cache for relevance scoring, skill_optimizer stubbed, Telegram 4096-char chunker
- v0.3 — filename collision fix, config hot-reload, keyword relevance filter, index_builder.py spec'd
- v0.2 — patched 4 blocking bugs + 4 recommended fixes from code review
- v0.1 — initial

---

## 1. Guiding Principles

1. **Flat files are the database.** Every memory and skill is a markdown file. The LLM is the index.
2. **Dead simple beats theoretically optimal.** No infra you don't need yet.
3. **Local-first storage, cloud LLM processing.** iCloud for sync, LiteLLM for model routing.
4. **The daemon is the brain stem.** One Python process, always running, three jobs.
5. **Telegram is the terminal.** All human interaction goes through the bot.

## 1.1. Key Design Decisions

**Local SQLite read-cache:** All 14 daemon loops on the full-role machine read memories via `MemoryCache` (backed by `~/secondbrain/memory-cache.sqlite`), never directly from `MEMORIES_DIR`. iCloud remains the authoritative store and the watcher → full transport; the cache is a derived read-side accelerator only. The watcher role does not run the cache — it reads only its own write-namespace from iCloud directly. Cache misses fall through to `read_text_with_retry_async`. Two-layer invalidation: immediate `cache.invalidate()` on local writes, 60-second sweep for iCloud-arrived files. The cache is fully derivative — `rm memory-cache.sqlite` at any time and it repopulates lazily.

---

## 2. Directory Layout

```
~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/
│
├── memories/
│   ├── 2026-04-11-anthropic-mcp-spec.md
│   ├── 2026-04-11-litellm-routing-patterns.md
│   └── ...
│
├── skills/
│   ├── summarize-webpage.md
│   ├── chat.md
│   ├── connect-memories.md
│   ├── morning-briefing.md
│   └── skill-optimizer.md
│
├── inbox/
│   └── (raw browser captures pending processing)
│
├── index.md          ← rolling summary, LLM-maintained
└── config.yaml       ← routing rules, thresholds, user prefs
```

iCloud Drive path ensures automatic sync to iPhone and iPad with zero additional infrastructure.

---

## 3. Memory File Schema

Every memory is a markdown file with YAML frontmatter.

```markdown
---
id: 2026-04-11-litellm-routing-patterns
created: 2026-04-11T14:32:00
source_url: https://docs.litellm.ai/docs/routing
source_title: LiteLLM Router Documentation
dwell_seconds: 312
visit_count: 1
tags: [litellm, routing, llm, infrastructure]
skill_used: summarize-webpage
skill_version: 2
model_used: gemini/gemini-2.0-flash
---

## Summary
LiteLLM's router supports fallback chains, load balancing across providers,
and spend tracking via a single OpenAI-compatible interface...

## Key Points
- Fallback chains defined in config YAML, not code
- Supports custom api_base for local endpoints
- Built-in retry with exponential backoff

## Entities
- **LiteLLM**: open-source LLM proxy/router
- **Router config**: YAML file at ~/.litellm/config.yaml

## Connections
<!-- LLM-populated on next connect-memories pass -->
```

**Filename convention:** `YYYY-MM-DD-{title-slug}-{6-char-url-hash}.md`  
The URL hash suffix eliminates collisions between pages with similar titles on the same day. Human-readable slug is kept for browsability; hash makes it unique.  
**Max file size:** ~2KB. If content is longer, summarize harder.

---

## 4. Skill File Schema

Skills are self-logging markdown files. The executor reads the instructions, runs them, and appends the outcome.

```markdown
---
name: summarize-webpage
version: 2
preferred_model: gemini/gemini-2.0-flash
fallback_model: claude-haiku-4-5-20251001
success_rate: 0.84
total_runs: 47
last_optimized: 2026-04-10
---

## Instructions

You are creating a long-term memory entry from a webpage.

Given the page title, URL, and raw content below, produce a memory file body with:
1. A 2-3 sentence **Summary** of the page's core idea
2. **Key Points** — 3-7 bullet points of the most important facts or ideas
3. **Entities** — named things (people, tools, concepts, companies) worth remembering
4. **Tags** — 3-6 lowercase tags for retrieval

Be ruthlessly concise. Omit navigation, ads, boilerplate. Focus on what a smart person
would want to remember about this page six months from now.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log
### v2 (2026-04-10) — added Entities section, improved tag quality
### v1 (2026-04-07) — initial version

## Execution History

| date | url_slug | model | score | notes |
|------|----------|-------|-------|-------|
| 2026-04-11 | litellm-routing | gemini-flash | 0.91 | clean output |
| 2026-04-10 | anthropic-mcp | gemini-flash | 0.78 | missed key entities |
| 2026-04-10 | some-news-article | gemini-flash | 0.55 | paywalled, poor content |
```

**Score rubric (for optimizer):**
- `1.0` — excellent summary, good tags, useful entities
- `0.7` — acceptable, minor gaps
- `0.5` — weak, missing key content
- `0.0` — failed / junk output

Scores are assigned by the skill-optimizer on its daily pass, using LLM-as-judge against the source content.

---

## 5. Config File

```yaml
# ~/iCloud Drive/second-brain/config.yaml

daemon:
  role: full
  # Role controls which tasks this machine runs. Set per-machine in a local
  # override or just edit before launch. Valid values:
  #
  #   full     — browser_watcher + telegram bot + index_builder + skill_optimizer
  #              Run on your primary always-on machine (Mac Studio / Mac Mini).
  #              Needs ANTHROPIC_API_KEY + GEMINI_API_KEY.
  #
  #   watcher  — browser_watcher only. Summarizes pages visited on this machine,
  #              writes memory files to iCloud. No Telegram, no index, no optimizer.
  #              Only needs GEMINI_API_KEY (summarize-webpage uses gemini-flash).
  #              Run on MacBook Pro when traveling.

user:
  telegram_user_id: YOUR_TELEGRAM_USER_ID   # whitelist — bot ignores all others
  name: Chris
  timezone: America/Los_Angeles

telegram:
  bot_token: YOUR_BOT_TOKEN                 # from @BotFather — only needed on full node

browser_watcher:
  interval_seconds: 300          # poll every 5 minutes
  min_dwell_seconds: 30          # ignore pages visited < 30s
  min_content_chars: 500         # ignore stubs, redirects
  skip_domains:                  # noise filter
    - google.com
    - googleadservices.com
    - doubleclick.net
    - facebook.com
    - twitter.com
    - t.co
    - linkedin.com
    - youtube.com                # optional — remove if you want YT titles captured

litellm:
  # Local endpoints
  providers:
    - name: mac-studio
      api_base: http://mac-studio.local:8000/v1
      api_key: none
      models: [nemotron-cascade-2]
    - name: dgx-spark
      api_base: http://dgx-spark.local:8001/v1
      api_key: none
      models: [qwen3-coder, minimax]

skill_optimizer:
  run_hour: 3                    # 3am daily
  min_runs_before_optimize: 10   # don't optimize on sparse data
  underperformance_threshold: 0.70

memory:
  max_context_memories: 20       # max files loaded into chat context
  index_rebuild_interval: 3600   # rebuild index.md every hour
```

---

## 6. LiteLLM Routing Config

```yaml
# ~/.litellm/config.yaml

model_list:
  # Summarization — high volume, cheap, fast
  - model_name: summarize
    litellm_params:
      model: gemini/gemini-2.0-flash
      api_key: os.environ/GEMINI_API_KEY
    model_info:
      mode: chat

  # Chat / reasoning — quality matters
  - model_name: chat
    litellm_params:
      model: claude-sonnet-4-20250514
      api_key: os.environ/ANTHROPIC_API_KEY
    model_info:
      mode: chat

  # Skill optimization — needs instruction-following
  - model_name: optimizer
    litellm_params:
      model: claude-sonnet-4-20250514
      api_key: os.environ/ANTHROPIC_API_KEY
    model_info:
      mode: chat

  # Skill judge — fast/cheap scoring of execution history rows
  - model_name: judge
    litellm_params:
      model: claude-haiku-4-5-20251001
      api_key: os.environ/ANTHROPIC_API_KEY
    model_info:
      mode: chat

  # Local fallback — Mac Studio
  - model_name: local
    litellm_params:
      model: openai/nemotron-cascade-2
      api_base: http://mac-studio.local:8000/v1
      api_key: none
    model_info:
      mode: chat

router_settings:
  num_retries: 2
  fallbacks:
    - summarize: [local]
    - chat: [local]
```

---

## 7. The Daemon

### 7.1 Process Structure

```
second_brain/
├── daemon.py              ← entry point, starts all loops
├── browser_watcher.py     ← Chrome/Firefox SQLite reader
├── skill_executor.py      ← loads skill file, calls LiteLLM, logs outcome
├── memory_writer.py       ← writes/updates memory markdown files
├── chat_handler.py        ← Telegram bot logic
├── index_builder.py       ← maintains index.md
├── skill_optimizer.py     ← daily skill improvement pass
└── utils.py               ← shared helpers
```

### 7.2 daemon.py

```python
import asyncio, signal, logging, yaml
from pathlib import Path
from browser_watcher import BrowserWatcher
# Full-node imports are deferred into the role=="full" block below.
# Watcher nodes may not have python-telegram-bot installed — a top-level
# import would crash before the role check is ever reached.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("second-brain")

CONFIG_PATH = Path.home() / \
    "Library/Mobile Documents/com~apple~CloudDocs/second-brain/config.yaml"

async def main():
    config = yaml.safe_load(CONFIG_PATH.read_text())
    # Env var takes precedence over config.yaml — config is shared via iCloud,
    # role is per-machine. Set SECOND_BRAIN_ROLE in each machine's launchd plist.
    import os
    role = os.environ.get("SECOND_BRAIN_ROLE") or config.get("daemon", {}).get("role", "full")
    log.info(f"Starting second-brain daemon — role: {role}")

    watcher = BrowserWatcher(role=role)
    tasks = [watcher.run_loop]

    # Full node only: import and instantiate after role check so watcher
    # nodes never touch packages that may not be installed.
    chat = None
    if role == "full":
        from chat_handler import TelegramChatHandler
        from skill_optimizer import SkillOptimizer
        from index_builder import IndexBuilder
        chat = TelegramChatHandler()
        optimizer = SkillOptimizer()
        indexer = IndexBuilder()
        await chat.start()
        tasks += [
            chat.poll_loop,
            optimizer.run_loop,
            indexer.run_loop,
        ]

    stop_event = asyncio.Event()

    # Register signal handlers only after all objects are constructed.
    # stop_event is guaranteed to exist here; no risk of NameError on early signal.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig, lambda: (log.info("Shutdown signal received"), stop_event.set())
        )

    try:
        await asyncio.gather(*[t(stop_event) for t in tasks])
    finally:
        log.info("Flushing state before exit")
        watcher.save_seen_urls()
        if chat is not None:
            await chat.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

### 7.3 browser_watcher.py

```python
import sqlite3, shutil, asyncio, logging
import httpx
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from skill_executor import SkillExecutor
from memory_writer import MemoryWriter

log = logging.getLogger("browser-watcher")

CHROME_HISTORY = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
FIREFOX_HISTORY = Path.home() / "Library/Application Support/Firefox/Profiles"
SEEN_URLS_FILE = Path.home() / ".second-brain-seen-urls"

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
                    soup = BeautifulSoup(r.text, "html.parser")
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
        import yaml
        config_path = Path.home() / \
            "Library/Mobile Documents/com~apple~CloudDocs/second-brain/config.yaml"

        while not stop_event.is_set():
            # Re-read config every iteration — picks up skip_domain edits, interval
            # changes, etc. without requiring a daemon restart. It's a tiny YAML file.
            try:
                config = yaml.safe_load(config_path.read_text())
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
```

### 7.4 skill_executor.py

```python
from pathlib import Path
import yaml, re, logging, json
from datetime import datetime
from litellm import acompletion

log = logging.getLogger("skill-executor")
ERROR_LOG = Path.home() / ".second-brain-errors.log"
# Watcher nodes write execution history here instead of the iCloud skill file.
# The leader's optimizer can optionally ingest this on its daily pass (v0.2+).
LOCAL_EXEC_LOG = Path.home() / ".second-brain-execution-log.jsonl"

SKILLS_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/skills"

class SkillExecutor:
    def __init__(self, skill_name: str, role: str = "full"):
        self.skill_name = skill_name
        self.role = role                      # "full" or "watcher"
        self.skill_path = SKILLS_DIR / f"{skill_name}.md"
        self._skill = self._load()

    def _load(self) -> dict:
        text = self.skill_path.read_text()
        parts = text.split("---", 2)
        meta = yaml.safe_load(parts[1])
        body = parts[2]
        instructions_match = re.search(
            r'## Instructions\n(.*?)(?=\n## |\Z)', body, re.DOTALL)
        instructions = instructions_match.group(1).strip() if instructions_match else ""
        return {"meta": meta, "instructions": instructions, "raw": text}

    async def run(self, inputs: dict, score: float | None = None) -> str | None:
        meta = self._skill["meta"]
        model = meta.get("preferred_model", "gemini/gemini-2.0-flash")
        user_msg = "\n".join(f"**{k}:**\n{v}" for k, v in inputs.items())

        try:
            response = await acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": self._skill["instructions"]},
                    {"role": "user", "content": user_msg}
                ],
                max_tokens=1000
            )
            result = response.choices[0].message.content
            await self._log_execution(inputs, model, score=score)
            return result

        except Exception as e:
            err_msg = f"{datetime.now().isoformat()} [{self.skill_name}] {model} ERROR: {e}\n"
            log.error(err_msg.strip())
            with open(ERROR_LOG, "a") as f:
                f.write(err_msg)
            await self._log_execution(inputs, model, score=0.0, notes=str(e)[:80])
            return None

    async def _log_execution(self, inputs: dict, model: str,
                              score: float | None, notes: str = ""):
        slug = list(inputs.values())[0][:20].replace(" ", "-").lower() \
               if inputs else "unknown"
        date = datetime.now().strftime("%Y-%m-%d")
        score_str = f"{score:.2f}" if score is not None else "pending"

        if self.role == "watcher":
            # Watcher nodes must not write to the shared iCloud skill file.
            # Two machines appending simultaneously produces iCloud conflict copies.
            # Log locally — optimizer can merge these in v0.2+.
            record = {
                "date": date, "skill": self.skill_name, "input_slug": slug,
                "model": model, "score": score_str, "notes": notes,
                "hostname": __import__("socket").gethostname()
            }
            with open(LOCAL_EXEC_LOG, "a") as f:
                f.write(json.dumps(record) + "\n")
            return

        # Full node: write to the skill file in iCloud as before
        row = f"| {date} | {slug} | {model} | {score_str} | {notes} |\n"
        text = self.skill_path.read_text()

        if "## Execution History" not in text:
            text += (f"\n## Execution History\n\n"
                     f"| date | input_slug | model | score | notes |\n"
                     f"|------|-----------|-------|-------|-------|\n{row}")
        else:
            lines = text.splitlines(keepends=True)
            insert_at = len(lines)
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip().startswith("|"):
                    insert_at = i + 1
                    break
            lines.insert(insert_at, row)
            text = "".join(lines)

        self.skill_path.write_text(text)
```

### 7.5 chat_handler.py

```python
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from pathlib import Path
import yaml, asyncio, logging, re

log = logging.getLogger("chat-handler")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MAX_CONTEXT_CHARS = 80_000
TG_MAX_CHARS = 4096  # Telegram hard limit per message

class TelegramChatHandler:
    def __init__(self):
        config = yaml.safe_load((BRAIN_DIR / "config.yaml").read_text())
        self.token = config["telegram"]["bot_token"]
        self.allowed_user_id = int(config["user"]["telegram_user_id"])
        from skill_executor import SkillExecutor
        self.executor = SkillExecutor("chat")
        self.app = ApplicationBuilder().token(self.token).build()
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )
        # Cache: path -> (mtime, header_text). Invalidated when mtime changes.
        # Avoids reading every file on every chat message.
        self._header_cache: dict[Path, tuple[float, str]] = {}

    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot polling started")

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        log.info("Telegram bot stopped")

    async def poll_loop(self, stop_event: asyncio.Event):
        await stop_event.wait()

    def _get_header(self, path: Path) -> str:
        """Return cached first-500-chars header, refreshing only when mtime changes."""
        mtime = path.stat().st_mtime
        cached = self._header_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        header = path.read_text()[:500]
        self._header_cache[path] = (mtime, header)
        return header

    def _score_relevance(self, path: Path, query: str) -> int:
        """
        Cheap keyword intersection against cached file header.
        Score = query tokens (3+ chars) found in title/tags frontmatter block.
        One file read per new/modified file — not per message.
        """
        header = self._get_header(path).lower()
        tokens = {w for w in re.findall(r'\b\w{3,}\b', query.lower())}
        return sum(1 for t in tokens if t in header)

    def _load_context(self, query: str) -> str:
        """Load memory files into context with relevance sorting and hard char budget."""
        parts = []
        budget = MAX_CONTEXT_CHARS

        index_path = BRAIN_DIR / "index.md"
        if index_path.exists():
            chunk = f"# Memory Index\n{index_path.read_text()}"
            parts.append(chunk)
            budget -= len(chunk)

        memory_files = list((BRAIN_DIR / "memories").glob("*.md"))

        # Score using cached headers — O(cache_size) not O(files * file_size)
        scored = sorted(
            memory_files,
            key=lambda p: (self._score_relevance(p, query), p.stat().st_mtime),
            reverse=True
        )

        for f in scored:
            if budget <= 0:
                log.debug(f"Context budget exhausted after {len(parts)-1} memory files")
                break
            text = f.read_text()
            if len(text) > budget:
                text = text[:budget] + "\n[truncated]"
            parts.append(text)
            budget -= len(text)

        return "\n\n---\n\n".join(parts)

    async def _send_reply(self, update: Update, text: str):
        """Chunk response into ≤4096-char messages to respect Telegram's hard limit."""
        if not text:
            await update.message.reply_text("No response generated.")
            return
        for i in range(0, len(text), TG_MAX_CHARS):
            await update.message.reply_text(text[i:i + TG_MAX_CHARS])

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != self.allowed_user_id:
            return

        query = update.message.text
        memory_context = self._load_context(query)

        response = await self.executor.run({
            "memory_context": memory_context,
            "user_query": query
        })

        await self._send_reply(update, response)
```

### 7.6 memory_writer.py

```python
from pathlib import Path
from datetime import datetime
import re, yaml, hashlib, os

MEMORIES_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/memories"

class MemoryWriter:
    async def write(self, entry: dict, body: str) -> str:
        date = datetime.now().strftime("%Y-%m-%d")

        title_part = re.sub(r'[^a-z0-9]+', '-',
                            entry.get("title", entry["url"])[:50].lower()).strip('-')
        url_hash = hashlib.sha1(entry["url"].encode()).hexdigest()[:6]
        slug = f"{title_part}-{url_hash}"
        filename = f"{date}-{slug}.md"

        # Collision note: two machines visiting the same URL on the same day produce
        # identical filenames (same title slug + same URL hash). write_text is an
        # atomic overwrite on APFS — the second write wins with the same content.
        # This is intentional: duplicate memories are harmless, not additive.
        # seen_urls on each machine prevents re-processing on that machine; iCloud
        # deduplication handles the cross-machine case via identical filenames.
        target = MEMORIES_DIR / filename

        tags_match = re.search(r'\*\*Tags.*?:\*\*\s*(.+)', body)
        tags = [t.strip() for t in tags_match.group(1).split(",")] if tags_match else []

        frontmatter = {
            "id": slug,
            "created": datetime.now().isoformat(),
            "source_url": entry["url"],
            "source_title": entry.get("title", ""),
            "visit_count": entry.get("visit_count", 1),
            "tags": tags,
            "browser": entry.get("browser", "unknown"),
            "hostname": __import__("socket").gethostname(),  # useful for health signal
        }

        content = f"---\n{yaml.dump(frontmatter)}---\n\n{body}\n"

        # Atomic write: write to .tmp sibling, then rename.
        # os.rename() is atomic on APFS — a crash mid-write never leaves a partial
        # file that syncs to iCloud. The .tmp file stays local until rename commits.
        tmp_path = target.with_suffix(".tmp")
        tmp_path.write_text(content)
        os.rename(tmp_path, target)

        return filename
```

---

## 8. Skill Files (Initial Set)

### skills/chat.md

```markdown
---
name: chat
version: 1
preferred_model: claude-sonnet-4-20250514
fallback_model: openai/nemotron-cascade-2
success_rate: null
total_runs: 0
---

## Instructions

You are Chris's second brain — a personal AI assistant with access to his
reading history and accumulated knowledge.

The memory context below contains summaries of web pages Chris has read,
organized as markdown files. Use this context to answer his questions,
make connections he might not have made, and surface relevant things he
has read before.

Behavior:
- Be direct and concise. Chris is technical; don't over-explain.
- If something in memory is directly relevant, cite it (mention the source title).
- If you don't have relevant memory, say so — don't hallucinate.
- Surface connections between memories when you notice them.
- Treat this as a conversation, not a search engine response.

## Execution History

| date | query_slug | model | score | notes |
|------|-----------|-------|-------|-------|
```

### skills/summarize-webpage.md
*(as defined in Section 4 above)*

### skills/skill-optimizer.md

```markdown
---
name: skill-optimizer
version: 1
preferred_model: claude-sonnet-4-20250514
---

## Instructions

You are optimizing a second-brain skill based on its execution history.

You will be given:
1. The current skill instructions
2. The execution history table (date, input, model, score, notes)
3. Example inputs and outputs from low-scoring runs

Your job:
- Identify patterns in low-scoring runs (score < 0.70)
- Rewrite the Instructions section to address those failure patterns
- Do NOT change the frontmatter, Evolution Log structure, or Execution History
- Append a new entry to the Evolution Log describing what you changed and why
- Output the complete updated skill file

Be conservative. Only change what the evidence suggests is broken.
```

---

## 9. index_builder.py

Runs hourly. Reads all memory files, asks an LLM to synthesize a rolling 400-500 word
summary, writes it to `index.md`. This file is prepended to every chat context window —
it's the "executive summary" of the brain.

**Model routing:** `gemini/gemini-2.0-flash` — high frequency, read-only, no reasoning
required. Cheap.

```python
import asyncio, logging, yaml
from pathlib import Path
from datetime import datetime
from litellm import acompletion

log = logging.getLogger("index-builder")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
INDEX_PATH = BRAIN_DIR / "index.md"
MODEL = "gemini/gemini-2.0-flash"
MAX_INPUT_CHARS = 120_000   # cap input to indexer — summarize summaries

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
        import re as _re
        seen: dict[str, float] = {}   # hostname -> most recent mtime
        for f in memory_files[:20]:
            try:
                header = f.read_text()[:300]
                match = _re.search(r'hostname:\s*(\S+)', header)
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
        import yaml
        config_path = BRAIN_DIR / "config.yaml"
        while not stop_event.is_set():
            try:
                config = yaml.safe_load(config_path.read_text())
                interval = config.get("memory", {}).get("index_rebuild_interval", 3600)
            except Exception:
                interval = 3600
            await self._build()
            await asyncio.sleep(interval)
```

---

## 10. skill_optimizer.py

Day 1: minimal stub — present so `daemon.py` imports and `asyncio.gather` don't crash.  
Day 2: fill in `_optimize_skill()` with the LLM-as-judge pass.

```python
import asyncio, logging, yaml
from pathlib import Path

log = logging.getLogger("skill-optimizer")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"

class SkillOptimizer:
    """
    Daily pass: reads each skill's execution history, asks an LLM to identify
    failure patterns in low-scoring runs, rewrites the Instructions section
    in-place, and appends to the Evolution Log.

    v0.1 stub — run_loop sleeps until stop_event. Optimizer logic in v0.2.
    """

    async def _optimize_skill(self, skill_path: Path):
        # TODO (day 2):
        # 1. Parse skill file — extract instructions + execution history
        # 2. Filter rows where score < threshold (from config)
        # 3. Skip if fewer than min_runs rows (avoid optimizing on noise)
        # 4. Call LiteLLM with skill-optimizer.md instructions + skill content
        # 5. Write updated skill file atomically (write tmp, rename)
        # 6. Append Evolution Log entry with version bump
        log.debug(f"Optimizer stub — skipping {skill_path.name}")

    async def run_loop(self, stop_event: asyncio.Event):
        config_path = BRAIN_DIR / "config.yaml"
        while not stop_event.is_set():
            try:
                config = yaml.safe_load(config_path.read_text())
                run_hour = config.get("skill_optimizer", {}).get("run_hour", 3)
            except Exception:
                run_hour = 3

            # Sleep until stop or the next scheduled hour
            # Stub just waits — replace with real scheduling in day 2
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass  # woke up on schedule, not on stop
```

**Day 2 implementation notes:**
- Write the updated skill file to a `.tmp` sibling then `os.rename()` — atomic on macOS, prevents partial writes if the daemon is killed mid-update
- Re-instantiate any live `SkillExecutor` referencing that skill after write, or implement the `_reload_if_modified()` mtime check described in Section 14

---

## 11. macOS Launch Agent

Save as `~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.chrisrobertson.secondbrain</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/python3</string>
    <string>/Users/chrisrobertson/repos/second-brain/daemon.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/second-brain.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/second-brain.error.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- Role is set here, not in config.yaml — config.yaml syncs via iCloud
         and a change on one machine would propagate to all machines.
         Mac Studio (leader): full   MacBook Pro (watcher): watcher -->
    <key>SECOND_BRAIN_ROLE</key>
    <string>full</string>
    <key>ANTHROPIC_API_KEY</key>
    <string>YOUR_KEY</string>
    <key>GEMINI_API_KEY</key>
    <string>YOUR_KEY</string>
  </dict>
</dict>
</plist>
```

Load with:
```bash
launchctl load ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist
```

---

## 12. Telegram Bot Setup

1. Message `@BotFather` on Telegram → `/newbot` → get token
2. Message `@userinfobot` → get your numeric user ID
3. Add both to `config.yaml`
4. Bot is private by default — it will ignore all users except your ID

---

## 13. Dependencies

```
# requirements.txt
litellm>=1.35.0
python-telegram-bot>=21.0
httpx>=0.27.0
pyyaml>=6.0
beautifulsoup4>=4.12   # day 1 — needed for content extraction
lxml>=5.0              # faster BS4 parser
watchdog>=4.0          # optional — file system events
```

```bash
pip install -r requirements.txt
```

---

## 14. Build Order for Tomorrow

| # | Task | Est. Time | Notes |
|---|------|-----------|-------|
| 1 | Directory setup + config.yaml | 10 min | |
| 2 | Telegram bot token + user ID | 5 min | |
| 3 | LiteLLM config + API keys | 15 min | |
| 4 | Write initial skill files | 20 min | summarize-webpage + chat |
| 5 | browser_watcher.py | 45 min | Chrome first, FF second |
| 6 | skill_executor.py | 30 min | |
| 7 | memory_writer.py | 20 min | |
| 8 | chat_handler.py | 30 min | |
| 9 | skill_optimizer.py stub | 5 min | copy from spec — no logic yet |
| 10 | daemon.py wiring | 20 min | |
| 11 | End-to-end test | 30 min | browse something, query it |
| 12 | index_builder.py | 20 min | |
| 13 | launchd plist | 10 min | |
| 14 | skill_optimizer full impl | 45 min | day 2 — defer if short on time |

**Total day 1: ~4.5 hours to working system (tasks 1–12)**

---

## 15. Known Deferred Items (v0.2+)

- Scroll depth / dwell time measurement (requires browser extension)
- iOS Share Sheet capture
- Clipboard monitoring
- Memory decay / forgetting rules
- connect-memories skill (multi-hop reasoning across files)
- Skill discovery / auto-generation
- Paywall detection and graceful skip
- morning-briefing skill
- Semantic search (if flat-file context window approach hits limits)
- **Seen-URLs store → SQLite.** The current flat newline file works fine early on but will grow to tens of thousands of entries over months. Migration path: replace `~/.second-brain-seen-urls` with a tiny SQLite DB (`seen_urls` table, url + timestamp). Drop-in swap, no other code changes.
- **Skill executor cache invalidation.** `SkillExecutor._load()` caches skill instructions at `__init__` time. The skill optimizer rewrites skill files at 3am, but running executor instances hold stale prompts until daemon restart. v0.2 fix: add a `_reload_if_modified()` check that compares `skill_path.stat().st_mtime` against a stored timestamp before each `run()` call.

**System dependency note:**  
`lxml` requires `libxml2`. On macOS this is normally present via Xcode CLI tools (`xcode-select --install`). If you ever containerize the daemon, add `libxml2-dev` to the base image.

**iCloud caveats (day 1 awareness):**  
iCloud Drive is eventually consistent. On iPhone/iPad, undownloaded files appear as `.icloud` stubs (e.g., `2026-04-11-foo.md.icloud`). Reading memories directly from iOS Files app or Obsidian Mobile will trigger download automatically, but any code running on-device would need to skip `.icloud` files or trigger download via `NSFileCoordinator`. Not a concern for the Mac daemon — it reads/writes locally and iCloud syncs in the background.
