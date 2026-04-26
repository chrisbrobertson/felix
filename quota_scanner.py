import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("quota-scanner")

DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "quota-scanner-state.json"


# ── Module-level state helpers ───────────────────────────────────────────────

def _load_state() -> dict:
    """Load quota state from JSON file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning("Failed to load quota state: %s", e)
    return {}


def _save_state(state: dict):
    """Atomically save quota state."""
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        os.rename(str(tmp), str(STATE_FILE))
    except Exception as e:
        log.error("Failed to save quota state: %s", e)
        raise


# ── Pure helpers for notification_manager ─────────────────────────────────────

def render_one(platform: str, quota_state: dict) -> str:
    """Render a single platform's quota state as a human-readable string.

    Pure function — can be imported by notification_manager without coupling.
    """
    if platform not in quota_state:
        return f"{platform}: (no data yet)"

    state = quota_state[platform]
    used = state.get("messages_used", 0)
    cap = state.get("messages_cap", 0)
    window_resets_at_str = state.get("window_resets_at", "")
    source = state.get("source", "unknown")

    if not window_resets_at_str:
        return f"{platform}: {used}/{cap} (source: {source}, no reset time)"

    try:
        window_resets_at = datetime.fromisoformat(window_resets_at_str)
        now = datetime.now(window_resets_at.tzinfo or None)

        if window_resets_at <= now:
            return f"{platform}: {used}/{cap} (window elapsed; report current quota)"

        remaining = window_resets_at - now
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)

        if hours > 0:
            reset_str = f"resets in {hours}h{minutes:02d}m"
        else:
            reset_str = f"resets in {minutes}m"

        return f"{platform}: {used}/{cap} ({reset_str})"
    except Exception as e:
        log.warning("Failed to parse window_resets_at for %s: %s", platform, e)
        return f"{platform}: {used}/{cap} (source: {source})"


def detect_threshold_crossings(
    quota_state: dict,
    sent_alerts: dict,
    warn: float,
    crit: float,
    cooldown_min: int,
) -> dict:
    """Detect threshold crossings for all platforms.

    Pure function — notification_manager calls this without holding a scanner instance.

    Args:
        quota_state: Full quota-scanner-state.json contents
        sent_alerts: State dict tracking last alert times per platform per level
        warn: Warning threshold (e.g. 0.75)
        crit: Critical threshold (e.g. 0.90)
        cooldown_min: Cooldown period in minutes (e.g. 60)

    Returns:
        dict[platform, level] — platforms that crossed a threshold and aren't in cooldown
    """
    now = datetime.now()
    crossings = {}

    for platform, state in quota_state.items():
        used = state.get("messages_used", 0)
        cap = state.get("messages_cap", 1)  # avoid div/0
        if cap == 0:
            continue

        utilization = used / cap

        # Determine current level
        current_level = None
        if utilization >= crit:
            current_level = "critical"
        elif utilization >= warn:
            current_level = "warning"

        if current_level is None:
            continue

        # Check cooldown
        key = f"{platform}:{current_level}"
        last_sent_str = sent_alerts.get(key)

        if last_sent_str:
            try:
                last_sent = datetime.fromisoformat(last_sent_str)
                if (now - last_sent).total_seconds() < cooldown_min * 60:
                    continue  # still in cooldown
            except Exception:
                pass

        # Fire alert
        crossings[platform] = current_level
        sent_alerts[key] = now.isoformat()

    return crossings


# ── QuotaScanner ──────────────────────────────────────────────────────────────

class QuotaScanner:
    """Tracks Claude.ai Pro and ChatGPT Plus 5-hour rolling-window message quotas.

    Operates on role == "full" only. Async loop polls every 30 min when scrape is
    enabled; otherwise idles. Self-report path (via /quota report) writes state
    directly without needing a tick.
    """

    def __init__(self, deploy_dir: Path, config: dict, role: str):
        self.deploy_dir = deploy_dir
        self.state_path = deploy_dir / "quota-scanner-state.json"
        self.config = config.get("quota", {})
        self.role = role
        self._state = _load_state()

    def report(self, platform: str, used: int, cap: int, reset_minutes: Optional[int] = None):
        """Self-report current quota (primary path).

        Args:
            platform: "claude" or "chatgpt"
            used: Messages used in current window
            cap: Total message cap for the window
            reset_minutes: Minutes until window resets (default 300 = 5h)
        """
        if reset_minutes is None:
            reset_minutes = 300  # 5 hours

        resets_at = datetime.now() + timedelta(minutes=reset_minutes)

        self._state[platform] = {
            "messages_used": used,
            "messages_cap": cap,
            "window_resets_at": resets_at.isoformat(),
            "source": "self_report",
            "last_seen_at": datetime.now().isoformat(),
        }
        _save_state(self._state)
        log.info("Self-reported quota for %s: %d/%d (resets at %s)",
                 platform, used, cap, resets_at.isoformat())

    def clear(self, platform: str):
        """Clear state for a platform (e.g. when window has rolled over)."""
        if platform in self._state:
            del self._state[platform]
            _save_state(self._state)
            log.info("Cleared quota state for %s", platform)

    def render_status(self) -> str:
        """Render multi-line summary of both platforms."""
        lines = []
        for platform in ["claude", "chatgpt"]:
            lines.append(render_one(platform, self._state))
        return "\n".join(lines)

    async def run_loop(self, stop_event: asyncio.Event):
        """Async loop — 30-min cadence by default. Idles when scrape disabled."""
        if self.role != "full":
            log.info("QuotaScanner disabled on role=%s", self.role)
            return

        interval = self.config.get("poll_interval_minutes", 30) * 60
        log.info("QuotaScanner started — interval: %d seconds", interval)

        while not stop_event.is_set():
            await self._tick()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self):
        """Poll tick — scrapes if enabled, otherwise idles."""
        scrape_enabled = self.config.get("scrape_enabled", False)

        if not scrape_enabled:
            # Self-report-only mode — nothing to do on tick
            return

        # Scraping path — import quota_scrapers only when enabled
        try:
            from quota_scrapers import scrape_claude, scrape_chatgpt
        except ImportError as e:
            log.warning("quota_scrapers not available: %s", e)
            return

        # Attempt to scrape each platform
        for platform, scraper in [("claude", scrape_claude), ("chatgpt", scrape_chatgpt)]:
            cookie_path_key = f"{platform}_cookie_path"
            cookie_path_str = self.config.get(cookie_path_key, "")

            if not cookie_path_str:
                continue

            cookie_path = Path(cookie_path_str).expanduser()
            if not cookie_path.exists():
                log.warning("Cookie path for %s not found: %s", platform, cookie_path)
                continue

            try:
                snapshot = await scraper(cookie_path)
                self._apply_scrape(platform, snapshot)
            except NotImplementedError:
                # Expected — scrapers are stubs
                pass
            except Exception as e:
                log.warning("Quota scrape failed for %s: %s", platform, e)

    def _apply_scrape(self, platform: str, snapshot: dict):
        """Apply scraped snapshot to state."""
        self._state[platform] = {
            "messages_used": snapshot["used"],
            "messages_cap": snapshot["cap"],
            "window_resets_at": snapshot["window_resets_at"],
            "source": "scrape",
            "last_seen_at": datetime.now().isoformat(),
        }
        _save_state(self._state)
        log.info("Scraped quota for %s: %d/%d", platform, snapshot["used"], snapshot["cap"])
