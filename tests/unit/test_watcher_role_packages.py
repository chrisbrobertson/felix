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


def _read_pins(path: Path) -> dict[str, str]:
    """Return {normalized-name: version} for `name==version` lines in a requirements file."""
    pins = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        assert sep, f"{path.name}: unpinned requirement {line!r}"
        pins[name.strip().lower().replace("_", "-")] = version.strip()
    return pins


def _parse_watcher_pip_list() -> set[str]:
    """The watcher-role package set: requirements-watcher.txt, which install.sh installs."""
    return set(_read_pins(REPO_ROOT / "requirements-watcher.txt"))


_watcher_third_party = third_party_imports_in(WATCHER_ENTRY_POINTS)
_watcher_pkgs = _parse_watcher_pip_list()


def test_watcher_closure_third_party_subset_of_watcher_packages():
    """Every third-party import reachable from watcher-role modules must be
    in requirements-watcher.txt (the watcher-role install list)."""
    leaked = sorted(
        name for name in _watcher_third_party
        if dist_for(name) not in _watcher_pkgs
    )
    assert not leaked, (
        f"These third-party packages are reachable from watcher-role modules "
        f"but are NOT in requirements-watcher.txt: {leaked}. "
        f"Options: (a) move the import inside the role-gated code path, "
        f"(b) add the package, pinned as in requirements.txt, to requirements-watcher.txt, "
        f"or (c) factor the offending code out of the watcher import path."
    )


def test_watcher_pins_match_requirements():
    """Every watcher pin must equal the requirements.txt pin, so a version bump
    (e.g. a security fix) cannot reach full-role nodes and silently skip watchers."""
    watcher = _read_pins(REPO_ROOT / "requirements-watcher.txt")
    full = _read_pins(REPO_ROOT / "requirements.txt")
    missing = sorted(set(watcher) - set(full))
    assert not missing, f"watcher packages not pinned in requirements.txt: {missing}"
    drift = {n: (watcher[n], full[n]) for n in watcher if watcher[n] != full[n]}
    assert not drift, f"requirements-watcher.txt pins differ from requirements.txt: {drift}"


def test_install_sh_watcher_installs_and_hashes_pinned_file():
    """install.sh's watcher branch must install requirements-watcher.txt and hash
    that file, so changing a pin changes the hash and triggers a reinstall."""
    text = (REPO_ROOT / "install.sh").read_text()
    block = text[text.index('if [ "$ROLE" = "watcher" ]; then'):text.index("# ── 8.")]
    assert 'shasum -a 256 "$REPO_DIR/requirements-watcher.txt"' in block
    assert 'install -q -r "$REPO_DIR/requirements-watcher.txt"' in block
    assert not re.search(r"install -q litellm", block), "watcher must not pip-install unpinned names"
