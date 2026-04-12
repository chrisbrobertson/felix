---
name: summarize-transcript
version: 1
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
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
1. A 2-3 sentence **Summary** covering the speaker(s), context (interview/lecture/presentation), and the main thesis or argument
2. **Key Points** — 3-7 bullet points with the most important insights, memorable quotes (attribute to speaker if named), counterintuitive claims, or actionable advice
3. **Entities** — speaker names and roles/affiliations, people or projects mentioned, concepts or frameworks introduced, books or papers referenced
4. **Tags:** — 3-6 lowercase comma-separated tags for retrieval (include topic, format like interview/talk/tutorial, intended audience)

Capture what makes this video worth remembering. What's the core idea? What specific insights or quotes would you want to recall six months from now? Who should watch this and why?

Be ruthlessly concise. Skip filler, intros/outros, and off-topic tangents. Focus on intellectual content and actionable takeaways.

Output only the markdown body (no frontmatter). Start with ## Summary.

## Evolution Log

### v1 (2026-04-12) — initial version

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
