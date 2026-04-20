"""Shared URL content fetcher used by browser_watcher and on-demand /remember."""
import io
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("content-fetcher")

_NOISE_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form"]
_MAX_CONTENT_CHARS = 8000
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Validate URL against SSRF policy.

    Returns (True, "") if safe, (False, reason) if unsafe.
    Rejects:
    - Non-HTTP(S) schemes
    - Private, loopback, link-local, reserved, multicast IPs
    - DNS resolution failures
    """
    try:
        parsed = urlparse(url)

        # Reject non-HTTP schemes
        if parsed.scheme not in {"http", "https"}:
            return False, f"scheme {parsed.scheme!r} not allowed"

        hostname = parsed.hostname
        if not hostname:
            return False, "missing hostname"

        # Resolve all IPs for hostname
        try:
            addr_info = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False, "DNS resolution failed"

        # Check each resolved IP
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if any([
                    ip.is_private,
                    ip.is_loopback,
                    ip.is_link_local,
                    ip.is_reserved,
                    ip.is_multicast,
                ]):
                    return False, f"IP {ip_str} is non-public"
            except ValueError:
                # Invalid IP format — reject
                return False, f"invalid IP {ip_str!r}"

        return True, ""
    except Exception as e:
        return False, f"validation error: {e}"


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


async def fetch_url_content(url: str) -> tuple:
    """Fetch a URL and return (title, cleaned_text).

    Returns ("", "") on failure rather than raising, so callers can
    decide how to handle a fetch failure without try/except boilerplate.
    """
    # SSRF guard — block private IPs and non-HTTP schemes
    safe, reason = _is_safe_url(url)
    if not safe:
        log.warning("fetch_url_content: URL blocked by SSRF policy (%s): %s", reason, url)
        return "", ""

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
