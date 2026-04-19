"""Unit tests for report_scheduler — parse_schedule, is_due, DigestGenerator, ReportScheduler."""
import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import report_scheduler as rs
from report_scheduler import (
    DigestGenerator,
    ReportScheduler,
    is_due,
    parse_schedule,
)


# ── parse_schedule ────────────────────────────────────────────────────────────

def test_parse_schedule_daily():
    result = parse_schedule("daily 08:00")
    assert result["days"] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert result["time"] == "08:00"


def test_parse_schedule_weekday():
    result = parse_schedule("weekday 09:30")
    assert result["days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert result["time"] == "09:30"


def test_parse_schedule_weekend():
    result = parse_schedule("weekend 10:00")
    assert result["days"] == ["sat", "sun"]
    assert result["time"] == "10:00"


def test_parse_schedule_single_day():
    result = parse_schedule("tue 07:00")
    assert result["days"] == ["tue"]
    assert result["time"] == "07:00"


def test_parse_schedule_comma_separated_days():
    result = parse_schedule("mon,wed,fri 18:00")
    assert result["days"] == ["mon", "wed", "fri"]
    assert result["time"] == "18:00"


def test_parse_schedule_invalid_format_raises():
    with pytest.raises(ValueError):
        parse_schedule("daily")  # missing time


def test_parse_schedule_invalid_time_format_raises():
    with pytest.raises(ValueError):
        parse_schedule("daily 8:00")  # not HH:MM


def test_parse_schedule_invalid_time_value_raises():
    with pytest.raises(ValueError):
        parse_schedule("daily 25:00")


def test_parse_schedule_invalid_day_raises():
    with pytest.raises(ValueError):
        parse_schedule("xyz 09:00")


def test_parse_schedule_invalid_csv_day_raises():
    with pytest.raises(ValueError):
        parse_schedule("mon,xyz 09:00")


# ── is_due ────────────────────────────────────────────────────────────────────

def _report(schedule="daily 09:00", paused=False):
    return {"schedule": schedule, "paused": paused}


def test_is_due_returns_true_when_all_conditions_met():
    # Tuesday 09:01, not sent today
    now = datetime(2026, 4, 7, 9, 1)  # Tuesday
    assert is_due(_report("daily 09:00"), last_sent_date=None, now=now) is True


def test_is_due_false_when_paused():
    now = datetime(2026, 4, 7, 9, 1)
    assert is_due(_report(paused=True), last_sent_date=None, now=now) is False


def test_is_due_false_when_wrong_weekday():
    # Report runs weekdays only; now is Saturday
    now = datetime(2026, 4, 11, 9, 1)  # Saturday
    assert is_due(_report("weekday 09:00"), last_sent_date=None, now=now) is False


def test_is_due_false_when_before_scheduled_time():
    now = datetime(2026, 4, 7, 8, 59)
    assert is_due(_report("daily 09:00"), last_sent_date=None, now=now) is False


def test_is_due_false_when_already_sent_today():
    now = datetime(2026, 4, 7, 10, 0)
    assert is_due(_report(), last_sent_date="2026-04-07", now=now) is False


def test_is_due_true_when_sent_yesterday():
    now = datetime(2026, 4, 7, 10, 0)
    assert is_due(_report(), last_sent_date="2026-04-06", now=now) is True


def test_is_due_false_on_invalid_schedule():
    now = datetime(2026, 4, 7, 10, 0)
    report = {"schedule": "bad-schedule", "paused": False}
    assert is_due(report, last_sent_date=None, now=now) is False


# ── DigestGenerator ───────────────────────────────────────────────────────────

def _make_memory(mem_type, title="Test Item", i=0):
    return {
        "path": Path(f"/tmp/mem{i}.md"),
        "fm": {"type": mem_type, "title": title, "status": "active", "due_date": "2026-05-01"},
        "body_snippet": "Some snippet.",
        "created": datetime(2026, 4, 1),
    }


def test_digest_generator_produces_header_with_title():
    gen = DigestGenerator()
    report = {"title": "Weekly Digest", "sources": ["commitments"]}
    memories = [_make_memory("commitment", "Buy milk", i=0)]

    text = gen.generate(report, memories)
    assert "Weekly Digest" in text


def test_digest_generator_includes_memory_items():
    gen = DigestGenerator()
    report = {"title": "Report", "sources": ["commitments"]}
    memories = [_make_memory("commitment", "Send email", i=0)]

    text = gen.generate(report, memories)
    assert "Send email" in text


def test_digest_generator_caps_at_10_items_per_source():
    gen = DigestGenerator()
    report = {"title": "Report", "sources": ["commitments"]}
    memories = [_make_memory("commitment", f"Item {i}", i=i) for i in range(15)]

    text = gen.generate(report, memories)
    assert "5 more" in text  # 15 - 10 = 5


def test_digest_generator_no_items_returns_no_items_message():
    gen = DigestGenerator()
    report = {"title": "Empty Report", "sources": ["commitments"]}
    text = gen.generate(report, memories=[])
    assert "No items to report" in text


def test_digest_generator_skips_source_with_no_matching_memories():
    gen = DigestGenerator()
    report = {"title": "Report", "sources": ["commitments", "calendar"]}
    memories = [_make_memory("commitment", "Do thing")]
    text = gen.generate(report, memories)
    # Only commitments section should appear; no calendar section header
    assert "Commitments" in text
    assert "Calendar" not in text


# ── ReportScheduler — state and runtime reports ───────────────────────────────

@pytest.fixture
def scheduler(tmp_path):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    config = {"reports": {}}
    return ReportScheduler(config=config, bot=bot, chat_id_getter=lambda: 1, deploy_dir=tmp_path)


def test_add_runtime_report_stores_report(scheduler):
    defn = {"schedule": "daily 08:00", "type": "digest", "sources": ["commitments"], "title": "T"}
    report_id = scheduler.add_runtime_report(defn)
    assert report_id.startswith("r-")
    reports = scheduler.get_all_reports()
    assert any(r["name"] == report_id for r in reports)


def test_add_runtime_report_invalid_schedule_raises(scheduler):
    defn = {"schedule": "bad", "type": "digest", "sources": ["commitments"]}
    with pytest.raises(ValueError, match="Invalid schedule"):
        scheduler.add_runtime_report(defn)


def test_add_runtime_report_invalid_type_raises(scheduler):
    defn = {"schedule": "daily 08:00", "type": "unknown", "sources": ["commitments"]}
    with pytest.raises(ValueError, match="Invalid type"):
        scheduler.add_runtime_report(defn)


def test_add_runtime_report_invalid_source_raises(scheduler):
    defn = {"schedule": "daily 08:00", "type": "digest", "sources": ["badSource"]}
    with pytest.raises(ValueError, match="Invalid source"):
        scheduler.add_runtime_report(defn)


def test_remove_runtime_report_returns_true_and_removes(scheduler):
    defn = {"schedule": "daily 08:00", "type": "digest", "sources": ["commitments"], "title": "T"}
    rid = scheduler.add_runtime_report(defn)
    result = scheduler.remove_runtime_report(rid)
    assert result is True
    reports = scheduler.get_all_reports()
    assert not any(r["name"] == rid for r in reports)


def test_remove_runtime_report_returns_false_when_not_found(scheduler):
    result = scheduler.remove_runtime_report("r-nonexistent")
    assert result is False


def test_get_all_reports_includes_config_reports(tmp_path):
    bot = MagicMock()
    config = {
        "reports": {
            "morning": {"schedule": "daily 07:00", "type": "digest", "sources": ["calendar"]},
        }
    }
    scheduler = ReportScheduler(config=config, bot=bot, chat_id_getter=lambda: 1, deploy_dir=tmp_path)
    reports = scheduler.get_all_reports()
    names = [r["name"] for r in reports]
    assert "morning" in names


def test_set_paused_config_report(tmp_path):
    bot = MagicMock()
    config = {"reports": {"morning": {"schedule": "daily 07:00", "type": "digest", "sources": ["calendar"]}}}
    scheduler = ReportScheduler(config=config, bot=bot, chat_id_getter=lambda: 1, deploy_dir=tmp_path)
    result = scheduler.set_paused("morning", paused=True, is_runtime=False)
    assert result is True
    reports = scheduler.get_all_reports()
    morning = next(r for r in reports if r["name"] == "morning")
    assert morning["paused"] is True


def test_set_paused_runtime_report(scheduler):
    defn = {"schedule": "daily 09:00", "type": "digest", "sources": ["meetings"], "title": "T"}
    rid = scheduler.add_runtime_report(defn)
    scheduler.set_paused(rid, paused=True, is_runtime=True)
    reports = scheduler.get_all_reports()
    r = next(x for x in reports if x["name"] == rid)
    assert r["paused"] is True


def test_set_paused_returns_false_for_missing_runtime_report(scheduler):
    result = scheduler.set_paused("r-missing", paused=True, is_runtime=True)
    assert result is False


# ── ReportScheduler — _chunk ──────────────────────────────────────────────────

def test_chunk_short_text_returns_single_chunk(scheduler):
    assert scheduler._chunk("hello", 4000) == ["hello"]


def test_chunk_splits_at_newline(scheduler):
    text = ("a" * 3999) + "\n" + "b" * 10
    chunks = scheduler._chunk(text, 4000)
    assert len(chunks) == 2
    assert chunks[0].endswith("a" * (3999 - len("a" * 3999) % 3999 if False else 3999))


def test_chunk_hard_cuts_when_no_whitespace(scheduler):
    text = "x" * 5000
    chunks = scheduler._chunk(text, 4000)
    assert len(chunks) == 2
    assert len(chunks[0]) <= 4000


# ── ReportScheduler — state persistence ──────────────────────────────────────

def test_state_file_is_created_atomically(scheduler, tmp_path):
    state = {"last_sent": {"r-abc": "2026-04-07"}, "runtime_reports": []}
    scheduler._save_state(state)
    loaded = scheduler._load_state()
    assert loaded["last_sent"]["r-abc"] == "2026-04-07"


def test_load_state_returns_defaults_when_no_file(scheduler):
    state = scheduler._load_state()
    assert "runtime_reports" in state
    assert "last_sent" in state
    assert "paused_config_reports" in state
