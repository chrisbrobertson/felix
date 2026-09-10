---
spec_type: TIF
id: feat-reasoning-retrieval-fallback
status: review
owners:
  - chrisbrobertson@gmail.com
dependencies:
  - feat-memory-cache
  - feat-chat-handler
  - feat-deep-memories
fit_check: partial — literal PageIndex adoption rejected (see §1); a narrow, in-house reasoning-navigation fallback is the implementation-ready scope of this spec
complexity: moderate
specmas: 3.0
kind: feature
version: 0.1.0
created: 2026-09-09
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-memory-cache
  - feat-chat-handler
  - feat-deep-memories
  - feat-memory-management
source_ticket: https://github.com/chrisbrobertson/felix/issues/130
---

# Reasoning-Based Retrieval Fallback for Chat Context

Source ticket: [#130 — Revamp the Felix memory/indexing system to use PageIndex](https://github.com/chrisbrobertson/felix/issues/130)

## 0. Ticket as filed vs. what this spec implements

Ticket #130 asks to "revamp the Felix memory/indexing system to use PageIndex
(github.com/VectifyAI/PageIndex) for structured document indexing and
retrieval, potentially replacing or augmenting the current keyword-intersection
scoring approach." It was captured via `add_feature` with no further notes —
there is no linked incident, no example of a failed query, and no complaint
that `/search` or chat context loading currently returns bad results.

This spec's §1 documents why adopting the PageIndex library/service as
literally requested is a poor architectural fit for this codebase. §2 onward
specs a narrower feature — an optional, in-house, reasoning-based navigation
fallback for chat context retrieval — that captures the legitimate part of the
underlying idea (LLM reasoning over document structure can outperform keyword
matching for certain queries) without the parts that don't fit (external
package, per-document LLM indexing pass, single-long-document tree model,
optional cloud submission). This is offered as the actionable interpretation
of the ticket; if the reporter's intent was narrower or different, this spec
should be revised before implementation starts (see §9, Phase 0 gate).

## 1. Fit check: why not the PageIndex library

Researched primary source: `github.com/VectifyAI/PageIndex` (fetched
2026-09-09; PageIndex is under active development, so specifics may have
moved — treat model/package names below as **assumptions to re-verify** at
implementation time, not settled fact).

**What PageIndex is.** A "vectorless RAG" library: it converts *a single long,
structured document* (financial report, legal filing, technical manual,
textbook, long PDF) into a hierarchical tree index mirroring the document's
own layout (chapters → sections → pages), using a cheap LLM pass to summarize
each node once at indexing time. At query time, a reasoning LLM walks the tree
top-down, using node summaries to decide which subtrees are relevant, instead
of embedding-similarity search over chunks. It ships as a `pip install
pageindex` client (`PageIndexClient`) that takes a document, returns a
`doc_id`, and answers queries against that one document; it also offers a
managed cloud-indexing mode. It requires an LLM API key (OpenAI or compatible)
independent of whatever routing the host application already uses.

**Why that shape does not map onto Second Brain's corpus:**

1. **Wrong unit of retrieval.** PageIndex indexes the internal structure of
   *one long document*. Second Brain's corpus is the opposite shape: ~3,500+
   independent flat files (per `specs/feat-memory-cache.md`, "Memory file
   population today"), averaging ~380 bytes, capped at ~6 KB for standard
   memories and ~8 KB for `depth: deep` memories (`specs/feat-deep-memories.md`).
   Every individual memory file already fits entirely in an LLM context
   window in one shot — there is no long-document-internal-navigation problem
   to solve. The closest thing to a "long structured document" in this system
   is `index.md`, which is deliberately capped at ~400-500 words
   (`CLAUDE.md` §Runtime File Locations) — far too small to need hierarchical
   tree retrieval.
2. **This tradeoff was already adjudicated at this exact scale.** `specs/feat-memory-cache.md`
   §"Why not FTS5, BM25, embeddings?" explicitly evaluated adding a heavier
   retrieval layer (there: FTS5 and embeddings) over this same ~3,500-file,
   ~7 MB corpus and rejected it: "With ~3,500 files of cached 500-char headers
   in memory, Python set intersection runs in single-digit ms... neither
   matters at this scale." PageIndex is heavier than FTS5 or embeddings (it
   requires an LLM call per document at index time and at least one LLM call
   per query at retrieval time), so the same conclusion applies a fortiori
   unless a new, different problem is identified (§9 Phase 0 addresses this).
3. **Bypasses existing routing and telemetry contracts.** `PageIndexClient`
   configures its own model names directly, bypassing `llm_routes.resolve()`
   / `SECOND_BRAIN_PROVIDER` (`llm_routes.py`) and the skill-execution-history
   self-logging pattern (`skill_executor.py`, CLAUDE.md §Key Design
   Decisions). Adopting it as a black box would create a second, incompatible
   LLM-calling path alongside the one every other module uses.
4. **Privacy.** PageIndex's cloud-indexing mode would mean submitting a
   third-party service the contents of email threads, calendar events, and
   meeting transcripts — personal data this system otherwise keeps to
   `ANTHROPIC_API_KEY`/`GEMINI_API_KEY` calls and iCloud. Local mode avoids
   this but still adds an unvetted external dependency (`pageindex` on PyPI)
   to a codebase whose `requirements.txt` is currently 10 pinned packages,
   none of which wrap a competing agent/retrieval framework.
5. **Karpathy flat-file philosophy.** CLAUDE.md states the project's guiding
   constraint plainly: "No vector DB, no graph DB, no embeddings. Files + LLM
   = database." PageIndex is marketed as "vectorless," but it replaces "files
   + LLM" with "files + a second product's tree-index format + a second
   product's LLM client" — it doesn't eliminate an indexing layer, it adds a
   different one, owned by an external project we don't control the roadmap
   of.

**Conclusion:** adopting the `pageindex` package or PageIndex Cloud as a
drop-in replacement or wrapper for `MemoryCache`/`_score_relevance` is
**not recommended**. `fit_check: partial` reflects that the ticket's literal
ask is rejected, while the underlying idea — LLM reasoning over document
*structure* can surface relevant material that keyword intersection misses —
is worth a narrow, in-house experiment, specified below.

## 2. What this spec actually proposes

A **second retrieval tier**, gated behind a config flag defaulting to off,
that activates only when keyword-intersection scoring (`MemoryCache.score_keywords`,
i.e. today's `_score_relevance` algorithm) returns weak results. Tier 2
builds a small, deterministic, two-level **outline** of the corpus (type →
month → titles) from data already in `MemoryCache` — no new indexing pass, no
external package — and asks the existing chat LLM (via the existing
`llm_routes` / skill-execution machinery) to reason over that outline and
pick candidate files, the same way a human would use a table of contents.
This is deliberately *inspired by* PageIndex's "reasoning over structure
beats similarity search" insight without importing its document model,
package, or LLM client.

### 2.1 Call sites (both already exist; no new command surface)

- `chat_handler.py:574` — `_load_context()`, used by every Telegram
  message.
- `chat_handler.py:1446` — `_search_memories_text()`, used by `/search`.

Both currently call `self._cache.score_keywords(query, top_n=50)` and stop.
This spec adds one function, `_maybe_reasoning_fallback()`, called
immediately after that line at both sites.

### 2.2 Trigger condition (tier 1 → tier 2)

Tier 2 fires only when **all** of the following hold:

- `chat.retrieval.reasoning_fallback: true` in `config.yaml` (default `false`).
- Role is `full` (watcher role never runs chat; already excluded by daemon
  structure).
- Tier 1 result is weak: `len([s for _, s in scored if s >= 2]) < 3` — fewer
  than 3 files scored at least 2 keyword-token hits. This is a starting
  threshold to validate empirically in Phase 0 (§9), not a tuned constant.
- The query, after tokenization (`re.findall(r'\b\w{3,}\b', query.lower())`),
  has at least 1 token — an empty/punctuation-only query never triggers tier 2.

When triggered, tier 2's candidate filenames are **merged in front of** the
tier-1 ordering (deduplicated), not used to replace it. If tier 2 fails,
times out, or returns nothing, tier-1 results are used unmodified — tier 2 is
strictly additive and fails open to existing behavior.

### 2.3 Outline construction — `MemoryCache.outline()`

New method on `MemoryCache` (both cache and pass-through modes), added
alongside `query_all()`/`score_keywords()`:

```python
async def outline(self, *, max_titles_per_bucket: int = 40) -> dict:
    """Deterministic two-level outline of the corpus: {prefix_or_type: {month: [(filename, title)]}}.

    Built from existing frontmatter (`type`/`prefix`/`mtime`/`source_title` or
    equivalent), not a new LLM pass. Title fields checked in order:
    source_title, title, name, subject — first present wins; falls back to
    the filename stem. No raw glob/read — reads via query_all(), consistent
    with the read-path invariant in specs/feat-memory-cache.md.
    """
```

- Bucket key: `row["prefix"] or row["type"] or "web"` (mirrors
  `_extract_prefix` / `_SEARCH_GROUP_ORDER` naming already used in
  `chat_handler.py:1376-1385`).
- Sub-bucket key: `YYYY-MM` derived from `mtime`.
- Within a bucket/month, cap at `max_titles_per_bucket` (default 40), keeping
  the most recent by `mtime` — this bounds a single very active month/type
  from blowing up the outline size.
- Pass-through mode: same shape, built by globbing once (`query_all()`
  already handles the pass-through/cache split) — no additional glob call
  is introduced at this layer.

**Token bound.** With ~3,500 files, a flat "all titles" outline is roughly
80,000–100,000 tokens (rough estimate: ~3,500 files × ~25 tokens/title
line) — too large to hand an LLM in one call and not meaningfully cheaper
than just running tier 1 on everything. This is why outline construction is
**two-level**: level 1 shows only bucket names + counts (a few hundred
tokens), level 2 expands only the buckets the LLM picks in level 1. See §2.4.

### 2.4 Two-level navigation (the "reasoning" step)

Implemented as a new skill file, `skills/retrieve-navigate.md`, run through
the existing `skill_executor.py` path (so it gets `## Execution History`
self-logging and is eligible for `skill_optimizer` scoring like every other
skill — this is the telemetry story; no bespoke logging is added).

- **Level 1 call:** prompt includes the query and a compact list of
  `{bucket: total_count}` (all buckets, one line each — bounded by the number
  of distinct type/prefix values, currently ~15). LLM returns up to 5 bucket
  names to expand.
- **Level 2 call:** for the chosen buckets only, expand to
  `{month: [(filename, title)]}` (bounded by `max_titles_per_bucket` per
  month) and ask the LLM to return up to 20 candidate filenames ranked by
  relevance.
- Both calls route through `llm_routes.resolve("summarize")` (the cheap
  tier — Haiku-class) — this is a retrieval-assist task, not a
  quality-critical chat response; no new alias is introduced.
- **Hard timeout:** 8 seconds total for both calls combined
  (`asyncio.wait_for`), well under the existing 25-second band-aid timeout
  documented in `specs/feat-memory-cache.md` ("v1.7.2 introduced a 25-second
  timeout in `_load_context` as a band-aid") so tier 2 cannot itself become
  the next version of that problem. On timeout or any exception, log at
  `warning` level and return `[]` (tier-1-only, per §2.2).

### 2.5 Config surface

```yaml
chat:
  retrieval:
    reasoning_fallback: false      # default off; opt-in per-install
    weak_result_threshold: 3       # tier-1 files scoring >=2 below which tier 2 fires
    max_titles_per_bucket: 40
    timeout_seconds: 8
```

All four keys optional with the defaults shown; missing `chat.retrieval`
block entirely is equivalent to `reasoning_fallback: false`.

## 3. Known facts vs. assumptions

**Known facts** (verified in this checkout):
- `MemoryCache.score_keywords` implements the keyword-intersection algorithm
  exactly as described (`memory_cache.py:380-421`); it is the single
  relevance signal used by both call sites today.
- `_load_context` has a 150,000-token budget (`MAX_CONTEXT_TOKENS`,
  `chat_handler.py:33`) and the file already documents a prior 25-second
  timeout band-aid for the same function (per `specs/feat-memory-cache.md`).
- The read-path invariant ("every loop and Telegram command reads memory
  files through `memory_cache.MemoryCache.query_*()`/`get()`") is enforced by
  an AST-level test, `tests/unit/test_memory_cache_migration.py`
  (CLAUDE.md §Key Design Decisions) — any new code touching retrieval must
  route through `MemoryCache`, not `MEMORIES_DIR.glob()`.
- `skills/*.md` follow a documented frontmatter contract (`name`,
  `preferred_model`, `fallback_model`, `max_tokens`, execution-history table)
  per `skills/summarize-webpage.md` and CLAUDE.md's "self-logging skills"
  design decision.
- `llm_routes.resolve()` is the sanctioned way to turn a route alias into a
  concrete model ID, provider-aware via `SECOND_BRAIN_PROVIDER`
  (`llm_routes.py`).

**Assumptions requiring validation before/at implementation:**
- PageIndex's exact current API surface, pricing, and model names (the
  WebFetch summary in §1 names `PageIndexClient`, `submit_document`, and
  specific model identifiers that could not be independently corroborated in
  this offline research pass — treat as directionally correct, not verbatim
  accurate).
- That keyword-intersection retrieval is, in practice, currently missing
  relevant memories for real user queries. **No evidence of this exists yet**
  — the ticket was filed with no example query, no complaint, and no linked
  incident. §9 makes this an explicit go/no-go gate before implementation
  begins, following the same discipline as `specs/migrate-to-hermes.md` §9
  ("Suggested First Step").
- The `weak_result_threshold: 3` / `score >= 2` starting values in §2.2 are
  untuned guesses; Phase 0 (§9) should sample real query logs
  (`~/secondbrain/chat-execution-log.jsonl`) to set them empirically rather
  than shipping with invented constants.

## 4. API / CLI contract

No new Telegram commands. Behavior change only, gated by config, at two
existing call sites.

```python
# memory_cache.py — new method
async def outline(self, *, max_titles_per_bucket: int = 40) -> dict[str, dict[str, list[tuple[str, str]]]]

# chat_handler.py — new private helper, called from _load_context and _search_memories_text
async def _maybe_reasoning_fallback(
    self, query: str, tier1_scored: list[tuple[str, float]]
) -> list[str]:
    """Returns [] if disabled, tier-1 already strong, or tier 2 fails/times out.
    Otherwise returns up to 20 candidate filenames, most-relevant first."""
```

`_maybe_reasoning_fallback` output is merged into the existing scored list at
both call sites as synthetic entries at the front (score = tier-1 max score +
1, so they sort ahead without disturbing tier-1 relative ordering), then the
existing dedup-by-filename and truncation logic (already present at both
sites) runs unchanged.

## 5. Invariants

- **I1 — Fail open.** Any exception, timeout, or malformed LLM response in
  tier 2 must degrade to tier-1-only results. Tier 2 must never cause a chat
  response to fail, be empty, or hang past `chat.retrieval.timeout_seconds`.
- **I2 — Additive, never subtractive.** Tier 2 can only add candidate
  filenames ahead of tier-1 results; it never removes or reorders files tier
  1 already found relevant.
- **I3 — Read-path invariant preserved.** `outline()` and the navigation
  skill read memory data exclusively via `MemoryCache` methods
  (`query_all()`, existing frontmatter fields already cached). No new
  `MEMORIES_DIR.glob()` or `Path.read_text()` call sites are introduced —
  `tests/unit/test_memory_cache_migration.py` must continue to pass
  unmodified.
- **I4 — Default off, zero behavior change out of the box.** With
  `chat.retrieval.reasoning_fallback` absent or `false` (the shipped
  default), `_load_context` and `_search_memories_text` behave byte-for-byte
  as they do today. This is a purely opt-in feature for the initial release.
- **I5 — Routing/telemetry parity.** All LLM calls in this feature go through
  `llm_routes.resolve()` and `skill_executor.py`'s skill-execution path
  (`skills/retrieve-navigate.md`), exactly like every other LLM-calling
  module in the codebase — no bespoke `acompletion()` call sites.
- **I6 — Watcher role unaffected.** Watcher-role machines do not import
  `chat_handler.py` (CLAUDE.md §Two Deployment Roles); this feature is dead
  code on that role by construction, requiring no explicit role check beyond
  what already gates chat_handler's import.

## 6. Idempotency

- `outline()` is a pure read/derive function over already-cached data; calling
  it repeatedly for the same corpus state returns the same result. It has no
  side effects (no writes, no cache mutation).
- `_maybe_reasoning_fallback()` is not required to be deterministic across
  calls (LLM output can vary), but repeated calls with an unchanged corpus
  and query must not corrupt state — there is no persisted state for this
  feature (§8, no new state file). Re-running a chat query that previously
  triggered tier 2 simply re-runs both LLM calls; no caching of navigation
  results is introduced in this spec (out of scope — see §10).

## 7. Error model

| Failure | Handling |
|---|---|
| `chat.retrieval.reasoning_fallback` missing/false | Tier 2 skipped entirely; no code path executed beyond a config read. |
| Tier 1 already strong (trigger condition not met) | Tier 2 skipped; `_maybe_reasoning_fallback` returns `[]` without any LLM call (cost control). |
| Level-1 or level-2 LLM call raises (`acompletion` exception, malformed JSON/response parse failure) | Caught, logged at `warning`, return `[]`. |
| Combined tier-2 latency exceeds `timeout_seconds` | `asyncio.wait_for` cancels, logged at `warning`, return `[]`. |
| `outline()` called when `MemoryCache` is in pass-through mode (watcher-equivalent config, or `daemon.memory_cache.enabled: false`) | Same shape returned via `query_all()`'s existing pass-through path; no special-casing needed at this layer (§2.3). |
| LLM returns filenames that don't exist in the corpus (hallucination) | Filtered out — `_maybe_reasoning_fallback` validates each returned filename against `tier1_scored`'s source set / a fresh `cache.get()` before including it; unknown filenames are dropped silently, not surfaced as an error to the user. |
| Corpus empty (fresh install, zero memories) | `outline()` returns `{}`; level-1 call is skipped (nothing to navigate) and `_maybe_reasoning_fallback` returns `[]` immediately. |

## 8. Security & privacy

- No new external network dependency: both LLM calls use the existing
  `litellm`/`acompletion` path already trusted for chat and summarization
  (`ANTHROPIC_API_KEY` / `GEMINI_API_KEY`, per CLAUDE.md §LLM Routing) — no
  data leaves the existing trust boundary.
- Outline content sent to the LLM is titles/dates/type buckets only — never
  full memory bodies — until level 2 selects specific files, at which point
  the existing tier-1 body-fetch-and-truncate logic in `_load_context`
  (already reviewed/shipped) handles the actual content, unchanged.
- No new state file, so no new data-at-rest surface (§6, §10).
- Rejecting the PageIndex cloud-indexing path (§1, point 4) is itself a
  security/privacy decision: it avoids sending personal email/calendar/
  meeting content to a third-party indexing service.

## 9. Bounds, telemetry, and the Phase 0 gate

**This feature must not be implemented before Phase 0 completes.** Mirroring
`specs/migrate-to-hermes.md` §9's discipline of a cheap validation step before
committing to a larger build:

**Phase 0 (measurement, ~1-2 hours, no code shipped):**
1. Sample the last N (suggest 50-100) user queries from
   `~/secondbrain/chat-execution-log.jsonl` (full-node chat execution log,
   per CLAUDE.md §Deploy directory).
2. For each, run `MemoryCache.score_keywords` offline and record how many
   score `>= 2`. Compute what fraction would trip the proposed
   `weak_result_threshold: 3` gate.
3. For a handful of queries that trip weak-result, manually check whether
   more relevant memories exist in the corpus that keyword intersection
   missed (i.e., is this a real recall problem, or does keyword intersection
   already find everything relevant and there's nothing to gain?).
4. **Go/no-go:** if the sample shows keyword intersection already finds the
   relevant files for weak-result queries (i.e., "weak result" correlates
   with "genuinely few relevant memories exist," not "relevant memories
   exist but keyword-missed them"), do not implement §2-§8 — close this
   spec as not-needed and note the finding on ticket #130. If it shows a
   real, recurring recall gap, proceed to implementation with
   `weak_result_threshold` and `max_titles_per_bucket` tuned from the sample
   rather than the placeholder values in §2.2/§2.5.

**Bounds (once implemented):**
- Outline size capped per §2.3 (`max_titles_per_bucket`, default 40/bucket/month).
- Level-1 prompt bounded by distinct bucket count (~15 today, grows slowly
  since new `type`/`prefix` values are rare — see CLAUDE.md's namespace
  evolution history for `code`/`project`).
- Combined tier-2 latency bounded by `timeout_seconds` (default 8s).
- Tier 2 fires on at most the subset of queries meeting the weak-result
  trigger — expected to be a minority of chat messages, bounding added LLM
  cost. Phase 0's sample should report this fraction explicitly.

**Telemetry:** `skills/retrieve-navigate.md`'s `## Execution History` table
(self-logging, per CLAUDE.md's skill design decision) is the telemetry
surface — no bespoke metrics/logging system is introduced. `skill_optimizer`
picks this skill up for LLM-as-judge scoring like any other skill once it has
execution history.

## 10. Out of scope

- Replacing `MemoryCache.score_keywords` / tier 1 — it remains the primary,
  always-on relevance signal (I4).
- Any use of the `pageindex` PyPI package, `PageIndexClient`, or PageIndex
  Cloud (§1).
- Embeddings, vector DB, FTS5, or any persistent secondary index — the
  outline in §2.3 is computed on demand from already-cached data, not
  pre-built and stored.
- Caching/memoizing navigation results across queries or across daemon
  restarts.
- Applying reasoning-based navigation to any consumer other than
  `_load_context` and `_search_memories_text` (e.g. `notification_manager`,
  `goal_project_agent` are unaffected).
- Tuning or auto-adjusting `weak_result_threshold` at runtime — it is a
  static config value, set once from Phase 0 findings.

## 11. Failure modes summary (operational)

- **Feature disabled (default):** zero behavior change, zero risk.
- **Feature enabled, tier 2 healthy:** occasional extra ~8s-bounded LLM
  round-trip on weak-result queries only; results merge additively.
- **Feature enabled, LLM provider outage:** every tier-2 call times out or
  errors within 8s and falls back to tier 1 — user-visible impact is at most
  an 8-second delay on weak-result queries, never a failed response (I1).
- **Feature enabled, corpus grows 10x (35,000 files):** level-1 outline
  (bucket counts) stays cheap (bucket count doesn't grow with file count);
  level-2 expansion is capped by `max_titles_per_bucket` per bucket/month
  regardless of total corpus size, so per-query cost stays bounded. This
  should be re-verified empirically if the corpus reaches that scale.
- **Operator wants to roll back:** flip `chat.retrieval.reasoning_fallback`
  to `false` (or delete the block) and restart the daemon — no data
  migration, no state to clean up (§6, §10).

## 12. Acceptance tests

Unit tests in `tests/unit/test_memory_cache.py` (outline) and
`tests/unit/test_chat_handler.py` (fallback wiring), following existing
patterns (mocked `acompletion` via `patch("litellm.acompletion", ...)`,
`tmp_path` fixtures, patched `MEMORIES_DIR`):

1. `test_outline_buckets_by_prefix_and_month` — given memory files with
   varying `type`/`prefix` and `mtime`, `outline()` groups them correctly and
   respects `max_titles_per_bucket`, keeping the most recent by `mtime` when
   a bucket/month exceeds the cap.
2. `test_outline_pass_through_mode` — `MemoryCache(db_path=None, ...)`
   produces the same outline shape as cache mode for an identical fixture
   directory.
3. `test_outline_empty_corpus` — zero memory files returns `{}`, no
   exception.
4. `test_reasoning_fallback_disabled_by_default` — with no `chat.retrieval`
   config block, `_maybe_reasoning_fallback` returns `[]` and `acompletion`
   is not called (assert zero calls on the mock).
5. `test_reasoning_fallback_skipped_when_tier1_strong` — tier-1 scored list
   has >= `weak_result_threshold` files scoring >= 2; `_maybe_reasoning_fallback`
   returns `[]` without calling the mocked `acompletion`.
6. `test_reasoning_fallback_fires_on_weak_tier1` — tier-1 scored list is
   weak; mocked `acompletion` returns a valid two-level navigation response;
   assert the returned filenames are merged ahead of tier-1 results in
   `_load_context`'s assembled context, deduplicated.
7. `test_reasoning_fallback_timeout_degrades_gracefully` — mocked
   `acompletion` sleeps past `timeout_seconds`; assert `_maybe_reasoning_fallback`
   returns `[]` within a bounded test-clock tolerance and no exception
   propagates to the caller.
8. `test_reasoning_fallback_drops_hallucinated_filenames` — mocked
   `acompletion` returns a filename not present in the corpus; assert it is
   filtered out of the merged result.
9. `test_reasoning_fallback_malformed_llm_response` — mocked `acompletion`
   returns unparseable content; assert graceful `[]` return, `warning`-level
   log, no exception.
10. `test_memory_cache_migration` (existing, in
    `tests/unit/test_memory_cache_migration.py`) — must continue passing
    unmodified, confirming no raw `MEMORIES_DIR.glob()`/`Path.read_text()`
    call sites were introduced by this feature (I3).
11. Integration test in `tests/integration/` — end-to-end `_load_context`
    call with `reasoning_fallback: true`, a weak-result query, mocked
    `acompletion` for both navigation levels and the final chat completion,
    asserting the assembled context includes the tier-2-surfaced file's body.

## 13. Release notes for this spec (not yet performed — see CLAUDE.md workflow)

This spec intentionally does not modify `CHANGELOG.md`, `VERSION`, or any
code (per task constraints). When implementation begins, follow
CLAUDE.md's standard workflow:
- Add a `CHANGELOG.md [Unreleased] → Added` bullet for the new config-gated
  retrieval fallback.
- Document the `chat.retrieval.*` config keys in `README.md` (user-facing
  config surface) and in `config.yaml.template`.
- This is a **minor** version bump (new config option, new user-visible
  retrieval behavior when enabled) per CLAUDE.md's semver rules — not major,
  since it changes no on-disk memory format and is off by default.
