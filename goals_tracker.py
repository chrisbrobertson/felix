"""Goals and projects CRUD manager."""
import hashlib
import os
import re
from datetime import datetime
from pathlib import Path

import yaml

# ── Module-level constants ────────────────────────────────────────────────────
MEMORIES_DIR = Path(
    os.environ.get(
        "SECOND_BRAIN_DIR",
        Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
    )
) / "memories"


# ── Goal Manager Class ─────────────────────────────────────────────────────────
class GoalManager:
    """Pure CRUD manager for goal and project memory files.

    No daemon loop, no Telegram surface. Instantiated on demand by command
    handlers and LLM tools.
    """

    def __init__(self, memories_dir: Path, config: dict):
        self.memories_dir = memories_dir
        self.config = config

    # ── Category validation ────────────────────────────────────────────────────
    def _categories(self) -> list[str]:
        """Return the configured category list from config."""
        return self.config.get("goals", {}).get(
            "categories",
            ["personal", "work", "family", "learning", "other"]
        )

    def _validate_category(self, category: str) -> None:
        """Raise ValueError if category is not in configured list or is 'code'."""
        if category == "code":
            raise ValueError(
                f"Category 'code' is reserved for code repositories (type: code). "
                f"Use a goals category instead: {self._categories()}"
            )
        cats = self._categories()
        if category not in cats:
            raise ValueError(f"Invalid category '{category}'. Must be one of: {cats}")

    # ── ID and slug helpers ────────────────────────────────────────────────────
    def _stable_id(self, title: str, created_iso: str) -> str:
        """Generate a stable 6-character ID from title and creation timestamp."""
        return hashlib.sha1(f"{title}{created_iso}".encode()).hexdigest()[:6]

    def _slug(self, title: str, max_len: int = 40) -> str:
        """Generate a URL-friendly slug from title."""
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return slug[:max_len].rstrip("-")

    # ── Date validation ────────────────────────────────────────────────────────
    def _validate_due_date(self, due_date: str) -> None:
        """Raise ValueError if due_date is not in YYYY-MM-DD format."""
        if due_date is None:
            return
        # Check format YYYY-MM-DD
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", due_date):
            raise ValueError(
                f"Invalid due_date format '{due_date}'. Must be YYYY-MM-DD or null."
            )
        # Validate it's a real date
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"Invalid due_date '{due_date}': {e}")

    # ── Atomic write helper ────────────────────────────────────────────────────
    def _atomic_write(self, path: Path, frontmatter: dict, body: str = "") -> None:
        """Write frontmatter + body to path using atomic temp + rename pattern."""
        self.memories_dir.mkdir(parents=True, exist_ok=True)

        # Build file content
        fm_yaml = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False)
        content = f"---\n{fm_yaml}---\n\n{body}"

        # Atomic write: write to .tmp sibling, then rename
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content)
        os.rename(tmp_path, path)

    # ── Goal CRUD ──────────────────────────────────────────────────────────────
    def create_goal(
        self,
        title: str,
        category: str,
        due_date: str = None,
        priority: str = "medium",
        tags: list = None,
        notes: str = ""
    ) -> Path:
        """Create a new goal memory file. Returns path to created file.

        Stable ID dedup: if a file with the same ID already exists, returns
        existing path without overwriting.
        """
        # Validate inputs
        self._validate_category(category)
        self._validate_due_date(due_date)

        # Generate stable ID and filename
        created = datetime.utcnow().isoformat(timespec="seconds")
        stable_id = self._stable_id(title, created)
        slug = self._slug(title)
        filename = f"goal-{slug}-{stable_id}.md"
        path = self.memories_dir / filename

        # Dedup check: if file already exists, return existing path
        if path.exists():
            return path

        # Build frontmatter in exact field order
        fm = {
            "type": "goal",
            "category": category,
            "source_title": title,
            "summary": "",
            "tags": tags or [],
            "created": created,
            "due_date": due_date,
            "status": "active",
            "priority": priority,
            "linked_projects": [],
            "notes": notes,
        }

        # Write file
        body = f"## Notes\n{notes}\n" if notes else "## Notes\n"
        self._atomic_write(path, fm, body)

        return path

    def list_goals(self, category: str = None, status: str = None) -> list[Path]:
        """List goal files, optionally filtered by category and/or status.

        Sorted by due_date ascending (nulls last), then by created descending.
        """
        # Glob all goal files
        goals = list(self.memories_dir.glob("goal-*.md"))

        # Filter by frontmatter fields
        filtered = []
        for path in goals:
            try:
                with open(path) as f:
                    content = f.read()
                # Parse frontmatter
                match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
                if not match:
                    continue
                fm = yaml.safe_load(match.group(1))

                # Apply filters
                if category and fm.get("category") != category:
                    continue
                if status and fm.get("status") != status:
                    continue

                filtered.append((path, fm))
            except Exception:
                continue

        # Sort: due_date ascending (nulls last), then created descending
        def sort_key(item):
            path, fm = item
            due = fm.get("due_date")
            created = fm.get("created", "")
            # Nulls last: use a far-future date for nulls
            due_sort = due if due else "9999-12-31"
            return (due_sort, -ord(created[0]) if created else 0)

        filtered.sort(key=sort_key)

        return [path for path, _ in filtered]

    def update_goal_status(self, path: Path, new_status: str) -> None:
        """Update goal status. Valid transitions: active → completed|abandoned.

        Idempotent: if current status is same as new_status, returns without error.
        """
        valid_statuses = ["active", "completed", "abandoned"]
        if new_status not in valid_statuses:
            raise ValueError(f"Invalid status '{new_status}'. Must be one of: {valid_statuses}")

        # Read current frontmatter
        with open(path) as f:
            content = f.read()
        match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", content, re.DOTALL)
        if not match:
            raise ValueError(f"Invalid file format: {path}")

        fm = yaml.safe_load(match.group(1))
        body = match.group(2)
        current_status = fm.get("status")

        # Idempotent: if already at target status, return
        if current_status == new_status:
            return

        # Validate transition
        if current_status != "active" and new_status != current_status:
            raise ValueError(
                f"Invalid status transition: {current_status} → {new_status}. "
                f"Only active goals can be completed or abandoned."
            )

        # Update status and write back
        fm["status"] = new_status
        self._atomic_write(path, fm, body)

    # ── Project CRUD ───────────────────────────────────────────────────────────
    def create_project(
        self,
        title: str,
        category: str,
        due_date: str = None,
        linked_goal: str = None,
        tags: list = None,
        notes: str = "",
        inferred_from: list = None
    ) -> Path:
        """Create a new project memory file. Returns path to created file.

        Stable ID dedup: if a file with the same ID already exists, returns
        existing path without overwriting.
        """
        # Validate inputs
        self._validate_category(category)
        self._validate_due_date(due_date)

        # Generate stable ID and filename
        created = datetime.utcnow().isoformat(timespec="seconds")
        stable_id = self._stable_id(title, created)
        slug = self._slug(title)
        filename = f"project-{category}-{slug}-{stable_id}.md"
        path = self.memories_dir / filename

        # Dedup check
        if path.exists():
            return path

        # Build frontmatter in exact field order
        fm = {
            "type": "project",
            "category": category,
            "source_title": title,
            "summary": "",
            "tags": tags or [],
            "created": created,
            "due_date": due_date,
            "status": "active",
            "priority": "medium",
            "linked_goal": linked_goal,
            "milestones": [],
            "inferred_from": inferred_from or [],
            "notes": notes,
        }

        # Write file
        body = f"## Notes\n{notes}\n" if notes else "## Notes\n"
        self._atomic_write(path, fm, body)

        return path

    def list_projects(self, category: str = None, status: str = None) -> list[Path]:
        """List project files, optionally filtered by category and/or status.

        Sorted by due_date ascending (nulls last).
        Only includes files with type: project (excludes candidates).
        """
        # Glob project files (but not candidates)
        projects = list(self.memories_dir.glob("project-*.md"))

        # Filter by frontmatter
        filtered = []
        for path in projects:
            try:
                with open(path) as f:
                    content = f.read()
                match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
                if not match:
                    continue
                fm = yaml.safe_load(match.group(1))

                # Skip candidates
                if fm.get("type") != "project":
                    continue

                # Apply filters
                if category and fm.get("category") != category:
                    continue
                if status and fm.get("status") != status:
                    continue

                filtered.append((path, fm))
            except Exception:
                continue

        # Sort by due_date ascending (nulls last)
        def sort_key(item):
            path, fm = item
            due = fm.get("due_date")
            return due if due else "9999-12-31"

        filtered.sort(key=sort_key)

        return [path for path, _ in filtered]

    def update_project_status(self, path: Path, new_status: str) -> None:
        """Update project status.

        Valid statuses: active, completed, abandoned, on-hold.
        Valid transitions:
          - from active: any
          - from on-hold: active or abandoned
          - from completed/abandoned: nothing
        """
        valid_statuses = ["active", "completed", "abandoned", "on-hold"]
        if new_status not in valid_statuses:
            raise ValueError(f"Invalid status '{new_status}'. Must be one of: {valid_statuses}")

        # Read current frontmatter
        with open(path) as f:
            content = f.read()
        match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", content, re.DOTALL)
        if not match:
            raise ValueError(f"Invalid file format: {path}")

        fm = yaml.safe_load(match.group(1))
        body = match.group(2)
        current_status = fm.get("status")

        # Idempotent
        if current_status == new_status:
            return

        # Validate transition
        if current_status == "active":
            # Can go anywhere
            pass
        elif current_status == "on-hold":
            # Can only go to active or abandoned
            if new_status not in ["active", "abandoned"]:
                raise ValueError(
                    f"Invalid transition: on-hold → {new_status}. "
                    f"Can only transition to active or abandoned."
                )
        elif current_status in ["completed", "abandoned"]:
            # Cannot transition from terminal states
            raise ValueError(
                f"Invalid transition: {current_status} → {new_status}. "
                f"Completed/abandoned projects cannot be changed."
            )

        # Update and write back
        fm["status"] = new_status
        self._atomic_write(path, fm, body)

    # ── Milestone operations ───────────────────────────────────────────────────
    def add_milestone(self, project_path: Path, text: str) -> None:
        """Append a milestone to project's milestone list."""
        # Read current frontmatter
        with open(project_path) as f:
            content = f.read()
        match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", content, re.DOTALL)
        if not match:
            raise ValueError(f"Invalid file format: {project_path}")

        fm = yaml.safe_load(match.group(1))
        body = match.group(2)

        # Append milestone (truncate to 200 chars)
        milestones = fm.get("milestones", [])
        milestones.append({"text": text[:200], "done": False})
        fm["milestones"] = milestones

        # Write back
        self._atomic_write(project_path, fm, body)

    def toggle_milestone(self, project_path: Path, milestone_index: int) -> None:
        """Toggle the 'done' status of a milestone (1-based index)."""
        # Read current frontmatter
        with open(project_path) as f:
            content = f.read()
        match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", content, re.DOTALL)
        if not match:
            raise ValueError(f"Invalid file format: {project_path}")

        fm = yaml.safe_load(match.group(1))
        body = match.group(2)

        milestones = fm.get("milestones", [])

        # Validate index (1-based)
        if milestone_index < 1 or milestone_index > len(milestones):
            raise ValueError(
                f"Milestone index {milestone_index} out of range "
                f"(project has {len(milestones)} milestones)"
            )

        # Toggle done status (convert to 0-based)
        milestones[milestone_index - 1]["done"] = not milestones[milestone_index - 1]["done"]
        fm["milestones"] = milestones

        # Write back
        self._atomic_write(project_path, fm, body)

    # ── Goal↔Project linking ───────────────────────────────────────────────────
    def link_goal_to_project(self, project_path: Path, goal_path: Path) -> None:
        """Link a project to a goal. Updates both files atomically with rollback."""
        # Read both frontmatters
        with open(goal_path) as f:
            goal_content = f.read()
        goal_match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", goal_content, re.DOTALL)
        if not goal_match:
            raise ValueError(f"Invalid goal file format: {goal_path}")
        fm_goal = yaml.safe_load(goal_match.group(1))
        goal_body = goal_match.group(2)

        with open(project_path) as f:
            project_content = f.read()
        project_match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", project_content, re.DOTALL)
        if not project_match:
            raise ValueError(f"Invalid project file format: {project_path}")
        fm_project = yaml.safe_load(project_match.group(1))
        project_body = project_match.group(2)

        # Update both frontmatters
        fm_project["linked_goal"] = goal_path.name
        linked_projects = fm_goal.get("linked_projects", [])
        if project_path.name not in linked_projects:
            linked_projects.append(project_path.name)
        fm_goal["linked_projects"] = linked_projects

        # Write goal first
        try:
            self._atomic_write(goal_path, fm_goal, goal_body)
        except Exception as e:
            raise ValueError(f"Failed to write goal file: {e}")

        # Write project second (with rollback on failure)
        try:
            self._atomic_write(project_path, fm_project, project_body)
        except Exception as e:
            # Rollback: remove project from goal's linked_projects
            linked_projects.remove(project_path.name)
            fm_goal["linked_projects"] = linked_projects
            self._atomic_write(goal_path, fm_goal, goal_body)
            raise ValueError(f"Failed to write project file (rolled back goal): {e}")

    def unlink_goal_from_project(self, project_path: Path) -> None:
        """Unlink a project from its goal. Updates both files atomically."""
        # Read project frontmatter
        with open(project_path) as f:
            project_content = f.read()
        project_match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", project_content, re.DOTALL)
        if not project_match:
            raise ValueError(f"Invalid project file format: {project_path}")
        fm_project = yaml.safe_load(project_match.group(1))
        project_body = project_match.group(2)

        # Check if project has a linked goal
        linked_goal = fm_project.get("linked_goal")
        if not linked_goal:
            return  # No-op

        # Resolve goal path
        goal_path = self.memories_dir / linked_goal

        # Update goal file if it exists
        if goal_path.exists():
            with open(goal_path) as f:
                goal_content = f.read()
            goal_match = re.match(r"^---\n(.*?)\n---\n\n(.*)$", goal_content, re.DOTALL)
            if goal_match:
                fm_goal = yaml.safe_load(goal_match.group(1))
                goal_body = goal_match.group(2)

                # Remove project from linked_projects
                linked_projects = fm_goal.get("linked_projects", [])
                if project_path.name in linked_projects:
                    linked_projects.remove(project_path.name)
                fm_goal["linked_projects"] = linked_projects

                # Write goal
                self._atomic_write(goal_path, fm_goal, goal_body)

        # Clear linked_goal on project
        fm_project["linked_goal"] = None
        self._atomic_write(project_path, fm_project, project_body)

    # ── Candidate operations ───────────────────────────────────────────────────
    def confirm_candidate(self, candidate_path: Path, category_override: str = None) -> Path:
        """Promote a candidate to a real project or code entry.

        Returns the created file path, or None for code_repo candidates.
        """
        # Read candidate frontmatter
        with open(candidate_path) as f:
            content = f.read()
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            raise ValueError(f"Invalid candidate file format: {candidate_path}")
        fm = yaml.safe_load(match.group(1))

        candidate_type = fm.get("candidate_type")
        extracted = fm.get("extracted_fields", {})

        if candidate_type == "project":
            # Create a real project
            category = category_override or fm.get("category_guess", "other")
            self._validate_category(category)

            created_path = self.create_project(
                title=extracted.get("title", fm.get("source_title", "Untitled")),
                category=category,
                due_date=extracted.get("due_date"),
                tags=extracted.get("tags", []),
                notes=fm.get("summary", ""),
                inferred_from=fm.get("evidence", [])
            )

            # Delete candidate
            candidate_path.unlink()
            return created_path

        elif candidate_type == "code_repo":
            # Caller handles code repo write via CodeScanner helper
            # Just delete the candidate
            candidate_path.unlink()
            return None

        else:
            raise ValueError(f"Unknown candidate_type: {candidate_type}")

    def reject_candidate(self, candidate_path: Path, rejected_json_path: Path) -> None:
        """Reject a candidate and log it to rejected-candidates.json."""
        # Read candidate frontmatter
        with open(candidate_path) as f:
            content = f.read()
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            raise ValueError(f"Invalid candidate file format: {candidate_path}")
        fm = yaml.safe_load(match.group(1))

        # Load rejected list
        if rejected_json_path.exists():
            with open(rejected_json_path) as f:
                rejected_data = yaml.safe_load(f) or {}
        else:
            rejected_data = {"rejected": []}

        # Append entry
        rejected_data["rejected"].append({
            "source_title": fm.get("source_title", ""),
            "evidence": fm.get("evidence", []),
            "rejected_at": datetime.utcnow().isoformat(timespec="seconds"),
        })

        # Atomic write of rejected JSON
        tmp_path = rejected_json_path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            yaml.dump(rejected_data, f, sort_keys=False, allow_unicode=True)
        os.rename(tmp_path, rejected_json_path)

        # Delete candidate
        candidate_path.unlink()
