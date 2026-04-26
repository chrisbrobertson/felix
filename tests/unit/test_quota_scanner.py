"""
Unit tests for quota_scanner.

All external access (filesystem, network) is mocked.
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import quota_scanner as qs
from quota_scanner import QuotaScanner, render_one, detect_threshold_crossings


# ── Test fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def mock_deploy_dir(tmp_path, monkeypatch):
    """Redirect DEPLOY_DIR to tmp_path."""
    monkeypatch.setattr(qs, "DEPLOY_DIR", tmp_path)
    monkeypatch.setattr(qs, "STATE_FILE", tmp_path / "quota-scanner-state.json")
    return tmp_path


# ── Self-report tests ─────────────────────────────────────────────────────────

def test_self_report_persists_state(tmp_path, mock_deploy_dir):
    """Self-report writes state with correct shape."""
    scanner = QuotaScanner(tmp_path, {"quota": {}}, "full")

    before = datetime.now()
    scanner.report("claude", 23, 40)
    after = datetime.now()

    state_file = tmp_path / "quota-scanner-state.json"
    assert state_file.exists()

    state = json.loads(state_file.read_text())
    assert "claude" in state

    claude_state = state["claude"]
    assert claude_state["messages_used"] == 23
    assert claude_state["messages_cap"] == 40
    assert claude_state["source"] == "self_report"

    # Window resets at roughly now + 5h (300 min)
    resets_at = datetime.fromisoformat(claude_state["window_resets_at"])
    expected = before + timedelta(minutes=300)
    # Allow 2-second tolerance for test execution time
    assert abs((resets_at - expected).total_seconds()) < 2


def test_self_report_with_explicit_reset(tmp_path, mock_deploy_dir):
    """Self-report with custom reset minutes."""
    scanner = QuotaScanner(tmp_path, {"quota": {}}, "full")

    before = datetime.now()
    scanner.report("claude", 23, 40, reset_minutes=90)
    after = datetime.now()

    state_file = tmp_path / "quota-scanner-state.json"
    state = json.loads(state_file.read_text())

    resets_at = datetime.fromisoformat(state["claude"]["window_resets_at"])
    expected = before + timedelta(minutes=90)
    assert abs((resets_at - expected).total_seconds()) < 2


def test_render_status_both_platforms(tmp_path, mock_deploy_dir):
    """Render status when both platforms have data."""
    scanner = QuotaScanner(tmp_path, {"quota": {}}, "full")
    scanner.report("claude", 10, 40)
    scanner.report("chatgpt", 20, 40)

    status = scanner.render_status()
    assert "claude:" in status.lower()
    assert "chatgpt:" in status.lower()
    assert "10/40" in status
    assert "20/40" in status


def test_render_status_unknown_when_unset(tmp_path, mock_deploy_dir):
    """Render shows 'no data yet' for missing platforms."""
    scanner = QuotaScanner(tmp_path, {"quota": {}}, "full")

    status = scanner.render_status()
    assert "claude:" in status.lower()
    assert "chatgpt:" in status.lower()
    assert "no data yet" in status.lower()


def test_window_decay_text(tmp_path, mock_deploy_dir):
    """Render shows 'resets in Xm' based on window_resets_at."""
    scanner = QuotaScanner(tmp_path, {"quota": {}}, "full")

    # Report with 60-minute reset
    scanner.report("claude", 10, 40, reset_minutes=60)

    # Immediately check — should show roughly 60m (or 59m due to sub-second elapsed time)
    status = scanner.render_status()
    # Could be "resets in 59m" or "resets in 1h00m" depending on exact timing
    assert "resets in" in status.lower()


def test_threshold_warning_fires_once_per_window(tmp_path):
    """Threshold crossing fires alert, then cooldown suppresses duplicate."""
    quota_state = {
        "claude": {
            "messages_used": 30,
            "messages_cap": 40,
            "window_resets_at": (datetime.now() + timedelta(hours=2)).isoformat(),
        }
    }
    sent_alerts = {}

    # First check — should cross warning threshold (30/40 = 75%)
    crossings = detect_threshold_crossings(
        quota_state, sent_alerts, warn=0.75, crit=0.90, cooldown_min=60
    )
    assert "claude" in crossings
    assert crossings["claude"] == "warning"

    # Second check immediately — cooldown suppresses
    crossings2 = detect_threshold_crossings(
        quota_state, sent_alerts, warn=0.75, crit=0.90, cooldown_min=60
    )
    assert "claude" not in crossings2


def test_threshold_critical_after_warning(tmp_path):
    """Both warning and critical fire independently (separate cooldowns)."""
    quota_state = {
        "claude": {
            "messages_used": 30,
            "messages_cap": 40,
            "window_resets_at": (datetime.now() + timedelta(hours=2)).isoformat(),
        }
    }
    sent_alerts = {}

    # Cross warning at 75%
    crossings1 = detect_threshold_crossings(
        quota_state, sent_alerts, warn=0.75, crit=0.90, cooldown_min=60
    )
    assert crossings1["claude"] == "warning"

    # Update to 90% — cross critical
    quota_state["claude"]["messages_used"] = 36
    crossings2 = detect_threshold_crossings(
        quota_state, sent_alerts, warn=0.75, crit=0.90, cooldown_min=60
    )
    assert "claude" in crossings2
    assert crossings2["claude"] == "critical"


def test_clear_platform_state(tmp_path, mock_deploy_dir):
    """Clear removes platform key, leaves others intact."""
    scanner = QuotaScanner(tmp_path, {"quota": {}}, "full")
    scanner.report("claude", 10, 40)
    scanner.report("chatgpt", 20, 40)

    scanner.clear("claude")

    state_file = tmp_path / "quota-scanner-state.json"
    state = json.loads(state_file.read_text())

    assert "claude" not in state
    assert "chatgpt" in state


def test_scrape_disabled_no_module_import(tmp_path, mock_deploy_dir):
    """Scanner constructs and ticks when scrape_enabled=false (no import attempt)."""
    scanner = QuotaScanner(tmp_path, {"quota": {"scrape_enabled": False}}, "full")

    # Tick should succeed without importing quota_scrapers
    import asyncio
    asyncio.run(scanner._tick())

    # No exception = success


@pytest.mark.asyncio
async def test_scrape_failure_silent_with_24h_nudge(tmp_path, mock_deploy_dir):
    """Scraper failure is silent; nudge logic would go in notification_manager (not tested here)."""
    # This test verifies that scrape failure doesn't crash the tick
    scanner = QuotaScanner(
        tmp_path,
        {
            "quota": {
                "scrape_enabled": True,
                "claude_cookie_path": str(tmp_path / "fake.cookies"),
            }
        },
        "full",
    )

    # Create fake cookie file so path check passes
    (tmp_path / "fake.cookies").write_text("fake")

    # Mock scraper to raise
    async def mock_scraper(path):
        raise RuntimeError("Scrape failed")

    # Mock the import path that _tick() uses
    with patch("quota_scrapers.scrape_claude", mock_scraper):
        # Tick should not raise
        await scanner._tick()
