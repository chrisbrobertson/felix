"""Shared helpers for deployment-manifest tests.

Used by test_install_manifest.py, test_requirements_manifest.py, and
test_watcher_role_packages.py to verify that the DAEMON_FILES array in
install.sh, the requirements.txt pin list, and the watcher pip install
list all stay consistent with the actual import closure from daemon.py.
"""
import ast
import importlib.util
import re
import sys
import sysconfig
from pathlib import Path
from typing import Set

REPO_ROOT = Path(__file__).parent.parent.parent

# Import names that differ from their PyPI distribution name.
# Keys are what appears in import statements; values are the distribution
# name as written in requirements.txt (before version specifiers).
IMPORT_TO_DISTRIBUTION: dict[str, str] = {
    "yaml":       "pyyaml",
    "bs4":        "beautifulsoup4",
    "telegram":   "python-telegram-bot",
    "EventKit":   "pyobjc-framework-EventKit",
    "pdfminer":   "pdfminer.six",
    "slack_bolt": "slack_bolt",
    # Foundation is provided by pyobjc-framework-Cocoa, which is a transitive
    # dependency of pyobjc-framework-EventKit (not directly in requirements.txt)
    "Foundation": "pyobjc-framework-EventKit",
}


def _local_imports(path: Path) -> Set[str]:
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


def _import_closure(entry: Path) -> Set[str]:
    """Transitive closure of local-module imports from entry point, as .py filenames."""
    visited: Set[str] = set()
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


def _is_stdlib(name: str) -> bool:
    """Return True if name is a stdlib module on the current Python."""
    # Python 3.10+ has sys.stdlib_module_names
    if hasattr(sys, "stdlib_module_names"):
        return name in sys.stdlib_module_names

    # Fallback: check if find_spec resolves to stdlib path or is a builtin
    try:
        spec = importlib.util.find_spec(name)
        if spec is None:
            return False
        origin = getattr(spec, "origin", None)
        if origin is None:
            return False
        # Builtins have origin="built-in"
        if origin == "built-in":
            return True
        # Standard library modules live in the stdlib path
        stdlib_path = sysconfig.get_paths()["stdlib"]
        return origin.startswith(stdlib_path)
    except (ImportError, ValueError, AttributeError):
        return False


def dist_for(import_name: str) -> str:
    """Map an import-name to its PyPI distribution name (lowercase, - not _)."""
    mapped = IMPORT_TO_DISTRIBUTION.get(import_name, import_name)
    return mapped.lower().replace("_", "-")


def third_party_imports_in(entry_modules: list[str]) -> Set[str]:
    """Return all third-party import names reachable from a list of module names.

    This computes the transitive closure of local modules from each entry point,
    then scans every module in that closure for non-local, non-stdlib imports.
    Returns import *names* (not distribution names).
    """
    # Compute union of local closures
    all_local_names: Set[str] = set()
    for mod in entry_modules:
        py = REPO_ROOT / f"{mod}.py"
        if py.exists():
            closure_filenames = _import_closure(py)
            all_local_names |= {f[:-3] for f in closure_filenames}

    # Now collect ALL imports (including third-party) from those modules
    third_party: Set[str] = set()
    for name in all_local_names:
        py = REPO_ROOT / f"{name}.py"
        if not py.exists():
            continue
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if not (REPO_ROOT / f"{top}.py").exists() and not _is_stdlib(top):
                        third_party.add(top)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    top = node.module.split(".")[0]
                    if not (REPO_ROOT / f"{top}.py").exists() and not _is_stdlib(top):
                        third_party.add(top)
    return third_party


def parse_daemon_files() -> Set[str]:
    """Extract the DAEMON_FILES array from install.sh."""
    text = (REPO_ROOT / "install.sh").read_text()
    m = re.search(r"DAEMON_FILES=\(\s*(.*?)\s*\)", text, re.DOTALL)
    assert m, "Could not find DAEMON_FILES array in install.sh"
    return set(m.group(1).split())
