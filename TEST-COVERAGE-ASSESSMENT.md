# Test Coverage Assessment — secondbrain

Assessor: Claude Code (claude-sonnet-4-6)
Date: 2026-05-01 (initial assessment 2026-04-30; updated after Gap 1 and Gap 2 remediation)
Scope: Full repo — all modules reachable from daemon.py, all tests under tests/
Project type: Personal backend daemon / data pipeline (15 async loops, Telegram bot interface, no external consumers)
Evidence access: Full repo on main branch (755d9e7); 1626 tests run after remediation (1 pre-existing date-sensitive failure in test_related_memories_recency_filter, unrelated to remediation)
Confidence: High

## Executive summary

The test suite is well above average for a personal project of this complexity. Near-complete unit test coverage spans all 15 scanner modules, the Telegram bot handler, memory infrastructure, LLM routing, and — unusually — the deployment contract itself (install manifest guard, requirements manifest, watcher role package isolation). The installation integrity tests are particularly sophisticated: they statically trace the import graph from `daemon.py` and enforce that every reachable module is listed in `install.sh`'s `DAEMON_FILES` array, preventing an entire class of deploy-time `ModuleNotFoundError` regressions. The `test_e2e_registry_coverage.py` test enforces that every `COMMAND_REGISTRY` entry has a smoke test, making the command surface self-governing.

The primary gap was assertion depth in the integration (e2e) layer. **Gap 1 and Gap 2 have been remediated** (2026-05-01): 15 content-assertion tests added in `test_content_assertions.py` verify reply text for `/commitments`, `/complete`, `/goals`, and `/features`; 19 frontmatter contract tests added in `test_frontmatter_contract.py` verify writer→reader field compatibility across 5 source types. The contract tests also uncovered and fixed two latent bugs in `seed.py`: (1) `calendar_event` used `start:` instead of `start_time:`, silently breaking pre-meeting notifications and `/event` detail in tests; (2) unquoted ISO datetime strings in YAML frontmatter were parsed as `datetime` objects rather than strings, causing `[:10]` slicing in `cmd_features` to raise `TypeError`. Both bugs are now fixed in the seed. The remaining gaps are: no `pip audit` / dependency scanning, no daemon lifecycle test.

**Overall verdict:** partial

**Release recommendation:** ship with documented risks (Gap 3 CI is user-preference local-only; dependency scanning is backlog)

---

## Per-level findings

### Unit

- **What exists:** 51 unit test files covering every production module except `quota_scrapers.py` and `migrate_memories.py` (both intentionally excluded — the former is a disabled-by-default scraper with an explicit ToS warning; the latter is a one-shot migration script listed in `DO_NOT_DEPLOY`). Standout coverage: `test_chat_handler.py` (348 tests) covers timeout handling, unauthorized access rejection, 4096-char chunking, history token trimming, reaction emoji state machine, and the COMMAND_REGISTRY completeness assertion. `test_memory_writer.py` tests URL canonicalization (tracking-param stripping, hash stability across utm variants), atomic write (no `.tmp` files), frontmatter field ordering, and fall-through behavior when title or tags are absent. `test_secrets.py` tests the Keychain path, env fallback, caching, and subprocess failure modes (timeout, FileNotFoundError). Infrastructure guard tests (`test_install_manifest.py`, `test_requirements_manifest.py`, `test_watcher_role_packages.py`) statically verify deployment invariants via AST import-graph analysis.
- **What is missing:** `quota_scrapers.py` (47 lines, scraping logic for Claude.ai Pro and ChatGPT Plus UI) has no tests. The scraper is disabled by default and carries a ToS warning, so absent tests are an acceptable risk but not ideal. `migrate_memories.py` is similarly untested and documented as a deliberate choice.
- **What is broken:** No structurally broken tests observed. Assertion strength is high in the modules sampled (specific field-by-field YAML frontmatter checks, not `assert result is not None`).
- **Verdict:** sufficient

### Integration

- **What exists:** `tests/integration/test_pipeline.py` (25 tests) runs the real executor → memory_writer → file-on-disk path with mocked LLM responses, verifying YAML frontmatter structure, atomic writes, and duplicate-URL deduplication. `tests/integration/test_chat_context.py` (10 tests) wires `TelegramChatHandler` with a real `MemoryCache` backed by SQLite, verifying context assembly, relevance ordering, and budget exhaustion. `test_e2e_*.py` (20 files, ~110 tests) provides command smoke coverage — every entry in `COMMAND_REGISTRY` has at minimum a smoke test via the `test_e2e_registry_coverage.py` enforcement mechanism. A shared `seed.py` helper creates realistic memory files (commitments, goals, features, contacts) so tests exercise real frontmatter parsing.
- **What is missing:** Approximately 75% of smoke tests assert only `reply_text.assert_called()` — that a reply was sent — without inspecting content. A handler that returns static "OK" text for any input would pass. Specific untested behaviors: `/commitments` output format when items have owners and due dates; `/features` list ordering by priority; `/briefing` content when all sections are empty; error message text when an argument is invalid (e.g., `/feature 999` with no such feature). No test covers the multi-role scenario (watcher node writes memory via iCloud sync → full node reads it). No test covers the heartbeat → `/status` data flow across simulated machine boundaries.
- **What is broken:** The smoke tests are intentionally shallow (their name signals this). They are not broken, but their coverage claim ("e2e") overstates what they verify — they are integration tests against `TelegramChatHandler` directly, not against a live Telegram API.
- **Verdict:** partial

### Contract

- **What exists:** The deployment contract is verified rigorously: import-graph analysis enforces DAEMON_FILES completeness; requirements.txt pin coverage is statically verified; watcher-role package isolation is verified. The command contract is enforced via `test_e2e_registry_coverage.py` (every COMMAND_REGISTRY command must have a smoke test). These are strong deployment-contract tests uncommon at this project scale.
- **What is missing:** The memory file frontmatter schema is the system's central data contract — scanners write YAML frontmatter with typed fields, and downstream consumers (commitment_tracker, contact_tracker, notification_manager, index_builder) read specific fields. No test verifies: (a) that an email_scanner output with all expected fields is readable by commitment_tracker without KeyError; (b) that a zoom_scanner meeting file without `participants:` is handled gracefully by contact_tracker; (c) that `_safe_frontmatter()` in notification_manager correctly handles all variants the scanners emit. This contract is exercised incidentally through integration smoke tests but is not verified as a formal invariant.
- **What is broken:** Nothing broken — the gap is absence, not incorrectness.
- **Verdict:** partial

### End-to-end (E2E)

- **What exists:** None in the strict sense — no test starts the daemon, connects to a real Telegram API, and sends messages. The `test_e2e_*.py` files are integration tests that exercise `TelegramChatHandler` directly.
- **What is missing:** Full E2E would require a live bot token, a real LLM API key, and a running daemon instance.
- **Verdict:** not_applicable — personal single-user daemon; no automated E2E is practical given the live Telegram API + LLM cost requirements. Manual smoke testing against the deployed daemon is the appropriate verification path. The integration smoke tests cover the testable portion of this surface.

### UX / user journey

- **What exists:** None.
- **What is missing:** Documented user journeys (e.g., "user asks about a meeting from last week → bot retrieves relevant memory → response cites specific details") would allow verifying LLM response quality beyond "a response was sent."
- **Verdict:** not_applicable — Telegram bot UI; user journey quality depends on LLM behavior, which is non-deterministic and verified by the skill optimizer's LLM-as-judge loop in production, not by automated tests. No practical automated test strategy applies here.

### Performance

- **What exists:** None.
- **What is missing:** No documented latency SLOs; no load tests.
- **Verdict:** not_applicable — single-instance personal daemon with no concurrent users; each scanner loop runs on a 5-minute cadence and is self-throttling. No user-observable SLOs are defined. Async loop timing is observable via heartbeat-{hostname}.json and `/status` in production.

### Security

- **What exists:** `test_secrets.py` (9 tests) verifies the macOS Keychain secrets subsystem: successful retrieval, miss handling, caching, subprocess failure modes (timeout, FileNotFoundError), env-var fallback priority. `test_chat_handler.py` includes `test_handle_message_ignores_unauthorised_user` verifying that the bot rejects Telegram messages from any user ID that isn't the configured `allowed_user_id`. No API keys, bot tokens, or real credentials appear in the repository (config.yaml is iCloud-only, not committed).
- **What is missing:** No dependency scanning (no Dependabot, no `pip audit` in CI — and there is no CI). The daemon processes untrusted external data (email bodies, web page content, Slack messages) and pipes it to LLMs and writes it to files; a malicious email body could potentially craft content that, when included in an LLM prompt, causes unexpected behavior (prompt injection). No test verifies that the email/Slack/web content sanitization prevents such inputs from affecting system behavior. No SAST tooling is configured.
- **What is broken:** Nothing broken.
- **Verdict:** partial — AuthN is tested; secrets subsystem is tested; no supply-chain scanning exists.

### Accessibility

- **Verdict:** not_applicable — Telegram bot with no custom web UI; accessibility is delegated entirely to the Telegram client application.

---

## Anti-patterns observed

- **Invocation-only assertions in ~75% of e2e smoke tests** (`update.message.reply_text.assert_called()` without content check). File pattern: `tests/integration/test_e2e_*.py`. These tests confirm the handler doesn't crash, not that it returns useful output. This is a deliberate trade-off (easy to maintain, catches regressions that raise exceptions) but limits what the integration layer actually validates.
- **"E2E" naming for integration tests** — `test_e2e_*.py` don't touch a live Telegram API; they're integration tests against `TelegramChatHandler`. Not a quality problem, but the naming could mislead someone assessing whether E2E coverage exists.
- **No CI** — Tests must be run manually before every commit (per CLAUDE.md). The `pytest` pass is enforced by convention, not automation. A pre-commit hook or GitHub Actions workflow would eliminate the risk of a missed run.

---

## Prioritized gap list

| # | Gap statement | Level | Priority | Status |
|---|---|---|---|---|
| 1 | ~75% of e2e smoke tests assert only `reply_text.assert_called()` — a handler that always returns "OK" would pass. Specific untested behaviors: `/commitments` formatting, `/complete` error path, `/goals` list content, `/features` status/priority tags. | Integration | P1 | ✅ **Remediated 2026-05-01** — `tests/integration/test_content_assertions.py` (15 tests) |
| 2 | Frontmatter schema contract between writers (scanners) and readers (trackers, notification_manager) untested. Schema drift causes silent KeyErrors in production. The fix also uncovered and corrected `seed.calendar_event` using `start:` instead of `start_time:` (silent test bug). | Contract | P1 | ✅ **Remediated 2026-05-01** — `tests/integration/test_frontmatter_contract.py` (19 tests); `seed.py` fixed to use `yaml.dump` for frontmatter (prevents YAML parse bugs on datetime strings and colon-containing titles) |
| 3 | No automated CI runs the test suite. "Run pytest before every commit" is an honor-system convention. Risk: a failing commit reaches main if the author forgets. | Integration | P1 | ⚙️ **User preference: local-only CI** — suggested approach: pre-commit hook (`echo '#!/bin/sh\npytest -q' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`) to gate every commit automatically without a remote CI service. |
| 4 | No daemon lifecycle test verifies all 15 loops start cleanly and shut down on SIGTERM. `test_daemon.py` (3 tests) scope is narrow. | Integration | P2 | Open |
| 5 | No dependency scanning (`pip audit` or Dependabot). Known CVEs in `litellm`, `python-telegram-bot`, or `httpx` would be invisible. | Security | P2 | Open — run `pip audit` locally or add to pre-commit hook |
| 6 | `quota_scrapers.py` (47 lines, disabled by default) has no tests. | Unit | P3 | Open — acceptable risk given ToS-warning status |

---

## Recommendations

1. **Add content assertions to 5–10 key smoke tests (P1)** — Pick the commands most likely to silently regress: `/commitments`, `/features`, `/goals`, `/briefing`, `/complete <N>` error path. Assert the reply text contains expected substrings. Expected outcome: regressions in formatting and content are caught before deploy, not discovered via manual Telegram testing.

2. **Add a frontmatter schema contract test (P1)** — Write one parameterized test that covers all (writer, reader) pairs. Use the existing `seed.py` patterns to generate minimal memory files and assert readers handle them without KeyError. Expected outcome: schema drift between a scanner update and its downstream consumer is caught in CI.

3. **Add a GitHub Actions CI workflow (P1)** — One `.github/workflows/test.yml` that installs dev deps and runs `pytest`. No secrets required. Expected outcome: every PR and push is validated; manual "run pytest before committing" becomes a backup, not the primary safety net.

4. **Add a daemon lifecycle integration test (P2)** — Verify that all 15 scanner constructors succeed and that `asyncio.gather` with an immediate `stop_event` exits cleanly. Expected outcome: new scanners with broken constructors fail in test, not at deploy time.

5. **Enable `pip audit` in CI (P2)** — Once CI exists, add a `pip audit` step after `pip install`. Expected outcome: known CVEs in dependencies surface before they reach the deployed daemon.

---

## Source limitations

- No CI history available (no `.github/workflows/`); test pass rate over time is unknown.
- No coverage report (`.coveragerc` absent; `pytest-cov` not in `requirements-dev.txt`). Line coverage figures are unavailable — assessment is based on behavioral coverage inferred from test file inspection.
- `test_daemon.py` (3 tests) was not read in detail — its exact scope is unverified.
- Integration tests were sampled, not exhaustively read. A small number of smoke tests may have deeper assertions than the majority pattern observed.
- Assessment is against the `main` branch as of 2026-04-30 (commit 755d9e7).

## Out of scope

- LLM output quality (skill optimizer handles this in production)
- iCloud sync reliability and latency
- macOS Keychain and Full Disk Access permission behavior in production
- Manual Telegram bot smoke testing against the live deployed daemon
