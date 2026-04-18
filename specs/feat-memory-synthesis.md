---
specmas: 3.0
kind: feature
id: feat-memory-synthesis
version: 1.0.0
created: 2026-04-17
status: implemented
shipped_version: "1.4.0"
complexity: high
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-memory-dedup
---

# Memory Synthesis

## Overview

### Problem Statement

Memories accumulate in isolation. Felix can answer "what did I read about RAG?" but cannot proactively surface patterns across memories: "you've read five things about RAG this week and two meetings mentioned it — here's what's emerging." No process currently synthesizes cross-cutting insights. Filed as feature `3e8e0b`.

### Scope

**In scope:**
- New 15th async loop: `synthesis_scanner.py` — runs once daily (configurable time)
- Groups memories by topic overlap using tag intersection and title keyword overlap
- Clusters of 3+ related memories within a configurable lookback window trigger a synthesis
- LLM call generates a synthesis memory (`type: synthesis`) with: theme, key findings, cross-references, emerging patterns
- Deduplication: cluster fingerprint (sorted short_ids) hashed and stored in state file; same cluster not re-synthesized
- Telegram: `/insights [N]` lists recent synthesis memories; `/insight N` shows detail

**Out of scope:**
- Real-time synthesis (daily cadence only)
- Cross-user synthesis
- Synthesis of synthesis memories (one level only)
- Semantic/embedding-based clustering (keyword overlap only — consistent with project philosophy)

### Success Metrics

- After 3+ memories with overlapping tags accumulate, a synthesis memory appears in memories/
- `/insights` shows the synthesis with source references
- Same cluster not re-synthesized on next daily run

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `synthesis_scanner.py` runs daily at configurable time (default 06:00), full role only |
| FR-2 | Lookback window configurable via `synthesis_scanner.lookback_days` (default 7) |
| FR-3 | Cluster formation: two memories are "related" if they share ≥2 tags OR ≥3 title words (stop-word filtered); clusters are connected components of the related-pairs graph |
| FR-4 | Only clusters of ≥3 memories generate a synthesis |
| FR-5 | LLM call uses `optimizer` route with structured prompt: list of memory titles + tags + first 500 chars of each body |
| FR-6 | Synthesis memory written as `synthesis-{theme-slug}-{date}-{hash}.md` with `type: synthesis`, `source_ids: [...]`, `theme: str` |
| FR-7 | Cluster fingerprint = SHA1 of sorted `source_ids`; stored in `synthesis-state.json`; skip if fingerprint already seen |
| FR-8 | `/insights` lists last N synthesis memories (default 10), with date and theme |
| FR-9 | `/insight N` shows full synthesis content + source list |
| FR-10 | `daemon.py` starts synthesis loop after existing loops; watcher role skips |

---

## Design

### `synthesis_scanner.py` structure

```python
class SynthesisScanner:
    def __init__(self, config, chat_handler=None): ...

    async def run_loop(self):
        while True:
            await self._wait_until_scheduled_time()
            await self._run_synthesis()

    async def _run_synthesis(self):
        memories = self._load_recent_memories(lookback_days)
        clusters = self._cluster_memories(memories)
        state = self._load_state()
        for cluster in clusters:
            if len(cluster) < 3:
                continue
            fingerprint = self._cluster_fingerprint(cluster)
            if fingerprint in state.get("seen_fingerprints", []):
                continue
            synthesis = await self._synthesize(cluster)
            if synthesis:
                self._write_synthesis(cluster, synthesis)
                state["seen_fingerprints"].append(fingerprint)
        self._save_state(state)

    def _cluster_memories(self, memories) -> list[list[Path]]:
        # Build related-pairs graph, find connected components
        ...

    async def _synthesize(self, cluster: list[Path]) -> Optional[str]:
        # LLM call via skill_executor or direct acompletion
        ...
```

### Synthesis memory format

```markdown
---
type: synthesis
theme: Retrieval-Augmented Generation Patterns
source_ids:
  - 2026-04-12-advanced-rag-abc123.md
  - 2026-04-14-rag-evaluation-def456.md
  - 2026-04-15-rag-production-ghi789.md
created: 2026-04-17T06:00:00
tags: [rag, llm, retrieval]
---

## Theme

Retrieval-Augmented Generation Patterns

## Key Findings

- Three sources emphasize hybrid search (dense + sparse) as the dominant approach
- Evaluation frameworks (RAGAS, TruLens) appear consistently across sources
- Production deployments all mention context-window management as the primary challenge

## Emerging Patterns

...

## Sources

1. Advanced RAG Techniques (2026-04-12)
2. RAG Evaluation Frameworks (2026-04-14)
3. RAG in Production (2026-04-15)
```

### Telegram commands

`/insights` → reads all `synthesis-*.md`, sorts by date, shows list with indices
`/insight N` → shows full content of item N

---

## Test Plan

**Unit tests in `tests/unit/test_synthesis_scanner.py`:**

1. `test_cluster_formation_tag_overlap` — two memories sharing 2 tags → related
2. `test_cluster_formation_title_overlap` — 3 shared title words → related
3. `test_cluster_minimum_size` — cluster of 2 not synthesized; cluster of 3 is
4. `test_fingerprint_deduplication` — same cluster not re-synthesized on second run
5. `test_synthesis_memory_written` — mock LLM, verify file written with correct frontmatter
6. `test_synthesis_source_ids_in_frontmatter` — all cluster members listed in source_ids
7. `test_state_updated_after_synthesis` — fingerprint added to state file
8. `test_watcher_role_skips` — synthesis loop exits immediately on watcher role
