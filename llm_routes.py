"""
Central LLM route table.

Maps short route aliases ("summarize", "chat", "optimizer", "judge") to
concrete LiteLLM model IDs. Callers that do not go through SkillExecutor
(which has its own skill-frontmatter-driven preferred/fallback mechanism)
should call resolve() on their route argument before passing it to
acompletion().

Background: ~/.litellm/config.yaml used to be the source of truth for these
aliases, resolved implicitly by LiteLLM at acompletion time. That path is
fragile — silent failures when the YAML is missing, stale, or rewritten by
scripts/apply_skill_provider.py. Hardcoding the table here makes route
resolution deterministic and testable.
"""

ROUTES = {
    "summarize": "gemini/gemini-2.0-flash",        # high-volume scanners
    "chat": "claude-sonnet-4-20250514",            # Telegram Q&A
    "optimizer": "claude-sonnet-4-20250514",       # skill optimizer rewrites
    "judge": "claude-haiku-4-5-20251001",          # skill LLM-as-judge scoring
}


def resolve(alias_or_id: str) -> str:
    """Translate a route alias to a concrete LiteLLM model ID.

    If the input is a known alias (key in ROUTES), returns the mapped model ID.
    Otherwise returns the input unchanged — callers may pass concrete model IDs
    like "gemini/gemini-2.0-flash" and they pass through transparently, which
    preserves the existing behavior of config.yaml values that already point
    at concrete IDs.
    """
    return ROUTES.get(alias_or_id, alias_or_id)
