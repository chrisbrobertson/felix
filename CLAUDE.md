# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Second Brain** — a personal knowledge system that monitors browser history, auto-summarizes visited pages using LLMs, stores the results as flat markdown files in iCloud Drive, and exposes them through a Telegram bot.

**Philosophy:** Karpathy flat-file pattern. No vector DB, no graph DB, no embeddings. Files + LLM = database.

**Status:** Implemented. Spec at `specs/second-brain-spec-v1.0.md`.

## Code Structure

```
daemon.py                      # Entry point; starts all async loops
browser_watcher.py             # Polls Chrome/Firefox SQLite history DBs
skill_executor.py              # Loads skill .md files, calls LiteLLM, appends execution log
memory_writer.py               # Atomic writes of memory markdown to iCloud
chat_handler.py                # Telegram bot; keyword-relevance context loading
index_builder.py               # Hourly index.md rebuild
skill_optimizer.py             # Daily LLM-as-judge skill improvement (v0.1 is a stub)
code_scanner.py                # Scans ~/repos for git repos, writes code-{hostname}-*.md memory files
email_scanner.py               # Reads Apple Mail, writes email-thread-*.md memory files
zoom_scanner.py                # Polls Zoom API, parses VTT transcripts, writes meeting-*.md files
commitment_tracker.py          # Extracts commitments from meeting/email memories, /commitments cmd
contact_tracker.py             # Aggregates participants across memories, /contacts cmd
goals_tracker.py               # GoalManager CRUD class for goal and project memories
project_inference_scanner.py  # Scans comms memories, infers projects, writes candidates
goal_project_agent.py          # 14th loop — checks active goals/projects, proposes actions via Telegram
github_client.py               # Async GitHub Issues API client (optional GH backing for /feature and /bug)
llm_routes.py                  # resolve(alias) → concrete model ID; centralises LiteLLM route aliases
utils.py                       # Shared helpers
VERSION                        # Semver version string (single source of truth)
CHANGELOG.md                   # Version history — update with every version bump
skills/                        # Skill .md files (committed; deployed to iCloud skills/ dir)
tests/
├── unit/                      # Per-module unit tests (mocked LLM + filesystem)
└── integration/               # End-to-end flow tests (real file I/O, mocked LLM)
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

## Versioning

This project uses [Semantic Versioning](https://semver.org/). The `VERSION` file at the repo root is the single source of truth — `daemon.py` reads it at startup, `/version` reports it via Telegram, and `install.sh` displays it in the header.

**When to bump:**
- **PATCH** (`1.3.x`) — bug fixes, logging improvements, performance tweaks, docs-only changes
- **MINOR** (`1.x.0`) — new features, new Telegram commands, new async loops, new scanners
- **MAJOR** (`x.0.0`) — breaking changes to memory file formats, config schema, or deployment model that require manual migration

**Release workflow** (do this whenever a meaningful set of changes is complete):

1. Edit `VERSION` — increment the appropriate component
2. Update `CHANGELOG.md` — add a new `## [X.Y.Z] — YYYY-MM-DD` section describing what changed
3. Commit both files: `git commit -m "Bump version to X.Y.Z"`
4. Tag: `git tag vX.Y.Z && git push --tags`
5. Deploy: `./install.sh`

Do not create a version bump commit for every individual change. Group related changes into a release. A release commit should only touch `VERSION` and `CHANGELOG.md` — the actual code changes are in the preceding commits.

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

## Versioning and Changelog

`VERSION` at the repo root is the single source of truth for the release number — it's displayed by `install.sh`, logged at daemon startup, and reported by the `/version` Telegram command. `CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) with semver.

**Every shipping commit must** append a bullet to the `[Unreleased]` section of `CHANGELOG.md` under the appropriate subsection (`Added`, `Changed`, `Fixed`, `Removed`). Most individual commits do **not** need a `VERSION` bump — batch several related patches together and bump once before `./install.sh`.

**When you do bump `VERSION`, also close out `[Unreleased]`:** rename the section header to `## [x.y.z] — YYYY-MM-DD` and add a new empty `## [Unreleased]` above it. Both edits go in the same "Bump version to x.y.z" commit.

**Semver rules:**
- **Patch** (`1.3.0 → 1.3.1`) — bug fixes, behaviour-affecting cleanup, deploy-script fixes.
- **Minor** (`1.3.1 → 1.4.0`) — new async loop, new Telegram command, new config option, new skill file, new user-visible capability.
- **Major** (`1.x → 2.0.0`) — breaking change to `config.yaml` schema, the install flow, the Telegram command contract, or the on-disk memory format.

**Does not warrant a CHANGELOG entry or bump:** test-only changes, pure refactors with no behaviour change, edits under `specs/`, status flips on iCloud feature-request files, README prose that doesn't describe new behaviour.

**Rule of thumb:** never run `./install.sh` on a code change without first appending to `CHANGELOG.md [Unreleased]`. The daemon should never report a `VERSION` whose shipped code doesn't match what the changelog says landed at that version.

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

## Architecture: Fifteen Async Loops

1. **Browser Watcher** (every 5 min) — reads Chrome/Firefox SQLite DBs, filters by dwell time and skip-domain list, fetches page content, runs `summarize-webpage` skill, writes memory file.

2. **Telegram Chat Handler** (always polling) — receives queries, loads `index.md` + up to 20 relevant memory files (keyword intersection against cached 500-char headers), streams response via `chat` skill. Includes a reconnect-poller sub-task (30s cadence) that queues undelivered replies to `~/secondbrain/pending-replies.json` and notifies the user when connectivity is restored.

3. **Index Builder** (every hour) — reads all memory files, calls LLM to synthesize `index.md`. Also emits a health-check log line with last-seen timestamp per hostname.

4. **Skill Optimizer** (3 AM daily) — scores recent executions using LLM-as-judge against source content, rewrites underperforming skill instructions, appends to evolution log.

5. **Code Scanner** (every 5 min) — globs `~/repos/` and `~/repo/` for git repos, extracts metadata (remote URL, HEAD sha, recent commits, branches, languages), generates a 1-2 sentence LLM summary from README on first scan or README change, writes `code-{hostname}-{name}.md` memory file. Skips write when HEAD sha unchanged. `type: code` in frontmatter (was `type: code_project` before v1.1.0, then `type: project` + `category: code` before v1.2.0 — migration runs automatically on `CodeScanner.__init__`). Filename migration from `project-{name}.md` to hostname-scoped pattern and then from `project-{hostname}-*.md` to `code-{hostname}-*.md` also runs automatically on init. Exposes `/code [N]` Telegram command (with N argument shows detail, without shows list); the list view groups by repo base name when the same repo exists on multiple machines. New repos optionally go through the candidate confirmation flow when `code_scanner.require_confirmation: true`.

6. **Email Scanner** (every 5 min) — reads Apple Mail.app data (SQLite Envelope Index primary, AppleScript fallback), writes one `email-thread-{slug}-{conv-id}.md` per conversation thread. Skips write when `message_count` and `last_message` unchanged. `type: email_thread` + `classification:` (one of `human`, `transactional`, `marketing`, `automated`, `unknown`) in frontmatter. Classification happens during the same LLM call that generates summary + tags. Downstream consumers (`contact_tracker`, `commitment_tracker`) skip `marketing` and `automated` emails; `chat_handler`'s `/comms email` hides those two unless `/comms email all` is used. Kill-switch: `email_scanner.classification_enabled: false` in config. Requires Full Disk Access for SQLite path; on macOS Sonoma, Homebrew Python (ad-hoc signed) silently fails FDA at runtime even when granted — AppleScript fallback (120s timeout, last-500-message item-slice approach) is the operative path on most setups. State (high-water ROWID) persisted in `DEPLOY_DIR/email-scanner-state.json`.

7. **Zoom Scanner** (every 5 min, `full` role only) — polls Zoom Cloud Recordings API via Server-to-Server OAuth (M2M), downloads VTT transcripts, parses speaker-attributed segments, generates LLM summary, writes `meeting-{date}-{slug}-{id}.md` per meeting. `type: meeting_transcript` in frontmatter. Deduplication via `DEPLOY_DIR/zoom-scanner-state.json`. Requires `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` env vars; exits gracefully if missing.

8. **Commitment Tracker** (every 5 min, `full` role only) — scans `meeting_transcript` and `email_thread` memory files for new/changed content (mtime-based), calls LLM to extract commitments and waiting-on items, writes one `commitment-{slug}-{id}.md` per item. Confidence ≥0.7 → auto-active; 0.5–0.69 → `needs-review` tag; <0.5 → discarded. Exposes `/commitments`, `/complete N`, `/dismiss N` Telegram commands. State persisted in `DEPLOY_DIR/commitment-scanner-state.json`.

9. **Calendar Scanner** (every 5 min, all roles) — reads Apple Calendar.app data (SQLite Calendar Cache primary, EventKit fallback, AppleScript last resort), writes one `calendar-event-{hostname}-{date}-{slug}-{id}.md` per event in a rolling ±7-day window. `type: calendar_event` + `hostname:` in frontmatter. Change detection via modification timestamp. SQLite path requires no permissions; EventKit requires Calendar access grant; AppleScript fallback requires Automation permission to Calendar.app. Exposes `/events [N]` and `/event <N>` Telegram commands (full role only — chat handler is tier-2). State persisted in `DEPLOY_DIR/calendar-scanner-state.json`.

10. **Contact Tracker** (every 5 min, `full` role only) — scans `email_thread`, `meeting_transcript`, `calendar_event`, and `slack_thread` memory files for participant names and emails (mtime-based). Writes one `contact-{name-slug}.md` per person with email-based deduplication, recency-weighted relationship scoring, and interaction history. Exposes `/contacts [N]` (alias: `/people`), `/contact <name|N>` Telegram commands. State persisted in `DEPLOY_DIR/contact-tracker-state.json`.

11. **Slack Scanner** (every 5 min, `full` role only) — polls Slack Web API for threads in monitored channels, writes `slack-thread-*.md` memory files. Requires `SLACK_USER_TOKEN` env var (xoxp-); user ID auto-discovered via auth.test. Exits gracefully if missing. State persisted in `DEPLOY_DIR/slack-scanner-state.json`.

12. **Notification Manager** (every 60 sec, `full` role only) — pushes proactive messages to Telegram: daily morning briefing (calendar, commitments, goals/projects, memory digest), pre-meeting context (10 min before events), commitment deadline alerts (today/tomorrow), goal and project deadline alerts (7 days and 1 day before due date). Exposes `/briefing`, `/mute`, `/unmute` commands. State persisted in `DEPLOY_DIR/notification-state.json`.

13. **Project Inference Scanner** (every 15 min, `full` role only) — scans `email_thread`, `meeting_transcript`, and `slack_thread` memory files for new/updated content (mtime-based), calls LLM to infer what projects the user is working on (confidence ≥ 0.7), writes `project-candidate-{slug}-{id}.md` files with `status: pending_confirmation`. Users review via `/review` and confirm/reject via `/confirm` and `/reject`. Deduplication against existing project files (title similarity ≥ 0.8) and `rejected-candidates.json`. State persisted in `DEPLOY_DIR/project-inference-state.json`.

14. **Goal/Project Agent** (every 6 hours, `full` role only) — checks all active goals/projects for new related memories (via `inferred_from`, tag overlap, title Jaccard, participant overlap), calls LLM to generate reports and proposed actions, writes `action-{source-slug}-{action-id}.md` files with `status: pending`, sends urgent pings via Telegram (with 24h cooldown). Auto-supersedes actions when preconditions no longer hold. Exposes `/actions [filter]`, `/action <N>`, `/run <N>`, `/drop <N>`, `/defer <N> [hours]` Telegram commands. Integrates into daily briefing (shows pending actions and recent goal/project updates). State persisted in `DEPLOY_DIR/goal-agent-state.json` and `rejected-actions.json`.

15. **Quota Scanner** (every 30 min, `full` role only) — tracks Claude.ai Pro and ChatGPT Plus 5-hour rolling-window message quotas. Primary path is manual self-report via `/quota report <platform> <used>/<cap> [reset <min>]`. Optional scraping path (disabled by default; requires `quota.scrape_enabled: true` and cookie file) — WARNING: may violate vendor ToS. Threshold alerts at 75% (warning) and 90% (critical) with per-threshold per-platform 60-min cooldown. Exposes `/quota`, `/quota report`, `/quota reset` Telegram commands. Integrates into daily briefing when `quota.briefing_enabled: true`. State persisted in `DEPLOY_DIR/quota-scanner-state.json`.

16. **Notes Scanner** (every 5 min, all roles) — reads Apple Notes.app via AppleScript, writes `apple-notes-{folder-slug}-{note-slug}-{id}.md` per note. `type: apple_notes` in frontmatter. Notes in todo-style folders (Todos, Tasks, To Do) or with checklist body patterns are flagged `has_todos: true`. Exposes `/notes [N|todos|<folder>]` Telegram command. State persisted in `DEPLOY_DIR/notes-scanner-state.json`. Configurable: `notes_scanner.enabled` (default true), `notes_scanner.skip_folders`, `notes_scanner.interval_seconds`.

**Zoom Scanner** also exposes `/meetings [N]` and `/meeting <N>` Telegram commands for browsing meeting transcripts.

## Two Deployment Roles

- **`full`** — all sixteen loops. Runs on always-on machine (Mac Studio/Mini). Needs `ANTHROPIC_API_KEY` + `GEMINI_API_KEY`.
- **`watcher`** — six capture loops (browser watcher + code/email/calendar/notes/slack scanners). Runs on MacBook. Needs only `GEMINI_API_KEY`. Full-node imports (`python-telegram-bot`, etc.) must be deferred inside the `role == "full"` block to avoid crashing on watcher nodes that don't have those packages installed.

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
- **Max memory file size:** ~6KB. Aim for richer summaries with specific details (numbers, names, dates) rather than brief stubs.
- **Telegram 4096-char limit:** Chat handler must chunk responses.
- **COMMAND_REGISTRY:** Module-level constant in `chat_handler.py` is the single source of truth for all Telegram commands. `/help` renders from it. A test asserts every `CommandHandler` registration has a matching entry — add both when adding a new command.
- **Code repo namespace:** Code repo memories use `type: code` (was `type: code_project` before v1.1.0, then `type: project` + `category: code` before v1.2.0). `CodeScanner.__init__` migrates legacy files automatically. Filename prefix evolved from `project-{name}.md` → `project-{hostname}-{name}.md` → `code-{hostname}-{name}.md`. Module renamed from `project_scanner.py` → `code_scanner.py`. Telegram command renamed from `/projects` → `/code`.
- **Goals and projects namespace:** `type: goal` (outcomes) and `type: project` (efforts) are distinct from `type: code` (auto-scanned repositories). Categories for goals/projects are configurable via `goals.categories` in config.yaml. The `code` category is reserved and cannot be used for goals or projects. The rename of `type: project` + `category: code` → `type: code` was completed in April 2026; see the one-shot migration in `CodeScanner.__init__`.
- **Unified /comms:** Email and Slack threads share `/comms [email|slack]` + `/comm <N>`. No separate `/emails` or `/slack` commands.
- **MemoryCache is the only read path:** Every loop and Telegram command reads memory files through `memory_cache.MemoryCache.query_*()` / `get()` — never via `MEMORIES_DIR.glob()` + `read_text()`. The cache is a derived SQLite index over the iCloud memories directory, rebuilt automatically, and absorbs iCloud `EDEADLK`/`EAGAIN` storms that would otherwise stack up across 15+ async loops. Write-side scanners (browser_watcher, calendar/code/email/notes/slack/zoom scanners, memory_writer) read their own write namespace and remain pass-through by design. `tests/unit/test_memory_cache_migration.py` enforces this invariant at the AST level — regressions land as test failures. See `specs/feat-memory-cache.md` for the full spec.

## Deploy directory

All runtime state lives in `~/secondbrain/` — separate from the repo and from the iCloud brain data:

```
~/secondbrain/
├── venv/                           # Python virtual environment (created by install.sh)
├── logs/                           # out.log, error.log (RotatingFileHandler; 10 MB × 5 backups)
├── seen-urls                       # flat file of processed URLs (browser watcher)
├── execution-log.jsonl             # watcher-node skill execution log
├── chat-execution-log.jsonl        # full-node chat skill execution log (merged into chat.md nightly)
├── memory-cache.sqlite             # derived SQLite read-cache of all memory files; rebuilds automatically
├── email-scanner-state.json        # high-water ROWID for email scanner
├── zoom-scanner-state.json         # processed meeting UUIDs for zoom scanner
├── commitment-scanner-state.json   # processed file mtimes for commitment tracker
├── calendar-scanner-state.json     # processed event modification timestamps
├── notes-scanner-state.json        # processed note modification dates for Apple Notes scanner
├── contact-tracker-state.json      # processed file mtimes and interaction timestamps
├── slack-scanner-state.json        # processed Slack thread timestamps
├── project-inference-state.json    # mtime state for project inference scanner
├── goal-agent-state.json           # last_checked, last_report_hash, last_urgent_ping for goal/project agent
├── rejected-actions.json           # rejected action proposals to prevent re-proposal
├── commitment-corrections.jsonl    # /wrong and /missed feedback log
├── commitment-accuracy.json        # extraction precision stats per source type
├── rejected-candidates.json        # rejected candidate sources to prevent re-proposal
├── notification-state.json         # chat_id, mute state, sent alerts for notification manager
└── quota-scanner-state.json        # Claude.ai Pro and ChatGPT Plus quota tracking state
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
