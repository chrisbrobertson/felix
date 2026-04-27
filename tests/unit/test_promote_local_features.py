import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "promote_local_features.py"

spec = importlib.util.spec_from_file_location("promote_local_features", SCRIPT_PATH)
promote = importlib.util.module_from_spec(spec)
sys.modules["promote_local_features"] = promote
spec.loader.exec_module(promote)


def _make_feature_file(memories_dir: Path, name: str, fm: dict, body: str = "## Request\n\nDo a thing.\n") -> Path:
    memories_dir.mkdir(parents=True, exist_ok=True)
    f = memories_dir / name
    f.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n{body}")
    return f


def test_skips_files_already_imported(tmp_path, monkeypatch):
    memories = tmp_path / "memories"
    _make_feature_file(memories, "feature-request-already-aaa111.md", {
        "title": "Already in GH",
        "type": "feature_request",
        "kind": "feature",
        "status": "new",
        "priority": "medium",
        "github_issue_number": 42,
    })
    _make_feature_file(memories, "feature-request-skip-bbb222.md", {
        "title": "Wrong type",
        "type": "something_else",
    })
    pending = promote.collect_pending(memories)
    assert pending == []


def test_collects_only_pending_feature_requests(tmp_path):
    memories = tmp_path / "memories"
    pending_file = _make_feature_file(memories, "feature-request-new-ccc333.md", {
        "title": "New thing",
        "type": "feature_request",
        "kind": "bug",
        "status": "new",
        "priority": "high",
        "tags": ["telegram"],
    })
    _make_feature_file(memories, "feature-request-done-ddd444.md", {
        "title": "Already imported",
        "type": "feature_request",
        "github_issue_number": 7,
    })
    pending = promote.collect_pending(memories)
    assert len(pending) == 1
    assert pending[0][0] == pending_file
    assert pending[0][1]["kind"] == "bug"


def test_dry_run_makes_no_filesystem_or_gh_calls(tmp_path, monkeypatch, capsys):
    memories = tmp_path / "memories"
    f = _make_feature_file(memories, "feature-request-x-eee555.md", {
        "title": "X",
        "type": "feature_request",
        "kind": "feature",
        "status": "new",
        "priority": "medium",
    })
    monkeypatch.setattr(promote, "BRAIN_DIR", tmp_path)

    with patch.object(promote.subprocess, "run") as mock_run:
        rc = promote.main.__wrapped__() if hasattr(promote.main, "__wrapped__") else None

    # Run via argv instead so argparse picks up --dry-run
    monkeypatch.setattr(sys, "argv", ["promote_local_features.py", "--dry-run"])
    with patch.object(promote.subprocess, "run") as mock_run:
        rc = promote.main()

    assert rc == 0
    mock_run.assert_not_called()
    # File still in place, not archived
    assert f.exists()
    assert not (memories / "archive").exists()
    out = capsys.readouterr().out
    assert "1 file(s) to promote" in out


def test_successful_promote_stamps_frontmatter_and_archives(tmp_path, monkeypatch, capsys):
    memories = tmp_path / "memories"
    f = _make_feature_file(memories, "feature-request-go-fff666.md", {
        "title": "Go for it",
        "type": "feature_request",
        "kind": "feature",
        "status": "new",
        "priority": "medium",
        "tags": ["chat"],
    }, body="## Request\n\nShip it.\n")

    monkeypatch.setattr(promote, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["promote_local_features.py"])
    monkeypatch.setattr(promote, "gh_ensure_labels", lambda repo: None)

    fake_create = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="https://github.com/owner/repo/issues/123\n",
        stderr="",
    )
    with patch.object(promote.subprocess, "run", return_value=fake_create) as mock_run:
        rc = promote.main()

    assert rc == 0
    # File moved to archive
    assert not f.exists()
    archived = memories / "archive" / "feature-request-go-fff666.md"
    assert archived.exists()
    # Frontmatter stamped
    fm_text = archived.read_text().split("---", 2)[1]
    fm = yaml.safe_load(fm_text)
    assert fm["github_issue_number"] == 123
    # gh was invoked once with expected labels
    assert mock_run.call_count == 1
    call_args = mock_run.call_args[0][0]
    assert call_args[:3] == ["gh", "issue", "create"]
    assert "kind:feature" in call_args
    assert "priority:medium" in call_args
    assert "chat" in call_args


def test_done_status_closes_issue(tmp_path, monkeypatch):
    memories = tmp_path / "memories"
    _make_feature_file(memories, "feature-request-done-ggg777.md", {
        "title": "Already done",
        "type": "feature_request",
        "kind": "feature",
        "status": "done",
        "priority": "low",
    })
    monkeypatch.setattr(promote, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["promote_local_features.py"])
    monkeypatch.setattr(promote, "gh_ensure_labels", lambda repo: None)

    create_result = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="https://github.com/o/r/issues/55\n", stderr="",
    )
    close_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch.object(promote.subprocess, "run", side_effect=[create_result, close_result]) as mock_run:
        rc = promote.main()

    assert rc == 0
    assert mock_run.call_count == 2
    close_args = mock_run.call_args_list[1][0][0]
    assert close_args[:3] == ["gh", "issue", "close"]
    assert "55" in close_args
    assert "completed" in close_args


def test_gh_failure_returns_nonzero(tmp_path, monkeypatch, capsys):
    memories = tmp_path / "memories"
    _make_feature_file(memories, "feature-request-fail-hhh888.md", {
        "title": "Will fail",
        "type": "feature_request",
        "kind": "bug",
        "status": "new",
        "priority": "medium",
    })
    monkeypatch.setattr(promote, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["promote_local_features.py"])
    monkeypatch.setattr(promote, "gh_ensure_labels", lambda repo: None)

    err = subprocess.CalledProcessError(1, ["gh"], stderr="API rate limit")
    with patch.object(promote.subprocess, "run", side_effect=err):
        rc = promote.main()

    assert rc == 1


def test_gh_ensure_labels_calls_gh_for_each_standard_label(tmp_path, monkeypatch):
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(promote.subprocess, "run", return_value=ok) as mock_run:
        promote.gh_ensure_labels(repo=None)

    assert mock_run.call_count == len(promote.STANDARD_LABELS)
    first_args = mock_run.call_args_list[0][0][0]
    assert first_args[:3] == ["gh", "label", "create"]
    assert "--force" in first_args


def test_gh_ensure_labels_warns_on_failure_but_continues(tmp_path, capsys):
    fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="not found")
    with patch.object(promote.subprocess, "run", return_value=fail):
        promote.gh_ensure_labels(repo=None)  # must not raise

    err = capsys.readouterr().err
    assert "WARN" in err


def test_nothing_to_promote_returns_zero(tmp_path, monkeypatch, capsys):
    (tmp_path / "memories").mkdir()
    monkeypatch.setattr(promote, "BRAIN_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["promote_local_features.py"])
    rc = promote.main()
    assert rc == 0
    assert "Nothing to promote" in capsys.readouterr().out
