"""Integration test: all daemon task loops start cleanly and exit on stop_event.

Covers two failure modes that would otherwise ship silently:
  1. A scanner constructor that raises (broken __init__, missing required key, etc.)
  2. A run_loop that doesn't honour stop_event (hangs on shutdown).

The drift guard (test_lifecycle_covers_all_daemon_scanners) fails automatically
when a new scanner is added to daemon.py but not added here.
"""
import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]

FAKE_CONFIG = {
    "telegram": {
        "bot_token": "12345:fake-token-for-lifecycle-test",
    },
    "user": {
        "telegram_user_id": "99999",
        "name": "TestUser",
        "timezone": "America/Los_Angeles",
    },
    "daemon": {"role": "full"},
}

# Task-producing classes imported in daemon.py (each contributes a run_loop/poll_loop).
# Drift guard below asserts this stays in sync with daemon.py's imports.
LIFECYCLE_SCANNER_CLASSES = {
    "BrowserWatcher",
    "CalendarScanner",
    "CircleSyncScanner",
    "CodeScanner",
    "CommitmentTracker",
    "ContactTracker",
    "EmailScanner",
    "GoalProjectAgent",
    "IndexBuilder",
    "NotesScanner",
    "NotificationManager",
    "ProjectInferenceScanner",
    "QuotaScanner",
    "ReportScheduler",
    "SkillOptimizer",
    "SlackScanner",
    "SynthesisScanner",
    "TelegramChatHandler",
    "ZoomScanner",
}

# Local-module imports in daemon.py that are utilities, not task-producing scanners.
NON_TASK_CLASSES = {
    "_load_ruleset",          # function alias imported as a name
    "CircleBotRunner",        # per-circle bot; conditional on circle config files
    "CommandRouter",          # routing utility; no run_loop
    "MemoryCache",            # cache layer; no run_loop
    "SkillCreator",           # wired into watcher/chat; no run_loop
    "SlackTransportAdapter",  # optional transport; appended via .start(), not a scanner
    "TelegramAdapter",        # transport wrapper; no run_loop
}


@pytest.fixture
def daemon_dirs(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / "config.yaml").write_text(yaml.dump(FAKE_CONFIG))
    return {"memories": memories, "deploy": deploy, "brain": brain}


async def test_all_loops_exit_cleanly_on_stop_event(daemon_dirs, monkeypatch):
    """All scanner constructors succeed and run_loops exit immediately when stop_event is set.

    Mirrors daemon.main(role='full') without signal handlers or chat.start().
    """
    import utils
    utils._reset_config_cache()

    memories = daemon_dirs["memories"]
    deploy = daemon_dirs["deploy"]
    brain = daemon_dirs["brain"]

    # ── Prevent real Keychain subprocess calls (Slack/Zoom/etc credential lookup) ──
    # slack_scanner and zoom_scanner call get_secret_or_env before the while loop,
    # so a pre-set stop_event does not prevent the Keychain subprocess from running.
    # Patching secrets.get_secret to return None avoids 5s-per-item timeouts.
    import secrets as secrets_mod
    monkeypatch.setattr(secrets_mod, "get_secret", lambda *_: None)

    # ── BrowserWatcher: constructor calls SkillExecutor and _load_seen_urls ──
    monkeypatch.setattr("browser_watcher.SkillExecutor", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("browser_watcher.SEEN_URLS_FILE", deploy / "seen-urls")

    # ── CodeScanner: constructor runs three migration methods ─────────────────
    monkeypatch.setattr("code_scanner.MEMORIES_DIR", memories)
    monkeypatch.setattr("code_scanner.DEPLOY_DIR", deploy)

    # ── CalendarScanner: constructor runs filename migration via STATE_FILE ───
    monkeypatch.setattr("calendar_scanner.MEMORIES_DIR", memories)
    monkeypatch.setattr("calendar_scanner.DEPLOY_DIR", deploy)
    monkeypatch.setattr("calendar_scanner.STATE_FILE", deploy / "calendar-scanner-state.json")

    # ── ProjectInferenceScanner: constructor runs candidate filename migration ─
    monkeypatch.setattr("project_inference_scanner.MEMORIES_DIR", memories)
    monkeypatch.setattr("project_inference_scanner.DEPLOY_DIR", deploy)

    # ── QuotaScanner: constructor calls _load_state() which reads STATE_FILE ──
    monkeypatch.setattr("quota_scanner.STATE_FILE", deploy / "quota-scanner-state.json")

    # ── TelegramChatHandler: constructor reads config, builds Telegram app ────
    # Use MagicMock for app so synchronous add_handler() calls don't produce
    # "coroutine never awaited" warnings; make only stop/shutdown awaitable.
    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.stop = AsyncMock()
    mock_app.shutdown = AsyncMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.concurrent_updates.return_value = mock_builder
    mock_builder.build.return_value = mock_app
    monkeypatch.setattr("chat_handler.ApplicationBuilder", MagicMock(return_value=mock_builder))
    monkeypatch.setattr("chat_handler.SkillExecutor", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("chat_handler.BRAIN_DIR", brain)
    monkeypatch.setattr("chat_handler.DEPLOY_DIR", deploy)

    # ── Shared MemoryCache (pass-through for isolation) ───────────────────────
    from memory_cache import MemoryCache
    cache = MemoryCache(deploy / "memory-cache.sqlite", memories, enabled=True)

    # ── Tier-1: always-on scanners ────────────────────────────────────────────
    from browser_watcher import BrowserWatcher
    from calendar_scanner import CalendarScanner
    from code_scanner import CodeScanner
    from email_scanner import EmailScanner
    from notes_scanner import NotesScanner
    from slack_scanner import SlackScanner

    watcher = BrowserWatcher(role="full", cache=cache)
    code_scanner = CodeScanner(role="full")
    email_scanner = EmailScanner(role="full")
    calendar_scanner = CalendarScanner(role="full")
    notes_scanner = NotesScanner(role="full")
    slack_scanner = SlackScanner(role="full")

    # ── Tier-2: full-node scanners ────────────────────────────────────────────
    from circle_sync_scanner import CircleSyncScanner
    from commitment_tracker import CommitmentTracker
    from contact_tracker import ContactTracker
    from goal_project_agent import GoalProjectAgent
    from index_builder import IndexBuilder
    from notification_manager import NotificationManager
    from project_inference_scanner import ProjectInferenceScanner
    from quota_scanner import QuotaScanner
    from report_scheduler import ReportScheduler
    from skill_optimizer import SkillOptimizer
    from synthesis_scanner import SynthesisScanner
    from zoom_scanner import ZoomScanner

    optimizer = SkillOptimizer(FAKE_CONFIG)
    indexer = IndexBuilder(cache=cache)
    zoom_scanner = ZoomScanner(role="full")
    commitment_tracker = CommitmentTracker(role="full", cache=cache)
    contact_tracker = ContactTracker(role="full", cache=cache)
    project_inference_scanner = ProjectInferenceScanner(role="full", cache=cache)
    goal_agent = GoalProjectAgent(role="full", cache=cache)
    synthesis_scanner = SynthesisScanner(role="full", cache=cache)
    quota_scanner = QuotaScanner(deploy, FAKE_CONFIG, "full")
    circle_sync_scanner = CircleSyncScanner(role="full", cache=cache)

    # TelegramChatHandler: instantiated last so scanners_dict is fully populated
    from chat_handler import TelegramChatHandler
    scanners_dict = {
        "readings": watcher,
        "email": email_scanner,
        "zoom": zoom_scanner,
        "calendar": calendar_scanner,
        "notes": notes_scanner,
        "slack": slack_scanner,
        "code": code_scanner,
        "quota_scanner": quota_scanner,
    }
    chat = TelegramChatHandler(scanners=scanners_dict, cache=cache)

    notification_mgr = NotificationManager(
        bot=mock_app.bot,
        deploy_dir=deploy,
        transports=[],
        cache=cache,
    )
    report_scheduler = ReportScheduler(
        config=FAKE_CONFIG,
        bot=MagicMock(),
        chat_id_getter=lambda: None,
        deploy_dir=deploy,
        cache=cache,
    )

    # ── Inline cache_sweep_loop (mirrors daemon.main()) ──────────────────────
    async def cache_sweep_loop(stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await cache.sweep()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    # ── Task list mirrors daemon.main(role='full') ────────────────────────────
    tasks = [
        # Tier 1
        watcher.run_loop,
        code_scanner.run_loop,
        email_scanner.run_loop,
        calendar_scanner.run_loop,
        notes_scanner.run_loop,
        slack_scanner.run_loop,
        # Tier 2
        chat.poll_loop,
        optimizer.run_loop,
        optimizer.run_urgent_loop,
        indexer.run_loop,
        zoom_scanner.run_loop,
        commitment_tracker.run_loop,
        contact_tracker.run_loop,
        notification_mgr.run_loop,
        report_scheduler.run_loop,
        project_inference_scanner.run_loop,
        goal_agent.run_loop,
        synthesis_scanner.run_loop,
        cache_sweep_loop,
        quota_scanner.run_loop,
        circle_sync_scanner.run_loop,
    ]

    # ── Pre-set stop_event so every while loop exits on first check ───────────
    # Design note: the event is set *before* tasks start so each loop's
    # `while not stop_event.is_set()` guard fires immediately, skipping the
    # work body and the inner sleep. This intentionally avoids running real
    # side-effectful work (browser history reads, Mail/Calendar/Slack API calls)
    # in the test environment. The tradeoff is that shutdown from *inside* a
    # sleep (asyncio.wait_for) is not exercised here; that path is covered by
    # per-module unit tests that mock the work body.
    stop_event = asyncio.Event()
    stop_event.set()

    results = await asyncio.wait_for(
        asyncio.gather(*[t(stop_event) for t in tasks], return_exceptions=True),
        timeout=10.0,
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, (
        f"{len(errors)} task loop(s) raised exceptions on startup:\n"
        + "\n".join(f"  {type(e).__name__}: {e}" for e in errors)
    )

    cache.close()


def test_lifecycle_covers_all_daemon_scanners():
    """Drift guard: fail when a scanner is added to daemon.py without updating this test.

    Parses daemon.py's ImportFrom statements and asserts every local-module
    import appears in LIFECYCLE_SCANNER_CLASSES or NON_TASK_CLASSES.
    """
    daemon_src = (REPO_ROOT / "daemon.py").read_text()
    tree = ast.parse(daemon_src)

    daemon_local_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if (REPO_ROOT / f"{node.module}.py").exists():
                for alias in node.names:
                    daemon_local_names.add(alias.asname or alias.name)

    uncovered = daemon_local_names - LIFECYCLE_SCANNER_CLASSES - NON_TASK_CLASSES
    assert not uncovered, (
        f"These names are imported from local modules in daemon.py but missing "
        f"from LIFECYCLE_SCANNER_CLASSES or NON_TASK_CLASSES in this test:\n"
        f"  {sorted(uncovered)}\n"
        f"If the new class has a run_loop, add it to LIFECYCLE_SCANNER_CLASSES "
        f"and to the test_all_loops_exit_cleanly_on_stop_event task list.\n"
        f"If it is a utility with no run_loop, add it to NON_TASK_CLASSES."
    )
