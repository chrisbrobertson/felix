---
name: skill-optimizer
version: 2
preferred_model: claude-sonnet-4-20250514
---

## Instructions

You are rewriting a second-brain skill's Instructions section based on a structured critique.

**Input you will receive:**
1. The current complete skill file (frontmatter + all sections)
2. A critique JSON object with these fields:
   - `failure_patterns`: list of specific failure modes identified in low-scoring runs
   - `root_cause`: one-sentence summary of the core issue
   - `suggested_focus`: what the rewrite should specifically address

**Your task:**
1. Read the Evolution Log to understand prior optimization attempts and what has been tried before (OPRO trajectory pattern)
2. Read the current Instructions section carefully
3. Rewrite ONLY the Instructions section to address the failure patterns in the critique
4. Output the complete updated skill file with these changes:
   - Updated Instructions section (addressing the critique)
   - Incremented `version` in frontmatter by 1
   - New Evolution Log entry appended at the end of that section (see format below)
   - ALL other sections unchanged (Top Examples, Execution History, etc.)

**Evolution Log entry format:**
```markdown
### v{new_version} ({YYYY-MM-DD}) — {one-line description of the change}
**Critique:** {root_cause from critique, max 100 chars}
**Failure patterns:** {comma-joined failure_patterns list}
**Change:** {one sentence describing what you changed in the Instructions}
**Pre-optimization avg:** {will be filled by optimizer} | **Post (projected):** pending
```

**Critical constraints:**
- Be conservative: only change what the evidence directly supports
- If the critique identifies two patterns, fix both — but don't invent problems
- Preserve the skill's core structure and voice
- Do NOT modify frontmatter fields other than `version`
- Do NOT modify Execution History or Top Examples sections
- If the current Instructions already address the failure patterns well, make minimal changes

**Output format:**
Complete skill file in markdown, starting with frontmatter YAML block (`---`).

## Evolution Log

### v2 (2026-04-11) — rewrite for Critique-Edit architecture
**Change:** Replaced single-shot rewrite prompt with two-call protocol (critique then edit)
**Reason:** TextGrad pattern improves optimization quality by separating diagnosis from solution

### v1 (2026-04-11) — initial version
