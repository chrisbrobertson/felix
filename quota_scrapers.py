"""Best-effort scrapers for Claude.ai Pro and ChatGPT Plus quota counters.

WARNING: These scrape vendor web UIs that have no quota API. They will
break whenever Anthropic or OpenAI ships a layout change. They may also
violate vendor Terms of Service. Both functions are imported only when
the user explicitly sets quota.scrape_enabled = true and supplies a
session cookie path.

Use at your own risk. The recommended path is manual self-report via
/quota report <platform> <used>/<cap> [reset <minutes>].
"""

from pathlib import Path


async def scrape_claude(cookie_path: Path) -> dict:
    """Scrape Claude.ai Pro quota from the web UI.

    Args:
        cookie_path: Path to saved session cookie file

    Returns:
        dict with keys: used, cap, window_resets_at (ISO timestamp)

    Raises:
        NotImplementedError: Scraping not yet implemented
    """
    raise NotImplementedError(
        "Scraping not yet implemented; use /quota report for self-report."
    )


async def scrape_chatgpt(cookie_path: Path) -> dict:
    """Scrape ChatGPT Plus quota from the web UI.

    Args:
        cookie_path: Path to saved session cookie file

    Returns:
        dict with keys: used, cap, window_resets_at (ISO timestamp)

    Raises:
        NotImplementedError: Scraping not yet implemented
    """
    raise NotImplementedError(
        "Scraping not yet implemented; use /quota report for self-report."
    )
