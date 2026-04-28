import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import socket as _socket
import subprocess

from datetime import datetime
from pathlib import Path

import yaml

from llm_routes import resolve
from usage_tracker import record_usage
from skill_executor import SkillExecutor
from utils import load_config
from heartbeat import record_beat

log = logging.getLogger("code-scanner")


def _hostname() -> str:
    return _socket.gethostname().split(".")[0]

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))

# One-shot sentinels next to the deploy dir's state files. Once stamped, the
# corresponding migration is a no-op on subsequent scanner inits. Needed
# because the migrations' previous idempotency checks (filename-prefix
# startswith) are unstable on macOS where socket.gethostname() flips between
# values, causing the migration to re-run and stack hostnames on every flip.
HOSTNAME_MIGRATION_SENTINEL_NAME = ".code-hostname-prefix-v2.done"
PROJECT_TO_CODE_SENTINEL_NAME = ".code-project-to-code-v2.done"
LEGACY_CODE_PROJECT_SENTINEL_NAME = ".code-legacy-code-project-v2.done"


def _is_transient_oserror(e: OSError) -> bool:
    """True if ``e`` is an iCloud/FS transient error worth retrying next boot.

    macOS iCloud Drive can return ``EDEADLK`` (errno 11, "Resource deadlock
    avoided") when a file is an evicted placeholder and the materialization
    deadlocks against concurrent iCloud activity. ``EAGAIN`` is the same
    class. These are not code-level corruption — the correct response is to
    skip the file and try again next scanner boot.
    """
    return e.errno in (errno.EDEADLK, errno.EAGAIN)

EXTENSION_MAP = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".sh": "shell",
    ".bash": "shell",
    ".swift": "swift",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
}

SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "env", "__pycache__",
    ".tox", "dist", "build", ".build", ".next", "target", "vendor",
    ".gradle", ".idea", ".vscode", "coverage", ".coverage",
    "*.egg-info",
}


class CodeScanner:
    def __init__(self, role: str = "full"):
        self.role = role
        self._executor = None  # lazy — only created if LLM call needed
        self._migrate_legacy_code_project_files()
        self._migrate_project_filenames()
        self._migrate_project_to_code_files()
        # Load confirmation flag and rejected repos
        cfg = self._load_config()
        scanner_cfg = cfg.get("code_scanner", {})
        self.require_confirmation = scanner_cfg.get("require_confirmation", False)
        self.rejected_repos = self._load_rejected_repos()

    def _load_rejected_repos(self) -> set:
        """Load rejected repo local_paths from rejected-candidates.json."""
        rejected_file = DEPLOY_DIR / "rejected-candidates.json"
        if not rejected_file.exists():
            return set()
        try:
            data = json.loads(rejected_file.read_text())
            return set(e.get("local_path", "") for e in data.get("rejected_repos", []) if e.get("local_path"))
        except Exception:
            log.exception("Failed to load rejected-candidates.json")
            return set()

    def _migrate_legacy_code_project_files(self):
        """Rewrite any project-*.md with type: code_project → type: project + category: code.

        Sentinel-gated: once complete on a machine, this is a no-op. Only
        touches files where ``type: code_project`` is explicitly set — other
        types (project_candidate, goal, etc.) are skipped without any
        filesystem mutation beyond the read. iCloud EDEADLK on individual
        files is treated as transient: the file is skipped, and the sentinel
        is NOT stamped, so the next boot retries.
        """
        sentinel = DEPLOY_DIR / LEGACY_CODE_PROJECT_SENTINEL_NAME
        if sentinel.exists():
            return
        if not MEMORIES_DIR.exists():
            self._stamp_sentinel(sentinel)
            return
        migrated = 0
        transient = 0
        for path in MEMORIES_DIR.glob("project-*.md"):
            try:
                text = path.read_text()
                m = re.match(r"^(---\n)(.*?)(\n---\n)(.*)", text, re.DOTALL)
                if not m:
                    continue
                fm_text = m.group(2)
                fm = yaml.safe_load(fm_text) or {}
                if fm.get("type") != "code_project":
                    continue
                fm["type"] = "project"
                if "category" not in fm:
                    fm["category"] = "code"
                new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
                new_text = f"---\n{new_fm}---\n{m.group(4)}"
                tmp = path.with_suffix(".tmp")
                tmp.write_text(new_text)
                tmp.replace(path)
                migrated += 1
            except OSError as e:
                if _is_transient_oserror(e):
                    log.debug("Legacy code_project migration: transient OSError on %s (%s)", path.name, e)
                    transient += 1
                else:
                    log.exception("Legacy code_project migration failed for %s", path)
            except Exception:
                log.exception("Legacy code_project migration failed for %s", path)
        if migrated:
            log.info("Migrated %d legacy code_project files to type: project", migrated)
        if transient == 0:
            self._stamp_sentinel(sentinel)

    def _migrate_project_filenames(self):
        """One-shot migration of legacy unprefixed project-*.md → project-{hostname}-*.md.

        Only touches files that *belong to* code_scanner: those with
        ``type: project`` AND ``category: code``. Before v1.6.2 this was too
        greedy and re-prefixed any `project-*.md` file — including the
        `project-candidate-*.md` files owned by project_inference_scanner,
        which caused mass hostname stacking (474 candidate files mangled in
        April 2026).

        Sentinel-gated: once stamped, the migration is a no-op. The
        sentinel is the primary idempotency guard. The old hostname-prefix
        startswith() check was fragile because macOS's socket.gethostname()
        flips between values — stamped-once beats a per-file runtime check.

        iCloud EDEADLK is treated as transient (skip + don't stamp).
        """
        sentinel = DEPLOY_DIR / HOSTNAME_MIGRATION_SENTINEL_NAME
        if sentinel.exists():
            return
        if not MEMORIES_DIR.exists():
            self._stamp_sentinel(sentinel)
            return
        my_hostname = _hostname()
        migrated = 0
        transient = 0
        for path in MEMORIES_DIR.glob("project-*.md"):
            try:
                stem = path.stem
                rest = stem[len("project-"):]
                # Already hostname-scoped for this host? Skip without reading
                # frontmatter (fast path for canonical files).
                if rest.startswith(f"{my_hostname}-"):
                    continue

                # Read frontmatter to determine ownership.
                text = path.read_text()
                fm = _parse_frontmatter(text)
                file_type = fm.get("type")

                # STRICT ownership check: only code-project memories are
                # migrated here. Everything else — project_candidate, goal,
                # generic project with non-code category, already-migrated
                # type:code, etc. — is skipped entirely. This is what
                # prevents the project-candidate mangling regression.
                if file_type != "project":
                    continue
                if fm.get("category") != "code":
                    continue

                fm_hostname = fm.get("hostname", "")
                # Only migrate files whose frontmatter hostname matches us
                # (or is absent, meaning a legacy file that should belong
                # to us). Files for other hosts stay put.
                if fm_hostname and fm_hostname != my_hostname:
                    continue

                new_name = f"project-{my_hostname}-{rest}.md"
                new_path = path.parent / new_name
                if new_path.exists():
                    # Canonical already exists — drop the legacy duplicate.
                    path.unlink()
                    continue
                path.rename(new_path)
                migrated += 1
            except OSError as e:
                if _is_transient_oserror(e):
                    log.debug("Code filename migration: transient OSError on %s (%s)", path.name, e)
                    transient += 1
                else:
                    log.exception("Code filename migration failed for %s", path)
            except Exception:
                log.exception("Code filename migration failed for %s", path)
        if migrated:
            log.info("Migrated %d project files to hostname-scoped filenames", migrated)
        if transient == 0:
            self._stamp_sentinel(sentinel)

    def _migrate_project_to_code_files(self):
        """One-shot migration of project-{hostname}-*.md (type:project+category:code) → code-{hostname}-*.md (type:code).

        Only touches files with BOTH ``type: project`` AND ``category: code``.
        All other types (notably ``project_candidate``) are skipped after the
        frontmatter parse — no rename, no rewrite.

        Sentinel-gated. iCloud EDEADLK is treated as transient.
        """
        sentinel = DEPLOY_DIR / PROJECT_TO_CODE_SENTINEL_NAME
        if sentinel.exists():
            return
        if not MEMORIES_DIR.exists():
            self._stamp_sentinel(sentinel)
            return
        migrated = 0
        transient = 0
        for path in MEMORIES_DIR.glob("project-*.md"):
            try:
                text = path.read_text()
                m = re.match(r"^(---\n)(.*?)(\n---\n)(.*)", text, re.DOTALL)
                if not m:
                    continue
                fm_text = m.group(2)
                fm = yaml.safe_load(fm_text) or {}
                if fm.get("type") != "project":
                    continue
                if fm.get("category") != "code":
                    continue
                del fm["category"]
                fm["type"] = "code"
                stem = path.stem
                if not stem.startswith("project-"):
                    continue
                rest = stem[len("project-"):]
                new_path = path.parent / f"code-{rest}.md"
                if new_path.exists():
                    path.unlink()
                    continue
                new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
                new_text = f"---\n{new_fm}---\n{m.group(4)}"
                tmp = new_path.with_suffix(".tmp")
                tmp.write_text(new_text)
                tmp.replace(new_path)
                path.unlink()
                migrated += 1
            except OSError as e:
                if _is_transient_oserror(e):
                    log.debug("Code migration: transient OSError on %s (%s)", path.name, e)
                    transient += 1
                else:
                    log.exception("Code migration failed for %s", path)
            except Exception:
                log.exception("Code migration failed for %s", path)
        if migrated:
            log.info("Migrated %d project-{hostname}-*.md → code-{hostname}-*.md", migrated)
        if transient == 0:
            self._stamp_sentinel(sentinel)

    @staticmethod
    def _stamp_sentinel(path: Path) -> None:
        """Write an empty sentinel file, creating parent dirs as needed."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        except Exception:
            log.exception("Failed to write code-scanner migration sentinel %s", path)

    def _load_config(self) -> dict:
        return load_config(CONFIG_PATH)

    def _scanner_config(self) -> dict:
        cfg = self._load_config()
        return cfg.get("code_scanner", {})

    async def backfill(self, days: int) -> dict:
        """Delete all code memories and recreate from current state. days parameter ignored."""
        # Delete only this host's code-*.md files
        deleted = 0
        my_hostname = _hostname()
        if MEMORIES_DIR.exists():
            for path in MEMORIES_DIR.glob(f"code-{my_hostname}-*.md"):
                try:
                    path.unlink()
                    deleted += 1
                except Exception as e:
                    log.error(f"Failed to delete {path}: {e}")
            # Also delete legacy project-*.md files that belong to us (migration remnants)
            for path in MEMORIES_DIR.glob("project-*.md"):
                stem = path.stem
                rest = stem[len("project-"):]
                # Skip if already hostname-scoped (by any host)
                if "-" in rest and not rest.startswith(f"{my_hostname}-"):
                    continue
                # Legacy file (no hostname prefix) — assume it's ours if hostname field matches or is empty
                try:
                    text = path.read_text()
                    fm = _parse_frontmatter(text)
                    fm_hostname = fm.get("hostname", "")
                    if fm_hostname == my_hostname or not fm_hostname:
                        path.unlink()
                        deleted += 1
                except Exception as e:
                    log.error(f"Failed to delete {path}: {e}")

        # Run full scan
        await self._run_scan()

        # Count newly created files for this host
        created = 0
        if MEMORIES_DIR.exists():
            for path in MEMORIES_DIR.glob(f"code-{my_hostname}-*.md"):
                created += 1

        return {
            "processed": created,
            "skipped": 0,
            "errors": 0,
            "notes": f"Deleted {deleted} and recreated {created} code memories for {my_hostname}"
        }

    async def run_loop(self, stop_event: asyncio.Event):
        sc = self._scanner_config()
        interval = sc.get("interval_seconds", 300)
        log.info("Code scanner started — polling every %ds", interval)

        while not stop_event.is_set():
            beat_status, beat_error = "ok", None
            try:
                await self._run_scan()
            except Exception as exc:
                log.exception("Uncaught error in project scanner scan cycle")
                beat_status, beat_error = "error", str(exc)
            record_beat("code_scanner", beat_status, beat_error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _run_scan(self):
        sc = self._scanner_config()
        repos = self._discover_repos(sc)
        if not repos:
            log.debug("No repos discovered")
            return

        # First pass: collect basic metadata for all repos (for related-project detection)
        all_projects = []
        for repo_path in repos:
            try:
                p = self._scan_repo_basic(repo_path)
                if p:
                    all_projects.append(p)
            except Exception:
                log.exception("Error in basic scan of %s", repo_path)

        # Second pass: generate related projects, possibly call LLM, write memory files
        for project in all_projects:
            try:
                await self._finalize_and_write(project, all_projects)
            except Exception:
                log.exception("Error finalizing project %s", project.get("name"))

        log.info("Code scan complete — %d repos processed", len(all_projects))

    def _discover_repos(self, sc: dict) -> list:
        repo_dirs_raw = sc.get("repo_dirs", ["~/repos", "~/repo"])
        skip_repos = set(sc.get("skip_repos", []))
        repos = []
        for raw in repo_dirs_raw:
            base = Path(raw).expanduser()
            if not base.is_dir():
                continue
            for candidate in sorted(base.iterdir()):
                if not candidate.is_dir():
                    continue
                if candidate.name in skip_repos:
                    continue
                if (candidate / ".git").is_dir():
                    repos.append(candidate)
        return repos

    def _git(self, path: Path, *args) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(path)] + list(args),
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _scan_repo_basic(self, path: Path) -> dict:
        name = path.name
        remote_url = self._git(path, "remote", "get-url", "origin") or str(path)
        head_sha = self._git(path, "rev-parse", "HEAD")
        if not head_sha:
            # Repo with no commits — skip
            return {}

        # Default branch: try origin/HEAD first, fall back to current branch
        default_branch = self._git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if default_branch.startswith("origin/"):
            default_branch = default_branch[len("origin/"):]
        if not default_branch:
            default_branch = self._git(path, "branch", "--show-current") or "main"

        recent_commits_raw = self._git(path, "log", "-10", "--format=%h %ad %s", "--date=short")
        recent_commits = [line for line in recent_commits_raw.splitlines() if line.strip()]

        branches_raw = self._git(path, "branch", "--format=%(refname:short)")
        branches = [b for b in branches_raw.splitlines() if b.strip()]

        languages = self._detect_languages(path)

        readme_text = ""
        readme_mtime = None
        readme_path = path / "README.md"
        if readme_path.exists():
            try:
                readme_text = readme_path.read_text(encoding="utf-8", errors="replace")[:3000]
                readme_mtime = readme_path.stat().st_mtime
            except Exception:
                pass

        return {
            "name": name,
            "local_path": str(path),
            "remote_url": remote_url,
            "head_sha": head_sha,
            "default_branch": default_branch,
            "recent_commits": recent_commits,
            "branches": branches,
            "languages": languages,
            "readme_text": readme_text,
            "readme_mtime": readme_mtime,
        }

    def _detect_languages(self, path: Path, _depth: int = 0) -> list:
        if _depth > 4:
            return []
        counts: dict = {}
        try:
            entries = list(path.iterdir())
        except PermissionError:
            return []
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIRS or entry.name.startswith("."):
                    continue
                for lang, n in self._count_languages(entry, _depth + 1).items():
                    counts[lang] = counts.get(lang, 0) + n
            elif entry.is_file():
                lang = EXTENSION_MAP.get(entry.suffix.lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
        sorted_langs = sorted(counts, key=lambda l: counts[l], reverse=True)
        return sorted_langs[:3]

    def _count_languages(self, path: Path, depth: int) -> dict:
        if depth > 4:
            return {}
        counts: dict = {}
        try:
            entries = list(path.iterdir())
        except PermissionError:
            return {}
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIRS or entry.name.startswith("."):
                    continue
                for lang, n in self._count_languages(entry, depth + 1).items():
                    counts[lang] = counts.get(lang, 0) + n
            elif entry.is_file():
                lang = EXTENSION_MAP.get(entry.suffix.lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
        return counts

    def _find_related(self, name: str, languages: list, remote_url: str, all_projects: list) -> list:
        def org_from_url(url: str) -> str:
            # git@github.com:org/repo.git  or  https://github.com/org/repo
            m = re.search(r'[:/]([^/]+)/[^/]+$', url)
            return m.group(1) if m else ""

        my_org = org_from_url(remote_url)
        my_lang_set = set(languages)

        # Collect import references from dependency files
        import_refs: set = set()
        for dep_file in ("requirements.txt", "package.json", "go.mod", "pyproject.toml"):
            p = Path(all_projects[0]["local_path"]).parent / name / dep_file if all_projects else None
            fp = Path(all_projects[0]["local_path"]).parent.parent / name / dep_file if all_projects else None
            # Search in the actual repo path
            actual = None
            for proj in all_projects:
                if proj.get("name") == name:
                    actual = Path(proj["local_path"]) / dep_file
                    break
            if actual and actual.exists():
                try:
                    content = actual.read_text(errors="replace")
                    for other in all_projects:
                        other_name = other.get("name", "")
                        if other_name != name and other_name and other_name in content:
                            import_refs.add(other_name)
                except Exception:
                    pass

        scores: dict = {}
        for proj in all_projects:
            if proj.get("name") == name:
                continue
            other_name = proj.get("name", "")
            score = 0

            if my_org and org_from_url(proj.get("remote_url", "")) == my_org:
                score += 3
            if other_name in import_refs:
                score += 2
            shared_langs = my_lang_set & set(proj.get("languages", []))
            score += len(shared_langs)

            if score > 0:
                scores[other_name] = (score, proj)

        ranked = sorted(scores.values(), key=lambda x: x[0], reverse=True)[:5]
        result = []
        my_hostname = _hostname()
        for _score, proj in ranked:
            # Use summary from existing memory file if available
            # Try hostname-scoped filename first, then legacy
            mem = MEMORIES_DIR / f"code-{my_hostname}-{proj['name']}.md"
            if not mem.exists():
                mem = MEMORIES_DIR / f"project-{proj['name']}.md"
            summary = ""
            if mem.exists():
                try:
                    text = mem.read_text()
                    fm = _parse_frontmatter(text)
                    summary = fm.get("summary", "")
                except Exception:
                    pass
            if not summary and proj.get("recent_commits"):
                # Fallback: first commit message
                summary = proj["recent_commits"][0].split(" ", 2)[-1] if proj["recent_commits"] else ""
            result.append({"name": proj["name"], "summary": summary})
        return result

    def _needs_update(self, path: Path, memory_path: Path) -> bool:
        if not memory_path.exists():
            return True
        try:
            text = memory_path.read_text()
            fm = _parse_frontmatter(text)
            stored_sha = fm.get("head_sha", "")
            current_sha = self._git(path, "rev-parse", "HEAD")
            if stored_sha != current_sha:
                return True
            # Also trigger update if README changed since last scan
            readme_path = path / "README.md"
            if readme_path.exists():
                last_scanned_str = fm.get("last_scanned", "")
                if last_scanned_str:
                    try:
                        last_scanned = datetime.fromisoformat(str(last_scanned_str))
                        readme_mtime = datetime.fromtimestamp(readme_path.stat().st_mtime)
                        if readme_mtime > last_scanned:
                            return True
                    except Exception:
                        return True
            return False
        except Exception:
            return True

    async def _generate_summary_and_tags(self, readme_text: str, commits: list) -> tuple:
        if self._executor is None:
            self._executor = SkillExecutor("summarize-webpage", role=self.role)

        if readme_text:
            prompt_content = readme_text[:3000]
        elif commits:
            prompt_content = "\n".join(commits[:5])
        else:
            return "", []

        prompt = (
            "You are summarizing a software project for a personal knowledge base.\n\n"
            "Project content:\n"
            f"{prompt_content}\n\n"
            "Respond with EXACTLY this format (no other text):\n"
            "SUMMARY: <1-2 sentence description of what this project does>\n"
            "TAGS: <3-6 lowercase comma-separated tags, starting with language/framework, then domain>"
        )

        try:
            from litellm import acompletion
            resp = await acompletion(
                model=resolve("summarize"),
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
            )
            if hasattr(resp, "usage") and resp.usage:
                record_usage(resolve("summarize"), resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
            text = resp.choices[0].message.content.strip()
            summary = ""
            tags = []
            for line in text.splitlines():
                if line.startswith("SUMMARY:"):
                    summary = line[len("SUMMARY:"):].strip()
                elif line.startswith("TAGS:"):
                    raw_tags = line[len("TAGS:"):].strip()
                    tags = [t.strip().lower() for t in raw_tags.split(",") if t.strip()]
            return summary, tags
        except Exception:
            log.exception("LLM call failed for project summary generation")
            return "", []

    def _get_existing_summary_and_tags(self, memory_path: Path, readme_mtime) -> tuple:
        """Return (summary, tags) from existing memory if README hasn't changed."""
        if not memory_path.exists():
            return None, None
        try:
            text = memory_path.read_text()
            fm = _parse_frontmatter(text)
            last_scanned_str = fm.get("last_scanned", "")
            if not last_scanned_str:
                return None, None
            last_scanned = datetime.fromisoformat(str(last_scanned_str))
            if readme_mtime is not None:
                readme_dt = datetime.fromtimestamp(readme_mtime)
                if readme_dt > last_scanned:
                    return None, None  # README changed — regenerate
            summary = fm.get("summary", "")
            tags = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            return summary, tags
        except Exception:
            return None, None

    async def _finalize_and_write(self, project: dict, all_projects: list):
        if not project or not project.get("name"):
            return
        name = project["name"]
        local_path = project["local_path"]
        my_hostname = _hostname()
        memory_path = MEMORIES_DIR / f"code-{my_hostname}-{name}.md"
        repo_path = Path(local_path)

        # Skip if this repo's local_path is in the rejected list
        if local_path in self.rejected_repos:
            log.debug("Skipping %s — local_path in rejected list", name)
            return

        # With require_confirmation enabled, check if this is a new repo
        if self.require_confirmation and not memory_path.exists():
            # Check if a candidate already exists for this repo
            candidate_id = hashlib.sha1(f"code:{my_hostname}:{name}".encode()).hexdigest()[:6]
            slug = re.sub(r'[^a-z0-9]+', '-', name.lower())[:40]
            candidate_path = MEMORIES_DIR / f"project-candidate-{slug}-{candidate_id}.md"

            if candidate_path.exists():
                log.debug("Candidate already exists for %s — skipping", name)
                return

            # New repo discovery — write candidate instead of confirmed file
            log.info("Writing candidate for new repo %s", name)
            await self._write_candidate_for_new_repo(project)
            return

        if not self._needs_update(repo_path, memory_path):
            log.debug("Skipping %s — no changes", name)
            return

        log.info("Updating memory for %s", name)

        # Summary and tags: reuse from existing file if README hasn't changed
        summary, tags = self._get_existing_summary_and_tags(memory_path, project.get("readme_mtime"))
        if not summary or not tags:
            if project.get("readme_text") or project.get("recent_commits"):
                summary, tags = await self._generate_summary_and_tags(
                    project.get("readme_text", ""),
                    project.get("recent_commits", [])
                )
            # Fallback: use commit message as summary, languages as tags
            if not summary and project.get("recent_commits"):
                summary = project["recent_commits"][0].split(" ", 2)[-1] if project["recent_commits"] else ""
            if not tags:
                tags = list(project.get("languages", []))

        related = self._find_related(name, project["languages"], project["remote_url"], all_projects)

        data = {
            "name": name,
            "local_path": project["local_path"],
            "remote_url": project["remote_url"],
            "head_sha": project["head_sha"],
            "default_branch": project["default_branch"],
            "recent_commits": project["recent_commits"],
            "branches": project["branches"],
            "languages": project["languages"],
            "summary": summary,
            "tags": tags,
            "related": related,
        }
        self._write_memory(data)

    async def _write_candidate_for_new_repo(self, project: dict):
        """Write a project-candidate-*.md file for a newly discovered repo."""
        name = project["name"]
        local_path = project["local_path"]
        my_hostname = _hostname()

        # Generate summary if we have README or commits
        summary = ""
        if project.get("readme_text") or project.get("recent_commits"):
            summary, _ = await self._generate_summary_and_tags(
                project.get("readme_text", ""),
                project.get("recent_commits", [])
            )
        # Fallback: use first commit message as summary
        if not summary and project.get("recent_commits"):
            summary = project["recent_commits"][0].split(" ", 2)[-1] if project["recent_commits"] else ""
        if not summary:
            summary = f"Git repository at {local_path}"

        # Stable ID based on hostname and repo name
        candidate_id = hashlib.sha1(f"code:{my_hostname}:{name}".encode()).hexdigest()[:6]
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower())[:40]
        candidate_path = MEMORIES_DIR / f"project-candidate-{slug}-{candidate_id}.md"

        # Build frontmatter
        now = datetime.now().isoformat()
        fm = {
            "type": "project_candidate",
            "candidate_type": "code_repo",
            "category_guess": None,
            "source_title": f"{name} (code repository)",
            "summary": summary,
            "confidence": 1.0,
            "evidence": [f"code-discovery:{my_hostname}:{name}"],
            "extracted_fields": {
                "title": name,
                "local_path": local_path,
                "default_branch": project.get("default_branch", "main"),
                "languages": project.get("languages", []),
                "head_sha": project.get("head_sha", ""),
                "hostname": my_hostname,
            },
            "status": "pending_confirmation",
            "created": now,
        }
        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
        content = f"---\n{frontmatter}---\n"

        # Atomic write: tmp + rename
        tmp_path = candidate_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(candidate_path))
            log.info("Wrote candidate %s", candidate_path.name)
        except Exception:
            log.exception("Failed to write candidate %s", candidate_path)
            try:
                tmp_path.unlink()
            except Exception:
                pass

    def _write_memory(self, data: dict):
        name = data["name"]
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        my_hostname = _hostname()
        memory_path = MEMORIES_DIR / f"code-{my_hostname}-{name}.md"

        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = [tags]

        # Build frontmatter with explicit field order per spec
        fm = {
            "source_title": name,
            "summary": data.get("summary", ""),
            "tags": tags,
            "last_scanned": now,
            "source_url": data["remote_url"],
            "type": "code",
            "hostname": my_hostname,
            "local_path": data["local_path"],
            "default_branch": data["default_branch"],
            "languages": data.get("languages", []),
            "head_sha": data["head_sha"],
        }
        frontmatter = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)

        # Recent activity section
        activity_lines = []
        for commit in data.get("recent_commits", [])[:10]:
            activity_lines.append(f"- {commit}")
        activity_block = "\n".join(activity_lines) if activity_lines else "- (no commits)"

        # Related projects section
        related_lines = []
        for rel in data.get("related", []):
            rel_summary = rel.get("summary", "")
            if rel_summary:
                related_lines.append(f"- **{rel['name']}** — {rel_summary}")
            else:
                related_lines.append(f"- **{rel['name']}**")
        related_block = "\n".join(related_lines) if related_lines else "- (none detected)"

        # Active branches section
        branches = data.get("branches", [])
        default_branch = data.get("default_branch", "main")
        branch_lines = []
        for b in branches[:10]:
            marker = " (current)" if b == default_branch else ""
            branch_lines.append(f"- {b}{marker}")
        branches_block = "\n".join(branch_lines) if branch_lines else f"- {default_branch}"

        content = (
            f"---\n{frontmatter}---\n\n"
            f"## Recent Activity\n{activity_block}\n\n"
            f"## Related Projects\n{related_block}\n\n"
            f"## Active Branches\n{branches_block}\n"
        )

        # Atomic write: tmp + rename
        tmp_path = memory_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.rename(str(tmp_path), str(memory_path))
            log.debug("Wrote %s", memory_path.name)
        except Exception:
            log.exception("Failed to write %s", memory_path)
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from a markdown file."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}
