import asyncio
import logging
import os
import signal
import yaml
from pathlib import Path

from browser_watcher import BrowserWatcher
from project_scanner import ProjectScanner
from email_scanner import EmailScanner
from calendar_scanner import CalendarScanner
from slack_scanner import SlackScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("second-brain")

CONFIG_PATH = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/config.yaml"


async def main():
    config = yaml.safe_load(CONFIG_PATH.read_text())
    # Env var takes precedence over config.yaml — config is shared via iCloud,
    # role is per-machine. Set SECOND_BRAIN_ROLE in each machine's launchd plist.
    role = os.environ.get("SECOND_BRAIN_ROLE") or config.get("daemon", {}).get("role", "full")
    log.info(f"Starting second-brain daemon — role: {role}")

    # Tier 1: local-source scanners — run on both watcher and full roles
    watcher = BrowserWatcher(role=role)
    project_scanner = ProjectScanner(role=role)
    email_scanner = EmailScanner(role=role)
    calendar_scanner = CalendarScanner(role=role)
    slack_scanner = SlackScanner(role=role)

    tasks = [
        watcher.run_loop,
        project_scanner.run_loop,
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

        # Instantiate tier-2 scanners and services
        optimizer = SkillOptimizer(config)
        indexer = IndexBuilder()
        zoom_scanner = ZoomScanner(role=role)
        commitment_tracker = CommitmentTracker(role=role)
        contact_tracker = ContactTracker(role=role)

        # Build scanners dict for backfill command (tier-1 scanners already instantiated)
        scanners_dict = {
            "readings": watcher,
            "email": email_scanner,
            "zoom": zoom_scanner,
            "calendar": calendar_scanner,
            "slack": slack_scanner,
            "projects": project_scanner,
        }

        # Instantiate chat handler with scanners
        chat = TelegramChatHandler(scanners=scanners_dict)
        await chat.start()

        # Instantiate notification manager and wire up cross-references
        DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
        notification_mgr = NotificationManager(bot=chat.app.bot, deploy_dir=DEPLOY_DIR)
        chat.notification_manager = notification_mgr

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
        await asyncio.gather(*[t(stop_event) for t in tasks])
    finally:
        log.info("Flushing state before exit")
        watcher.save_seen_urls()
        if chat is not None:
            await chat.stop()


if __name__ == "__main__":
    asyncio.run(main())
