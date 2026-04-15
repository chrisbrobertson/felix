"""
Unit tests for code_scanner.CodeScanner.

All git subprocess calls are patched. All file I/O uses tmp_path.
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import code_scanner as cs
from code_scanner import CodeScanner, _parse_frontmatter


# ── helpers ──────────────────────────────────────────────────────────────────

def make_git_repo(base: Path, name: str, commits: int = 1) -> Path:
    """Create a minimal bare git repo structure for testing."""
    repo = base / name
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def write_memory(memories_dir: Path, name: str, head_sha: str = "abc123",
                 summary: str = "A project", tags=None, readme_mtime: str = ""):
    tags = tags or ["python"]
    content = (
        f"---\nsource_title: {name}\nsummary: {summary}\n"
        f"tags: {tags}\nlast_scanned: '2026-04-11T10:00:00'\n"
        f"source_url: git@github.com:org/{name}.git\ntype: project\ncategory: code\n"
        f"local_path: /tmp/{name}\ndefault_branch: main\n"
        f"languages: {tags}\nhead_sha: {head_sha}\n---\n\n"
        f"## Recent Activity\n- abc123 2026-04-11 initial\n"
    )
    (memories_dir / f"project-{name}.md").write_text(content)


# ── FR-1: Repository discovery ────────────────────────────────────────────────

def test_discover_repos_finds_git_dirs(tmp_path):
    repo_a = tmp_path / "alpha"
    repo_a.mkdir()
    (repo_a / ".git").mkdir()

    non_git = tmp_path / "not-a-repo"
    non_git.mkdir()

    scanner = CodeScanner()
    sc = {"repo_dirs": [str(tmp_path)], "skip_repos": []}
    result = scanner._discover_repos(sc)

    assert repo_a in result
    assert non_git not in result


def test_discover_repos_skips_configured(tmp_path):
    repo_a = tmp_path / "keep"
    repo_a.mkdir()
    (repo_a / ".git").mkdir()

    repo_b = tmp_path / "skip-me"
    repo_b.mkdir()
    (repo_b / ".git").mkdir()

    scanner = CodeScanner()
    sc = {"repo_dirs": [str(tmp_path)], "skip_repos": ["skip-me"]}
    result = scanner._discover_repos(sc)

    assert repo_a in result
    assert repo_b not in result


def test_discover_repos_ignores_nonexistent_dirs(tmp_path):
    scanner = CodeScanner()
    sc = {"repo_dirs": [str(tmp_path / "does-not-exist")], "skip_repos": []}
    result = scanner._discover_repos(sc)
    assert result == []


# ── FR-3: Language detection ──────────────────────────────────────────────────

def test_detect_languages_python(tmp_path):
    repo = tmp_path / "myproject"
    repo.mkdir()
    (repo / "main.py").write_text("# python")
    (repo / "utils.py").write_text("# python")

    scanner = CodeScanner()
    langs = scanner._detect_languages(repo)
    assert "python" in langs


def test_detect_languages_multi(tmp_path):
    repo = tmp_path / "mixed"
    repo.mkdir()
    (repo / "main.py").write_text("")
    (repo / "util.py").write_text("")
    (repo / "app.ts").write_text("")
    (repo / "server.go").write_text("")

    scanner = CodeScanner()
    langs = scanner._detect_languages(repo)
    assert len(langs) <= 3
    assert "python" in langs


def test_detect_languages_skips_venv(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "main.go").write_text("")
    venv = repo / "venv"
    venv.mkdir()
    for i in range(20):
        (venv / f"fake{i}.py").write_text("")  # would dominate if counted

    scanner = CodeScanner()
    langs = scanner._detect_languages(repo)
    # venv Python files must not appear; only main.go should register
    assert langs == ["go"]


def test_detect_languages_skips_dot_venv(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "main.go").write_text("")
    dotvenv = repo / ".venv"
    dotvenv.mkdir()
    for i in range(20):
        (dotvenv / f"fake{i}.py").write_text("")

    scanner = CodeScanner()
    langs = scanner._detect_languages(repo)
    assert langs == ["go"]


def test_detect_languages_recurses_into_src(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    for i in range(5):
        (src / f"mod{i}.ts").write_text("")

    scanner = CodeScanner()
    langs = scanner._detect_languages(repo)
    assert "typescript" in langs


def test_detect_languages_returns_top_3(tmp_path):
    repo = tmp_path / "polyglot"
    repo.mkdir()
    for i in range(5):
        (repo / f"a{i}.py").write_text("")
    for i in range(3):
        (repo / f"b{i}.ts").write_text("")
    for i in range(2):
        (repo / f"c{i}.go").write_text("")
    (repo / "extra.rb").write_text("")

    scanner = CodeScanner()
    langs = scanner._detect_languages(repo)
    assert len(langs) <= 3
    assert langs[0] == "python"


# ── FR-6: Change detection ────────────────────────────────────────────────────

def test_needs_update_true_when_no_memory(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    memory_path = memories_dir / "project-myrepo.md"

    scanner = CodeScanner()
    with patch.object(scanner, "_git", return_value="newsha123"):
        result = scanner._needs_update(repo, memory_path)
    assert result is True


def test_needs_update_false_when_sha_matches(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    write_memory(memories_dir, "myrepo", head_sha="abc123")
    memory_path = memories_dir / "project-myrepo.md"

    scanner = CodeScanner()
    with patch.object(scanner, "_git", return_value="abc123"):
        result = scanner._needs_update(repo, memory_path)
    assert result is False


def test_needs_update_true_when_sha_differs(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    write_memory(memories_dir, "myrepo", head_sha="old_sha")
    memory_path = memories_dir / "project-myrepo.md"

    scanner = CodeScanner()
    with patch.object(scanner, "_git", return_value="new_sha"):
        result = scanner._needs_update(repo, memory_path)
    assert result is True


# ── FR-7: Memory file write ───────────────────────────────────────────────────

def test_write_memory_field_order(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scanner = CodeScanner()
    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch("code_scanner._hostname", return_value="testhost"):
        scanner._write_memory({
            "name": "testproject",
            "local_path": "/Users/chris/repos/testproject",
            "remote_url": "git@github.com:org/testproject.git",
            "head_sha": "abc123def456",
            "default_branch": "main",
            "recent_commits": ["abc123 2026-04-11 initial commit"],
            "branches": ["main"],
            "languages": ["python"],
            "summary": "A test project.",
            "tags": ["python", "testing"],
            "related": [],
        })

    mem = memories_dir / "code-testhost-testproject.md"
    assert mem.exists()
    lines = mem.read_text().splitlines()
    # First line is "---", second is first frontmatter field
    assert lines[0] == "---"
    assert lines[1].startswith("source_title:")
    assert lines[2].startswith("summary:")
    assert lines[3].startswith("tags:")
    assert lines[4].startswith("last_scanned:")


def test_write_memory_atomic(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scanner = CodeScanner()
    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch("code_scanner._hostname", return_value="testhost"):
        scanner._write_memory({
            "name": "atomictest",
            "local_path": str(tmp_path),
            "remote_url": "git@github.com:org/atomictest.git",
            "head_sha": "sha1",
            "default_branch": "main",
            "recent_commits": [],
            "branches": [],
            "languages": ["go"],
            "summary": "Atomic test.",
            "tags": ["go"],
            "related": [],
        })

    # No leftover .tmp file
    tmp_files = list(memories_dir.glob("*.tmp"))
    assert tmp_files == []
    assert (memories_dir / "code-testhost-atomictest.md").exists()


def test_write_memory_type_is_project(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scanner = CodeScanner()
    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch("code_scanner._hostname", return_value="testhost"):
        scanner._write_memory({
            "name": "typecheck",
            "local_path": str(tmp_path),
            "remote_url": "git@github.com:org/typecheck.git",
            "head_sha": "sha999",
            "default_branch": "main",
            "recent_commits": [],
            "branches": [],
            "languages": ["rust"],
            "summary": "Type check project.",
            "tags": ["rust"],
            "related": [],
        })

    mem = memories_dir / "code-testhost-typecheck.md"
    text = mem.read_text()
    fm = _parse_frontmatter(text)
    assert fm["type"] == "code"
    assert "category" not in fm


def test_write_memory_frontmatter_parseable(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scanner = CodeScanner()
    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch("code_scanner._hostname", return_value="testhost"):
        scanner._write_memory({
            "name": "parsetest",
            "local_path": "/tmp/parsetest",
            "remote_url": "git@github.com:org/parsetest.git",
            "head_sha": "sha42",
            "default_branch": "main",
            "recent_commits": ["sha42 2026-04-11 initial"],
            "branches": ["main"],
            "languages": ["python", "typescript"],
            "summary": "Parse test project.",
            "tags": ["python", "typescript", "testing"],
            "related": [{"name": "other", "summary": "Another project."}],
        })

    mem = memories_dir / "code-testhost-parsetest.md"
    fm = _parse_frontmatter(mem.read_text())
    assert fm["source_title"] == "parsetest"
    assert fm["head_sha"] == "sha42"
    assert fm["type"] == "code"
    assert "category" not in fm
    assert "python" in fm["languages"]
    assert isinstance(fm["tags"], list)


# ── FR-5: Related project detection ──────────────────────────────────────────

def test_find_related_same_org(tmp_path):
    all_projects = [
        {
            "name": "alpha",
            "local_path": str(tmp_path / "alpha"),
            "remote_url": "git@github.com:myorg/alpha.git",
            "languages": ["python"],
            "recent_commits": ["abc 2026-04-11 init"],
        },
        {
            "name": "beta",
            "local_path": str(tmp_path / "beta"),
            "remote_url": "git@github.com:myorg/beta.git",
            "languages": ["python"],
            "recent_commits": [],
        },
        {
            "name": "gamma",
            "local_path": str(tmp_path / "gamma"),
            "remote_url": "git@github.com:otherog/gamma.git",
            "languages": ["go"],
            "recent_commits": [],
        },
    ]

    scanner = CodeScanner()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    with patch.object(cs, "MEMORIES_DIR", memories_dir):
        related = scanner._find_related(
            "alpha",
            ["python"],
            "git@github.com:myorg/alpha.git",
            all_projects,
        )

    names = [r["name"] for r in related]
    assert "beta" in names   # same org
    assert "gamma" not in names  # different org, no shared lang bonus enough


def test_find_related_shared_language(tmp_path):
    all_projects = [
        {
            "name": "proj-a",
            "local_path": str(tmp_path / "proj-a"),
            "remote_url": "git@github.com:org1/proj-a.git",
            "languages": ["typescript", "python"],
            "recent_commits": [],
        },
        {
            "name": "proj-b",
            "local_path": str(tmp_path / "proj-b"),
            "remote_url": "git@github.com:org2/proj-b.git",
            "languages": ["typescript"],
            "recent_commits": [],
        },
        {
            "name": "proj-c",
            "local_path": str(tmp_path / "proj-c"),
            "remote_url": "git@github.com:org3/proj-c.git",
            "languages": ["rust"],
            "recent_commits": [],
        },
    ]

    scanner = CodeScanner()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    with patch.object(cs, "MEMORIES_DIR", memories_dir):
        related = scanner._find_related(
            "proj-a",
            ["typescript", "python"],
            "git@github.com:org1/proj-a.git",
            all_projects,
        )

    names = [r["name"] for r in related]
    assert "proj-b" in names   # shared typescript
    assert "proj-c" not in names  # no shared language


# ── Subprocess helper ─────────────────────────────────────────────────────────

def test_git_helper_returns_empty_on_error(tmp_path):
    scanner = CodeScanner()
    # Point at a non-git path — git will fail
    result = scanner._git(tmp_path / "nonexistent", "rev-parse", "HEAD")
    assert result == ""


def test_git_helper_strips_whitespace(tmp_path):
    scanner = CodeScanner()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="  abc123\n", returncode=0)
        result = scanner._git(tmp_path, "rev-parse", "HEAD")
    assert result == "abc123"


# ── _parse_frontmatter helper ─────────────────────────────────────────────────

def test_parse_frontmatter_returns_dict():
    text = "---\nsource_title: foo\nhead_sha: abc123\n---\n\n## Body\n"
    fm = _parse_frontmatter(text)
    assert fm["source_title"] == "foo"
    assert fm["head_sha"] == "abc123"


def test_parse_frontmatter_returns_empty_on_no_delimiters():
    fm = _parse_frontmatter("no frontmatter here")
    assert fm == {}


# ── Backfill ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_deletes_and_recreates_memory_files(tmp_path):
    """backfill() deletes project-*.md files then runs scan."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Create some existing project files
    (memories_dir / "project-foo.md").write_text("old content 1")
    (memories_dir / "project-bar.md").write_text("old content 2")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "CONFIG_PATH", tmp_path / "config.yaml"):

        scanner = CodeScanner(role="full")
        mock_run_scan = AsyncMock()
        scanner._run_scan = mock_run_scan

        (tmp_path / "config.yaml").write_text("code_scanner:\n  repo_dirs: []\n")

        result = await scanner.backfill(0)  # days ignored

    # Original files should be deleted
    assert not (memories_dir / "project-foo.md").exists()
    assert not (memories_dir / "project-bar.md").exists()

    # Verify scan was called
    mock_run_scan.assert_called_once()
    assert result["notes"].startswith("Deleted")


# ── Hostname-scoped filenames ─────────────────────────────────────────────────

def test_hostname_scoped_filename_written(tmp_path):
    """Written memory files use project-{hostname}-{name}.md format."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scanner = CodeScanner()
    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch("code_scanner._hostname", return_value="testhost"):
        scanner._write_memory({
            "name": "myrepo",
            "local_path": str(tmp_path),
            "remote_url": "git@github.com:org/myrepo.git",
            "head_sha": "abc123",
            "default_branch": "main",
            "recent_commits": [],
            "branches": [],
            "languages": ["python"],
            "summary": "Test repo.",
            "tags": ["python"],
            "related": [],
        })

    # Should write project-testhost-myrepo.md
    expected = memories_dir / "code-testhost-myrepo.md"
    assert expected.exists()
    fm = _parse_frontmatter(expected.read_text())
    assert fm["hostname"] == "testhost"
    assert fm["source_title"] == "myrepo"


def test_migration_renames_legacy_file(tmp_path):
    """Migration renames project-{name}.md → code-{hostname}-{name}.md (all 3 migrations run)."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Create legacy file with matching hostname in frontmatter (type:project+category:code)
    legacy_file = memories_dir / "project-oldrepo.md"
    legacy_file.write_text(
        "---\nsource_title: oldrepo\nhostname: testhost\nhead_sha: abc\n"
        "type: project\ncategory: code\n---\n\nBody\n"
    )

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch("code_scanner._hostname", return_value="testhost"):
        scanner = CodeScanner(role="full")  # triggers all 3 migrations in __init__

    # Legacy file should be fully migrated to code-{hostname}-{name}.md with type:code
    assert not legacy_file.exists()
    new_file = memories_dir / "code-testhost-oldrepo.md"
    assert new_file.exists()
    fm = _parse_frontmatter(new_file.read_text())
    assert fm["source_title"] == "oldrepo"
    assert fm["type"] == "code"
    assert "category" not in fm


def test_migration_leaves_other_hosts_file(tmp_path):
    """Migration #2 doesn't rename files from other hosts, but migration #3 does convert project→code."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Create file from a different host (already hostname-scoped)
    other_file = memories_dir / "project-otherhost-otherrepo.md"
    other_file.write_text(
        "---\nsource_title: otherrepo\nhostname: otherhost\nhead_sha: xyz\n"
        "type: project\ncategory: code\n---\n\nBody\n"
    )

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch("code_scanner._hostname", return_value="testhost"):
        scanner = CodeScanner(role="full")

    # Original file should be gone, migrated to code-otherhost-otherrepo.md
    assert not other_file.exists()
    migrated = memories_dir / "code-otherhost-otherrepo.md"
    assert migrated.exists()
    fm = _parse_frontmatter(migrated.read_text())
    assert fm["type"] == "code"
    assert "category" not in fm


def test_migration_handles_already_migrated_files(tmp_path):
    """Migration skips files already fully migrated to code-{hostname}-{name}.md with type:code."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    already_migrated = memories_dir / "code-testhost-myrepo.md"
    already_migrated.write_text(
        "---\nsource_title: myrepo\nhostname: testhost\nhead_sha: abc\n"
        "type: code\n---\n\nBody\n"
    )

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch("code_scanner._hostname", return_value="testhost"):
        scanner = CodeScanner(role="full")

    # Should remain unchanged (already migrated)
    assert already_migrated.exists()
    fm = _parse_frontmatter(already_migrated.read_text())
    assert fm["source_title"] == "myrepo"
    assert fm["type"] == "code"
    assert "category" not in fm


@pytest.mark.asyncio
async def test_backfill_only_deletes_own_host_files(tmp_path):
    """backfill() only deletes project files for the current hostname."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Create files for different hosts
    (memories_dir / "project-testhost-a.md").write_text("---\nhostname: testhost\n---\n")
    (memories_dir / "project-otherhost-a.md").write_text("---\nhostname: otherhost\n---\n")

    with patch.object(cs, "MEMORIES_DIR", memories_dir), \
         patch.object(cs, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("code_scanner._hostname", return_value="testhost"):

        scanner = CodeScanner(role="full")
        mock_run_scan = AsyncMock()
        scanner._run_scan = mock_run_scan

        (tmp_path / "config.yaml").write_text("code_scanner:\n  repo_dirs: []\n")

        await scanner.backfill(0)

    # testhost file should be deleted
    assert not (memories_dir / "project-testhost-a.md").exists()
    # otherhost file should remain
    assert (memories_dir / "project-otherhost-a.md").exists()


# ── Migration: project → code ─────────────────────────────────────────────────

def test_migration_renames_file(tmp_path):
    """project-host-foo.md with type:project+category:code → code-host-foo.md"""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    old_file = memories_dir / "project-mymac-testrepo.md"
    fm = {"type": "project", "category": "code", "source_title": "testrepo", "hostname": "mymac"}
    old_file.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Notes\ntest\n")
    import code_scanner as cs
    with patch.object(cs, "MEMORIES_DIR", memories_dir):
        scanner = CodeScanner(role="full")
    new_file = memories_dir / "code-mymac-testrepo.md"
    assert new_file.exists()
    assert not old_file.exists()


def test_migration_updates_type_removes_category(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    old_file = memories_dir / "project-mymac-testrepo.md"
    fm = {"type": "project", "category": "code", "source_title": "testrepo", "hostname": "mymac"}
    old_file.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n## Notes\ntest\n")
    import code_scanner as cs
    with patch.object(cs, "MEMORIES_DIR", memories_dir):
        CodeScanner(role="full")
    new_file = memories_dir / "code-mymac-testrepo.md"
    content = yaml.safe_load(new_file.read_text().split("---")[1])
    assert content["type"] == "code"
    assert "category" not in content


def test_migration_idempotent(tmp_path):
    """Running migration twice on already-migrated files is a no-op (no errors)."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    # Already-migrated file (type:code, no category)
    already_migrated = memories_dir / "code-mymac-alreadydone.md"
    fm = {"type": "code", "source_title": "alreadydone", "hostname": "mymac"}
    already_migrated.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n")
    import code_scanner as cs
    with patch.object(cs, "MEMORIES_DIR", memories_dir):
        CodeScanner(role="full")
        CodeScanner(role="full")  # second run
    # File still there, still type:code
    assert already_migrated.exists()


def test_migration_skips_generic_projects(tmp_path):
    """Future project-work-*.md files (type:project, category:work) must not be touched."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    generic = memories_dir / "project-work-build-shed-abc123.md"
    fm = {"type": "project", "category": "work", "source_title": "Build a shed"}
    generic.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n")
    import code_scanner as cs
    with patch.object(cs, "MEMORIES_DIR", memories_dir):
        CodeScanner(role="full")
    assert generic.exists()  # untouched
    content = yaml.safe_load(generic.read_text().split("---")[1])
    assert content["type"] == "project"
    assert content["category"] == "work"


def test_migration_partial_recovery(tmp_path):
    """If new filename already exists when old also exists, just delete old."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    old_file = memories_dir / "project-mymac-testrepo.md"
    new_file = memories_dir / "code-mymac-testrepo.md"
    fm = {"type": "project", "category": "code", "source_title": "testrepo", "hostname": "mymac"}
    old_file.write_text(f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n")
    new_file.write_text("already exists\n")
    import code_scanner as cs
    with patch.object(cs, "MEMORIES_DIR", memories_dir):
        CodeScanner(role="full")
    assert not old_file.exists()  # old deleted
    assert new_file.read_text() == "already exists\n"  # new not overwritten
