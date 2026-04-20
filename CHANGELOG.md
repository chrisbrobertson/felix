# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Security (H1) Restrict credential file permissions** — `install.sh` now sets `chmod 600` on the launchd plist, iCloud `config.yaml`, and `~/.litellm/config.yaml` immediately after writing them; prevents local users from reading API keys and tokens stored in these files
- **Security (M6) Skill file checksum verification** — `install.sh` now generates SHA-256 checksums of all deployed skill files and stores them in `~/secondbrain/skill-checksums.json`; `skill_executor.py` verifies checksums on load and refuses to execute skills that don't match (prevents execution of tampered skill files); `skill_optimizer.py` updates the manifest after successful rewrites; gracefully allows execution when no manifest exists or skill not in manifest (for backward compatibility)
- **Security (C2) Prompt injection guard for skill inputs** — `skill_executor.py` now wraps each value in the `inputs` dict with `<untrusted-input name="...">…</untrusted-input>` delimiters and prepends a system-message note telling the model to treat tagged content as data, not instructions; closes the remaining prompt-injection surface (chat context was already protected in v1.4.1).
- **Security (C3) SSRF guard** — `content_fetcher.py` now validates URLs before fetching to block private IPs (127.0.0.1, 10.0.0.0/8, 169.254.0.0/16, etc.), non-HTTP schemes (file://, gopher://), and DNS resolution failures; prevents SSRF attacks against internal network resources
- **Security (C4) Zoom token in Authorization header** — `zoom_scanner.py` now passes the OAuth bearer token via `Authorization: Bearer` header instead of `?access_token=` URL query parameter when downloading recording files; prevents token exposure in server logs and HTTP Referer headers
- **Security (C1) AppleScript injection guard** — `email_scanner.py` now escapes double-quotes and backslashes in mailbox names from config before interpolating into AppleScript string literals; prevents arbitrary code execution via malicious mailbox names in excluded_mailboxes config
- **Security (C5) Unpredictable temp paths for SQLite copies** — `browser_watcher.py`, `email_scanner.py`, and `calendar_scanner.py` now use `tempfile.mkstemp()` with mode 0o600 instead of hard-coded `/tmp/` paths when copying browser history, Mail Envelope Index, and Calendar Cache databases; prevents symlink attacks where a local attacker pre-creates a symlink to redirect the copy and read sensitive data
- **Skill optimizer scoring deadlock** — three compounding bugs prevented the optimizer from ever scoring or optimizing any skill after 6 days of running: (1) `_make_slug()` in `SkillExecutor` now generates sanitized, stable slugs — URL-based skills embed the same SHA1(url)[:6] hash that `memory_writer.py` uses in filenames, non-URL skills are regex-sanitized to strip newlines and pipe characters; (2) `_find_output_by_slug()` in `SkillOptimizer` now matches by hash suffix (primary) with substring fallback for legacy slugs; (3) `chat` skill rows are now marked `n/a` instead of accumulating as perpetually-pending (chat responses stream to Telegram, never to memory files)
- **Score=0.00 excluded from stats** — `_parse_history_rows` was filtering out `score > 0`, silently dropping API-error rows scored at 0.00; changed to `>= 0` so error runs count as data points
- **Missed optimizer pass after daemon restart** — if the daemon restarted after the scheduled 3 AM run hour, the daily pass was skipped until the next day; `run_loop` now persists `last_pass_date` to `skill-optimizer-state.json` and runs a missed pass immediately on restart
- **Judge call hung indefinitely** — `_call_judge` (and `_generate_critique`, `_rewrite_skill`) had no timeout; a single slow Anthropic response blocked an entire scoring pass for 16+ minutes; replaced LiteLLM `timeout=` parameter (unreliable) with `asyncio.wait_for(..., timeout=N)` which guarantees OS-level cancellation; judge timeout=30s, rewrite/critique timeout=60s; timed-out rows remain `pending` and retry next pass
- **Judge JSON truncated at 200 tokens** — max_tokens was too low for some judge responses, producing invalid JSON; bumped to 300
- **Rewrite validation silent failure** — when LLM returned a rewritten skill without `## Instructions`, validator returned `None` silently; now logs a 300-char preview of the bad response so failures are diagnosable
- **O(n×m) memory scan in scoring** — `_find_output_by_slug` read all memory files for every pending row (503 rows × ~1000 files = 503k iCloud reads per pass); `_score_pending_rows` now calls `_build_memory_index()` once per skill to load all memory files into memory, then matches via `_find_output_in_index()` in O(1) per row

### Added
- **Security (H2) Keychain-first secret retrieval** — new `secrets.py` module provides `get_secret_or_env(name, env_var)` that reads from macOS Keychain (`secondbrain-{name}` service) first, falling back to environment variables; `install.sh` now stores all API keys and tokens in Keychain automatically; `zoom_scanner.py`, `slack_scanner.py`, and `github_client.py` updated to use Keychain retrieval; env-var fallback remains active so existing deployments continue working without interruption
- **Adapter protocol** — `transport.py` defines `TransportAdapter` protocol and `CommandContext` dataclass; any future transport (Slack, MCP, REST) implements this interface without touching command logic
- **CommandRouter** — `command_core.py` provides a transport-agnostic command dispatcher and moves `COMMAND_REGISTRY` to a single source of truth
- **TelegramAdapter** — `telegram_adapter.py` wraps `TelegramChatHandler` as a `TransportAdapter`, enabling multi-transport notification dispatch
- **Multi-transport NotificationManager** — `notification_manager.py` now accepts an optional `transports` list; `send_message` routes to all active adapters when set, falling back to the legacy `bot=` path
- **SlackClient** — `slack_client.py` extracts shared Slack API infrastructure (rate-limit-aware `api_call`, `resolve_user` with cache, `list_channels`, `post_message`) from the scanner into a reusable client; scanner and future chat adapter both use it
- **Full command bridge (Phase 3)** — `TelegramChatHandler.register_with_router()` wraps all 90+ `cmd_*` methods in fake Telegram objects so every command works over Slack DM without touching existing handlers; `CommandContext.raw_text` added to carry free-text for LLM chat delegation
- **Multi-transport notifications (Phase 4)** — `daemon.py` now builds a `TelegramAdapter` + optional `SlackTransportAdapter` and passes both to `NotificationManager` at startup; briefings, alerts, and goal notifications are now delivered to both Telegram and Slack simultaneously when both transports are configured

## [1.4.1] — 2026-04-18

### Fixed
- **Path traversal guard** — `goal_project_agent.py` now validates LLM-supplied `target` filenames before constructing paths: rejects `..`, `/`, `\`, confirms resolved path stays within `MEMORIES_DIR`, and checks target exists before writing or executing an action
- **Atomic skill file write** — `skill_executor.py` now uses tmp→rename pattern when appending to the execution history table, preventing corruption of the iCloud-synced skill file on crash mid-write
- **Atomic seen-urls write** — `browser_watcher.py` now uses tmp→rename for `save_seen_urls()`, preventing partial-write data loss on shutdown
- **Atomic index.md write** — `index_builder.py` now uses tmp→rename when writing the rebuilt index, preventing a torn iCloud sync on crash
- **Response size cap** — `content_fetcher.py` now streams HTTP responses and aborts after 10 MB, preventing out-of-memory on pathological responses; timeout still applies
- **Error message sanitization** — `chat_handler.py` now passes all exception messages through `_safe_error()` before sending to Telegram; strips filesystem paths, caps at 100 chars
- **Memory context isolation** — injected memory snippets in chat LLM prompts are now wrapped in `<memory-context>…</memory-context>` delimiters, making prompt-injection breakout harder
- **Dependency floor** — `litellm>=1.35.0` → `litellm>=1.49.1` (fixes CVE-2024-6587 SSRF in litellm's proxy)

## [1.4.0] — 2026-04-17

### Fixed
- `/complete` and `/dismiss` now accept multiple space-separated indices (`/complete 1 3 5`); per-item result reported in single reply; duplicate indices processed once
- Agent now has `close_issue` tool — can close bugs and features via conversation using short_id or title substring; status can be set to `done`, `wont_do`, or `in_progress`
- Chat history now persisted to `chat-history.json` — conversation context survives daemon restarts instead of being lost on every deploy or crash
- Chat skill prompt now explicitly instructs the LLM to use conversation history for resolving follow-ups and pronouns
- Zoom AI Companion: HTTP 400 errors no longer permanently disable the integration for the session — only 403 (missing scopes) triggers permanent disable; 400 and other transient errors log the response body and retry next cycle

### Added
- **LLM chat import** — `/import_chats` accepts ChatGPT (ZIP with `conversations.json`) and Claude (ZIP or JSON) conversation exports attached to the Telegram message; writes one `llm-chat-{platform}-{date}-{slug}-{id}.md` per conversation with `type: llm_chat` frontmatter; body includes first 3 exchanges truncated to ~500 chars each; auto-detects format; `llm_chat_importer.py` module with `import_file()`, `_parse_chatgpt()`, `_parse_claude()` functions
- **Communication watchlists** — `/watch "topic" [from:person] [type:email|slack|meeting]` creates a watchlist; email, slack, and zoom scanners check active watchlists after each successful memory write and send a Telegram alert on match; `/watches` lists active/triggered watchlists; `/unwatch N` deactivates; watchlists stored as `watchlist-*.md` in memories with `type: watchlist`; matching checks topic keywords (all must appear), optional person (substring in participants or body), optional type filter; `watchlist_checker.py` utility module; `notification_callback` wired on all three scanners in `daemon.py`
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
