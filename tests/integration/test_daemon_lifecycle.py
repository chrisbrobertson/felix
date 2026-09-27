"""Daemon lifecycle integration test — all async loops start and stop cleanly.

A scanner added with a broken constructor or a missing import will fail here
rather than silently at ``./install.sh && launchd reload``.

Two scenarios:
  1. Watcher-role: tier-1 (local-source) scanners only.
  2. Full-role: all scanners, with Telegram and LLM dependencies mocked.

Both tests pre-set ``stop_event`` so every ``run_loop`` exits on its very
first ``stop_event.is_set()`` check — the point is to catch broken
constructors and import errors, not to exercise scan logic.
"""
import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml


# ─── Minimal daemon config ────────────────────────────────────────────────────

_CONFIG: dict = {
    "telegram": {"bot_token": "fake-token"},
    "user": {"telegram_user_id": "12345", "name": "Test", "timezone": "UTC"},
    "daemon": {"role": "full", "memory_cache": {"enabled": False}},
    "goals": {"categories": ["work", "personal"]},
    "browser_watcher": {"interval_seconds": 300},
    "code_scanner": {"require_confirmation": False},
    "quota": {},
    "circles": {"enabled": False},
    "notifications": {},
    "skill_optimizer": {},
}

# Scanner modules that expose MEMORIES_DIR / DEPLOY_DIR / CONFIG_PATH / BRAIN_DIR
_SCANNER_MODULES = [
    "browser_watcher",
    "code_scanner",
    "email_scanner",
    "calendar_scanner",
    "notes_scanner",
    "slack_scanner",
    "commitment_tracker",
    "contact_tracker",
    "notification_manager",
    "project_inference_scanner",
    "goal_project_agent",
    "synthesis_scanner",
    "quota_scanner",
    "index_builder",
    "skill_optimizer",
    "skill_creator",
    "report_scheduler",
    "zoom_scanner",
    "heartbeat",
    "daemon",
]


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def lifecycle_dirs(tmp_path: Path):
    """Minimal on-disk structure; returns (brain_dir, deploy_dir)."""
    brain = tmp_path / "brain"
    deploy = tmp_path / "deploy"
    (brain / "memories").mkdir(parents=True)
    (brain / "skills").mkdir(parents=True)
    deploy.mkdir()
    (brain / "config.yaml").write_text(yaml.dump(_CONFIG))
    return brain, deploy


def _redirect_paths(monkeypatch, brain: Path, deploy: Path) -> None:
    """Point every scanner's module-level path constant at tmp dirs."""
    memories = brain / "memories"
    config_path = brain / "config.yaml"
    for mod_name in _SCANNER_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for attr, val in [
            ("MEMORIES_DIR", memories),
            ("DEPLOY_DIR", deploy),
            ("CONFIG_PATH", config_path),
            ("BRAIN_DIR", brain),
        ]:
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val)


def _make_fake_cache(brain: Path):
    from memory_cache import MemoryCache
    return MemoryCache(db_path=None, memories_dir=brain / "memories", enabled=False)


# ─── Test 1: watcher role ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watcher_role_loops_start_and_stop(lifecycle_dirs, monkeypatch):
    """Tier-1 (watcher-role) scanners all shut down cleanly when stop_event fires."""
    brain, deploy = lifecycle_dirs
    _redirect_paths(monkeypatch, brain, deploy)

    # SkillExecutor reads skill files from iCloud — mock it out.
    mock_executor_cls = MagicMock()
    mock_executor_cls.return_value = MagicMock()
    monkeypatch.setattr("browser_watcher.SkillExecutor", mock_executor_cls)

    from browser_watcher import BrowserWatcher
    from code_scanner import CodeScanner
    from email_scanner import EmailScanner
    from calendar_scanner import CalendarScanner
    from notes_scanner import NotesScanner
    from slack_scanner import SlackScanner

    cache = _make_fake_cache(brain)
    watcher = BrowserWatcher(role="watcher", cache=cache)
    code_scanner = CodeScanner(role="watcher")
    email_scanner = EmailScanner(role="watcher")
    calendar_scanner = CalendarScanner(role="watcher")
    notes_scanner = NotesScanner(role="watcher")
    slack_scanner = SlackScanner(role="watcher")

    stop_event = asyncio.Event()
    stop_event.set()  # immediate shutdown

    results = await asyncio.wait_for(
        asyncio.gather(
            watcher.run_loop(stop_event),
            code_scanner.run_loop(stop_event),
            email_scanner.run_loop(stop_event),
            calendar_scanner.run_loop(stop_event),
            notes_scanner.run_loop(stop_event),
            slack_scanner.run_loop(stop_event),
            return_exceptions=True,
        ),
        timeout=5.0,
    )

    exceptions = [r for r in results if isinstance(r, Exception)]
    assert not exceptions, f"Watcher-role loops raised exceptions on shutdown: {exceptions}"


# ─── Test 2: full role ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_role_loops_start_and_stop(lifecycle_dirs, monkeypatch):
    """All full-role scanners (tiers 1+2) shut down cleanly when stop_event fires.

    Heavy external dependencies (Telegram, LiteLLM, Zoom, Slack) are mocked.
    The test asserts that every constructor succeeds and every run_loop exits
    without raising when stop_event is pre-set.
    """
    brain, deploy = lifecycle_dirs
    _redirect_paths(monkeypatch, brain, deploy)

    # ── Mock LiteLLM so no real API calls happen ──────────────────────────────
    fake_llm_response = MagicMock()
    fake_llm_response.choices = [MagicMock()]
    fake_llm_response.choices[0].message.content = "ok"
    fake_llm_response.choices[0].message.tool_calls = None
    monkeypatch.setattr("litellm.acompletion", AsyncMock(return_value=fake_llm_response))

    # ── Mock SkillExecutor in every module that uses it ───────────────────────
    mock_skill_exec = MagicMock()
    mock_skill_exec.return_value = MagicMock()
    monkeypatch.setattr("browser_watcher.SkillExecutor", mock_skill_exec)
    monkeypatch.setattr("chat_handler.SkillExecutor", mock_skill_exec)

    # ── Mock Telegram ApplicationBuilder ──────────────────────────────────────
    mock_app = MagicMock()
    mock_app.add_handler = MagicMock()
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app
    monkeypatch.setattr("chat_handler.ApplicationBuilder", MagicMock(return_value=mock_builder))

    # ── Tier-1 scanners ───────────────────────────────────────────────────────
    from browser_watcher import BrowserWatcher
    from code_scanner import CodeScanner
    from email_scanner import EmailScanner
    from calendar_scanner import CalendarScanner
    from notes_scanner import NotesScanner
    from slack_scanner import SlackScanner

    # ── Tier-2 scanners ───────────────────────────────────────────────────────
    from chat_handler import TelegramChatHandler
    from skill_optimizer import SkillOptimizer
    from index_builder import IndexBuilder
    from zoom_scanner import ZoomScanner
    from commitment_tracker import CommitmentTracker
    from contact_tracker import ContactTracker
    from notification_manager import NotificationManager
    from skill_creator import SkillCreator
    from report_scheduler import ReportScheduler
    from project_inference_scanner import ProjectInferenceScanner
    from goal_project_agent import GoalProjectAgent
    from synthesis_scanner import SynthesisScanner
    from quota_scanner import QuotaScanner

    cache = _make_fake_cache(brain)

    # Instantiate tier-1 scanners
    watcher = BrowserWatcher(role="full", cache=cache)
    code_scanner = CodeScanner(role="full")
    email_scanner = EmailScanner(role="full")
    calendar_scanner = CalendarScanner(role="full")
    notes_scanner = NotesScanner(role="full")
    slack_scanner = SlackScanner(role="full")

    # Instantiate tier-2 scanners
    chat = TelegramChatHandler(scanners={}, cache=cache)
    optimizer = SkillOptimizer(_CONFIG)
    indexer = IndexBuilder(cache=cache)
    zoom_scanner = ZoomScanner(role="full")
    commitment_tracker = CommitmentTracker(role="full", cache=cache)
    contact_tracker = ContactTracker(role="full", cache=cache)
    notification_mgr = NotificationManager(
        bot=MagicMock(),
        deploy_dir=deploy,
        transports=[],
        cache=cache,
    )
    skill_creator = SkillCreator(_CONFIG)
    report_scheduler = ReportScheduler(
        config=_CONFIG,
        bot=MagicMock(),
        chat_id_getter=lambda: None,
        deploy_dir=deploy,
        cache=cache,
    )
    project_inference = ProjectInferenceScanner(role="full", cache=cache)
    goal_agent = GoalProjectAgent(role="full", cache=cache)
    synthesis_scanner = SynthesisScanner(role="full", cache=cache)
    quota_scanner = QuotaScanner(deploy, _CONFIG, "full")

    stop_event = asyncio.Event()
    stop_event.set()  # immediate shutdown

    # The cache_sweep_loop from daemon.py — inline it to keep the test self-contained
    async def cache_sweep_loop(stop_event):
        while not stop_event.is_set():
            await asyncio.wait_for(stop_event.wait(), timeout=60)

    task_fns = [
        watcher.run_loop,
        code_scanner.run_loop,
        email_scanner.run_loop,
        calendar_scanner.run_loop,
        notes_scanner.run_loop,
        slack_scanner.run_loop,
        chat.poll_loop,
        optimizer.run_loop,
        optimizer.run_urgent_loop,
        indexer.run_loop,
        zoom_scanner.run_loop,
        commitment_tracker.run_loop,
        contact_tracker.run_loop,
        notification_mgr.run_loop,
        report_scheduler.run_loop,
        project_inference.run_loop,
        goal_agent.run_loop,
        synthesis_scanner.run_loop,
        cache_sweep_loop,
        quota_scanner.run_loop,
    ]

    results = await asyncio.wait_for(
        asyncio.gather(
            *[fn(stop_event) for fn in task_fns],
            return_exceptions=True,
        ),
        timeout=10.0,
    )

    exceptions = [
        (task_fns[i].__qualname__ if i < len(task_fns) else f"task_{i}", r)
        for i, r in enumerate(results)
        if isinstance(r, Exception)
    ]
    assert not exceptions, (
        f"Full-role loops raised exceptions on shutdown:\n"
        + "\n".join(f"  {name}: {exc}" for name, exc in exceptions)
    )
