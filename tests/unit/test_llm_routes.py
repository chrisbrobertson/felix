"""Unit tests for llm_routes.resolve()."""
import pytest
from llm_routes import ROUTES, resolve


# ── Default (claude) provider ─────────────────────────────────────────────────

def test_summarize_resolves_to_claude_haiku(monkeypatch):
    monkeypatch.delenv("SECOND_BRAIN_PROVIDER", raising=False)
    assert resolve("summarize") == "claude-haiku-4-5-20251001"


def test_chat_resolves_to_claude_sonnet(monkeypatch):
    monkeypatch.delenv("SECOND_BRAIN_PROVIDER", raising=False)
    assert resolve("chat") == "claude-sonnet-4-6"


def test_optimizer_resolves_to_claude_sonnet(monkeypatch):
    monkeypatch.delenv("SECOND_BRAIN_PROVIDER", raising=False)
    assert resolve("optimizer") == "claude-sonnet-4-6"


def test_judge_resolves_to_claude_haiku(monkeypatch):
    monkeypatch.delenv("SECOND_BRAIN_PROVIDER", raising=False)
    assert resolve("judge") == "claude-haiku-4-5-20251001"


def test_concrete_model_id_passes_through(monkeypatch):
    monkeypatch.delenv("SECOND_BRAIN_PROVIDER", raising=False)
    assert resolve("gemini/gemini-2.0-flash") == "gemini/gemini-2.0-flash"
    assert resolve("claude-opus-4-6") == "claude-opus-4-6"


def test_unknown_alias_passes_through_unchanged(monkeypatch):
    monkeypatch.delenv("SECOND_BRAIN_PROVIDER", raising=False)
    assert resolve("does-not-exist") == "does-not-exist"


def test_routes_covers_all_current_aliases():
    assert set(ROUTES.keys()) == {"summarize", "chat", "optimizer", "judge"}


# ── gemini provider ───────────────────────────────────────────────────────────

def test_gemini_provider_all_routes_resolve_to_gemini_flash(monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "gemini")
    for alias in ("summarize", "chat", "optimizer", "judge"):
        assert resolve(alias) == "gemini/gemini-2.0-flash", f"alias={alias}"


def test_gemini_provider_concrete_id_passes_through(monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "gemini")
    assert resolve("openai/gpt-4o") == "openai/gpt-4o"


# ── openai provider ───────────────────────────────────────────────────────────

def test_openai_provider_cheap_routes_use_gpt4o_mini(monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "openai")
    assert resolve("summarize") == "openai/gpt-4o-mini"
    assert resolve("judge") == "openai/gpt-4o-mini"


def test_openai_provider_quality_routes_use_gpt4o(monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "openai")
    assert resolve("chat") == "openai/gpt-4o"
    assert resolve("optimizer") == "openai/gpt-4o"


def test_openai_provider_concrete_id_passes_through(monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "openai")
    assert resolve("claude-sonnet-4-6") == "claude-sonnet-4-6"


# ── both provider ─────────────────────────────────────────────────────────────

def test_both_provider_uses_claude_models(monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "both")
    assert resolve("summarize") == "claude-haiku-4-5-20251001"
    assert resolve("chat") == "claude-sonnet-4-6"


# ── unknown provider falls back to claude ─────────────────────────────────────

def test_unknown_provider_falls_back_to_claude(monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "unknown-provider")
    assert resolve("summarize") == "claude-haiku-4-5-20251001"
    assert resolve("chat") == "claude-sonnet-4-6"


# ── provider name is case-insensitive ─────────────────────────────────────────

def test_provider_name_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "OPENAI")
    assert resolve("chat") == "openai/gpt-4o"

    monkeypatch.setenv("SECOND_BRAIN_PROVIDER", "Gemini")
    assert resolve("summarize") == "gemini/gemini-2.0-flash"
