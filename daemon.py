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
from slack_scanner import SlackScanner

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

    # Tier 1: local-source scanners — run on both watcher and full roles
    watcher = BrowserWatcher(role=role)
    code_scanner = CodeScanner(role=role)
    email_scanner = EmailScanner(role=role)
    calendar_scanner = CalendarScanner(role=role)
    slack_scanner = SlackScanner(role=role)

    tasks = [
        watcher.run_loop,
        code_scanner.run_loop,
        email_scanner.run_loop,
        calendar_scanner.run_loop,
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

        # Instantiate tier-2 scanners and services
        optimizer = SkillOptimizer(config)
        indexer = IndexBuilder()
        zoom_scanner = ZoomScanner(role=role)
        commitment_tracker = CommitmentTracker(role=role)
        contact_tracker = ContactTracker(role=role)
        project_inference_scanner = ProjectInferenceScanner(role=role)
        goal_agent = GoalProjectAgent(role=role)
        synthesis_scanner = SynthesisScanner(role=role)

        # Build scanners dict for backfill command (tier-1 scanners already instantiated)
        scanners_dict = {
            "readings": watcher,
            "email": email_scanner,
            "zoom": zoom_scanner,
            "calendar": calendar_scanner,
            "slack": slack_scanner,
            "code": code_scanner,
        }

        # Instantiate chat handler with scanners
        chat = TelegramChatHandler(scanners=scanners_dict)
        await chat.start()

        # Instantiate notification manager and wire up cross-references
        DEPLOY_DIR = deploy_dir  # set at top of main() for _configure_logging
        notification_mgr = NotificationManager(bot=chat.app.bot, deploy_dir=DEPLOY_DIR)
        chat.notification_manager = notification_mgr
        goal_agent.notification_callback = notification_mgr.send_message

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
        ]

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


if __name__ == "__main__":
    asyncio.run(main())
