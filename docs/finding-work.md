# Finding Work — Bugs and Feature Requests

Reference for Claude Code sessions and the `scripts/work_reports.sh` autonomous drainer.
For the human-operator Telegram workflow see `README.md § Feature and bug tracking`.

---

## TL;DR

1. **Check if GitHub backing is on** (§1) — if yes, GitHub Issues is authoritative.
2. **GitHub is on?** → use `gh issue list` recipes (§2).
3. **GitHub is off / pre-promotion?** → glob `feature-request-*.md` in iCloud memories (§3).
4. **Drain automatically?** → `scripts/work_reports.sh` (§6).

---

## 1. Is GitHub backing enabled?

Both must be set:

| What | Where it comes from |
|------|---------------------|
| `GITHUB_PAT` | Keychain item `secondbrain-github_pat`, else env var `GITHUB_PAT` |
| `GITHUB_REPO` | `GITHUB_REPO` env var, else `config.yaml § github.repo` |

Enablement check: `github_client.py:38-40` (`enabled` property — requires non-empty PAT, non-empty repo, and a `/` in the repo string).

Handler init: `chat_handler.py:248-256`.

**If either is absent** → local `feature-request-*.md` files are the only source of truth.

---

## 2. GitHub Issues (primary source when enabled)

### Label vocabulary

Defined at `github_client.py:12-21`. Three namespaces:

```
kind:feature          kind:bug
status:planned        status:in-progress
priority:low          priority:medium    priority:high    priority:critical
```

Hashtags in the original `/feature` or `/bug` description become plain labels
(e.g. `#auth`, `#performance`).

### Closed-state semantics

Closed issues carry no `status:` label; kind is inferred from `state_reason`
(`chat_handler.py:4473-4479`):

| `state_reason` | Displayed status |
|----------------|-----------------|
| `completed`    | `done`          |
| `not_planned`  | `wont-do`       |

### `gh` CLI recipes (read-only)

```bash
# All open bugs
gh issue list --label kind:bug --state open

# Open features, high priority
gh issue list --label kind:feature --label priority:high --state open

# Anything currently in-progress
gh issue list --label status:in-progress --state open

# Full detail + comments for one issue
gh issue view <NNN> --comments

# All open items regardless of kind, newest first
gh issue list --state open --limit 50
```

### `features-index.md` snapshot

`memories/features-index.md` mirrors the open and recently-closed lists.
Rewritten by `_rewrite_features_index_snapshot` (`chat_handler.py:4532-4568`) after every
mutating Telegram command. Useful when you want a quick read without hitting the API.
Frontmatter: `type: feature_request_index`.

---

## 3. Local files (fallback / pre-promotion)

### Location

```
~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/memories/feature-request-{slug}-{6char}.md
```

`BRAIN_DIR` constant: `chat_handler.py:27`.

### Frontmatter schema

Written by `cmd_feature` (`chat_handler.py:5059-5068`) and `cmd_bug` (`5121-5130`).

| Field | Values |
|-------|--------|
| `type` | `feature_request` — the discriminator used by every loader |
| `kind` | `feature` \| `bug` |
| `status` | `new` → `planned` → `in-progress` → `done` \| `wont-do` |
| `priority` | `low` \| `medium` \| `high` \| `critical` (default: `medium`) |
| `short_id` | 6-char sha1 hash — used by `/feature_detail` and the `close_issue` tool |
| `github_issue_number` | integer — present only after promotion to GH |

### Quick grep recipes

```bash
cd "~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/memories"

# All open bugs
grep -rl "kind: bug" . --include="feature-request-*.md" | xargs grep -l "status: new"

# Any un-promoted items (no github_issue_number)
grep -rL "github_issue_number" . --include="feature-request-*.md"

# By priority
grep -rl "priority: high" . --include="feature-request-*.md"
```

### Archive

After promotion via `/feature_import` or `scripts/promote_local_features.py`, files are
moved to `memories/archive/` and their frontmatter gains `github_issue_number`.
Both promoters skip files that already have that field (`chat_handler.py:5418-5481`).

---

## 4. Telegram commands

Canonical definition: `command_core.py:107-122`. Handlers: `chat_handler.py:5017-5481`.

| Command | Purpose |
|---------|---------|
| `/feature <desc>` | Capture feature request (GH issue or local file) |
| `/bug <desc>` | Capture bug report |
| `/features [bug\|feature\|<status>\|all]` | List backlog — default hides `done`/`wont-do` |
| `/bugs` | Alias for `/features bug` |
| `/feature_detail N\|#NNN` | Full detail; supports verb dispatch (`/feature_detail done 3`) |
| `/feature_plan N` | Mark planned |
| `/feature_start N` | Mark in-progress |
| `/feature_done N [note]` | Close as completed |
| `/feature_wont_do N [reason]` | Close as not-planned |
| `/feature_priority N <level>` | Replace priority label |
| `/feature_note N <text>` | Add timestamped note |
| `/feature_import [confirm]` | One-time local→GH migration (requires GH backing) |

---

## 5. LLM tool entry points

Used when the `chat` skill invokes tools rather than slash commands directly.

| Tool | Schema | Does |
|------|--------|------|
| `add_feature` | `chat_tools.py:218-237` | File a feature request |
| `add_bug` | `chat_tools.py:238-257` | File a bug report |
| `close_issue` | `chat_tools.py:284-313` | Update `status:` on a local file |

**`close_issue` behaviour:** calls `_close_issue_text` in `chat_handler.py`. When the
memory file carries a `github_issue_number`, the GitHub issue is updated first (via
`_gh_set_status`); the local file is only written if that succeeds. For files without a
GitHub number the update is local-only.

---

## 6. Autonomous drainer

### `scripts/work_reports.sh`

Runs `promote_local_features.py`, then loops `claude -p` over open issues until one of:
- Claude outputs `STOP` (no actionable work left)
- Loop stalls on the same result `STUCK_N` times
- `MAX_ITER` is reached

Logs to `~/sisyphus-logs/`. Graceful stop: `rm ~/sisyphus-logs/secondbrain-work-reports.stop`.

```bash
scripts/work_reports.sh               # MAX_ITER=20, SLEEP_SEC=10, STUCK_N=3
MAX_ITER=5 scripts/work_reports.sh
```

Requires: `gh` authenticated against the target repo, `claude` CLI on PATH.

### `scripts/promote_local_features.py`

Standalone one-shot promoter — mirrors `/feature_import` but uses the `gh` CLI directly,
no running daemon required.

```bash
python scripts/promote_local_features.py --dry-run   # preview without touching anything
python scripts/promote_local_features.py             # promote all un-promoted local files
```

---

## 7. Gotchas

- **`close_issue` tool is local-only** — it does not call the GitHub API (see §5 caveat).
- **`index_builder.py` does not write `features-index.md`** — that file is written by the
  chat handler (`_rewrite_features_index_snapshot`) and only when GitHub backing is enabled.
- **No separate `feature-requests/` folder** — all files are in `memories/` alongside other
  memory types, distinguished by the `feature-request-` filename prefix.
- **Spec files** (read-only historical context):
  - `specs/feat-feature-tracker.md` — local-file tracker, shipped v1.3.1
  - `specs/feat-close-issues-tool.md` — `close_issue` tool, shipped v1.4.0
