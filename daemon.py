import asyncio
import logging
import os
import signal
import yaml
from pathlib import Path

from browser_watcher import BrowserWatcher

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

    watcher = BrowserWatcher(role=role)
    tasks = [watcher.run_loop]

    # Full node only: import and instantiate after role check so watcher
    # nodes never touch packages that may not be installed.
    chat = None
    if role == "full":
        from chat_handler import TelegramChatHandler
        from skill_optimizer import SkillOptimizer
        from index_builder import IndexBuilder
        from project_scanner import ProjectScanner
        chat = TelegramChatHandler()
        optimizer = SkillOptimizer()
        indexer = IndexBuilder()
        scanner = ProjectScanner(role=role)
        await chat.start()
        tasks += [
            chat.poll_loop,
            optimizer.run_loop,
            indexer.run_loop,
            scanner.run_loop,
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
