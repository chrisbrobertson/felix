---
specmas: 3.0
kind: feature
id: feat-circles-sharing
version: 1.1.0
created: 2026-04-16
updated: 2026-06-29
status: partial
phases_done: [A, B, C, D]
phases_pending: []
complexity: high
maturity: 2
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-goals-projects
  - feat-proactive-notifications
---

# Circles — Selective Memory Sharing

## Overview

### Problem Statement

The Second Brain accumulates everything: work emails, family calendar events, personal
goals, health notes, private commitments. This richness is exactly the point — but it
also makes the brain completely private. There is no way to share a curated slice with a
partner, a family member, or a work team without giving them full access to everything.

The user wants to share portions of the brain with specific people (family members,
coworkers) while keeping the rest private. This is conceptually similar to Life360's
"circles" model: named groups of people who share a focused view of a larger dataset.
The technical constraint is that the system already has a reliable, permission-aware
sharing primitive: iCloud Drive's native folder sharing. That is the boundary to build on
— no custom backend, no cloud credentials, no new trust surface.

### Approach

A **circle** is a named group of people with a shared iCloud folder and a ruleset that
decides which memories flow into that folder. The host's daemon scans memories every 5
minutes, applies each circle's ruleset, and atomically syncs matching files into the
circle's shared iCloud folder. Circle members access those memories through their own
Telegram bot instance scoped to their circle folder — they get a read view of the
memories the host chose to share, with the same `/ask`, `/search`, and browse commands
they'd have on a personal brain.

### Scope

**In Scope:**
- **Circle metadata** — named circles with a member list and YAML ruleset
- **Circle Sync Scanner** — new async loop that applies rulesets and syncs memory files
  into per-circle shared iCloud folders (one-way: host → circle members)
- **Rule-based classification** — include/exclude rules matching on `type`, `tags`,
  `classification`, `hostname`, and arbitrary frontmatter fields
- **Deletion propagation** — when a file no longer matches a rule, remove it from the
  circle folder
- **Host Telegram commands** — `/circles`, `/circle <N>`, `/circle-status` for the host
  to manage and inspect circles
- **Member Telegram bot** — one bot token per circle; members get read-only
  `/ask`, `/search`, `/memories`, `/events`, `/commitments` commands scoped to the
  circle folder
- **Phased rollout** — Phase A (single circle, one-way sync) through Phase D (in-Telegram
  rule editor)
- **Config additions** — `circles.enabled`, `circles.dir`, `circles.scan_interval_seconds`
- **Deploy state file** — `circle-sync-state.json`

**Out of Scope:**
- End-to-end encryption of circle folders (iCloud handles at-rest encryption; that is sufficient)
- Multi-tenant cloud backend — iCloud folder sharing is the only sharing primitive
- Members writing *back* into the host brain (one-way only, at least through Phase C)
- LLM-based classification — rules are declarative YAML, not ML
- Cross-circle dedup (the same memory can appear in multiple circles)
- Member-to-member sharing (star topology only: host at centre, members at the edges)
- Circle membership managed through Apple ID changes (members are added/removed by the host editing the ruleset file)

### Success Metrics

- A memory tagged `[family]` appears in the family circle folder within one scan cycle
  (≤ 5 minutes) after being written
- Removing the `family` tag causes the file to disappear from the circle folder within
  the next scan cycle
- No private memories (not matching any include rule) appear in any circle folder
- Member bot responds to `/ask` scoped to the circle folder, not the host brain
- `pytest` passes with no new failures after each Phase A commit

---

## Functional Requirements

### FR-1: Circle Ruleset Format

Each circle is described by one YAML file stored in the **circles runtime directory**
(`~/secondbrain/circles/{circle-slug}.yaml`). The host edits these files; the daemon
reads them on every scan cycle (no restart needed).

**Ruleset shape:**

```yaml
circle: family                   # slug — must match the filename stem
display_name: "Robertson Family" # human-readable name for Telegram output
members:
  - telegram_user_id: 123456789
    name: "Alex"
  - telegram_user_id: 987654321
    name: "Sam"
bot_token: "7654321:AAxxxxxx"    # Telegram bot token for the member-facing bot
icloud_folder: "second-brain-circles/family/memories"  # relative to iCloud root
rules:
  include:
    - type: calendar_event
      tags_contains_any: [family, home, kids, school]
    - type: memory
      tags_contains_any: [travel, vacation, kids, home]
    - type: goal
      category: family
    - type: project
      category: family
  exclude:
    - tags_contains_any: [work, confidential, private]
    - classification: marketing
    - classification: automated
```

**Rule evaluation:**
1. A memory file passes if it matches **at least one** `include` rule.
2. A memory file is blocked if it matches **any** `exclude` rule.
3. `exclude` takes precedence over `include` — if both match, the file is not synced.
4. An empty `include` list means nothing is included. An empty `exclude` list means
   nothing is excluded.

**Rule predicates (all are optional per rule; absent predicates are wildcards):**

| Predicate | Matches when |
|-----------|-------------|
| `type: <value>` | frontmatter `type` equals value |
| `tags_contains_any: [a, b]` | frontmatter `tags` list contains at least one of the listed values |
| `tags_contains_all: [a, b]` | frontmatter `tags` list contains all listed values |
| `classification: <value>` | frontmatter `classification` equals value |
| `category: <value>` | frontmatter `category` equals value |
| `hostname: <value>` | frontmatter `hostname` equals value |
| `source_title_contains: <substr>` | frontmatter `source_title` contains substring (case-insensitive) |
| `frontmatter: {key: value}` | arbitrary frontmatter field equals value |

**Validation criteria:**
- Ruleset file missing `circle` field → scanner logs WARNING and skips the circle
- `circle` slug must match the filename stem (e.g. `family.yaml` → `circle: family`)
- Unknown predicate keys logged at WARNING, treated as no-op (forward compat)
- Empty ruleset file or syntax error → scanner logs ERROR and skips the circle
- `bot_token` missing → scanner still syncs files; member bot just won't start

---

### FR-2: Circle Sync Scanner

New **fourteenth async loop** (`circle_sync_scanner.py`, `full` role only). Runs every
5 minutes (configurable via `circles.scan_interval_seconds`).

**Per-cycle algorithm:**

1. Load all `*.yaml` files from the circles runtime dir. Skip malformed files (FR-1).
2. For each circle:
   a. Read the circle's `icloud_folder` path. Verify it exists; log WARNING and skip
      if not (member of the host hasn't shared the folder yet).
   b. Read all memory files from `MEMORIES_DIR/*.md` (use cached headers — same
      500-char frontmatter cache pattern as `chat_handler.py`).
   c. For each memory file, apply the ruleset:
      - If **matches include** and **not excluded**: the file should be present in the
        circle folder.
      - Otherwise: the file should not be present in the circle folder.
   d. Compute the delta:
      - Files that should be present but aren't → **sync** (atomic write to circle folder).
      - Files that should not be present but are → **delete** from circle folder.
      - Files that should be present and are, but whose mtime is newer than the last
        synced mtime → **re-sync** (content changed).
   e. Persist the updated per-circle file list (filename → mtime) to
      `circle-sync-state.json`.

**Atomic write pattern:** same as `memory_writer.py` — write to `{filename}.tmp`, then
`os.rename()` to the final path. Never write directly.

**Deletion:** `os.unlink()` the file from the circle folder. If the file is missing
(already gone), log DEBUG and continue — do not raise.

**State file format (`~/secondbrain/circle-sync-state.json`):**

```json
{
  "family": {
    "synced_files": {
      "2026-04-15-school-play-abc123.md": 1713182400.0,
      "calendar-event-macstudio-2026-04-20-dentist-def456.md": 1713182500.0
    },
    "last_run": "2026-04-16T09:00:00"
  },
  "work-team": {
    "synced_files": {},
    "last_run": "2026-04-16T09:00:00"
  }
}
```

**Validation criteria:**
- File not matching any include rule is never written to the circle folder
- File matching an exclude rule is never written even if it also matches an include rule
- File removed from MEMORIES_DIR is also removed from circle folder on next cycle
- File whose tags change so it no longer matches is removed from circle folder
- File whose tags change so it now matches is added to circle folder
- Missing state file treated as empty (first run)
- Circle folder not found: WARNING logged, circle skipped, no crash

---

### FR-3: Host Telegram Commands

Commands available only to the host user (the owner of the daemon). These are gated by
the existing host `chat_id` in `notification-state.json` — any `update.effective_user.id`
not matching the host's chat_id receives "Access denied."

| Command | Behaviour |
|---------|-----------|
| `/circles` | List all configured circles with member count and last-sync time |
| `/circle <N>` | Show detail for circle N: members, rule summary, synced file count, last-sync time |
| `/circle-status` | Quick health check: which circles are syncing, which have missing iCloud folders |

**List format:**
```
Circles (2 configured):
1. family — 2 members · 14 files synced · last sync 3 min ago
2. work-team — 5 members · 7 files synced · last sync 4 min ago
```

**Detail format:**
```
family (Robertson Family)
Members: Alex, Sam
iCloud folder: second-brain-circles/family/memories ✓
Synced: 14 files

Include rules:
  · type:calendar_event tags:[family,home,kids,school]
  · type:memory tags:[travel,vacation,kids,home]
  · type:goal category:family
Exclude rules:
  · tags:[work,confidential,private]
  · classification:marketing
```

**Session result set:** `_last_circle_set` — populated by `/circles`, consumed by `/circle N`.

**Validation criteria:**
- Invalid index returns "Invalid index. Run /circles first."
- Non-host user receives "Access denied." with no other output
- Missing iCloud folder shown as `❌` in detail view

---

### FR-4: Member Telegram Bot

Each circle gets its own Telegram bot (separate `bot_token` in the ruleset file). The
host creates the bot via @BotFather, pastes the token into the ruleset, and the daemon
starts a bot runner for each circle on startup (Phase B+).

**Architecture:**

One `python-telegram-bot` `Application` per circle, running as a separate task inside
the daemon's asyncio event loop. Each application is initialised with the circle's
`bot_token` and a `CircleBotHandler` instance bound to that circle's `icloud_folder`
path.

**`CircleBotHandler` commands (all read-only, scoped to the circle folder):**

| Command | Behaviour |
|---------|-----------|
| `/ask <question>` | Same as host `/ask` but queries only the circle folder's memory files |
| `/search <query>` | Keyword search across circle memories |
| `/memories [N]` | List recent memories in the circle; detail on `/memories N` |
| `/events [N]` | List calendar events in the circle folder |
| `/commitments [N]` | List commitments in the circle folder |
| `/help` | Show available commands and the circle's display name |

**User authentication:** Any Telegram user who messages the circle bot gets access (the
bot is invite-only by bot-token obscurity — it does not appear in public bot directories).
Phase C adds explicit member ID enforcement (`members[].telegram_user_id` allowlist).

**Scope enforcement:** All file reads in `CircleBotHandler` are restricted to the
circle's `icloud_folder` path. The handler never touches `MEMORIES_DIR`. This is
enforced by passing `memories_dir=circle_icloud_path` to all helper functions.

**Validation criteria:**
- `/ask` on the circle bot does not read from the host's MEMORIES_DIR
- Unauthenticated user in Phase C receives "You are not a member of this circle."
- Circle bot with missing `bot_token` is not started; host receives WARNING in logs
- Circle bot crash does not take down the host daemon

---

### FR-5: Ruleset Change Detection

The scanner reloads circle rulesets on every cycle (no daemon restart needed). Changes
to `~/secondbrain/circles/*.yaml` take effect within one scan cycle.

**On ruleset change:**
- Compute the new set of matching files against the updated ruleset.
- Sync additions (newly matching files).
- Remove deletions (formerly matching files that no longer match).
- State file updated to reflect new synced set.

**Validation criteria:**
- Adding a tag to a memory file causes it to appear in the circle folder on the next cycle
- Editing the ruleset to add a new include rule causes all matching existing memories to
  be synced on the next cycle
- Narrowing the ruleset causes non-matching files to be removed on the next cycle

---

### FR-6: Invite Flow (Phase C)

When a new member wants to join a circle, the host generates a one-time invite code that
the member redeems by messaging the circle bot. On redemption, the member's Telegram user
ID is added to the circle's member list in the ruleset file.

**Host command:** `/circle-invite <N>` — generates a one-time code (8-char hex, stored
in `circle-sync-state.json` with 24h TTL) and replies with:

```
Invite link for family:
Ask Alex to message @FamilyBrainBot and send: /join a1b2c3d4
(expires in 24 hours)
```

**Member command (on circle bot):** `/join <code>` — validates the code, appends
`{telegram_user_id: <id>, name: <first_name>}` to `members` in the ruleset file,
replies "Welcome to the family circle!".

**Validation criteria:**
- Expired code rejected with "Invalid or expired invite code."
- Code can only be used once (deleted from state on first use)
- `/join` without a valid code rejected
- New member ID added to ruleset file atomically

---

## Phased Rollout

### Phase A — One-way sync, manual ruleset, no member bot

**Deliverables:**
- `circle_sync_scanner.py` — scan loop with ruleset application and delta sync
- `circle_ruleset.py` — parser and rule-match predicate
- `daemon.py` — wire up scanner under `full` role guard
- `install.sh` — add `circle_sync_scanner.py`, `circle_ruleset.py` to `DAEMON_FILES`
- `config.yaml` template — add `circles` section
- `README.md` — how to create a circle folder, write a ruleset, share in iCloud

**Not in Phase A:**
- No Telegram commands (host or member)
- No member bot
- Manual ruleset editing only

**Test for Phase A:** iCloud folder exists, ruleset file written, daemon runs one sync
cycle — verify files appear and disappear correctly.

### Phase B — Host Telegram commands

**Deliverables:**
- `/circles`, `/circle N`, `/circle-status` commands in `chat_handler.py`
- `COMMAND_REGISTRY` entries for all three
- Unit tests for all three commands

### Phase C — Member bot (per-circle Telegram app)

**Deliverables:**
- `CircleBotHandler` class in `circle_bot.py` ✓
- `CircleBotRunner` in `circle_bot.py` — wraps one `Application` per circle ✓
- Per-circle bot startup in `daemon.py` (one `Application` per circle with `bot_token`) ✓
- Member ID enforcement (allowlist from `members[].telegram_user_id`) ✓
- `/circle-invite N` host command + `/join <code>` member command (FR-6) ✓
- Unit tests: scope enforcement, member allowlist, invite flow, runner lifecycle ✓

**Deferred (FR-4):** `/ask <question>` LLM query command for member bots.

### Phase D — In-Telegram rule editor

**Deliverables:**
- `/circle-rule add <N> include type:calendar_event tags:family` host command
- `/circle-rule remove <N> <rule_index>` host command
- Ruleset file updated atomically after each edit
- Unit tests for rule add/remove

---

## Config

```yaml
circles:
  enabled: false            # master kill-switch; no scan loop started if false
  dir: ~/secondbrain/circles  # runtime dir for *.yaml ruleset files
  icloud_root: ~/Library/Mobile Documents/com~apple~CloudDocs
  scan_interval_seconds: 300
```

The `icloud_root` is used to resolve the relative `icloud_folder` path in each
circle's ruleset. The full path for the family circle would be:

```
{icloud_root}/second-brain-circles/family/memories/
```

This directory must be created manually by the host before the scanner can sync to it.
iCloud folder sharing is configured through Finder → context menu → "Share" → add
invitees' Apple IDs. There is no API for this step.

---

## Runtime Directory Layout

```
~/secondbrain/
├── circles/                        # host-editable ruleset files (source of truth)
│   ├── family.yaml
│   └── work-team.yaml
└── circle-sync-state.json          # per-circle mtime state + invite codes
```

```
~/Library/Mobile Documents/com~apple~CloudDocs/
└── second-brain-circles/
    ├── family/
    │   └── memories/               # shared with family members via iCloud
    │       ├── 2026-04-15-school-play-abc123.md
    │       └── calendar-event-macstudio-2026-04-20-dentist-def456.md
    └── work-team/
        └── memories/               # shared with work team via iCloud
```

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `circle_ruleset.py` | **Create** — `CircleRuleset` dataclass; `load_ruleset(path)` parser; `matches_include(fm)` and `matches_exclude(fm)` predicates |
| `circle_sync_scanner.py` | **Create** — `CircleSyncScanner` class with `run_loop()`; loads all rulesets, applies delta sync, updates state; Phase C adds `CircleBotHandler` |
| `daemon.py` | Import and start `CircleSyncScanner` under `full` role + `circles.enabled` guard |
| `chat_handler.py` | Phase B: add `cmd_circles`, `cmd_circle`, `cmd_circle_status`, `cmd_circle_invite`; COMMAND_REGISTRY entries; `_last_circle_set` session state |
| `install.sh` | Add `circle_ruleset.py`, `circle_sync_scanner.py` to `DAEMON_FILES` |
| `config.yaml` template | Add `circles` section |
| `README.md` | Document circle setup (create iCloud folder, write ruleset, share folder), Telegram commands, member bot setup |
| `tests/unit/test_circle_ruleset.py` | **Create** — unit tests for parser and predicate logic |
| `tests/unit/test_circle_sync_scanner.py` | **Create** — unit tests for sync loop, delta logic, deletion |
| `tests/unit/test_chat_handler.py` | Extend — tests for `/circles`, `/circle N` commands (Phase B) |

---

## Unit Tests

### `tests/unit/test_circle_ruleset.py`

| Test | Assertion |
|------|-----------|
| `test_load_ruleset_valid` | Parses circle YAML, returns `CircleRuleset` with correct fields |
| `test_load_ruleset_missing_circle_field` | Missing `circle` key → `ValueError` |
| `test_load_ruleset_slug_mismatch` | `circle: foo` in `bar.yaml` → WARNING logged |
| `test_load_ruleset_unknown_predicate` | Unknown predicate key → WARNING, not crash |
| `test_matches_include_type_only` | `type: calendar_event` matches a calendar memory |
| `test_matches_include_tags_any` | `tags_contains_any: [family]` matches memory with `tags: [family, home]` |
| `test_matches_include_tags_all` | `tags_contains_all: [family, home]` requires both tags |
| `test_matches_include_category` | `category: family` matches goal with that category |
| `test_matches_exclude_overrides_include` | File matching both include and exclude → not synced |
| `test_empty_include_matches_nothing` | Empty include list → no files matched |
| `test_empty_exclude_blocks_nothing` | Empty exclude list → all includes pass |
| `test_frontmatter_predicate` | `frontmatter: {hostname: macstudio}` matches correct host |
| `test_source_title_contains` | `source_title_contains: dentist` matches case-insensitively |

### `tests/unit/test_circle_sync_scanner.py`

| Test | Assertion |
|------|-----------|
| `test_sync_adds_matching_file` | Memory matching include rule → written to circle folder |
| `test_sync_excludes_blocked_file` | Memory matching exclude → not written to circle folder |
| `test_sync_removes_stale_file` | File in state but no longer matching → deleted from circle folder |
| `test_sync_updates_changed_file` | Matching file with newer mtime → re-synced (overwritten) |
| `test_sync_skips_missing_icloud_folder` | Circle folder not found → WARNING, no crash, other circles still processed |
| `test_sync_skips_malformed_ruleset` | Invalid YAML in ruleset → circle skipped, no crash |
| `test_state_file_written` | After sync cycle, `circle-sync-state.json` updated with correct mtimes |
| `test_missing_state_file_treated_as_empty` | No state file → scanner starts fresh, no crash |
| `test_circles_disabled` | `circles.enabled: false` → loop body never executes |
| `test_atomic_write` | Sync write uses temp file + rename (no partial file in circle folder) |
| `test_deletion_missing_file_is_noop` | File already absent from circle folder → no error on delete |
| `test_ruleset_change_detected_each_cycle` | Updated ruleset on disk → new rules applied next cycle |

---

## Open Questions

1. **Deletion semantics:** When a synced file no longer matches the ruleset (tag removed,
   memory edited), should it be deleted from the circle folder immediately, kept as a
   "frozen" snapshot, or moved to a `removed/` subfolder? Current spec says delete.
   The frozen-snapshot approach would be safer for members who've read and are referencing
   the content, but adds complexity.

2. **Bidirectional sync (Phase E):** Should members be able to write memories back into
   the host brain? If so, what frontmatter is required to prevent collisions? What review
   flow does the host need? This is explicitly out of scope for v1 but should not be
   architecturally foreclosed.

3. **Summary redaction:** Instead of syncing full memory files, could the host opt to
   share only `source_title` + `summary` (stripping `messages`, `transcript`, etc.)? A
   `share_level: summary | full` field in the include rule could enable this. Not in
   v1 but worth designing for.

4. **iCloud sharing UI automation:** The host must manually share each circle folder in
   Finder. Is there a scriptable path (AppleScript, `cloudcontroller`/`brctl`) that
   could automate folder sharing from the daemon? If so, Phase C could add a
   `/circle-share <N> apple_id@example.com` command that triggers the share.

5. **Multi-machine:** Circle sync runs on the `full` role machine. Watcher-role machines
   write to `MEMORIES_DIR`. As long as iCloud sync keeps `MEMORIES_DIR` in sync across
   machines, the circle sync loop on the full machine will pick up watcher-written
   memories correctly. But if iCloud sync lags, circle members may see stale content.
   Is an explicit staleness indicator needed?

---

## Changelog

### v1.0.0 (2026-04-16)
- Initial spec: rule-based one-way sync via iCloud shared folders
- Four-phase rollout: sync loop → host commands → member bot → rule editor
- Ruleset format: include/exclude rules on type, tags, classification, category, hostname, frontmatter
- `CircleSyncScanner` delta algorithm with state file
- Per-circle Telegram bot (one bot token per circle) for Phase C
- Invite flow via one-time codes for Phase C
- Host Telegram commands: `/circles`, `/circle N`, `/circle-status`, `/circle-invite N`
- Member bot commands: `/ask`, `/search`, `/memories`, `/events`, `/commitments`
