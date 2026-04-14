---
name: summarize-webpage-detailed
version: 1
preferred_model: claude-sonnet-4-6
fallback_model: gemini/gemini-2.0-flash
max_tokens: 4000
success_rate: null
total_runs: 0
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: true
---

## Instructions

You are creating a detailed long-term memory entry from a webpage the user explicitly asked to capture and study.

Given the page title, URL, and raw content below, produce a thorough memory file body with:

1. A **Summary** — 3-4 paragraphs covering the core argument, methodology, findings, and significance. Be substantive, not just descriptive.
2. **Key Points** — 8-15 bullet points of the most important facts, claims, or ideas. Include specific details (numbers, names, dates) that make each point actionable.
3. **Notable Quotes** — 2-4 direct quotes worth preserving verbatim (include speaker/source attribution).
4. **Entities** — named things (people, tools, concepts, companies, papers, datasets) worth remembering, with a 1-sentence description of each.
5. **Open Questions** — 2-5 questions this content raises that are worth investigating further.
6. **Tags:** — 4-8 lowercase comma-separated tags for retrieval.

Omit navigation, ads, boilerplate. Prioritize depth over brevity — the user chose /note specifically to get a richer capture than /remember provides.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v1 (2026-04-13) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
