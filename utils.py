"""Shared helpers for second-brain daemon."""
import re
from pathlib import Path
from typing import Iterator

# Security (M8): filter iCloud conflict copies from memory file loaders
_CONFLICT_RE = re.compile(r" \(.*conflicted copy.*\)", re.IGNORECASE)


def is_conflict_copy(path: Path) -> bool:
    """True if filename looks like an iCloud sync conflict copy.

    Examples:
    - foo (conflicted copy).md
    - foo (Mac's conflicted copy).md
    - foo (Chris's MacBook Pro's conflicted copy 3).md
    """
    return bool(_CONFLICT_RE.search(path.name))


def glob_memories(directory: Path, pattern: str = "*.md") -> Iterator[Path]:
    """Glob memory files, filtering out iCloud conflict copies.

    Use this instead of raw directory.glob() to ensure conflict copies
    are excluded from all memory file loaders.
    """
    for path in directory.glob(pattern):
        if not is_conflict_copy(path):
            yield path
