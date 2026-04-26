---
specmas: 3.0
kind: feature
id: feat-llm-quota-tracking
version: 1.0.0
created: 2026-04-25
status: implemented
shipped_version: "1.9.0"
complexity: moderate
maturity: 0
parent_system: second-brain
related_specs:
  - feat-llm-chat-import
  - feat-llm-chat-memories
  - feat-chat-handler
  - feat-proactive-notifications
---

# LLM Subscription Quota Tracking

## Overview

### Problem Statement

Claude.ai Pro and ChatGPT Plus impose 5-hour rolling-window message quotas on their web subscriptions. Hitting the cap mid-flow is disruptive — the user wants visibility into "how many messages remain in this window, when does it reset" so they can pace heavy work, switch tools, or queue prompts before throttling.

The user explicitly **rejected** these data sources during scoping:

- **Claude Code local usage files** — only covers CLI usage, not the Claude.ai web subscription quota (separate counter).
- **Anthropic API spend** — measures programmatic API token spend, not the Claude.ai Pro web message quota.
- **OpenAI API usage** — same problem for ChatGPT Plus: API counter ≠ subscription counter.

The user explicitly **chose** the web subscription as the source of truth. Vendors do not expose a public quota API. Anything we do is reverse-engineering the web UI, which makes this spec **fragile-on-purpose**: the scrape path will break whenever Claude.ai or ChatGPT changes its layout. To stay honest about that, the design ships with a manual self-report path as the primary mechanism, and treats automated scraping as opt-in / advanced.

### Scope

**In scope:**
- Track Claude.ai Pro 5-hour rolling-window message quota (used / cap / reset time).
- Track ChatGPT Plus 5-hour rolling-window message quota (same fields).
- New async loop `quota_scanner.py`, `full` role only, polls every 30 min when scrape is enabled; otherwise idle.
- Telegram `/quota` command — current state for both platforms.
- Telegram `/quota report <platform> <used>/<cap>` command — user self-reports current quota in seconds; sets `window_resets_at = now + 5h` (or `+ <reset> minutes` if supplied).
- Threshold alerts at 75% (warning) and 90% (critical) per platform, with per-threshold cooldown.
- Optional inclusion in daily briefing.
- Two acquisition paths, in priority order:
  1. **Self-report** (primary, ToS-clean): user types `/quota report …` after glancing at the web UI.
  2. **Web scrape** (opt-in, ToS-questionable): headless fetch using saved session cookie; brittle.
- State persisted in `quota-scanner-state.json`.

**Out of scope:**
- API token spend tracking — separate concern; could ship later as `feat-api-spend-tracking`.
- Other LLM products (Gemini Advanced, Copilot Pro, etc.) — only the two the user pays for today.
- Per-conversation cost decomposition.
- Predictive forecasting ("you'll hit cap in 2 hrs at this rate").
- Active throttling — observation + alerts only; no behaviour change to outbound LLM use.
- Sharing scraped session cookies across machines — each machine that opts into scraping holds its own cookie.

### Success Metrics

- `/quota` returns accurate state for both platforms when self-reported.
- A 75% threshold crossing fires exactly one warning per window, not on every poll.
- Daily briefing optionally includes a quota line when configured.
- Scrape-disabled deployments produce zero outbound HTTP to vendor sites.

---

## Open Questions

- **ToS:** Is automated web scraping of Claude.ai or ChatGPT against vendor ToS? Likely yes for both. Default ships with `quota.scrape_enabled: false`; the user must explicitly opt in per machine and accept the risk. The README will say so plainly.
- **Auth flow for scraping:** Saved session cookie file? OAuth-style helper? Selenium-style headless browser? — TBD before MVP. Self-report path needs no auth, so it can ship first while we sort scraping out.
- **Quota math:** Claude.ai's 5-hour windows are not aligned to wall-clock; the UI shows a relative timer. We capture the reset epoch at report time and decay locally. The decay is wrong if the user keeps using the web UI between reports — accepted as a known limitation of self-report.
- **What counts as a "message"?** Vendors include uploads/edits/regens differently. We track the number the UI literally shows, not what we infer.
- **Threshold defaults:** 75% / 90% chosen to match common "slow down" / "stop now" framing — adjust after a week of dogfooding.
- **ChatGPT quota model is non-uniform.** Claude.ai Pro is a single 5-hour message-count window. ChatGPT Plus has *different* caps per model (GPT-4o, o1, etc.) — some message-based, some token-based, all with different windows. This spec models a uniform 5-hour message window per platform, which is correct for Claude and approximate for ChatGPT. **Day-one position:** track ChatGPT as a single combined counter (whatever number the UI shows on the most-used model). **Future spec extension:** per-model sub-tracking under `chatgpt` if the simplification proves lossy in practice.

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `quota_scanner.py` async loop, `full` role only, 30-min cadence; if `scrape_enabled=false` and there's a recent self-report it just decays state; if both are absent it idles |
| FR-2 | State file `quota-scanner-state.json` per platform: `messages_used`, `messages_cap`, `window_resets_at` (ISO), `source` (`self_report\|scrape`), `last_seen_at`. Window decay is computed purely from `window_resets_at` against wall-clock time; `last_seen_at` is informational only |
| FR-3 | `/quota` (no args) shows both platforms: `Claude.ai: 23/40 (resets in 2h14m)`, `ChatGPT: 12/40 (resets in 4h02m)`; missing platforms show `(no data yet)` |
| FR-4 | `/quota report <platform> <used>/<cap>` — user self-reports current quota; sets `window_resets_at = now + 5h`, `source = self_report` |
| FR-5 | `/quota report <platform> <used>/<cap> reset <minutes>` — user supplies remaining-time override if visible in the UI |
| FR-6 | `/quota reset <platform>` — clears state for that platform (used when window has rolled over and user wants a clean baseline) |
| FR-7 | Threshold alert fires at `quota.warning_threshold` (default 0.75) and `quota.critical_threshold` (default 0.90); per-platform per-threshold cooldown 60 min |
| FR-8 | Daily briefing includes quota line when `quota.briefing_enabled: true` (default true) |
| FR-9 | Optional scraper functions `scrape_claude(cookie_path)` and `scrape_chatgpt(cookie_path)` return `dict(used, cap, window_resets_at)` matching the self-report shape; isolated in `quota_scrapers.py` |
| FR-10 | Scrapers gated on `quota.scrape_enabled: true` AND a cookie path being set; otherwise `quota_scrapers.py` is not even imported |
| FR-11 | Scrape failure degrades silently to last-known state and surfaces a one-time Telegram error nudge per 24h per platform |
| FR-12 | `COMMAND_REGISTRY` entry for `/quota`; `/help` renders it; handler-registration test passes |
| FR-13 | Config: `quota.scrape_enabled` (default false), `quota.claude_cookie_path`, `quota.chatgpt_cookie_path`, `quota.briefing_enabled` (default true), `quota.warning_threshold` (default 0.75), `quota.critical_threshold` (default 0.90), `quota.poll_interval_minutes` (default 30) |
| FR-14 | README clearly marks scraping as "advanced / fragile / at your own risk" and walks through the self-report flow as the recommended path |

---

## Design

### `quota_scanner.py`

Modeled after `commitment_tracker.py` shape:

```python
class QuotaScanner:
    def __init__(self, deploy_dir, config, send_telegram=None):
        self.state_path = deploy_dir / "quota-scanner-state.json"
        self.config = config["quota"]
        self.send = send_telegram
        self._state = self._load_state()

    async def run_loop(self):
        interval = self.config.get("poll_interval_minutes", 30) * 60
        while True:
            await self._tick()
            await asyncio.sleep(interval)

    async def _tick(self):
        if self.config.get("scrape_enabled", False):
            for platform, scraper in self._enabled_scrapers().items():
                try:
                    snapshot = await scraper(self.config[f"{platform}_cookie_path"])
                    self._apply(platform, snapshot, source="scrape")
                except Exception as e:
                    log.warning("Quota scrape failed for %s: %s", platform, e)
                    self._maybe_send_error_nudge(platform, e)
        # Self-report path needs no tick action — state was set by /quota report.
        # We just save (mtime preserves last_seen_at) and exit.
        self._save_state()

    def report(self, platform, used, cap, reset_minutes=None):
        resets_at = now() + timedelta(minutes=reset_minutes or 300)
        self._state[platform] = {
            "messages_used": used,
            "messages_cap": cap,
            "window_resets_at": resets_at.isoformat(),
            "source": "self_report",
            "last_seen_at": now().isoformat(),
        }
        self._save_state()

    def render_status(self) -> str: ...
    def threshold_state(self) -> dict[str, str]: ...   # for notification_manager
```

### `chat_handler.py` — `/quota` command

```python
async def cmd_quota(self, update, context):
    args = context.args or []
    if not args:
        await update.message.reply_text(self.quota.render_status())
        return

    if args[0] == "report":
        # /quota report claude 23/40 [reset 60]
        platform, used_cap, *rest = args[1:]
        used, cap = map(int, used_cap.split("/"))
        reset_min = None
        if rest and rest[0] == "reset":
            reset_min = int(rest[1])
        self.quota.report(platform, used, cap, reset_min)
        await update.message.reply_text(f"OK — {platform} at {used}/{cap}.")
        return

    if args[0] == "reset":
        self.quota.clear(args[1])
        await update.message.reply_text(f"Cleared {args[1]} state.")
        return

    await update.message.reply_text("Usage: /quota | /quota report <platform> <used>/<cap> [reset <min>] | /quota reset <platform>")
```

`COMMAND_REGISTRY` entry:

```python
("quota", "Track Claude.ai Pro / ChatGPT Plus message quotas. /quota | /quota report <p> <u>/<c> [reset <min>] | /quota reset <p>"),
```

### `quota_scrapers.py` (opt-in only)

```python
"""Best-effort scrapers for Claude.ai Pro and ChatGPT Plus quota counters.

WARNING: These scrape vendor web UIs that have no quota API. They will
break whenever Anthropic or OpenAI ships a layout change. They may also
violate vendor ToS. Both modules are imported only when the user explicitly
sets quota.scrape_enabled = true and supplies a session cookie path.
"""

async def scrape_claude(cookie_path: Path) -> dict: ...
async def scrape_chatgpt(cookie_path: Path) -> dict: ...
```

Implementation choice (Selenium vs Playwright vs raw httpx with cookie) is deferred to MVP — the spec just fixes the contract.

### `notification_manager.py` — threshold alerts

Add `_check_quota_thresholds(state: dict)` to the 60-sec tick. Notification manager today does not hold direct references to scanner instances — it reads scanner state files directly and operates on a `state` dict that the caller persists once per tick. This `_check_quota_thresholds` follows that same pattern:

```python
async def _check_quota_thresholds(self, state: dict) -> None:
    if state.get("muted", False):
        return
    cfg = self.config.get("quota", {})

    # Read quota state directly from quota-scanner-state.json — consistent with
    # how notification_manager handles other scanner state today (no instance
    # reference plumbing into notification_manager).
    quota_state = self._read_quota_state()

    # Threshold-crossing memo lives on the notification state dict, mirroring
    # existing keys like `sent_commitment_alerts` and `sent_pre_meeting`.
    sent = state.setdefault("sent_quota_alerts", {})
    transitions = detect_threshold_crossings(
        quota_state, sent,
        warn=cfg.get("warning_threshold", 0.75),
        crit=cfg.get("critical_threshold", 0.90),
        cooldown_min=60,
    )
    for platform, level in transitions.items():
        await self.send_message(
            f"⚠️ {platform} quota {level} — {render_one(platform, quota_state)}"
        )
    # Caller persists state once per tick.
```

`detect_threshold_crossings` and `render_one` are pure functions exposed by `quota_scanner.py` so notification_manager can call them without holding a scanner instance.

### Daily briefing integration

`notification_manager._compose_briefing()` appends a "Quotas" section when `quota.briefing_enabled` and at least one platform has data younger than 24h.

### Config (`config.yaml`)

```yaml
quota:
  scrape_enabled: false
  claude_cookie_path: ~/.config/secondbrain/claude.cookies
  chatgpt_cookie_path: ~/.config/secondbrain/chatgpt.cookies
  briefing_enabled: true
  warning_threshold: 0.75
  critical_threshold: 0.90
  poll_interval_minutes: 30
```

### `daemon.py`

- Add a 15th async loop entry: `quota_scanner.run_loop()` gated on `role == "full"`.
- Add `quota_scanner` to the `scanners_dict` passed to `TelegramChatHandler` (mirroring how `commitment_tracker`, `code_scanner`, etc. are injected today at `daemon.py:175,185`). `cmd_quota` resolves it via `self.scanners["quota_scanner"]`.
- Notification manager does **not** receive `quota_scanner` directly — it reads `quota-scanner-state.json` plus the pure helpers `detect_threshold_crossings` / `render_one` (see the threshold-alert section above).
- Reflect the new fourteen-→fifteen loop count in CLAUDE.md when the spec ships (not before).

### `~/secondbrain/` runtime files

Add `quota-scanner-state.json` to the deploy-dir doc table in CLAUDE.md and README.md.

---

## Test Plan

**Unit tests in `tests/unit/test_quota_scanner.py`:**

1. `test_self_report_persists_state` — `report("claude", 23, 40)` → state file has used=23, cap=40, source=`self_report`, `window_resets_at` ≈ now + 5h.
2. `test_self_report_with_explicit_reset` — `report("claude", 23, 40, reset_minutes=90)` → `window_resets_at` ≈ now + 90m.
3. `test_render_status_both_platforms` — state with both populated → render contains both lines.
4. `test_render_status_unknown_when_unset` — empty state → "no data yet" line per platform.
5. `test_window_decay_text` — report with `reset_minutes=60`, frozen-clock advance 30m → `render_status` shows "resets in 30m".
6. `test_threshold_warning_fires_once_per_window` — used→75% crossed → `detect_threshold_crossings` returns warning; immediate re-check within cooldown → empty.
7. `test_threshold_critical_after_warning` — used→75% then 90% → both fire; cooldown is per-threshold so critical isn't suppressed by warning.
8. `test_clear_platform_state` — `clear("claude")` → claude key removed, chatgpt key intact.
9. `test_scrape_disabled_no_module_import` — `scrape_enabled=false` and dependency `quota_scrapers` deliberately broken → scanner still constructs and ticks (proves no top-level import).
10. `test_scrape_failure_silent_with_24h_nudge` — mock scraper raises; first failure → one Telegram nudge; second failure within 24h → no nudge; third 25h later → nudge again.

**Unit tests in `tests/unit/test_chat_handler.py`:**

11. `test_cmd_quota_renders_status` — populated state → reply contains both platforms.
12. `test_cmd_quota_report_parses_used_cap` — `/quota report claude 23/40` → `quota.report` called with (claude, 23, 40, None).
13. `test_cmd_quota_report_with_reset_arg` — `/quota report claude 23/40 reset 90` → `report` called with (claude, 23, 40, 90).
14. `test_cmd_quota_reset_clears_platform` — `/quota reset chatgpt` → `quota.clear("chatgpt")` called.
15. `test_cmd_quota_usage_help` — bad subcommand → usage string returned.
16. `test_command_registry_includes_quota` — registry test passes for the new command.

**Unit tests in `tests/unit/test_notification_manager.py`:**

17. `test_quota_threshold_alert_uses_quota_module` — quota module reports warning crossing → `send_message` called with platform name and level; `state["sent_quota_alerts"]` updated.
18. `test_quota_threshold_respects_mute` — `state["muted"]=True` → no `send_message` even on critical crossing.
19. `test_briefing_includes_quota_when_enabled` — briefing path with `briefing_enabled=true` and recent state → output contains "Quotas" section.

---

## Verification

1. With `scrape_enabled=false` (default), zero outbound network traffic to claude.ai or chat.openai.com from the daemon — verify with `tcpdump` or Little Snitch during a poll.
2. Self-report flow: `/quota report claude 10/40`, then 30 min later `/quota` shows ~`(resets in 4h30m)`.
3. Threshold flow: report progressing through 30→60→75→90 → exactly one warning at 75 and one critical at 90.
4. Spec-status check: spec ships with `status: draft`; the babysitter prompt's priority-3 rule (approved specs without implementation) does not pick this up until the user flips status to `approved`.
