---
name: summarize-webpage
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

You are creating a long-term memory entry from a webpage.

Given the page title, URL, and raw content below, produce a memory file body with:
1. A 2-3 sentence **Summary** of the page's core idea
2. **Key Points** — 3-7 bullet points of the most important facts or ideas
3. **Entities** — named things (people, tools, concepts, companies) worth remembering
4. **Tags:** — 3-6 lowercase comma-separated tags for retrieval

Be ruthlessly concise. Omit navigation, ads, boilerplate. Focus on what a smart person
would want to remember about this page six months from now.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v1 (2026-04-11) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
