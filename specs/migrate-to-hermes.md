# Second Brain → Hermes Migration Design

**Date:** 2026-06-24  
**Status:** Draft — pre-implementation  
**Hermes version:** 0.17.0 ("The Reach Release")  
**Scope:** Full system analysis and phased migration roadmap

---

## 1. Executive Summary

Migrating Second Brain to Nous Research's Hermes agent platform is **viable but not a clean swap**. Hermes replaces roughly half the system natively (chat, channels, scheduling, skills, provider routing, skill self-improvement), but the scanner infrastructure and the 3,500-file typed memory corpus require a **hybrid approach** — Hermes as the agent/interaction brain, with scanners continuing to write to iCloud and a custom memory-read plugin bridging the corpus to Hermes.

**Recommendation: Hybrid migration over full replacement.**

A full migration — replacing the iCloud corpus with Hermes's native memory — is architecturally unsound: Hermes's built-in memory caps at ~3,575 characters total (MEMORY.md + USER.md), while the corpus holds ~3,500 rich YAML-frontmatter documents averaging 3–6 KB each. The typed-frontmatter contract (15+ distinct `type:` values consumed by trackers) cannot survive compression into plain-text memory slots. Additionally, scanner reliability (Full Disk Access, EventKit, AppleScript, multi-machine iCloud convergence) is genuinely difficult to guarantee inside an agent runtime and offers no gain.

What the hybrid achieves:
- Eliminates `chat_handler.py` (7,599 lines) — the project's largest maintenance burden
- Eliminates `daemon.py`, `transport.py`, `slack_adapter.py`, `llm_routes.py`, `skill_executor.py`, `notification_manager.py`, `report_scheduler.py`, `skill_optimizer.py` — replaced by Hermes primitives
- Retains the iCloud scanner pipeline (proven, multi-machine, OS-level access)
- Gains iMessage, WhatsApp, Discord, Signal channels at zero additional cost
- Gains Hermes's built-in skill self-improvement loop (replaces the custom skill_optimizer)

---

## 2. Capability Mapping

### 2.1 Replace — Native Hermes Equivalents

These Second Brain components have direct Hermes replacements and can be deleted once Hermes is live.

| Second Brain | Lines | Hermes Equivalent | Notes |
|---|---|---|---|
| `chat_handler.py` | 7,599 | Hermes gateway + channel loop | 130+ slash commands become Hermes slash commands |
| `transport.py` | ~200 | Hermes gateway | Telegram/Slack abstraction built-in |
| `slack_adapter.py` | ~150 | Hermes gateway (Slack channel) | Native Slack support |
| `skill_executor.py` | 452 | Hermes skills system | `.md` skills → agentskills.io SKILL.md format |
| `llm_routes.py` | ~80 | Hermes provider routing | `hermes model` + fallback config |
| `notification_manager.py` | 1,380 | Hermes cron scheduler + channels | Natural language scheduling; push to any channel |
| `report_scheduler.py` | 676 | Hermes cron scheduler | Scheduled skills with `delegate_task` |
| `skill_optimizer.py` | 1,463 | Hermes learning loop | Built-in LLM-as-judge + skill rewrite; replaces all 1,463 lines |
| `index_builder.py` | ~300 | Scheduled Hermes skill | Nightly skill reading corpus → MEMORY.md digest |
| `daemon.py` | 339 | Hermes daemon | `hermes gateway start` replaces launchd orchestration |
| `secrets.py` | ~50 | Hermes credential system | macOS Keychain integration built-in |
| `usage_tracker.py` | ~100 | Hermes built-in usage tracking | Native per-model cost tracking |
| `quota_scanner.py` | ~200 | Hermes usage tracking | Hermes tracks quota natively |
| **skills/*.md** | 15 files | agentskills.io SKILL.md | Format migration required (see §4.1) |

**Channels gained for free:**  
iMessage (v0.17), WhatsApp Business, Discord, Signal — no Second Brain equivalent exists today.

### 2.2 Rebuild — Custom Hermes Plugins

These are the differentiated value of Second Brain. They must be ported as Hermes plugins (`~/.hermes/plugins/`), wrapping the existing Python capture logic. The plugin API is Python-based: `ctx.register_tool(name, schema, handler)`. When running locally on macOS (non-Docker), plugins can import any Python library — sqlite3, subprocess (AppleScript), pyobjc (EventKit) — with no additional sandbox restrictions.

Each scanner becomes a **scheduled plugin** (Hermes cron) that still writes `.md` files to the iCloud corpus. The capture path is unchanged; only the orchestration layer moves.

| Second Brain | Lines | Hermes Plugin | Key Dependencies |
|---|---|---|---|
| `browser_watcher.py` | ~400 | `plugin: browser-watcher` | Chrome/Firefox SQLite, `content_fetcher.py` |
| `email_scanner.py` | 1,036 | `plugin: email-scanner` | Mail.app SQLite, AppleScript fallback, Full Disk Access |
| `calendar_scanner.py` | 1,217 | `plugin: calendar-scanner` | EventKit (pyobjc or subprocess), AppleScript |
| `notes_scanner.py` | ~400 | `plugin: notes-scanner` | AppleScript |
| `zoom_scanner.py` | 1,237 | `plugin: zoom-scanner` | Zoom Server-to-Server OAuth API |
| `code_scanner.py` | 892 | `plugin: code-scanner` | `git` CLI, README reading |
| `slack_scanner.py` | 624 | `plugin: slack-scanner` | Slack Web API (`xoxp-` token) |
| `commitment_tracker.py` | 585 | `plugin: commitment-tracker` | Reads corpus via `corpus-reader` tool |
| `contact_tracker.py` | 418 | `plugin: contact-tracker` | Reads corpus via `corpus-reader` tool |
| `project_inference_scanner.py` | 639 | `plugin: project-inference` | Reads corpus via `corpus-reader` tool |
| `goal_project_agent.py` | 886 | Hermes skill + `delegate_task` | Reads corpus; subagent delegation replaces custom loop |
| `memory_writer.py` | ~200 | Shared plugin utility | Atomic iCloud write logic, reused by all scanner plugins |
| `corpus_reader` | (new) | `plugin: corpus-reader` | MemoryCache wrapper; primary Hermes tool for reading corpus |

**`corpus-reader` is the linchpin plugin.** It exposes the existing `MemoryCache` SQLite index to Hermes as first-class tools: `query_memories(type, keywords, limit)`, `get_memory(filename)`, `search_memories(fts_query)`. Every Hermes skill that today would read `MEMORIES_DIR` instead calls `corpus-reader`. This preserves the typed-frontmatter contract and the EDEADLK resilience of MemoryCache without rewriting anything.

### 2.3 Drop / Made Obsolete

These exist solely to serve the current architecture and disappear in the hybrid migration.

| Component | Why It Disappears |
|---|---|
| `daemon.py` async gather loop | Hermes daemon owns the event loop |
| `utils.py` EDEADLK retry wrappers | Still needed inside scanner plugins; but the module-level export is dead |
| Full/watcher role split in `daemon.py` | Replaced by per-machine plugin selection + `hermes cron tick` on laptops (see §3.5) |
| launchd plist orchestration | Full node: `hermes gateway start`. Laptops: launchd calls `hermes cron tick` every 5 min |
| `heartbeat.py` | Hermes has a built-in `/status` equivalent |
| `llm_chat_importer.py` | Hermes maintains conversation history natively (FTS5 session search) |
| `dedup_checker.py` | Integrate into corpus-reader plugin or drop entirely |

---

## 3. Memory Corpus Migration

This is the most critical architectural decision. The Second Brain has ~3,500 typed `.md` files in iCloud with a strict YAML-frontmatter contract. Hermes's native memory (MEMORY.md: 2,200 chars + USER.md: 1,375 chars) holds roughly 15–25 plain-text entries total. The corpus **cannot live in native Hermes memory.**

### Option A: Keep iCloud, expose via `corpus-reader` plugin (Recommended)

- Scanners continue writing to `~/Library/Mobile Documents/.../memories/`
- `corpus-reader` plugin wraps MemoryCache SQLite for Hermes skill reads
- External memory backend (Mem0 or RetainDB) optionally indexes the corpus for semantic search
- Zero data migration; frontmatter contract fully preserved
- Multi-machine iCloud sync continues working

**Trade-off:** Hermes skills must call `corpus-reader` instead of reading MEMORY.md — slightly more explicit, but more powerful (typed queries, keyword scoring, frontmatter access).

### Option B: Migrate to Hermes session search (Not Recommended)

- Import all 3,500 files into `~/.hermes/state.db` as synthetic sessions
- FTS5 search becomes the query path
- **Problems:** Typed frontmatter is lost (stored as plain text), no `type:` filtering, no `status:` lifecycle, no mtime-based change detection, no multi-machine convergence, one-time migration with ongoing friction

### Option C: External memory backend (Future Phase)

- Point Mem0, Honcho, or RetainDB at the iCloud corpus
- These backends run alongside Hermes and can theoretically index arbitrary files
- Hermes docs don't confirm whether existing corpora can be imported (requires direct API exploration)
- Treat as a future Phase 4 investigation after the core migration proves out

**Decision: Implement Option A now. Evaluate Option C in Phase 4.**

---

## 3.5 Multi-Machine Architecture

*This section was added after researching Hermes's cron and gateway mechanics.*

### How Hermes cron actually works

Hermes's cron jobs are fired by the **gateway's background ticker thread**, which ticks every 60 seconds. A regular CLI session does not fire cron jobs automatically. The two operational modes are:

- `hermes gateway start` — installs a persistent launchd/systemd service; ticker runs continuously
- `hermes cron tick` — executes all due jobs once and exits; safe to call from an external scheduler

Critically: **two gateway instances conflict via file-based locking** — if two machines both run `hermes gateway start` against the same `HERMES_HOME`, jobs will be delayed or skipped. This rules out the naive approach of running a full Hermes gateway on every laptop.

### Recommended topology

```
Full node (Mac Studio/Mini)
├── hermes gateway start          ← one gateway, one ticker
├── corpus-reader plugin          ← MemoryCache SQLite read path
├── tracker plugins               ← commitment, contact, project, goal
└── iCloud corpus (read + write)  ← convergence point

Laptop 1, Laptop 2, ...
├── launchd → hermes cron tick (every 5 min)   ← no gateway
├── scanner plugins only          ← browser, email, calendar, notes, code, slack
├── per-machine state files       ← ~/secondbrain/*.json  (local, not synced)
└── iCloud corpus (write only)    ← files sync to full node automatically
```

Each laptop has its own `~/.hermes/` with only scanner plugins installed. There is no Hermes gateway on laptops — `hermes cron tick` is called by a launchd plist on the same 5-minute cadence that `daemon.py` uses today. The ticker fires due scanner jobs, writes `.md` files to iCloud, and exits. iCloud sync carries those files to the full node, where `corpus-reader` picks them up via the 60-second MemoryCache sweep.

### Comparison to current watcher-role architecture

| Concern | Current (daemon.py) | Hermes migration |
|---|---|---|
| Orchestration on full node | `asyncio.gather()` of 16 loops | `hermes gateway start` |
| Orchestration on laptops | `asyncio.gather()` of 6 loops | launchd → `hermes cron tick` every 5 min |
| Capture convergence | iCloud sync | iCloud sync (unchanged) |
| State files | `~/secondbrain/*.json` per machine | `~/secondbrain/*.json` per machine (unchanged) |
| Hostname-scoped filenames | `code-{hostname}-*.md`, etc. | Unchanged — scanner plugins set hostname |
| Full Disk Access / AppleScript | Granted to Python process | Must be granted to Hermes process (or plugin subprocess) |
| Package isolation (no telegram-bot on laptops) | Role check in daemon.py imports | Natural — tracker plugins simply aren't installed on laptops |

### What changes on each machine type

**Full node** — install Hermes fully, run `hermes gateway start` as a launchd service. Install all plugins. Remove old launchd plist for `daemon.py`.

**Laptops** — install Hermes (no gateway). Install scanner plugins only. Replace the `daemon.py` launchd plist with a new plist that runs `hermes cron tick` every 5 minutes. No Telegram token needed on laptops.

### Open risk: `hermes cron tick` reliability

`hermes cron tick` is documented as a one-shot executor for testing. It is unclear whether it is intended as a production scheduling path or whether it has edge cases (e.g., what happens if a previous tick is still running when the next fires). This must be validated during Phase 1 before relying on it for scanner cadence. Fallback if unreliable: keep a thin `scanner_daemon.py` (the 6 capture loops only, no telegram-bot dependency) on laptops as a permanent feeder process — this preserves the scanner isolation benefit while still eliminating `daemon.py` from the full node.

---

## 4. Migration Phasing

### Phase 1 — Foundation (Week 1–2)

**Goal:** Hermes running alongside Second Brain daemon. No functionality removed yet.

Tasks:
1. Install Hermes: `hermes setup --portal`
2. Configure provider routing to match `llm_routes.py` aliases: `summarize → haiku`, `chat → sonnet`, `judge → haiku`
3. Configure Hermes gateway for Telegram (replaces telegram bot token in config.yaml)
4. Migrate `skills/*.md` to agentskills.io SKILL.md format (15 files; primarily frontmatter restructure + verification section)
5. Author `corpus-reader` plugin (`~/.hermes/plugins/corpus-reader/`) wrapping MemoryCache
6. Configure `SOUL.md` with Second Brain persona and `AGENTS.md` context

**Exit criteria:** `hermes gateway start` responds to Telegram with skill-based answers drawing from the iCloud corpus.

**Risk:** None — daemon still running in parallel; Hermes is additive.

### Phase 2 — Chat Migration (Week 3–4)

**Goal:** All 130+ slash commands live in Hermes. `chat_handler.py` retired.

Tasks:
1. Port `COMMAND_REGISTRY` commands as Hermes slash commands (skills + inline handlers)
   - High-volume read commands (`/memories`, `/comms`, `/contacts`, `/code`, `/notes`, `/events`, `/commitments`) → Hermes skills calling `corpus-reader`
   - Write commands (`/complete`, `/dismiss`, `/confirm`, `/reject`) → Hermes skills calling corpus-writer tool
   - Admin commands (`/version`, `/status`, `/help`) → Hermes built-ins + custom skill
2. Port context-loading logic (keyword scoring → top-20 files) into `corpus-reader` query API
3. Port 4,096-char chunking into a Hermes tool wrapper (or configure Hermes Telegram split)
4. Disable Telegram polling in `daemon.py` (remove `TelegramChatHandler` from gather)
5. Delete `chat_handler.py`, `command_core.py`, `transport.py`, `slack_adapter.py`, `chat_tools.py`, `skill_router.py`

**Exit criteria:** All COMMAND_REGISTRY commands verified via integration test (port `test_e2e_registry_coverage.py` to Hermes test harness).

**Risk:** Largest single phase — 7,599 lines of chat_handler. Recommend porting command groups in batches, verified against `test_content_assertions.py` patterns.

### Phase 3 — Scheduling & Notifications (Week 5)

**Goal:** All cron logic lives in Hermes. `notification_manager.py` and `report_scheduler.py` retired.

Tasks:
1. Port all `notification_manager.py` schedules to Hermes natural-language cron:
   - Daily briefing → `"daily at 7:30 AM"`
   - Pre-meeting alerts → `"10 minutes before each calendar event"` (skill reads upcoming events)
   - Commitment alerts → `"daily at noon and 5 PM"`
   - Goal/project deadline warnings → `"daily"`
2. Port `report_scheduler.py` scheduled reports to Hermes skills
3. Port `skill_optimizer.py` scoring → defer to Hermes built-in learning loop (configure scoring criteria in SOUL.md / skill frontmatter)
4. Disable these loops in `daemon.py`
5. Delete `notification_manager.py`, `report_scheduler.py`, `skill_optimizer.py`, `llm_routes.py`, `usage_tracker.py`, `quota_scanner.py`

**Exit criteria:** Morning briefing arrives on Telegram. Pre-meeting alert fires in test (mock calendar event 12 minutes out).

**Risk:** Hermes cron precision for "10 minutes before event" requires the skill to calculate the delta on each scheduled run — slightly more complex than the current `asyncio.sleep` approach. May need a 60s polling skill.

### Phase 4 — Scanner Migration (Week 6–8)

**Goal:** All scanners are Hermes plugins. `daemon.py` retired. Laptop launchd plists updated.

Tasks (one plugin per scanner, in parallel where possible):
1. Port `browser_watcher.py` → `plugin: browser-watcher` (5-min cron on laptops + full node)
2. Port `email_scanner.py` → `plugin: email-scanner`
3. Port `calendar_scanner.py` → `plugin: calendar-scanner`
4. Port `notes_scanner.py` → `plugin: notes-scanner`
5. Port `zoom_scanner.py` → `plugin: zoom-scanner`
6. Port `code_scanner.py` → `plugin: code-scanner`
7. Port `slack_scanner.py` → `plugin: slack-scanner` (or replace with native Hermes Slack channel — evaluate overlap)
8. Port `commitment_tracker.py`, `contact_tracker.py`, `project_inference_scanner.py` → tracker plugins using `corpus-reader` (full node only)
9. Port `goal_project_agent.py` → Hermes skill with `delegate_task(background=true)` for parallel goal checks (full node only)
10. **Per-machine deployment:**
    - Full node: register all plugins; `hermes gateway start` is the only process
    - Each laptop: register scanner plugins only; replace `daemon.py` launchd plist with plist calling `hermes cron tick` every 5 min
11. Remove loops from `daemon.py` one by one; delete when empty
12. Delete `memory_cache.py`, `memory_writer.py`, `daemon.py`, `utils.py`, `heartbeat.py`
    - **Exception:** Keep `memory_writer.py` and EDEADLK retry logic as shared utilities inside `plugins/_shared/`

**Exit criteria:** Full node: `hermes gateway start` is the only process. Laptops: launchd plist fires `hermes cron tick` every 5 min, no gateway process running. All 3,500 memory files appear in corpus-reader queries on the full node. iCloud sync carries laptop-written files within normal sync latency.

**Risk:** Full Disk Access and EventKit grants must be given to the Hermes process (or the plugin subprocess). Test early on macOS Sonoma with Homebrew Python (same FDA issue that required AppleScript fallback in email_scanner). Also validate `hermes cron tick` as a production-safe one-shot before relying on it for laptop scanner cadence (see §3.5 fallback).

### Phase 5 — Cleanup & New Capabilities (Week 9+)

**Goal:** Remove all Second Brain infrastructure that Hermes supersedes. Activate new capabilities.

Tasks:
1. Activate iMessage channel (Hermes 0.17 via Photon Spectrum)
2. Activate WhatsApp Business channel
3. Remove `install.sh` launchd plist generation; write Hermes startup guide
4. Evaluate RetainDB/Mem0 as external memory backend over iCloud corpus (Option C from §3)
5. Explore Hermes Raft network for cross-agent capability sharing
6. Update README.md to reflect Hermes-based architecture
7. Archive `specs/second-brain-spec-v1.0.md` and create `specs/hermes-architecture.md`

---

## 5. Risk Register

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Hermes plugin sandbox blocks macOS OS-level access | Medium | High | Local macOS install (non-Docker) should bypass; test in Phase 1 before committing to Phase 4 |
| FDA grant for Hermes process on macOS Sonoma | Medium | High | AppleScript fallback already implemented in email/calendar scanners; port the fallback |
| `hermes cron tick` not production-safe as one-shot | Medium | High | If unreliable: keep thin `scanner_daemon.py` (6 loops, no telegram-bot) on laptops as fallback; eliminates daemon.py from full node either way |
| Two gateway instances conflict (laptop + full node) | High | High | Laptops must use `hermes cron tick`, never `hermes gateway start`; enforce via install script |
| Hermes gateway Telegram 4,096-char chunking | Low | Medium | Implement chunking in `corpus-reader` response wrapper |
| agentskills.io SKILL.md format breaking changes | Low | Low | Pin Hermes version; skills are text files and easy to hand-migrate |
| Phase 2 command parity gap discovered mid-migration | Medium | Medium | Keep daemon.py running in parallel until Phase 2 exit criteria pass |
| Hermes cron minimum interval | Low | Low | Documented ticker is 60s; 5-min scanner cadence comfortably within that; verify empirically |
| Corpus-reader plugin performance at 3,500+ files | Low | Medium | MemoryCache SQLite already handles this; no new risk |

---

## 6. What Gets Deleted

At full migration completion (end of Phase 4), the following files are deleted from the repo:

```
daemon.py
chat_handler.py          # -7,599 lines
skill_optimizer.py       # -1,463 lines
notification_manager.py  # -1,380 lines
skill_executor.py
llm_routes.py
transport.py
slack_adapter.py
command_core.py
chat_tools.py
skill_router.py
heartbeat.py
usage_tracker.py
quota_scanner.py
quota_scrapers.py
llm_chat_importer.py
dedup_checker.py
report_scheduler.py
index_builder.py
secrets.py               # Hermes credential system
```

What remains in the repo:
```
# Scanner plugins (ported Python, now in plugin package structure)
plugins/
├── corpus-reader/
├── browser-watcher/
├── email-scanner/
├── calendar-scanner/
├── notes-scanner/
├── zoom-scanner/
├── code-scanner/
├── slack-scanner/
├── commitment-tracker/
├── contact-tracker/
├── project-inference/
├── goal-project-agent/
└── _shared/             # memory_writer, utils EDEADLK retry, github_client

# Skills (ported to agentskills.io format)
skills/

# Supporting infrastructure
memory_cache.py          # still used by corpus-reader plugin
content_fetcher.py       # still used by browser-watcher plugin
github_client.py         # still used by goal-project-agent plugin
circle_ruleset.py        # still used if circles feature retained
goals_tracker.py         # still used by goal-project-agent plugin

# Config and metadata
install.sh               # simplified (no plist; just hermes setup)
VERSION, CHANGELOG.md, README.md, CLAUDE.md
specs/
tests/                   # unit tests for plugin logic
```

**Net reduction: ~14,000+ lines deleted (roughly 55% of current codebase).**

---

## 7. What We Don't Get From Hermes

Honest accounting of features that require custom work regardless:

- **Typed frontmatter corpus** — Hermes memory is unstructured plain text. The `type:` / `status:` / `participants:` contract that drives all trackers must live in the corpus files and be exposed via `corpus-reader`. Hermes has no concept of typed knowledge documents.
- **iCloud multi-machine convergence** — Hermes has no equivalent of the watcher-role architecture (multiple machines writing to one iCloud corpus). The plugin-based scanner approach preserves this; a full Hermes-native migration would lose it.
- **macOS-native capture reliability** — AppleScript, EventKit, Full Disk Access are not Hermes primitives. They work through the Python plugin system but require manual capability grants and fallback paths.
- **Skill execution history** — Second Brain's `## Execution History` table inside skill files is a custom pattern. Hermes's learning loop tracks skill performance differently (not embedded in the skill file itself).

---

## 8. Open Questions

1. **`hermes cron tick` production safety:** Is it safe to call `hermes cron tick` from an external launchd plist every 5 minutes in production? What happens if a prior tick is still running (file lock? silent skip? crash)? This is the highest-priority empirical test for laptops. *(Researched: documented as a testing tool; production behavior under concurrent invocation unknown — must test.)*
2. **External memory backend:** Can RetainDB or Mem0 index an existing directory of typed markdown files without a custom adapter? Needs primary-source investigation (`hermes-agent.nousresearch.com/docs/integrations/`).
3. **Slack scanner overlap:** Hermes has a native Slack channel. Does native Slack message access (reading channel history) overlap with `slack_scanner.py`'s use case, or is the scanner writing memory files that the native channel cannot?
4. **Plugin inter-dependency:** Can corpus-reader be called synchronously from within another plugin's handler? Needs plugin API testing.
5. ~~**watcher-node install:** Does a watcher-node Hermes install work without the full Hermes feature set?~~ *Resolved: Hermes supports a "Blank Slate" install mode with minimal features. Scanner-only laptops install Hermes in blank-slate mode, register only scanner plugins, and call `hermes cron tick` via launchd. No gateway required.*
6. **Gateway conflict enforcement:** The install script must prevent `hermes gateway start` from running on laptops. What is the right mechanism — a config flag, a missing gateway config, or a wrapper script?

---

## 9. Suggested First Step

Before committing to Phase 1, run one validation experiment:

> Install Hermes locally. Author a minimal `corpus-reader` plugin that calls `MemoryCache.query_by_type("email_thread")` and returns JSON. Register one slash command (`/test-corpus`) in Hermes that calls it. Confirm it works over Telegram.

This takes ~2 hours and answers the three highest-risk questions simultaneously: plugin macOS access, Hermes Telegram integration, and MemoryCache interop. If it works, the migration is viable. If it doesn't, the failure mode tells you exactly what to adapt.
