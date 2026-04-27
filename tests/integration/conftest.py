"""Shared pytest fixtures for integration tests."""
import pytest
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def brain_dir(tmp_path: Path) -> Path:
    """Create a temporary brain directory with config."""
    d = tmp_path / "brain"
    (d / "memories").mkdir(parents=True)
    (d / "config.yaml").write_text(
        'telegram:\n  bot_token: fake-token\n'
        'user:\n  telegram_user_id: "12345"\n  name: Chris\n'
        '  timezone: America/Los_Angeles\n'
        'goals:\n'
        '  categories:\n    - personal\n    - work\n    - family\n    - learning\n    - other\n'
    )
    return d


@pytest.fixture
def deploy_dir(tmp_path: Path) -> Path:
    """Create a temporary deploy directory."""
    d = tmp_path / "deploy"
    d.mkdir()
    return d


@pytest.fixture
def fake_llm(monkeypatch):
    """Mock LiteLLM acompletion to avoid real API calls."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "OK"
    response.choices[0].message.tool_calls = None
    mock = AsyncMock(return_value=response)
    monkeypatch.setattr("litellm.acompletion", mock)
    return mock


@pytest.fixture
def handler(brain_dir, deploy_dir, fake_llm, monkeypatch):
    """Create a TelegramChatHandler with mocked dependencies."""
    import chat_handler as ch
    from memory_cache import MemoryCache

    # Patch module-level constants used by cmd_* methods
    monkeypatch.setattr(ch, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(ch, "DEPLOY_DIR", deploy_dir)

    # Also patch imported module constants
    try:
        from commitment_tracker import MEMORIES_DIR as ct_mem
        monkeypatch.setattr("commitment_tracker.MEMORIES_DIR", brain_dir / "memories")
        monkeypatch.setattr("commitment_tracker.DEPLOY_DIR", deploy_dir)
    except ImportError:
        pass

    try:
        from goals_tracker import MEMORIES_DIR as gt_mem
        monkeypatch.setattr("goals_tracker.MEMORIES_DIR", brain_dir / "memories")
    except ImportError:
        pass

    try:
        from contact_tracker import MEMORIES_DIR as contact_mem
        monkeypatch.setattr("contact_tracker.MEMORIES_DIR", brain_dir / "memories")
        monkeypatch.setattr("contact_tracker.DEPLOY_DIR", deploy_dir)
    except ImportError:
        pass

    # Mock Telegram application builder
    mock_app = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    # Create memory cache
    cache = MemoryCache(
        deploy_dir / "memory-cache.sqlite",
        brain_dir / "memories",
        enabled=True
    )

    with patch("chat_handler.ApplicationBuilder", return_value=mock_builder), \
         patch("chat_handler.SkillExecutor"):
        h = ch.TelegramChatHandler(cache=cache)
        h.allowed_user_id = 12345

        # Inject dependencies that daemon.py sets
        h.notification_manager = MagicMock()
        h.skill_creator = MagicMock()
        h.report_scheduler = MagicMock()

        yield h


@pytest.fixture
def mk_update():
    """Factory for creating mock Telegram Update objects."""
    def _make(text: str = "", args: Optional[list] = None):
        u = MagicMock()
        u.effective_user.id = 12345
        u.message = AsyncMock()
        u.message.text = text
        ctx = MagicMock()
        ctx.args = args or []
        return u, ctx
    return _make
