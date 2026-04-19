import asyncio
import logging
from typing import Optional

import httpx

log = logging.getLogger("slack-client")

SLACK_API_BASE = "https://slack.com/api"


class SlackClient:
    """Shared Slack API client — used by slack_scanner.py and slack_adapter.py.

    token: pass xoxp- for scanner (user token), xoxb- for chat adapter (bot token).
    """

    def __init__(self, token: str):
        self._token = token
        self._user_cache: dict[str, str] = {}

    async def api_call(
        self, method: str, params: dict = None, *, _retry: int = 0
    ) -> Optional[dict]:
        """Rate-limit-aware API call. Returns parsed JSON dict or None on error."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{SLACK_API_BASE}/{method}",
                    headers={"Authorization": f"Bearer {self._token}"},
                    params=params or {},
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    log.warning("Slack rate limited — waiting %ds (method=%s)", retry_after, method)
                    if _retry < 1:
                        await asyncio.sleep(retry_after)
                        return await self.api_call(method, params, _retry=_retry + 1)
                    log.error("Persistent rate limit on %s — skipping", method)
                    return None
                if resp.status_code == 401:
                    log.error("Slack API auth failed (401) on %s", method)
                    return None
                resp.raise_for_status()
                try:
                    data = resp.json()
                except Exception:
                    log.warning("Slack API returned non-JSON response for %s", method)
                    return None
                if not data.get("ok"):
                    log.warning("Slack API error on %s: %s", method, data.get("error", "unknown"))
                    return None
                return data
        except httpx.HTTPStatusError as e:
            log.warning("Slack API HTTP error %s: %s", e.response.status_code, method)
            return None
        except Exception as e:
            log.warning("Slack API request failed (%s): %s", method, e)
            return None

    async def resolve_user(self, user_id: str) -> str:
        """Resolve Slack user_id to display name, with in-memory cache."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        data = await self.api_call("users.info", {"user": user_id})
        if data and data.get("user"):
            name = data["user"].get("real_name") or data["user"].get("name") or "Unknown User"
        else:
            name = "Unknown User"
        self._user_cache[user_id] = name
        await asyncio.sleep(1)  # rate-limit compliance
        return name

    async def list_channels(self) -> list[tuple[str, str]]:
        """Return list of (channel_id, channel_name) tuples for channels user is a member of."""
        channels = []
        cursor = None
        while True:
            params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self.api_call("users.conversations", params)
            if not data:
                break
            for ch in data.get("channels", []):
                if not ch.get("is_archived", False):
                    channels.append((ch["id"], ch.get("name", "unknown")))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            await asyncio.sleep(1)
        log.info("Enumerated %d channels via users.conversations", len(channels))
        return channels

    async def post_message(self, channel: str, text: str) -> bool:
        """Send a message to a channel or DM. Returns True on success."""
        data = await self.api_call("chat.postMessage", {"channel": channel, "text": text})
        return data is not None

    def clear_user_cache(self) -> None:
        """Clear the in-memory user display-name cache."""
        self._user_cache.clear()
