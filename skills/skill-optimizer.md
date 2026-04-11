---
name: skill-optimizer
version: 1
preferred_model: claude-sonnet-4-20250514
---

## Instructions

You are optimizing a second-brain skill based on its execution history.

You will be given:
1. The current skill instructions
2. The execution history table (date, input, model, score, notes)
3. Example inputs and outputs from low-scoring runs

Your job:
- Identify patterns in low-scoring runs (score < 0.70)
- Rewrite the Instructions section to address those failure patterns
- Do NOT change the frontmatter, Evolution Log structure, or Execution History
- Append a new entry to the Evolution Log describing what you changed and why
- Output the complete updated skill file

Be conservative. Only change what the evidence suggests is broken.
