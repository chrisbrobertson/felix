---
spec_type: bugfix
id: fix-command-tool-parity
specmas: 3.0
kind: bug
version: 1.0.0
created: 2026-09-09
status: review
owners:
  - Chris Robertson
complexity: high
maturity: 1
parent_system: second-brain
dependencies:
  - chat_tools.py
  - chat_handler.py
  - command_core.py
related_specs:
  - feat-chat-handler
  - feat-close-issues-tool
  - feat-goal-project-agent
  - feat-configurable-reports
  - feat-circles-sharing
  - feat-communication-watchlists
  - feat-memory-dedup
  - feat-domain-skip-filter
  - feat-llm-quota-tracking
fit_check: >
  Reuses the existing chat_tools.py TOOLS / MUTATING_TOOLS / dispatch() convention and the
  chat_handler.py `_X_text()` helper pattern (a text-producing method called by both the
  Telegram cmd_X handler and the tool dispatcher — see _get_action_text, _close_issue_text,
  _list_code_text). Adds one new registry, COMMAND_TOOL_MAP, next to COMMAND_REGISTRY in
  command_core.py, and enforces it with an AST-free, dict-driven parity test in
  tests/unit/test_chat_tools.py — the same "invariant enforced by a dedicated test" pattern
  CLAUDE.md documents for the MemoryCache read-path rule (test_memory_cache_migration.py). No
  new runtime dependencies, no schema/config changes, no new async loop.
source_ticket: https://github.com/chrisbrobertson/felix/issues/129
---

# Slash-command / LLM-tool parity

## 1. Frame

### 1.1 Problem statement

Ticket [#129](https://github.com/chrisbrobertson/felix/issues/129) ("not all slash commands
are available as a tool call") was filed via `/bug` with no expected/actual detail beyond the
title. The title is accurate and independently verifiable against the code: `chat_tools.py`
defines `TOOLS`, a fixed list of ~40 function-calling schemas the chat LLM can invoke, but
`command_core.py`'s `COMMAND_REGISTRY` — the single source of truth for Telegram slash
commands (per `CLAUDE.md`) — lists over 100 commands across 17 groups. There is no mechanism
that keeps the two in sync, so a user asking the bot in natural language to do something a
slash command already does (e.g. "what does /status show", "any duplicate memories?", "show me
my skill drafts") gets no tool call and either a wrong answer or "I don't have a way to check
that" — even though the equivalent slash command exists and works.

This is not the first fix under this ticket number. Commit `9b2254b` ("fix(chat): get_action
tool and add run/drop/defer LLM action tools (#129)") already closed the gap for the "Agent
actions" group (`/actions`, `/action`, `/run`, `/drop`, `/defer` now all have tool
equivalents). That commit fixed one instance of the pattern but did not address the other 16
command groups, which is why the ticket is still open. This spec generalizes that fix: instead
of adding tools for one more group and leaving the ticket open again, it (a) enumerates every
remaining gap against the current tree, and (b) adds an enforcement mechanism so a future
command added to `COMMAND_REGISTRY` without a matching tool decision fails CI instead of
silently reopening this ticket a third time.

### 1.2 Goals

- Close the enumerated read-only coverage gap: every list/detail/status command in
  `COMMAND_REGISTRY` that has no side effects gets a corresponding LLM tool.
- Make every command's tool-availability status an explicit, reviewable decision — mapped to a
  tool, or exempted with a stated reason — rather than an implicit omission.
- Add a regression test that fails when a new command is added to `COMMAND_REGISTRY` without
  a corresponding entry in the new map, so this class of bug cannot recur silently.

### 1.3 Non-goals

- Exposing every *mutating* command as a tool. `chat_tools.py`'s own module docstring states
  the design intent: "Read-only retrieval commands exposed as function-calling tools... State-
  mutating commands are usually excluded." This spec keeps that default and treats mutating
  commands as exemptions unless a specific one is called out below. (Note: the docstring is
  now stale — `run_action`/`drop_action`/`defer_action`/`add_todo` etc. are already mutating
  exceptions — so this spec also updates the docstring to reflect the exemption-based model
  instead of removing the true parts of it.)
- Multi-turn command sequences, compound verb-routing commands' full argument surface (e.g.
  `/quota report ...`, `/settings <key> <value>`, `/skill_approval on|off`) — these are
  exempted as documented mutating/compound commands (§4.3); only the pure-view forms already
  covered by existing exemption reasoning are considered.
- Changing which tools are visible per-turn (the `active_tools` filter in
  `chat_handler.py:7898-7902`, which only gates the two pending-reply tools). Out of scope.
- Re-architecting `COMMAND_REGISTRY` or `CommandRouter`.

## 2. Known facts vs. assumptions

**Known facts (verified against this checkout):**

- `chat_tools.TOOLS` currently has 40 entries (`grep -c '"name":' chat_tools.py` inside the
  `TOOLS` list, excluding the one nested `"name"` under `get_memory`'s parameter schema).
- `command_core.COMMAND_REGISTRY` has 17 groups and >100 command entries (verified via
  `test_command_registry_has_expected_groups` in `tests/unit/test_command_core.py`).
- `chat_handler.py:7898-7902` filters `TOOLS` into `active_tools` only to conditionally
  include/exclude `deliver_pending_replies` / `discard_pending_replies`; it is not a role or
  name allowlist. Adding an entry to `TOOLS` is sufficient to expose it to the LLM on every
  chat turn — no second registration point exists.
- The "list populates a per-session result set, detail command indexes into it" pattern
  already exists for every group under discussion: `_last_dupes_set`, `_last_watchlist_set`,
  `_last_commitment_set`, `_last_action_set`, `_last_candidate_set`, `_last_code_set`,
  `_last_skill_draft_set`, `_last_report_set`, `_last_circle_set`, etc. are all already
  populated by the existing `cmd_*` methods. This means the state-tracking half of "list then
  get by index" is already done for the groups this spec adds tools to — only the tool schema,
  the `_X_text()` extraction (where missing), and the dispatch wiring are new.
- Some groups already have the `_X_text()` extraction the tool layer needs
  (`_list_notes_text` at `chat_handler.py:5841`, `_list_code_text` at `chat_handler.py:5507`,
  `_show_candidate_detail` used by `cmd_review`, `quota_scanner.render_status()` used by
  `cmd_quota`, `_format_aichat_list` / `_format_aichat_list_grouped` / `_format_aichat_detail`
  used by `cmd_aichat`). Others do not (`cmd_status`, `cmd_pending`, `cmd_reports`,
  `cmd_circles`, `cmd_skill_drafts`, `cmd_skill_health`, `cmd_watches`, `cmd_dupes`,
  `cmd_skiplist`, `cmd_accuracy`, `cmd_changes`, `cmd_insights`, `cmd_briefing`, `cmd_version`,
  `cmd_usage`) — for those, this spec requires extracting the reply-construction logic into a
  new `_X_text()` method first, same shape as the existing ones, with `cmd_X` reduced to
  `await self._send_reply(update, await self._X_text(...))` (or the sync equivalent).
- `close_issue`'s `status` enum is `["done", "wont_do", "in_progress"]` — it has no "planned"
  value, so `/feature_plan` (which sets `status: planned` via `_gh_set_status` /
  `_rewrite_feature_frontmatter`) has no tool equivalent today even though `close_issue`
  already exists and is the natural home for it.

**Assumptions (flagged for reviewer confirmation, not blocking):**

- The ticket author (also the sole end user) wants a durable *mechanism*, not just this one
  batch of tools filled in — inferred from the fact that the previous partial fix under the
  same ticket number left it open. If that reading is wrong and only "add a few more tools" was
  wanted, §4 below is still directly actionable as a checklist; the enforcement test in §7 can
  be dropped without affecting the tool additions.
- Growing `TOOLS` from 40 to ~65 entries is acceptable token-budget-wise. No token-count
  regression test exists today; §8 recommends adding a bound rather than assuming this is free.
- Full-role-only backing services (`quota_scanner`, circles config, reports config) being
  absent on a `watcher`-role node should make the new tools return the same "not available"
  string their slash-command counterparts already return, not raise — consistent with every
  existing full-role-gated command.

## 3. Design

### 3.1 New registry: `COMMAND_TOOL_MAP` in `chat_tools.py`

```python
# command name (as it appears as a key in COMMAND_REGISTRY, e.g. "notes") -> tool name,
# or None if the command is intentionally not exposed as a tool.
COMMAND_TOOL_MAP: dict[str, str | None] = {
    "readings": "list_readings",
    "search": "search_memories",
    "reading": "get_reading",
    "forget": None,
    "people": "list_contacts",
    "contacts": "list_contacts",
    "contact": "get_contact",
    "code": "list_projects",
    "events": "list_events",
    "event": "get_event",
    "notes": "list_notes",
    "meetings": "list_meetings",
    "meeting": "get_meeting",
    "comms": "list_comms",
    "comm": "get_comm",
    "aichat": "list_aichat",
    "messages": "list_comms",
    "communications": "list_comms",
    "message": "get_comm",
    "communication": "get_comm",
    "insights": "get_insights",
    # ... one entry per command in every COMMAND_REGISTRY group (full table in §4)
}

# command name -> human-readable reason, required for every command whose
# COMMAND_TOOL_MAP value is None.
COMMAND_TOOL_EXEMPTIONS: dict[str, str] = {
    "forget": "mutating — deletes memory files",
    # ... full table in §4.3
}
```

Rationale for a `command -> tool name | None` map rather than a `command -> tool name` map
plus a separate allowlist: it forces every command to be visited exactly once when the map is
authored, which is what makes the parity test in §7 exhaustive rather than best-effort.

Aliases resolve to the same tool as their canonical command (`people` and `contacts` both map
to `list_contacts`; `bugs` and `features` both map to `list_features`); this is expected and
the parity test must not require distinct tools per alias.

### 3.2 Extraction pattern for commands with no existing `_X_text()`

For every command in §4.1 whose backing `cmd_X` method builds and sends its reply inline
(`cmd_status`, `cmd_pending`, `cmd_reports`, `cmd_circles` [list mode], `cmd_skill_drafts`,
`cmd_skill_health`, `cmd_watches`, `cmd_dupes`, `cmd_skiplist`, `cmd_accuracy`, `cmd_changes`,
`cmd_insights`, `cmd_briefing`, `cmd_version`, `cmd_usage`), apply the same refactor already
used throughout the file (e.g. `_get_action_text` / `cmd_action`, `_list_code_text` /
`cmd_code`):

1. Move the reply-text-building body into a new method `_X_text(self, ...) -> str` (async if it
   awaits anything, sync otherwise), taking the same optional filter/limit arguments the slash
   command accepts and returning the string that was previously passed to `reply_text`.
2. Reduce `cmd_X` to: parse Telegram args → call `_X_text(...)` → `await self._send_reply(update, text)` (or
   `update.message.reply_text(text)`, matching the existing convention for that command).
3. Any per-session `_last_*_set` assignment stays in `_X_text()` (not in `cmd_X`) so a tool call
   populates the same result set a slash command would, letting a later `get_X` tool call or
   `/X_detail` command resolve indices identically regardless of which path populated the list.
4. Full-role-only commands (`quota`, `reports`, `circles`, `skill_drafts`, `skill_health`) must
   have `_X_text()` return the existing "not available" string when the backing scanner/service
   is `None` — never raise. This already happens in every affected `cmd_X` today; the extraction
   must preserve it verbatim.

### 3.3 New tool schemas

Each new tool follows the existing `TOOLS` entry shape (`type: "function"`, `function.name`,
`function.description`, `function.parameters` as JSON Schema). Detail ("get_X") tools take an
`index` (1-based, from the most recent `list_X` result) and, where the existing slash command
supports it, a fallback `title`/`name` string — matching `get_contact`'s
`name_or_index` precedent where a title-only lookup makes sense.

### 3.4 Dispatch wiring

Add one `if name == "...":` branch per new tool inside `chat_tools.py`'s `_call()` (the
function `dispatch()` wraps), calling the corresponding `handler._X_text(...)`, following the
exact style of every existing branch. No changes to `dispatch()` itself, `MUTATING_TOOLS`, or
the `tool_dispatch` closure in `chat_handler.py` are needed for the new tools (they are all
read-only) — only `close_issue`'s enum changes touch existing tool behavior (§4.2).

### 3.5 `chat_tools.py` module docstring update

Replace:

```
Read-only retrieval commands exposed as function-calling tools so the
LLM can fetch data itself. State-mutating commands are usually excluded,
but add_goal and add_project are deliberate exceptions — natural-language
creation is the whole point of those operations.
```

with wording that names `COMMAND_TOOL_MAP` as the source of truth for what is and is not
exposed, and points at `COMMAND_TOOL_EXEMPTIONS` for the mutating/compound/security exclusions,
so the comment doesn't drift out of sync with the code the way it just did (the old comment
undersold the number of mutating exceptions already shipped: `run_action`, `drop_action`,
`defer_action`, `close_issue`, `close_commitment`, `close_goal`, `close_project`, `add_todo`,
`update_feature`, `update_issue_priority`, `deliver_pending_replies`,
`discard_pending_replies`).

## 4. API / CLI contract

### 4.1 New tools to add (in scope)

All are read-only, take no chat-session side effects beyond populating the same `_last_*_set`
their slash-command equivalent already populates, and are **not** added to `MUTATING_TOOLS`.

| New tool | Backing command(s) | `_X_text()` | Args |
|---|---|---|---|
| `list_notes` | `/notes` | exists: `_list_notes_text` (5841) | `limit`, `folder`, `todos_only` |
| `list_aichat` | `/aichat`, `/aichat search <q>` | new, wraps existing `_format_aichat_list` / `_format_aichat_list_grouped` | `query` (optional), `limit` |
| `get_aichat` | `/aichat <N>` | new, wraps existing `_format_aichat_detail` | `index` |
| `get_insights` | `/insights` | new (extract from `cmd_insights`) | `limit` |
| `get_commitment_accuracy` | `/accuracy` | new (extract from `cmd_accuracy`) | — |
| `get_quota` | `/quota` (view form only) | new, thin wrapper over `quota_scanner.render_status()` | — |
| `get_changes_digest` | `/changes [hours]` | new (extract from `cmd_changes`) | `hours` (default 24) |
| `get_pending_summary` | `/pending` | new (extract from `cmd_pending`) | — |
| `list_candidates` | `/review` (list mode) | new, reuses existing candidate-loading block in `cmd_review` | — |
| `get_candidate` | `/review N` | new, wraps existing `_show_candidate_detail` | `index` |
| `get_briefing` | `/briefing` | new, wraps `notification_manager._assemble_briefing()` | — |
| `list_skipped_domains` | `/skiplist` | new (extract from `cmd_skiplist`) | — |
| `list_duplicates` | `/dupes` | new (extract from `cmd_dupes`) | — |
| `list_watchlists` | `/watches` | new (extract from `cmd_watches`) | — |
| `list_skill_drafts` | `/skill_drafts` | new (extract from `cmd_skill_drafts`) | — |
| `get_skill_draft` | `/skill_draft N` | new | `index` |
| `get_skill_health` | `/skill_health` | new (extract from `cmd_skill_health`) | — |
| `list_reports` | `/reports` | new (extract from `cmd_reports`) | — |
| `get_report` | `/report N` | new | `index` |
| `list_circles` | `/circles` | new (extract from `cmd_circles`) | — |
| `get_circle` | `/circle N` | new | `index` |
| `get_circle_status` | `/circle_status` | new | — |
| `get_version` | `/version` | new (trivial, reads `VERSION`) | — |
| `get_usage` | `/usage [days]` | new (extract from `cmd_usage`) | `days` (default 7), `daily` (bool) |
| `get_daemon_status` | `/status` | new (extract from `cmd_status`) | — |

25 new tools. `TOOLS` grows from 40 to 65 entries.

### 4.2 Existing-tool fix: `close_issue` status enum gains `planned`

`/feature_plan` sets `status: planned` (`chat_handler.py:7088-7096`) but `close_issue`'s
`status` enum is `["done", "wont_do", "in_progress"]`. Extend the enum to
`["done", "wont_do", "in_progress", "planned"]`. No change to `_close_issue_text`'s hyphen-
mapping dict is needed — `"planned"` has no underscore/hyphen variant to translate, and the
GitHub sync path (`_gh_set_status`) already accepts it (it's the same call
`cmd_feature_plan` makes directly). Update `COMMAND_TOOL_MAP["feature_plan"] = "close_issue"`.

### 4.3 Exemptions (`COMMAND_TOOL_EXEMPTIONS`)

Every command below is intentionally **not** given a tool. Grouped by reason:

**Mutating — destructive or state-changing, no read-only equivalent to expose:**
`forget`, `wrong`, `missed`, `goal_note`, `goal_due`, `project_note`, `project_due`,
`addmilestone`, `milestone`, `linkgoal`, `unlinkgoal`, `confirm`, `reject`, `review_purge`,
`edit`, `mute`, `unmute`, `skip`, `unskip`, `merge`, `keep`, `watch`, `unwatch`, `feature_note`,
`approve_skill`, `reject_skill`, `skill_approval`, `report_add`, `report_remove`,
`report_pause`, `report_resume`, `report_run`, `circle_rule`, `reset`.

**Mutating with an elevated-risk reason (call out explicitly, don't fold into the generic
"mutating" bucket — see §6 Security):**
- `remember`, `note`, `backfill`, `deepen`, `rebuild_cache` — expensive or externally-reaching
  operations (arbitrary URL fetch, bulk reprocessing, full cache rebuild); allowing the LLM to
  trigger these from conversational inference risks cost/DoS-shaped surprises and, for
  `remember`/`note`, SSRF-shaped risk if a memory file's content ever contains an attacker-
  influenced URL that gets echoed back into a prompt the model then "acts on" by fetching it.
- `circle_invite` — generates a 24h access-granting credential; must never be reachable by
  implicit LLM inference, only by the user explicitly typing the command.

**Compound view/set commands — the view-only sub-form is low natural-language value and is
deliberately deferred rather than added now (see §9 Out of scope):**
`settings`, `skill_approval` (already listed above as mutating since its net effect includes
toggling), `quota` (only the `/quota report ...` mutating subcommand is exempt — the bare
`/quota` view form is covered by `get_quota` in §4.1).

**Pure aliases — resolve to the same tool as their canonical command, not a distinct
exemption:** `people`→`list_contacts`, `messages`/`communications`→`list_comms`,
`message`/`communication`→`get_comm`, `commands`→`list_commands`, `feature_new`→`add_feature`,
`fdetail`→`get_feature`, `bugs`/`features`→`list_features`.

**Transport-incompatible:** `import_chats` — requires a file attachment on the Telegram
message; the tool-calling contract has no file-upload channel today.

**One-time migration utility, not a durable user-facing capability:** `feature_import`.

**Already covered by an existing many-to-one mapping (no new tool needed):**
`completegoal`/`abandongoal` → `close_goal`; `completeproject`/`abandonproject`/`holdproject`
→ `close_project`; `complete`/`dismiss` → `close_commitment`; `feature_start` → `close_issue`
(`in_progress`); `feature_done` → `close_issue` (`done`); `feature_wont_do` → `close_issue`
(`wont_do`); `feature_plan` → `close_issue` (`planned`, new per §4.2); `todo` → `add_todo`.
The `/todos done N` / `/todos dismiss N` sub-verbs are a distinct code path from `/complete` /
`/dismiss` — `cmd_todos` calls `CommitmentTracker.update_commitment_status` directly against
`_last_todos_set` rather than reusing `_close_commitment_text` (`chat_handler.py:2601-2646`) —
but they are not a separate `COMMAND_REGISTRY` entry (`todos` is one entry covering list +
both verbs), so `todos` maps to `list_todos` for the registry-level decision and its
mutating sub-verbs are covered by the existing `close_commitment` exception in spirit, not by
a literal shared function today. No code change to unify these two paths is required by this
spec; noted here only so the mapping isn't misread as implying they're already unified.

**Already has full parity (no action needed — cited as the working example this spec
generalizes from):** `actions`→`list_actions`, `action`→`get_action`, `run`→`run_action`,
`drop`→`drop_action`, `defer`→`defer_action`.

### 4.4 Full command → decision table

The complete, exhaustive mapping (every key in `COMMAND_REGISTRY`, flattened across all 17
groups) is the literal content of `COMMAND_TOOL_MAP` + `COMMAND_TOOL_EXEMPTIONS` described in
§3.1, §4.1, §4.2, and §4.3 above. Implementers must derive the dict from those four
subsections directly — there is no additional undocumented command left over; §7's parity test
is what proves that.

## 5. Invariants

- **I1**: Every key in `COMMAND_REGISTRY` (flattened `{cmd for group in COMMAND_REGISTRY.values() for cmd, _ in group}`) appears as a key in `COMMAND_TOOL_MAP`.
- **I2**: For every `(cmd, tool)` in `COMMAND_TOOL_MAP.items()` where `tool is not None`, `tool` is the name of an entry in `chat_tools.TOOLS`.
- **I3**: For every `(cmd, tool)` in `COMMAND_TOOL_MAP.items()` where `tool is None`, `cmd` is a key in `COMMAND_TOOL_EXEMPTIONS` with a non-empty string reason.
- **I4**: No tool added under this spec is added to `MUTATING_TOOLS` (all 25 are read-only).
- **I5**: For every command whose `cmd_X` was refactored to call a new `_X_text()`, the Telegram-visible output of `/X` is byte-identical to the tool's return value for equivalent arguments (verified by acceptance tests, §10).
- **I6**: Adding a new command to `COMMAND_REGISTRY` without a corresponding `COMMAND_TOOL_MAP` entry fails `tests/unit/test_chat_tools.py` (this is I1 restated as an enforcement claim, not a new rule).

## 6. Idempotency

All 25 new tools are pure reads (memory-cache queries, config reads, in-memory state reads) —
calling any of them any number of times with the same arguments produces the same result
(modulo underlying data changing between calls) and has zero side effects on iCloud files,
GitHub issues, or scanner state. This mirrors every existing `list_*`/`get_*` tool. No
idempotency key or dedup logic is needed.

## 7. Error model

Follows the existing `chat_tools.dispatch()` contract unchanged:

- Missing required argument → `KeyError` inside `_call()` is caught by `dispatch()` and
  returned as `"Error running {name}: missing required argument {e}"`.
- Any other exception inside a new `_X_text()` → caught by `dispatch()`'s generic `except
  Exception` and returned as `"Error running {name}: {e}"`.
- Full-role service unavailable (`quota_scanner`, circles config, reports config, skill
  creator) → `_X_text()` returns the same plain "not available" string the existing `cmd_X`
  returns today; this is a normal string return, not an exception, matching existing behavior
  for e.g. `cmd_quota` when `quota_scanner` is `None`.
- Out-of-range `index` on a `get_X` tool → `"Index N out of range (1-M)."`, matching
  `_get_action_text`'s existing pattern exactly.
- Empty/never-populated `_last_X_set` on a `get_X` tool before the matching `list_X` was called
  → `"No {X} listed yet. Call list_{X} first."`, matching `_get_action_text`'s existing pattern.

## 8. Security

- **New tools introduce no new privilege**: every new tool reads data a `_check_auth`-gated
  Telegram command already exposes to the same authorized user; there is no tool here that
  surfaces data the user couldn't already `/command` their way to.
- **Elevated-risk commands stay excluded, explicitly** (§4.3): `remember`, `note`, `backfill`,
  `deepen`, `rebuild_cache`, `circle_invite` are not given tools by this spec, and
  `COMMAND_TOOL_EXEMPTIONS` must record *why* (cost/DoS/SSRF/credential-issuance) so a future
  contributor doesn't "complete the parity table" by adding them without re-litigating the
  risk.
- **`reset` stays excluded**: allowing the LLM to infer when to clear its own conversation
  history is a footgun (a model could clear context defensively mid-conversation); this is a
  stability/UX exclusion, not a data-sensitivity one, but it's still deliberate.
- No new external network calls, no new file-system write paths, no new attack surface beyond
  what `chat_handler.py`'s existing `_check_auth` gate already governs.

## 9. Telemetry

- `chat_tools.dispatch()` already logs `dispatch {name} args={arguments}` and
  `dispatch {name} → {N} chars` for every call (`chat_tools.py:1146-1150`); the 25 new tools
  get this for free with no additional instrumentation.
- No new telemetry is required for this fix. If usage data later shows some of the deferred
  compound-command tools (`get_settings`, `get_skill_approval_mode`) would in fact see LLM
  usage, that's discoverable from these existing dispatch logs without new counters.

## 10. Bounds

- `TOOLS` grows from 40 to 65 entries (+62%). This spec does not add a hard limit, but flags it
  as a bound worth watching: **acceptance test AT-9** below asserts the total tool count is
  ≤70, so an unreviewed future addition that pushes well past this batch fails loudly rather
  than silently degrading prompt budget. If real-world token measurements (not required by
  this spec, but recommended as a fast follow) show the schema payload is a meaningful fraction
  of the 200K context budget, trimming candidates are the "compound view" exemptions in §4.3,
  which were deliberately deferred rather than added.
- Every new list-style `_X_text()` reuses the truncation conventions already present in its
  extracted source (`[:5]` disambiguation caps, `[:60]`/`[:80]` string truncation, `limit`
  parameters defaulting to the slash command's existing default) — no new unbounded-output
  paths are introduced.

## 11. Failure modes

| Failure | Detection | Behavior |
|---|---|---|
| New command added to `COMMAND_REGISTRY`, forgotten in `COMMAND_TOOL_MAP` | `tests/unit/test_chat_tools.py::test_command_tool_map_is_exhaustive` fails at PR time | Test failure blocks merge (per CLAUDE.md: pytest must pass before every commit) |
| `COMMAND_TOOL_MAP` entry points at a tool name that doesn't exist in `TOOLS` (typo) | `test_command_tool_map_tools_exist` fails | Test failure blocks merge |
| `COMMAND_TOOL_EXEMPTIONS` entry with empty/missing reason string | `test_command_tool_exemptions_have_reasons` fails | Test failure blocks merge |
| Extraction refactor accidentally changes `/X` Telegram output (e.g. drops a line, reorders) | Parity acceptance test (§I5) comparing `/X` reply text to `list_X`/`get_X` tool output | Test failure at PR time |
| Full-role tool called on a `watcher`-role deploy | `_X_text()` returns "not available" string (existing behavior preserved) | No crash, degraded-but-correct response, consistent with existing full-role commands |
| LLM calls a new tool with a stale `index` after the user ran an unrelated command that also uses `_last_X_set` naming collision | Same failure mode already exists for every current `get_X` tool (shared instance state, not per-chat) | Out of scope — pre-existing architectural property, not introduced by this spec |

## 12. Acceptance tests

All new tests land in `tests/unit/test_chat_tools.py` unless noted; extraction-refactor parity
tests land alongside the relevant existing test file for that command family
(`tests/unit/test_chat_handler.py`).

1. **AT-1** `test_command_tool_map_is_exhaustive` — every command key across all
   `COMMAND_REGISTRY` groups appears in `COMMAND_TOOL_MAP` (I1).
2. **AT-2** `test_command_tool_map_tools_exist` — every non-`None` value in
   `COMMAND_TOOL_MAP` is a name present in `{t["function"]["name"] for t in TOOLS}` (I2).
3. **AT-3** `test_command_tool_exemptions_have_reasons` — every `cmd` with
   `COMMAND_TOOL_MAP[cmd] is None` has a non-empty string in `COMMAND_TOOL_EXEMPTIONS[cmd]` (I3).
4. **AT-4** `test_no_new_tool_is_mutating` — none of the 25 new tool names from §4.1 appear in
   `MUTATING_TOOLS` (I4).
5. **AT-5** (parametrized, one case per new tool) `test_dispatch_<tool_name>` — dispatch routes
   to the expected `_X_text()` mock and returns its value, mirroring the existing
   `test_dispatch_list_projects`-style tests.
6. **AT-6** (one per refactored command, e.g. `test_status_command_matches_get_daemon_status_tool`)
   — seed identical fixture state, call `cmd_X` and capture the `reply_text` argument, call
   `get_daemon_status`/`list_X` via `chat_tools.dispatch`, assert the two strings are equal
   (I5). Minimum required cases: `status`, `pending`, `reports`, `circles`, `skill_drafts`,
   `skill_health`, `watches`, `dupes`, `skiplist`, `accuracy`, `changes`, `insights`, `briefing`,
   `version`, `usage`, `notes` (existing helper — just needs the new tool-side test),
   `aichat` (list, search, and detail forms), `review` (list and detail).
7. **AT-7** `test_close_issue_accepts_planned_status` — `close_issue` tool called with
   `status="planned"` updates frontmatter to `status: planned` and the enum in the tool schema
   includes `"planned"`.
8. **AT-8** `test_feature_plan_maps_to_close_issue` (in `test_command_core.py` or
   `test_chat_tools.py`) — `COMMAND_TOOL_MAP["feature_plan"] == "close_issue"`.
9. **AT-9** `test_tools_list_bounded` — `len(chat_tools.TOOLS) <= 70`.
10. **AT-10** `test_full_role_tool_graceful_when_service_absent` — `get_quota`, `list_circles`,
    `list_reports` return a non-error "not available"-style string (not an exception) when the
    corresponding handler attribute/scanner is `None`, matching each command's existing
    behavior.
11. **AT-11** `test_elevated_risk_commands_not_in_map_as_tools` — explicit negative test that
    `COMMAND_TOOL_MAP["remember"]`, `["note"]`, `["backfill"]`, `["deepen"]`,
    `["rebuild_cache"]`, `["circle_invite"]`, `["reset"]` are all `None` — guards against a
    future well-meaning "complete the table" edit silently re-adding these (§8).

## 13. Versioning

This is a MINOR bump per `CLAUDE.md` ("new user-visible capability" — 25 new tool-callable
capabilities plus a `close_issue` status option). Suggested `CHANGELOG.md [Unreleased] → Added`
entries (to be written at implementation time, not by this spec):

- Added LLM tool-call equivalents for `/notes`, `/aichat`, `/insights`, `/accuracy`, `/quota`,
  `/changes`, `/pending`, `/review`, `/briefing`, `/skiplist`, `/dupes`, `/watches`,
  `/skill_drafts`, `/skill_health`, `/reports`, `/circles`, `/circle_status`, `/version`,
  `/usage`, `/status` — closing the remaining gap from issue #129.
- Added `planned` to `close_issue`'s status enum so `/feature_plan` is reachable via
  conversation.

Per CLAUDE.md's release workflow, the actual `VERSION` bump and `CHANGELOG.md` section rename
happen in a separate "Bump version to X.Y.Z" commit once this work (and any other queued
changes) lands — not part of this spec's deliverable.

## 14. Out of scope / explicit follow-ups

- `get_settings` / `get_skill_approval_mode` view-only tools (deferred per §4.3 — low expected
  natural-language demand; revisit if dispatch logs show users asking for them in chat).
- Any mutating-command tool not already covered by an existing exception (§4.3's "mutating"
  bucket) — if the ticket author wants broader mutating coverage after reviewing this spec,
  that is a distinct, separately-scoped follow-up ticket, not a silent scope expansion here.
- A token-count budget test for the full `TOOLS` payload — recommended (§10) but not required
  by this spec; the count-based bound (AT-9) is the minimum bar.
- `/import_chats` becoming tool-callable once/if the chat transport gains a way to pass file
  content through a function-call argument.
