"""Verify that every local module reachable from daemon.py is listed in
install.sh's DAEMON_FILES array.

This catches the bug class where a new .py file is added to the repo and
imported by an existing deployed module, but the author forgets to add it
to the installer manifest — causing a ModuleNotFoundError crash loop at
deploy time.
"""
import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

# .py files at repo root that are intentionally not deployed.
# One-shot scripts, manual-run utilities, etc.
DO_NOT_DEPLOY = {
    "migrate_memories.py",
}


def _parse_daemon_files() -> set[str]:
    """Extract the DAEMON_FILES array from install.sh."""
    text = (REPO_ROOT / "install.sh").read_text()
    m = re.search(r"DAEMON_FILES=\(\s*(.*?)\s*\)", text, re.DOTALL)
    assert m, "Could not find DAEMON_FILES array in install.sh"
    return set(m.group(1).split())


def _local_imports(path: Path) -> set[str]:
    """Return local-module names directly imported by path (non-recursive)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return {n for n in names if (REPO_ROOT / f"{n}.py").exists()}


def _import_closure(entry: Path) -> set[str]:
    """Compute the transitive closure of local-module imports from entry."""
    visited: set[str] = set()
    frontier = {entry.stem}
    while frontier:
        name = frontier.pop()
        if name in visited:
            continue
        visited.add(name)
        py = REPO_ROOT / f"{name}.py"
        if py.exists():
            frontier |= _local_imports(py)
    # Return as filenames, not module names
    return {f"{n}.py" for n in visited}


# ── Shared state (computed once at module import) ──────────────────────────

_daemon_files = _parse_daemon_files()
_closure = _import_closure(REPO_ROOT / "daemon.py")


# ── Tests ───────────────────────────────────────────────────────────────────

def test_every_imported_module_is_in_daemon_files():
    """Every .py reachable from daemon.py must be in DAEMON_FILES."""
    missing = _closure - _daemon_files
    assert not missing, (
        f"These modules are imported by daemon.py (transitively) but are "
        f"missing from install.sh DAEMON_FILES: {sorted(missing)}. "
        f"Add them to the DAEMON_FILES array in install.sh, or the daemon "
        f"will crash with ModuleNotFoundError after the next ./install.sh."
    )


def test_no_orphan_files_in_daemon_files():
    """Every DAEMON_FILES entry (except VERSION) must be reachable from daemon.py."""
    extra = _daemon_files - _closure - {"VERSION"}
    assert not extra, (
        f"These entries are in install.sh DAEMON_FILES but are not reachable "
        f"via any import from daemon.py: {sorted(extra)}. "
        f"Either remove them from DAEMON_FILES or import them somewhere in "
        f"the daemon's transitive dependency graph."
    )


def test_every_repo_root_py_is_accounted_for():
    """Every .py at repo root must be either in DAEMON_FILES or DO_NOT_DEPLOY.

    Prevents silently shipping a new module that the author forgot to add
    to the manifest AND forgot to import from an existing deployed file.
    """
    all_root_py = {p.name for p in REPO_ROOT.glob("*.py")}
    # Exclude test files and scripts that live under subdirectories
    unknown = all_root_py - _daemon_files - DO_NOT_DEPLOY
    assert not unknown, (
        f"New .py files at repo root are neither in install.sh DAEMON_FILES "
        f"nor in DO_NOT_DEPLOY: {sorted(unknown)}. "
        f"If this is a new daemon module, add it to DAEMON_FILES in "
        f"install.sh. If it is a one-shot script, add it to DO_NOT_DEPLOY "
        f"in this test file ({__file__})."
    )
