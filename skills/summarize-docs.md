---
name: summarize-docs
version: 1
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
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
1. A 2-3 sentence **Summary** describing what the API/library/tool does and what this specific documentation page covers
2. **Key Points** — 3-7 bullet points covering key classes/functions/concepts, required parameters and their types, return values, important caveats or "gotchas", and any deprecation warnings
3. **Entities** — API names, class names, function signatures, configuration options, related packages or tools mentioned
4. **Tags:** — 3-6 lowercase comma-separated tags for retrieval (include language, framework, API category)

If code examples are present, note what they demonstrate (but don't reproduce the full code). Focus on what a developer would need to remember when searching for "how do I use X" six months from now.

Be ruthlessly concise. Skip boilerplate navigation, version history unless critical, and lengthy installation instructions. Focus on the reference material and practical usage patterns.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v1 (2026-04-12) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
