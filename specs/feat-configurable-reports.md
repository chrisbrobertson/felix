---
specmas: 3.0
kind: feature
id: feat-configurable-reports
version: 1.0.0
created: 2026-04-12
status: draft
complexity: moderate
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-proactive-notifications
  - feat-commitment-tracker
  - feat-calendar-scanner
  - feat-memory-management
---

# Configurable Reports

## Overview

### Problem Statement

The daily morning briefing from `notification_manager.py` is hardcoded: it always runs at
the configured time, it always pulls the same data sources (calendar, commitments, new
memories), and its format is fixed. Users cannot schedule custom recurring reports that
pull different subsets of memory data or use LLM analysis to synthesize narrative
insights. A user who wants a weekly digest of all meetings, or a daily standup brief with
LLM analysis, or a Friday summary of new contacts, has no way to configure these without
editing Python code.

The Configurable Reports system lets users define custom recurring reports with
configurable schedules, data sources, and output formats. Each report can be a simple
structured digest (fast, no LLM call) or an LLM-analyzed narrative synthesis. Reports can
be defined in `config.yaml` for permanent use or added at runtime via Telegram commands,
and they persist across daemon restarts.

### Scope

**In Scope:**
- Thirteenth async daemon loop: `ReportScheduler.run_loop()` (`full` role only)
- Two report types: `digest` (structured data pull) and `analysis` (LLM synthesis)
- Schedule syntax supporting daily, specific days of week, weekday/weekend, and multi-day
- Config-file report definitions in `config.yaml`
- Runtime report management via Telegram: `/reports`, `/report <N>`, `/report-add`,
  `/report-remove`, `/report-pause`, `/report-resume`, `/report-run`
- Report state persistence in `DEPLOY_DIR/reports-state.json`
- Delivery via Telegram only
- Chunked output respecting Telegram's 4096-character limit

**Out of Scope:**
- Email or Slack delivery (Telegram only for v1)
- Parameterized reports (no dynamic date ranges from user at query time)
- Report history or archive (no storing past report output)
- Multiple concurrent deliveries (single Telegram `chat_id` only)
- Web UI for report configuration
- Report templates or presets
- Per-report timezone configuration (uses `user.timezone` globally)

### Success Metrics

- Reports sent within 2 minutes of scheduled time
- Digest reports assembled in <1 second (no LLM call)
- Analysis reports complete in <10 seconds (single LLM call)
- Config-file reports load correctly on daemon startup
- Runtime-added reports persist across daemon restarts
- Paused reports remain paused across restarts
- `/report-run` triggers report immediately without affecting schedule

---

## Functional Requirements

### FR-1: Report Definition Schema

Both `config.yaml` and runtime-added reports share a common schema. All field types,
defaults, and constraints are specified below.

**Schema fields:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | `str` | Yes | — | Unique identifier for the report (alphanumeric + underscore + dash only) |
| `title` | `str` | No | Value of `name` | Human-readable report header text |
| `schedule` | `str` | Yes | — | Schedule string (see FR-2 for syntax) |
| `type` | `str` | Yes | — | Report type: `digest` or `analysis` |
| `sources` | `list[str]` | Yes | — | Memory types to pull data from (see FR-4 and FR-5 for allowed values) |
| `window_days` | `int` | No | `7` | How many days back to look for memory files |
| `prompt` | `str` | Conditional | — | Analysis prompt (required if `type == analysis`) |
| `model_route` | `str` | No | `"chat"` | LiteLLM route for analysis reports (ignored for digest reports) |
| `paused` | `bool` | No | `false` | If true, report is skipped by scheduler |
| `deliver_to` | `str` | No | `"telegram"` | Delivery channel (only `telegram` supported in v1) |
| `created` | `str` | Auto | ISO timestamp | When the report was defined (set automatically for runtime reports) |

**Config YAML example:**
```yaml
reports:
  weekly_digest:
    schedule: "mon 07:00"
    type: digest
    sources: [commitments, meetings, contacts]
    window_days: 7
    title: "Weekly Digest"
    paused: false
  
  daily_standup:
    schedule: "weekday 08:30"
    type: analysis
    sources: [commitments, calendar, meetings]
    window_days: 1
    prompt: "Summarize my day: what meetings do I have, what commitments are due, and what should I focus on?"
    title: "Daily Standup Brief"
    model_route: chat
```

**Runtime report storage (in `reports-state.json`):**
```json
{
  "runtime_reports": [
    {
      "name": "weekly_digest",
      "title": "Weekly Project Update",
      "schedule": "fri 17:00",
      "type": "digest",
      "sources": ["projects", "comms"],
      "window_days": 7,
      "paused": false,
      "created": "2026-04-12T10:30:00"
    }
  ],
  "last_sent": {
    "weekly_digest": "2026-04-12",
    "daily_standup": "2026-04-12"
  },
  "paused_config_reports": ["quarterly_review"]
}
```

**Validation rules:**
- `name` must be unique across config + runtime reports
- `name` must match `^[a-z0-9_-]+$` (lowercase alphanumeric, underscore, dash only)
- `schedule` must be valid per FR-2
- `type` must be `digest` or `analysis`
- `sources` must be a non-empty list of allowed source types (see FR-4 and FR-5)
- `window_days` must be >= 1
- `prompt` is required if `type == analysis`; if provided for `digest`, it is ignored
- `model_route` must be a valid LiteLLM route name (validated against config)
- `deliver_to` must be `telegram` (only supported channel in v1)

**Validation criteria:**
- Malformed config entries logged as ERROR on startup, skipped
- Runtime `/report-add` command rejects invalid fields with clear error message
- Config-file reports with duplicate names: last-defined wins (logged as WARN)
- Runtime reports with duplicate names: rejected on add

---

### FR-2: Schedule Parsing

Parse a schedule string into a `ScheduleSpec` object containing `days: list[str]` and
`time: str`.

**Schedule string syntax:**

| Syntax | Meaning | Example |
|--------|---------|---------|
| `"daily HH:MM"` | Every day at the given time | `"daily 07:00"` |
| `"mon HH:MM"` | Every Monday at the given time | `"mon 07:00"` |
| `"tue HH:MM"` | Every Tuesday | `"tue 09:30"` |
| `"wed HH:MM"` | Every Wednesday | `"wed 14:00"` |
| `"thu HH:MM"` | Every Thursday | `"thu 11:15"` |
| `"fri HH:MM"` | Every Friday | `"fri 17:00"` |
| `"sat HH:MM"` | Every Saturday | `"sat 10:00"` |
| `"sun HH:MM"` | Every Sunday | `"sun 18:00"` |
| `"weekday HH:MM"` | Monday through Friday | `"weekday 08:00"` |
| `"weekend HH:MM"` | Saturday and Sunday | `"weekend 09:00"` |
| `"mon,wed,fri HH:MM"` | Multiple specific days (comma-separated, no spaces) | `"mon,wed,fri 12:00"` |

**Parser implementation:**
```python
from dataclasses import dataclass

@dataclass
class ScheduleSpec:
    days: list[str]  # e.g., ["mon", "wed", "fri"]
    time: str        # HH:MM (24-hour)

def parse_schedule(schedule_str: str) -> ScheduleSpec:
    """
    Parse a schedule string into a ScheduleSpec.
    
    Raises ValueError if the format is invalid.
    """
    parts = schedule_str.strip().split()
    if len(parts) != 2:
        raise ValueError(f"Invalid schedule format: '{schedule_str}' (expected 'DAYS HH:MM')")
    
    day_part, time_part = parts
    
    # Validate time format
    if not re.match(r'^\d{2}:\d{2}$', time_part):
        raise ValueError(f"Invalid time format: '{time_part}' (expected HH:MM)")
    
    hour, minute = map(int, time_part.split(':'))
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid time: '{time_part}'")
    
    # Parse day specifier
    if day_part == "daily":
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    elif day_part == "weekday":
        days = ["mon", "tue", "wed", "thu", "fri"]
    elif day_part == "weekend":
        days = ["sat", "sun"]
    elif "," in day_part:
        days = day_part.split(",")
        valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        for d in days:
            if d not in valid_days:
                raise ValueError(f"Invalid day: '{d}'")
    else:
        valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        if day_part not in valid_days:
            raise ValueError(f"Invalid day specifier: '{day_part}'")
        days = [day_part]
    
    return ScheduleSpec(days=days, time=time_part)
```

**Validation criteria:**
- Valid formats parsed correctly
- Invalid formats raise `ValueError` with clear message
- Multi-day comma-separated lists parsed correctly
- Case-insensitive matching (e.g., `"Mon 07:00"` → `["mon"]`)

---

### FR-3: Due-Check Logic

Determine if a report is due to run on the current scheduler tick.

**Due-check function signature:**
```python
def is_due(
    report_name: str,
    schedule_spec: ScheduleSpec,
    last_sent: dict[str, str],
    paused: bool,
    now: datetime
) -> bool:
    """
    Check if a report is due to send.
    
    Args:
        report_name: Unique report identifier
        schedule_spec: Parsed schedule (from FR-2)
        last_sent: Dict mapping report_name -> "YYYY-MM-DD" of last send
        paused: True if report is paused
        now: Current local datetime (timezone-aware)
    
    Returns:
        True if the report should be sent this tick
    """
```

**Due conditions (all must be true):**
1. `paused == False`
2. Current weekday (as 3-letter lowercase string, e.g., "mon") is in `schedule_spec.days`
3. Current time (HH:MM) >= `schedule_spec.time`
4. `last_sent.get(report_name) != today_date_str` (deduplicate: don't send twice in same day)

**Edge cases:**
- If current time is exactly `schedule_spec.time`, the report is due
- If current time is past `schedule_spec.time` (e.g., daemon was stopped and restarted
  later in the day), the report is still sent once (last_sent prevents duplicate)
- If `last_sent` has no entry for the report (new report), it is due on the first
  matching day

**Local time handling:**
- All time checks use the local timezone from `config["user"]["timezone"]`
- `now` is a timezone-aware `datetime` object
- Weekday names map: Monday=mon, Tuesday=tue, Wednesday=wed, Thursday=thu, Friday=fri,
  Saturday=sat, Sunday=sun

**Validation criteria:**
- Report not sent if paused
- Report not sent on wrong day of week
- Report not sent before scheduled time
- Report not sent twice on same day
- New reports (no last_sent entry) send on first matching day after creation

---

### FR-4: Digest Report Generation

Generate a structured pull from specified data sources without calling an LLM.

**Allowed source types:**
- `commitments` — active commitments from `commitment-*.md` files
- `calendar` — calendar events from `calendar-event-*.md` files
- `meetings` — meeting transcripts from `meeting-*.md` files
- `contacts` — contact records from `contact-*.md` files
- `memories` — web page captures from `YYYY-MM-DD-*.md` files (non-typed memories)
- `projects` — code projects from `project-*.md` files
- `comms` — email and Slack threads from `email-thread-*.md` and `slack-thread-*.md`

**Data loading and filtering:**
For each source in the report's `sources` list:
1. Glob the appropriate memory file pattern in `BRAIN_DIR/memories/`
2. Parse frontmatter to extract metadata (use `memory_writer._parse_frontmatter`)
3. Filter to items within `window_days` from today (based on source-specific timestamp field)
4. Sort by recency (most recent first)
5. Cap at 20 items per source

**Timestamp field per source:**
- `commitments`: `created` (when commitment was extracted)
- `calendar`: `start_time` (event start datetime)
- `meetings`: `date` (meeting date)
- `contacts`: `last_interaction` (most recent interaction timestamp)
- `memories`: `created` (when memory file was created)
- `projects`: `last_commit` (most recent commit timestamp)
- `comms`: `last_message` (most recent message in thread)

**Section formatting per source:**

**Commitments:**
```
Commitments (3):
• [outbound] Send revised budget numbers → Sarah Chen (due 2026-04-12)
• [waiting_on] Design mockups from Alex Wong (due 2026-04-15)
• [inbound] Code review from Mike Peters (no due date)
```
Format: `[{commitment_type}] {description} → {recipient or owner} (due {due_date})`
If `due_date` is null, show "(no due date)".
Only include `status: active` commitments.

**Calendar:**
```
Calendar (2):
• 2026-04-15 09:00 — Team Standup (Sarah Chen, Mike Peters) @ Zoom
• 2026-04-15 14:00 — Q4 Budget Review (Sarah Chen, Alex Wong) @ Conference Room A
```
Format: `{start_time} — {title} ({participants}) @ {location}`
If `location` is empty, omit the " @ " part.

**Meetings:**
```
Meetings (2):
• 2026-04-10 — Q4 Planning Review (5 participants, 47 min)
• 2026-04-08 — Product Kickoff (3 participants, 32 min)
```
Format: `{date} — {title} ({participant_count} participants, {duration} min)`

**Contacts:**
```
New Contacts (1):
• Alex Wong (alex.wong@example.com) — first interaction 2026-04-10, score 0.95
```
Format: `{name} ({email}) — first interaction {first_seen}, score {relationship_score}`
Only include contacts where `first_seen` is within `window_days`.

**Memories:**
```
Web Captures (5):
• 2026-04-12 — LiteLLM Router Documentation (docs.litellm.ai)
• 2026-04-11 — Anthropic MCP Spec (anthropic.com)
• 2026-04-10 — Second Brain Design Patterns (karpathy.ai)
```
Format: `{created} — {source_title} ({domain})`
Extract domain from `source_url`.

**Projects:**
```
Projects (2):
• secondbrain — last commit 2026-04-12 (Python, 15 commits this week)
• llm-router — last commit 2026-04-10 (Go, 3 commits this week)
```
Format: `{name} — last commit {last_commit} ({primary_language}, {commits_in_window} commits this week)`
Only include projects where `last_commit` is within `window_days`.
Count commits within `window_days` from `commits` list in frontmatter.

**Comms (email + Slack threads):**
```
Threads (4):
• [email] Q4 Budget Planning — 8 messages, last: 2026-04-12
• [slack] #eng-team: Design Review — 12 messages, last: 2026-04-11
• [email] Vendor Contract Renewal — 3 messages, last: 2026-04-10
```
Format: `[{source}] {subject or channel+topic} — {message_count} messages, last: {last_message}`

**Report header and footer:**
```
# {title} — {date}

{source sections}

---
Generated at {timestamp}
```

**Empty sections:**
If a source returns zero items, omit the section entirely (do not show "Commitments (0)").

**Chunking:**
If the assembled report exceeds 4000 characters, split at paragraph boundaries (double
newline). Send each chunk as a separate Telegram message in sequence.

**Validation criteria:**
- All sources in `sources` list processed
- Items correctly filtered to `window_days` window
- Sections sorted by recency
- Cap at 20 items per source enforced
- Empty sections omitted
- Chunking preserves readability (splits at paragraph boundaries, not mid-line)
- No LLM call made

---

### FR-5: Analysis Report Generation

Generate a narrative synthesis by feeding memory context to an LLM.

**Context loading:**
For each source in the report's `sources` list:
1. Glob and filter memory files to `window_days` (same as FR-4)
2. For each file, extract: frontmatter + first 500 characters of body
3. Concatenate into a single context string, one file per paragraph
4. Cap total context at 8000 characters (truncate oldest files first)

**Context format:**
```
--- {filename} ---
Type: {type}
Created: {created}
{other relevant frontmatter fields}

{first 500 chars of body}

--- {filename} ---
Type: {type}
...
```

**LLM prompt construction:**
```python
system_prompt = """You are synthesizing a personal knowledge report for the user. Be concise and actionable. Focus on insights and connections, not just listing facts."""

user_prompt = f"""{report['prompt']}

Context from the last {window_days} days:

{context_string}
"""
```

**LLM call:**
```python
from litellm import acompletion

response = await acompletion(
    model=report.get("model_route", "chat"),
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ],
    max_tokens=2000,
    temperature=0.7
)

report_body = response.choices[0].message.content
```

**Report format:**
```
# {title} — {date}

{LLM-generated body}

---
Generated at {timestamp} via {model_route}
```

**Chunking:**
If the LLM output exceeds 4000 characters, split at paragraph boundaries and send as
multiple messages.

**Error handling:**
- If LLM call fails (timeout, API error), log ERROR and send fallback message:
  ```
  # {title} — {date}
  
  Report generation failed: {error_message}
  
  This report will retry at the next scheduled time.
  ```
- Do NOT update `last_sent` if generation fails (allows retry)

**Validation criteria:**
- Context correctly capped at 8000 chars
- Oldest files truncated first when over cap
- LLM called with correct model route
- Report body delivered via Telegram
- Failures logged and user notified
- Failed reports do not update `last_sent`

---

### FR-6: Report Scheduler Loop

The `ReportScheduler` class runs a 60-second polling loop that checks all reports and
sends any that are due.

**Class interface:**
```python
class ReportScheduler:
    def __init__(
        self,
        brain_dir: Path,
        deploy_dir: Path,
        config: dict,
        bot,
        chat_id_getter: callable
    ):
        """
        Initialize the report scheduler.
        
        Args:
            brain_dir: Path to iCloud brain directory
            deploy_dir: Path to deployment directory (for state files)
            config: Full config dict (for timezone, reports section)
            bot: Telegram bot instance (for send_message)
            chat_id_getter: Callable returning current chat_id or None
        """
        self.brain_dir = brain_dir
        self.deploy_dir = deploy_dir
        self.config = config
        self.bot = bot
        self.chat_id_getter = chat_id_getter
        self.state_path = deploy_dir / "reports-state.json"
    
    async def run_loop(self, stop_event: asyncio.Event):
        """Main scheduler loop — runs every 60 seconds."""
        while not stop_event.is_set():
            try:
                await self._check_and_send()
            except Exception:
                log.exception("Uncaught error in report scheduler loop")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
    
    async def _check_and_send(self):
        """Check all reports and send any that are due."""
        pass  # Implementation detailed below
```

**Loop tick logic (`_check_and_send`):**
1. Load `reports-state.json` (create empty if not exists)
2. Get current chat_id from `chat_id_getter()`
3. If chat_id is None, log DEBUG "No chat_id configured, skipping reports" and return
4. Get current local datetime (timezone-aware, from `config["user"]["timezone"]`)
5. Assemble list of all reports: config-file reports + `runtime_reports` from state
6. For each report:
   a. Parse schedule string (FR-2)
   b. Check if paused (check both `report.get("paused")` and state's `paused_config_reports`)
   c. Call `is_due()` (FR-3)
   d. If due:
      - Generate report body (FR-4 for digest, FR-5 for analysis)
      - Send via Telegram (chunked if needed)
      - Update `last_sent[report_name]` to today's date
      - Save state file
   e. If error in one report: log ERROR, continue to next report

**Report list assembly:**
Config-file reports come from `config.get("reports", {})` — each key is a report name,
each value is the report definition. Runtime reports come from
`state["runtime_reports"]` (a list of report dicts, each with a `name` field).

**Paused state handling:**
- Runtime reports: `report["paused"]` field
- Config-file reports: name appears in `state["paused_config_reports"]` list

**State file structure:**
```json
{
  "runtime_reports": [
    {
      "name": "weekly_digest",
      "title": "Weekly Digest",
      "schedule": "mon 07:00",
      "type": "digest",
      "sources": ["commitments", "meetings"],
      "window_days": 7,
      "paused": false,
      "created": "2026-04-12T10:00:00"
    }
  ],
  "last_sent": {
    "weekly_digest": "2026-04-12",
    "daily_standup": "2026-04-12"
  },
  "paused_config_reports": ["quarterly_review"]
}
```

**Telegram send:**
```python
async def _send_report(self, chat_id: int, report_body: str):
    """Send a report via Telegram, chunking if needed."""
    chunks = _chunk_message(report_body, max_length=4000)
    for chunk in chunks:
        await self.bot.send_message(chat_id=chat_id, text=chunk)

def _chunk_message(text: str, max_length: int) -> list[str]:
    """Split a message at paragraph boundaries to respect Telegram limits."""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 > max_length:
            chunks.append(current.strip())
            current = paragraph + "\n\n"
        else:
            current += paragraph + "\n\n"
    
    if current.strip():
        chunks.append(current.strip())
    
    return chunks
```

**Validation criteria:**
- Loop runs every 60 seconds
- One report error does not crash the loop
- Chat_id checked before any send attempt
- All reports (config + runtime) checked each tick
- Paused reports skipped
- `last_sent` updated after successful send
- State file saved atomically after each send

---

### FR-7: Telegram Commands

All commands require auth check (same pattern as existing commands). All registered in
`COMMAND_REGISTRY` with description and usage string.

**Command list:**
- `/reports` — list all reports
- `/report <N>` — show detail for report N
- `/report-add` — create a new runtime report (interactive)
- `/report-remove <N>` — delete a runtime report
- `/report-pause <N>` — pause a report
- `/report-resume <N>` — resume a paused report
- `/report-run <N>` — run a report immediately

---

#### `/reports`

**Usage:** `/reports`

**Description:** List all configured reports (config-file + runtime), showing schedule,
type, last sent date, and paused status.

**Output format:**
```
Reports:

1. [digest] weekly_digest — mon 07:00 (last: 2026-04-12)
2. [analysis] daily_standup — weekday 08:30 (last: 2026-04-12)
3. [digest] friday_wrap — fri 17:00 (last: 2026-04-08) [PAUSED]

Use /report N to view details or /report-run N to send immediately.
```

**Empty state:**
```
No reports configured.

Use /report-add to create a new report.
```

**Implementation notes:**
- Store assembled report list in `self._last_report_set` (list of report dicts) for
  detail access via `/report <N>`
- Number reports 1-indexed
- Sort: config-file reports first (alphabetically by name), then runtime reports
  (chronologically by creation date)
- Show "[PAUSED]" suffix for paused reports
- If `last_sent` has no entry for a report, show "(never sent)"

**Validation criteria:**
- Config-file and runtime reports both included
- Paused status shown correctly
- Last sent date accurate
- Result set stored in `_last_report_set`

---

#### `/report <N>`

**Usage:** `/report <N>` or `/report N`

**Description:** Show full definition of report N from the last `/reports` result set.

**Output format:**
```
Report: weekly_digest

Title: Weekly Digest
Type: digest
Schedule: mon 07:00
Sources: commitments, meetings, contacts
Window: 7 days
Paused: No
Last sent: 2026-04-12
Created: 2026-04-01 (config-file)

Use /report-pause 1 to pause this report.
```

For analysis reports, also show:
```
Prompt: "Summarize my day: what meetings do I have..."
Model: chat
```

**Error handling:**
- If no `/reports` has been run: "Please run /reports first."
- If N is out of range: "Invalid report number. Run /reports to see the list."

**Validation criteria:**
- All report fields displayed
- Config-file vs runtime origin shown
- Paused state shown
- Prompt shown for analysis reports only

---

#### `/report-add`

**Usage:** `/report-add`

**Description:** Interactive command to create a new runtime report. Prompts user for
each field step-by-step.

**Interaction flow:**

1. Command invoked: `/report-add`
   ```
   Creating a new report. First, what type?
   
   Reply with: digest or analysis
   ```

2. User replies: `digest`
   ```
   Great. What schedule?
   
   Examples:
   • daily 07:00
   • mon 09:00
   • weekday 08:30
   • mon,wed,fri 12:00
   
   Reply with your schedule:
   ```

3. User replies: `mon 07:00`
   ```
   Which data sources should this report include?
   
   Available: commitments, calendar, meetings, contacts, memories, projects, comms
   
   Reply with a space-separated list (e.g., "commitments meetings"):
   ```

4. User replies: `commitments meetings contacts`
   ```
   How many days back should the report look? (default: 7)
   
   Reply with a number:
   ```

5. User replies: `7` (or just hits enter for default)
   ```
   Report name? (optional — will auto-generate if omitted)
   
   Reply with a name (alphanumeric + underscore + dash only):
   ```

6. User replies: `weekly_team_digest`
   ```
   Report title? (optional — will use name if omitted)
   
   Reply with a title:
   ```

7. User replies: `Weekly Team Digest`
   ```
   Report created: weekly_team_digest
   
   Next delivery: Monday, 2026-04-14 07:00
   
   Use /reports to see all reports.
   ```

**For analysis reports:**
After sources are specified, prompt for analysis prompt:
```
What should the analysis prompt be?

This is the instruction given to the LLM when generating the report.

Reply with your prompt:
```

User replies with prompt text. Then continue with optional name/title prompts.

**Auto-generated names:**
If user skips the name prompt, generate a name from type + sources:
- `digest_commitments_meetings`
- `analysis_calendar_projects`

Ensure uniqueness by appending a number if needed: `digest_commitments_meetings_2`

**State transitions:**
The bot tracks conversation state in `self._report_add_state` dict:
```python
self._report_add_state[user_id] = {
    "step": "type",  # type | schedule | sources | window | prompt | name | title
    "type": None,
    "schedule": None,
    "sources": None,
    "window_days": 7,
    "prompt": None,
    "name": None,
    "title": None
}
```

Each user reply advances the state to the next step. If user sends `/cancel`, clear state
and exit.

**Validation:**
- Schedule string validated per FR-2; if invalid, re-prompt
- Sources validated against allowed list; if invalid, re-prompt
- Name validated against `^[a-z0-9_-]+$`; if invalid, re-prompt
- Window must be >= 1; if invalid, re-prompt

**Persistence:**
After all fields collected, create report dict and append to
`state["runtime_reports"]`, save state file, send confirmation.

**Validation criteria:**
- Interactive flow completes successfully
- Invalid inputs re-prompt (do not crash)
- Auto-generated names are unique
- Analysis reports require prompt
- Report persisted to `reports-state.json`
- Confirmation message shows next delivery datetime

---

#### `/report-remove <N>`

**Usage:** `/report-remove <N>` or `/report-remove N`

**Description:** Delete a runtime report. Config-file reports cannot be removed (only
paused).

**Success response:**
```
Report 'weekly_digest' removed.
```

**Config-file report error:**
```
Config-file reports cannot be removed.

Use /report-pause 1 to disable this report instead.
```

**Implementation:**
- Check if report N is from config or runtime (use origin stored in `_last_report_set`)
- If config: return error message
- If runtime: remove from `state["runtime_reports"]`, save state file, send confirmation

**Validation criteria:**
- Runtime reports removed successfully
- Config-file reports rejected with clear message
- State file updated after removal

---

#### `/report-pause <N>`

**Usage:** `/report-pause <N>` or `/report-pause N`

**Description:** Pause a report (works for both config-file and runtime reports). Paused
reports are skipped by the scheduler.

**Success response:**
```
Report 'weekly_digest' paused.
```

**Implementation:**
- If runtime report: set `report["paused"] = true` in `state["runtime_reports"]`
- If config-file report: add report name to `state["paused_config_reports"]` list
- Save state file

**Validation criteria:**
- Runtime reports: `paused` field set to true
- Config-file reports: name added to `paused_config_reports` list
- Paused state persists across daemon restarts

---

#### `/report-resume <N>`

**Usage:** `/report-resume <N>` or `/report-resume N`

**Description:** Resume a paused report.

**Success response:**
```
Report 'weekly_digest' resumed.

Next delivery: Monday, 2026-04-14 07:00
```

**Implementation:**
- If runtime report: set `report["paused"] = false`
- If config-file report: remove name from `state["paused_config_reports"]`
- Save state file
- Calculate next delivery datetime and include in response

**Next delivery calculation:**
Parse the report's schedule, find the next occurrence after now. Examples:
- Today is Friday 10:00, schedule is "mon 07:00" → "Monday, 2026-04-14 07:00"
- Today is Monday 06:00, schedule is "mon 07:00" → "Monday, 2026-04-14 07:00" (today)
- Today is Monday 08:00, schedule is "mon 07:00" → "Monday, 2026-04-21 07:00" (next week)

**Validation criteria:**
- Paused state cleared
- Next delivery datetime calculated correctly
- State file updated

---

#### `/report-run <N>`

**Usage:** `/report-run <N>` or `/report-run N`

**Description:** Trigger a report immediately, regardless of schedule and paused state.
Does NOT update `last_sent` (does not prevent the scheduled delivery).

**Success response:**
The report itself is sent (no separate confirmation message).

**Implementation:**
- Load report definition from `_last_report_set`
- Generate report body (FR-4 for digest, FR-5 for analysis)
- Send via Telegram
- Do NOT update `last_sent` in state file

**Error handling:**
If generation fails, send error message:
```
Report generation failed: {error_message}
```

**Validation criteria:**
- Report sent immediately
- Paused reports can be manually triggered
- `last_sent` not updated (scheduled delivery unaffected)
- Errors returned to user

---

### FR-8: daemon.py Integration

Add `ReportScheduler` to the daemon's async loop collection.

**Instantiation (in daemon.py, `full` role block):**
```python
# After NotificationManager and TelegramChatHandler are created
report_scheduler = ReportScheduler(
    brain_dir=BRAIN_DIR,
    deploy_dir=DEPLOY_DIR,
    config=config,
    bot=chat.app.bot,
    chat_id_getter=lambda: notification_mgr.get_chat_id()
)

# Add to gather
tasks.append(report_scheduler.run_loop(stop))
```

**Guard condition:**
Only instantiate `ReportScheduler` if:
- Daemon role is `full`, AND
- Either `config.get("reports")` is non-empty OR `reports-state.json` exists with
  non-empty `runtime_reports`

**Deferred imports:**
Import `report_scheduler` inside the `role == "full"` block to avoid crashing on
`watcher` nodes that don't have `python-telegram-bot` installed.

**Validation criteria:**
- Scheduler started on `full` role only
- Scheduler not started if no reports configured
- Imports guarded correctly
- Bot reference passed correctly

---

### FR-9: config.yaml Template Addition

Add a `reports` section template to the default `config.yaml` (commented out).

**Template:**
```yaml
# Configurable Reports
#
# Define recurring reports with custom schedules and data sources. Each report can be a
# simple digest (structured data pull, no LLM call) or an analysis (LLM synthesis).
#
# Schedule syntax:
#   daily HH:MM          — every day
#   mon HH:MM            — every Monday
#   weekday HH:MM        — Monday through Friday
#   weekend HH:MM        — Saturday and Sunday
#   mon,wed,fri HH:MM    — specific days (comma-separated)
#
# Available sources: commitments, calendar, meetings, contacts, memories, projects, comms
#
# Example reports:

reports:
  # Weekly digest — structured pull from commitments, meetings, and contacts
  # weekly_digest:
  #   schedule: "mon 07:00"
  #   type: digest
  #   sources: [commitments, meetings, contacts, calendar]
  #   window_days: 7
  #   title: "Weekly Digest"
  #   paused: false
  
  # Daily standup — LLM analysis of commitments and calendar
  # daily_standup:
  #   schedule: "weekday 08:30"
  #   type: analysis
  #   sources: [commitments, calendar]
  #   window_days: 1
  #   prompt: "What should I focus on today? Summarize my commitments and meetings concisely."
  #   title: "Daily Standup"
  #   model_route: chat
  #   paused: false
  
  # Friday wrap-up — LLM analysis of the week's activity
  # friday_wrap:
  #   schedule: "fri 17:00"
  #   type: analysis
  #   sources: [commitments, meetings, projects, comms]
  #   window_days: 7
  #   prompt: "Summarize my week: what did I accomplish, who did I interact with, and what's left undone?"
  #   title: "Friday Wrap-Up"
  #   model_route: chat
```

**Placement in config.yaml:**
Insert after the `notifications` section, before the `litellm` section.

**Validation criteria:**
- Template is valid YAML when uncommented
- Examples cover both digest and analysis types
- Schedule syntax examples are clear

---

## Non-Functional Requirements

### NFR-1: Performance

- Digest reports generated in <1 second (no LLM call overhead)
- Analysis reports complete in <10 seconds (single LLM call with capped context)
- Scheduler tick completes in <5 seconds when no reports are due
- State file I/O does not block the main loop

### NFR-2: Reliability

- One report error does not prevent other reports from running
- State file corruption handled gracefully (logged, re-initialized)
- LLM API failures logged and retried on next scheduled tick
- Telegram send failures logged and retried on next tick

### NFR-3: Observability

- Each report send logged at INFO: "Sent report '{name}' to chat_id {chat_id}"
- Report generation failures logged at ERROR with full traceback
- Due-check logic logged at DEBUG for troubleshooting
- State file updates logged at DEBUG

---

## Architecture

### Module Structure

New module: `report_scheduler.py`

**Class hierarchy:**
```
ReportScheduler
├── __init__(brain_dir, deploy_dir, config, bot, chat_id_getter)
├── run_loop(stop_event)                    # Main scheduler loop
├── _check_and_send()                       # Check all reports and send due ones
├── _load_state() -> dict                   # Load reports-state.json
├── _save_state(state: dict)                # Atomic write to state file
├── _get_all_reports() -> list[dict]        # Merge config + runtime reports
├── _generate_digest(report: dict) -> str   # FR-4 implementation
├── _generate_analysis(report: dict) -> str # FR-5 implementation
└── _send_report(chat_id, body)             # Chunked Telegram send

DigestGenerator
├── _load_commitments(window_days) -> list
├── _load_calendar(window_days) -> list
├── _load_meetings(window_days) -> list
├── _load_contacts(window_days) -> list
├── _load_memories(window_days) -> list
├── _load_projects(window_days) -> list
├── _load_comms(window_days) -> list
└── _format_section(source, items) -> str

AnalysisGenerator
├── _build_context(sources, window_days) -> str
└── _call_llm(prompt, context, model_route) -> str

parse_schedule(schedule_str) -> ScheduleSpec
is_due(report_name, schedule_spec, last_sent, paused, now) -> bool
```

**Dependencies:**
- `memory_writer._parse_frontmatter` — reuse existing frontmatter parser
- `litellm.acompletion` — for analysis reports
- `python-telegram-bot` — for sending messages
- `pathlib.Path` — for file I/O
- `datetime`, `zoneinfo` — for time/date handling
- `json` — for state persistence

**Deferred imports:**
All imports from `python-telegram-bot` and LiteLLM must be inside the `full` role guard
to avoid crashing on `watcher` nodes.

---

### chat_handler.py Changes

Add command registrations to `COMMAND_REGISTRY`:
```python
COMMAND_REGISTRY = {
    # ... existing commands ...
    
    "/reports": {
        "description": "List all configured reports",
        "usage": "/reports",
        "handler": "handle_reports"
    },
    "/report": {
        "description": "Show details for a specific report",
        "usage": "/report <N>",
        "handler": "handle_report_detail"
    },
    "/report-add": {
        "description": "Create a new runtime report (interactive)",
        "usage": "/report-add",
        "handler": "handle_report_add"
    },
    "/report-remove": {
        "description": "Delete a runtime report",
        "usage": "/report-remove <N>",
        "handler": "handle_report_remove"
    },
    "/report-pause": {
        "description": "Pause a report",
        "usage": "/report-pause <N>",
        "handler": "handle_report_pause"
    },
    "/report-resume": {
        "description": "Resume a paused report",
        "usage": "/report-resume <N>",
        "handler": "handle_report_resume"
    },
    "/report-run": {
        "description": "Run a report immediately",
        "usage": "/report-run <N>",
        "handler": "handle_report_run"
    }
}
```

Add handler methods to `TelegramChatHandler`:
```python
class TelegramChatHandler:
    def __init__(self, ...):
        # ... existing init ...
        self._last_report_set = []
        self._report_add_state = {}  # user_id -> state dict
    
    async def handle_reports(self, update, context):
        """List all reports."""
        pass
    
    async def handle_report_detail(self, update, context):
        """Show detail for report N."""
        pass
    
    async def handle_report_add(self, update, context):
        """Interactive report creation."""
        pass
    
    async def handle_report_remove(self, update, context):
        """Delete a runtime report."""
        pass
    
    async def handle_report_pause(self, update, context):
        """Pause a report."""
        pass
    
    async def handle_report_resume(self, update, context):
        """Resume a report."""
        pass
    
    async def handle_report_run(self, update, context):
        """Run a report immediately."""
        pass
```

**Handler dependency on ReportScheduler:**
The handler methods need access to `ReportScheduler` to manage reports. Pass the
scheduler instance to `TelegramChatHandler.__init__()` in daemon.py:
```python
chat = TelegramChatHandler(
    brain_dir=BRAIN_DIR,
    deploy_dir=DEPLOY_DIR,
    config=config,
    report_scheduler=report_scheduler
)
```

Store as `self.report_scheduler` and call its methods from handlers.

---

### daemon.py Changes

**Import (inside `full` role block):**
```python
if role == "full":
    from report_scheduler import ReportScheduler
    # ... other full-role imports ...
```

**Instantiation (after Telegram bot is started):**
```python
# After notification_mgr is created
report_scheduler = ReportScheduler(
    brain_dir=BRAIN_DIR,
    deploy_dir=DEPLOY_DIR,
    config=config,
    bot=chat.app.bot,
    chat_id_getter=lambda: notification_mgr.get_chat_id()
)

# Pass to chat handler
chat = TelegramChatHandler(
    brain_dir=BRAIN_DIR,
    deploy_dir=DEPLOY_DIR,
    config=config,
    notification_mgr=notification_mgr,
    report_scheduler=report_scheduler
)
```

**Add to asyncio.gather:**
```python
tasks = [
    # ... existing tasks ...
    report_scheduler.run_loop(stop)
]

await asyncio.gather(*tasks)
```

---

## Testing Strategy

### Unit Tests

**tests/unit/test_report_scheduler.py**

**Test parse_schedule():**
- `test_parse_daily_schedule` — "daily 07:00" → all 7 days
- `test_parse_weekday_schedule` — "weekday 08:30" → mon–fri
- `test_parse_weekend_schedule` — "weekend 10:00" → sat–sun
- `test_parse_single_day_schedule` — "mon 07:00" → ["mon"]
- `test_parse_multi_day_schedule` — "mon,wed,fri 12:00" → ["mon", "wed", "fri"]
- `test_parse_invalid_format` — "daily" (no time) → ValueError
- `test_parse_invalid_time` — "daily 25:00" → ValueError
- `test_parse_invalid_day` — "funday 07:00" → ValueError

**Test is_due():**
- `test_is_due_correct_day_and_time` — due=True
- `test_is_due_wrong_day` — due=False
- `test_is_due_before_time` — due=False
- `test_is_due_already_sent_today` — due=False
- `test_is_due_paused` — due=False
- `test_is_due_exactly_at_time` — due=True
- `test_is_due_past_time_same_day` — due=True (if not already sent)
- `test_is_due_new_report_no_last_sent` — due=True on first matching day

**Test DigestGenerator:**
- `test_generate_digest_commitments` — mock memory files, assert section format
- `test_generate_digest_calendar` — assert event format
- `test_generate_digest_meetings` — assert meeting format
- `test_generate_digest_contacts` — assert contact format
- `test_generate_digest_memories` — assert web capture format
- `test_generate_digest_projects` — assert project format
- `test_generate_digest_comms` — assert thread format
- `test_generate_digest_empty_sources` — sections omitted
- `test_generate_digest_chunking` — assert long reports split at paragraph boundaries
- `test_generate_digest_window_filtering` — only items within window_days included
- `test_generate_digest_cap_20_items` — no more than 20 items per source

**Test AnalysisGenerator:**
- `test_generate_analysis_context_capping` — context capped at 8000 chars
- `test_generate_analysis_llm_call` — mock LLM, assert prompt construction
- `test_generate_analysis_error_handling` — LLM failure → error message returned
- `test_generate_analysis_chunking` — long LLM output chunked

**Test ReportScheduler:**
- `test_load_state_empty` — no state file → empty state
- `test_load_state_corrupt` — invalid JSON → logged, re-initialized
- `test_save_state_atomic` — temp file + rename pattern
- `test_get_all_reports` — merges config + runtime
- `test_check_and_send_no_chat_id` — no send attempted
- `test_check_and_send_muted` — reports NOT suppressed by mute (see FR-6 notes)
- `test_check_and_send_due_report` — report sent, last_sent updated
- `test_check_and_send_not_due` — no send
- `test_check_and_send_paused` — no send
- `test_check_and_send_error_handling` — one report error does not stop others

---

### Integration Tests

**tests/integration/test_report_flow.py**

**Test digest report end-to-end:**
1. Write fixture memory files (commitments, calendar, meetings)
2. Create a digest report in config
3. Tick the scheduler at the scheduled time
4. Assert Telegram `send_message` called with correct content
5. Assert `last_sent` updated in state file

**Test analysis report end-to-end:**
1. Write fixture memory files
2. Create an analysis report in config
3. Mock LLM response
4. Tick the scheduler
5. Assert LLM called with correct prompt
6. Assert Telegram send called with LLM output

**Test runtime report creation:**
1. Invoke `/report-add` via mock Telegram update
2. Simulate user replies for each prompt step
3. Assert report created in `reports-state.json`
4. Assert confirmation message sent

**Test report pause/resume:**
1. Create a runtime report
2. Invoke `/report-pause 1`
3. Tick scheduler at scheduled time → no send
4. Invoke `/report-resume 1`
5. Tick scheduler again → send occurs

**Test `/report-run`:**
1. Create a paused report
2. Invoke `/report-run 1`
3. Assert report sent immediately
4. Assert `last_sent` NOT updated
5. Tick scheduler at scheduled time → scheduled send still occurs

---

## Migration and Deployment

### Initial Deployment

1. Commit `report_scheduler.py` to repo
2. Add command handlers to `chat_handler.py`
3. Update `daemon.py` to instantiate and run scheduler
4. Add `reports` section to `config.yaml` template
5. Run `pytest` to ensure all tests pass
6. Run `./install.sh` to deploy
7. Restart daemon via `launchctl unload/load`

### Config Migration

No migration required — the `reports` section is new. Existing configs continue to work
without it (scheduler is not started if no reports are configured).

### State File Initialization

If `reports-state.json` does not exist, it is created on first tick with:
```json
{
  "runtime_reports": [],
  "last_sent": {},
  "paused_config_reports": []
}
```

---

## Open Questions and Risks

### Risk: Digest Report Format Drift

As memory file schemas evolve (new frontmatter fields, new memory types), digest reports
may break or show incomplete data. Mitigation: unit tests for each source type will catch
schema changes. Update `DigestGenerator` when memory schemas change.

### Risk: LLM API Cost for Analysis Reports

Analysis reports call the LLM on every scheduled run. If a user configures many analysis
reports with daily schedules, API costs could add up. Mitigation: default `model_route`
is `chat` (Sonnet), but users can override to a cheaper model (e.g., Haiku) for less
critical reports. Digest reports remain free (no LLM call).

### Risk: Timezone Handling Edge Cases

If the daemon runs across a DST transition or the user changes `user.timezone` in config,
scheduled times may shift unexpectedly. Mitigation: all time checks use the current
config value for `user.timezone`, so a restart after config change will use the new
timezone. DST transitions are handled automatically by `zoneinfo`.

---

## Future Enhancements (Out of Scope for v1)

- **Email and Slack delivery:** Add `deliver_to: email` and `deliver_to: slack` support
- **Parameterized reports:** Allow users to specify dynamic date ranges at query time
  (e.g., "send me a digest for the last 30 days")
- **Report templates:** Predefined report configs users can enable with one command
- **Report history:** Store past report outputs in `BRAIN_DIR/reports/` for browsing
- **Per-report mute:** Allow muting individual reports without pausing them
- **Multi-user routing:** Support multiple Telegram users with separate report configs
- **Conditional reports:** Only send if certain conditions are met (e.g., "only if I have
  3+ commitments due this week")
- **Report chaining:** One report's output triggers another report

---

## Changelog

**v1.0.0** (2026-04-12) — Initial spec

- Two report types: digest (structured pull) and analysis (LLM synthesis)
- Schedule syntax: daily, weekday, weekend, specific days, multi-day
- Config-file and runtime report definitions
- Telegram commands: `/reports`, `/report`, `/report-add`, `/report-remove`,
  `/report-pause`, `/report-resume`, `/report-run`
- State persistence in `reports-state.json`
- 13th async loop in daemon.py (`full` role only)
- Digest sources: commitments, calendar, meetings, contacts, memories, projects, comms
- Analysis reports use configurable LLM model route
- Chunked Telegram output respecting 4096-char limit
