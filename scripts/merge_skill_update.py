#!/usr/bin/env python3
"""
Merge skill file updates from repo into the deployed skills directory.

For each .md skill file in the repo skills dir:
  - If the deployed file doesn't exist: copy it.
  - If repo version > deployed version: splice — take the repo prompt/instructions,
    preserve the deployed ## Execution History so run records are not lost.
  - Otherwise: skip (idempotent; also prevents clobbering optimizer-edited copies
    whose version may have been bumped higher than the repo).

Invoked by install.sh as:
    python3 scripts/merge_skill_update.py <repo_skills_dir> <deployed_skills_dir>
"""
import sys
import re
import tempfile
import os
from pathlib import Path

import yaml

_EXEC_HISTORY_MARKER = "\n## Execution History"


def _parse_version(content: str) -> int:
    """Return the integer version: from YAML frontmatter, or 0 if absent/unparseable."""
    m = re.match(r"^---\n(.*?\n)---\n", content, re.DOTALL)
    if not m:
        return 0
    try:
        fm = yaml.safe_load(m.group(1))
        return int(fm.get("version", 0)) if fm else 0
    except Exception:
        return 0


def _split_at_exec_history(content: str) -> tuple[str, str]:
    """Split content into (everything_before_exec_history, exec_history_section_and_after)."""
    idx = content.find(_EXEC_HISTORY_MARKER)
    if idx < 0:
        return content, ""
    return content[:idx], content[idx:]


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via a temp file + rename."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def merge_skill(repo_path: Path, dest_path: Path) -> str:
    """
    Merge a single skill file from repo into the deployed location.

    Returns:
        'copied'   — dest did not exist; repo file was copied verbatim.
        'updated'  — repo version > deployed version; prompt spliced, history preserved.
        'skipped'  — deployed version >= repo version; no change made.
    """
    repo_content = repo_path.read_text(encoding="utf-8")

    if not dest_path.exists():
        _atomic_write(dest_path, repo_content)
        return "copied"

    deployed_content = dest_path.read_text(encoding="utf-8")

    repo_ver = _parse_version(repo_content)
    deployed_ver = _parse_version(deployed_content)

    if repo_ver <= deployed_ver:
        return "skipped"

    # Splice: take repo prompt section, attach deployed execution history.
    repo_prompt, _ = _split_at_exec_history(repo_content)
    _, deployed_history = _split_at_exec_history(deployed_content)
    merged = repo_prompt + deployed_history
    _atomic_write(dest_path, merged)
    return "updated"


def run(repo_skills_dir: Path, deployed_skills_dir: Path) -> int:
    """Process all skill files. Returns exit code (0 = success)."""
    errors = 0
    for repo_skill in sorted(repo_skills_dir.glob("*.md")):
        dest = deployed_skills_dir / repo_skill.name
        try:
            result = merge_skill(repo_skill, dest)
            if result == "copied":
                print(f"  +  Copied {repo_skill.name}")
            elif result == "updated":
                print(f"  ↑  Updated {repo_skill.name} (prompt spliced, execution history preserved)")
            else:
                print(f"  -  Skipped {repo_skill.name}  (already up to date)")
        except Exception as exc:
            print(f"  !  ERROR processing {repo_skill.name}: {exc}", file=sys.stderr)
            errors += 1
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <repo_skills_dir> <deployed_skills_dir>", file=sys.stderr)
        sys.exit(2)
    repo_dir = Path(sys.argv[1])
    deployed_dir = Path(sys.argv[2])
    if not repo_dir.is_dir():
        print(f"Error: repo skills dir not found: {repo_dir}", file=sys.stderr)
        sys.exit(2)
    if not deployed_dir.is_dir():
        print(f"Error: deployed skills dir not found: {deployed_dir}", file=sys.stderr)
        sys.exit(2)
    sys.exit(run(repo_dir, deployed_dir))
