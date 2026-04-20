import os
import subprocess
from unittest.mock import patch, MagicMock
import pytest

import secrets as secrets_module


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the secrets cache before each test."""
    secrets_module._cache.clear()
    yield
    secrets_module._cache.clear()


def test_get_secret_returns_value_on_success():
    """get_secret returns the secret when security command succeeds."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "my-secret-value\n"

    with patch('subprocess.run', return_value=mock_result) as mock_run:
        result = secrets_module.get_secret("test_key")

    assert result == "my-secret-value"
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "security" in args
    assert "find-generic-password" in args
    assert "secondbrain-test_key" in args


def test_get_secret_returns_none_on_miss():
    """get_secret returns None when Keychain item not found (returncode 44)."""
    mock_result = MagicMock()
    mock_result.returncode = 44
    mock_result.stdout = ""

    with patch('subprocess.run', return_value=mock_result):
        result = secrets_module.get_secret("missing_key")

    assert result is None


def test_get_secret_caches_result():
    """get_secret caches the result and doesn't call subprocess again."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "cached-secret\n"

    with patch('subprocess.run', return_value=mock_result) as mock_run:
        result1 = secrets_module.get_secret("cached_key")
        result2 = secrets_module.get_secret("cached_key")

    assert result1 == "cached-secret"
    assert result2 == "cached-secret"
    # Should only be called once due to caching
    assert mock_run.call_count == 1


def test_get_secret_handles_subprocess_error():
    """get_secret returns None when subprocess raises FileNotFoundError."""
    with patch('subprocess.run', side_effect=FileNotFoundError("security not found")):
        result = secrets_module.get_secret("error_key")

    assert result is None


def test_get_secret_handles_timeout():
    """get_secret returns None when subprocess times out."""
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired("security", 5)):
        result = secrets_module.get_secret("timeout_key")

    assert result is None


def test_get_secret_or_env_prefers_keychain():
    """get_secret_or_env returns Keychain value even when env var is set."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "keychain-value\n"

    with patch('subprocess.run', return_value=mock_result):
        with patch.dict(os.environ, {"TEST_ENV_VAR": "env-value"}):
            result = secrets_module.get_secret_or_env("test_key", "TEST_ENV_VAR")

    assert result == "keychain-value"


def test_get_secret_or_env_falls_back_to_env():
    """get_secret_or_env returns env var when Keychain misses."""
    mock_result = MagicMock()
    mock_result.returncode = 44
    mock_result.stdout = ""

    with patch('subprocess.run', return_value=mock_result):
        with patch.dict(os.environ, {"TEST_ENV_VAR": "env-fallback"}):
            result = secrets_module.get_secret_or_env("test_key", "TEST_ENV_VAR")

    assert result == "env-fallback"


def test_get_secret_or_env_returns_none_when_missing():
    """get_secret_or_env returns None when both Keychain and env var miss."""
    mock_result = MagicMock()
    mock_result.returncode = 44
    mock_result.stdout = ""

    with patch('subprocess.run', return_value=mock_result):
        with patch.dict(os.environ, {}, clear=True):
            result = secrets_module.get_secret_or_env("test_key", "MISSING_VAR")

    assert result is None


def test_get_secret_or_env_caches_env_fallback():
    """get_secret_or_env caches env-var fallback to avoid repeated log messages."""
    mock_result = MagicMock()
    mock_result.returncode = 44

    with patch('subprocess.run', return_value=mock_result):
        with patch.dict(os.environ, {"TEST_ENV": "env-val"}):
            result1 = secrets_module.get_secret_or_env("cached_env", "TEST_ENV")
            result2 = secrets_module.get_secret_or_env("cached_env", "TEST_ENV")

    assert result1 == "env-val"
    assert result2 == "env-val"
    # Second call should hit cache, not call subprocess again
    # (cache was populated by first call's env fallback)
    assert "cached_env" in secrets_module._cache
