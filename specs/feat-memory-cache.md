# Local SQLite Memory Cache

## Motivation

The second-brain daemon's 14 async loops all read directly from iCloud Drive with no shared cache layer. Under iCloud sync pressure, individual file reads return `EDEADLK`/`EAGAIN` with multi-second retries, causing cumulative latency spikes on user-visible paths.

### Read amplification by loop (per cycle)

| Loop | Cadence | iCloud reads per cycle |
|---|---|---|
| **chat_handler `_load_context`** | per Telegram message | `glob("*.md")` + `stat()` × 3,571 + read top-20 by relevance. Plus ~20 other glob sites for admin commands. |
| **notification_manager** | every 60 s | 8 separate full globs — `calendar-event-*`, `commitment-*` (×4), `goal-*`, `project-*`, `action-*`, plus a generic `*.md`. **Worst offender by cadence × volume.** |
| **goal_project_agent** | every 6 h | full-corpus header scan + `goal-*` + `project-*` + `action-*` |
| **project_inference_scanner** | every 15 min | full-corpus scan |
| **commitment_tracker / contact_tracker** | every 5 min | full-corpus header scans |
| **skill_optimizer** | 3 AM daily | full-corpus reads |
| **index_builder** | every 60 min | full-corpus reads up to 120 KB |

**Memory file population today:** 3,571 files / ~7 MB total in `~/Library/.../second-brain/memories/`. Files are tiny (~380 B avg) — the issue is iCloud sync semantics, not data volume.

**Evidence of the underlying problem:** v1.7.2 introduced a 25-second timeout in `_load_context` as a band-aid. The timeout became necessary because under iCloud pressure, chat responses were hanging indefinitely while retrying `EDEADLK` errors on hundreds of file reads.

### Why a cache (vs. alternatives)

- **Why not move off iCloud?** iCloud is the only cross-machine transport between the watcher MacBook (the user's work computer, highest-leverage source of memories) and the always-on full machine (spec line 22, "Guiding Principles"). The codebase has no other distributed channel — verified by exhaustive grep for SSH/rsync/HTTP/Tailscale/git. The user explicitly endorses iCloud handling distributed consistency; Tailscale-on-all-nodes is not feasible.
- **Why not SQLite as storage?** User reads memory `.md` files occasionally and edits them rarely via iPhone Files.app, Obsidian Mobile, and Finder (spec line 1158). Flat files in iCloud preserve this user-visible surface. The Karpathy philosophy ("files + LLM = database") is a first-class constraint, not an implementation detail.
- **Why not FTS5, BM25, embeddings?** Only `chat_handler._load_context` does keyword search. With ~3,500 files of cached 500-char headers in memory, Python set intersection runs in single-digit ms. FTS5 would buy BM25 ranking and sub-ms search — neither matters at this scale. Embeddings would add a model dependency, vector storage, and semantic-drift maintenance cost for zero latency gain over keyword intersection. The existing `_score_relevance` algorithm is preserved verbatim.
- **Why SQLite?** It's stdlib, zero-setup, proven at this data scale (browser history DBs are larger), and WAL mode supports many readers + one writer with sub-millisecond contention. The cache is fully derivative — `rm memory-cache.sqlite` at any time and it repopulates lazily.

## Constraints

All five constraints below are hard requirements, not negotiable tradeoffs:

1. **iCloud must remain the storage substrate.** It is the only transport between the watcher MacBook and the full machine. Source: spec line 22 (Guiding Principles); user confirmation (explicit endorsement of iCloud handling distributed consistency); codebase reality (no SSH/rsync/HTTP/Tailscale).
2. **iCloud user-visible access stays.** The user reads memory `.md` files occasionally and edits them rarely via iPhone Files.app, Obsidian Mobile, and Finder. Source: spec line 1158; Karpathy philosophy (spec line 4).
3. **The watcher writes 6 prefixes that the full machine consumes via iCloud:** `YYYY-MM-DD-{slug}-{hash}` (browser captures), `code-{watcher-hostname}-*`, `email-thread-*`, `calendar-event-{watcher-hostname}-*`, `slack-thread-*`, and `project-candidate-*` (when `require_confirmation: true`). The cache must catch these without the watcher having to know it exists. Source: watcher role definition (CLAUDE.md §Two Deployment Roles); scanner write paths.
4. **Atomic-write semantics on iCloud are unchanged.** Today's tmp-file + `os.rename()` pattern is correct and stays. The cache is a pure read-side accelerator. Source: `memory_writer.py:105-110`; CLAUDE.md §Key Design Decisions ("Atomic writes").
5. **Watcher role does not run the cache.** Its reads are limited to its own write-path namespace; the read-amplification problem is full-role only. Source: CLAUDE.md §Two Deployment Roles; daemon architecture (watcher runs 5 scanners, no full-node imports).

## Architecture

A single new module **`memory_cache.py`** backed by `~/secondbrain/memory-cache.sqlite` (WAL mode). Single-writer (the daemon), many in-process readers (its own async loops). Consumers always call `cache.get()` / `cache.query_*()` — when the cache is disabled (`daemon.memory_cache.enabled: false`) or absent (watcher role), the same `MemoryCache` class operates in **pass-through mode** and routes calls straight to `read_text_with_retry_async`. One codepath, one knob, zero `if cache is not None` branches at call sites.

### Module API

```python
class MemoryCache:
    def __init__(self, db_path: Path | None, memories_dir: Path,
                 *, enabled: bool = True) -> None
        # db_path=None or enabled=False → pass-through mode (no SQLite, direct iCloud reads)

    async def rebuild(self) -> int
        # full re-scan; returns count of indexed files

    async def get(self, filename: str) -> dict | None
        # {filename, mtime, type, status, prefix, frontmatter, header500, body}
        # None if file doesn't exist

    async def query_by_type(self, type_: str, *, status: str | None = None) -> list[dict]
        # e.g. query_by_type("commitment", status="active")

    async def query_by_prefix(self, prefix: str) -> list[dict]
        # e.g. query_by_prefix("calendar-event-")

    async def score_keywords(self, query: str, top_n: int = 50) -> list[tuple[str, float]]
        # In-memory intersection over cached header500. Same algorithm as today's _score_relevance.
        # Returns [(filename, score), ...] sorted by score descending.

    async def invalidate(self, filename: str) -> None
        # re-read & upsert (or delete row if missing)

    async def sweep(self) -> tuple[int, int, int]
        # (added, updated, removed) — mtime-based sync with MEMORIES_DIR

    def close(self) -> None
```

All methods `async` for API symmetry between cached and pass-through modes (pass-through `get()` calls `read_text_with_retry_async`). SQLite calls themselves are sub-millisecond and stay on the event-loop thread.

### Schema (one table, no FTS5, no triggers)

```sql
CREATE TABLE memories (
  filename       TEXT PRIMARY KEY,
  mtime          REAL NOT NULL,
  size           INTEGER NOT NULL,
  type           TEXT,                     -- frontmatter "type"
  status         TEXT,                     -- frontmatter "status"
  prefix         TEXT,                     -- e.g. "calendar-event"
  frontmatter    TEXT,                     -- json
  header500      TEXT,                     -- first 500 chars
  body           TEXT,                     -- full file content
  indexed_at     REAL NOT NULL
);
CREATE INDEX idx_memories_type   ON memories(type);
CREATE INDEX idx_memories_prefix ON memories(prefix);
```

PRAGMAs at open: `journal_mode=WAL`, `synchronous=NORMAL`, `temp_store=MEMORY`, `mmap_size=268435456`, `busy_timeout=5000`.

**No FTS5.** Only `chat_handler._load_context` does keyword search. With ~3,500 files of cached 500-char headers in memory, Python set intersection runs in single-digit ms. FTS5 would add a virtual table + 3 triggers + tokenizer config we'd have to maintain for zero latency gain.

**Body is cached.** ~7 MB total; the cache becomes fully self-sufficient for read paths. Without body caching, `chat_handler` and the trackers still pay `EDEADLK` on every body fetch — defeats the purpose.

## Sync model

### Lazy population (no blocking warm-up loop)

The cache starts empty on first boot. Every consumer that misses falls through to `read_text_with_retry_async` and opportunistically populates via `await cache.invalidate(filename)`. The sweep loop (below) catches stragglers within 60 s. Daemon is responsive immediately on boot — first chat query on a cold cache may be slow, but once a file is in the cache it stays warm across daemon restarts (SQLite persists).

This drops the dedicated warm-up loop from earlier designs: no separate task, no startup deadline, no "6 minutes of background warming." Cache simply fills as the system uses it.

### Two-layer invalidation (no FSEvents)

- **Local-write invalidation (instant, primary).** `memory_writer.write()` calls `await cache.invalidate(filename)` after the rename. Same for the deletion sites in `chat_handler` (`_purge_domain` etc.). The full-role daemon is the only writer of most prefixes, so this catches 100% of in-process changes immediately.
- **Periodic sweep (catches iCloud-arrived files from the watcher).** A dedicated sweep loop runs every **60 s** (aligned with `notification_manager`'s tick — the loop closest to user-perceived latency). Implementation: `os.scandir(MEMORIES_DIR)` (one syscall, no per-file `stat()` storm) → mtime-diff against `SELECT filename, mtime, size FROM memories` → invalidate deltas, delete missing rows.

We deliberately skip FSEvents/watchdog: it's unreliable under iCloud sync pressure (the very condition we're solving), and the two layers above cover the cases that matter.

## Failure modes

- **Corrupt DB at open:** catch `sqlite3.DatabaseError`, `db_path.unlink()`, recreate schema, log a warning. Cache continues empty; lazy population refills it.
- **Cache miss on `get()`:** fall back to `read_text_with_retry_async(MEMORIES_DIR / filename)`; opportunistic `await cache.invalidate(filename)` to populate for next call.
- **User `rm`s `~/secondbrain/memory-cache.sqlite`:** next `MemoryCache.__init__` recreates schema; lazy population refills.
- **iCloud `EDEADLK` during a populate:** existing retry helper handles it; failed file is left out of the cache and retried on the next sweep tick.

## Operator surface

- **`/rebuild_cache` Telegram command** (full role only) — calls `await cache.rebuild()`, replies with "Cache rebuilt: N files indexed." Documented in `README.md` as the operator recovery hatch when the cache disagrees with iCloud.
- **`daemon.memory_cache.enabled: true` config knob** (default `true`) — set `false` to revert to direct iCloud reads with zero code change (cache becomes pass-through).
- **`~/secondbrain/memory-cache.sqlite` file location** — predictable path, documented in `CLAUDE.md` §Deploy directory table.
- **`rm` + restart recovery path** — fully derivative; safe to delete and repopulate.

## What this deliberately does NOT do

- **Does not change the on-disk storage format.** Memories remain plain markdown in iCloud, human-readable, git-diffable, accessible from the watcher MacBook and from iPhone Files.app / Obsidian Mobile.
- **Does not change the watcher → full transport.** iCloud remains the bus. Watchers continue to write directly to iCloud and don't run the cache; the full-role sweep picks up their writes within 60 s of iCloud convergence.
- **Does not introduce a vector DB, embeddings, or semantic search.** Keyword scoring (Python set intersection over cached headers, same algorithm as today's `_score_relevance`) remains the relevance signal. Karpathy philosophy intact.
- **Does not introduce FTS5.** Single table, two indexes — that's the entire schema.
- **Does not add external services or new dependencies.** Everything is `sqlite3` (stdlib) + Python in the existing daemon process.

## Migration discipline

**Post-Wave-2 invariant:** No consumer reads `MEMORIES_DIR` directly except via `cache.get()` / `cache.query_*()`. The watcher role is the exception — it passes through because it reads only its own write-namespace. This discipline is enforced by: (1) Wave 1 + Wave 2 migration coverage of all 14 loops; (2) `COMMAND_REGISTRY` test asserting no raw `glob()` or `Path.read_text()` in migrated modules; (3) this spec documenting the invariant as a system-level constraint.

## Latent bug fix

`chat_tools.py:424,441,499` reads `os.environ.get("SECOND_BRAIN_DIR", iCloud-default) / "memories"`. Since the launchd plist sets `SECOND_BRAIN_DIR=$HOME/secondbrain`, `/feature`, `/bug`, `add_goal`, and `add_project` from Telegram have been silently writing to `~/secondbrain/memories/` — a directory no other consumer reads. The fix: define `MEMORIES_DIR` as a module-level constant (same pattern as other modules) pointing to the canonical iCloud path, and replace all three inline `Path(os.environ.get(...))` uses with it. Add unit tests asserting that `add_feature`/`add_bug` write to the correct iCloud `MEMORIES_DIR`, not to `~/secondbrain/memories/`.
