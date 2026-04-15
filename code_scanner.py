import asyncio
import logging
import os
import re
import socket as _socket
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from llm_routes import resolve
from skill_executor import SkillExecutor

log = logging.getLogger("code-scanner")


def _hostname() -> str:
    return _socket.gethostname().split(".")[0]

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
MEMORIES_DIR = BRAIN_DIR / "memories"
CONFIG_PATH = BRAIN_DIR / "config.yaml"

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

    def _migrate_legacy_code_project_files(self):
        """Rewrite any project-*.md with type: code_project → type: project + category: code."""
        if not MEMORIES_DIR.exists():
            return
        migrated = 0
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
                # Insert category after type; preserve other fields
                if "category" not in fm:
                    fm["category"] = "code"
                new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
                new_text = f"---\n{new_fm}---\n{m.group(4)}"
                # Atomic write
                tmp = path.with_suffix(".tmp")
                tmp.write_text(new_text)
                tmp.replace(path)
                migrated += 1
            except Exception:
                log.exception("Migration failed for %s", path)
        if migrated:
            log.info("Migrated %d legacy code_project files to type: project", migrated)

    def _migrate_project_filenames(self):
        """Migrate project-*.md to project-{hostname}-*.md for this node's files."""
        if not MEMORIES_DIR.exists():
            return
        my_hostname = _hostname()
        migrated = 0
        for path in MEMORIES_DIR.glob("project-*.md"):
            try:
                stem = path.stem
                rest = stem[len("project-"):]
                # Already hostname-scoped for this host?
                if rest.startswith(f"{my_hostname}-"):
                    continue
                # Check if this file belongs to us by reading frontmatter
                text = path.read_text()
                fm = _parse_frontmatter(text)
                # Skip non-code projects (future generic project types)
                if fm.get("type") == "project" and fm.get("category") != "code":
                    continue
                # Also skip if type is already "code" (already migrated by migration #3)
                if fm.get("type") == "code":
                    continue
                fm_hostname = fm.get("hostname", "")
                # If frontmatter hostname matches ours, this file belongs to us — rename it
                if fm_hostname == my_hostname or not fm_hostname:
                    # Empty hostname means legacy file — assume it's ours
                    new_name = f"project-{my_hostname}-{rest}.md"
                    new_path = path.parent / new_name
                    path.rename(new_path)
                    migrated += 1
                # If hostname is different, leave it (other node will handle)
            except (OSError, FileNotFoundError):
                pass
            except Exception:
                log.exception("Filename migration failed for %s", path)
        if migrated:
            log.info("Migrated %d project files to hostname-scoped filenames", migrated)

    def _migrate_project_to_code_files(self):
        """Migrate project-{hostname}-*.md with type:project+category:code → code-{hostname}-*.md with type:code."""
        if not MEMORIES_DIR.exists():
            return
        migrated = 0
        for path in MEMORIES_DIR.glob("project-*.md"):
            try:
                text = path.read_text()
                m = re.match(r"^(---\n)(.*?)(\n---\n)(.*)", text, re.DOTALL)
                if not m:
                    continue
                fm_text = m.group(2)
                fm = yaml.safe_load(fm_text) or {}
                # Skip if not type:project+category:code
                if fm.get("type") != "project":
                    continue
                if fm.get("category") != "code":
                    continue
                # Update frontmatter: remove category, change type to code
                del fm["category"]
                fm["type"] = "code"
                # Compute new filename: replace project-{hostname}- with code-{hostname}-
                stem = path.stem
                if stem.startswith("project-"):
                    rest = stem[len("project-"):]
                    new_stem = f"code-{rest}"
                    new_path = path.parent / f"{new_stem}.md"
                else:
                    # Should not happen but be defensive
                    continue
                # If new filename already exists, just delete old (partial run recovery)
                if new_path.exists():
                    path.unlink()
                    continue
                # Atomic write to new path
                new_fm = yaml.dump(fm, default_flow_style=None, allow_unicode=True, sort_keys=False)
                new_text = f"---\n{new_fm}---\n{m.group(4)}"
                tmp = new_path.with_suffix(".tmp")
                tmp.write_text(new_text)
                tmp.replace(new_path)
                # Delete old file
                path.unlink()
                migrated += 1
            except Exception:
                log.exception("Code migration failed for %s", path)
        if migrated:
            log.info("Migrated %d project-{hostname}-*.md → code-{hostname}-*.md", migrated)

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return {}

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
            try:
                await self._run_scan()
            except Exception:
                log.exception("Uncaught error in project scanner scan cycle")
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
        my_hostname = _hostname()
        memory_path = MEMORIES_DIR / f"code-{my_hostname}-{name}.md"
        repo_path = Path(project["local_path"])

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
