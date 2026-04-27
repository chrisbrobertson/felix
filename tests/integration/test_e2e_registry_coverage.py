"""Fail if a COMMAND_REGISTRY entry lacks a test_<cmd>_smoke."""
import re
from pathlib import Path
from command_core import COMMAND_REGISTRY

ALIASES = {
    "feature_new":    "feature",
    "fdetail":        "feature_detail",
    "people":         "contacts",
    "messages":       "comms",
    "communications": "comms",
    "message":        "comm",
    "communication":  "comm",
    "commands":       "help",
    "bugs":           "features",
}


def _collected_smoke_names() -> set[str]:
    """Scan all test_e2e_*.py files for test_<cmd>_smoke function definitions."""
    pattern = re.compile(r"^async def test_([a-z0-9_]+)_smoke\b", re.MULTILINE)
    names = set()
    for f in Path(__file__).parent.glob("test_e2e_*.py"):
        if f.name == "test_e2e_registry_coverage.py":
            continue
        names.update(pattern.findall(f.read_text()))
    return names


def test_every_registry_command_has_smoke_test():
    """Every command in COMMAND_REGISTRY must have a smoke test or be aliased."""
    expected = {cmd for section in COMMAND_REGISTRY.values() for cmd, _ in section}
    smoke = _collected_smoke_names()
    missing = {c for c in expected if c not in smoke and c not in ALIASES}
    assert not missing, (
        f"Slash commands missing smoke test: {sorted(missing)}. "
        f"Add test_<cmd>_smoke in tests/integration/test_e2e_*.py "
        f"or add to ALIASES if it shares a handler."
    )


def test_aliases_point_at_real_smoke_tests():
    """Every alias target must have a smoke test."""
    smoke = _collected_smoke_names()
    bad = {a: t for a, t in ALIASES.items() if t not in smoke}
    assert not bad, f"ALIASES targets without smoke test: {bad}"
