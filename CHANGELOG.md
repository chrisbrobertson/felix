# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- `/complete` and `/dismiss` now accept multiple space-separated indices (`/complete 1 3 5`); per-item result reported in single reply; duplicate indices processed once
- Agent now has `close_issue` tool — can close bugs and features via conversation using short_id or title substring; status can be set to `done`, `wont_do`, or `in_progress`
- Chat history now persisted to `chat-history.json` — conversation context survives daemon restarts instead of being lost on every deploy or crash
- Chat skill prompt now explicitly instructs the LLM to use conversation history for resolving follow-ups and pronouns
- Zoom AI Companion: HTTP 400 errors no longer permanently disable the integration for the session — only 403 (missing scopes) triggers permanent disable; 400 and other transient errors log the response body and retry next cycle

### Added
- **Memory synthesis** — new `synthesis_scanner.py` (15th async loop, `full` role only) clusters related memories by shared tags or title Jaccard ≥0.40, generates structured synthesis insights for clusters of ≥3 memories using `chat` LLM route, writes `type: synthesis` memory files; skips already-processed clusters via SHA1 state in `synthesis-state.json`; `/insights` Telegram command lists 10 most recent synthesis memories
- `memory_writer.py` now propagates `source_files` and `type` entry fields to frontmatter (needed for synthesis memories)
- **Memory deduplication** — new `dedup_checker.py` runs after every index rebuild; Pass 1 auto-merges URL-identical memories (strips utm_* / tracking params, strips www., keeps richer file); Pass 2 detects near-duplicate titles by Jaccard similarity ≥0.70 and stores them as candidates in `dedup-state.json`; `/dupes` lists candidates, `/merge N` merges keeping richer file + union of tags, `/keep N` dismisses as intentionally distinct
- **Deep memories** — new `summarize-deep` skill produces structured 1000-word analysis (Summary, Key Findings, Notable Quotes, Implications) using `claude-sonnet-4-6`; `skill_router.py` auto-classifies research papers and long-form articles as `depth: deep`; browser watcher passes depth through to `MemoryWriter`; `/deepen N` Telegram command re-processes any URL-sourced memory with the deep skill; `memory_writer.py` records `depth` in frontmatter
- PDF text extraction — `content_fetcher.py` now uses `pdfminer.six` to extract text from PDF responses (detected by `Content-Type: application/pdf` or `.pdf` suffix); title extracted from PDF metadata; falls back to empty string on extraction failure; `requirements.txt` updated
- Document upload via Telegram — bot now accepts file attachments in addition to URLs; PDFs, plain-text, and HTML files are downloaded, content extracted, and processed through the appropriate skill, producing a memory file; `/remember` URL path unchanged
- **Skill utility scoring** — `skill_optimizer.py` now computes a recency-weighted `utility_score` (half-life decay, default 14 days) and `score_trend` (improving/declining/stable) alongside the existing `success_rate`; optimizer gates prefer `utility_score` when available; declining skills with `utility_score < 0.80` bypass `min_runs` and cadence gates for immediate rewrite; `/skill_health` Telegram command now shows real scores and trend arrows (▲ ▼ ◆) instead of `?`

- **Zoom AI Companion integration** — `zoom_scanner.py` now polls `GET /v2/meetings/meeting_summaries` each cycle; when a meeting has both a VTT and an AI Companion summary the AI Companion overview replaces the LLM summary call (cost savings) and a `## Action Items` section is added; meetings with AI Companion but no cloud recording get their own memory file for the first time
- **Zoom local recording scanner** — scans `~/Documents/Zoom/` for meeting folders matching `YYYY-MM-DD HH.MM.SS <Topic>` that contain `closed_caption.vtt`; runs on watcher role; opt-in via `local_recordings_enabled: true` in config
- `summary_source: ai_companion | llm` frontmatter field on all meeting memory files for traceability
- `processed_summaries` and `processed_local` dedup sets added to `zoom-scanner-state.json` (backwards-compatible; missing keys initialise to `[]`)
- `specs/feat-zoom-ai-companion.md` — spec for integrating Zoom AI Companion meeting summaries: polls `GET /v2/meetings/meeting_summaries` alongside cloud recordings, uses AI Companion summary instead of LLM when available, expands coverage to meetings with AI Companion but no cloud recording
- `specs/feat-zoom-transcript-scanner.md` updated (v1.2.0) — adds FR-11 through FR-19 for local recording support: scans `~/Documents/Zoom/` for meeting folders with `closed_caption.vtt`, reuses existing VTT parser, opt-in via config

## [1.3.1] — 2026-04-16

### Added
- `add_bug` and `add_feature` Telegram tools — agent can now file bugs and feature requests directly from chat (previously only `add_project` existed)
- `chat.md` system prompt now explicitly reminds the agent it always has function-calling tools, preventing "I have no tools" refusals

### Fixed
- Chat handler: context scoring now augments short queries (< 3 tokens) with recent conversation turns, so follow-up messages ("yes", "which one?") retrieve the right memories
- Chat handler: `deliver_pending_replies` tool now only appears when the last history entry is the "📬 Network is back" notification — prevents spurious fires on unrelated "yes" replies
- Chat handler: `/close` accepts a 6-char `short_id` hash in addition to a numeric index, matching the ID shown in `/feature` and `/bug` listings
- Chat handler: `timedelta` alias was shadowing `datetime` in `_rewrite_features_index_snapshot`, causing `AttributeError: type object 'timedelta' has no attribute 'now'`
- Notification manager: pre-meeting 10-min alert dedup now keyed on `file.stem` (the full canonical name) rather than a glob-matched `event_id` that never matched, so the alert fires exactly once per event instead of on every scan cycle
- Skill executor: fallback model loop now catches only transient errors (`RateLimitError`, `APIConnectionError`, `ServiceUnavailableError`, `InternalServerError`, `Timeout`); auth errors and schema errors propagate immediately instead of being silently swallowed
- Install: `content_fetcher.py` was missing from `DAEMON_FILES` — watcher nodes crashed with `ModuleNotFoundError: No module named 'content_fetcher'` on startup
- Install: `VERSION` file now deployed to `~/secondbrain/` alongside daemon modules
- Email scanner: `get_threads_since()` and `get_threads_updated_since()` now run via `asyncio.run_in_executor` — eliminates `OSError: [Errno 11] Resource deadlock avoided` caused by blocking `subprocess.communicate()` on the event loop thread on macOS
- Daemon: `LITELLM_LOG=ERROR` set before any scanner module imports litellm — eliminates spurious INFO-level completion lines appearing in `error.log` (litellm installs a `StreamHandler(stderr, DEBUG)` at import time that bypassed our log-level filters)

## [1.3.0] — 2026-04-16

First tagged release. Establishes semver infrastructure (VERSION file,
`/version` command, version in startup log, tag reminder in install.sh).

### Added
- **Goal/Project Agent** (14th async loop) — periodically checks active goals and
  projects for new related memories, proposes actions via Telegram (`/actions`,
  `/action`, `/run`, `/drop`, `/defer`)
- **Project Inference Scanner** (13th loop) — infers projects from email/meeting/slack
  memories and writes candidate files for confirmation (`/review`, `/confirm`, `/reject`)
- **Report Scheduler** — configurable periodic reports delivered via Telegram
- **Notification Manager** (12th loop) — daily briefing, pre-meeting context, commitment
  and goal deadline alerts (`/briefing`, `/mute`, `/unmute`)
- **Slack Scanner** (11th loop) — polls Slack channels, writes `slack-thread-*.md` memories
- **Contact Tracker** (10th loop) — aggregates participants across memories
  (`/contacts`, `/contact`)
- **Goals and Projects** — full CRUD via Telegram (`/addgoal`, `/addproject`, `/goals`,
  `/projects`, and related commands)
- **Commitment Tracker** (8th loop) — extracts commitments from meetings and email
  (`/commitments`, `/complete`, `/dismiss`)
- **Calendar Scanner** (9th loop) — reads Apple Calendar, writes `calendar-event-*.md`
  memories (`/events`, `/event`)
- **Email Scanner** (6th loop) — reads Apple Mail, classifies and summarises threads
  (`/comms`, `/comm`)
- **Code Scanner** (5th loop) — scans git repos, writes `code-{hostname}-*.md` memories
  (`/code`)
- **Zoom Scanner** (7th loop) — polls Zoom Cloud Recordings, writes meeting transcripts
  (`/meetings`, `/meeting`)
- Semver infrastructure: `VERSION` file, `/version` command, version in daemon startup log

### Fixed
- Verb dispatch: all list+detail commands now accept natural-language verbs in addition
  to index numbers (e.g. `/goal add "..."` routes correctly rather than returning
  "Invalid index")
- Calendar scanner: AppleScript `modified_time` was `datetime.now()`, causing all events
  to be re-summarised every scan cycle — now uses `start_time` as a stable proxy
- Calendar scanner: improved diagnostic logging (data source selected, event counts,
  Automation permission errors now visible at WARNING level)

## [1.2.0] — 2026-04 (approximate)

### Changed
- Code repo memories renamed from `type: project` + `category: code` → `type: code`
- Filenames migrated from `project-{hostname}-*.md` → `code-{hostname}-*.md`
- Telegram command renamed from `/projects` → `/code`

## [1.1.0] — 2026-04 (approximate)

### Added
- Commitment Tracker, Code Scanner, Calendar Scanner (hostname-scoped filenames)
- Email classification (human / transactional / marketing / automated)

## [1.0.0] — 2026-04-11

Initial working system: browser watcher, Telegram chat handler, index builder,
skill optimizer, and the core flat-file memory architecture.
