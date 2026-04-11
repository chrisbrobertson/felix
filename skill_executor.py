import json
import logging
import re
import yaml
from datetime import datetime
from pathlib import Path

from litellm import acompletion

log = logging.getLogger("skill-executor")

ERROR_LOG = Path.home() / ".second-brain-errors.log"
# Watcher nodes write execution history here instead of the iCloud skill file.
# The leader's optimizer can optionally ingest this on its daily pass (v0.2+).
LOCAL_EXEC_LOG = Path.home() / ".second-brain-execution-log.jsonl"

SKILLS_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/second-brain/skills"


class SkillExecutor:
    def __init__(self, skill_name: str, role: str = "full"):
        self.skill_name = skill_name
        self.role = role  # "full" or "watcher"
        self.skill_path = SKILLS_DIR / f"{skill_name}.md"
        self._skill = self._load()

    def _load(self) -> dict:
        text = self.skill_path.read_text()
        parts = text.split("---", 2)
        meta = yaml.safe_load(parts[1])
        body = parts[2]
        instructions_match = re.search(
            r'## Instructions\n(.*?)(?=\n## |\Z)', body, re.DOTALL)
        instructions = instructions_match.group(1).strip() if instructions_match else ""
        return {"meta": meta, "instructions": instructions, "raw": text}

    async def run(self, inputs: dict, score: float | None = None) -> str | None:
        meta = self._skill["meta"]
        model = meta.get("preferred_model", "gemini/gemini-2.0-flash")
        user_msg = "\n".join(f"**{k}:**\n{v}" for k, v in inputs.items())

        try:
            response = await acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": self._skill["instructions"]},
                    {"role": "user", "content": user_msg}
                ],
                max_tokens=1000
            )
            result = response.choices[0].message.content
            await self._log_execution(inputs, model, score=score)
            return result

        except Exception as e:
            err_msg = f"{datetime.now().isoformat()} [{self.skill_name}] {model} ERROR: {e}\n"
            log.error(err_msg.strip())
            with open(ERROR_LOG, "a") as f:
                f.write(err_msg)
            await self._log_execution(inputs, model, score=0.0, notes=str(e)[:80])
            return None

    async def _log_execution(self, inputs: dict, model: str,
                              score: float | None, notes: str = ""):
        slug = list(inputs.values())[0][:20].replace(" ", "-").lower() \
               if inputs else "unknown"
        date = datetime.now().strftime("%Y-%m-%d")
        score_str = f"{score:.2f}" if score is not None else "pending"

        if self.role == "watcher":
            # Watcher nodes must not write to the shared iCloud skill file.
            # Two machines appending simultaneously produces iCloud conflict copies.
            # Log locally — optimizer can merge these in v0.2+.
            record = {
                "date": date, "skill": self.skill_name, "input_slug": slug,
                "model": model, "score": score_str, "notes": notes,
                "hostname": __import__("socket").gethostname()
            }
            with open(LOCAL_EXEC_LOG, "a") as f:
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
