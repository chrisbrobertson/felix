"""Static invariant: Wave-2 migrated modules must not read MEMORIES_DIR directly.

Spec invariant (`specs/feat-memory-cache.md:144`):
    No consumer reads MEMORIES_DIR directly except via cache.get() / cache.query_*().
    The watcher role is the exception.

This test walks the AST of every migrated module and asserts that no glob() or
read_text-style call is anchored on:
  - MEMORIES_DIR / self.memories_dir (bare name)
  - BRAIN_DIR / "memories" (computed alias)
  - MEMORIES_DIR / <any expr> (inline path construction, e.g. (MEMORIES_DIR / filename).read_text())

The third form was added in #150 — it catches patterns like:
  path = MEMORIES_DIR / row["filename"]  # stored in variable
  path.read_text()                       # NOT caught — taint analysis needed
  (MEMORIES_DIR / row["filename"]).read_text()  # NOW caught

Write-side scanners (``email_scanner``, ``slack_scanner``, etc.) are intentionally
out of scope — they read their own write namespace and the watcher role is the
spec-sanctioned exception.

What is still NOT caught:
- A migrated module stores ``path = MEMORIES_DIR / name`` in a variable and then
  calls ``path.read_text()`` on a later line — taint analysis would be needed to
  close this gap.
- Read-modify-write helpers (e.g. ``_rewrite_feature_frontmatter``) that call
  ``read_text_with_retry(path)`` where ``path`` is a MEMORIES_DIR file. These are
  intentionally exempt because they need to read before an atomic write-back; the
  invariant only applies to pure read paths.
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
    # BinOp — either BRAIN_DIR / "memories" or MEMORIES_DIR / something
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = node.left
        right = node.right
        # BRAIN_DIR / "memories"
        if (
            isinstance(left, ast.Name)
            and left.id == "BRAIN_DIR"
            and isinstance(right, ast.Constant)
            and right.value == "memories"
        ):
            return True
        # MEMORIES_DIR / <anything>  — catches (MEMORIES_DIR / filename).read_text()
        if _is_memories_dir_expr(left):
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


# ── AST helper unit tests ─────────────────────────────────────────────────────

def test_is_memories_dir_expr_catches_bare_name():
    node = ast.parse("MEMORIES_DIR.glob('*.md')", mode="eval").body.func.value
    assert _is_memories_dir_expr(node)


def test_is_memories_dir_expr_catches_self_attr():
    node = ast.parse("self.memories_dir.read_text()", mode="eval").body.func.value
    assert _is_memories_dir_expr(node)


def test_is_memories_dir_expr_catches_brain_dir_slash_memories():
    node = ast.parse("(BRAIN_DIR / 'memories').glob('*.md')", mode="eval").body.func.value
    assert _is_memories_dir_expr(node)


def test_is_memories_dir_expr_catches_memories_dir_slash_something():
    """#150 — inline path construction MEMORIES_DIR / expr should be flagged."""
    node = ast.parse("(MEMORIES_DIR / filename).read_text()", mode="eval").body.func.value
    assert _is_memories_dir_expr(node)


def test_is_memories_dir_expr_does_not_flag_other_paths():
    node = ast.parse("STATE_FILE.read_text()", mode="eval").body.func.value
    assert not _is_memories_dir_expr(node)


def test_find_disk_reads_catches_glob_on_memories_dir():
    src = "MEMORIES_DIR.glob('*.md')"
    tree = ast.parse(src)
    assert len(_find_disk_reads(tree)) == 1


def test_find_disk_reads_catches_inline_path_construction():
    """#150 — (MEMORIES_DIR / name).read_text() must be detected."""
    src = "(MEMORIES_DIR / row['filename']).read_text(encoding='utf-8')"
    tree = ast.parse(src)
    assert len(_find_disk_reads(tree)) == 1


def test_find_disk_reads_does_not_flag_state_file_reads():
    src = "STATE_FILE.read_text()"
    tree = ast.parse(src)
    assert _find_disk_reads(tree) == []


