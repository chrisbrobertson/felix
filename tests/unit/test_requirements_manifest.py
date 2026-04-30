"""Verify requirements.txt covers every third-party import used by the daemon."""
import re
from pathlib import Path
from _manifest_helpers import (
    REPO_ROOT,
    dist_for,
    third_party_imports_in,
)


def _parse_requirements() -> set[str]:
    """Return lowercased distribution names from requirements.txt."""
    text = (REPO_ROOT / "requirements.txt").read_text()
    pinned = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-r"):
            continue
        # Strip version specifier: pyyaml==6.0.2 -> pyyaml
        name = re.split(r"[>=<!]", line)[0].strip().lower().replace("_", "-")
        pinned.add(name)
    return pinned


# Compute once at import (fast, pure AST)
_third_party = third_party_imports_in(["daemon"])  # full closure from daemon.py entry
_pinned = _parse_requirements()


def test_every_third_party_import_in_requirements():
    """Each third-party import reachable from daemon.py must have a requirements.txt pin."""
    missing = sorted(
        name for name in _third_party
        if dist_for(name) not in _pinned
    )
    assert not missing, (
        f"These imports are used by the daemon but have no matching pin in "
        f"requirements.txt: {missing}. Add the distribution to requirements.txt "
        f"or add a mapping to IMPORT_TO_DISTRIBUTION in _manifest_helpers.py."
    )


def test_no_unused_requirements():
    """Every requirements.txt pin must map to at least one import (or be in ALLOWLIST)."""
    # Some packages may be runtime-only deps that never appear in import statements
    # (e.g., a litellm backend provider that's only referenced by string config).
    UNIMPORTED_BUT_NEEDED: set[str] = {
        "lxml",      # Used by BeautifulSoup as parser: BeautifulSoup(html, "lxml")
        "watchdog",  # Used by litellm file watcher (if enabled in config)
    }

    used = {dist_for(n) for n in _third_party}
    extra = sorted(_pinned - used - UNIMPORTED_BUT_NEEDED)
    assert not extra, (
        f"requirements.txt entries not imported anywhere in the daemon: {extra}. "
        f"Either remove the pin from requirements.txt or add to "
        f"UNIMPORTED_BUT_NEEDED in this test if it's a runtime-only dep."
    )
