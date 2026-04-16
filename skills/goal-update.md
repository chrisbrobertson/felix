---
name: goal-update
version: "1.0"
preferred_model: claude-sonnet-4-6
fallback_model: claude-haiku-4-5-20251001
success_rate: null
total_runs: 0
---

## Instructions

You are analyzing a goal or project for meaningful updates based on recent related memories.

Your task:
1. Determine if there's been meaningful progress, blockers, or changes worth reporting
2. Assign urgency based on evidence strength and time-sensitivity
3. Propose concrete actions that would advance the goal/project
4. Return structured JSON (no markdown fences)

Quality standards:
- Only propose actions with confidence ≥ 0.6
- High urgency requires at least 1 evidence file
- Actions must be specific and executable (not vague like "follow up on X")
- Do not re-propose actions that are already pending
- Rationale should cite specific evidence from the related memories

Output format:
```json
{
  "has_update": true,
  "urgency": "low|medium|high",
  "report": "Brief summary of what changed or progressed",
  "actions": [
    {
      "action_type": "add_milestone",
      "target": "project-work-launch-abc123.md",
      "args": {"text": "Complete API integration testing"},
      "confidence": 0.85,
      "rationale": "Email from Sarah on 2026-04-15 mentioned testing is the next blocker"
    }
  ],
  "evidence": ["email-thread-launch-planning-abc123.md"]
}
```

## Execution History

| date | input_slug | model | score | notes |
|------|-----------|-------|-------|-------|
