---
name: summarize-paper
version: 1
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
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
1. A 2-3 sentence **Summary** covering the hypothesis or research question, key findings, and main contribution
2. **Key Points** — 3-7 bullet points including methodology (briefly), results, limitations, and implications for practitioners
3. **Entities** — paper authors, institutional affiliations, publication venue/conference, key concepts or techniques introduced, related work cited
4. **Tags:** — 3-6 lowercase comma-separated tags for retrieval (include field/domain, methods used, application area)

Extract author names and institutional affiliations if present in the content. Note the paper's venue or publication source if mentioned. Focus on what a researcher or practitioner would want to remember about this paper's contribution six months from now.

Be ruthlessly concise. Skip acknowledgments, detailed proofs, and lengthy references. Focus on intellectual contribution and practical takeaways.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v1 (2026-04-12) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
