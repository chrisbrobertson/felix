---
name: summarize-paper
version: 2
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
max_tokens: 2000
success_rate: null
total_runs: 0
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: true
content_type: research-paper
---

## Instructions

You are creating a long-term memory entry from a research paper.

Given the paper title, URL, and raw content below, produce a memory file body with:
1. A **Summary** — 4-6 sentences covering the research question or hypothesis, the methodology in brief, the key findings with specific results (numbers, benchmarks, comparisons), and the main contribution or takeaway for practitioners.
2. **Key Points** — 5-10 bullet points including: specific experimental results with numbers, methodology details, limitations and their implications, comparisons to prior work (with specific baselines), implications for practitioners, open questions raised, and anything a reader would want to cite or build on.
3. **Entities** — paper authors, institutional affiliations, publication venue/conference, key concepts or techniques introduced, datasets used, related work cited, with brief descriptions.
4. **Tags:** — 4-8 lowercase comma-separated tags for retrieval (include field/domain, methods used, application area).

Extract author names, institutional affiliations, and publication venue if present. Focus on what a researcher or practitioner would want to remember about this paper's specific contribution six months from now. Skip acknowledgments, proof details, and boilerplate.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v2 (2026-04-28) — increased detail: 4-6 sentence summary, 5-10 key points with specifics, max_tokens raised to 2000 (#48)

### v1 (2026-04-12) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
