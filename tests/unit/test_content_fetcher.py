"""Unit tests for content_fetcher.py."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import content_fetcher as cf


def _make_response(status: int, html: str):
    r = MagicMock()
    r.status_code = status
    r.text = html
    return r


@pytest.mark.asyncio
async def test_fetch_returns_title_and_text():
    html = "<html><head><title>Hello World</title></head><body><p>Useful content here.</p></body></html>"
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_make_response(200, html))

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        title, text = await cf.fetch_url_content("https://example.com/article")

    assert title == "Hello World"
    assert "Useful content" in text


@pytest.mark.asyncio
async def test_fetch_strips_noise_tags():
    html = (
        "<html><body>"
        "<nav>Skip nav</nav>"
        "<p>Real content.</p>"
        "<footer>Footer junk</footer>"
        "</body></html>"
    )
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_make_response(200, html))

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        _, text = await cf.fetch_url_content("https://example.com/")

    assert "Real content" in text
    assert "Skip nav" not in text
    assert "Footer junk" not in text


@pytest.mark.asyncio
async def test_fetch_non_200_returns_empty():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_make_response(404, ""))

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        title, text = await cf.fetch_url_content("https://example.com/missing")

    assert title == ""
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_network_error_returns_empty():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        title, text = await cf.fetch_url_content("https://unreachable.example.com/")

    assert title == ""
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_truncates_to_max_chars():
    huge_body = "x " * 10000
    html = f"<html><body><p>{huge_body}</p></body></html>"
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_make_response(200, html))

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        _, text = await cf.fetch_url_content("https://example.com/huge")

    assert len(text) <= cf._MAX_CONTENT_CHARS
