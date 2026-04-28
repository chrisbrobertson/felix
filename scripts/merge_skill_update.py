#!/usr/bin/env python3
"""
Merge skill file updates from repo into the deployed skills directory.

For each .md skill file in the repo skills dir:
  - If the deployed file doesn't exist: copy it.
  - If repo version >= deployed version: splice — take the repo prompt/instructions,
    preserve deployed optimizer sections (## Top Examples, ## Execution History)
    and optimizer-tracked frontmatter stats so run records are not lost.
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
_TOP_EXAMPLES_MARKER = "\n## Top Examples"

# Fields written exclusively by the skill optimizer — preserve them across upgrades
# so regression tracking and optimizer gating don't restart from scratch.
_OPTIMIZER_STATS_FIELDS = frozenset(
    {
        "success_rate",
        "total_runs",
        "last_optimized",
        "prev_version_avg_score",
        "utility_score",
        "utility_score_updated",
        "score_trend",
    }
)


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


def _split_at_optimizer_sections(content: str) -> tuple[str, str]:
    """Split at the first optimizer-managed section (Top Examples or Execution History).

    Returns (prompt_body, optimizer_tail) where optimizer_tail contains everything
    from the first of ## Top Examples / ## Execution History onward, preserving
    both learned exemplars and execution history records across installs.
    """
    split_idx = len(content)
    for marker in (_TOP_EXAMPLES_MARKER, _EXEC_HISTORY_MARKER):
        idx = content.find(marker)
        if 0 <= idx < split_idx:
            split_idx = idx
    if split_idx == len(content):
        return content, ""
    return content[:split_idx], content[split_idx:]


def _merge_frontmatter_stats(repo_prompt: str, deployed_content: str) -> str:
    """Overlay optimizer-tracked stats from deployed frontmatter into repo_prompt.

    Only patches fields present in both files; leaves structure and field order intact.
    Returns repo_prompt unchanged if either file has no parseable frontmatter.
    """
    dep_fm_match = re.match(r"^---\n(.*?\n)---\n", deployed_content, re.DOTALL)
    repo_fm_match = re.match(r"^---\n(.*?\n)---\n", repo_prompt, re.DOTALL)
    if not dep_fm_match or not repo_fm_match:
        return repo_prompt
    try:
        dep_fm = yaml.safe_load(dep_fm_match.group(1)) or {}
    except Exception:
        return repo_prompt

    fm_block = repo_fm_match.group(0)
    body_after = repo_prompt[repo_fm_match.end():]

    to_insert: list[str] = []
    for field in _OPTIMIZER_STATS_FIELDS:
        if field not in dep_fm:
            continue
        val = dep_fm[field]
        if val is None:
            yaml_val = "null"
        elif isinstance(val, bool):
            yaml_val = "true" if val else "false"
        else:
            yaml_val = str(val)
        if re.search(rf"(?m)^{re.escape(field)}:", fm_block):
            # Field already in repo frontmatter: patch value in-place.
            fm_block = re.sub(
                rf"(?m)^({re.escape(field)}:)\s*.*$",
                rf"\1 {yaml_val}",
                fm_block,
            )
        else:
            # Field exists in deployed but not repo: queue for insertion so that
            # optimizer scores (utility_score, score_trend, etc.) survive installs
            # even on skill files that didn't ship those keys originally.
            to_insert.append(f"{field}: {yaml_val}")

    if to_insert:
        # fm_block ends with "---\n"; insert before the closing marker.
        fm_block = fm_block[:-4] + "\n".join(to_insert) + "\n---\n"

    return fm_block + body_after


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

    if repo_ver < deployed_ver:
        return "skipped"

    # Splice: take repo prompt section, preserve deployed optimizer sections
    # (## Top Examples and ## Execution History) and optimizer-tracked stats
    # so regression tracking and learned exemplars don't restart from zero.
    repo_prompt, _ = _split_at_optimizer_sections(repo_content)
    _, deployed_optimizer_tail = _split_at_optimizer_sections(deployed_content)
    repo_prompt = _merge_frontmatter_stats(repo_prompt, deployed_content)
    merged = repo_prompt + deployed_optimizer_tail
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
