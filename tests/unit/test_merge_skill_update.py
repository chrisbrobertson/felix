"""Unit tests for scripts/merge_skill_update.py."""
import textwrap
from pathlib import Path

import pytest

from scripts.merge_skill_update import (
    merge_skill,
    _parse_version,
    _split_at_optimizer_sections,
    _merge_frontmatter_stats,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_skill(
    version: int,
    instructions: str,
    history: str = "",
    *,
    total_runs: int = 0,
    success_rate: str = "null",
    last_optimized: str = "null",
    prev_version_avg_score: str = "null",
) -> str:
    fm = textwrap.dedent(f"""\
        ---
        name: test-skill
        version: {version}
        preferred_model: claude-haiku-4-5-20251001
        success_rate: {success_rate}
        total_runs: {total_runs}
        last_optimized: {last_optimized}
        prev_version_avg_score: {prev_version_avg_score}
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


# ── _split_at_optimizer_sections ─────────────────────────────────────────────

def test_split_with_exec_history():
    content = "prompt text\n## Execution History\n\nrow1"
    prompt, tail = _split_at_optimizer_sections(content)
    assert prompt == "prompt text"
    assert tail == "\n## Execution History\n\nrow1"


def test_split_without_optimizer_sections():
    content = "prompt text only"
    prompt, tail = _split_at_optimizer_sections(content)
    assert prompt == "prompt text only"
    assert tail == ""


def test_split_with_top_examples_before_history():
    content = "prompt\n## Top Examples\n\nexample1\n## Execution History\n\nrow1"
    prompt, tail = _split_at_optimizer_sections(content)
    assert prompt == "prompt"
    assert tail == "\n## Top Examples\n\nexample1\n## Execution History\n\nrow1"


def test_split_with_top_examples_only():
    content = "prompt\n## Top Examples\n\nexample1"
    prompt, tail = _split_at_optimizer_sections(content)
    assert prompt == "prompt"
    assert tail == "\n## Top Examples\n\nexample1"


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


def test_update_when_versions_equal(tmp_path):
    """Repo v2 == deployed v2: repo prompt wins (fixes optimizer-bumped-to-same-version bug)."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir()
    deployed_dir.mkdir()

    history_rows = "| 2026-04-01 | page-abc | haiku | 0.9 | ok |\n"
    deployed_content = _make_skill(2, "optimizer-customised prompt", history_rows)
    repo_content = _make_skill(2, "canonical repo v2 prompt")

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(repo_content, encoding="utf-8")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "updated"
    merged = dest.read_text(encoding="utf-8")
    assert "canonical repo v2 prompt" in merged
    assert "optimizer-customised prompt" not in merged
    # Deployed execution history must be preserved
    assert history_rows in merged


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


def test_update_preserves_optimizer_stats_on_version_upgrade(tmp_path):
    """Stats fields from deployed are carried into the merged file on a version upgrade."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir()
    deployed_dir.mkdir()

    deployed_content = _make_skill(
        1, "v1 instructions",
        total_runs=50, success_rate="0.85",
        last_optimized="2026-03-01", prev_version_avg_score="0.72",
    )
    repo_content = _make_skill(2, "v2 richer instructions")

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(repo_content, encoding="utf-8")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "updated"

    merged = dest.read_text(encoding="utf-8")
    assert "v2 richer instructions" in merged
    assert "total_runs: 50" in merged
    assert "success_rate: 0.85" in merged
    assert "last_optimized: 2026-03-01" in merged
    assert "prev_version_avg_score: 0.72" in merged


def test_update_preserves_optimizer_stats_on_equal_version(tmp_path):
    """Stats fields from deployed are carried through when repo and deployed share a version."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir()
    deployed_dir.mkdir()

    deployed_content = _make_skill(
        2, "optimizer-bumped v2 prompt",
        total_runs=30, success_rate="0.90",
    )
    repo_content = _make_skill(2, "canonical repo v2 prompt")

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(repo_content, encoding="utf-8")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "updated"

    merged = dest.read_text(encoding="utf-8")
    assert "total_runs: 30" in merged
    # YAML normalises 0.90 → 0.9 on round-trip
    assert "success_rate: 0.9" in merged


def test_update_preserves_utility_score_when_absent_from_repo(tmp_path):
    """utility_score/score_trend fields are inserted even if the repo file lacks them."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir(); deployed_dir.mkdir()

    # Repo file has no utility_score / score_trend keys
    repo_content = _make_skill(2, "v2 instructions")
    # Deployed file has optimizer-computed scores
    deployed_fm = textwrap.dedent("""\
        ---
        name: test-skill
        version: 1
        preferred_model: claude-haiku-4-5-20251001
        success_rate: 0.82
        total_runs: 20
        last_optimized: null
        prev_version_avg_score: null
        utility_score: 0.78
        utility_score_updated: 2026-04-20T08:00:00
        score_trend: stable
        ---
        """)
    deployed_content = deployed_fm + "\n## Instructions\n\nv1 instructions\n"

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(repo_content, encoding="utf-8")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "updated"
    merged = dest.read_text(encoding="utf-8")
    assert "utility_score: 0.78" in merged
    assert "score_trend: stable" in merged
    # YAML parses the ISO datetime and str() serialises it with a space separator
    assert "utility_score_updated: 2026-04-20" in merged
    assert "v2 instructions" in merged


def test_update_preserves_utility_score_when_present_in_repo(tmp_path):
    """utility_score is patched in-place when the repo file already declares the key."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir(); deployed_dir.mkdir()

    repo_fm = textwrap.dedent("""\
        ---
        name: test-skill
        version: 2
        preferred_model: claude-haiku-4-5-20251001
        success_rate: null
        total_runs: 0
        utility_score: null
        score_trend: null
        ---
        """)
    repo_content = repo_fm + "\n## Instructions\n\nv2 instructions\n"

    deployed_fm = textwrap.dedent("""\
        ---
        name: test-skill
        version: 1
        preferred_model: claude-haiku-4-5-20251001
        success_rate: 0.75
        total_runs: 15
        utility_score: 0.71
        score_trend: declining
        ---
        """)
    deployed_content = deployed_fm + "\n## Instructions\n\nv1 instructions\n"

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(repo_content, encoding="utf-8")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "updated"
    merged = dest.read_text(encoding="utf-8")
    assert "utility_score: 0.71" in merged
    assert "score_trend: declining" in merged
    assert "v2 instructions" in merged


def test_update_preserves_top_examples_section(tmp_path):
    """## Top Examples (optimizer-written) survives a prompt upgrade."""
    repo_dir = tmp_path / "repo"
    deployed_dir = tmp_path / "deployed"
    repo_dir.mkdir(); deployed_dir.mkdir()

    examples_block = "\n## Top Examples\n\nexample output 1\nexample output 2\n"
    history_rows = "| 2026-04-01 | page-abc | haiku | 0.9 | ok |\n"
    deployed_content = (
        _make_skill(1, "v1 instructions").rstrip("\n")
        + examples_block
        + "\n## Execution History\n\n" + history_rows
    )
    repo_content = _make_skill(2, "v2 richer instructions")

    repo_skill = repo_dir / "test.md"
    dest = deployed_dir / "test.md"
    repo_skill.write_text(repo_content, encoding="utf-8")
    dest.write_text(deployed_content, encoding="utf-8")

    result = merge_skill(repo_skill, dest)
    assert result == "updated"
    merged = dest.read_text(encoding="utf-8")
    assert "v2 richer instructions" in merged
    assert "example output 1" in merged
    assert "example output 2" in merged
    assert history_rows in merged
    # Top Examples must appear before Execution History
    assert merged.index("## Top Examples") < merged.index("## Execution History")


def test_merge_frontmatter_stats_no_op_when_no_frontmatter():
    """Returns repo_prompt unchanged when either file lacks frontmatter."""
    repo_prompt = "no frontmatter here"
    deployed = "---\nname: foo\nversion: 1\n---\nbody"
    assert _merge_frontmatter_stats(repo_prompt, deployed) == repo_prompt

    deployed_no_fm = "no frontmatter either"
    repo_with_fm = "---\nname: foo\nversion: 1\ntotal_runs: 0\n---\nbody"
    assert _merge_frontmatter_stats(repo_with_fm, deployed_no_fm) == repo_with_fm


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
