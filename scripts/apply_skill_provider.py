#!/usr/bin/env python3
"""
Apply LLM provider preference to skill files.

Invoked by install.sh at deploy time as:
    PROVIDER=<gemini|claude|both> python3 scripts/apply_skill_provider.py <skills_dir>

Rewrites preferred_model and fallback_model in skill frontmatter according to
the chosen provider strategy.
"""
import os
import sys
import tempfile
from pathlib import Path

import yaml


def apply_provider(skills_dir: Path, provider: str):
    """
    Walk all .md files in skills_dir and rewrite frontmatter model fields
    according to the provider strategy.

    Args:
        skills_dir: Path to directory containing skill .md files
        provider: One of "gemini", "claude", or "both"
    """
    if provider not in ("gemini", "claude", "both"):
        print(f"[error] Invalid provider: {provider}")
        sys.exit(1)

    for skill_file in sorted(skills_dir.glob("*.md")):
        _apply_to_file(skill_file, provider)


def _apply_to_file(skill_file: Path, provider: str):
    """Apply provider strategy to a single skill file."""
    content = skill_file.read_text()

    # Parse frontmatter
    if not content.startswith("---\n"):
        print(f"[warn] {skill_file.name}  (no frontmatter)")
        return

    parts = content.split("---", 2)
    if len(parts) < 3:
        print(f"[warn] {skill_file.name}  (malformed frontmatter)")
        return

    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        print(f"[warn] {skill_file.name}  (YAML parse error: {e})")
        return

    if "preferred_model" not in meta:
        print(f"[skip] {skill_file.name}  (no preferred_model field)")
        return

    current_preferred = meta["preferred_model"]
    is_sonnet = "sonnet" in current_preferred.lower()

    # Determine new values
    if provider == "gemini":
        new_preferred = "gemini/gemini-2.0-flash"
        new_fallback = None
    elif provider == "claude":
        if is_sonnet:
            new_preferred = current_preferred  # keep existing sonnet model
        else:
            new_preferred = "claude-haiku-4-5-20251001"
        new_fallback = None
    else:  # both
        if is_sonnet:
            new_preferred = current_preferred  # keep existing sonnet model
        else:
            new_preferred = "claude-haiku-4-5-20251001"
        new_fallback = "claude-haiku-4-5-20251001"

    # Check if changes are needed
    current_fallback = meta.get("fallback_model")
    if new_preferred == current_preferred and new_fallback == current_fallback:
        print(f"[skip] {skill_file.name}  (already at target config)")
        return

    # Update metadata
    meta["preferred_model"] = new_preferred
    if new_fallback is not None:
        meta["fallback_model"] = new_fallback
    elif "fallback_model" in meta:
        del meta["fallback_model"]

    # Rebuild frontmatter
    new_frontmatter = yaml.dump(meta, default_flow_style=False, sort_keys=False)
    new_content = f"---\n{new_frontmatter}---{parts[2]}"

    # Atomic write
    tmp_fd, tmp_path = tempfile.mkstemp(dir=skill_file.parent, prefix=f".{skill_file.name}.", suffix=".tmp")
    try:
        os.write(tmp_fd, new_content.encode("utf-8"))
        os.close(tmp_fd)
        os.rename(tmp_path, skill_file)
        print(f"[ok] {skill_file.name}  (preferred={new_preferred}, fallback={new_fallback or 'none'})")
    except Exception as e:
        os.close(tmp_fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        print(f"[error] {skill_file.name}  ({e})")
        raise


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: PROVIDER=<gemini|claude|both> python3 apply_skill_provider.py <skills_dir>")
        sys.exit(1)

    provider = os.environ.get("PROVIDER")
    if not provider:
        print("Error: PROVIDER environment variable not set")
        sys.exit(1)

    skills_dir = Path(sys.argv[1])
    if not skills_dir.is_dir():
        print(f"Error: {skills_dir} is not a directory")
        sys.exit(1)

    apply_provider(skills_dir, provider)
