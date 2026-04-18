---
specmas: 3.0
kind: bug
id: feat-pdf-content
version: 1.0.0
created: 2026-04-17
status: implemented
shipped_version: "1.4.0"
complexity: small
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-document-upload
---

# PDF Content Extraction

## Overview

### Problem Statement

`content_fetcher.py` fetches URLs using `httpx` and parses the response as HTML via BeautifulSoup. When a PDF URL is requested, the response body is binary data — `r.text` produces garbled output, BeautifulSoup finds no title and no useful text, and `fetch_url_content()` returns `("", "")`. The user cannot create memories from PDF URLs via `/remember` or browser watcher. Filed as bug `59fd6c`.

### Scope

**In scope:**
- PDF content extraction added to `content_fetcher.py`
- Detection via `Content-Type: application/pdf` response header or `.pdf` URL suffix
- Text extraction using `pdfminer.six` (pure Python, no system dependencies)
- Truncation to the same 8000-char budget as HTML
- Title extracted from PDF metadata (`/Title` field) with URL-based fallback
- `requirements.txt` updated with `pdfminer.six`

**Out of scope:**
- OCR for scanned/image PDFs (text-layer only)
- Password-protected PDFs
- Extracting figures, tables, or embedded images
- DOCX, EPUB, or other document formats (separate feature)

### Success Metrics

- `/remember <pdf-url>` produces a populated memory file with extracted text
- PDF memories are classified as `research-paper` by skill_router (already working)
- Existing HTML fetch behavior unchanged

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `fetch_url_content()` detects PDF by `Content-Type: application/pdf` header or URL ending in `.pdf` (case-insensitive) |
| FR-2 | PDF bytes extracted to plain text using `pdfminer.six` `extract_text()` |
| FR-3 | Title extracted from PDF `/Title` metadata; falls back to the last path segment of the URL (without extension) |
| FR-4 | Extracted text truncated to 8000 characters (same as HTML path) |
| FR-5 | Returns `("", "")` on extraction failure (same contract as HTML path) |
| FR-6 | Non-PDF responses continue through the existing HTML parse path unchanged |

---

## Design

### `content_fetcher.py` changes

```python
async def fetch_url_content(url: str) -> tuple:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return "", ""

        content_type = r.headers.get("content-type", "")
        is_pdf = "application/pdf" in content_type or url.lower().endswith(".pdf")

        if is_pdf:
            return _extract_pdf(r.content, url)

        # existing HTML path unchanged
        soup = BeautifulSoup(r.text, "lxml")
        ...

def _extract_pdf(data: bytes, url: str) -> tuple:
    """Extract (title, text) from PDF bytes using pdfminer.six."""
    import io
    try:
        from pdfminer.high_level import extract_text
        from pdfminer.pdfpage import PDFPage
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfparser import PDFParser

        # Extract title from metadata
        title = ""
        try:
            parser = PDFParser(io.BytesIO(data))
            doc = PDFDocument(parser)
            info = doc.info[0] if doc.info else {}
            raw_title = info.get("Title", b"")
            if isinstance(raw_title, bytes):
                title = raw_title.decode("utf-8", errors="replace").strip()
            else:
                title = str(raw_title).strip()
        except Exception:
            pass

        if not title:
            # Fall back to URL filename
            from pathlib import PurePosixPath
            title = PurePosixPath(url.split("?")[0]).stem.replace("-", " ").replace("_", " ").title()

        text = extract_text(io.BytesIO(data))
        return title, text[:_MAX_CONTENT_CHARS]
    except Exception as e:
        log.debug("PDF extraction failed for %s: %s", url, e)
        return "", ""
```

### `requirements.txt`

Add: `pdfminer.six>=20221105`

---

## Test Plan

**Unit tests in `tests/unit/test_content_fetcher.py`:**

1. `test_pdf_detected_by_content_type` — mock response with `Content-Type: application/pdf`, assert `_extract_pdf` is called
2. `test_pdf_detected_by_url_suffix` — `.pdf` URL with `text/html` content type still routes to PDF extractor
3. `test_pdf_returns_title_from_metadata` — PDF with `/Title` metadata returns that as the title
4. `test_pdf_title_fallback_from_url` — PDF with no `/Title` returns URL stem as title
5. `test_pdf_text_truncated_to_8000` — PDF with 10000+ chars of text returns exactly 8000
6. `test_pdf_extraction_failure_returns_empty` — corrupt PDF data returns `("", "")`
7. `test_html_path_unchanged` — non-PDF response still goes through BeautifulSoup path
