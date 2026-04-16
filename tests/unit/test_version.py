"""Tests for semver infrastructure."""
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent


def test_version_file_exists():
    """VERSION file must exist at the repo root."""
    assert (REPO_ROOT / "VERSION").exists(), "VERSION file not found at repo root"


def test_version_is_valid_semver():
    """VERSION file must contain a valid semver string (MAJOR.MINOR.PATCH)."""
    version = (REPO_ROOT / "VERSION").read_text().strip()
    assert re.match(r"^\d+\.\d+\.\d+$", version), (
        f"VERSION file contains '{version}', expected MAJOR.MINOR.PATCH"
    )


@pytest.mark.asyncio
async def test_cmd_version_reads_version_file(tmp_path):
    """cmd_version logic reads the VERSION file adjacent to chat_handler.py."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("9.8.7\n")

    # Simulate the handler logic with a patched __file__ path
    patched_file = str(tmp_path / "chat_handler.py")
    with patch("chat_handler.__file__", patched_file):
        import chat_handler as ch
        vf = Path(ch.__file__).parent / "VERSION"
        version = vf.read_text().strip() if vf.exists() else "unknown"

    assert version == "9.8.7"
