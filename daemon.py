import asyncio
import logging
import logging.handlers
import os
import signal
import yaml
from pathlib import Path

# Suppress LiteLLM's default StreamHandler(stderr, DEBUG) — must be set before
# any module that imports litellm (calendar_scanner, email_scanner, etc.).
# Without this, every LiteLLM completion() call writes an INFO line directly to
# stderr, which launchd routes to error.log even though it isn't an error.
os.environ.setdefault("LITELLM_LOG", "ERROR")

from browser_watcher import BrowserWatcher
from code_scanner import CodeScanner
from email_scanner import EmailScanner
from calendar_scanner import CalendarScanner
from notes_scanner import NotesScanner
from slack_scanner import SlackScanner
from memory_cache import MemoryCache
import heartbeat as hb

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
LOG_MAX_BYTES = 10_000_000   # 10 MB per file
LOG_BACKUP_COUNT = 5         # keep up to 5 rotated files → ~60 MB ceiling


def _configure_logging(deploy_dir: Path) -> None:
    """Set up Python-managed rotating log files under deploy_dir/logs/.

    Writes to two files:
      - error.log  — WARNING and above (same as what launchd StandardErrorPath was capturing)
      - out.log    — INFO and above  (same as launchd StandardOutPath)

    A StreamHandler(stderr) is kept so launchd still captures fatal tracebacks
    that happen before logging is fully initialised on next restart.
    """
    logs_dir = deploy_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(LOG_FORMAT)

    # Root logger receives everything at INFO+
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # out.log — INFO and above
    out_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "out.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    out_handler.setLevel(logging.INFO)
    out_handler.setFormatter(fmt)

    # error.log — WARNING and above
    err_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "error.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(fmt)

    # stderr fallback — keeps launchd's StandardErrorPath useful for pre-init crashes
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)

    root.handlers.clear()
    root.addHandler(out_handler)
    root.addHandler(err_handler)
    root.addHandler(stderr_handler)


log = logging.getLogger("second-brain")

CONFIG_PATH = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/config.yaml"
BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"


def _build_slack_adapter(config: dict, chat):
    """Construct SlackTransportAdapter from config, or return None if not configured."""
    chat_cfg = config.get("chat", {})
    transports = chat_cfg.get("transports", chat_cfg.get("transport", "telegram"))
    if isinstance(transports, str):
        transports = [transports]
    if "slack" not in transports:
        return None
    slack_cfg = config.get("slack", {})
    bot_token = slack_cfg.get("bot_token", "")
    app_token = slack_cfg.get("app_token", "")
    user_id = slack_cfg.get("user_id", "")
    if not (bot_token and app_token and user_id):
        log.warning(
            "chat.transports includes 'slack' but slack.bot_token / slack.app_token / "
            "slack.user_id not set — Slack adapter disabled"
        )
        return None
    try:
        from slack_adapter import SlackTransportAdapter
        from command_core import CommandRouter
        router = CommandRouter()
        # Phase 3: register all TelegramChatHandler commands with the router via bridge
        if chat is not None:
            chat.register_with_router(router)
            log.info("Slack adapter: registered %d commands from TelegramChatHandler",
                     len(router._cmd_handlers))
        return SlackTransportAdapter(
            router=router,
            bot_token=bot_token,
            app_token=app_token,
            user_id=user_id,
        )
    except Exception as e:
        log.error("Failed to build Slack adapter: %s", e)
        return None


async def main():
    # Configure Python-managed rotating log files before any other logging
    deploy_dir = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
    _configure_logging(deploy_dir)

    VERSION_FILE = Path(__file__).parent / "VERSION"
    version = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "unknown"

    config = yaml.safe_load(CONFIG_PATH.read_text())
    # Env var takes precedence over config.yaml — config is shared via iCloud,
    # role is per-machine. Set SECOND_BRAIN_ROLE in each machine's launchd plist.
    role = os.environ.get("SECOND_BRAIN_ROLE") or config.get("daemon", {}).get("role", "full")
    log.info(f"Starting second-brain daemon v{version} — role: {role}")

    hb.init(BRAIN_DIR, role, version)

    # ── Memory cache setup ────────────────────────────────────────────────────
    # Full role: SQLite cache at ~/secondbrain/memory-cache.sqlite
    # Watcher role: pass-through mode (enabled=False, no SQLite)
    cache_enabled = config.get("daemon", {}).get("memory_cache", {}).get("enabled", True)
    if role == "full":
        cache = MemoryCache(
            db_path=deploy_dir / "memory-cache.sqlite",
            memories_dir=MEMORIES_DIR,
            enabled=cache_enabled
        )
    else:
        # Watcher role: pass-through mode
        cache = MemoryCache(
            db_path=None,
            memories_dir=MEMORIES_DIR,
            enabled=False
        )

    # Tier 1: local-source scanners — run on both watcher and full roles
    watcher = BrowserWatcher(role=role, cache=cache)
    code_scanner = CodeScanner(role=role)
    email_scanner = EmailScanner(role=role)
    calendar_scanner = CalendarScanner(role=role)
    notes_scanner = NotesScanner(role=role)
    slack_scanner = SlackScanner(role=role)

    tasks = [
        watcher.run_loop,
        code_scanner.run_loop,
        email_scanner.run_loop,
        calendar_scanner.run_loop,
        notes_scanner.run_loop,
        slack_scanner.run_loop,
    ]

    # Tier 2: full node only — chat handler, cloud scanners, aggregators, optimizer
    # Import after role check so watcher nodes never touch packages that may not be installed.
    chat = None
    if role == "full":
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
        from circle_sync_scanner import CircleSyncScanner
        from quota_scanner import QuotaScanner

        # Instantiate tier-2 scanners and services
        optimizer = SkillOptimizer(config)
        indexer = IndexBuilder(cache=cache)
        zoom_scanner = ZoomScanner(role=role)
        commitment_tracker = CommitmentTracker(role=role)
        contact_tracker = ContactTracker(role=role)
        project_inference_scanner = ProjectInferenceScanner(role=role, cache=cache)
        goal_agent = GoalProjectAgent(role=role, cache=cache)
        synthesis_scanner = SynthesisScanner(role=role, cache=cache)
        quota_scanner = QuotaScanner(deploy_dir, config, role)

        # Instantiate circle sync scanner if enabled
        circles_cfg = config.get("circles", {})
        circle_sync_scanner = CircleSyncScanner(role=role, cache=cache) if circles_cfg.get("enabled", False) else None

        # Build scanners dict for backfill command (tier-1 scanners already instantiated)
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

        # Instantiate chat handler with scanners and cache
        chat = TelegramChatHandler(scanners=scanners_dict, cache=cache)
        await chat.start()

        # ── Slack chat adapter (Phase 3+4) — built before notification_mgr ─────
        slack_adapter = _build_slack_adapter(config, chat)

        # Instantiate notification manager with all active transports (Phase 4)
        DEPLOY_DIR = deploy_dir  # set at top of main() for _configure_logging
        from telegram_adapter import TelegramAdapter
        tg_adapter = TelegramAdapter(chat)
        active_transports = [tg_adapter]
        if slack_adapter is not None:
            active_transports.append(slack_adapter)
        notification_mgr = NotificationManager(
            bot=chat.app.bot,
            deploy_dir=DEPLOY_DIR,
            transports=active_transports,
            cache=cache,
        )
        chat.notification_manager = notification_mgr
        goal_agent.notification_callback = notification_mgr.send_message

        # Wire up scanners for watchlist notifications
        email_scanner.notification_callback = notification_mgr.send_message
        slack_scanner.notification_callback = notification_mgr.send_message
        zoom_scanner.notification_callback = notification_mgr.send_message

        # Skill creator — wired into browser_watcher and chat_handler
        skill_creator = SkillCreator(config)
        skill_creator._notification_callback = notification_mgr.send_message
        watcher.skill_creator = skill_creator
        watcher.skill_optimizer = optimizer
        chat.skill_creator = skill_creator

        # Report scheduler — 13th loop
        report_scheduler = ReportScheduler(
            config=config,
            bot=chat.app.bot,
            chat_id_getter=notification_mgr.get_chat_id,
            deploy_dir=DEPLOY_DIR,
        )
        chat.report_scheduler = report_scheduler

        # Cache sweep loop — 60s cadence, full role only
        async def cache_sweep_loop(stop_event):
            """Sweep cache every 60s to catch iCloud-arrived files from watcher."""
            while not stop_event.is_set():
                beat_status, beat_error = "ok", None
                try:
                    added, updated, removed = await cache.sweep()
                    if added or updated or removed:
                        log.info(f"Cache sweep: {added} added, {updated} updated, {removed} removed")
                except Exception as exc:
                    log.exception("Cache sweep failed")
                    beat_status, beat_error = "error", str(exc)
                hb.record_beat("cache_sweep", beat_status, beat_error)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass

        tasks += [
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
        ]

        # Add circle sync scanner task if enabled
        if circle_sync_scanner is not None:
            tasks.append(circle_sync_scanner.run_loop)

        # Add Slack adapter task if configured
        if slack_adapter is not None:
            tasks.append(slack_adapter.start)

    stop_event = asyncio.Event()

    # Register signal handlers only after all objects are constructed.
    # stop_event is guaranteed to exist here; no risk of NameError on early signal.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig, lambda: (log.info("Shutdown signal received"), stop_event.set())
        )

    try:
        results = await asyncio.gather(
            *[t(stop_event) for t in tasks],
            return_exceptions=True,
        )
        for task_fn, result in zip(tasks, results):
            if isinstance(result, Exception):
                log.error(
                    "Task %s crashed: %s",
                    getattr(task_fn, "__qualname__", repr(task_fn)),
                    result,
                    exc_info=result,
                )
    finally:
        log.info("Flushing state before exit")
        watcher.save_seen_urls()
        if chat is not None:
            await chat.stop()
        # Stop Slack adapter if running (only available in full role)
        if role == "full" and 'slack_adapter' in locals() and slack_adapter is not None:
            await slack_adapter.stop()
        # Close cache connection
        cache.close()


if __name__ == "__main__":
    asyncio.run(main())
