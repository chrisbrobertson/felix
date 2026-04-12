import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch
from github_client import GitHubClient, GitHubNotConfigured


@pytest.mark.asyncio
async def test_enabled_when_pat_and_repo_set():
    with patch.dict(os.environ, {"GITHUB_PAT": "testpat", "GITHUB_REPO": "owner/repo"}):
        client = GitHubClient()
        assert client.enabled is True


@pytest.mark.asyncio
async def test_disabled_when_pat_missing():
    with patch.dict(os.environ, {}, clear=True):
        client = GitHubClient(repo="owner/repo")
        assert client.enabled is False


@pytest.mark.asyncio
async def test_disabled_when_repo_missing():
    with patch.dict(os.environ, {"GITHUB_PAT": "testpat"}, clear=True):
        client = GitHubClient()
        assert client.enabled is False


@pytest.mark.asyncio
async def test_disabled_when_repo_has_no_slash():
    with patch.dict(os.environ, {"GITHUB_PAT": "testpat", "GITHUB_REPO": "noslash"}):
        client = GitHubClient()
        assert client.enabled is False


@pytest.mark.asyncio
async def test_create_issue_posts_correct_payload():
    client = GitHubClient(repo="owner/repo", pat="mytoken")
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"number": 42, "html_url": "https://github.com/owner/repo/issues/42"}
    mock_response.raise_for_status = MagicMock()
    mock_post = AsyncMock(return_value=mock_response)

    # Initialize the internal httpx client and patch its post method
    _ = client._ensure_client()
    client._client.post = mock_post

    result = await client.create_issue("Test issue", "body text", ["kind:feature"])

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args[1]
    assert "kind:feature" in call_kwargs["json"]["labels"]
    assert call_kwargs["json"]["title"] == "Test issue"
    assert call_kwargs["json"]["body"] == "body text"
    assert result["number"] == 42


@pytest.mark.asyncio
async def test_list_issues_filters_out_prs():
    client = GitHubClient(repo="owner/repo", pat="mytoken")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"number": 1, "title": "Issue 1"},
        {"number": 2, "title": "PR 2", "pull_request": {"url": "https://..."}},
        {"number": 3, "title": "Issue 3"},
    ]
    mock_response.raise_for_status = MagicMock()
    mock_get = AsyncMock(return_value=mock_response)

    _ = client._ensure_client()
    client._client.get = mock_get

    result = await client.list_issues()

    assert len(result) == 2
    assert result[0]["number"] == 1
    assert result[1]["number"] == 3


@pytest.mark.asyncio
async def test_update_issue_patches_state():
    client = GitHubClient(repo="owner/repo", pat="mytoken")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"number": 42, "state": "closed", "state_reason": "completed"}
    mock_response.raise_for_status = MagicMock()
    mock_patch = AsyncMock(return_value=mock_response)

    _ = client._ensure_client()
    client._client.patch = mock_patch

    result = await client.update_issue(42, state="closed", state_reason="completed")

    mock_patch.assert_called_once()
    call_kwargs = mock_patch.call_args[1]
    assert call_kwargs["json"]["state"] == "closed"
    assert call_kwargs["json"]["state_reason"] == "completed"
    assert result["number"] == 42


@pytest.mark.asyncio
async def test_ensure_labels_ignores_422():
    client = GitHubClient(repo="owner/repo", pat="mytoken")

    # First POST returns 422 (already exists), second returns 201
    response_422 = MagicMock()
    response_422.status_code = 422
    response_422.raise_for_status = MagicMock()

    response_201 = MagicMock()
    response_201.status_code = 201
    response_201.raise_for_status = MagicMock()

    mock_post = AsyncMock(side_effect=[response_422, response_201])

    _ = client._ensure_client()
    client._client.post = mock_post

    # Should not raise an exception
    await client.ensure_labels([
        {"name": "existing", "color": "000000", "description": "Existing label"},
        {"name": "new", "color": "ffffff", "description": "New label"},
    ])

    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_get_comments():
    client = GitHubClient(repo="owner/repo", pat="mytoken")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"id": 1, "body": "First comment", "created_at": "2024-01-01T10:00:00Z"},
        {"id": 2, "body": "Second comment", "created_at": "2024-01-02T10:00:00Z"},
    ]
    mock_response.raise_for_status = MagicMock()
    mock_get = AsyncMock(return_value=mock_response)

    _ = client._ensure_client()
    client._client.get = mock_get

    result = await client.get_comments(42)

    assert len(result) == 2
    assert result[0]["body"] == "First comment"
    assert result[1]["body"] == "Second comment"


@pytest.mark.asyncio
async def test_ensure_client_raises_when_not_enabled():
    with patch.dict(os.environ, {}, clear=True):
        client = GitHubClient()
        with pytest.raises(GitHubNotConfigured):
            client._ensure_client()
