"""
Unit tests for quota_scrapers.py.

The scraping functions are intentional stubs — actual HTTP scraping is
not implemented. These tests cover the pre-flight guards and document the
expected failure-path behaviour so that a future implementation can't
accidentally regress to raising on inputs that should silently return {}.
"""
import pytest

from quota_scrapers import scrape_claude, scrape_chatgpt


# ── scrape_claude ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scrape_claude_missing_cookie_file(tmp_path):
    """Returns {} when cookie file does not exist — never raises."""
    result = await scrape_claude(tmp_path / "missing.json")
    assert result == {}


@pytest.mark.asyncio
async def test_scrape_claude_not_implemented_when_cookie_present(tmp_path):
    """Raises NotImplementedError when cookie file exists (scraping stub)."""
    cookie = tmp_path / "cookies.json"
    cookie.write_text("{}")
    with pytest.raises(NotImplementedError):
        await scrape_claude(cookie)


# ── scrape_chatgpt ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scrape_chatgpt_missing_cookie_file(tmp_path):
    """Returns {} when cookie file does not exist — never raises."""
    result = await scrape_chatgpt(tmp_path / "missing.json")
    assert result == {}


@pytest.mark.asyncio
async def test_scrape_chatgpt_not_implemented_when_cookie_present(tmp_path):
    """Raises NotImplementedError when cookie file exists (scraping stub)."""
    cookie = tmp_path / "cookies.json"
    cookie.write_text("{}")
    with pytest.raises(NotImplementedError):
        await scrape_chatgpt(cookie)
