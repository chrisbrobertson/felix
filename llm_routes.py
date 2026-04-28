"""
Central LLM route table.

Maps short route aliases ("summarize", "chat", "optimizer", "judge") to
concrete LiteLLM model IDs. Callers that do not go through SkillExecutor
(which has its own skill-frontmatter-driven preferred/fallback mechanism)
should call resolve() on their route argument before passing it to
acompletion().

Routes are provider-aware: the SECOND_BRAIN_PROVIDER env var (set by
install.sh in the launchd plist) controls which model family is used.
Defaults to Claude when unset, preserving backward compatibility with
existing installs.

Background: ~/.litellm/config.yaml used to be the source of truth for these
aliases, resolved implicitly by LiteLLM at acompletion time. That path is
fragile — silent failures when the YAML is missing, stale, or rewritten by
scripts/apply_skill_provider.py. Hardcoding the table here makes route
resolution deterministic and testable.
"""

import os

# Canonical "quality tier" model for each provider (sonnet/GPT-4o-class).
# Mirrors the _QUALITY_MODEL table in scripts/apply_skill_provider.py.
_QUALITY_MODEL = {
    "gemini": "gemini/gemini-2.0-flash",
    "claude": "claude-sonnet-4-6",
    "both":   "claude-sonnet-4-6",
    "openai": "openai/gpt-4o",
}

# Canonical "cheap tier" model for each provider (high-volume summarisation).
# Mirrors the _CHEAP_MODEL table in scripts/apply_skill_provider.py.
_CHEAP_MODEL = {
    "gemini": "gemini/gemini-2.0-flash",
    "claude": "claude-haiku-4-5-20251001",
    "both":   "claude-haiku-4-5-20251001",
    "openai": "openai/gpt-4o-mini",
}


def _get_provider() -> str:
    """Return the active provider, defaulting to 'claude' when unset."""
    return os.environ.get("SECOND_BRAIN_PROVIDER", "claude").lower()


def _build_routes(provider: str | None = None) -> dict[str, str]:
    """Build the alias→model-ID table for the given (or current) provider."""
    p = provider if provider is not None else _get_provider()
    quality = _QUALITY_MODEL.get(p, _QUALITY_MODEL["claude"])
    cheap = _CHEAP_MODEL.get(p, _CHEAP_MODEL["claude"])
    return {
        "summarize": cheap,    # high-volume scanners
        "chat": quality,       # Telegram Q&A
        "optimizer": quality,  # skill optimizer rewrites
        "judge": cheap,        # skill LLM-as-judge scoring
    }


# Module-level snapshot for callers that import ROUTES directly (e.g. tests).
# Reflects the provider at import time; use resolve() for call-time routing.
ROUTES = _build_routes()


def resolve(alias_or_id: str) -> str:
    """Translate a route alias to a concrete LiteLLM model ID.

    If the input is a known alias (key in ROUTES), returns the mapped model ID
    for the active SECOND_BRAIN_PROVIDER. Otherwise returns the input unchanged
    — callers may pass concrete model IDs like "gemini/gemini-2.0-flash" and
    they pass through transparently, which preserves the existing behavior of
    config.yaml values that already point at concrete IDs.
    """
    return _build_routes().get(alias_or_id, alias_or_id)
