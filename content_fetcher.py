"""Shared URL content fetcher used by browser_watcher and on-demand /remember."""
import logging

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("content-fetcher")

_NOISE_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form"]
_MAX_CONTENT_CHARS = 8000


async def fetch_url_content(url: str) -> tuple:
    """Fetch a URL and return (title, cleaned_text).

    Returns ("", "") on failure rather than raising, so callers can
    decide how to handle a fetch failure without try/except boilerplate.
    """
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                log.debug("fetch_url_content: HTTP %s for %s", r.status_code, url)
                return "", ""
            soup = BeautifulSoup(r.text, "lxml")
            title = soup.title.string.strip() if soup.title and soup.title.string else ""
            for tag in soup(_NOISE_TAGS):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            return title, text[:_MAX_CONTENT_CHARS]
    except Exception as e:
        log.debug("fetch_url_content failed for %s: %s", url, e)
        return "", ""
