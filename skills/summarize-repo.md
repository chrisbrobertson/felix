---
name: summarize-repo
version: 2
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
max_tokens: 2000
success_rate: null
total_runs: 0
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: true
content_type: code-repo
---

## Instructions

You are creating a long-term memory entry from a code repository page.

Given the page title, URL, and raw content below, produce a memory file body with:
1. A **Summary** — 4-6 sentences covering what problem this project solves, its primary use case, what makes it notable, and important context (maturity, adoption, ecosystem position).
2. **Key Points** — 5-10 bullet points including: primary language and tech stack, key dependencies or frameworks, installation/usage highlights, notable features or design choices, activity level (stars/forks/last commit if visible), performance characteristics or benchmarks if mentioned, important limitations or requirements, and anything a developer would want to know before adopting it.
3. **Entities** — project name, author/organization, programming languages used, key dependencies or frameworks, related projects mentioned, license type, with brief descriptions.
4. **Tags:** — 4-8 lowercase comma-separated tags for retrieval (include language, domain, project type like library/tool/framework).

Focus on what a developer evaluating this project would want to remember: What does it do? How mature is it? What's the tech stack? What are the tradeoffs? Skip contributor lists, detailed changelogs, and CI/CD badges.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v2 (2026-04-28) — increased detail: 4-6 sentence summary, 5-10 key points with context, max_tokens raised to 2000 (#48)

### v1 (2026-04-12) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
