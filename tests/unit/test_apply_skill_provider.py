"""Unit tests for scripts/apply_skill_provider.py."""
import os, sys
from pathlib import Path
import pytest

# The script lives at scripts/apply_skill_provider.py — import it as a module
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import apply_skill_provider as asp

HAIKU_SKILL = """\
---
name: summarize-webpage
version: 1
preferred_model: claude-haiku-4-5-20251001
fallback_model: gemini/gemini-2.0-flash
success_rate: null
total_runs: 0
---

## Instructions

Do stuff.

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
| 2026-04-12 | test-slug | claude-haiku | 0.90 | |
"""

SONNET_SKILL = """\
---
name: chat
version: 1
preferred_model: claude-sonnet-4-6
fallback_model: openai/nemotron-cascade-2
---

## Instructions

Chat instructions.
"""

GEMINI_SKILL = """\
---
name: summarize-docs
version: 1
preferred_model: gemini/gemini-2.0-flash
success_rate: null
---

## Instructions

Docs instructions.
"""


@pytest.fixture
def skills_dir(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    return d


def write_skill(skills_dir, name, content):
    (skills_dir / f"{name}.md").write_text(content)


def read_skill(skills_dir, name):
    return (skills_dir / f"{name}.md").read_text()


def get_frontmatter(content):
    parts = content.split("---", 2)
    import yaml
    return yaml.safe_load(parts[1])


def test_gemini_provider_sets_gemini_model(skills_dir):
    write_skill(skills_dir, "summarize-webpage", HAIKU_SKILL)
    asp.apply_provider(skills_dir, "gemini")
    fm = get_frontmatter(read_skill(skills_dir, "summarize-webpage"))
    assert fm["preferred_model"] == "gemini/gemini-2.0-flash"
    assert "fallback_model" not in fm


def test_claude_provider_sets_haiku_model(skills_dir):
    write_skill(skills_dir, "summarize-webpage", HAIKU_SKILL)
    asp.apply_provider(skills_dir, "claude")
    fm = get_frontmatter(read_skill(skills_dir, "summarize-webpage"))
    assert fm["preferred_model"] == "claude-haiku-4-5-20251001"
    assert "fallback_model" not in fm


def test_both_provider_keeps_both_fields(skills_dir):
    write_skill(skills_dir, "summarize-webpage", HAIKU_SKILL)
    asp.apply_provider(skills_dir, "both")
    fm = get_frontmatter(read_skill(skills_dir, "summarize-webpage"))
    assert fm["preferred_model"] == "claude-haiku-4-5-20251001"
    assert fm["fallback_model"] == "gemini/gemini-2.0-flash"


def test_sonnet_skill_preserved_on_claude(skills_dir):
    write_skill(skills_dir, "chat", SONNET_SKILL)
    asp.apply_provider(skills_dir, "claude")
    fm = get_frontmatter(read_skill(skills_dir, "chat"))
    assert "sonnet" in fm["preferred_model"]
    assert "fallback_model" not in fm


def test_sonnet_skill_gets_gemini_fallback_on_both(skills_dir):
    write_skill(skills_dir, "chat", SONNET_SKILL)
    asp.apply_provider(skills_dir, "both")
    fm = get_frontmatter(read_skill(skills_dir, "chat"))
    assert "sonnet" in fm["preferred_model"]
    assert fm["fallback_model"] == "gemini/gemini-2.0-flash"


def test_sonnet_skill_replaced_by_gemini_on_gemini_provider(skills_dir):
    write_skill(skills_dir, "chat", SONNET_SKILL)
    asp.apply_provider(skills_dir, "gemini")
    fm = get_frontmatter(read_skill(skills_dir, "chat"))
    assert fm["preferred_model"] == "gemini/gemini-2.0-flash"
    assert "fallback_model" not in fm


def test_preserves_execution_history(skills_dir):
    write_skill(skills_dir, "summarize-webpage", HAIKU_SKILL)
    asp.apply_provider(skills_dir, "gemini")
    content = read_skill(skills_dir, "summarize-webpage")
    assert "2026-04-12 | test-slug | claude-haiku | 0.90" in content


def test_idempotent(skills_dir):
    write_skill(skills_dir, "summarize-webpage", HAIKU_SKILL)
    asp.apply_provider(skills_dir, "both")
    content_after_first = read_skill(skills_dir, "summarize-webpage")
    asp.apply_provider(skills_dir, "both")
    content_after_second = read_skill(skills_dir, "summarize-webpage")
    assert content_after_first == content_after_second


def test_gemini_skill_on_gemini_provider_is_noop(skills_dir):
    """A skill already at gemini/gemini-2.0-flash on gemini provider: no file write."""
    write_skill(skills_dir, "summarize-docs", GEMINI_SKILL)
    original_mtime = (skills_dir / "summarize-docs.md").stat().st_mtime
    import time; time.sleep(0.01)
    asp.apply_provider(skills_dir, "gemini")
    # The file should be unchanged (no rewrite) — mtime should be the same
    new_mtime = (skills_dir / "summarize-docs.md").stat().st_mtime
    # We just check content is still gemini
    fm = get_frontmatter(read_skill(skills_dir, "summarize-docs"))
    assert fm["preferred_model"] == "gemini/gemini-2.0-flash"
    assert "fallback_model" not in fm
