---
spec_type: feature
id: feat-babysit-status-export
status: review
owners:
  - chrisbrobertson@gmail.com
dependencies:
  - heartbeat.py
  - scripts/babysit-with-review.sh
  - scripts/work_reports.sh
  - scripts/promote_local_features.py
  - scripts/babysit_status.py (new)
  - tests/unit/test_babysit_status.py (new)
  - tests/unit/test_babysit_scripts_status_wiring.py (new)
  - README.md
  - CHANGELOG.md
  - VERSION
fit_check: >
  Reuses the repo's established atomic-write idiom (tmp-file + os.rename,
  as in heartbeat.py and memory_writer.py) and BRAIN_DIR convention used
  by every scanner module. New helper is stdlib-only, in-process
  unit-testable (mirrors tests/unit/test_heartbeat.py and
  tests/unit/test_promote_local_features.py), introduces no new
  dependencies, network listeners, or daemon changes, and does not touch
  install.sh since scripts/ already runs uninstalled from the repo
  checkout.
complexity: moderate
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
---

# Babysit Script Status Export

Source ticket: https://github.com/chrisbrobertson/felix/issues/127

## Overview

### Problem Statement

`scripts/babysit-with-review.sh` and `scripts/work_reports.sh` are long-running,
unattended bash loops that repeatedly invoke `claude -p` (and, for the
review-gated variant, `codex exec`) to drain the feature/bug backlog into
merged, deployed PRs. They can run for hours. Today their only observability
is:

- A lock file (`~/sisyphus-logs/<project>-<script>.stop`) whose mere
  *presence* is the only "is it running" signal — it says nothing about
  progress, and it does not get cleaned up if the process is killed with
  `SIGKILL` (the `trap ... EXIT` that removes it never fires).
- A raw, ever-growing text log (`~/sisyphus-logs/<project>-<script>-<ts>-<pid>.log`)
  that must be tailed and read by a human; nothing machine-readable.

There is no structured status a monitoring tool can poll. The ticket asks
for these scripts' status (and "other functionality/status") to be visible
on **home-lab-monitor**, a separate monitoring service the user runs on the
Mac mini, alongside whatever else it already watches on that machine.

`home-lab-monitor` is **not part of this repository** and was not found
anywhere in this checkout (confirmed by grepping the working tree for
`home-lab-monitor`, `home_lab_monitor`, `homelab`, and `mac mini` — the only
hits are this repo's own docs describing where the `full`-role daemon runs).
Its ingestion mechanism (file poll, directory watch, HTTP scrape, log tail,
Prometheus exporter, etc.) is therefore unknown and cannot be designed here.
This spec scopes the felix-side deliverable to **producing a correct,
documented, machine-readable status artifact** at a stable, predictable
location. Wiring `home-lab-monitor` to actually read it is an external
integration step the user performs separately, using the schema and path
this spec fixes as the contract.

### Known facts (from this checkout)

- `heartbeat.py` already gives every daemon async loop (all 16, in
  `daemon.py`) a JSON status file at `BRAIN_DIR/heartbeat-{hostname}.json`,
  written with an atomic tmp-write + `rename()`, and read by
  `heartbeat.read_all()` / the Telegram `/status` command
  (`chat_handler.py:cmd_status`). This already covers "other
  functionality/status" for the daemon itself, on any machine where the
  daemon is running and BRAIN_DIR (iCloud Drive) is mounted — which
  includes the Mac mini per `CLAUDE.md` ("`full` — runs on always-on
  machine (Mac Studio/Mini)"). **This spec does not touch that mechanism**
  and does not duplicate it.
- The gap is specifically the two babysit scripts: they are standalone
  bash processes, not daemon loops. They never `import heartbeat` and are
  not started or supervised by `daemon.py` — they run independently
  (including while the daemon itself is stopped), invoked by hand or via
  cron on whichever machine the operator runs them from.
- `scripts/promote_local_features.py` is invoked via plain `python3
  "$PROMOTER"` (not the venv interpreter) and already imports `PyYAML`,
  so the machine's system `python3` has at least that dependency
  available; the new helper below is still written stdlib-only to avoid
  adding any dependency requirement at all.
- Neither babysit script is deployed by `install.sh` — both already run
  directly out of the repo checkout ("Run from inside the secondbrain repo
  root"). This spec does not need to touch `install.sh`.
- `circle_sync_scanner.py` only globs `MEMORIES_DIR` (`BRAIN_DIR/memories/`)
  for circle sharing; anything written at `BRAIN_DIR` root (like
  `heartbeat-*.json` today) is outside that scope and will not be
  synced into a shared Circle folder. The new status files must be
  placed at `BRAIN_DIR` root for the same reason.

### Assumptions (flagged — not verifiable from this checkout)

1. **Where the babysit scripts run vs. where home-lab-monitor runs.** The
   ticket says home-lab-monitor runs on the Mac mini, but does not say the
   babysit scripts run there too (`CLAUDE.md` describes the repo living on
   multiple machines, e.g. a MacBook `watcher` node). This spec writes the
   status artifact to **`BRAIN_DIR`** (iCloud Drive), not the
   machine-local `DEPLOY_DIR`, specifically so it is visible regardless of
   which machine the babysit script runs from, as long as home-lab-monitor
   can read the same iCloud-synced folder the daemon already reads
   `heartbeat-*.json` from. The write volume is one update per
   lifecycle event (script start, per outer-loop iteration, per review
   cycle, per terminal state) — minutes to hours apart — which is far
   below the 5-minute-loop churn the memory-cache spec
   (`specs/feat-memory-cache.md`) was built to absorb, so no read-cache or
   throttling layer is needed for this write path.
2. **home-lab-monitor's actual ingestion contract is unspecified.** No
   code, port, config file, or protocol reference to it exists in this
   checkout. This spec fixes a file path and JSON schema as the contract
   and treats pointing home-lab-monitor's config at that path as an
   out-of-repo follow-up for the user.
3. `~/repos/scripts/babysit.sh` (cited in `CHANGELOG.md` as the model for
   `work_reports.sh`) and the `babysit-work-prep` wrapper that generated
   *this* ticket are both outside this repository and out of scope. The
   schema below is generic (arbitrary `--script` slug, no hardcoded script
   list) specifically so those could adopt the same helper later without
   a schema change.

### Scope

**In scope:**
- A new stdlib-only helper, `scripts/babysit_status.py`, exposing an
  importable `update_status(...)` function plus a thin argparse CLI, that
  performs a **merge-and-write** of one script instance's status to
  `BRAIN_DIR/babysit-status-{hostname}-{script}.json` using the repo's
  standard atomic tmp-write + `rename()` pattern.
- Wiring both `scripts/babysit-with-review.sh` and `scripts/work_reports.sh`
  to call this helper at every lifecycle transition (start, iteration
  start, review-cycle phases, merge/deploy, pre-sleep, every terminal
  state, and an `EXIT` trap fallback for unexpected termination).
- A documented, stable JSON schema (below) as the integration contract for
  any external reader, including but not limited to home-lab-monitor.
- README.md documentation of the artifact's path, schema, and update
  cadence.
- Unit tests for the helper (`tests/unit/test_babysit_status.py`) and a
  structural regression test that asserts both shell scripts still call
  the helper at each required call site
  (`tests/unit/test_babysit_scripts_status_wiring.py`).

**Out of scope:**
- Any change to `home-lab-monitor` itself (not in this repo).
- Any change to `heartbeat.py`, `chat_handler.py`'s `/status` command, or
  the daemon's own loops — the daemon-side status story is already solved.
- A new Telegram command surfacing babysit status (the ticket's target
  surface is home-lab-monitor, not Telegram; nothing here blocks adding
  one later against the same JSON files).
- Networked status (HTTP endpoint, push webhook, Prometheus exporter) —
  file-based, matching every other cross-process status mechanism already
  in this repo.
- `install.sh` changes — babysit scripts already run uninstalled from the
  repo checkout.
- Retrying or queuing failed status writes — best-effort, matching
  `heartbeat.py`'s own `except Exception: log.warning(...)` philosophy.

### Success Metrics

- Every lifecycle transition of both babysit scripts produces a correctly
  updated `babysit-status-{hostname}-{script}.json` file within one
  process step (no batching delay).
- A script killed with `SIGKILL`, or one whose `claude`/`codex` subprocess
  hangs, still leaves behind a status file whose `updated_at` age alone
  tells an external reader "this stopped mattering a while ago" — no
  reliance on the lock file, which is not cleaned up on `SIGKILL`.
- Zero new runtime dependencies; zero changes to `daemon.py`, `heartbeat.py`,
  or `install.sh`.

## API / Artifact Contract

### File path

```
BRAIN_DIR/babysit-status-{hostname}-{project}-{script}.json
```

- `BRAIN_DIR` = `~/Library/Mobile Documents/com~apple~CloudDocs/second-brain`
  (same constant, same literal path, duplicated the same way it already is
  in every other module — see "Known facts").
- `{hostname}` = `socket.gethostname().split(".")[0]`, identical derivation
  to `heartbeat.py`.
- `{project}` = `$PROJECT` (`basename "$PWD"` at script start, already
  computed by both scripts today and used in their own log/lock filenames)
  passed through as `--project`. **Required**, not cosmetic: the existing
  `.stop` lock file is scoped per-project
  (`${PROJECT}-babysit-with-review.stop`), and Assumption 3 explicitly
  invites reuse of this helper by `~/repos/scripts/babysit.sh` running the
  same script name against a *different* checkout on the same host. Without
  `{project}` in the filename, two such instances would race on one file,
  breaking Invariant 2 below. `project` is also a top-level field in the
  schema for the same reason `script`/`hostname` are — so a glob-only
  consumer doesn't have to parse the filename.
- `{script}` = the value passed via `--script`, restricted to
  `^[a-z0-9-]+$` (e.g. `babysit-with-review`, `work-reports`). Deliberately
  not a hardcoded enum — see Assumption 3.
- One file per (hostname, project, script) triple. **Not** named
  `heartbeat-*.json` and **not** merged into the daemon's existing
  heartbeat file — see "Rejected alternative" below.

### Rejected alternative: reusing `heartbeat-{hostname}.json`

Naming the file to match `heartbeat.read_all()`'s `heartbeat-*.json` glob
would make it show up in the Telegram `/status` command for free. This is
rejected: `cmd_status` hardcodes a 600-second staleness threshold
(`age_s > 600` → `[STALE]`) tuned for loops that run every 5 minutes. A
single `claude -p` outer-loop iteration routinely takes longer than 10
minutes, so every babysit run would falsely flag as stale in `/status`.
Keeping a separate file with its own schema avoids corrupting that
existing, working display.

### JSON schema

```json
{
  "hostname": "mac-mini",
  "project": "felix",
  "script": "babysit-with-review",
  "pid": 41213,
  "status": "review",
  "detail": "iter 4/20: PR #142 review cycle 2/3 (codex pass)",
  "iteration": 4,
  "max_iter": 20,
  "started_at": "2026-09-09T14:00:03+00:00",
  "updated_at": "2026-09-09T14:12:41+00:00",
  "last_error": null,
  "last_error_at": null,
  "log_path": "/Users/chrisrobertson/sisyphus-logs/felix-babysit-with-review-20260909-140003-41213.log",
  "lock_path": "/Users/chrisrobertson/sisyphus-logs/felix-babysit-with-review.stop"
}
```

Field notes:

| Field | Type | Notes |
|---|---|---|
| `hostname` | string | Set once per file; never changes after creation. |
| `project` | string | Matches `--project` (`$PROJECT`, i.e. `basename "$PWD"` at script start); redundant with the filename but kept inline for the same reason `script` is. Part of the file's identity key alongside `hostname`/`script` — see File path. |
| `script` | string | Matches `--script`; redundant with filename but kept inline so a directory-glob consumer doesn't need to parse filenames. |
| `pid` | int \| null | PID of the bash process, set at the `starting` call. Consumers **may** cross-check liveness (`kill -0`) only when reading on the same host as `hostname` — cross-host PID numbers are meaningless and must not be interpreted. |
| `status` | string enum | Current lifecycle phase (see enum below). Always reflects the *current* phase, not history. |
| `detail` | string \| null | Free-text, human-readable progress note. Truncated to 2000 chars (see Bounds). |
| `iteration` / `max_iter` | int \| null | Outer-loop counters. `work_reports.sh` (which has no review-cycle sub-loop) still sets these; review-cycle progress is folded into `detail` text rather than adding more structured fields. |
| `started_at` | ISO 8601 string | Set once, at the `starting` call for this process. |
| `updated_at` | ISO 8601 string | Refreshed on **every** call, unconditionally — this is the field a monitor uses for staleness, not `started_at`. |
| `last_error` | string \| null | **Sticky**: once set, it is not cleared by unrelated status/detail updates. Only cleared by the next `starting` call (fresh process) or an explicit `--clear-error`. This means "the loop is currently healthy again but something failed at `last_error_at`" is visible instead of silently disappearing on the next successful iteration. |
| `last_error_at` | ISO 8601 string \| null | Set whenever `last_error` is set; `null` iff `last_error` is `null`. |
| `log_path` | string \| null | Absolute path to the current run's log file, for a human (or a monitor with log-tailing capability) to drill in. |
| `lock_path` | string \| null | Absolute path to the `.stop` lock file, for a human to know how to signal a graceful stop (`rm <lock_path>`). |

### `status` enum

| Value | Meaning | Terminal? |
|---|---|---|
| `starting` | Lock acquired; helper copied to temp path; about to run the promoter and then enter the outer loop. | no |
| `running` | Executing one outer-loop iteration (`collect_state` + `claude -p`). | no |
| `review` | Inside `run_review_cycle` (codex or claude review pass). `babysit-with-review.sh` only. | no |
| `merging` | Inside `merge_and_deploy` (`gh pr merge` + `install.sh`). `babysit-with-review.sh` only. | no |
| `sleeping` | Between iterations, in the `sleep "$SLEEP_SEC"` window. | no |
| `stopped_ok` | Claude emitted `STOP`; empty backlog. | yes |
| `stopped_stuck` | Stuck-loop guard fired (`STUCK_N` identical results). | yes |
| `stopped_maxiter` | `MAX_ITER` reached. | yes |
| `stopped_error` | A hard failure broke the outer loop (`claude` non-zero exit) or the promoter step failed before the outer loop was ever entered (`python3 "$PROMOTER"` returning non-zero, which today does `exit 1` immediately). | yes |
| `stopped_interrupted` | `EXIT` trap fired without any of the above having been written first (kill signal, crash, unhandled bash error). | yes |

A "terminal" status is one after which no further updates are expected
from that PID; a monitor should treat the file as describing a finished
run once one of these lands, not as stale/broken.

### CLI contract (`scripts/babysit_status.py`)

```
python3 babysit_status.py --script SLUG --project NAME [--status ENUM] [--detail TEXT]
    [--iteration N] [--max-iter N] [--pid N] [--started-at ISO]
    [--log-path PATH] [--lock-path PATH]
    [--last-error TEXT] [--clear-error]
    [--brain-dir PATH] [--hostname NAME]
```

- `--script` and `--project` are the only required arguments. `--project`
  is always passed as `"$PROJECT"` (the value both scripts already compute
  as `basename "$PWD"`) — see File path for why this is load-bearing, not
  optional.
- Every call site in both shell scripts redirects the helper's stderr into
  the run log: `python3 "$STATUS_HELPER" ... 2>>"$LOG" || true`. This is
  required, not a suggestion — see Telemetry.
- Every other field argument is **optional and merge-semantics**: if
  omitted, the existing value already on disk for that (hostname, script)
  is preserved untouched. `updated_at` is the only field always refreshed,
  on every call.
- `--last-error TEXT` sets both `last_error` and `last_error_at = now`.
  `--clear-error` explicitly resets both to `null` (used only in the
  `starting` call, so a fresh process doesn't inherit a stale error from a
  previous run — see "sticky" note above). Passing both is invalid (exit 2).
- `--status` and `--last-error`/`--clear-error` are independent: a
  transient failure inside `run_review_cycle` or `merge_and_deploy` that
  does **not** end the outer loop calls the helper with only
  `--last-error TEXT` (no `--status`), leaving `status` at whatever
  phase the outer loop is actually in.
- `--brain-dir` / `--hostname` exist only to make the helper unit-testable
  without touching the real iCloud path or the real machine's hostname
  (mirrors how tests patch `MEMORIES_DIR` / `BRAIN_DIR` elsewhere in this
  repo per `CLAUDE.md`).
- Argument-parsing errors (missing `--script`, invalid `--status` choice,
  both `--last-error` and `--clear-error` given) are **caller bugs** —
  argparse's default behavior applies: print usage to stderr, exit 2. This
  is intentional so a typo in the wiring is loud during development.
- Any *runtime* failure (BRAIN_DIR unwritable, disk full, malformed
  existing JSON on disk) is caught, logged to stderr, and the process
  **still exits 0** — this is a best-effort side channel and must never be
  the reason the babysit loop dies. Every call site in both shell scripts
  additionally appends `|| true` as defense in depth.
- On a read failure of the existing file (corrupt JSON, unexpected
  content), the helper logs a warning to stderr and proceeds as if no
  prior file existed (starts from `hostname`/`script`/`pid` defaults plus
  whatever this call's arguments provide) rather than crashing or refusing
  to write — self-healing, matching the "never block the primary
  workflow" principle.

## Invariants

1. **Status writes must not depend on the currently checked-out git
   branch.** Both `run_review_cycle` (`gh pr checkout <pr_num>`) and
   `merge_and_deploy` (`git checkout main`) replace the *entire working
   tree in place* — if an open PR predates this feature (a realistic case,
   since "advance open PRs" is priority #1 in both scripts' base prompts),
   `scripts/babysit_status.py` will not exist on disk during that window,
   and a relative-path invocation would fail. **Mitigation, required by
   this spec:** immediately after the lock-file *check* (the existing
   `if [ -f "$STOP_FILE" ]; then ... exit 1; fi` block), and **before**
   the promoter step (`promote_local_features.py`) runs, each script
   copies the helper to a temp path (`STATUS_HELPER=$(mktemp); cp
   "$(dirname "$0")/babysit_status.py" "$STATUS_HELPER"`) and calls
   `python3 "$STATUS_HELPER" ...` for the remainder of the process. This
   ordering — before the promoter, not merely before the first git
   checkout — is required so a promoter failure (which today `exit 1`s
   the whole script before the outer loop ever starts) can still be
   recorded (see `stopped_error` below). The temp copy is removed in the
   existing `trap ... EXIT` alongside the other temp files. This is the
   same defense `promote_local_features.py` gets "for free" only because
   it happens to run before any checkout — that accident must not be
   relied on for the new helper.
2. **No concurrent writers to the same file.** Each script instance writes
   only its own `babysit-status-{hostname}-{project}-{script}.json`; the
   existing `.stop` lock file is scoped the same way
   (`${PROJECT}-babysit-with-review.stop`), so folding `project` into the
   status filename keeps the two identity keys aligned and guarantees at
   most one writer per file — including the case Assumption 3 invites,
   where the same script name runs against two different checkouts
   (different `$PROJECT`) on one host. `babysit-with-review.sh` and
   `work_reports.sh` also write to different files (different `{script}`
   slugs), so no cross-script race exists either. No file locking
   (`flock`) is needed as a result — do not add it.
3. **`updated_at` always advances.** Every helper invocation, regardless
   of which fields it changes, refreshes `updated_at` to the current UTC
   time. This is what lets an external reader compute staleness without
   needing to reason about which specific field last changed.
4. **The `EXIT` trap always leaves a terminal status.** If none of the
   normal terminal-status call sites ran before the trap fires (kill
   signal, unhandled error, crash), the trap writes `stopped_interrupted`
   with a `last_error` describing that. Track this with a plain shell
   variable, e.g. `FINAL_STATUS_WRITTEN=0`, flipped to `1` immediately
   after each of the five normal terminal writes; the trap checks it
   before deciding whether to write the fallback. The trap's status write
   must happen **before** it removes `$STATUS_HELPER` (order matters:
   write first, clean up temp files second).
5. **`last_error` is sticky across non-terminal calls.** A field omitted
   from the CLI call is preserved, not cleared — this applies especially
   to `last_error`/`last_error_at`, which must persist until the next
   `starting` call (`--clear-error`) so an operator or monitor can see
   "recovered, but something failed at `last_error_at`" rather than the
   error silently vanishing on the next successful iteration.

## Idempotency

Re-running the same CLI invocation twice (e.g. a retried call after a
transient write failure) is safe: it produces the same field values except
`updated_at`, which advances. There is no create-vs-update distinction to
get wrong — the merge-and-write is unconditional upsert keyed by
(hostname, script), matching `memory_writer.py`'s stated philosophy that
"the second write wins with the same content" for its own atomic-overwrite
case.

## Error Model

| Failure | Handling |
|---|---|
| `BRAIN_DIR` does not exist yet (first run before daemon has created it) | Helper calls `Path.mkdir(parents=True, exist_ok=True)` before writing, matching `memory_writer.write()`'s own `MEMORIES_DIR.mkdir(parents=True, exist_ok=True)` pattern. |
| Existing status file is corrupt / not valid JSON | Logged as a warning to stderr; treated as absent, write proceeds from defaults + this call's args. |
| Write fails (disk full, iCloud `EAGAIN`/`EDEADLK`, permission error) | Caught, logged to stderr, process still exits 0. Next lifecycle event will simply try again; no retry loop, no queue — matches `heartbeat.py`'s `_flush()` exactly (`except Exception: log.warning(...)`). |
| Bash caller passes malformed arguments (missing `--script`, bad `--status` value, both `--last-error` and `--clear-error`) | argparse error, exit 2, message to stderr — a caller bug, meant to be loud in development, not swallowed. |
| Babysit script killed with `SIGKILL` (trap cannot run) | No new status write occurs; `updated_at` simply stops advancing. This is the intended signal for a monitor — "last update N minutes/hours ago" — since PID liveness across the two potentially-different `hostname`s (babysit host vs. monitor host) cannot be assumed. |
| `python3` itself missing or broken on the box running the babysit script | Every call site is `... || true`; the babysit script's primary workflow (backlog draining) continues uninterrupted with no status reporting rather than failing. |

## Security

- No new secrets, tokens, or credentials are introduced or handled by this
  feature.
- No new network surface: purely local file writes, no listener, no
  outbound call.
- `detail` and `last_error` are free text drawn from this repo's own
  script output (iteration counters, PR numbers, codex/claude exit
  messages) — the same class of information already visible in the plain
  log file the babysit scripts already write to
  `~/sisyphus-logs/*.log`, and less sensitive than what regularly lands in
  code/email/meeting memory files under `MEMORIES_DIR`. No additional
  redaction is required beyond the existing truncation bound (below).
- Placed at `BRAIN_DIR` root (like `heartbeat-*.json`), not under
  `MEMORIES_DIR`, so it is explicitly outside `circle_sync_scanner.py`'s
  glob scope and can never be synced into a shared Circle folder.
- Do not log full PR/issue titles or arbitrary user-authored issue body
  text into `detail`/`last_error` — only script-generated progress
  strings (iteration counters, PR/issue numbers, exit codes, sentinel
  values). This keeps the artifact's content class equivalent to what a
  process-monitoring tool would normally see, not user content.

## Telemetry

This feature *is* the telemetry — there is no additional logging
requirement beyond what's specified above, with one wiring rule: every
call site in both shell scripts must redirect the helper's stderr into
the script's own run log — `python3 "$STATUS_HELPER" ... 2>>"$LOG" ||
true` — not to the terminal. The helper swallows and logs runtime
failures (see Error Model) rather than propagating them; without this
redirect, a silently-failing status write would be invisible to anyone
reviewing `$LOG` after the fact, which defeats the point of making status
writes best-effort instead of fatal. The existing `~/sisyphus-logs/*.log`
files remain the detailed audit trail; the new JSON file is a compact
current-state snapshot derived from the same lifecycle events, not a
replacement for the log.

## Bounds

- `detail` and `last_error` are truncated to 2000 characters each before
  writing (prevents an accidental full codex-review dump from bloating an
  iCloud-synced file that's supposed to stay small and cheap to sync,
  consistent with this repo's general "keep files small" bias — see
  `CLAUDE.md`'s ~6KB memory-file guidance for the same underlying
  concern).
- One file per (hostname, script) pair, overwritten in place — no
  unbounded growth over the life of the repo.
- Update frequency is event-driven (lifecycle transitions), not
  timer-driven — at minimum one write per outer-loop iteration, at most a
  handful of writes per review cycle. This stays far below the polling
  cadence (5 minutes) that motivated `feat-memory-cache.md`'s read-cache
  layer; this write path does not need one.

## Failure Modes Summary

| Scenario | Observable via this artifact? |
|---|---|
| Script running normally | `status` cycles through `starting`→`running`→(`review`/`merging`)→`sleeping`→`running`→...; `updated_at` advances on each lifecycle event. Gaps of tens of minutes with no update are normal and expected *within* a single `running`/`review` phase — a `claude -p` or `codex exec` call routinely runs 30+ minutes with no intermediate write. A monitor must not treat that alone as staleness; see Bounds. |
| Script finished (empty backlog) | `status: stopped_ok`, `updated_at` frozen at that time. |
| Script stuck-looped and bailed | `status: stopped_stuck`. |
| Script hit `MAX_ITER` | `status: stopped_maxiter`. |
| `claude -p` hard-failed | `status: stopped_error`, `last_error` set with the failure context. |
| Review cycle failed but outer loop continued (e.g. codex non-zero, `STUCK_REVIEW`) | `status` remains `running`/`sleeping` (whatever the outer loop is doing), `last_error` set, `last_error_at` recent — visible as "still going, but something failed recently." |
| Process killed with `SIGKILL` / machine slept / crashed | No trap runs; `updated_at` simply stops advancing. A monitor's own staleness policy (unspecified here — home-lab-monitor's concern) is what turns "old `updated_at`" into an alert. |
| `BRAIN_DIR` (iCloud) temporarily unavailable | Write fails, is logged and swallowed; babysit workflow is unaffected; next lifecycle event retries the write. |

## Acceptance Tests

### `tests/unit/test_babysit_status.py` (new; mirrors `tests/unit/test_heartbeat.py` and `tests/unit/test_promote_local_features.py`)

1. First call for a (hostname, script) pair with `--status starting --pid N
   --started-at T` creates `babysit-status-{host}-{script}.json` with those
   fields, `last_error`/`last_error_at` null, and `updated_at` set.
2. A second call with only `--detail "..."` preserves `pid`, `started_at`,
   `status` from the first call untouched, updates `detail`, and advances
   `updated_at`.
3. A call with `--last-error "boom"` sets `last_error`/`last_error_at`;
   a subsequent call with only `--detail "..."` (no `--status`, no
   `--clear-error`) leaves `last_error` unchanged (sticky behavior).
4. A call with `--clear-error` resets `last_error`/`last_error_at` to
   `null` without requiring `--status`.
5. Passing both `--last-error` and `--clear-error` exits 2 and writes no
   file / leaves any existing file untouched.
6. An invalid `--status` value (not in the enum) exits 2 via argparse.
7. Missing `--script` exits 2 via argparse.
8. Writing over a pre-existing file containing corrupt JSON logs a warning
   (captured via `caplog` or stderr capture) and still succeeds, producing
   a valid file from defaults + the call's arguments.
9. Write happens via tmp-file + rename (assert no `.tmp` file survives
   after a successful call, mirroring `test_atomic_write_leaves_no_tmp_file`
   pattern already used for `memory_writer`).
10. `detail` longer than 2000 characters is truncated in the written file.
11. Two independent (hostname, script) pairs never collide — writing for
    `script="a"` does not affect an existing file for `script="b"` on the
    same host.
12. `--brain-dir`/`--hostname` overrides are honored (used by every test
    above via `tmp_path`, never touching the real iCloud path — per
    `CLAUDE.md` testing conventions).

### `tests/unit/test_babysit_scripts_status_wiring.py` (new; structural/text-level, analogous in spirit to the AST-level invariant checks in `tests/unit/test_memory_cache_migration.py`)

Bash has no AST tooling available in this repo, so these are text/regex
assertions against the two `.sh` files' source, verifying required call
sites exist and are correctly ordered — protecting against someone
deleting a call site or reintroducing the branch-checkout hazard:

1. Both scripts contain a line copying the helper to a temp path
   (`cp ... babysit_status.py ...`) and that line's position in the file
   is **before** the first occurrence of `gh pr checkout` and `git checkout`.
2. Both scripts reference the temp copy variable (e.g. `$STATUS_HELPER`),
   not a relative `scripts/babysit_status.py` path, at every call site
   after the copy.
3. Both scripts contain a call with `--status starting` and, on the same
   line or an adjacent one, `--clear-error`.
4. `babysit-with-review.sh` contains calls with `--status review` (inside
   `run_review_cycle`) and `--status merging` (inside `merge_and_deploy`).
   `work_reports.sh` has no review/merge sub-loop, so it is exempt from
   this assertion entirely — the test only checks `babysit-with-review.sh`
   for these two states.
5. Both scripts contain calls with each of `--status stopped_ok`,
   `--status stopped_stuck`, `--status stopped_maxiter`, and
   `--status stopped_error` somewhere in their respective terminal
   branches.
6. Both scripts define a `FINAL_STATUS_WRITTEN`-style guard variable, set
   it at each terminal call site, and check it in the `trap ... EXIT`
   handler before writing `stopped_interrupted`.
7. Every status-helper invocation in both scripts is followed by `|| true`.

### Manual / integration verification (documented, not automated — no sandbox here can run multi-hour `claude -p`/`codex exec` loops)

- Run `scripts/work_reports.sh` (simpler of the two, no review cycle)
  against a repo with at least one open `kind:bug`/`kind:feature` issue
  and confirm `babysit-status-{host}-work-reports.json` appears within
  seconds of start, cycles `status` correctly across one iteration, and
  reaches a terminal status when manually stopped via `rm <lock_path>`.
- Kill a running instance with `kill -9` and confirm the file is left
  behind with a frozen `updated_at` and no fabricated terminal status
  (this is expected — trap cannot run on `SIGKILL`; see Failure Modes).

## Documentation

Add a new README.md subsection near the existing babysit-scripts
documentation (README.md:520, "Working the backlog autonomously")
describing:
- The artifact path and schema (link to or summarize this spec's schema
  table).
- That it's intended as an integration point for external monitoring
  tools (naming home-lab-monitor as the motivating example, per the
  source ticket), while being explicit that wiring any specific external
  tool is the operator's responsibility, not something this repo
  automates.
- The `status` enum and what "terminal" means for a monitor's staleness
  logic.

## Versioning

Per `CLAUDE.md`: this is a new user-visible capability (new artifact +
README section) → **MINOR** bump (`1.19.0` → `1.20.0`) at release time,
batched with whatever else is in `[Unreleased]` at that point — not a
bump for this change alone. A `CHANGELOG.md [Unreleased] → Added` entry
should land with the implementing commit(s) regardless of when the version
bump itself happens.
