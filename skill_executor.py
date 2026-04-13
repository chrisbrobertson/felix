import json
import logging
import os
import re
import yaml
from datetime import datetime
from pathlib import Path

from litellm import acompletion

log = logging.getLogger("skill-executor")

DEPLOY_DIR = Path(os.environ.get("SECOND_BRAIN_DIR", str(Path.home() / "secondbrain")))
ERROR_LOG = DEPLOY_DIR / "errors.log"

BRAIN_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain"
SKILLS_DIR = BRAIN_DIR / "skills"


class SkillExecutor:
    def __init__(self, skill_name: str, role: str = "full"):
        self.skill_name = skill_name
        self.role = role  # "full" or "watcher"
        self.skill_path = SKILLS_DIR / f"{skill_name}.md"
        self._skill = self._load()
        self._mtime = self.skill_path.stat().st_mtime if self.skill_path.exists() else 0

    def _load(self) -> dict:
        text = self.skill_path.read_text()
        parts = text.split("---", 2)
        meta = yaml.safe_load(parts[1])
        body = parts[2]
        instructions_match = re.search(
            r'## Instructions\n(.*?)(?=\n## |\Z)', body, re.DOTALL)
        instructions = instructions_match.group(1).strip() if instructions_match else ""
        return {"meta": meta, "instructions": instructions, "raw": text}

    def _reload_if_modified(self):
        """FR-14: Reload skill if file has been modified (optimizer rewrote it)."""
        if not self.skill_path.exists():
            return
        current_mtime = self.skill_path.stat().st_mtime
        if current_mtime > self._mtime:
            log.info(f"Reloading {self.skill_name} (file modified)")
            self._skill = self._load()
            self._mtime = current_mtime

    async def run(self, inputs: dict, score=None):
        # FR-14: Reload skill if optimizer has rewritten it
        self._reload_if_modified()

        meta = self._skill["meta"]
        preferred = meta.get("preferred_model", "gemini/gemini-2.0-flash")
        fallback = meta.get("fallback_model")  # may be None
        user_msg = "\n".join(f"**{k}:**\n{v}" for k, v in inputs.items())
        messages = [
            {"role": "system", "content": self._skill["instructions"]},
            {"role": "user", "content": user_msg},
        ]

        models_to_try = [preferred, fallback] if fallback else [preferred]
        last_err = None
        for model in models_to_try:
            try:
                response = await acompletion(model=model, messages=messages, max_tokens=1000)
                result = response.choices[0].message.content
                await self._log_execution(inputs, model, score=score)
                if model != preferred:
                    log.warning(f"{self.skill_name} succeeded on fallback {model} "
                                f"(preferred {preferred} failed: {last_err})")
                return result
            except Exception as e:
                last_err = e
                log.warning(f"{self.skill_name} failed on {model}: {e}")
                continue

        # Every attempt failed
        err_msg = f"{datetime.now().isoformat()} [{self.skill_name}] {preferred} ERROR: {last_err}\n"
        log.error(err_msg.strip())
        with open(ERROR_LOG, "a") as f:
            f.write(err_msg)
        await self._log_execution(inputs, preferred, score=0.0, notes=str(last_err)[:80])
        return None

    async def _log_execution(self, inputs: dict, model: str,
                              score, notes: str = ""):
        slug = list(inputs.values())[0][:20].replace(" ", "-").lower() \
               if inputs else "unknown"
        date = datetime.now().strftime("%Y-%m-%d")
        score_str = f"{score:.2f}" if score is not None else "pending"

        if self.role == "watcher":
            # Watcher nodes must not write to the shared iCloud skill file.
            # Two machines appending simultaneously produces iCloud conflict copies.
            # Log to iCloud logs dir — optimizer will merge on daily pass.
            hostname = __import__("socket").gethostname()
            logs_dir = BRAIN_DIR / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            watcher_log = logs_dir / f"{hostname}-execution-log.jsonl"

            record = {
                "date": date, "skill": self.skill_name, "input_slug": slug,
                "model": model, "score": score_str, "notes": notes,
                "hostname": hostname
            }
            with open(watcher_log, "a") as f:
                f.write(json.dumps(record) + "\n")
            return

        # Full node: write to the skill file in iCloud as before
        row = f"| {date} | {slug} | {model} | {score_str} | {notes} |\n"
        text = self.skill_path.read_text()

        if "## Execution History" not in text:
            text += (f"\n## Execution History\n\n"
                     f"| date | input_slug | model | score | notes |\n"
                     f"|------|-----------|-------|-------|-------|\n{row}")
        else:
            lines = text.splitlines(keepends=True)
            insert_at = len(lines)
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip().startswith("|"):
                    insert_at = i + 1
                    break
            lines.insert(insert_at, row)
            text = "".join(lines)

        self.skill_path.write_text(text)
