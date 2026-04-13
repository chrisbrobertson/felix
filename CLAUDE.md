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
project_scanner.py     # Scans ~/repos for git repos, writes project-{hostname}-*.md memory files
email_scanner.py       # Reads Apple Mail, writes email-thread-*.md memory files
zoom_scanner.py        # Polls Zoom API, parses VTT transcripts, writes meeting-*.md files
commitment_tracker.py  # Extracts commitments from meeting/email memories, /commitments cmd
contact_tracker.py     # Aggregates participants across memories, /contacts cmd
github_client.py       # Async GitHub Issues API client (optional GH backing for /feature and /bug)
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

## Agent Isolation

When spawning implementation agents (via the `Agent` tool), always pass `isolation: "worktree"`. This gives each agent its own git worktree so its in-progress changes never touch the working tree of the main session or other agents running in parallel.

```python
Agent(
    description="...",
    prompt="...",
    isolation="worktree",   # required for all implementation agents
)
```

Research-only agents (Explore, Plan, docs reads) do not need a worktree — only agents that write files or run tests.

## Committing and Deploying

Commit automatically after each work item is completed — do not wait to be asked. A work item is a logical unit: a new feature, a bug fix, a test suite addition, a docs update. Do not batch unrelated changes into one commit.

After committing, always deploy with `./install.sh`. Never copy files to `~/secondbrain/` by hand.

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

`GITHUB_PAT` + `GITHUB_REPO` (optional) enable GitHub-Issues backing for `/feature` and `/bug`. Both must be set; if either is missing, the local-file fallback is used.

## Architecture: Twelve Async Loops

1. **Browser Watcher** (every 5 min) — reads Chrome/Firefox SQLite DBs, filters by dwell time and skip-domain list, fetches page content, runs `summarize-webpage` skill, writes memory file.

2. **Telegram Chat Handler** (always polling) — receives queries, loads `index.md` + up to 20 relevant memory files (keyword intersection against cached 500-char headers), streams response via `chat` skill.

3. **Index Builder** (every hour) — reads all memory files, calls LLM to synthesize `index.md`. Also emits a health-check log line with last-seen timestamp per hostname.

4. **Skill Optimizer** (3 AM daily) — scores recent executions using LLM-as-judge against source content, rewrites underperforming skill instructions, appends to evolution log.

5. **Project Scanner** (every 5 min) — globs `~/repos/` and `~/repo/` for git repos, extracts metadata (remote URL, HEAD sha, recent commits, branches, languages), generates a 1-2 sentence LLM summary from README on first scan or README change, writes `project-{hostname}-{name}.md` memory file. Skips write when HEAD sha unchanged. `type: project` + `category: code` in frontmatter (was `type: code_project` before v1.1.0 — migration runs automatically on `ProjectScanner.__init__`). Filename migration from `project-{name}.md` to hostname-scoped pattern also runs automatically on init. Exposes `/projects [category] [N]` and `/project <N>` Telegram commands; the list view groups by repo base name when the same repo exists on multiple machines.

6. **Email Scanner** (every 5 min) — reads Apple Mail.app data (SQLite Envelope Index primary, AppleScript fallback), writes one `email-thread-{slug}-{conv-id}.md` per conversation thread. Skips write when `message_count` and `last_message` unchanged. `type: email_thread` in frontmatter. Requires Full Disk Access for SQLite path; on macOS Sonoma, Homebrew Python (ad-hoc signed) silently fails FDA at runtime even when granted — AppleScript fallback (120s timeout, last-500-message item-slice approach) is the operative path on most setups. State (high-water ROWID) persisted in `DEPLOY_DIR/email-scanner-state.json`.

7. **Zoom Scanner** (every 5 min, `full` role only) — polls Zoom Cloud Recordings API via Server-to-Server OAuth (M2M), downloads VTT transcripts, parses speaker-attributed segments, generates LLM summary, writes `meeting-{date}-{slug}-{id}.md` per meeting. `type: meeting_transcript` in frontmatter. Deduplication via `DEPLOY_DIR/zoom-scanner-state.json`. Requires `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` env vars; exits gracefully if missing.

8. **Commitment Tracker** (every 5 min, `full` role only) — scans `meeting_transcript` and `email_thread` memory files for new/changed content (mtime-based), calls LLM to extract commitments and waiting-on items, writes one `commitment-{slug}-{id}.md` per item. Confidence ≥0.7 → auto-active; 0.5–0.69 → `needs-review` tag; <0.5 → discarded. Exposes `/commitments`, `/complete N`, `/dismiss N` Telegram commands. State persisted in `DEPLOY_DIR/commitment-scanner-state.json`.

9. **Calendar Scanner** (every 5 min, all roles) — reads Apple Calendar.app data (SQLite Calendar Cache primary, EventKit fallback, AppleScript last resort), writes one `calendar-event-{hostname}-{date}-{slug}-{id}.md` per event in a rolling ±7-day window. `type: calendar_event` + `hostname:` in frontmatter. Change detection via modification timestamp. SQLite path requires no permissions; EventKit requires Calendar access grant; AppleScript fallback requires Automation permission to Calendar.app. Exposes `/events [N]` and `/event <N>` Telegram commands (full role only — chat handler is tier-2). State persisted in `DEPLOY_DIR/calendar-scanner-state.json`.

10. **Contact Tracker** (every 5 min, `full` role only) — scans `email_thread`, `meeting_transcript`, `calendar_event`, and `slack_thread` memory files for participant names and emails (mtime-based). Writes one `contact-{name-slug}.md` per person with email-based deduplication, recency-weighted relationship scoring, and interaction history. Exposes `/contacts [N]` (alias: `/people`), `/contact <name|N>` Telegram commands. State persisted in `DEPLOY_DIR/contact-tracker-state.json`.

11. **Slack Scanner** (every 5 min, `full` role only) — polls Slack Web API for threads in monitored channels, writes `slack-thread-*.md` memory files. Requires `SLACK_USER_TOKEN` env var (xoxp-); user ID auto-discovered via auth.test. Exits gracefully if missing. State persisted in `DEPLOY_DIR/slack-scanner-state.json`.

12. **Notification Manager** (every 60 sec, `full` role only) — pushes proactive messages to Telegram: daily morning briefing (calendar, commitments, memory digest), pre-meeting context (10 min before events), commitment deadline alerts (today/tomorrow). Exposes `/briefing`, `/mute`, `/unmute` commands. State persisted in `DEPLOY_DIR/notification-state.json`.

**Zoom Scanner** also exposes `/meetings [N]` and `/meeting <N>` Telegram commands for browsing meeting transcripts.

## Two Deployment Roles

- **`full`** — all twelve loops. Runs on always-on machine (Mac Studio/Mini). Needs `ANTHROPIC_API_KEY` + `GEMINI_API_KEY`.
- **`watcher`** — five capture loops (browser watcher + project/email/calendar/slack scanners). Runs on MacBook. Needs only `GEMINI_API_KEY`. Full-node imports (`python-telegram-bot`, etc.) must be deferred inside the `role == "full"` block to avoid crashing on watcher nodes that don't have those packages installed.

## LLM Routing

LiteLLM unified API with named routes:
- `summarize` → `claude-haiku-4-5-20251001` (high volume, cheap)
- `chat` / `optimizer` → `claude-sonnet-4-6` (quality matters)
- `judge` → `claude-haiku-4-5-20251001` (skill optimizer scoring)
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
- **COMMAND_REGISTRY:** Module-level constant in `chat_handler.py` is the single source of truth for all Telegram commands. `/help` renders from it. A test asserts every `CommandHandler` registration has a matching entry — add both when adding a new command.
- **Project type generalization:** Project memories use `type: project` + `category: code` (not `type: code_project`). `ProjectScanner.__init__` migrates legacy files automatically. Future scanners for person/work projects write the same `type: project` with a different `category`.
- **Unified /comms:** Email and Slack threads share `/comms [email|slack]` + `/comm <N>`. No separate `/emails` or `/slack` commands.

## Deploy directory

All runtime state lives in `~/secondbrain/` — separate from the repo and from the iCloud brain data:

```
~/secondbrain/
├── venv/                           # Python virtual environment (created by install.sh)
├── logs/                           # out.log, error.log (written by launchd)
├── seen-urls                       # flat file of processed URLs (browser watcher)
├── errors.log                      # LLM API errors
├── execution-log.jsonl             # watcher-node skill execution log
├── email-scanner-state.json        # high-water ROWID for email scanner
├── zoom-scanner-state.json         # processed meeting UUIDs for zoom scanner
├── commitment-scanner-state.json   # processed file mtimes for commitment tracker
├── calendar-scanner-state.json     # processed event modification timestamps
├── contact-tracker-state.json      # processed file mtimes and interaction timestamps
├── slack-scanner-state.json        # processed Slack thread timestamps
├── commitment-corrections.jsonl    # /wrong and /missed feedback log
├── commitment-accuracy.json        # extraction precision stats per source type
└── notification-state.json         # chat_id, mute state, sent alerts for notification manager
```

`SECOND_BRAIN_DIR` env var overrides the deploy dir location (defaults to `~/secondbrain`). The launchd plist sets this explicitly so the daemon always finds its runtime files.

## Deploying Code Changes

**Always use the installer to deploy.** Never copy files to `~/secondbrain/` manually.

```bash
./install.sh
```

The installer is idempotent — it skips unchanged files, deploys only what changed,
and reloads the daemon. Running it is the only correct way to push code from the
repo to the live daemon. Direct `cp` bypasses the FDA check, plist update, and
reload sequence.

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
