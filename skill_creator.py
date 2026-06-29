"""
skill_creator.py — Automatic skill file creation for new content types.

When the router detects a content type with no registered skill (a gap),
SkillCreator drafts a new skill file via LLM, then either requires operator
approval (HITL mode) or enters probation automatically.

Lifecycle: gap detected → seed generated → pending-approval|probation →
  shadow execution × N → graduation check → active|failed
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import yaml
from litellm import acompletion

from llm_routes import resolve
from usage_tracker import record_usage

if TYPE_CHECKING:
    from skill_optimizer import SkillOptimizer

log = logging.getLogger("skill-creator")

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
SKILLS_DIR = BRAIN_DIR / "skills"
DRAFTS_DIR = BRAIN_DIR / "skill-drafts"


def _get_registry_file():
    """Get registry file path (dynamic for testing)."""
    return DEPLOY_DIR / "skills-registry.json"


class SkillCreator:
    def __init__(self, config: dict):
        """
        config: the full daemon config dict (reads config['skill_creation'])
        """
        sc = config.get("skill_creation", {})
        self.enabled = sc.get("enabled", True)
        self.require_approval_config = sc.get("require_approval", False)
        self.probation_target = sc.get("probation_executions", 5)
        self.graduation_threshold = sc.get("graduation_utility_threshold", 0.6)
        self.model_route = sc.get("model_route", "chat")
        self.cooldown_hours = sc.get("rejection_cooldown_hours", 24)
        self.max_graduation_attempts = sc.get("max_graduation_attempts", 3)
        self._notification_callback = None  # set by daemon.py: async fn(msg: str)

    async def handle_gap(
        self,
        content_type: str,
        example_url: str,
        example_content: str
    ) -> Optional[str]:
        """
        Called when the router falls back to default for a non-default content type.

        Returns the skill_name if a new skill was created or is pending, None otherwise.
        """
        if not self.enabled:
            return None

        # Check cooldown
        if self._is_in_cooldown(content_type):
            log.debug(f"Gap suppressed (cooldown): {content_type}")
            return None

        # Check if skill already exists or is pending
        registry = self._load_registry()
        skill_name = f"summarize-{content_type}"

        # Check SKILL_REGISTRY for existing mapping
        import skill_router
        if content_type in skill_router.SKILL_REGISTRY:
            existing = skill_router.SKILL_REGISTRY[content_type]
            if existing != "summarize-webpage":  # not using default
                return existing

        # Check registry for pending/active/probation skills
        if skill_name in registry.get("skills", {}):
            entry = registry["skills"][skill_name]
            status = entry.get("status")
            if status not in ("failed", "rejected"):
                return skill_name if status == "pending-approval" else None

        log.info(f"New content type gap detected: {content_type} (example: {example_url[:60]})")

        # Generate seed
        seed_text = await self._generate_seed(content_type, example_url, example_content)
        if seed_text is None:
            return None

        # Determine approval mode
        require_approval = self.get_effective_approval_mode()

        if require_approval:
            # Write draft
            DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
            draft_path = DRAFTS_DIR / f"{skill_name}.md"
            self._atomic_write(draft_path, seed_text)

            # Write registry entry
            self._write_registry_entry(
                skill_name,
                content_type,
                "pending-approval",
                {
                    "draft_path": str(draft_path),
                    "example_url": example_url,
                }
            )

            # Send notification
            if self._notification_callback:
                msg = (
                    f"📝 New skill draft ready: {skill_name}\n"
                    f"Content type: {content_type}\n"
                    f"Example: {example_url[:80]}\n"
                    f"Use /skill-drafts to review."
                )
                await self._notification_callback(msg)

            log.info(f"Skill draft created: {skill_name} (awaiting approval)")
            return skill_name

        else:
            # Write to skills directory
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            skill_path = SKILLS_DIR / f"{skill_name}.md"
            self._atomic_write(skill_path, seed_text)

            # Write registry entry
            self._write_registry_entry(
                skill_name,
                content_type,
                "probation",
                {
                    "probation_count": 0,
                    "example_url": example_url,
                }
            )

            # Update SKILL_REGISTRY
            import skill_router
            skill_router.SKILL_REGISTRY[content_type] = skill_name

            log.info(f"New skill {skill_name} created, entering probation")

            # Send notification
            if self._notification_callback:
                msg = (
                    f"✨ New skill created: {skill_name} "
                    f"(probation, 0/{self.probation_target} runs)"
                )
                await self._notification_callback(msg)

            return skill_name

    async def _generate_seed(
        self,
        content_type: str,
        example_url: str,
        example_content: str
    ) -> Optional[str]:
        """Generate a new skill file from the template."""
        template_path = SKILLS_DIR / "summarize-webpage.md"
        if not template_path.exists():
            log.error(f"Template skill not found: {template_path}")
            return None

        try:
            template_text = template_path.read_text()
        except Exception as e:
            log.error(f"Failed to read template: {e}")
            return None

        skill_name = f"summarize-{content_type}"
        prompt = f"""You are creating a new LLM skill file for a personal knowledge system.

Base template (follow this structure exactly):
{template_text}

Task: Create a skill variant specialized for content of type "{content_type}".

Example URL: {example_url}
Example content snippet (first 500 chars):
{example_content[:500]}

Requirements:
- Keep the same frontmatter structure. Add `content_type: {content_type}` field after the last existing frontmatter field.
- Replace the Instructions section with type-specific extraction guidance (150-250 words).
- Keep the Variables section identical to the template (if present).
- Keep the Execution History table header identical.
- Do not fill in any Execution History rows.
- Set version: 1, total_runs: 0, success_rate: null
- Skill name in frontmatter: {skill_name}

Output ONLY the complete skill markdown file. No preamble, no explanation, no code fences."""

        max_retries = 2
        for attempt in range(max_retries):
            try:
                response = await acompletion(
                    model=resolve(self.model_route),
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1500
                )
                if hasattr(response, "usage") and response.usage:
                    record_usage(resolve(self.model_route), response.usage.prompt_tokens or 0, response.usage.completion_tokens or 0)
                result = response.choices[0].message.content

                # Strip code fences if present
                result = re.sub(r'^```(?:markdown|md)?\s*\n', '', result)
                result = re.sub(r'\n```\s*$', '', result)
                result = result.strip()

                # Validate
                if "## Instructions" in result and "## Execution History" in result:
                    return result

                log.warning(f"Generated seed missing required sections (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    # Retry with stricter prompt
                    prompt += "\n\nIMPORTANT: You MUST include both ## Instructions and ## Execution History sections."

            except Exception as e:
                log.error(f"Failed to generate seed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    return None

        log.error(f"Failed to generate valid seed after {max_retries} attempts")
        return None

    def _is_in_cooldown(self, content_type: str) -> bool:
        """Check if content_type is in rejection cooldown."""
        registry = self._load_registry()
        rejected_types = registry.get("rejected_types", {})

        if content_type not in rejected_types:
            return False

        try:
            rejected_at = datetime.fromisoformat(rejected_types[content_type])
            now = datetime.now()
            cooldown_delta = timedelta(hours=self.cooldown_hours)
            return (now - rejected_at) < cooldown_delta
        except Exception:
            return False

    def approve_draft(self, skill_name: str) -> bool:
        """
        Approve a pending draft. Called by chat_handler.

        Returns True if successful, False if draft not found.
        """
        draft_path = DRAFTS_DIR / f"{skill_name}.md"
        if not draft_path.exists():
            return False

        try:
            # Read draft content
            draft_content = draft_path.read_text()

            # Write to skills directory
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            skill_path = SKILLS_DIR / f"{skill_name}.md"
            self._atomic_write(skill_path, draft_content)

            # Update registry
            registry = self._load_registry()
            entry = registry.get("skills", {}).get(skill_name, {})
            content_type = entry.get("content_types", [None])[0]

            if content_type:
                self._write_registry_entry(
                    skill_name,
                    content_type,
                    "probation",
                    {
                        "probation_count": 0,
                        "draft_path": None,
                        "example_url": entry.get("example_url"),
                        "approved": datetime.now().isoformat(),
                    }
                )

                # Update SKILL_REGISTRY
                import skill_router
                skill_router.SKILL_REGISTRY[content_type] = skill_name

            # Delete draft file
            draft_path.unlink()

            log.info(f"Skill {skill_name} approved, entering probation")
            return True

        except Exception as e:
            log.error(f"Failed to approve draft {skill_name}: {e}")
            return False

    def reject_draft(self, skill_name: str) -> bool:
        """
        Reject a pending draft. Called by chat_handler.

        Returns True if successful, False if draft not found.
        """
        draft_path = DRAFTS_DIR / f"{skill_name}.md"
        if not draft_path.exists():
            return False

        try:
            # Get content_type from registry
            registry = self._load_registry()
            entry = registry.get("skills", {}).get(skill_name, {})
            content_type = entry.get("content_types", [None])[0]

            # Update rejected_types
            if content_type:
                if "rejected_types" not in registry:
                    registry["rejected_types"] = {}
                registry["rejected_types"][content_type] = datetime.now().isoformat()
                self._save_registry(registry)

            # Update skill entry
            self._write_registry_entry(
                skill_name,
                content_type,
                "rejected",
                {
                    "draft_path": None,
                    "rejected": datetime.now().isoformat(),
                }
            )

            # Delete draft file
            draft_path.unlink()

            log.info(f"Skill {skill_name} rejected, cooldown {self.cooldown_hours}h")
            return True

        except Exception as e:
            log.error(f"Failed to reject draft {skill_name}: {e}")
            return False

    def increment_probation(self, skill_name: str) -> dict:
        """
        Increment probation count after a shadow execution.
        Called by BrowserWatcher.

        Returns updated registry entry.
        """
        registry = self._load_registry()
        entry = registry.get("skills", {}).get(skill_name, {})

        if entry.get("status") != "probation":
            return entry

        entry["probation_count"] = entry.get("probation_count", 0) + 1
        self._save_registry(registry)

        log.debug(f"Probation {skill_name}: {entry['probation_count']}/{self.probation_target}")
        return entry

    def set_approval_override(self, value: Optional[bool]):
        """
        Set require_approval_runtime_override in registry.
        Called by /skill-approval command.
        """
        registry = self._load_registry()
        registry["require_approval_runtime_override"] = value
        self._save_registry(registry)
        log.info(f"Set approval override: {value}")

    def get_effective_approval_mode(self) -> bool:
        """
        Get effective approval mode: runtime override if set, else config.
        """
        registry = self._load_registry()
        override = registry.get("require_approval_runtime_override")
        if override is not None:
            return override
        return self.require_approval_config

    def list_pending_drafts(self) -> list[dict]:
        """
        List all pending drafts.

        Returns list of entries with: skill_name, content_types, created, draft_path.
        """
        registry = self._load_registry()
        pending = []

        for skill_name, entry in registry.get("skills", {}).items():
            if entry.get("status") == "pending-approval":
                pending.append({
                    "skill_name": skill_name,
                    "content_types": entry.get("content_types", []),
                    "created": entry.get("created"),
                    "draft_path": entry.get("draft_path"),
                    "example_url": entry.get("example_url"),
                })

        return pending

    async def run_probation_check(self, optimizer=None):
        """
        Check probation skills for graduation eligibility.
        Called after each nightly optimizer run.
        """
        registry = self._load_registry()
        skills = registry.get("skills", {})

        for skill_name, entry in skills.items():
            if entry.get("status") != "probation":
                continue

            probation_count = entry.get("probation_count", 0)
            if probation_count < self.probation_target:
                log.debug(f"Skill {skill_name} not ready for graduation ({probation_count}/{self.probation_target})")
                continue

            # Compute utility score from execution history
            utility_score = await self._compute_utility_score(skill_name)
            if utility_score is None:
                log.debug(f"Skill {skill_name} has no scored executions, skipping graduation")
                continue

            graduation_attempts = entry.get("graduation_attempts", 0)

            if utility_score >= self.graduation_threshold:
                # Graduate!
                await self._graduate_skill(skill_name, utility_score)
            else:
                # Failed graduation attempt
                graduation_attempts += 1
                entry["graduation_attempts"] = graduation_attempts

                if graduation_attempts >= self.max_graduation_attempts:
                    # Mark as failed
                    await self._fail_skill(skill_name, utility_score, graduation_attempts)
                else:
                    # Reset probation and try again
                    entry["probation_count"] = 0
                    self._save_registry(registry)

                    log.info(
                        f"Skill {skill_name} graduation failed "
                        f"(score: {utility_score:.2f}, attempt {graduation_attempts}/{self.max_graduation_attempts}), "
                        f"re-entering probation"
                    )

                    if self._notification_callback:
                        msg = (
                            f"⚠ Skill {skill_name} graduation failed "
                            f"(score: {utility_score:.2f}, attempt {graduation_attempts}/{self.max_graduation_attempts}), "
                            f"re-entering probation"
                        )
                        asyncio.create_task(self._notification_callback(msg))

    async def _compute_utility_score(self, skill_name: str) -> Optional[float]:
        """
        Compute mean score from execution history.

        Returns None if no scored rows exist.
        """
        skill_path = SKILLS_DIR / f"{skill_name}.md"
        if not skill_path.exists():
            return None

        try:
            text = skill_path.read_text()

            # Extract execution history section
            history_match = re.search(
                r'## Execution History\n(.*?)(?=\n## |\Z)',
                text,
                re.DOTALL
            )
            if not history_match:
                return None

            history_section = history_match.group(1)
            scores = []

            for line in history_section.splitlines():
                if not line.strip().startswith("|") or "| date |" in line or "|---" in line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 5:
                    continue
                score_str = parts[4]
                if score_str == "pending":
                    continue
                try:
                    score = float(score_str)
                    if score > 0:
                        scores.append(score)
                except ValueError:
                    continue

            if not scores:
                return None

            return sum(scores) / len(scores)

        except Exception as e:
            log.error(f"Failed to compute utility score for {skill_name}: {e}")
            return None

    async def _graduate_skill(self, skill_name: str, utility_score: float):
        """Mark skill as active (graduated)."""
        skill_path = SKILLS_DIR / f"{skill_name}.md"
        if not skill_path.exists():
            return

        try:
            # Update skill file frontmatter
            text = skill_path.read_text()
            parts = text.split("---", 2)
            if len(parts) < 3:
                return

            fm = yaml.safe_load(parts[1])
            fm["status"] = "active"
            parts[1] = yaml.dump(fm, sort_keys=False)
            updated_text = "---".join(parts)

            self._atomic_write(skill_path, updated_text)

            # Update registry
            registry = self._load_registry()
            entry = registry.get("skills", {}).get(skill_name, {})
            entry["status"] = "active"
            entry["graduated"] = datetime.now().isoformat()
            entry["graduation_score"] = round(utility_score, 2)
            self._save_registry(registry)

            log.info(f"Skill {skill_name} graduated (score: {utility_score:.2f})")

            if self._notification_callback:
                msg = f"✓ Skill graduated: {skill_name} (utility: {utility_score:.2f})"
                await self._notification_callback(msg)

        except Exception as e:
            log.error(f"Failed to graduate skill {skill_name}: {e}")

    async def _fail_skill(self, skill_name: str, utility_score: float, attempts: int):
        """Mark skill as failed after max graduation attempts."""
        skill_path = SKILLS_DIR / f"{skill_name}.md"
        if not skill_path.exists():
            return

        try:
            # Update skill file frontmatter
            text = skill_path.read_text()
            parts = text.split("---", 2)
            if len(parts) < 3:
                return

            fm = yaml.safe_load(parts[1])
            fm["status"] = "failed"
            parts[1] = yaml.dump(fm, sort_keys=False)
            updated_text = "---".join(parts)

            self._atomic_write(skill_path, updated_text)

            # Update registry
            registry = self._load_registry()
            entry = registry.get("skills", {}).get(skill_name, {})
            entry["status"] = "failed"
            entry["failed"] = datetime.now().isoformat()
            entry["final_score"] = round(utility_score, 2)
            self._save_registry(registry)

            log.info(f"Skill {skill_name} failed graduation after {attempts} attempts (score: {utility_score:.2f})")

            if self._notification_callback:
                msg = f"✗ Skill {skill_name} failed graduation after {attempts} attempts"
                await self._notification_callback(msg)

        except Exception as e:
            log.error(f"Failed to mark skill {skill_name} as failed: {e}")

    def _load_registry(self) -> dict:
        """Load registry from file. Returns empty dict if not found."""
        registry_file = _get_registry_file()
        if not registry_file.exists():
            return {
                "require_approval_runtime_override": None,
                "skills": {},
                "rejected_types": {},
            }

        try:
            return json.loads(registry_file.read_text())
        except Exception as e:
            log.warning(f"Failed to load registry: {e}")
            return {
                "require_approval_runtime_override": None,
                "skills": {},
                "rejected_types": {},
            }

    def _save_registry(self, registry: dict):
        """Save registry to file (atomic write)."""
        DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
        registry_file = _get_registry_file()
        content = json.dumps(registry, indent=2)
        self._atomic_write(registry_file, content)

    def _write_registry_entry(
        self,
        skill_name: str,
        content_type: str,
        status: str,
        extra_fields: Optional[dict] = None
    ):
        """Upsert registry entry for a skill."""
        registry = self._load_registry()

        if "skills" not in registry:
            registry["skills"] = {}

        entry = registry["skills"].get(skill_name, {})

        # Update core fields
        if "created" not in entry:
            entry["created"] = datetime.now().isoformat()

        entry["content_types"] = [content_type] if content_type else entry.get("content_types", [])
        entry["status"] = status
        entry["updated"] = datetime.now().isoformat()

        # Merge extra fields
        if extra_fields:
            for key, value in extra_fields.items():
                if value is not None:
                    entry[key] = value

        registry["skills"][skill_name] = entry
        self._save_registry(registry)

    def _atomic_write(self, path: Path, content: str):
        """Atomic file write using temp file + rename."""
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content)
        os.rename(tmp_path, path)
