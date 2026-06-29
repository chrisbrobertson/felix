# github_client.py
import logging
import os
from typing import Optional
import httpx

from secrets import get_secret_or_env

log = logging.getLogger("github-client")

# Standard labels for feature/bug lifecycle
_STANDARD_LABELS = [
    {"name": "kind:feature", "color": "0075ca", "description": "New feature or enhancement"},
    {"name": "kind:bug",     "color": "d73a4a", "description": "Something isn't working"},
    {"name": "status:planned",     "color": "cfd3d7", "description": "Planned but not started"},
    {"name": "status:in-progress", "color": "fbca04", "description": "Currently being worked on"},
    {"name": "priority:low",      "color": "e4e669", "description": "Low priority"},
    {"name": "priority:medium",   "color": "ffa500", "description": "Normal priority"},
    {"name": "priority:high",     "color": "e11d48", "description": "High priority"},
    {"name": "priority:critical", "color": "b60205", "description": "Critical — blocking"},
]


class GitHubNotConfigured(Exception):
    """Raised when GITHUB_PAT or the repo is missing."""


class GitHubClient:
    """Thin async wrapper around the Issues API for a single repo."""

    BASE = "https://api.github.com"

    def __init__(self, repo: Optional[str] = None, pat: Optional[str] = None):
        self.pat = pat or get_secret_or_env("github_pat", "GITHUB_PAT") or ""
        self.repo = repo or os.environ.get("GITHUB_REPO", "")
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self.pat and self.repo and "/" in self.repo)

    def _ensure_client(self) -> httpx.AsyncClient:
        if not self.enabled:
            raise GitHubNotConfigured("GITHUB_PAT and GITHUB_REPO must be set")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE,
                headers={
                    "Authorization": f"Bearer {self.pat}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Issue operations ────────────────────────────────────────────────
    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        r = await self._ensure_client().post(
            f"/repos/{self.repo}/issues",
            json={"title": title, "body": body, "labels": labels},
        )
        r.raise_for_status()
        return r.json()

    async def list_issues(self, state: str = "open", labels: Optional[list] = None,
                          per_page: int = 50) -> list:
        params = {"state": state, "per_page": per_page, "sort": "updated", "direction": "desc"}
        if labels:
            params["labels"] = ",".join(labels)
        r = await self._ensure_client().get(f"/repos/{self.repo}/issues", params=params)
        r.raise_for_status()
        # GitHub's list_issues includes PRs — filter them out
        return [i for i in r.json() if "pull_request" not in i]

    async def get_issue(self, number: int) -> dict:
        r = await self._ensure_client().get(f"/repos/{self.repo}/issues/{number}")
        r.raise_for_status()
        return r.json()

    async def update_issue(self, number: int, **fields) -> dict:
        """fields may include: state, state_reason, title, body, labels."""
        r = await self._ensure_client().patch(
            f"/repos/{self.repo}/issues/{number}", json=fields,
        )
        r.raise_for_status()
        return r.json()

    async def replace_labels(self, number: int, labels: list[str]) -> None:
        r = await self._ensure_client().put(
            f"/repos/{self.repo}/issues/{number}/labels", json={"labels": labels},
        )
        r.raise_for_status()

    async def add_comment(self, number: int, body: str) -> dict:
        r = await self._ensure_client().post(
            f"/repos/{self.repo}/issues/{number}/comments", json={"body": body},
        )
        r.raise_for_status()
        return r.json()

    async def get_comments(self, number: int) -> list[dict]:
        """Get all comments for an issue."""
        r = await self._ensure_client().get(f"/repos/{self.repo}/issues/{number}/comments")
        r.raise_for_status()
        return r.json()

    async def ensure_labels(self, labels: list) -> None:
        """labels is a list of {name, color, description}. Idempotent."""
        for lb in labels:
            r = await self._ensure_client().post(
                f"/repos/{self.repo}/labels", json=lb,
            )
            if r.status_code == 422:  # already exists
                continue
            r.raise_for_status()

    # ── Pull Request operations ─────────────────────────────────────────
    async def list_pull_requests(self, state: str = "open", per_page: int = 30) -> list:
        params = {"state": state, "per_page": per_page, "sort": "updated", "direction": "desc"}
        r = await self._ensure_client().get(f"/repos/{self.repo}/pulls", params=params)
        r.raise_for_status()
        return r.json()
