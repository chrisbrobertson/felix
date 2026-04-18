---
specmas: 3.0
kind: feature
id: feat-document-upload
version: 1.0.0
created: 2026-04-17
status: implemented
shipped_version: "1.4.0"
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-pdf-content
---

# Document Upload to Memory

## Overview

### Problem Statement

`/remember <url>` only works with URLs. Users who have a PDF, text file, or other document on their device cannot create a memory from it without first hosting it at a URL. Filed as feature `1e823a`.

### Scope

**In scope:**
- Telegram document/file upload handler in `chat_handler.py`
- Supported file types: PDF, plain text (`.txt`), Markdown (`.md`)
- PDF text extraction via `content_fetcher._extract_pdf()` (from `feat-pdf-content`)
- Text/Markdown read directly, truncated to 8000 chars
- Route through `skill_router` to pick the right summarization skill
- Write memory via existing `MemoryWriter`
- Source URL set to `file://{original_filename}` for traceability
- Reply with title, filename, and 300-char preview (same as `/remember`)

**Out of scope:**
- DOCX, EPUB, spreadsheets, or presentation formats
- Images or audio files
- Files larger than 20MB (Telegram Bot API limit — reject with user message)
- Inline text pasting (use `/note` for that)

### Success Metrics

- User uploads a PDF → memory file written with extracted text
- User uploads a `.txt` → memory file written
- Unsupported file type → friendly error message, no crash

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `handle_message` checks `update.message.document` in addition to `update.message.text` |
| FR-2 | Supported MIME types: `application/pdf`, `text/plain`, `text/markdown` |
| FR-3 | Unsupported MIME type returns: "Unsupported file type. Send a PDF or .txt file." |
| FR-4 | File downloaded via `context.bot.get_file()` → `await file.download_as_bytearray()` |
| FR-5 | PDF bytes → `content_fetcher._extract_pdf(data, filename)` |
| FR-6 | Text bytes → decoded as UTF-8 (replace errors), truncated to 8000 chars; title = filename stem |
| FR-7 | Content type detection via `skill_router.detect_content_type()` passing filename as the URL hint |
| FR-8 | Skill run and memory write identical to `/remember` flow |
| FR-9 | `source_url` in memory frontmatter set to `file://{original_filename}` |
| FR-10 | Files > 20MB rejected before download attempt |

---

## Design

### Message handler extension in `chat_handler.py`

The existing `handle_message` method currently only processes `update.message.text`. Add an early branch:

```python
if update.message.document:
    await self._handle_document_upload(update, context)
    return
```

### New `_handle_document_upload()` method

```python
async def _handle_document_upload(self, update, context):
    doc = update.message.document
    fname = doc.file_name or "upload"
    mime = doc.mime_type or ""

    SUPPORTED = {"application/pdf", "text/plain", "text/markdown"}
    if mime not in SUPPORTED and not any(fname.lower().endswith(ext) for ext in (".pdf", ".txt", ".md")):
        await update.message.reply_text("Unsupported file type. Send a PDF or .txt file.")
        return

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("File too large (max 20 MB).")
        return

    await update.message.reply_text(f"Processing {fname}…")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = await tg_file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}")
        return

    is_pdf = "pdf" in mime or fname.lower().endswith(".pdf")
    if is_pdf:
        from content_fetcher import _extract_pdf
        title, text = _extract_pdf(bytes(data), fname)
    else:
        title = Path(fname).stem.replace("-", " ").replace("_", " ").title()
        text = bytes(data).decode("utf-8", errors="replace")[:8000]

    if not text.strip():
        await update.message.reply_text("Couldn't extract text from that file.")
        return

    source_url = f"file://{fname}"
    content_type = skill_router.detect_content_type(source_url, text)
    skill_name = skill_router.route(content_type)

    response = await self.executor.run({"content": text, "url": source_url})
    if not response:
        await update.message.reply_text("Summarization failed — check error.log.")
        return

    path = MemoryWriter().write(title=title or fname, url=source_url, summary=response, tags=[])
    preview = response[:300].replace("\n", " ")
    await update.message.reply_text(f"📄 Saved: {title}\n{path.name}\n\n{preview}…")
```

---

## Test Plan

**Unit tests in `tests/unit/test_chat_handler.py`:**

1. `test_document_upload_pdf_creates_memory` — mock document message with PDF mime type, assert memory written
2. `test_document_upload_txt_creates_memory` — plain text file produces memory
3. `test_document_upload_unsupported_type_rejected` — DOCX mime type → error reply, no memory
4. `test_document_upload_too_large_rejected` — file_size > 20MB → size error reply
5. `test_document_upload_empty_content_rejected` — empty extracted text → user message, no memory
6. `test_document_upload_source_url_scheme` — `source_url` in memory frontmatter starts with `file://`
