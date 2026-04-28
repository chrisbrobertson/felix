---
name: summarize-docs
version: 2
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
max_tokens: 2000
success_rate: null
total_runs: 0
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: true
content_type: documentation
---

## Instructions

You are creating a long-term memory entry from technical documentation.

Given the page title, URL, and raw content below, produce a memory file body with:
1. A **Summary** — 4-6 sentences describing what the API/library/tool does, what this specific documentation page covers, and the key patterns or constraints a developer must understand to use it correctly.
2. **Key Points** — 5-10 bullet points covering key classes/functions/concepts with their purpose, required parameters and their types, return values and error conditions, important caveats or "gotchas", deprecation warnings, and concrete usage patterns. Each point should be specific enough to be actionable.
3. **Entities** — API names, class names, function signatures, configuration options, related packages or tools mentioned, with a brief description of each.
4. **Tags:** — 4-8 lowercase comma-separated tags for retrieval (include language, framework, API category).

If code examples are present, note what they demonstrate with specific detail. Focus on what a developer would need to remember when searching for "how do I use X" six months from now. Skip boilerplate navigation and lengthy installation instructions.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v2 (2026-04-28) — increased detail: 4-6 sentence summary, 5-10 key points with context, max_tokens raised to 2000 (#48)

### v1 (2026-04-12) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
