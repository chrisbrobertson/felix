"""Unit tests for scripts/merge_skill_update.py."""
import textwrap
from pathlib import Path

import pytest

from scripts.merge_skill_update import merge_skill, _parse_version, _split_at_exec_history


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_skill(version: int, instructions: str, history: str = "") -> str:
    fm = textwrap.dedent(f"""\
        ---
        name: test-skill
        version: {version}
        preferred_model: claude-haiku-4-5-20251001
        ---
        """)
    body = f"\n## Instructions\n\n{instructions}\n"
    if history:
        body += f"\n## Execution History\n\n{history}"
    return fm + body


# ── _parse_version ────────────────────────────────────────────────────────────

def test_parse_version_returns_int():
    content = _make_skill(3, "do something")
    assert _parse_version(content) == 3


def test_parse_version_missing_returns_zero():
    assert _parse_version("no frontmatter here") == 0


def test_parse_version_no_version_field():
    content = "---\nname: foo\n---\n\n## Instructions\n"
    assert _parse_version(content) == 0


# ── _split_at_exec_history ───────────────────────────────────────────────────

def test_split_with_history():
    content = "prompt text\n## Execution History\n\nrow1"
    prompt, history = _split_at_exec_history(content)
    assert prompt == "prompt text"
    assert history == "\n## Execution History\n\nrow1"


def test_split_without_history():
    content = "prompt text only"
    prompt, history = _split_at_exec_history(content)
    assert prompt == "prompt text only"
    assert history == ""


# ── merge_skill ───────────────────────────────────────────────────────────────

def test_copy_when_dest_missing(tmp_path):
    repo_skill = tmp_path / "repo" / "test.md"
    repo_skill.parent.mkdir()
    repo_skill.write_text(_make_skill(1, "v1 instructions"), encoding="utf-8")

    dest = tmp_path / "deployed" / "test.md"
    dest.parent.mkdir()

    result = merge_skill(repo_skill, dest)
    assert result == "copied"
    assert dest.read_text(encoding="utf-8") == repo_skill.read_text(encoding="utf-8")


def test_update_when_repo_version_newer(tmp_path):
    """Repo v2 > deployed v1: splice repo prompt, preserve deployed history."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir()
    deployed_dir.mkdir()

    history_rows = "| 2026-04-01 | page-abc | haiku | 0.9 | ok |\n"
    deployed_content = _make_skill(1, "v1 short instructions", history_rows)
    repo_content = _make_skill(2, "v2 richer instructions with more detail")

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(repo_content, encoding="utf-8")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "updated"

    merged = dest.read_text(encoding="utf-8")
    # Repo prompt section must be present
    assert "v2 richer instructions with more detail" in merged
    assert "v1 short instructions" not in merged
    # Deployed execution history must be preserved
    assert history_rows in merged


def test_skip_when_versions_equal(tmp_path):
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir()
    deployed_dir.mkdir()

    content = _make_skill(2, "same version instructions")
    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(content, encoding="utf-8")
    deployed_content = _make_skill(2, "locally customised instructions")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "skipped"
    # Deployed content must be unchanged
    assert dest.read_text(encoding="utf-8") == deployed_content


def test_skip_when_deployed_version_higher(tmp_path):
    """Optimizer may have bumped deployed version above repo; do not clobber."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir()
    deployed_dir.mkdir()

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(_make_skill(2, "repo v2"), encoding="utf-8")
    deployed_content = _make_skill(3, "optimizer improved v3")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "skipped"
    assert dest.read_text(encoding="utf-8") == deployed_content


def test_update_preserves_history_when_deployed_has_none(tmp_path):
    """Deployed file at v1 with no execution history: update copies repo content as-is."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir()
    deployed_dir.mkdir()

    repo_content = _make_skill(2, "v2 instructions")
    deployed_content = _make_skill(1, "v1 instructions")  # no execution history rows

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(repo_content, encoding="utf-8")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "updated"

    merged = dest.read_text(encoding="utf-8")
    assert "v2 instructions" in merged
    assert "v1 instructions" not in merged


def test_update_is_atomic(tmp_path, monkeypatch):
    """A write failure must not corrupt the deployed file."""
    import os as _os

    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir()
    deployed_dir.mkdir()

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    original = _make_skill(1, "original instructions")
    repo_skill.write_text(_make_skill(2, "new instructions"), encoding="utf-8")
    dest.write_text(original, encoding="utf-8")

    real_rename = _os.rename

    def failing_rename(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(_os, "rename", failing_rename)

    with pytest.raises(OSError):
        merge_skill(repo_skill, dest)

    # Deployed file must be unchanged after a failed write
    assert dest.read_text(encoding="utf-8") == original
