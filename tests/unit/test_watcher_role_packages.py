"""Verify watcher-role import closure stays within watcher-installed packages."""
import re
from pathlib import Path
from _manifest_helpers import REPO_ROOT, dist_for, third_party_imports_in

WATCHER_ENTRY_POINTS = [
    "browser_watcher",
    "code_scanner",
    "email_scanner",
    "calendar_scanner",
    "slack_scanner",
    "memory_cache",
]


def _parse_watcher_pip_list() -> set[str]:
    """Extract the watcher-role pip install list from install.sh."""
    text = (REPO_ROOT / "install.sh").read_text()
    # Matches the inline: pip install -q litellm httpx beautifulsoup4 ...
    # inside the if [ "$ROLE" = "watcher" ] block (line ~513)
    m = re.search(r'\$VENV/bin/pip.*install -q (litellm\s+httpx[^\n]+)', text)
    assert m, "Could not find watcher pip install list in install.sh"
    packages = m.group(1).strip().split()
    return {p.lower().replace("_", "-") for p in packages}


_watcher_third_party = third_party_imports_in(WATCHER_ENTRY_POINTS)
_watcher_pkgs = _parse_watcher_pip_list()


def test_watcher_closure_third_party_subset_of_watcher_packages():
    """Every third-party import reachable from watcher-role modules must be
    in the watcher pip install list in install.sh."""
    leaked = sorted(
        name for name in _watcher_third_party
        if dist_for(name) not in _watcher_pkgs
    )
    assert not leaked, (
        f"These third-party packages are reachable from watcher-role modules "
        f"but are NOT in install.sh's watcher pip list (line ~513): {leaked}. "
        f"Options: (a) move the import inside the role-gated code path, "
        f"(b) add the package to the watcher pip install list in install.sh, "
        f"or (c) factor the offending code out of the watcher import path."
    )
