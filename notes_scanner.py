"""Notes Scanner — reads Apple Notes via AppleScript and writes apple_notes memory files.

Runs every 5 minutes on both watcher and full roles. Each Apple Note becomes an
``apple_notes`` memory file. Notes that live in todo-style folders (or whose
titles/bodies look like checklists) are flagged ``has_todos: true`` so they appear
prominently in briefings and /notes searches.

State is persisted in ``DEPLOY_DIR/notes-scanner-state.json`` keyed by note ID.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import yaml

from heartbeat import record_beat
from utils import load_config

log = logging.getLogger("notes-scanner")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
STATE_FILE = DEPLOY_DIR / "notes-scanner-state.json"

# Maximum notes to process per scan cycle (avoids long cycles when many notes exist)
MAX_NOTES_PER_CYCLE = 30

# Folder names that imply todo content even without explicit checklist markers
_TODO_FOLDER_NAMES: frozenset[str] = frozenset(
    {"todos", "to do", "to-do", "tasks", "task list", "task", "to dos"}
)

# Patterns in note title or body that hint at checklist content
_TODO_TITLE_RE = re.compile(r'\b(todo|to.?do|tasks?|checklist)\b', re.IGNORECASE)
_TODO_BODY_RE = re.compile(r'^\s*[\-\*•]\s+\S|^\s*\[\s*[xX ]?\s*\]', re.MULTILINE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', text.lower())
    return s.strip('-')[:40].rstrip('-')


def _note_id_hash(note_id: str) -> str:
    return hashlib.sha1(note_id.encode()).hexdigest()[:6]


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities from AppleScript body output."""
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common HTML entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"')
    # Collapse extra whitespace while preserving paragraph structure
    lines = [l.rstrip() for l in text.split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _has_todos(folder: str, title: str, body: str) -> bool:
    """Return True if the note appears to contain todo/checklist content."""
    if folder.lower().strip() in _TODO_FOLDER_NAMES:
        return True
    if _TODO_TITLE_RE.search(title):
        return True
    if _TODO_BODY_RE.search(body):
        return True
    return False


def _memory_path(folder: str, title: str, note_id: str) -> Path:
    folder_slug = _slugify(folder) or "notes"
    title_slug = _slugify(title) or "untitled"
    id_hash = _note_id_hash(note_id)
    return MEMORIES_DIR / f"apple-notes-{folder_slug}-{title_slug}-{id_hash}.md"


def _parse_frontmatter(path: Path) -> dict:
    try:
        parts = path.read_text(encoding="utf-8").split("---", 2)
        if len(parts) < 3:
            return {}
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


# ── AppleScript reader ────────────────────────────────────────────────────────

_METADATA_SCRIPT = """\
set sep to "|||"
set recordSep to "=====NOTE====="
tell application "Notes"
    set output to ""
    set allFolders to every folder
    repeat with aFolder in allFolders
        set fName to name of aFolder
        repeat with aNote in notes of aFolder
            try
                set nTitle to name of aNote as string
                set nId to id of aNote as string
                set modDate to modification date of aNote
                set modStr to (year of modDate as string) & "-"
                if (month of modDate as integer) < 10 then
                    set modStr to modStr & "0"
                end if
                set modStr to modStr & (month of modDate as integer as string) & "-"
                if (day of modDate as integer) < 10 then
                    set modStr to modStr & "0"
                end if
                set modStr to modStr & (day of modDate as integer as string)
                set output to output & recordSep & return
                set output to output & fName & sep & nTitle & sep & nId & sep & modStr & return
            end try
        end repeat
    end repeat
end tell
return output
"""

_BODY_SCRIPT_TEMPLATE = """\
tell application "Notes"
    set noteRef to note id "{note_id}"
    return body of noteRef as string
end tell
"""


def _run_osascript(script: str, timeout: int = 60) -> str:
    """Run an AppleScript and return its stdout, or "" on error/timeout."""
    try:
        proc = subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            log.warning("AppleScript timed out after %ds", timeout)
            return ""
        if proc.returncode != 0:
            log.debug("osascript exited %d: %s", proc.returncode, stderr.decode(errors="replace").strip()[:200])
            return ""
        return stdout.decode(errors="replace").strip()
    except Exception as e:
        log.debug("osascript failed: %s", e)
        return ""


def _read_note_metadata() -> list[dict]:
    """Return list of {folder, title, id, modified_date} for all notes."""
    raw = _run_osascript(_METADATA_SCRIPT, timeout=60)
    if not raw:
        return []

    notes = []
    for record in raw.split("=====NOTE====="):
        record = record.strip()
        if not record:
            continue
        lines = [l.strip() for l in record.splitlines() if l.strip()]
        if not lines:
            continue
        # Last non-empty line should be: folder|||title|||id|||YYYY-MM-DD
        data_line = lines[-1]
        parts = data_line.split("|||")
        if len(parts) < 4:
            continue
        folder, title, note_id, modified = parts[0], parts[1], parts[2], parts[3]
        if not note_id:
            continue
        notes.append({
            "folder": folder.strip(),
            "title": title.strip(),
            "id": note_id.strip(),
            "modified": modified.strip(),
        })
    return notes


def _read_note_body(note_id: str) -> str:
    """Fetch body text for a single note by ID."""
    safe_id = note_id.replace('"', '')
    script = _BODY_SCRIPT_TEMPLATE.format(note_id=safe_id)
    raw = _run_osascript(script, timeout=30)
    if not raw:
        return ""
    return _strip_html(raw)


# ── State ─────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        log.exception("Failed to save notes scanner state")


# ── Memory writer ─────────────────────────────────────────────────────────────

def _write_memory(note: dict, body: str) -> None:
    """Atomically write a memory file for the given note."""
    path = _memory_path(note["folder"], note["title"], note["id"])
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    has_todos = _has_todos(note["folder"], note["title"], body)

    fm: dict = {
        "source_title": note["title"][:120],
        "type": "apple_notes",
        "folder": note["folder"],
        "has_todos": has_todos,
        "modified": note["modified"],
        "last_scanned": now,
        "tags": ["apple-notes"] + (["todo"] if has_todos else []),
    }
    frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

    # Truncate body to keep files reasonable
    body_display = body[:5000] if body else "(No content)"
    content = f"---\n{frontmatter}---\n\n# {note['title']}\n\n{body_display}\n"

    tmp_path = path.with_suffix(".tmp")
    try:
        MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(content, encoding="utf-8")
        os.rename(str(tmp_path), str(path))
        log.debug("Wrote %s", path.name)
    except Exception:
        log.exception("Failed to write %s", path)
        try:
            tmp_path.unlink()
        except Exception:
            pass


# ── Scanner class ─────────────────────────────────────────────────────────────

class NotesScanner:
    def __init__(self, role: str = "full"):
        self._role = role

    def _scanner_config(self) -> dict:
        cfg = load_config(CONFIG_PATH)
        return cfg.get("notes_scanner", {})

    def _run_scan(self) -> None:
        sc = self._scanner_config()
        if not sc.get("enabled", True):
            log.debug("Notes scanner disabled in config")
            return

        skip_folders = {f.lower() for f in sc.get("skip_folders", [])}

        notes = _read_note_metadata()
        if not notes:
            log.debug("No Apple Notes found (Notes.app may not be running or no notes exist)")
            return

        state = _load_state()
        to_process = []

        for note in notes:
            if note["folder"].lower() in skip_folders:
                continue
            last_mod = state.get(note["id"])
            if last_mod == note["modified"]:
                continue
            to_process.append(note)

        if not to_process:
            log.debug("Notes scanner: no changes detected (%d total notes)", len(notes))
            return

        # Rate-limit: process at most MAX_NOTES_PER_CYCLE per cycle
        batch = to_process[:MAX_NOTES_PER_CYCLE]
        log.info("Notes scanner: processing %d/%d changed notes", len(batch), len(to_process))

        for note in batch:
            try:
                body = _read_note_body(note["id"])
                _write_memory(note, body)
                state[note["id"]] = note["modified"]
            except Exception:
                log.exception("Failed to process note %r", note.get("title", "?"))

        _save_state(state)

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        sc = self._scanner_config()
        interval = sc.get("interval_seconds", 300)
        log.info("Notes scanner started — polling every %ds", interval)

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await asyncio.to_thread(self._run_scan)
            except Exception as exc:
                log.exception("Uncaught error in notes scanner cycle")
                beat_status, beat_error = "error", str(exc)
            record_beat("notes_scanner", beat_status, beat_error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
