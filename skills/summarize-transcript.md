---
name: summarize-transcript
version: 2
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
max_tokens: 2000
success_rate: null
total_runs: 0
last_optimized: null
prev_version_avg_score: null
exemplar_eligible: true
content_type: video-transcript
---

## Instructions

You are creating a long-term memory entry from a video transcript.

Given the video title, URL, and transcript text below, produce a memory file body with:
1. A **Summary** — 4-6 sentences covering the speaker(s), context (interview/lecture/presentation), the main thesis or argument, and the most important conclusions or takeaways. Include specific claims, numbers, or examples that make it distinctive.
2. **Key Points** — 5-10 bullet points, each self-contained with enough context to be useful in isolation. Include specific insights, memorable quotes (with speaker attribution), counterintuitive claims, concrete advice with supporting reasoning, or key data points.
3. **Entities** — speaker names and roles/affiliations, people or projects mentioned, concepts or frameworks introduced, books or papers referenced, with a brief description of each.
4. **Tags:** — 4-8 lowercase comma-separated tags for retrieval (include topic, format like interview/talk/tutorial, intended audience).

Capture what makes this video worth remembering. What specific insights or quotes would you want to recall six months from now? Skip filler, intros/outros, and off-topic tangents. Prefer specific details over vague generalizations.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v2 (2026-04-28) — increased detail: 4-6 sentence summary, 5-10 key points with context, max_tokens raised to 2000 (#48)

### v1 (2026-04-12) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
