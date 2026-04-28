---
name: summarize-webpage-quick
version: 1
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: true
---

## Instructions

You are creating a quick long-term memory entry from a webpage.

Given the page title, URL, and raw content below, produce a concise memory file body with:
1. A **Summary** — 1-2 sentences capturing the single most important idea.
2. **Key Points** — exactly 3 bullet points: the three most actionable or memorable facts.
3. **Tags:** — 2-4 lowercase comma-separated tags for retrieval.

Be ruthlessly concise. Omit everything that is not the core idea. This is a quick capture,
not a deep study — favour speed and brevity over completeness.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v1 (2026-04-28) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
