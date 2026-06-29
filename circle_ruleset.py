"""
circle_ruleset.py — Circle ruleset parser and rule-match predicates.

Pure Python module: no LLM calls, no async, no filesystem IO (just YAML parsing).
"""
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("circle-ruleset")

KNOWN_PREDICATES = {
    "type", "tags_contains_any", "tags_contains_all",
    "classification", "category", "hostname",
    "source_title_contains", "frontmatter",
}


@dataclass
class CircleRuleset:
    """Parsed circle ruleset from a YAML file."""
    slug: str                        # matches filename stem
    display_name: str
    members: list[dict]              # [{telegram_user_id: int, name: str}, ...]
    bot_token: str                   # may be empty string
    icloud_folder: str               # relative to icloud_root
    include_rules: list[dict]        # list of rule dicts
    exclude_rules: list[dict]        # list of rule dicts


def load_ruleset(path: Path) -> CircleRuleset:
    """
    Load and parse a circle ruleset YAML file.

    Raises:
        ValueError: if YAML parse error or missing 'circle' field
    """
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"circle ruleset syntax error: {e}")
    except OSError as e:
        raise ValueError(f"circle ruleset read error: {e}")

    if not isinstance(data, dict):
        raise ValueError("circle ruleset must be a YAML mapping")

    if "circle" not in data:
        raise ValueError("missing 'circle' field")

    slug = data["circle"]
    if slug != path.stem:
        log.warning(
            "Circle slug '%s' does not match filename stem '%s'",
            slug, path.stem
        )

    display_name = data.get("display_name", slug)
    members = data.get("members", [])
    bot_token = data.get("bot_token", "")
    icloud_folder = data.get("icloud_folder", "")

    rules = data.get("rules", {})
    include_rules = rules.get("include", [])
    exclude_rules = rules.get("exclude", [])

    # Warn about unknown predicates
    for rule in include_rules + exclude_rules:
        if not isinstance(rule, dict):
            continue
        for key in rule.keys():
            if key not in KNOWN_PREDICATES:
                log.warning(
                    "Circle '%s': unknown predicate '%s' in rule — ignoring",
                    slug, key
                )

    return CircleRuleset(
        slug=slug,
        display_name=display_name,
        members=members,
        bot_token=bot_token,
        icloud_folder=icloud_folder,
        include_rules=include_rules,
        exclude_rules=exclude_rules,
    )


def _rule_matches(rule: dict, fm: dict) -> bool:
    """
    Evaluate one rule dict against a frontmatter dict.

    All predicates in the rule are AND-ed — returns True only if EVERY
    predicate matches.
    """
    if not isinstance(rule, dict):
        return False

    for key, value in rule.items():
        if key == "type":
            if fm.get("type") != value:
                return False

        elif key == "tags_contains_any":
            tags = set(fm.get("tags", []))
            if not (set(value) & tags):
                return False

        elif key == "tags_contains_all":
            tags = set(fm.get("tags", []))
            if not (set(value) <= tags):
                return False

        elif key == "classification":
            if fm.get("classification") != value:
                return False

        elif key == "category":
            if fm.get("category") != value:
                return False

        elif key == "hostname":
            if fm.get("hostname") != value:
                return False

        elif key == "source_title_contains":
            source_title = str(fm.get("source_title", "")).lower()
            if value.lower() not in source_title:
                return False

        elif key == "frontmatter":
            # Nested dict: all k,v pairs must match
            if not isinstance(value, dict):
                return False
            for k, v in value.items():
                if fm.get(k) != v:
                    return False

        # Unknown keys are already warned in load_ruleset — skip silently

    return True


def matches_include(ruleset: CircleRuleset, fm: dict) -> bool:
    """
    Returns True if frontmatter matches at least one include rule.
    Empty include list → False.
    """
    if not ruleset.include_rules:
        return False
    return any(_rule_matches(rule, fm) for rule in ruleset.include_rules)


def matches_exclude(ruleset: CircleRuleset, fm: dict) -> bool:
    """
    Returns True if frontmatter matches at least one exclude rule.
    Empty exclude list → False.
    """
    if not ruleset.exclude_rules:
        return False
    return any(_rule_matches(rule, fm) for rule in ruleset.exclude_rules)


def should_sync(ruleset: CircleRuleset, fm: dict) -> bool:
    """
    Returns True if the frontmatter should be synced to this circle.

    Logic: matches at least one include rule AND does not match any exclude rule.
    """
    return matches_include(ruleset, fm) and not matches_exclude(ruleset, fm)


# ── Rule editing helpers ──────────────────────────────────────────────────────

# Maps short command-line token keys to canonical YAML rule keys.
_PREDICATE_ALIASES = {
    "type":           "type",
    "tags":           "tags_contains_any",
    "tags_any":       "tags_contains_any",
    "tags_all":       "tags_contains_all",
    "category":       "category",
    "classification": "classification",
    "hostname":       "hostname",
    "source_title":   "source_title_contains",
}

# Predicates that accept comma-separated lists.
_LIST_PREDICATES = {"tags_contains_any", "tags_contains_all"}


def parse_rule_predicates(tokens: list) -> dict:
    """
    Parse a list of ``key:value`` tokens into a rule dict suitable for YAML.

    Supported token forms:
    - ``type:calendar_event``
    - ``tags:family,home``  (→ tags_contains_any: [family, home])
    - ``tags_all:family,home``  (→ tags_contains_all: [family, home])
    - ``category:work``
    - ``classification:marketing``
    - ``hostname:macstudio``
    - ``source_title:Python docs``

    Unknown keys are ignored silently.  Tokens without a colon are skipped.
    Returns {} if no valid predicates are found.
    """
    rule: dict = {}
    for token in tokens:
        if ":" not in token:
            continue
        raw_key, _, val = token.partition(":")
        raw_key = raw_key.strip().lower()
        val = val.strip()
        if not val or raw_key not in _PREDICATE_ALIASES:
            continue
        canon = _PREDICATE_ALIASES[raw_key]
        if canon in _LIST_PREDICATES:
            rule[canon] = [v.strip() for v in val.split(",") if v.strip()]
        else:
            rule[canon] = val
    return rule


def write_ruleset_yaml(path: Path, data: dict) -> None:
    """Atomically overwrite *path* with the YAML serialisation of *data*."""
    tmp = path.with_suffix(".tmp")
    text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.write_text(text)
    os.rename(str(tmp), str(path))
