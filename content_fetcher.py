"""Shared URL content fetcher used by browser_watcher and on-demand /remember."""
import io
import logging

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("content-fetcher")

_NOISE_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form"]
_MAX_CONTENT_CHARS = 8000


def _extract_pdf(data: bytes, url: str) -> tuple:
    """Extract (title, text) from PDF bytes using pdfminer.six.

    Returns (title, text) on success, ("", "") on failure.
    """
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


_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


async def fetch_url_content(url: str) -> tuple:
    """Fetch a URL and return (title, cleaned_text).

    Returns ("", "") on failure rather than raising, so callers can
    decide how to handle a fetch failure without try/except boilerplate.
    """
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status_code != 200:
                    log.debug("fetch_url_content: HTTP %s for %s", r.status_code, url)
                    return "", ""

                chunks = []
                total = 0
                async for chunk in r.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        log.warning("fetch_url_content: response too large for %s, truncating at 10MB", url)
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)

            content_type = r.headers.get("content-type", "").lower()
            is_pdf = "application/pdf" in content_type or url.lower().split("?")[0].endswith(".pdf")

            if is_pdf:
                return _extract_pdf(data, url)

            text_content = data.decode("utf-8", errors="replace")
            soup = BeautifulSoup(text_content, "lxml")
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            for tag in soup(_NOISE_TAGS):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            return title, text[:_MAX_CONTENT_CHARS]
    except Exception as e:
        log.debug("fetch_url_content failed for %s: %s", url, e)
        return "", ""
