"""Shared helpers for second-brain daemon."""
import logging
import re
import time
from pathlib import Path
from typing import Iterator, Optional

import yaml

log = logging.getLogger("utils")

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


# ── iCloud-resilient config.yaml reader ──────────────────────────────────────
#
# config.yaml lives in iCloud Drive, so any read can hit
# `OSError(11, 'Resource deadlock avoided')` when the placeholder is being
# materialized. This crashed /briefing and every other Telegram command that
# routed through `_check_auth` during iCloud sync activity. All config reads
# should go through load_config(), which:
#   - retries EDEADLK up to 3 times with short backoffs,
#   - caches the parsed dict keyed by mtime so repeated command invocations
#     don't hit iCloud on every call,
#   - falls back to the last known-good cached value if all retries fail,
#   - returns {} if the file doesn't exist or has never been successfully read.

_config_cache: dict = {}
_config_cache_mtime: Optional[float] = None


def load_config(config_path: Path) -> dict:
    """Read and parse a YAML config file with iCloud EDEADLK resilience.

    Shared between chat_handler, notification_manager, and any other
    caller that hits `config.yaml` on the hot path of a user-visible
    command. Not suitable for background-loop callers that want to pick
    up mid-run config edits immediately — for those, call this and
    accept the at-most-mtime-stale semantics.
    """
    global _config_cache, _config_cache_mtime

    if not config_path.exists():
        return {}

    try:
        mtime = config_path.stat().st_mtime
        if _config_cache_mtime == mtime and _config_cache:
            return _config_cache
    except OSError:
        mtime = None

    delays = (0.1, 0.5, 1.0)
    for attempt, delay in enumerate(delays):
        try:
            parsed = yaml.safe_load(config_path.read_text()) or {}
            _config_cache = parsed
            _config_cache_mtime = mtime
            return parsed
        except OSError as e:
            if e.errno == 11:  # EDEADLK — transient iCloud lock
                if attempt < len(delays) - 1:
                    time.sleep(delay)
                    continue
                log.warning(
                    "config.yaml read hit iCloud EDEADLK after %d retries; "
                    "serving cached config (may be stale)", len(delays),
                )
            else:
                log.warning("config.yaml read failed: %s; serving cached config", e)
            break

    return _config_cache or {}


def _reset_config_cache() -> None:
    """Test-only: clear the module-level config cache."""
    global _config_cache, _config_cache_mtime
    _config_cache = {}
    _config_cache_mtime = None
