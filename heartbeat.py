"""
Per-loop heartbeat tracking for all daemon instances.

Each scanner calls record_beat() after every scan iteration. The state is
atomically flushed to BRAIN_DIR/heartbeat-{hostname}.json so the full-node
Telegram /status command can read across all connected machines.

Usage in a scanner's run_loop:
    from heartbeat import record_beat

    while not stop_event.is_set():
        beat_status, beat_error = "ok", None
        try:
            await self._run_scan()
        except Exception as exc:
            log.exception("...")
            beat_status, beat_error = "error", str(exc)
        record_beat("scanner_name", beat_status, beat_error)
        ...
"""

import json
import logging
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

_hostname: str = socket.gethostname()
_brain_dir: Optional[Path] = None
_role: str = "unknown"
_version: str = "unknown"
_daemon_started: str = ""
_loop_state: Dict[str, dict] = {}


def init(brain_dir: Path, role: str, version: str) -> None:
    """Call once from daemon.py after config is loaded."""
    global _brain_dir, _role, _version, _daemon_started
    _brain_dir = brain_dir
    _role = role
    _version = version
    _daemon_started = datetime.now(timezone.utc).isoformat()


def record_beat(loop_name: str, status: str = "ok", error: Optional[str] = None) -> None:
    """Record one scan iteration result for a named loop, then flush to disk."""
    _loop_state[loop_name] = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "error": error,
    }
    _flush()


def _flush() -> None:
    if _brain_dir is None:
        return
    data = {
        "hostname": _hostname,
        "role": _role,
        "version": _version,
        "daemon_started": _daemon_started,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "loops": _loop_state,
    }
    path = _brain_dir / f"heartbeat-{_hostname}.json"
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)
    except Exception as exc:
        log.debug("Heartbeat flush failed: %s", exc)


def read_all(brain_dir: Path) -> list:
    """Read all heartbeat files from brain_dir. Returns list of dicts, sorted by hostname."""
    results = []
    for path in sorted(brain_dir.glob("heartbeat-*.json")):
        try:
            results.append(json.loads(path.read_text()))
        except Exception:
            pass
    return results
