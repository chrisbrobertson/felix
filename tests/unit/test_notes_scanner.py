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
    assert _has_todos("Notes", "Shopping", "- milk\n- eggs")
    assert _has_todos("Notes", "Work", "[ ] Fix the bug")


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
