"""Track LLM token usage across all skill executor calls.

State file: ~/secondbrain/usage-tracker-state.json
Shape:
  {
    "YYYY-MM-DD": {
      "<model-id>": {
        "prompt_tokens": int,
        "completion_tokens": int,
        "calls": int
      }
    }
  }

Usage recording is best-effort — failures are logged but never raised.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("usage-tracker")

DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
USAGE_STATE_FILE = DEPLOY_DIR / "usage-tracker-state.json"
RETENTION_DAYS = 30


def _load_state(state_file: Path) -> dict:
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception as e:
            log.warning("Failed to load usage state: %s", e)
    return {}


def _save_state(state: dict, state_file: Path) -> None:
    tmp = state_file.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        os.rename(str(tmp), str(state_file))
    except Exception as e:
        log.error("Failed to save usage state: %s", e)


def _prune_old_days(state: dict, retention_days: int) -> dict:
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    return {k: v for k, v in state.items() if k >= cutoff}


def record_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    state_file: Path = USAGE_STATE_FILE,
) -> None:
    """Record token usage for one LLM API call. Best-effort — never raises."""
    if not prompt_tokens and not completion_tokens:
        return
    try:
        state = _load_state(state_file)
        today = datetime.now().strftime("%Y-%m-%d")
        day = state.setdefault(today, {})
        entry = day.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
        entry["prompt_tokens"] += prompt_tokens
        entry["completion_tokens"] += completion_tokens
        entry["calls"] += 1
        state = _prune_old_days(state, RETENTION_DAYS)
        _save_state(state, state_file)
    except Exception as e:
        log.debug("record_usage failed (non-fatal): %s", e)


def render_usage(days: int = 7, state_file: Path = USAGE_STATE_FILE) -> str:
    """Return a human-readable usage summary for the last *days* days."""
    state = _load_state(state_file)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    totals: dict[str, dict] = {}
    for date, day_data in sorted(state.items()):
        if date < cutoff:
            continue
        for model, usage in day_data.items():
            entry = totals.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
            entry["prompt_tokens"] += usage.get("prompt_tokens", 0)
            entry["completion_tokens"] += usage.get("completion_tokens", 0)
            entry["calls"] += usage.get("calls", 0)

    if not totals:
        return f"No usage data for the last {days} day(s). Usage is recorded automatically as skills run."

    lines = [f"Token usage — last {days} day(s):"]
    grand_prompt = grand_completion = 0
    for model, data in sorted(totals.items()):
        p = data["prompt_tokens"]
        c = data["completion_tokens"]
        n = data["calls"]
        grand_prompt += p
        grand_completion += c
        lines.append(f"  {model}: {p:,} in + {c:,} out ({n} call{'s' if n != 1 else ''})")

    lines.append(f"Total: {grand_prompt + grand_completion:,} tokens across all models")
    return "\n".join(lines)


def render_daily_breakdown(state_file: Path = USAGE_STATE_FILE) -> str:
    """Return per-day totals for the last 7 days (compact view)."""
    state = _load_state(state_file)
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    lines = ["Daily token totals (last 7 days):"]
    found_any = False
    for date in sorted(state.keys()):
        if date < cutoff:
            continue
        day_data = state[date]
        day_total = sum(
            v.get("prompt_tokens", 0) + v.get("completion_tokens", 0)
            for v in day_data.values()
        )
        lines.append(f"  {date}: {day_total:,} tokens")
        found_any = True

    if not found_any:
        return "No daily usage data available."
    return "\n".join(lines)
