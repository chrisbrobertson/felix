# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Commitment day checkpoints**: `notification_manager` now sends a midday summary (default 12:00) and an end-of-day reminder (default 17:00) for all active commitments due that day. Each slot fires at most once per calendar day and is silently skipped if the daemon was offline more than 2 hours past the scheduled time. Configurable via `notifications.midday_alert_time` and `notifications.eod_alert_time` in `config.yaml` (#106).
- `/pending` Telegram command: unified inbox showing the count of all items awaiting human review — project/repo candidates, pending agent actions, and skill drafts. Points to `/review`, `/actions`, and `/skill_drafts` for detail (#20).
- `/todos` Telegram command: shows all active commitments as a `[ ]` checklist. Supports `/todos done N [M…]` to mark complete and `/todos dismiss N` to dismiss. Personal todos are shown without a type tag; extracted commitments show their type in brackets (e.g. `[outbound]`). Populates the shared commitment index so `/complete N` also works after a `/todos` listing (#51).
- `usage_tracker`: records prompt/completion token counts per model per day from every LiteLLM call in `skill_executor`. State stored in `~/secondbrain/usage-tracker-state.json` with 30-day retention (#13).
- `/usage [days]` Telegram command: shows token usage totals per model for the last N days (default 7). `/usage daily` shows a per-day rolling total. Works across all models routed through LiteLLM (#13).
- `/goal_note <N> <text>` — append a timestamped note to a goal conversationally (#18).
- `/goal_due <N> <YYYY-MM-DD|none>` — update or clear a goal's due date (#18).
- `/project_note <N> <text>` — append a timestamped note to a project conversationally (#18).
- `/project_due <N> <YYYY-MM-DD|none>` — update or clear a project's due date (#18).
- `/changes [hours]` Telegram command: scans all active goals and projects, finds related memory files updated in the last N hours (default 24, max 168), and sends a concise LLM-generated activity digest per item — one paragraph per project/goal with recent activity. Implemented in `GoalProjectAgent.generate_change_digest()` (#74).
- `install.sh` now runs a Python smoke import (`import daemon` + `from chat_handler import TelegramChatHandler` on full role) after deploying source files and before reloading launchd. If the deployed artefact would crash at import time, the installer exits 1 and leaves the running daemon untouched.

### Fixed
- `/commitments`, `/todos`: past-due active items now display `was due <date> ⚠️` instead of the bare date, so overdue deliverables are visually flagged rather than silently showing a stale date. Same fix applied to `/goals` (shows `was due <date> ⚠️ OVERDUE`) and `/projects` (shows `was due <date> ⚠️ OVERDUE`) (#103).
- `notification_manager`: malformed or null frontmatter in any memory-cache entry no longer aborts `_assemble_briefing`, `_check_commitment_alerts`, or `_check_pre_meeting_alerts` — each `json.loads` call is now individually guarded by `try/except (JSONDecodeError, TypeError)`. Additionally, `_check_and_send` now runs each check (`_check_daily_briefing`, `_check_commitment_alerts`, etc.) in its own `try/except` so a single failing check cannot silence subsequent ones (#52).
- `usage_tracker.record_usage`: read-modify-write on the state JSON is now protected by a module-level `threading.Lock`, preventing lost increments when multiple async loops call it concurrently (#13).
- `skill_optimizer`: all four `acompletion` call sites now record token usage via `record_usage`, so `/usage` totals include judge and optimizer traffic (#13).
- `/usage` command now uses `_send_reply` instead of `reply_text` so long summaries are chunked and retried on transient network errors (#13).
- `skill_executor`: `AuthenticationError` from LiteLLM now returns `None` immediately and logs an ERROR — the fallback model is never tried when the API key itself is bad (#59).
- `skill_executor`: `PermissionDeniedError` (model-tier/entitlement 403) now falls back to the next configured model instead of hard-stopping, so skills with cross-provider fallbacks degrade gracefully when the preferred model is unavailable to the current key (#59).
- Chat tool-call loop: the `chat` skill prompt’s “Always attempt tool calls when appropriate” instruction caused the model to make unnecessary tool calls on questions already answered by `memory_context`, exhausting `max_iterations=5` and returning a “ran out of iterations” fallback visible to the user as “Too many tool calls” (#77). Rewrote the instruction to prefer context-first answers and call tools only when fresh data is genuinely required.
- Chat lock starvation: added a 240 s `asyncio.wait_for` timeout around `executor.run_with_tools` in `handle_message`. A slow tool-call loop could hold the per-chat asyncio lock indefinitely, serialising all subsequent messages for that chat (#65, #77).
- Mutation tracking: timeout warnings now show the result text of completed mutations (e.g. “Goal created: …”) rather than just the tool name, so users know what to verify before retrying. `deliver_pending_replies` added to `MUTATING_TOOLS` so it is tracked like other state-writing tools. In-flight mutations (in-progress when the timeout fired) are also reported separately, preventing blind retries that could resend already-delivered pending replies.

## [1.11.0] — 2026-04-28

### Added
- Daily briefing now includes an "Active projects" section listing all active `type:project` files with their due dates, milestone completion counts (N/M done), and a `[new]` marker for projects created within the last 7 days (#47).

### Fixed
- `memory_cache.query_by_prefix()` in cache mode now includes a disk fallback for files written since the last sweep, preventing `/review` and `/review purge` from returning empty or incomplete results on a cold cache (e.g. right after daemon startup or cache rebuild).
- `ProjectInferenceScanner._cleanup_stale_candidates()` now evicts deleted candidate rows from the SQLite cache immediately via `invalidate()`, so `/review` no longer lists already-deleted candidates until the next 60-second sweep.
- `/confirm` for project-type candidates now indexes the newly created `project-*` file in the cache immediately, so chat context and project listings reflect the confirmation without waiting for the next sweep. The `code_repo` confirm path also indexes the new `code-*` file.
- `memory_cache.invalidate()` wrongly deleted a valid cache entry (or skipped adding a new one) when iCloud returned EDEADLK while reading the file. The fix checks `path.exists()` before deleting: if the file is present but unreadable the old entry is preserved and the update is retried on the next 60-second sweep cycle. This prevented overdue commitments (and other recently-written files) from appearing in morning briefings when iCloud was actively syncing (#75).
- Briefing: unparseable `due_date` values (e.g. `"Unknown"`) now emit a `log.warning` instead of silently dropping the commitment, making future date-format bugs diagnosable in the logs.
- `ProjectInferenceScanner._scan()` early-returned without running `_cleanup_stale_candidates()` when no source files had changed. This caused project candidates to accumulate past the configured cap (default 200) because cleanup only ran after new files were processed. Fix: always run cleanup on every scan cycle (#39).
- `cmd_review` refactored to use `self._cache.query_by_prefix()` instead of globbing + reading each candidate file 2–3 times. With 600+ accumulated candidates this caused multi-second iCloud fan-outs during `/review` (#39).
- `memory_cache` pass-through mode serialises YAML `datetime.date` objects in frontmatter to JSON ISO strings; previously `json.dumps` raised `TypeError` on date-valued fields like `due_date: 2026-07-01`.

## [1.10.1] — 2026-04-28

### Fixed
- `/briefing` command returned "Internal error — check logs" because `cmd_briefing` called `notification_manager._assemble_briefing()` without `await`, passing a coroutine object to `reply_text` instead of the assembled briefing text (#76).
- `scripts/promote_local_features.py`: throttle `gh issue create` calls with a configurable inter-issue delay (default 2 s, `--delay-seconds` flag to override or disable) to avoid tripping GitHub's secondary rate limits when promoting large batches of local feature/bug files.

## [1.10.0] — 2026-04-27

### Added
- `/review_purge [N]` command to bulk-delete pending project candidates older than N days (default 30) (#56)
- Automatic candidate cleanup in `ProjectInferenceScanner`: files older than `candidate_ttl_days` days (default 30) or exceeding `max_pending_candidates` total (default 200) are deleted each scan cycle (#56)
- Config options `project_inference.candidate_ttl_days` and `project_inference.max_pending_candidates` to tune cleanup behaviour

## [1.9.2] — 2026-04-26

### Added
- `scripts/babysit-with-review.sh` — review-gated backlog drainer: promotes local feature/bug files, loops `claude -p` against open `kind:bug`/`kind:feature` issues, runs a codex<->claude review cycle on each PR, and automatically merges + deploys (`NONINTERACTIVE=1 ./install.sh`) when codex reports zero blocking findings.

### Fixed
- Pre-meeting alerts fired twice when both the watcher-role MacBook and full-role Mac Studio had written hostname-scoped `calendar-event-*.md` files for the same meeting. Dedup key changed from filename stem (machine-specific) to `source_url` (machine-independent, derived from title+start\_time by `calendar_scanner.py`). Legacy state entries (stem keys) and files without `source_url` still handled correctly via backward-compat fallback. (#33, #34)
- `scripts/promote_local_features.py` failed with "could not add label: 'kind:bug' not found" on any repo without pre-existing labels. Added `gh_ensure_labels()` that bootstraps the standard label vocabulary (`kind:`, `status:`, `priority:` labels via `gh label create --force`) before the first issue is created.
- Bumped `litellm` 1.83.4 → 1.83.14 to resolve 3 CVEs (critical SQL injection in proxy key verification, high SSTI in prompts endpoint, high command execution via MCP stdio endpoint; all fixed in 1.83.7).
- Bumped `lxml` 6.0.3 → 6.1.0 to resolve CVE: XXE via default `iterparse()` configuration (high).
- `list_projects` LLM tool crashed with `AttributeError` on every call because `_list_projects_text` was referenced in `chat_tools.py` but never implemented in `chat_handler.py`. Extracted the formatting logic from `cmd_projects` into a new `_list_projects_text(category, limit)` method so both the Telegram command and the tool dispatch share the same path (#64).

## [1.9.1] — 2026-04-25

### Fixed
- Chat latency: `_build_goal_project_context` was synchronously reading 584 `project-candidate-*.md` files on every query via `list_projects()` glob match (`project-*.md` matched candidates). At 750ms each under iCloud EDEADLK, "hello" took 52+ seconds. Migrated to `cache.query_by_type("goal"/"project")` — two SQL queries, sub-ms.
- `index.md` read in `_load_context` now uses `read_text_with_retry_async` instead of bare `read_text()`, keeping the event loop unblocked under iCloud pressure.
- `goals_tracker.list_projects()` now excludes `project-candidate-*` filenames before reading — protects `/projects`, `/list_projects` LLM tool, and `goal_project_agent` from the same fan-out.

## [1.9.0] — 2026-04-25

### Added
- `quota_scanner.py` loop — tracks Claude.ai Pro and ChatGPT Plus 5-hour rolling-window message quotas.
- `/quota` Telegram command — show current quota state, self-report via `/quota report <platform> <used>/<cap> [reset <min>]`, clear via `/quota reset <platform>`.
- Quota threshold alerts — warning at 75%, critical at 90% of cap, per-threshold per-platform 60-min cooldown.
- Daily briefing now includes a Quotas section when `quota.briefing_enabled: true`.
- Optional opt-in scraping path (`quota_scrapers.py`) — disabled by default; see README for ToS caveats.

## [1.8.0] — 2026-04-25

### Added
- `MemoryCache` module backed by `~/secondbrain/memory-cache.sqlite` — derived SQLite read-cache of all iCloud memory files; eliminates EDEADLK from the hot read path
- `/rebuild_cache` Telegram command — force-rescan memories into cache
- `chat_handler._load_context` migrated to SQLite; drops `_header_cache` (now redundant)
- `daemon.memory_cache.enabled` config gate (default `true`)
- `specs/feat-memory-cache.md` — design spec capturing architecture and constraints
- Cache sweep loop runs every 60s on full-role daemon to catch iCloud-arrived files from watcher

## [1.7.0] — 2026-04-25

### Added
- `/aichat` command — list, detail, and search for imported Claude/ChatGPT conversations. Default list mode groups by platform, `/aichat <N>` shows summary and topics, `/aichat search <q>` keyword-filters headers. Integrates imported llm_chat memories from v1.4.0 into first-class browse surface (closes FR-12 from feat-llm-chat-import).
- `/comms llm` filter — surface llm_chat memories alongside email and slack threads. Updated `/comms` to accept `email|slack|llm` filter, extended `_list_comms_text` and `cmd_comm` detail view with llm_chat branches.
- Refresh nudge — daily check that nudges the user when llm_chat memories are >14 days stale (configurable via `llm_chat.refresh_interval_days` and `llm_chat.nudge_cooldown_days`). Kill-switch via `llm_chat.nudge_enabled: false`.
- `search_memories` tool now recognizes `type=llm_chat` — chat skill can pull imported conversations naturally into context when answering questions about prior AI discussions.
- `scripts/work_reports.sh` — autonomous backlog drainer. Promotes any local `feature-request-*.md` files to GitHub issues (via `scripts/promote_local_features.py`), then loops `claude -p` against open `kind:bug` / `kind:feature` issues, picking one per iteration, branching, implementing, testing, committing, and opening a PR. Stops on `STOP` token, stuck loop, or `MAX_ITER`. Modeled on `~/repos/scripts/babysit.sh`. Documented in README under "Working the backlog autonomously".
- `scripts/promote_local_features.py` — standalone CLI that mirrors the promotion half of `/feature_import` (uses `gh` directly, no daemon required). Supports `--dry-run` and `--repo`.
- Circles Phase B: add `/circles`, `/circle <N>`, `/circle_status` host Telegram commands for inspecting circle sync state and ruleset details

## [1.7.2] — 2026-04-25

### Fixed
- **Telegram chat handler hung indefinitely on stalled LLM calls** — `skill_executor.acompletion()` calls in both `run()` and `run_with_tools()` had no `timeout=` argument. LiteLLM's default is unbounded, so a stalled HTTP connection (overloaded model, network blip, partial stream) wedged the chat handler's `await` forever — slash commands kept working but plain chat replies silently never arrived. Added `timeout=90` (overridable via `timeout:` in skill frontmatter) to both call sites. `LiteLLMTimeout` was already in `_RETRYABLE_ERRORS`, so a timeout now falls back to the secondary model and ultimately produces the existing user-visible "Sorry — the chat model failed" reply instead of silence. Live evidence: chat message at 11:58:46 received, `acompletion()` invoked at 11:59:27, no response logged in 30+ minutes.
- **`_load_context` hung chat replies for hours under iCloud EDEADLK pressure** — with 3300+ memory files all returning EDEADLK, the file-reading loop in `_load_context` called `read_text_with_retry` (3 retries × ~1.5 s sleep) on every file before giving up, taking potentially hours to complete. Because `_load_context` runs in `asyncio.to_thread`, the event loop stayed responsive (slash commands worked), but chat replies never arrived. Added a 25-second wall-clock deadline: the loop now breaks early and returns whatever partial context it has assembled, so chat always responds within ~30 s even under severe iCloud sync pressure.

## [1.7.1] — 2026-04-25

### Fixed
- **All proactive notifications silently dropped since Phase 4 transport wiring landed** — `TelegramAdapter._bot()` looked for `handler._app` (underscore) but `TelegramChatHandler` stores the Application as `handler.app` (no underscore). `_bot()` always returned `None`, causing `send_text()` to warn "bot not available" and return silently. Every daily morning briefing (07:30), pre-meeting alert, commitment deadline alert, and goal/project deadline alert was dropped without any error or state rollback. Fixed by correcting the attribute name to `"app"`.
- **`send_text()` swallowed all failures** — the old code returned `None` on bot unavailability and caught all `bot.send_message` exceptions with a `log.warning`. Combined with the attribute typo, failures were invisible. Now raises `RuntimeError` when the bot is unavailable and lets `send_message` exceptions propagate so callers can detect and recover from failures.
- **`notification_manager.send_message` marked sends as successful even after transport errors** — set `any_sent = True` before checking whether the transport actually succeeded, preventing the existing `_check_daily_briefing` rollback of `last_briefing_date` from ever firing. Now tracks `last_error` across transports and re-raises it when none succeeded.
- **`TelegramAdapter` unit tests masked the attribute mismatch** — mocks used `handler._app` matching the buggy code, so tests passed while never exercising the real attribute. Fixed to use `handler.app`; added a regression test with `spec=["app"]` that would have caught the original bug.

## [1.7.0] — 2026-04-25

### Added
- Circles Phase A: `circle_ruleset.py` parser and `circle_sync_scanner.py` async loop for rule-based one-way iCloud memory sharing; enabled via `circles.enabled: true` in config (off by default)

### Fixed
- **`browser_watcher` wrote duplicate memory files for the same URL visited via different tracking params** — URL hash was computed from the raw URL, so `example.com/page?utm_source=email` and `example.com/page?utm_source=twitter` produced different hashes and bypassed the `seen-urls` dedup guard. Added `_canonicalize_url()` in `memory_writer.py` that strips UTM/fbclid/gclid/etc. tracking params, strips fragments, and lowercases scheme + host before hashing. Both `BrowserWatcher.seen_urls` and `MemoryWriter._build_filename` now use the canonical form. Existing `seen-urls` entries are canonicalized on load so prior raw-URL entries are still matched.

## [1.6.9] — 2026-04-23

### Fixed
- **`_load_context` blocked the asyncio event loop for up to 8 minutes per query** — `chat_handler._handle_message` called `self._load_context(query, history)` directly from async context. With 3300+ memory files in iCloud, `_load_context` does thousands of `stat()` + `read_text()` calls, freezing Telegram polling and all other loops for the full duration. Now dispatched via `await asyncio.to_thread(self._load_context, query, history)`.
- **Context loading silently returned 0 files under iCloud sync pressure** — the memory-file read loop in `_load_context` used a bare `f.read_text()` inside a bare `except OSError: continue`. Any transient EDEADLK/EAGAIN during iCloud sync dropped the file silently with no retry, yielding "0 files, 0 tokens" context. Now uses `read_text_with_retry(f, default=None)` so transient locks are retried before skipping.
- **`notification_manager` used blocking `time.sleep()` during iCloud retries in async context** — `_assemble_briefing`, `_assemble_pre_meeting_context`, and `_prune_sent_alerts` called `read_text_with_retry()` (which uses `time.sleep()` for backoff) directly from async functions. Under iCloud sync pressure, this blocked the event loop per file. All three methods are now `async def`, all file reads use the new `read_text_with_retry_async()` helper (uses `await asyncio.sleep()`), and their callers are updated accordingly.
- **Added `read_text_with_retry_async`** — new coroutine in `utils.py` that mirrors `read_text_with_retry` but uses `await asyncio.sleep()` for backoff, safe to call from async contexts without blocking the event loop.

## [1.6.8] — 2026-04-23

### Fixed
- **`index_builder`, `browser_watcher`, `report_scheduler` ignored stop_event during sleep** — three `run_loop` implementations used `await asyncio.sleep(interval)` instead of the `await asyncio.wait_for(stop_event.wait(), timeout=interval)` pattern used by every other loop. On daemon shutdown (SIGTERM), these loops blocked for up to 60 min (`index_builder`), 5 min (`browser_watcher`), or 60 s (`report_scheduler`) before exiting. All three now respond to stop_event immediately.
- **`index_builder` inline iCloud retry only caught EDEADLK, missed EAGAIN** — the inline retry loop at the memory-file read site checked `e.errno == 11` only, unlike `utils.read_text_with_retry` which was fixed in v1.6.6 to check both EDEADLK (11) and EAGAIN (35). Updated to match.
- **`/defer` accepted negative hours** — `/defer 1 -5` set `defer_until` to 5 hours in the past, causing the action to immediately re-appear as not-deferred. Now rejects hours ≤ 0 with a clear error message.

## [1.6.7] — 2026-04-22

### Fixed
- **`browser_watcher` blocked the asyncio event loop on SQLite reads** — `run_loop` and `backfill` called `_fetch_recent_urls()` (which does `shutil.copy2` + `sqlite3.connect`) directly from async context. SQLite reads block for 10–200 ms under normal conditions, longer under I/O pressure. Both callsites now use `await asyncio.to_thread(self._fetch_recent_urls, since)`.
- **`calendar_scanner` blocked the asyncio event loop on `source.get_events()`** — `_run_scan` and the `/calendar` backfill handler called `source.get_events()` directly from async context. For `AppleScriptSource` (the primary fallback), this runs `subprocess.Popen(["osascript", ...], timeout=60)` — freezing all 14 event-loop tasks plus Telegram polling for up to 60 seconds. Both callsites now use `await asyncio.to_thread(source.get_events, ...)`.
- **`slack_scanner` pagination `while True` loops had no iteration cap** — `_get_channel_messages` and `_fetch_thread_replies` looped indefinitely on Slack cursor pagination. A high-volume channel or a runaway API response could loop forever. Both loops now cap at `MAX_SLACK_PAGES = 50` and log a warning if the limit is reached.
- **Processed-file state dicts grew unbounded** — `commitment_tracker`, `contact_tracker`, and `project_inference_scanner` all accumulated `{filename: mtime}` state entries but never pruned them when source memory files were deleted. Over months, state files bloated with thousands of stale entries. `_save_state()` in each module now prunes entries for files that no longer exist in MEMORIES_DIR before writing.

## [1.6.6] — 2026-04-23

### Fixed
- **Systematic iCloud EDEADLK/EAGAIN resilience across all modules** — full codebase audit identified three recurring bug classes and fixed them:
  1. **`skill_executor._log_execution` crashes chat** — bare `read_text()` on `skills/chat.md` raised EDEADLK after the LLM had already produced its answer, sending "Sorry — processing failed" instead. `_log_execution` is now best-effort: uses `read_text_with_retry(default=None)` and returns silently on any OSError — logging telemetry must never crash user-visible features. Also hardened the watcher-path JSONL write and the full-node tmp→rename write.
  2. **`skill_executor._load` bare `read_bytes()`** — transient iCloud lock during skill hot-reload could crash executor construction. Now uses `read_bytes_with_retry` (new helper in utils.py).
  3. **`utils.py` only retried errno 11 (EDEADLK), missed errno 35 (EAGAIN)** — `code_scanner` and `project_inference_scanner` already handled both; `read_text_with_retry` and `load_config` now match. Added `read_bytes_with_retry` to utils.py.
  4. **11 modules used bare `CONFIG_PATH.read_text()`** — `browser_watcher`, `calendar_scanner`, `code_scanner`, `commitment_tracker`, `contact_tracker`, `email_scanner`, `goal_project_agent`, `index_builder`, `skill_optimizer`, `slack_scanner`, `zoom_scanner` all replaced with `load_config()`.
  5. **`chat_handler._safe_read_text` caught OSError without retrying** — upgraded to delegate to `read_text_with_retry(default=None)` for consistent backoff.
  6. **~70 bare `path.read_text()` / `open()` calls on MEMORIES_DIR/SKILLS_DIR** — `goals_tracker`, `goal_project_agent`, `email_scanner`, `commitment_tracker`, `contact_tracker`, `project_inference_scanner`, `skill_optimizer` now use `read_text_with_retry`.
  7. **`skill_optimizer` used fragile string-split fence extraction** — replaced both instances with the standard `re.sub()` pair used by all other JSON-parsing modules.
- **Test date staleness** — EDEADLK briefing regression test used a hardcoded `2026-04-22` calendar event date that became yesterday on next run; now uses `date.today()`.

## [1.6.5] — 2026-04-22

### Fixed
- **`/briefing` and notification-manager alert loops now tolerate iCloud EDEADLK on memory file reads** — v1.6.4 only plugged the `config.yaml` read. `_assemble_briefing` (plus `_check_commitment_alerts`, `_check_goal_alerts`, `_check_project_alerts`, `_check_pre_meeting_alerts`, `_check_calendar_staleness`, `_prune_sent_alerts`, and `_assemble_pre_meeting_context`) still did raw `f.read_text()` on 14 distinct memory-file loops. A single EDEADLK in any of them raised straight out of the handler, so `/briefing` continued to return "internal error" even after the v1.6.4 deploy. Added `utils.read_text_with_retry()` — a generic iCloud-resilient reader (3× retry on errno 11, returns `""` on exhaustion so callers' existing `_parse_frontmatter`/`if fm.get(...)` guards skip unreadable files naturally). Every `f.read_text()` / `path.read_text(encoding="utf-8")` in `notification_manager.py` that targets an iCloud memory file now goes through the helper. Regression test asserts `_assemble_briefing()` completes without raising when one memory file raises `OSError(11)` on every read.

## [1.6.4] — 2026-04-22

### Fixed
- **Telegram commands no longer crash on iCloud `config.yaml` EDEADLK** — every Telegram command routes through `_check_auth` → `notification_manager.get_chat_id()` → `_load_config()`, which did a raw `CONFIG_PATH.read_text()` on the iCloud-backed `config.yaml`. When iCloud was materializing the placeholder, the read raised `OSError(11, 'Resource deadlock avoided')` straight out of the handler and Telegram got no reply. Observed symptom: `/briefing` silently failing (and by extension, every other command during an iCloud sync window). Added `utils.load_config()` — a shared iCloud-resilient loader that retries EDEADLK up to 3× with short backoffs, caches parsed YAML by mtime so repeat command invocations don't re-read iCloud, and falls back to the last known-good cached value if all retries fail. Rewired `notification_manager._load_config()`, `chat_handler.__init__`, `chat_handler._get_display_config`, `cmd_skiplist`, and the `/goal add` category validator to use it. Added three regression tests covering retry-then-succeed, persistent-EDEADLK fallback-to-cache, and mtime-based caching.
- **Calendar scanner now strips `` ```json `` fences before parsing LLM output** — `_generate_summary_and_tags` was the only LLM-calling scanner without the fence-strip pattern used everywhere else (commitment_tracker, slack_scanner, zoom_scanner, project_inference_scanner, goal_project_agent). Haiku reliably wraps JSON in markdown fences, so every event hit `json.JSONDecodeError`, logged "LLM returned invalid JSON for event: …", and got written with `summary: ""` + `tags: []`. Discovered immediately after the EventKit Add-Only fix landed and the worker started seeing real events for the first time.

## [1.6.3] — 2026-04-22

### Fixed
- **EventKit "Add Only" grant now rejected at init, not silently tolerated** — on macOS Sonoma+, Calendar authorization split into two levels: `EKAuthorizationStatusWriteOnly` (4, "Add Only") and `EKAuthorizationStatusFullAccess` (5). The Add Only grant silently returns `[]` from every `eventsMatchingPredicate_` call with no error, so the daemon would happily log "0 events" forever. `EventKitSource.create()` previously accepted `status in (3, 4)` — treating Add Only as equivalent to legacy Authorized. This was the root cause of the watcher-laptop regression discovered 2026-04-22: Calendar.app listed 11 calendars, EventKit saw only 1, and no events were ever written despite the v1.6.0 zero-event WARNING firing every 5 minutes. The acceptance set is now `(3, 5)`, and status 4 triggers a loud WARNING telling the user to grant Full Access explicitly (with the `tccutil reset Calendar` hint). Additionally, after a successful `requestFullAccessToEventsWithCompletion_` prompt, the post-grant status is verified to be Full Access rather than Add Only (the latter is the default button in the consent dialog). Added three regression tests covering WriteOnly rejection, FullAccess acceptance, and legacy Authorized acceptance.

## [1.6.2] — 2026-04-22

Cleanup release for the second-wave fallout of the hostname-stacking bug
class identified in v1.6.0. On the live daemon, `code_scanner.py`'s
over-broad filename migration had been silently stealing
`project-candidate-*.md` files from `project_inference_scanner.py` and
mangling 474 of them with stacked hostname prefixes; simultaneously, the
migration loops were crashing on iCloud `EDEADLK` placeholder-read errors
and spamming `Code migration failed` in error.log.

### Fixed
- **Project-candidate filenames unmangled via one-shot migration** — `project_inference_scanner.py` now runs `_unmangle_candidate_filenames` at `__init__`, sentinel-gated by `.project-candidate-unmangle-v1.done` in `DEPLOY_DIR`. It matches the `project-*-candidate-*.md` glob, extracts the canonical `candidate-{slug}-{id}` tail via regex, and renames each file back to `project-{tail}.md`. When two mangled copies of the same candidate exist, the winner is picked by `status` (`confirmed`/`rejected` beats `pending`), then `created` timestamp, then mtime. `OSError` with errno `EDEADLK`/`EAGAIN` is treated as transient: the file is skipped and the sentinel is NOT stamped, so the migration re-attempts on the next boot. Production state: 474 mangled files expected to collapse back to canonical form on first boot post-deploy.
- **`code_scanner` migrations scoped strictly to `type: code`/`category: code`** — `_migrate_project_filenames` previously matched any file whose name started with `project-`, which is how it stole `project-candidate-*.md` files from `project_inference_scanner`. It now requires the frontmatter to carry BOTH `type: project` AND `category: code` before touching a filename. All three code-scanner filename migrations (`_migrate_legacy_code_project_files`, `_migrate_project_filenames`, `_migrate_project_to_code_files`) are now sentinel-gated by `.code-*-v2.done` files in `DEPLOY_DIR`, so they run exactly once per install and can't re-enter on a hostname flip.
- **iCloud `EDEADLK` no longer crashes code-scanner migration** — the migration loops now catch `OSError` with errno `EDEADLK`/`EAGAIN` as a transient iCloud placeholder-read deadlock, log at DEBUG, and skip the file. When any transient error occurs during a pass, the sentinel is NOT stamped so the migration will re-attempt cleanly on the next boot. This eliminates the "Code migration failed / Resource deadlock avoided" spam that had been flooding `error.log`.
- **Test isolation: autouse fixture now redirects `MEMORIES_DIR` as well as `STATE_FILE`** — v1.6.0's fixture only isolated `STATE_FILE`. `test_filename_format` patched `_hostname="test-host"` but not `MEMORIES_DIR`, so a pytest run under v1.6.1's (now-working) migration renamed 27 real calendar-event files in iCloud to a `test-host-` prefix. Autouse fixture now redirects both; added a meta-test that snapshots real production `MEMORIES_DIR` calendar-event filenames+mtimes and asserts no mutation after `CalendarScanner()` is constructed under `_hostname="test-host"`. Equivalent autouse fixtures and meta-tests added for `test_code_scanner.py` and `test_project_inference_scanner.py`.

## [1.6.1] — 2026-04-22

### Fixed
- **Calendar migration silently skipped stacked files on APFS** — the v1.6.0 migration called `_stamp_hostname_in_frontmatter` on the stacked path before renaming it to canonical form. That helper writes a `.md.tmp` sibling whose filename is 4 chars longer than the source, so any stacked filename ≥252 chars pushed the `.tmp` sibling past the APFS 255-byte per-component limit → `OSError(63, 'File name too long')` → swallowed by the `except (OSError, FileNotFoundError): pass` block. Result: on the live daemon, 11 of 12 stacked files remained untouched after the v1.6.0 deploy (only the one sub-252-char stacked file was stamped, and even it failed to rename). The cleanup now renames to the canonical path FIRST (which shortens the name well under the limit) and then stamps the hostname into frontmatter. Added a regression test covering a 252+ char stacked stem without `hostname` in frontmatter.

## [1.6.0] — 2026-04-21

Calendar ingestion reliability release. Fixes a ~10-day silent outage on the
live daemon caused by three compounding bugs in `calendar_scanner.py` and adds
a staleness alert so similar outages become visible within 24 hours.

### Added
- **Calendar staleness alert in Notification Manager** — new `_check_calendar_staleness` fires a Telegram warning when no `calendar-event-*.md` file has been written in more than 24 hours, so a silent outage like the 10-day calendar ingestion failure in April 2026 is visible the next morning instead of going undetected. Dedup keyed by local date so at most one alert fires per day; decays after 7 days via `_prune_sent_alerts`. Uses the standard state-before-send pattern with rollback on send failure.

### Fixed
- **EventKit zero-event scans now logged at WARNING with diagnostics** — a predicate returning zero events was previously indistinguishable from a successful empty window. `EventKitSource.get_events` now enumerates visible calendars and logs a WARNING that includes both the window boundaries and the calendar count when zero events are returned, so silent failures (partial grant, predicate filtering all calendars, etc.) are visible in the log stream instead of hiding behind an INFO line. The `Calendar data source: EventKit` detect log line now also reports the calendar count.
- **Calendar SQLite path and schema probe** — `CalendarCacheSource._find_db_path` now prefers `~/Library/Calendars/Calendar.sqlitedb` (the actual filename on modern macOS) ahead of the legacy `Calendar Cache` names, restoring SQLite as the primary path per the CLAUDE.md precedence spec. `CalendarCacheSource.create()` additionally probes for the expected `ZCALENDARITEM` table via a cheap read-only `sqlite_master` query and returns `None` when absent, so `CalendarDataSource.detect` cleanly falls through to EventKit on modern macOS builds that use a different schema rather than failing at query time.
- **Calendar scanner: hostname stacking in filenames and state keys** — `_migrate_calendar_filenames` previously re-ran on every `__init__` and used a fragile `startswith(f"{my_hostname}-")` idempotency check. Because `socket.gethostname()` is not stable on macOS (it flips between values like `Chriss-Air` and `Chriss-MacBook-Air` depending on network state), the check repeatedly missed and re-prefixed already-migrated files, producing stems with 6–7 stacked hostname segments. State keys drifted out of sync with on-disk filenames, causing the scanner to treat every event as new, silently failing to write memory files for ~10 days on the live daemon. The migration is now a one-shot cleanup gated by a `.calendar-migration-hostname-v2.done` sentinel next to `calendar-scanner-state.json`: it extracts the canonical `YYYY-MM-DD-*` tail via regex, trusts the frontmatter `hostname` field as authoritative (stamping it when absent via atomic write), deletes stacked duplicates when a canonical file already exists, and remaps state keys to canonical form. Additionally, `_load_state` now prunes any state key whose hostname prefix has a duplicated token (stacking fingerprint).

## [1.5.0] — 2026-04-19

Security release closing 19 of 20 findings from `specs/security-scan.md` (5 Critical, 4 High, 9 Medium; M3 N/A). See the spec for per-finding commit hashes.

### Fixed
- **Install: preserve existing plist secrets on non-interactive deploy** — `install.sh` no longer overwrites existing secrets when prompts receive empty input; added `NONINTERACTIVE=1` env var for scripted deploys; prevents the Phase 3 deploy mishap where piped blank responses wiped SLACK_USER_TOKEN, GITHUB_PAT, and GEMINI_API_KEY
- **Security (H4) Bound seen_urls to 50k entries** — `browser_watcher.py` `seen_urls` is now a dict (insertion-order) capped at 50,000 entries with FIFO eviction on save; prevents unbounded memory/disk growth after a year of active browsing
- **Security (M1) Pin exact dependency versions and commit lockfile** — `requirements.txt` and `requirements-dev.txt` now use `==` pins matching a new `requirements.lock`; protects against supply-chain attacks via compromised transitive upgrades of `litellm`, `python-telegram-bot`, `httpx`, etc.
- **Security (M2) Demote query logging** — `chat_handler.py` logs only a hash prefix and length at INFO (`hash=... len=...`); full query text only appears at DEBUG; prevents sensitive user queries leaking to shipped log files
- **Security (M4) Exclusive flock on YAML config writes** — `chat_handler.py` `_edit_skip_domains` now acquires `fcntl.LOCK_EX` before the read-modify-write of `config.yaml`; prevents lost updates from concurrent `/skip` or `/unskip` commands
- **Security (M5) SHA-256 commitment IDs** — `commitment_tracker.py` switched ID generation from SHA-1 to SHA-256 (truncated to 12 chars); SHA-1 is deprecated; existing commitment files keep old IDs (opaque, used for dedup only)
- **Security (M7) Log swallowed exceptions** — several `except Exception: pass` blocks in `email_scanner.py` now emit `log.debug(..., exc_info=True)` so tmp-file cleanup failures and state-load errors are diagnosable instead of silent
- **Security (M8) Filter iCloud conflict-copy files** — new `utils.is_conflict_copy()` + `glob_memories()` wrapper; memory loaders now ignore `*(Mac's conflicted copy)*.md` and similar filenames that iCloud creates on cross-device write races
- **Security (M9) Token-aware chat context budget** — `chat_handler.py` context assembly switched from a character budget (80k chars) to a token budget (150k tokens) via `litellm.token_counter()`; falls back to char/4 heuristic if the counter raises; prevents near-context-limit overflow
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
