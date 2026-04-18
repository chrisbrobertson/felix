---
specmas: 3.0
kind: feature
id: feat-code-project-scanner
version: 1.1.0
created: 2026-04-11
status: implemented
shipped_version: "1.3.0"
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-memory-management
---

# Code Project Scanner

## Overview

### Problem Statement

The bot accumulates memories from web browsing but has no awareness of what the
user is actively building. With 29 git repos on the local machine, the bot
cannot answer questions like "what was I working on this week?", "what changed
in the secondbrain repo?", or "which projects use LiteLLM?". Project context is
as important as reading context for a personal second brain.

### Scope

**In Scope:**
- Periodic scan of `~/repos/` and `~/repo/` for git repositories
- One living memory file per project, updated in place on each scan
- Git-derived metadata: remote URL, branch, recent commits, HEAD sha
- Language detection via file extension heuristic
- LLM-generated summary from README.md (first scan or README change only)
- LLM-generated tags (first scan or README change only)
- Related-project detection via shared languages, git org, and import references
- Change detection via HEAD sha — skips write when nothing changed
- Configurable via `project_scanner` section in config.yaml

**Out of Scope:**
- Non-git project directories
- Submodule traversal
- Diff content (commit messages only, not file diffs)
- Private repo content beyond what `git log` exposes
- Cross-machine project sync (memories sync via iCloud automatically)

### Success Metrics

- 29 repos produce 29 `project-*.md` memory files within the first scan cycle
- Unchanged repos produce zero file writes on subsequent scans
- Each memory file scannable by the chat bot header cache (source_title, summary,
  tags, last_scanned within first 200 chars)
- LLM called at most once per repo per README change

---

## Functional Requirements

### FR-1: Repository discovery
**Priority:** Critical

Glob `~/repos/*/` and `~/repo/*/` for directories containing a `.git/`
subdirectory. Apply `skip_repos` filter from config. Resolve `~` to the
actual home directory. Directories without `.git/` (e.g., `node_modules/`,
archives, tarballs) are silently ignored.

---

### FR-2: Per-repo metadata extraction
**Priority:** Critical

For each discovered repo, extract via subprocess:

| Field | Command |
|-------|---------|
| `remote_url` | `git -C <path> remote get-url origin` (fallback: local path) |
| `head_sha` | `git -C <path> rev-parse HEAD` |
| `default_branch` | `git -C <path> symbolic-ref --short refs/remotes/origin/HEAD` → strip `origin/`; fallback: `git -C <path> branch --show-current` |
| `recent_commits` | `git -C <path> log -10 --format="%h %ad %s" --date=short` |
| `branches` | `git -C <path> branch --format="%(refname:short)"` |

All subprocess calls use `timeout=5` and return empty/fallback values on error.
Never raise — a broken repo must not abort the full scan.

---

### FR-3: Language detection
**Priority:** High

Count file extensions in the repo root and one level deep (`src/`, `lib/`,
`app/`). Map to language names:

| Extensions | Language |
|------------|----------|
| `.py` | python |
| `.ts`, `.tsx` | typescript |
| `.js`, `.jsx`, `.mjs` | javascript |
| `.go` | go |
| `.rs` | rust |
| `.rb` | ruby |
| `.java`, `.kt` | java |
| `.cs` | csharp |
| `.sh`, `.bash` | shell |
| `.yaml`, `.yml`, `.json` | config |

Return the top 3 languages by file count. Skip `node_modules/`, `.git/`,
`venv/`, `__pycache__/`.

---

### FR-4: Summary and tags generation
**Priority:** High

On first scan (no existing memory file) or when README.md mtime has changed
since last scan:
- Read up to 3000 chars of README.md
- Call LiteLLM with a short inline prompt (not a full skill file) to generate:
  - A 1-2 sentence summary
  - 3-6 lowercase tags (language + domain keywords)
- Cache result in the written memory file (no separate cache file needed)

When README.md has not changed: reuse `summary` and `tags` from the existing
memory file frontmatter. Do not call the LLM.

When no README.md exists: use the most recent commit message as the summary.
Tags are derived from detected languages only.

---

### FR-5: Related project detection
**Priority:** Medium

For each project, find up to 5 related projects using lightweight heuristics
(no LLM):

1. **Same git org**: extract org from remote URL; match other repos with same org
2. **Shared languages**: repos with ≥1 matching language in `languages` field
3. **Import references**: grep `requirements.txt`, `package.json`,
   `go.mod`, `pyproject.toml` for other repo names in the discovered set

Sort candidates by relevance score (org match = 3pts, import ref = 2pts,
shared language = 1pt). Return top 5 with their summary (from existing memory
or first commit message as fallback).

---

### FR-6: Change detection
**Priority:** Critical

Before writing a memory file, compare `git rev-parse HEAD` against the
`head_sha` stored in the existing memory file's frontmatter. If equal, skip
the write entirely. This keeps the 5-minute poll cheap (~30ms for 29 repos).

Always write on first scan (no existing memory file) or when README.md mtime
changed (triggers summary/tag regeneration).

---

### FR-7: Memory file write
**Priority:** Critical

Write `BRAIN_DIR/memories/project-{name}.md` atomically (tmp + rename).
Field order in frontmatter:

```
source_title, summary, tags, last_scanned,
source_url, type, category, local_path, default_branch, languages, head_sha
```

`type` is always `project`. `category` is always `code` for this scanner.
This generalises the former `code_project` type — future scanners may write
`category: person` or `category: work` under the same `type: project` umbrella.

---

## Memory File Format

```markdown
---
source_title: secondbrain
summary: Personal knowledge system — browser history watcher, Telegram bot, iCloud-synced memories.
tags: [python, telegram, llm, personal-tools]
last_scanned: '2026-04-11T14:30:00'
source_url: git@github.com:chrisrobertson/secondbrain.git
type: project
category: code
local_path: /Users/chrisrobertson/repos/secondbrain
default_branch: main
languages: [python]
head_sha: e7c75b9a1234567890abcdef
---

## Description
Automatically captures and summarizes everything you read on the web.
Stores summaries as flat markdown files in iCloud Drive.

## Recent Activity
- 2026-04-11 e7c75b9 — Deploy source files to ~/secondbrain/ instead of running from the repo
- 2026-04-11 9307d63 — Add /memories, /search, /memory, /delete Telegram commands
- 2026-04-11 b5e1299 — Put source_title/source_url/summary first in memory frontmatter
- 2026-04-11 7e73ad3 — Fix /purge missing memories whose source_url falls past 500-char read limit
- 2026-04-11 54a95d2 — Add /skip, /unskip, /skiplist, /purge, /purgeall Telegram commands

## Related Projects
- **EA-AI** — Executive assistant system with calendar intelligence and Slack integration
- **hermes** — MCP server framework and agent orchestration

## Active Branches
- main (current)
- feat/something-in-progress
```

---

## Configuration

Add to `config.yaml`:

```yaml
project_scanner:
  interval_seconds: 300    # scan every 5 minutes
  repo_dirs:               # directories to search for git repos
    - ~/repos
    - ~/repo
  skip_repos: []           # repo directory names to ignore (e.g. [archived-project])
```

---

## Implementation Notes

### Module: `project_scanner.py`

```
class ProjectScanner:
    __init__(self, role)
    async run_loop(self, stop_event)
    _discover_repos(self) -> list[Path]
    _git(self, path, *args) -> str          # subprocess helper, timeout=5, returns "" on error
    _scan_repo(self, path) -> dict
    _detect_languages(self, path) -> list[str]
    _find_related(self, name, tags, languages, remote_url, all_projects) -> list[dict]
    _needs_update(self, path, memory_path) -> bool
    async _generate_summary_and_tags(self, readme, commits) -> tuple[str, list[str]]
    _write_memory(self, data)
```

### Daemon integration

`project_scanner.py` is a full-role module (Telegram, index, optimizer, and
scanner all require the full node). Import inside the `if role == "full"` block
in `daemon.py`.

### Subprocess safety

All `git` calls are wrapped in try/except with `timeout=5`. A repo that is
mid-commit, locked, or on a slow filesystem must not block the scan loop or
raise to the daemon level.

---

## Files Modified

| File | Change |
|------|--------|
| `project_scanner.py` | **Create** |
| `daemon.py` | Add ProjectScanner to full-role gather |
| `config.yaml.template` | Add `project_scanner` section |
| `install.sh` | Add `project_scanner.py` to DAEMON_FILES |
| `README.md` | Document fifth async loop |
| `CLAUDE.md` | Update loop count and add project_scanner description |
| `tests/unit/test_project_scanner.py` | **Create** |
| `tests/integration/test_pipeline.py` | Add scanner integration test |

---

## Testing

### Unit tests

| Test | Assertion |
|------|-----------|
| `test_discover_repos_finds_git_dirs` | Returns only dirs with .git/ |
| `test_discover_repos_skips_configured` | skip_repos filter applied |
| `test_detect_languages_python` | .py files → python |
| `test_detect_languages_multi` | Mixed repo returns top 3 |
| `test_detect_languages_skips_venv` | venv/ excluded |
| `test_needs_update_false_when_sha_matches` | Returns False when HEAD sha unchanged |
| `test_needs_update_true_when_sha_differs` | Returns True when sha changed |
| `test_needs_update_true_when_no_memory` | Returns True when file absent |
| `test_write_memory_field_order` | source_title on line 2, summary line 3, tags line 4 |
| `test_write_memory_atomic` | No .tmp file left after write |
| `test_write_memory_type_is_project` | type field = project, category field = code |
| `test_migrate_legacy_code_project` | Existing file with `type: code_project` rewritten to `type: project` + `category: code` |
| `test_migrate_idempotent` | Running migration twice does not change already-migrated file |
| `test_cmd_projects_lists_all` | `/projects` returns all project-*.md files |
| `test_cmd_projects_filter_by_category` | `/projects code` returns only category=code files |
| `test_cmd_projects_default_n_10` | Without N arg, returns at most 10 |
| `test_cmd_projects_custom_n` | `/projects 5` returns 5 entries |
| `test_cmd_projects_n_clamped` | N=999 clamped to 50; N=0 clamped to 1 |
| `test_cmd_project_detail_view` | `/project 1` shows full description, languages, last commit |
| `test_cmd_project_invalid_index` | `/project 99` without prior list → error message |
| `test_find_related_same_org` | Repos with same git org appear as related |
| `test_find_related_shared_language` | Repos with shared language appear as related |

### Integration test

1. Create a tmp git repo with a commit and a README
2. Instantiate `ProjectScanner` pointed at tmp dir
3. Run one scan cycle
4. Assert `memories/project-{name}.md` exists with `type: project`, `category: code`
5. Run scan again with no new commits — assert no file write (mtime unchanged)

---

## Changelog

### v1.1.0 — 2026-04-11

**Breaking:** Memory file `type` field changed from `code_project` to `project`.
A `category: code` field is added alongside it to enable future project types
(`person`, `work`, etc.) without requiring another type rename.

**New FRs:**

#### FR-8: Legacy file migration
**Priority:** Critical

On `ProjectScanner.__init__`, glob all `project-*.md` files in `MEMORIES_DIR`.
For any file whose frontmatter contains `type: code_project`, rewrite the
frontmatter in place:
- Set `type: project`
- Add `category: code` after the `type` field

Use atomic write (temp + rename). Log one INFO line per file migrated.
The migration is idempotent — files already containing `type: project` are
skipped. No state file required.

---

#### FR-9: /projects and /project Telegram commands
**Priority:** High

**`/projects [category] [N]`** — list projects from memory files.

- Globs `BRAIN_DIR/memories/project-*.md`
- Filters on `type == "project"` (accepts both `project` and legacy `code_project` for a one-cycle transition)
- If first arg is a known category word (not a bare integer), filters on `category == first_arg`
- If first/only numeric arg present, uses as N (default 10; clamp `[1, 50]`)
- Sorts by `last_scanned` descending
- Sets `self._last_project_set` to the displayed file paths (for `/project N`)
- Reply format: `N. name [category] (last commit date)`
- If no results: `"No projects found."` or `"No code projects found."` if filter applied

**`/project <N>`** — show full detail for project N from last `/projects` list.

- Resolves index from `self._last_project_set` via `_resolve_project_index`
- Reply includes: title, `source_url`, `local_path`, languages, last commit, summary, related projects
- If N out of range or `_last_project_set` empty: `"Invalid index. Run /projects first."`

**CommandHandler registrations:**

```python
self.app.add_handler(CommandHandler("projects", self.cmd_projects))
self.app.add_handler(CommandHandler("project",  self.cmd_project))
```
