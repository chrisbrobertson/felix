"""Unit tests for notes_scanner.

External access (osascript, filesystem, config) is mocked throughout.
"""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import notes_scanner as ns
from notes_scanner import (
    NotesScanner,
    _slugify,
    _note_id_hash,
    _strip_html,
    _has_todos,
    _memory_path,
    _read_note_metadata,
    _load_state,
    _save_state,
    _write_memory,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Redirect module-level path constants to tmp directories."""
    mem = tmp_path / "memories"
    mem.mkdir()
    state = tmp_path / "notes-scanner-state.json"
    monkeypatch.setattr(ns, "MEMORIES_DIR", mem, raising=False)
    monkeypatch.setattr(ns, "STATE_FILE", state, raising=False)


# ── Helper unit tests ─────────────────────────────────────────────────────────

def test_slugify_basic():
    assert _slugify("Hello World!") == "hello-world"


def test_slugify_long():
    long = "a" * 60
    assert len(_slugify(long)) <= 40


def test_note_id_hash_length():
    assert len(_note_id_hash("x/Notes/1234")) == 6


def test_strip_html_removes_tags():
    html = "<p>Hello <b>World</b></p>"
    result = _strip_html(html)
    assert "<" not in result
    assert "Hello" in result
    assert "World" in result


def test_strip_html_decodes_entities():
    html = "AT&amp;T &lt;rocks&gt;"
    result = _strip_html(html)
    assert "&amp;" not in result
    assert "AT&T" in result


def test_has_todos_folder_name():
    assert _has_todos("Todos", "My Note", "some content")
    assert _has_todos("Tasks", "My Note", "some content")
    assert _has_todos("TO DO", "My Note", "some content")


def test_has_todos_title():
    assert _has_todos("Notes", "My TODO list", "some content")
    assert _has_todos("Notes", "Tasks for today", "some content")


def test_has_todos_body_checklist():
    # Markdown checkbox syntax
    assert _has_todos("Notes", "Shopping", "[ ] milk\n[x] eggs")
    assert _has_todos("Notes", "Work", "[ ] Fix the bug")
    # Unicode checkbox glyphs
    assert _has_todos("Notes", "Work", "☐ Call Alice\n☑ Email Bob")


def test_has_todos_body_generic_bullets_not_flagged():
    # Plain bullet lists must NOT trigger has_todos (#153 fix)
    assert not _has_todos("Notes", "Shopping", "- milk\n- eggs\n* bread")
    assert not _has_todos("Notes", "Trip", "• Visit the museum\n• Eat lunch")


def test_has_todos_action_items_folder():
    # New folder names added in #153
    assert _has_todos("Action Items", "Q2 tasks", "just content")
    assert _has_todos("Checklist", "Onboarding", "regular text")
    assert _has_todos("Checklists", "Packing", "regular text")


def test_has_todos_action_items_title():
    # Title regex covers "action items" pattern
    assert _has_todos("Notes", "My Action Items", "just content")
    assert _has_todos("Notes", "Team action item list", "just content")


def test_has_todos_false():
    assert not _has_todos("Personal", "My vacation plan", "Just a regular note about my trip.")


def test_memory_path_format():
    path = _memory_path("Work", "Project Plan", "x/Notes/abc123")
    assert path.name.startswith("apple-notes-")
    assert "work" in path.name
    assert "project-plan" in path.name


# ── AppleScript parsing tests ─────────────────────────────────────────────────

def test_read_note_metadata_parses_output():
    fake_output = (
        "=====NOTE=====\n"
        "Personal|||My Note|||x/Notes/1|||2026-05-01\n"
        "=====NOTE=====\n"
        "Work|||Project|||x/Notes/2|||2026-04-30\n"
    )
    with patch("notes_scanner._run_osascript", return_value=fake_output):
        notes = _read_note_metadata()
    assert len(notes) == 2
    assert notes[0]["folder"] == "Personal"
    assert notes[0]["title"] == "My Note"
    assert notes[0]["id"] == "x/Notes/1"
    assert notes[0]["modified"] == "2026-05-01"
    assert notes[1]["folder"] == "Work"


def test_read_note_metadata_empty_output():
    with patch("notes_scanner._run_osascript", return_value=""):
        notes = _read_note_metadata()
    assert notes == []


def test_read_note_metadata_skips_bad_lines():
    fake_output = (
        "=====NOTE=====\n"
        "incomplete_line\n"  # missing separators
        "=====NOTE=====\n"
        "Work|||Plan|||x/Notes/3|||2026-05-01\n"
    )
    with patch("notes_scanner._run_osascript", return_value=fake_output):
        notes = _read_note_metadata()
    # only the valid record should be parsed
    assert len(notes) == 1
    assert notes[0]["title"] == "Plan"


# ── State management tests ────────────────────────────────────────────────────

def test_load_state_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "STATE_FILE", tmp_path / "state.json", raising=False)
    state = _load_state()
    assert state == {}


def test_save_and_load_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "STATE_FILE", tmp_path / "state.json", raising=False)
    _save_state({"x/Notes/1": "2026-05-01"})
    loaded = _load_state()
    assert loaded == {"x/Notes/1": "2026-05-01"}


# ── Memory write tests ────────────────────────────────────────────────────────

def test_write_memory_creates_file():
    note = {"folder": "Personal", "title": "My Note", "id": "x/Notes/1", "modified": "2026-05-01"}
    _write_memory(note, "This is a note body.")
    files = list(ns.MEMORIES_DIR.glob("apple-notes-*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "apple_notes" in content
    assert "My Note" in content
    assert "This is a note body." in content


def test_write_memory_has_todos_flagged():
    note = {"folder": "Todos", "title": "Shopping", "id": "x/Notes/2", "modified": "2026-05-01"}
    _write_memory(note, "- milk\n- eggs\n- bread")
    files = list(ns.MEMORIES_DIR.glob("apple-notes-*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "has_todos: true" in content


def test_write_memory_no_tmp_file_left():
    note = {"folder": "Work", "title": "Plan", "id": "x/Notes/3", "modified": "2026-05-01"}
    _write_memory(note, "content")
    assert not list(ns.MEMORIES_DIR.glob("*.tmp"))


# ── Scanner run_scan tests ────────────────────────────────────────────────────

def test_run_scan_skips_unchanged_notes(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"x/Notes/1": "2026-05-01"}))
    monkeypatch.setattr(ns, "STATE_FILE", state_file, raising=False)

    fake_notes = [{"folder": "Work", "title": "Plan", "id": "x/Notes/1", "modified": "2026-05-01"}]
    with patch("notes_scanner._read_note_metadata", return_value=fake_notes), \
         patch("notes_scanner._read_note_body") as mock_body:
        scanner = NotesScanner(role="full")
        with patch.object(scanner, "_scanner_config", return_value={"enabled": True}):
            scanner._run_scan()
        mock_body.assert_not_called()


def test_run_scan_processes_new_notes(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    monkeypatch.setattr(ns, "STATE_FILE", state_file, raising=False)

    fake_notes = [{"folder": "Work", "title": "Plan", "id": "x/Notes/1", "modified": "2026-05-01"}]
    with patch("notes_scanner._read_note_metadata", return_value=fake_notes), \
         patch("notes_scanner._read_note_body", return_value="Note body text") as mock_body:
        scanner = NotesScanner(role="full")
        with patch.object(scanner, "_scanner_config", return_value={"enabled": True}):
            scanner._run_scan()
        mock_body.assert_called_once_with("x/Notes/1")
    assert len(list(ns.MEMORIES_DIR.glob("apple-notes-*.md"))) == 1


def test_run_scan_updates_state_after_write(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    monkeypatch.setattr(ns, "STATE_FILE", state_file, raising=False)

    fake_notes = [{"folder": "Work", "title": "Plan", "id": "x/Notes/1", "modified": "2026-05-05"}]
    with patch("notes_scanner._read_note_metadata", return_value=fake_notes), \
         patch("notes_scanner._read_note_body", return_value="body"):
        scanner = NotesScanner(role="full")
        with patch.object(scanner, "_scanner_config", return_value={"enabled": True}):
            scanner._run_scan()

    state = json.loads(state_file.read_text())
    assert state.get("x/Notes/1") == "2026-05-05"


def test_run_scan_skips_excluded_folders(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    monkeypatch.setattr(ns, "STATE_FILE", state_file, raising=False)

    fake_notes = [{"folder": "Archive", "title": "Old Note", "id": "x/Notes/99", "modified": "2024-01-01"}]
    with patch("notes_scanner._read_note_metadata", return_value=fake_notes), \
         patch("notes_scanner._read_note_body") as mock_body:
        scanner = NotesScanner(role="full")
        with patch.object(scanner, "_scanner_config", return_value={"enabled": True, "skip_folders": ["Archive"]}):
            scanner._run_scan()
        mock_body.assert_not_called()


def test_run_scan_disabled():
    with patch("notes_scanner._read_note_metadata") as mock_meta:
        scanner = NotesScanner(role="full")
        with patch.object(scanner, "_scanner_config", return_value={"enabled": False}):
            scanner._run_scan()
        mock_meta.assert_not_called()


# ── Bug-fix regression tests ──────────────────────────────────────────────────

def test_read_note_metadata_parses_datetime_format():
    """#146 — metadata parser handles the new YYYY-MM-DDTHH:MM:SS modified field."""
    fake_output = (
        "=====NOTE=====\n"
        "Personal|||My Note|||x/Notes/1|||2026-06-29T14:35:22\n"
    )
    with patch("notes_scanner._run_osascript", return_value=fake_output):
        notes = _read_note_metadata()
    assert len(notes) == 1
    assert notes[0]["modified"] == "2026-06-29T14:35:22"


def test_run_scan_detects_intraday_changes(tmp_path, monkeypatch):
    """#146 — two notes with same date but different times are treated as distinct."""
    state_file = tmp_path / "state.json"
    # State recorded after morning scan
    state_file.write_text(json.dumps({"x/Notes/1": "2026-06-29T09:00:00"}))
    monkeypatch.setattr(ns, "STATE_FILE", state_file, raising=False)

    # Afternoon modification — same date, different time
    fake_notes = [{"folder": "Work", "title": "Plan", "id": "x/Notes/1", "modified": "2026-06-29T15:30:00"}]
    with patch("notes_scanner._read_note_metadata", return_value=fake_notes), \
         patch("notes_scanner._read_note_body", return_value="updated body") as mock_body:
        scanner = NotesScanner(role="full")
        with patch.object(scanner, "_scanner_config", return_value={"enabled": True}):
            scanner._run_scan()
        mock_body.assert_called_once_with("x/Notes/1")


def test_write_memory_returns_true_on_success():
    """#147 — _write_memory returns True when the file is written successfully."""
    note = {"folder": "Work", "title": "Success", "id": "x/Notes/10", "modified": "2026-06-29T10:00:00"}
    result = _write_memory(note, "body text")
    assert result is True


def test_write_memory_returns_false_on_failure(tmp_path, monkeypatch):
    """#147 — _write_memory returns False when the atomic rename fails."""
    monkeypatch.setattr(ns, "MEMORIES_DIR", tmp_path / "memories", raising=False)
    note = {"folder": "Work", "title": "Fail", "id": "x/Notes/11", "modified": "2026-06-29T10:00:00"}
    with patch("os.rename", side_effect=OSError("disk full")):
        result = _write_memory(note, "body text")
    assert result is False


def test_run_scan_does_not_update_state_when_write_fails(tmp_path, monkeypatch):
    """#147 — state must not be updated when _write_memory returns False."""
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    monkeypatch.setattr(ns, "STATE_FILE", state_file, raising=False)

    fake_notes = [{"folder": "Work", "title": "Plan", "id": "x/Notes/1", "modified": "2026-05-05T10:00:00"}]
    with patch("notes_scanner._read_note_metadata", return_value=fake_notes), \
         patch("notes_scanner._read_note_body", return_value="body"), \
         patch("notes_scanner._write_memory", return_value=False):
        scanner = NotesScanner(role="full")
        with patch.object(scanner, "_scanner_config", return_value={"enabled": True}):
            scanner._run_scan()

    state = json.loads(state_file.read_text())
    assert "x/Notes/1" not in state, "State must not record a note whose file write failed"


def test_run_scan_skip_folders_substring_match(tmp_path, monkeypatch):
    """#148 — skip_folders uses substring matching, not exact-match."""
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    monkeypatch.setattr(ns, "STATE_FILE", state_file, raising=False)

    # "Work Archive" should be skipped by skip_folders: ["Archive"]
    fake_notes = [{"folder": "Work Archive", "title": "Old Note", "id": "x/Notes/99", "modified": "2024-01-01T00:00:00"}]
    with patch("notes_scanner._read_note_metadata", return_value=fake_notes), \
         patch("notes_scanner._read_note_body") as mock_body:
        scanner = NotesScanner(role="full")
        with patch.object(scanner, "_scanner_config", return_value={"enabled": True, "skip_folders": ["Archive"]}):
            scanner._run_scan()
        mock_body.assert_not_called()
