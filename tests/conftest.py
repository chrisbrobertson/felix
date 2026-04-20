import sys
from pathlib import Path

import pytest

# Add repo root to path so tests can import modules directly (they live in root, not a package)
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _isolate_skill_checksums(monkeypatch, tmp_path_factory):
    """Isolate the M6 skill-checksum manifest from tests.

    Production sets `_CHECKSUM_FILE` to `~/secondbrain/skill-checksums.json` at
    import time. When tests write their own `summarize-webpage.md` fixtures to
    `tmp_path`, the file hash differs from what `install.sh` recorded, causing
    checksum-verify failures. Redirecting to a non-existent tmp path makes
    `_verify_skill_checksum()` fall through the "no manifest" branch and allow
    the load.
    """
    try:
        import skill_executor
    except ImportError:
        return
    ghost = tmp_path_factory.mktemp("skill-checksums") / "ghost.json"
    monkeypatch.setattr(skill_executor, "_CHECKSUM_FILE", ghost, raising=False)
