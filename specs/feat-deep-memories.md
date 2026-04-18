---
specmas: 3.0
kind: feature
id: feat-deep-memories
version: 1.0.0
created: 2026-04-17
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-skill-routing
  - feat-pdf-content
---

# Deep Memories

## Overview

### Problem Statement

All memories are capped at ~2KB with a short summary. For research papers, long-form articles, and key reference material, this is insufficient — important details, key quotes, methodology, and data are lost. Users want a second class of memory that goes deeper. Filed as feature `ad0451`.

### Scope

**In scope:**
- New `depth` frontmatter field: `standard` (default) or `deep`
- Deep memories: higher token budget (up to 8KB), structured sections (summary, key findings, quotes, methodology if applicable)
- New skill: `skills/summarize-deep.md` — longer, more analytical prompt
- Automatic promotion: `skill_router.py` returns `deep=True` for research papers and long-form content above a word-count threshold
- Manual promotion: `/deepen N` command re-processes an existing standard memory with the deep skill
- Browser watcher routes deep-classified content to `summarize-deep` skill
- Chat context: deep memories injected first when relevant (higher priority in `_load_context`)

**Out of scope:**
- Automated deep processing of all new memories (cost — deep is opt-in or auto for specific types)
- Storing raw full-text (still summarized — just longer and more structured)
- Deep processing for email, Slack, or calendar memories (web/document content only)

### Success Metrics

- A research paper URL produces a structured deep memory with key findings section
- `/deepen N` re-processes an existing reading memory and updates the file in place
- Deep memories appear first in chat context when the query is research-oriented

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | New `depth: standard\|deep` frontmatter field; default `standard` for all existing memories |
| FR-2 | `skill_router.py` returns `{"skill": "summarize-deep", "depth": "deep"}` for: content_type=`research-paper`, OR word_count > 2000 AND content_type=`article` |
| FR-3 | New `skills/summarize-deep.md` — structured prompt requesting: summary, key findings, notable quotes, implications; `max_tokens: 3000` in frontmatter |
| FR-4 | `MemoryWriter.write()` accepts optional `depth` parameter; writes to frontmatter |
| FR-5 | `/deepen N` command resolves index N from last `/readings` list, re-fetches source URL, runs `summarize-deep`, overwrites the memory file |
| FR-6 | `/deepen` on a memory with no `source_url` (uploaded file) uses stored content if available, otherwise errors |
| FR-7 | `_load_context()` gives a 2x relevance-score bonus to `depth: deep` memories when matched |
| FR-8 | Deep memories displayed with a `📚` prefix in `/readings` list |

---

## Design

### `skills/summarize-deep.md`

```markdown
---
name: summarize-deep
version: 1
preferred_model: claude-sonnet-4-6
max_tokens: 3000
---

## Instructions

You are analyzing a document in depth for a personal knowledge system.

Produce a structured analysis with these sections:

**Summary** (2-3 sentences): What this document is about.

**Key Findings** (bullet list): The most important facts, conclusions, or arguments.

**Notable Quotes** (up to 3): Exact quotes worth preserving verbatim.

**Implications** (1-2 sentences): Why this matters or what it connects to.

Keep total length under 800 words. Be precise and analytical.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
```

### `skill_router.py` changes

Add word count estimation to the routing logic:

```python
word_count = len(content.split()) if content else 0
if content_type == "research-paper" or (content_type == "article" and word_count > 2000):
    return {"skill": "summarize-deep", "depth": "deep"}
```

### `/deepen N` in `chat_handler.py`

```python
async def cmd_deepen(self, update, context):
    ...
    path = self._resolve_reading_index(context.args[0])
    fm = self._parse_frontmatter(path)
    url = fm.get("source_url", "")
    if not url or url.startswith("file://"):
        await update.message.reply_text("Can only deepen URL-sourced memories.")
        return
    title, text = await fetch_url_content(url)
    response = await self.executor.run({"content": text, "url": url},
                                        skill_name="summarize-deep")
    # Overwrite file, updating depth and content fields
    ...
    await update.message.reply_text(f"📚 Deepened: {title}")
```

---

## Test Plan

**Unit tests in `tests/unit/test_skill_router.py`:**

1. `test_research_paper_routes_to_deep` — research-paper content type → skill=summarize-deep
2. `test_long_article_routes_to_deep` — article with >2000 words → skill=summarize-deep
3. `test_short_article_routes_standard` — article with <2000 words → standard skill

**Unit tests in `tests/unit/test_chat_handler.py`:**

4. `test_cmd_deepen_rewrites_memory` — mock fetch + executor, verify file overwritten with depth=deep
5. `test_cmd_deepen_no_url_returns_error` — file:// source_url returns user error
6. `test_cmd_deepen_no_source_url_returns_error` — missing source_url field returns error

**Unit tests in `tests/unit/test_memory_writer.py`:**

7. `test_write_with_depth_deep` — `depth=deep` appears in written frontmatter
8. `test_write_default_depth_standard` — omitting depth writes `depth: standard`
