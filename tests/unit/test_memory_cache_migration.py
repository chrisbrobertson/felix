"""Static invariant: Wave-2 migrated modules must not read MEMORIES_DIR directly.

Spec invariant (`specs/feat-memory-cache.md:144`):
    No consumer reads MEMORIES_DIR directly except via cache.get() / cache.query_*().
    The watcher role is the exception.

This test walks the AST of every migrated module and asserts that no glob() or
read_text-style call is anchored on MEMORIES_DIR / (BRAIN_DIR / "memories") /
self.memories_dir / similar. Regressions land as test failures rather than as
quietly resurrected EDEADLK retry storms on the live 3,571-file corpus.

Write-side scanners (`email_scanner`, `slack_scanner`, etc.) are intentionally
out of scope — they read their own write namespace and the watcher role is the
spec-sanctioned exception.
"""
import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Modules that have been migrated to MemoryCache. The invariant applies to these.
MIGRATED_MODULES = [
    "chat_handler.py",
    "commitment_tracker.py",
    "contact_tracker.py",
    "index_builder.py",
    "notification_manager.py",
    "report_scheduler.py",
    "synthesis_scanner.py",
    "circle_sync_scanner.py",
    "goal_project_agent.py",
    "project_inference_scanner.py",
]

# Attribute / name tokens that resolve to MEMORIES_DIR at runtime.
MEMORIES_DIR_NAMES = {"MEMORIES_DIR", "memories_dir", "_memories_dir"}

# Methods that open file *contents* anchored on MEMORIES_DIR are flagged.
# `iterdir` is intentionally not included — it lists filenames only, doesn't
# read content, so it doesn't incur EDEADLK retry pressure. The one legitimate
# remaining iterdir() is a one-shot __init__-time filename migration in
# project_inference_scanner.py.
DISK_READ_METHODS = {
    "glob",
    "rglob",
    "read_text",
    "read_text_with_retry",
    "read_text_with_retry_async",
    "open",
}


def _is_memories_dir_expr(node: ast.AST) -> bool:
    """Return True if the AST node evaluates to MEMORIES_DIR at runtime."""
    # Bare name: MEMORIES_DIR
    if isinstance(node, ast.Name) and node.id in MEMORIES_DIR_NAMES:
        return True
    # Attribute access: self.memories_dir / self._memories_dir
    if isinstance(node, ast.Attribute) and node.attr in MEMORIES_DIR_NAMES:
        return True
    # BinOp: BRAIN_DIR / "memories"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = node.left
        right = node.right
        if (
            isinstance(left, ast.Name)
            and left.id == "BRAIN_DIR"
            and isinstance(right, ast.Constant)
            and right.value == "memories"
        ):
            return True
    return False


def _find_disk_reads(tree: ast.AST) -> list[tuple[int, str]]:
    """Return [(line_no, snippet)] for every disk-read anchored on MEMORIES_DIR."""
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in DISK_READ_METHODS:
            continue
        if _is_memories_dir_expr(func.value):
            snippet = ast.unparse(node)
            violations.append((node.lineno, snippet))
    return violations


@pytest.mark.parametrize("module_filename", MIGRATED_MODULES)
def test_migrated_module_has_no_direct_memories_dir_reads(module_filename):
    """Every migrated module must read memories through MemoryCache."""
    path = REPO_ROOT / module_filename
    assert path.exists(), f"missing module {path}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = _find_disk_reads(tree)
    if violations:
        msg = f"{module_filename} has direct MEMORIES_DIR reads:\n"
        for lineno, snippet in violations:
            msg += f"  line {lineno}: {snippet}\n"
        msg += (
            "These bypass MemoryCache and re-introduce the EDEADLK retry surface "
            "the spec eliminated. Use self._cache.query_*() / self._cache.get() instead."
        )
        pytest.fail(msg)
