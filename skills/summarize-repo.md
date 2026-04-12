---
name: summarize-repo
version: 1
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
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
1. A 2-3 sentence **Summary** covering what problem this project solves, its primary use case, and what makes it notable
2. **Key Points** — 3-7 bullet points including primary language and tech stack, installation/usage in brief, notable features or design choices, activity level (stars/forks/last commit if visible), and any important limitations or requirements mentioned
3. **Entities** — project name, author/organization, programming languages used, key dependencies or frameworks, related projects mentioned, license type
4. **Tags:** — 3-6 lowercase comma-separated tags for retrieval (include language, domain, project type like library/tool/framework)

Focus on what a developer evaluating this project would want to remember: What does it do? How mature is it? What's the tech stack? What are the tradeoffs?

Be ruthlessly concise. Skip contributor lists, detailed changelogs, and CI/CD badges. Focus on the README's core narrative and practical facts.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v1 (2026-04-12) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
