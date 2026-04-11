# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Second Brain** — a personal knowledge system that monitors browser history, auto-summarizes visited pages using LLMs, stores the results as flat markdown files in iCloud Drive, and exposes them through a Telegram bot.

**Philosophy:** Karpathy flat-file pattern. No vector DB, no graph DB, no embeddings. Files + LLM = database.

**Status:** Implemented. Spec at `specs/second-brain-spec-v1.0.md`.

## Code Structure

```
daemon.py              # Entry point; starts all async loops
browser_watcher.py     # Polls Chrome/Firefox SQLite history DBs
skill_executor.py      # Loads skill .md files, calls LiteLLM, appends execution log
memory_writer.py       # Atomic writes of memory markdown to iCloud
chat_handler.py        # Telegram bot; keyword-relevance context loading
index_builder.py       # Hourly index.md rebuild
skill_optimizer.py     # Daily LLM-as-judge skill improvement (v0.1 is a stub)
utils.py               # Shared helpers
skills/                # Skill .md files (committed; deployed to iCloud skills/ dir)
tests/
├── unit/              # Per-module unit tests (mocked LLM + filesystem)
└── integration/       # End-to-end flow tests (real file I/O, mocked LLM)
```

## Testing

Tests are required. Run the full suite before every commit — no exceptions.

```bash
# Install dev dependencies (first time)
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run a single test file
pytest tests/unit/test_memory_writer.py

# Run a single test by name
pytest -k test_atomic_write_leaves_no_tmp_file
```

All modules are in the repo root (not a package). `tests/conftest.py` inserts the root into `sys.path` so imports work. Tests use `tmp_path` fixtures for file isolation and `unittest.mock.patch` to redirect module-level path constants (e.g. `MEMORIES_DIR`, `SKILLS_DIR`, `BRAIN_DIR`) to temporary directories — never touching real iCloud paths or browser DBs. `acompletion` and Telegram's `ApplicationBuilder` are always mocked.

When adding or changing any module, add or update the corresponding tests. Unit tests live in `tests/unit/test_<module>.py`. Integration tests that span module boundaries go in `tests/integration/`.

## Documentation

Every user-facing feature must have corresponding documentation in `README.md`. This includes new configuration options, new deployment steps, new runtime behaviour, and any change to how the daemon is invoked or monitored.

`CLAUDE.md` is guidance for Claude Code only — it is not a substitute for user-facing docs. If you add or change something a human operator needs to know to set up or run the system, update `README.md`.

## Committing

Commit automatically after each work item is completed — do not wait to be asked. A work item is a logical unit: a new feature, a bug fix, a test suite addition, a docs update. Do not batch unrelated changes into one commit.

Run `pytest` and confirm it passes before every commit. If tests fail, fix them — do not skip or comment them out.

Commit messages should state *why* the change was made, not just what changed. Examples:

```
Add mtime-based header cache to avoid re-reading memory files on every chat query
Fix watcher role writing to iCloud skill file — caused sync conflicts on multi-machine setup
Stub skill_optimizer run_loop so daemon.py gathers without error on day 1
```

Do not commit:
- `config.yaml` (contains API keys and bot tokens — lives in iCloud, not the repo)
- `com.chrisrobertson.secondbrain.plist` with real API keys filled in
- `~/.second-brain-seen-urls` or any runtime state files

## Runtime File Locations

All live data lives outside the repo, in iCloud Drive:

```
~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/
├── memories/       # One .md file per captured webpage
├── skills/         # Prompt templates with embedded execution history
├── inbox/          # Raw captures pending processing
├── index.md        # LLM-maintained rolling summary (~400-500 words)
└── config.yaml     # Daemon role, thresholds, API routing
```

Config is read from `config.yaml` on startup; `SECOND_BRAIN_ROLE` env var overrides `daemon.role` (use this per-machine so the override isn't synced via iCloud).

## Architecture: Six Async Loops

1. **Browser Watcher** (every 5 min) — reads Chrome/Firefox SQLite DBs, filters by dwell time and skip-domain list, fetches page content, runs `summarize-webpage` skill, writes memory file.

2. **Telegram Chat Handler** (always polling) — receives queries, loads `index.md` + up to 20 relevant memory files (keyword intersection against cached 500-char headers), streams response via `chat` skill.

3. **Index Builder** (every hour) — reads all memory files, calls LLM to synthesize `index.md`. Also emits a health-check log line with last-seen timestamp per hostname.

4. **Skill Optimizer** (3 AM daily) — scores recent executions using LLM-as-judge against source content, rewrites underperforming skill instructions, appends to evolution log.

5. **Project Scanner** (every 5 min) — globs `~/repos/` and `~/repo/` for git repos, extracts metadata (remote URL, HEAD sha, recent commits, branches, languages), generates a 1-2 sentence LLM summary from README on first scan or README change, writes `project-{name}.md` memory file. Skips write when HEAD sha unchanged. `type: code_project` in frontmatter.

6. **Email Scanner** (every 5 min) — reads Apple Mail.app data (SQLite Envelope Index primary, AppleScript fallback), writes one `email-thread-{slug}-{conv-id}.md` per conversation thread. Skips write when `message_count` and `last_message` unchanged. `type: email_thread` in frontmatter. Requires Full Disk Access for SQLite path. State (high-water ROWID) persisted in `DEPLOY_DIR/email-scanner-state.json`.

## Two Deployment Roles

- **`full`** — all six loops. Runs on always-on machine (Mac Studio/Mini). Needs `ANTHROPIC_API_KEY` + `GEMINI_API_KEY`.
- **`watcher`** — browser watcher only. Runs on MacBook. Needs only `GEMINI_API_KEY`. Full-node imports (`python-telegram-bot`, etc.) must be deferred inside the `role == "full"` block to avoid crashing on watcher nodes that don't have those packages installed.

## LLM Routing

LiteLLM unified API with two named routes:
- `summarize` → `gemini/gemini-2.0-flash` (high volume, cheap)
- `chat` / `optimizer` → `claude-sonnet-4-20250514` (quality matters)
- `local` fallback → OpenAI-compatible local endpoint

Config lives at `~/.litellm/config.yaml`. API keys come from env vars (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`).

## Key Design Decisions

- **Atomic writes:** Write to a temp file, then `os.rename()` — prevents partial iCloud sync.
- **Filename convention:** `YYYY-MM-DD-{title-slug}-{6-char-url-hash}.md` — human-readable + collision-resistant.
- **Relevance scoring:** Keyword intersection against cached first-500-chars of each memory file, not semantic search.
- **Skill files are self-logging:** The executor appends each run's outcome to an `## Execution History` table inside the skill `.md` file itself.
- **Watcher nodes log locally:** Watcher-role machines write execution logs to a local JSONL file (not iCloud) to avoid sync conflicts.
- **Max memory file size:** ~2KB. Summarize harder if content is longer.
- **Telegram 4096-char limit:** Chat handler must chunk responses.

## Deploy directory

All runtime state lives in `~/secondbrain/` — separate from the repo and from the iCloud brain data:

```
~/secondbrain/
├── venv/              # Python virtual environment (created by install.sh)
├── logs/              # out.log, error.log (written by launchd)
├── seen-urls          # flat file of processed URLs (browser watcher)
├── errors.log         # LLM API errors
└── execution-log.jsonl  # watcher-node skill execution log
```

`SECOND_BRAIN_DIR` env var overrides the deploy dir location (defaults to `~/secondbrain`). The launchd plist sets this explicitly so the daemon always finds its runtime files.

## Running the Daemon

```bash
# Direct (dev/testing — uses venv Python)
~/secondbrain/venv/bin/python3 daemon.py

# Via launchd (production)
launchctl load ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist
launchctl unload ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist

# Logs
tail -f ~/secondbrain/logs/out.log
tail -f ~/secondbrain/logs/error.log
```

## Dependencies

```bash
pip install -r requirements.txt        # production
pip install -r requirements-dev.txt    # adds pytest + pytest-asyncio
```
