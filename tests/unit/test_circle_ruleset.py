"""Unit tests for circle_ruleset.py."""
import logging
import pytest
import yaml
from pathlib import Path

from circle_ruleset import (
    CircleRuleset, load_ruleset, matches_include, matches_exclude, should_sync
)


def _write_ruleset(tmp_path, stem, data):
    """Helper to write a ruleset YAML file."""
    p = tmp_path / f"{stem}.yaml"
    p.write_text(yaml.dump(data))
    return p


VALID_RULESET_DATA = {
    "circle": "family",
    "display_name": "Robertson Family",
    "members": [{"telegram_user_id": 123, "name": "Alex"}],
    "bot_token": "7654321:AAxxxx",
    "icloud_folder": "second-brain-circles/family/memories",
    "rules": {
        "include": [
            {"type": "calendar_event", "tags_contains_any": ["family", "home"]},
            {"type": "goal", "category": "family"},
        ],
        "exclude": [
            {"tags_contains_any": ["work", "confidential"]},
            {"classification": "marketing"},
        ],
    },
}


def test_load_ruleset_valid(tmp_path):
    """Test loading a valid ruleset file."""
    path = _write_ruleset(tmp_path, "family", VALID_RULESET_DATA)
    ruleset = load_ruleset(path)

    assert ruleset.slug == "family"
    assert ruleset.display_name == "Robertson Family"
    assert len(ruleset.members) == 1
    assert ruleset.members[0]["telegram_user_id"] == 123
    assert ruleset.bot_token == "7654321:AAxxxx"
    assert ruleset.icloud_folder == "second-brain-circles/family/memories"
    assert len(ruleset.include_rules) == 2
    assert len(ruleset.exclude_rules) == 2


def test_load_ruleset_missing_circle_field(tmp_path):
    """Test that missing 'circle' field raises ValueError."""
    data = {"display_name": "Test"}
    path = _write_ruleset(tmp_path, "test", data)

    with pytest.raises(ValueError, match="missing 'circle' field"):
        load_ruleset(path)


def test_load_ruleset_slug_mismatch(tmp_path, caplog):
    """Test that slug mismatch logs warning but doesn't raise."""
    data = {"circle": "foo"}
    path = _write_ruleset(tmp_path, "bar", data)

    with caplog.at_level(logging.WARNING):
        ruleset = load_ruleset(path)

    assert ruleset.slug == "foo"
    assert "does not match filename stem" in caplog.text


def test_load_ruleset_unknown_predicate(tmp_path, caplog):
    """Test that unknown predicate logs warning but doesn't crash."""
    data = {
        "circle": "test",
        "rules": {
            "include": [
                {"unknown_key": "value"}
            ]
        }
    }
    path = _write_ruleset(tmp_path, "test", data)

    with caplog.at_level(logging.WARNING):
        ruleset = load_ruleset(path)

    assert "unknown predicate 'unknown_key'" in caplog.text


def test_matches_include_type_only():
    """Test include rule with type predicate only."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[{"type": "calendar_event"}],
        exclude_rules=[],
    )

    fm_match = {"type": "calendar_event"}
    fm_no_match = {"type": "memory"}

    assert matches_include(ruleset, fm_match) is True
    assert matches_include(ruleset, fm_no_match) is False


def test_matches_include_tags_any():
    """Test include rule with tags_contains_any predicate."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[{"tags_contains_any": ["family"]}],
        exclude_rules=[],
    )

    fm_match = {"tags": ["family", "home"]}
    fm_no_match = {"tags": ["work"]}

    assert matches_include(ruleset, fm_match) is True
    assert matches_include(ruleset, fm_no_match) is False


def test_matches_include_tags_all():
    """Test include rule with tags_contains_all predicate."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[{"tags_contains_all": ["family", "home"]}],
        exclude_rules=[],
    )

    fm_match = {"tags": ["family", "home", "kids"]}
    fm_partial = {"tags": ["family"]}

    assert matches_include(ruleset, fm_match) is True
    assert matches_include(ruleset, fm_partial) is False


def test_matches_include_category():
    """Test include rule with category predicate."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[{"category": "family"}],
        exclude_rules=[],
    )

    fm_match = {"category": "family", "type": "goal"}
    fm_no_match = {"category": "work"}

    assert matches_include(ruleset, fm_match) is True
    assert matches_include(ruleset, fm_no_match) is False


def test_matches_exclude_overrides_include():
    """Test that exclude rules override include rules."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[{"type": "calendar_event"}],
        exclude_rules=[{"tags_contains_any": ["work"]}],
    )

    fm_both = {"type": "calendar_event", "tags": ["work", "meeting"]}

    assert matches_include(ruleset, fm_both) is True
    assert matches_exclude(ruleset, fm_both) is True
    assert should_sync(ruleset, fm_both) is False


def test_empty_include_matches_nothing():
    """Test that empty include rules matches nothing."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[],
        exclude_rules=[],
    )

    fm = {"type": "calendar_event"}

    assert matches_include(ruleset, fm) is False
    assert should_sync(ruleset, fm) is False


def test_empty_exclude_blocks_nothing():
    """Test that empty exclude rules blocks nothing."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[{"type": "calendar_event"}],
        exclude_rules=[],
    )

    fm = {"type": "calendar_event"}

    assert matches_include(ruleset, fm) is True
    assert matches_exclude(ruleset, fm) is False
    assert should_sync(ruleset, fm) is True


def test_frontmatter_predicate():
    """Test frontmatter predicate for nested key matching."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[{"frontmatter": {"hostname": "macstudio"}}],
        exclude_rules=[],
    )

    fm_match = {"hostname": "macstudio"}
    fm_no_match = {"hostname": "other"}

    assert matches_include(ruleset, fm_match) is True
    assert matches_include(ruleset, fm_no_match) is False


def test_source_title_contains():
    """Test source_title_contains predicate (case-insensitive)."""
    ruleset = CircleRuleset(
        slug="test",
        display_name="Test",
        members=[],
        bot_token="",
        icloud_folder="",
        include_rules=[{"source_title_contains": "dentist"}],
        exclude_rules=[],
    )

    fm_match = {"source_title": "Dentist Appointment 2026-04-20"}
    fm_no_match = {"source_title": "school play"}

    assert matches_include(ruleset, fm_match) is True
    assert matches_include(ruleset, fm_no_match) is False
