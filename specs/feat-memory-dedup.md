---
specmas: 3.0
kind: feature
id: feat-memory-dedup
version: 1.0.0
created: 2026-04-17
status: implemented
shipped_version: "1.4.0"
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-memory-synthesis
---

# Memory Deduplication

## Overview

### Problem Statement

The same page browsed on two machines produces two memories with different filenames (different `seen-urls` per hostname). The same URL summarized twice with minor content differences produces two nearly identical memories. Over time this creates clutter and dilutes relevance scoring. Filed as feature `bbf38c`.

### Scope

**In scope:**
- `dedup_checker.py` utility: runs inside the hourly `index_builder.py` cycle (not a new loop)
- Two match tiers:
  1. **Auto-merge**: same `source_url` (normalized) + same `type` → definite duplicate; keep the file with richer content (longer body), delete the other
  2. **Candidate review**: title Jaccard similarity ≥ 0.70 + same `type` + no matching source_url → flag for user; user reviews via `/dupes`, merges or dismisses
- Merge: copy tags union, keep richer body, write combined file, delete the other
- Telegram commands: `/dupes`, `/merge N`, `/keep N`
- State file `dedup-state.json` tracks dismissed candidate pairs

**Out of scope:**
- Semantic similarity (embedding-based) — keyword/Jaccard only
- Cross-type deduplication (email vs web page)
- Automatic merge of candidate pairs (always user-reviewed)
- Deduplicating Zoom meetings or calendar events (IDs are already canonical)

### Success Metrics

- Same URL on two machines auto-merges to one file on next index build
- Near-duplicate readings appear in `/dupes` and can be merged or dismissed in 1-2 commands

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `dedup_checker.run(memories_dir, deploy_dir)` called from `index_builder._run()` after each index rebuild |
| FR-2 | **URL auto-merge**: for each `type`, group memories by normalized `source_url`; if ≥2 share same URL, keep the one with the longer body content, delete the rest, log the merge |
| FR-3 | URL normalization: strip trailing slash, lowercase scheme+host, strip `www.`, strip common tracking params (`utm_*`, `ref`, `fbclid`) |
| FR-4 | **Title candidates**: Jaccard similarity of title word-sets ≥ 0.70, same `type`, different `source_url` → add pair to candidate list |
| FR-5 | Candidate pairs stored in `dedup-state.json` under `candidates: [{a, b, similarity, detected_at}]`; already-dismissed pairs stored under `dismissed: [{a, b}]` |
| FR-6 | `/dupes` lists current candidate pairs with index, both titles, similarity score |
| FR-7 | `/merge N` merges pair N: union of tags, body = longer of the two, `source_url` = whichever has one, delete the other file |
| FR-8 | `/keep N` dismisses pair N as intentionally distinct — added to dismissed set, never re-surfaced |
| FR-9 | Auto-merge events logged at INFO level; no Telegram notification (silent) |
| FR-10 | New candidate pairs (not previously dismissed) send a Telegram notification: "🔍 Found N potential duplicate memories — /dupes to review" |

---

## Design

### Jaccard similarity

```python
def _jaccard(a: str, b: str) -> float:
    STOP = {"the", "a", "an", "of", "in", "to", "for", "and", "or", "is", "are"}
    wa = {w.lower() for w in a.split() if w.lower() not in STOP and len(w) > 2}
    wb = {w.lower() for w in b.split() if w.lower() not in STOP and len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
```

### URL normalization

```python
def _normalize_url(url: str) -> str:
    from urllib.parse import urlparse, urlencode, parse_qs
    p = urlparse(url.lower())
    host = p.netloc.removeprefix("www.")
    STRIP_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
                    "utm_term", "ref", "fbclid", "gclid"}
    qs = {k: v for k, v in parse_qs(p.query).items() if k not in STRIP_PARAMS}
    clean = p._replace(netloc=host, query=urlencode(qs, doseq=True), fragment="")
    return clean.geturl().rstrip("/")
```

### `dedup_checker.py` structure

```python
def run(memories_dir: Path, deploy_dir: Path, notify_fn=None) -> dict:
    """Returns {auto_merged: int, new_candidates: int}."""
    memories = _load_all(memories_dir)
    state = _load_state(deploy_dir)

    # Pass 1: URL auto-merge
    auto_merged = _auto_merge(memories, memories_dir)

    # Pass 2: Title candidates
    new_candidates = _find_candidates(memories, state, memories_dir)
    _save_state(deploy_dir, state)

    if notify_fn and new_candidates:
        asyncio.ensure_future(notify_fn(f"🔍 Found {new_candidates} potential duplicate memories — /dupes to review"))

    return {"auto_merged": auto_merged, "new_candidates": new_candidates}
```

### Telegram commands

- `/dupes` → read candidates from dedup-state.json, display with indices, populate `_last_dupes_set`
- `/merge N` → perform merge (union tags, keep longer body, delete shorter), remove from candidates
- `/keep N` → add pair to dismissed, remove from candidates

---

## Test Plan

**Unit tests in `tests/unit/test_dedup_checker.py`:**

1. `test_url_auto_merge_same_url` — two memories with same source_url → one file remains
2. `test_url_auto_merge_keeps_richer` — longer body file is kept
3. `test_url_normalization_strips_utm` — utm_ params stripped before comparison
4. `test_url_normalization_strips_www` — www. prefix stripped
5. `test_jaccard_above_threshold_creates_candidate` — titles with 0.75 similarity → candidate
6. `test_jaccard_below_threshold_no_candidate` — titles with 0.50 similarity → no candidate
7. `test_dismissed_pair_not_re_added` — previously dismissed pair skipped
8. `test_merge_combines_tags` — merged file has union of both tag lists
9. `test_keep_adds_to_dismissed` — /keep N adds pair to dismissed set

**Unit tests in `tests/unit/test_chat_handler.py`:**

10. `test_cmd_dupes_lists_candidates` — `/dupes` returns candidate pairs list
11. `test_cmd_merge_removes_pair` — `/merge N` deletes one file, updates state
12. `test_cmd_keep_dismisses_pair` — `/keep N` adds to dismissed, pair gone from candidates
