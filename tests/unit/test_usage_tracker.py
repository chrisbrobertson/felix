"""Unit tests for usage_tracker.py."""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import usage_tracker as ut
from usage_tracker import record_usage, render_usage, render_daily_breakdown


# ── Helpers ───────────────────────────────────────────────────────────────────

def _state(tmp_path: Path) -> dict:
    sf = tmp_path / "usage-tracker-state.json"
    return json.loads(sf.read_text()) if sf.exists() else {}


# ── record_usage ──────────────────────────────────────────────────────────────

def test_record_usage_creates_state_file(tmp_path):
    sf = tmp_path / "usage-tracker-state.json"
    record_usage("claude-haiku", 100, 50, state_file=sf)
    assert sf.exists()


def test_record_usage_accumulates_tokens(tmp_path):
    sf = tmp_path / "usage-tracker-state.json"
    record_usage("claude-haiku", 100, 50, state_file=sf)
    record_usage("claude-haiku", 200, 80, state_file=sf)
    state = _state(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    entry = state[today]["claude-haiku"]
    assert entry["prompt_tokens"] == 300
    assert entry["completion_tokens"] == 130
    assert entry["calls"] == 2


def test_record_usage_multiple_models(tmp_path):
    sf = tmp_path / "usage-tracker-state.json"
    record_usage("model-a", 100, 20, state_file=sf)
    record_usage("model-b", 50, 10, state_file=sf)
    state = _state(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    assert "model-a" in state[today]
    assert "model-b" in state[today]


def test_record_usage_skips_zero_tokens(tmp_path):
    sf = tmp_path / "usage-tracker-state.json"
    record_usage("model-a", 0, 0, state_file=sf)
    assert not sf.exists()


def test_record_usage_noop_on_exception(tmp_path, monkeypatch):
    """record_usage must not raise even if the state file is unwriteable."""
    sf = tmp_path / "usage-tracker-state.json"

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ut, "_save_state", boom)
    # Should not raise
    record_usage("model-a", 10, 5, state_file=sf)


def test_record_usage_atomic_write(tmp_path):
    """State file is written atomically — no .tmp file left behind."""
    sf = tmp_path / "usage-tracker-state.json"
    record_usage("model-a", 10, 5, state_file=sf)
    tmp_file = sf.with_suffix(".tmp")
    assert not tmp_file.exists()


# ── render_usage ──────────────────────────────────────────────────────────────

def test_render_usage_no_data(tmp_path):
    sf = tmp_path / "empty.json"
    result = render_usage(days=7, state_file=sf)
    assert "No usage data" in result


def test_render_usage_shows_totals(tmp_path):
    sf = tmp_path / "usage-tracker-state.json"
    record_usage("claude-haiku", 1000, 200, state_file=sf)
    result = render_usage(days=7, state_file=sf)
    assert "claude-haiku" in result
    assert "1,000" in result
    assert "200" in result


def test_render_usage_respects_days_window(tmp_path, monkeypatch):
    """Entries older than 'days' are excluded."""
    sf = tmp_path / "usage-tracker-state.json"
    old_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    state = {old_date: {"old-model": {"prompt_tokens": 9999, "completion_tokens": 1, "calls": 1}}}
    sf.write_text(json.dumps(state))
    result = render_usage(days=7, state_file=sf)
    assert "old-model" not in result


def test_render_usage_includes_recent_data(tmp_path):
    sf = tmp_path / "usage-tracker-state.json"
    recent = datetime.now().strftime("%Y-%m-%d")
    state = {recent: {"new-model": {"prompt_tokens": 500, "completion_tokens": 100, "calls": 3}}}
    sf.write_text(json.dumps(state))
    result = render_usage(days=7, state_file=sf)
    assert "new-model" in result
    assert "500" in result


def test_render_usage_shows_grand_total(tmp_path):
    sf = tmp_path / "usage-tracker-state.json"
    record_usage("model-a", 1000, 200, state_file=sf)
    record_usage("model-b", 500, 100, state_file=sf)
    result = render_usage(days=7, state_file=sf)
    assert "Total:" in result
    assert "1,800" in result  # 1000+200+500+100


# ── render_daily_breakdown ────────────────────────────────────────────────────

def test_render_daily_breakdown_no_data(tmp_path):
    sf = tmp_path / "empty.json"
    result = render_daily_breakdown(state_file=sf)
    assert "No daily usage data" in result


def test_render_daily_breakdown_shows_dates(tmp_path):
    sf = tmp_path / "usage-tracker-state.json"
    today = datetime.now().strftime("%Y-%m-%d")
    state = {today: {"model-a": {"prompt_tokens": 300, "completion_tokens": 100, "calls": 2}}}
    sf.write_text(json.dumps(state))
    result = render_daily_breakdown(state_file=sf)
    assert today in result
    assert "400" in result  # 300 + 100


# ── pruning ────────────────────────────────────────────────────────────────────

def test_old_entries_pruned_on_record(tmp_path):
    """Entries beyond RETENTION_DAYS are removed when a new record is written."""
    sf = tmp_path / "usage-tracker-state.json"
    old_date = (datetime.now() - timedelta(days=35)).strftime("%Y-%m-%d")
    state = {old_date: {"old-model": {"prompt_tokens": 1, "completion_tokens": 1, "calls": 1}}}
    sf.write_text(json.dumps(state))
    record_usage("new-model", 10, 5, state_file=sf)
    final = json.loads(sf.read_text())
    assert old_date not in final
