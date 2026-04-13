"""Unit tests for llm_routes.resolve()."""
from llm_routes import ROUTES, resolve


def test_summarize_resolves_to_gemini():
    assert resolve("summarize") == "gemini/gemini-2.0-flash"


def test_chat_resolves_to_claude_sonnet():
    assert resolve("chat") == "claude-sonnet-4-20250514"


def test_optimizer_resolves_to_claude_sonnet():
    assert resolve("optimizer") == "claude-sonnet-4-20250514"


def test_judge_resolves_to_claude_haiku():
    assert resolve("judge") == "claude-haiku-4-5-20251001"


def test_concrete_model_id_passes_through():
    assert resolve("gemini/gemini-2.0-flash") == "gemini/gemini-2.0-flash"
    assert resolve("claude-opus-4-6") == "claude-opus-4-6"


def test_unknown_alias_passes_through_unchanged():
    assert resolve("does-not-exist") == "does-not-exist"


def test_routes_covers_all_current_aliases():
    assert set(ROUTES.keys()) == {"summarize", "chat", "optimizer", "judge"}
