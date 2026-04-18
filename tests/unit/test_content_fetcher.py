"""Unit tests for content_fetcher.py."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import content_fetcher as cf


def _make_stream_ctx(status: int, html: str, content_type: str = "text/html"):
    """Build a mock for `async with client.stream(...) as r:` returning given HTML."""
    html_bytes = html.encode("utf-8") if isinstance(html, str) else html

    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.headers = {"content-type": content_type}

    async def _aiter_bytes():
        yield html_bytes

    mock_response.aiter_bytes = _aiter_bytes

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    return mock_stream_ctx


def _make_client_mock(stream_ctx):
    """Build a mock AsyncClient whose .stream() returns stream_ctx."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=stream_ctx)
    return mock_client


@pytest.mark.asyncio
async def test_fetch_returns_title_and_text():
    html = "<html><head><title>Hello World</title></head><body><p>Useful content here.</p></body></html>"
    stream_ctx = _make_stream_ctx(200, html)
    mock_client = _make_client_mock(stream_ctx)

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
    stream_ctx = _make_stream_ctx(200, html)
    mock_client = _make_client_mock(stream_ctx)

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        _, text = await cf.fetch_url_content("https://example.com/")

    assert "Real content" in text
    assert "Skip nav" not in text
    assert "Footer junk" not in text


@pytest.mark.asyncio
async def test_fetch_non_200_returns_empty():
    stream_ctx = _make_stream_ctx(404, "")
    mock_client = _make_client_mock(stream_ctx)

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        title, text = await cf.fetch_url_content("https://example.com/missing")

    assert title == ""
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_network_error_returns_empty():
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(side_effect=Exception("Connection refused"))

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        title, text = await cf.fetch_url_content("https://unreachable.example.com/")

    assert title == ""
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_truncates_to_max_chars():
    huge_body = "x " * 10000
    html = f"<html><body><p>{huge_body}</p></body></html>"
    stream_ctx = _make_stream_ctx(200, html)
    mock_client = _make_client_mock(stream_ctx)

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        _, text = await cf.fetch_url_content("https://example.com/huge")

    assert len(text) <= cf._MAX_CONTENT_CHARS


@pytest.mark.asyncio
async def test_fetch_truncates_oversized_response():
    """Responses exceeding 10MB are truncated and still return parsed content."""
    # Build a response that's just over 10MB when encoded
    chunk_size = cf._MAX_RESPONSE_BYTES + 1024
    big_html = b"<html><body><p>" + b"a" * chunk_size + b"</p></body></html>"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html"}

    async def _aiter_bytes():
        yield big_html  # single chunk over limit

    mock_response.aiter_bytes = _aiter_bytes

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    with patch("content_fetcher.httpx.AsyncClient", return_value=mock_client):
        title, text = await cf.fetch_url_content("https://example.com/huge")

    # Should not crash; content is truncated
    assert len(text) <= cf._MAX_CONTENT_CHARS
