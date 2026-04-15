"""
Unit tests for project_scanner.ProjectScanner.

All git subprocess calls are patched. All file I/O uses tmp_path.
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import project_scanner as ps
from project_scanner import ProjectScanner, _parse_frontmatter


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

    scanner = ProjectScanner()
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

    scanner = ProjectScanner()
    sc = {"repo_dirs": [str(tmp_path)], "skip_repos": ["skip-me"]}
    result = scanner._discover_repos(sc)

    assert repo_a in result
    assert repo_b not in result


def test_discover_repos_ignores_nonexistent_dirs(tmp_path):
    scanner = ProjectScanner()
    sc = {"repo_dirs": [str(tmp_path / "does-not-exist")], "skip_repos": []}
    result = scanner._discover_repos(sc)
    assert result == []


# ── FR-3: Language detection ──────────────────────────────────────────────────

def test_detect_languages_python(tmp_path):
    repo = tmp_path / "myproject"
    repo.mkdir()
    (repo / "main.py").write_text("# python")
    (repo / "utils.py").write_text("# python")

    scanner = ProjectScanner()
    langs = scanner._detect_languages(repo)
    assert "python" in langs


def test_detect_languages_multi(tmp_path):
    repo = tmp_path / "mixed"
    repo.mkdir()
    (repo / "main.py").write_text("")
    (repo / "util.py").write_text("")
    (repo / "app.ts").write_text("")
    (repo / "server.go").write_text("")

    scanner = ProjectScanner()
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

    scanner = ProjectScanner()
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

    scanner = ProjectScanner()
    langs = scanner._detect_languages(repo)
    assert langs == ["go"]


def test_detect_languages_recurses_into_src(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    for i in range(5):
        (src / f"mod{i}.ts").write_text("")

    scanner = ProjectScanner()
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

    scanner = ProjectScanner()
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

    scanner = ProjectScanner()
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

    scanner = ProjectScanner()
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

    scanner = ProjectScanner()
    with patch.object(scanner, "_git", return_value="new_sha"):
        result = scanner._needs_update(repo, memory_path)
    assert result is True


# ── FR-7: Memory file write ───────────────────────────────────────────────────

def test_write_memory_field_order(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scanner = ProjectScanner()
    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
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

    mem = memories_dir / "project-testhost-testproject.md"
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

    scanner = ProjectScanner()
    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
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
    assert (memories_dir / "project-testhost-atomictest.md").exists()


def test_write_memory_type_is_project(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scanner = ProjectScanner()
    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
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

    mem = memories_dir / "project-testhost-typecheck.md"
    text = mem.read_text()
    fm = _parse_frontmatter(text)
    assert fm["type"] == "project"
    assert fm["category"] == "code"


def test_write_memory_frontmatter_parseable(tmp_path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scanner = ProjectScanner()
    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
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

    mem = memories_dir / "project-testhost-parsetest.md"
    fm = _parse_frontmatter(mem.read_text())
    assert fm["source_title"] == "parsetest"
    assert fm["head_sha"] == "sha42"
    assert fm["type"] == "project"
    assert fm["category"] == "code"
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

    scanner = ProjectScanner()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    with patch.object(ps, "MEMORIES_DIR", memories_dir):
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

    scanner = ProjectScanner()
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    with patch.object(ps, "MEMORIES_DIR", memories_dir):
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
    scanner = ProjectScanner()
    # Point at a non-git path — git will fail
    result = scanner._git(tmp_path / "nonexistent", "rev-parse", "HEAD")
    assert result == ""


def test_git_helper_strips_whitespace(tmp_path):
    scanner = ProjectScanner()
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

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch.object(ps, "CONFIG_PATH", tmp_path / "config.yaml"):

        scanner = ProjectScanner(role="full")
        mock_run_scan = AsyncMock()
        scanner._run_scan = mock_run_scan

        (tmp_path / "config.yaml").write_text("project_scanner:\n  repo_dirs: []\n")

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

    scanner = ProjectScanner()
    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
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
    expected = memories_dir / "project-testhost-myrepo.md"
    assert expected.exists()
    fm = _parse_frontmatter(expected.read_text())
    assert fm["hostname"] == "testhost"
    assert fm["source_title"] == "myrepo"


def test_migration_renames_legacy_file(tmp_path):
    """Migration renames project-{name}.md → project-{hostname}-{name}.md if hostname matches."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Create legacy file with matching hostname in frontmatter
    legacy_file = memories_dir / "project-oldrepo.md"
    legacy_file.write_text(
        "---\nsource_title: oldrepo\nhostname: testhost\nhead_sha: abc\n"
        "type: project\ncategory: code\n---\n\nBody\n"
    )

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
        scanner = ProjectScanner(role="full")  # triggers migration in __init__

    # Legacy file should be renamed
    assert not legacy_file.exists()
    new_file = memories_dir / "project-testhost-oldrepo.md"
    assert new_file.exists()
    fm = _parse_frontmatter(new_file.read_text())
    assert fm["source_title"] == "oldrepo"


def test_migration_leaves_other_hosts_file(tmp_path):
    """Migration does not rename files with a different hostname."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Create file with different hostname
    other_file = memories_dir / "project-otherrepo.md"
    other_file.write_text(
        "---\nsource_title: otherrepo\nhostname: otherhost\nhead_sha: xyz\n"
        "type: project\ncategory: code\n---\n\nBody\n"
    )

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
        scanner = ProjectScanner(role="full")

    # File should remain unchanged
    assert other_file.exists()
    assert not (memories_dir / "project-testhost-otherrepo.md").exists()


def test_migration_handles_already_scoped_files(tmp_path):
    """Migration skips files already in project-{hostname}-{name}.md format."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    scoped_file = memories_dir / "project-testhost-myrepo.md"
    scoped_file.write_text(
        "---\nsource_title: myrepo\nhostname: testhost\nhead_sha: abc\n"
        "type: project\ncategory: code\n---\n\nBody\n"
    )

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch("project_scanner._hostname", return_value="testhost"):
        scanner = ProjectScanner(role="full")

    # Should remain unchanged
    assert scoped_file.exists()
    fm = _parse_frontmatter(scoped_file.read_text())
    assert fm["source_title"] == "myrepo"


@pytest.mark.asyncio
async def test_backfill_only_deletes_own_host_files(tmp_path):
    """backfill() only deletes project files for the current hostname."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()

    # Create files for different hosts
    (memories_dir / "project-testhost-a.md").write_text("---\nhostname: testhost\n---\n")
    (memories_dir / "project-otherhost-a.md").write_text("---\nhostname: otherhost\n---\n")

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch.object(ps, "CONFIG_PATH", tmp_path / "config.yaml"), \
         patch("project_scanner._hostname", return_value="testhost"):

        scanner = ProjectScanner(role="full")
        mock_run_scan = AsyncMock()
        scanner._run_scan = mock_run_scan

        (tmp_path / "config.yaml").write_text("project_scanner:\n  repo_dirs: []\n")

        await scanner.backfill(0)

    # testhost file should be deleted
    assert not (memories_dir / "project-testhost-a.md").exists()
    # otherhost file should remain
    assert (memories_dir / "project-otherhost-a.md").exists()


# ── FR-10: Confirmation gate for new repos ───────────────────────────────────

@pytest.mark.asyncio
async def test_require_confirmation_false_writes_directly(tmp_path):
    """With require_confirmation=false (default), new repo → project-*.md written directly."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    repo_dir = tmp_path / "repos"
    repo_dir.mkdir()
    repo = make_git_repo(repo_dir, "newrepo")
    (repo / "main.py").write_text("# python code")

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "project_scanner:\n"
        "  interval_seconds: 300\n"
        f"  repo_dirs: ['{str(repo_dir)}']\n"
        "  skip_repos: []\n"
        "  require_confirmation: false\n"
    )

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch.object(ps, "CONFIG_PATH", config_file), \
         patch.object(ps, "DEPLOY_DIR", deploy_dir), \
         patch("project_scanner._hostname", return_value="testhost"), \
         patch.object(ProjectScanner, "_git") as mock_git:

        mock_git.side_effect = lambda path, *args: {
            ("remote", "get-url", "origin"): "git@github.com:org/newrepo.git",
            ("rev-parse", "HEAD"): "abc123",
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/main",
            ("log", "-10", "--format=%h %ad %s", "--date=short"): "abc123 2026-04-15 initial",
            ("branch", "--format=%(refname:short)"): "main",
        }.get(args, "")

        scanner = ProjectScanner(role="full")
        await scanner._run_scan()

    # Should write project-testhost-newrepo.md directly
    expected_file = memories_dir / "project-testhost-newrepo.md"
    assert expected_file.exists()

    # No candidate file should exist
    candidate_files = list(memories_dir.glob("project-candidate-*.md"))
    assert len(candidate_files) == 0


@pytest.mark.asyncio
async def test_require_confirmation_true_writes_candidate(tmp_path):
    """With require_confirmation=true, new repo → project-candidate-*.md written."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    repo_dir = tmp_path / "repos"
    repo_dir.mkdir()
    repo = make_git_repo(repo_dir, "newrepo")
    (repo / "main.py").write_text("# python code")
    (repo / "README.md").write_text("A new Python project")

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "project_scanner:\n"
        "  interval_seconds: 300\n"
        f"  repo_dirs: ['{str(repo_dir)}']\n"
        "  skip_repos: []\n"
        "  require_confirmation: true\n"
    )

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch.object(ps, "CONFIG_PATH", config_file), \
         patch.object(ps, "DEPLOY_DIR", deploy_dir), \
         patch("project_scanner._hostname", return_value="testhost"), \
         patch.object(ProjectScanner, "_git") as mock_git, \
         patch("litellm.acompletion") as mock_llm:

        mock_git.side_effect = lambda path, *args: {
            ("remote", "get-url", "origin"): "git@github.com:org/newrepo.git",
            ("rev-parse", "HEAD"): "abc123",
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/main",
            ("log", "-10", "--format=%h %ad %s", "--date=short"): "abc123 2026-04-15 initial",
            ("branch", "--format=%(refname:short)"): "main",
        }.get(args, "")

        # Mock LLM response for summary generation
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="SUMMARY: A new Python project\nTAGS: python, project"))]
        mock_llm.return_value = mock_resp

        scanner = ProjectScanner(role="full")
        await scanner._run_scan()

    # Should NOT write project-testhost-newrepo.md
    confirmed_file = memories_dir / "project-testhost-newrepo.md"
    assert not confirmed_file.exists()

    # Should write project-candidate-*.md
    candidate_files = list(memories_dir.glob("project-candidate-*.md"))
    assert len(candidate_files) == 1

    # Verify candidate frontmatter
    candidate = candidate_files[0]
    fm = _parse_frontmatter(candidate.read_text())
    assert fm["type"] == "project_candidate"
    assert fm["candidate_type"] == "code_repo"
    assert fm["status"] == "pending_confirmation"
    assert "newrepo" in fm["source_title"]
    assert fm["extracted_fields"]["hostname"] == "testhost"
    assert fm["extracted_fields"]["local_path"] == str(repo)


@pytest.mark.asyncio
async def test_known_repo_rescan_updates_inplace_with_flag_on(tmp_path):
    """With flag on, already-confirmed repo (project-*.md exists) → auto-update, no new candidate."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    repo_dir = tmp_path / "repos"
    repo_dir.mkdir()
    repo = make_git_repo(repo_dir, "existingrepo")
    (repo / "main.go").write_text("// go code")

    # Create existing confirmed memory file with old SHA
    existing_file = memories_dir / "project-testhost-existingrepo.md"
    existing_file.write_text(
        "---\n"
        "source_title: existingrepo\n"
        "summary: Existing Go project\n"
        "tags: [go]\n"
        "last_scanned: '2026-04-14T10:00:00'\n"
        "source_url: git@github.com:org/existingrepo.git\n"
        "type: project\n"
        "category: code\n"
        "hostname: testhost\n"
        f"local_path: {str(repo)}\n"
        "default_branch: main\n"
        "languages: [go]\n"
        "head_sha: old_sha_123\n"
        "---\n\n"
        "## Recent Activity\n- old commit\n"
    )

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "project_scanner:\n"
        "  interval_seconds: 300\n"
        f"  repo_dirs: ['{str(repo_dir)}']\n"
        "  skip_repos: []\n"
        "  require_confirmation: true\n"
    )

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch.object(ps, "CONFIG_PATH", config_file), \
         patch.object(ps, "DEPLOY_DIR", deploy_dir), \
         patch("project_scanner._hostname", return_value="testhost"), \
         patch.object(ProjectScanner, "_git") as mock_git:

        mock_git.side_effect = lambda path, *args: {
            ("remote", "get-url", "origin"): "git@github.com:org/existingrepo.git",
            ("rev-parse", "HEAD"): "new_sha_456",
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/main",
            ("log", "-10", "--format=%h %ad %s", "--date=short"): "new456 2026-04-15 updated",
            ("branch", "--format=%(refname:short)"): "main",
        }.get(args, "")

        scanner = ProjectScanner(role="full")
        await scanner._run_scan()

    # Should update existing confirmed file in-place
    assert existing_file.exists()
    fm = _parse_frontmatter(existing_file.read_text())
    assert fm["head_sha"] == "new_sha_456"

    # No candidate file should be created
    candidate_files = list(memories_dir.glob("project-candidate-*.md"))
    assert len(candidate_files) == 0


@pytest.mark.asyncio
async def test_rejected_local_path_skipped(tmp_path):
    """Repos whose local_path is in rejected-candidates.json are skipped."""
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    repo_dir = tmp_path / "repos"
    repo_dir.mkdir()
    repo = make_git_repo(repo_dir, "rejectedrepo")
    (repo / "main.rs").write_text("// rust code")

    # Create rejected-candidates.json with this repo's local_path
    rejected_file = deploy_dir / "rejected-candidates.json"
    rejected_file.write_text(
        '{"rejected_repos": [{"local_path": "' + str(repo) + '", "reason": "test rejection"}]}'
    )

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "project_scanner:\n"
        "  interval_seconds: 300\n"
        f"  repo_dirs: ['{str(repo_dir)}']\n"
        "  skip_repos: []\n"
        "  require_confirmation: true\n"
    )

    with patch.object(ps, "MEMORIES_DIR", memories_dir), \
         patch.object(ps, "CONFIG_PATH", config_file), \
         patch.object(ps, "DEPLOY_DIR", deploy_dir), \
         patch("project_scanner._hostname", return_value="testhost"), \
         patch.object(ProjectScanner, "_git") as mock_git:

        mock_git.side_effect = lambda path, *args: {
            ("remote", "get-url", "origin"): "git@github.com:org/rejectedrepo.git",
            ("rev-parse", "HEAD"): "abc123",
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/main",
            ("log", "-10", "--format=%h %ad %s", "--date=short"): "abc123 2026-04-15 initial",
            ("branch", "--format=%(refname:short)"): "main",
        }.get(args, "")

        scanner = ProjectScanner(role="full")
        await scanner._run_scan()

    # Nothing should be written for this repo
    confirmed_files = list(memories_dir.glob("project-testhost-*.md"))
    candidate_files = list(memories_dir.glob("project-candidate-*.md"))
    assert len(confirmed_files) == 0
    assert len(candidate_files) == 0
