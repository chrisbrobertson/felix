---
specmas: 3.0
kind: feature
id: feat-proactive-notifications
version: 1.0.0
created: 2026-04-11
status: implemented
shipped_version: "1.3.0"
complexity: high
maturity: 1
parent_system: second-brain
related_specs:
  - second-brain-spec-v1.0
  - feat-commitment-tracker
  - feat-calendar-scanner
  - feat-contact-tracker
---

# Proactive Notifications

## Overview

### Problem Statement

Secondbrain is entirely reactive — it only responds when the user sends a Telegram
message. Critical time-sensitive information sits silently in memory files until the
user thinks to ask. The user misses reminders like "the budget review starts in 10
minutes and you have an open commitment to Sarah that's related to it", or "you have
3 commitments due today and one is overdue." The system knows everything needed to
surface these insights; it just never pushes them.

The Proactive Notifications system adds a scheduler loop that pushes timely, contextual
messages to the user's Telegram without requiring any user action.

### Scope

**In Scope:**
- Tenth async daemon loop, running every 60 seconds (`full` role only)
- Chat ID persistence: stored from first incoming user message; optional config override
- Daily morning briefing at a configurable local time
- Commitment deadline alerts (due today and due tomorrow)
- Pre-meeting context push (10 minutes before calendar events)
- On-demand `/briefing` command
- `/mute` and `/unmute` commands to suppress all proactive messages
- Mute state persisted in `DEPLOY_DIR/notification-state.json`

**Out of Scope:**
- Per-category notification preferences (v1 is all-or-nothing mute)
- Rich media messages (images, attachments) — text only
- Email or SMS fallback channels
- Notification history or log browsing via Telegram
- Configurable snooze durations
- Waiting-on staleness nudges (future feature, not in v1)
- Multi-user notification routing

### Success Metrics

- Daily briefing delivered within 2 minutes of configured time
- Pre-meeting context delivered 8–12 minutes before event start
- Muted state persists across daemon restarts
- Zero unsolicited messages when muted
- No duplicate briefings delivered on the same day

---

## Functional Requirements

### FR-1: Chat ID Persistence

Store the Telegram `chat_id` required to send unsolicited messages.

**How chat_id is discovered:**
The bot currently stores only `telegram_user_id` (for auth); it has no `chat_id`
to initiate outbound messages. The first time any allowed user sends a message, the
chat_id is captured and persisted.

**Capture on first message** (in `TelegramChatHandler.handle_message` and each
command handler):
```python
# After auth check passes, persist chat_id if not already stored
chat_id = update.effective_chat.id
if self._notification_state.get("chat_id") is None:
    self._notification_state["chat_id"] = chat_id
    _save_notification_state(self._notification_state)
```

**Config override:**
```yaml
notifications:
  telegram_chat_id: null   # set explicitly to override auto-detection
```
If non-null, the config value takes precedence over the persisted state value.

**State file:** `DEPLOY_DIR/notification-state.json`
```json
{
  "chat_id": 123456789,
  "muted": false,
  "last_briefing_date": "2026-04-11"
}
```

**Validation criteria:**
- `chat_id` persisted after first allowed user message
- Config `telegram_chat_id` overrides persisted value
- No outbound message attempted if `chat_id` is null (logged at DEBUG)
- State file written atomically

---

### FR-2: Notification Scheduling Loop

Run a lightweight loop every 60 seconds that checks whether any notification is due and
sends it.

**Loop pattern:**
```python
async def run_loop(self, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await self._check_and_send()
        except Exception:
            log.exception("Uncaught error in notification loop")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
```

**Bot reference:**
`daemon.py` passes `chat.app.bot` to `NotificationManager.__init__()` after
`await chat.start()`. The bot reference is used to call
`await bot.send_message(chat_id=chat_id, text=text)`.

**Per-check logic in `_check_and_send()`:**
1. Load state from `notification-state.json`
2. If `muted` is True, return immediately
3. If `chat_id` is None, return immediately
4. Check daily briefing (FR-3)
5. Check commitment deadline alerts (FR-4)
6. Check pre-meeting context (FR-5)

**Message chunking:**
All outbound messages must respect Telegram's 4096-character limit. Long messages
(e.g., briefings) are split into chunks of ≤ 4000 characters at paragraph or line
boundaries. Each chunk is sent as a separate message.

**Validation criteria:**
- Loop exits cleanly on `stop_event.is_set()`
- One uncaught exception does not kill the loop
- Muted state checked before any send attempt
- Messages over 4096 chars split and sent as multiple messages

---

### FR-3: Daily Morning Briefing

Send a morning briefing message once per day at the configured local time.

**Deduplication:**
- State file stores `last_briefing_date: "YYYY-MM-DD"` (local date)
- Briefing only sent if today's date > `last_briefing_date`
- After sending, update `last_briefing_date` to today

**Timing check:**
- Compare current local time to `notifications.briefing_time` (HH:MM, 24-hour)
- Send if `current_time >= briefing_time` and `last_briefing_date != today`
- Uses `user.timezone` from config (e.g., "America/Los_Angeles") for local time

**Briefing content (assembled from memory files, no LLM call):**
1. **Today's calendar events** — glob `calendar-event-*.md` where `start_time` date
   = today; list title, time, and participants
2. **Overdue commitments** — glob `commitment-*.md` where `status: active` and
   `due_date < today`; list description and owner
3. **Commitments due today** — same glob with `due_date == today`
4. **New memories since yesterday** — count of memory files with `last_scanned`
   timestamp > 24 hours ago
5. **Stale waiting-ons** — `commitment_type: waiting_on`, `status: active`,
   `last_scanned` > 7 days ago (configurable)

**Reply format:**
```
Good morning. Here's your briefing for Saturday, April 11:

Calendar (3 events):
• 9:00 AM — Team Standup (Sarah Chen, Mike Peters)
• 11:00 AM — Q4 Budget Review (Sarah Chen, Alex Wong)
• 3:00 PM — 1:1 with Alex

Commitments due today (2):
• [outbound] Send revised budget numbers → Sarah Chen
• [waiting_on] Vendor quote from Mike Peters

Overdue (1):
• [inbound] Alex to share design mockups — was due 2026-04-09

27 new memories captured since yesterday.
```

If a section is empty (no events, no overdue commitments), it is omitted.

**Validation criteria:**
- Briefing sent at most once per calendar day (local time)
- Correct local time used (respects `user.timezone`)
- Empty sections omitted (no "Calendar (0 events)" line)
- State file updated after send to prevent duplicate on next cycle check

---

### FR-4: Commitment Deadline Alerts

Push a short alert message when a commitment's due date is today or tomorrow.

**Trigger:**
- On each 60-second check: glob `commitment-*.md`, filter to `status: active` with
  `due_date` set
- Alert if `due_date == today` or `due_date == tomorrow`
- Deduplication: track sent alerts in `notification-state.json` under
  `sent_commitment_alerts: ["commitment-id-1", "commitment-id-2"]`
- Alert sent only once per commitment (not re-sent each cycle)
- Alerts cleared from the sent list when the commitment's `due_date` has passed by
  more than 1 day (to avoid stale entries accumulating)

**Alert format (due today):**
```
Commitment due today:
[outbound] Send revised budget numbers → Sarah Chen
Source: Q4 Planning Review (meeting)
```

**Alert format (due tomorrow):**
```
Reminder: commitment due tomorrow:
[waiting_on] Design mockups from Alex Wong
Source: Product kickoff email thread
```

**Validation criteria:**
- Alert sent at most once per commitment per deadline window (not re-sent each 60s)
- Alert not sent when muted
- Completed/dismissed commitments not alerted
- Alert not sent if due_date has already passed (handled by briefing instead)

---

### FR-5: Pre-Meeting Context Push

Push a context brief 10 minutes before each calendar event.

**Trigger:**
- On each 60-second check: glob `calendar-event-*.md` where `start_time` is between
  now+8min and now+12min (4-minute window to avoid re-firing on consecutive cycles)
- Skip all-day events (`all_day: true`)
- Deduplication: track sent pre-meeting alerts in `notification-state.json` under
  `sent_pre_meeting: ["calendar:event-id-1"]` — cleared after event start time passes

**Context assembly (no LLM call — assembled from flat files):**
1. Event title, time, and location from the calendar memory file
2. **Participants:** load `contact-{name-slug}.md` for each attendee (if present)
   — show last interaction date and relationship score
3. **Open commitments** involving any attendee: glob `commitment-*.md` where
   `status: active` and (`owner` or `recipient` matches any attendee name/email)
4. **Recent threads** mentioning any attendee: scan `_last_results` cache headers
   for name matches (top 3 most recent)

**Pre-meeting format:**
```
Q4 Budget Review starts in 10 minutes (11:00 AM, Zoom)

Attendees:
• Sarah Chen — last interaction 2026-04-10, relationship score 3.42
• Alex Wong — last interaction 2026-04-08, relationship score 0.95

Open commitments with attendees:
• [outbound] Send revised budget numbers → Sarah Chen (due today)
• [waiting_on] Design sign-off from Alex (no due date)

Recent context:
• Q4 Planning email thread (2026-04-10, email)
• Product roadmap discussion (2026-04-07, meeting)
```

**Validation criteria:**
- Pre-meeting push sent once per event per day (not re-fired each 60s)
- All-day events skipped
- Missing contact files handled gracefully (attendee shown without score)
- No commitments → commitments section omitted
- No recent threads → recent context section omitted

---

### FR-6: `/briefing` On-Demand Command

Trigger the daily briefing immediately, regardless of time and mute state.

**Usage:** `/briefing`

**Behaviour:**
- Assemble and send the same content as FR-3
- Does NOT update `last_briefing_date` (does not prevent the scheduled briefing
  from sending at its configured time)
- Ignores mute state (user is explicitly requesting it)

**Reply format:** Same as FR-3.

**Validation criteria:**
- `/briefing` delivers briefing even when muted
- `/briefing` does not advance `last_briefing_date`
- Briefing sent as reply to the user's `/briefing` command message

---

### FR-7: `/mute` and `/unmute` Commands

Suppress or resume all proactive messages.

**Usage:** `/mute`, `/unmute`

**Behaviour:**
- `/mute`: set `muted: true` in `notification-state.json`; reply "Proactive notifications muted."
- `/unmute`: set `muted: false`; reply "Proactive notifications resumed."
- Mute does not affect responses to user commands (only unsolicited pushes are suppressed)

**Validation criteria:**
- `/mute` prevents all outbound messages from FR-3, FR-4, FR-5
- `/unmute` resumes immediately on next 60-second check
- `/briefing` still works while muted (explicit request)
- Mute state persisted across daemon restarts

---

### FR-8: Mute State Persistence

Mute state and alert deduplication data survive daemon restarts.

**State file:** `DEPLOY_DIR/notification-state.json`

Full schema:
```json
{
  "chat_id": 123456789,
  "muted": false,
  "last_briefing_date": "2026-04-11",
  "sent_commitment_alerts": ["abc123def456", "789ghi012jkl"],
  "sent_pre_meeting": ["calendar:evt-abc123"]
}
```

**Rules:**
- `sent_commitment_alerts` pruned: remove entries for commitments whose `due_date`
  is more than 1 day in the past
- `sent_pre_meeting` pruned: remove entries for events whose `start_time` has passed
- State written atomically after each modification

**Validation criteria:**
- Daemon restart does not re-send alerts that were already delivered
- Stale entries pruned on each 60-second check (not on every save)
- State file write is atomic (temp file + os.rename)

---

### FR-9: Bot Reference Passing (daemon.py Integration)

Pass the Telegram bot reference from `daemon.py` to `NotificationManager` after the
chat handler is started, without circular imports.

**Pattern in `daemon.py`:**
```python
# In the full-role block:
from chat_handler import TelegramChatHandler
from notification_manager import NotificationManager

chat = TelegramChatHandler()
await chat.start()   # starts the Telegram application

notification_mgr = NotificationManager(
    bot=chat.app.bot,
    deploy_dir=DEPLOY_DIR,
)
tasks.append(notification_mgr.run_loop)
```

`NotificationManager.__init__` stores `self.bot = bot` and `self.deploy_dir = deploy_dir`.
All outbound sends use `await self.bot.send_message(chat_id=..., text=...)`.

**Validation criteria:**
- `NotificationManager` does not import `TelegramChatHandler` (no circular imports)
- `bot` is `None` → all sends skipped silently (not an error — allows unit testing)
- Loop starts correctly within `asyncio.gather` alongside other loops

---

## Config

```yaml
notifications:
  enabled: true
  briefing_time: "07:30"           # local time (uses user.timezone from config)
  pre_meeting_minutes: 10          # push context N minutes before events
  stale_waiting_on_days: 7         # waiting-ons older than N days appear in briefing
  telegram_chat_id: null           # auto-detected from first message if null

user:
  timezone: "America/Los_Angeles"  # used for local-time briefing scheduling
```

---

## Files to Create/Modify

| File | Change |
|------|--------|
| `specs/feat-proactive-notifications.md` | **This spec** |
| `notification_manager.py` | **Create** — NotificationManager class with scheduling loop |
| `daemon.py` | Instantiate NotificationManager, pass bot reference, add to full-role gather (loop 10) |
| `chat_handler.py` | Persist chat_id on first allowed message; add `/briefing`, `/mute`, `/unmute` handlers |
| `config.yaml.template` | Add `notifications` section; add `user.timezone` |
| `install.sh` | Add `notification_manager.py` to DAEMON_FILES |
| `CLAUDE.md` | Update to ten async loops, add NotificationManager description and new commands |
| `README.md` | Document proactive notifications, configuration, mute commands |
| `tests/unit/test_notification_manager.py` | **Create** |

---

## Unit Tests (`tests/unit/test_notification_manager.py`)

| Test | Assertion |
|------|-----------|
| `test_chat_id_persisted_on_first_message` | First allowed message writes chat_id to state file |
| `test_chat_id_from_config_overrides_state` | Non-null `telegram_chat_id` in config takes precedence |
| `test_no_send_when_chat_id_null` | send_message not called when chat_id is None |
| `test_no_send_when_muted` | FR-3/FR-4/FR-5 sends suppressed when `muted: true` |
| `test_briefing_bypasses_mute` | `/briefing` command delivers briefing even when muted |
| `test_mute_state_persists` | `muted: true` written to state; reloaded correctly |
| `test_unmute_resumes_notifications` | `muted: false` written; next check sends normally |
| `test_daily_briefing_at_configured_time` | Briefing triggered when local time >= briefing_time |
| `test_daily_briefing_not_before_configured_time` | No briefing before configured time |
| `test_daily_briefing_not_repeated_same_day` | `last_briefing_date == today` prevents second send |
| `test_daily_briefing_updates_last_date` | After send, `last_briefing_date` set to today |
| `test_on_demand_briefing_does_not_advance_date` | `/briefing` does not set `last_briefing_date` |
| `test_briefing_includes_todays_calendar_events` | calendar_event files for today listed |
| `test_briefing_includes_due_commitments` | Active commitments with due_date=today shown |
| `test_briefing_includes_overdue` | due_date < today shown as overdue |
| `test_briefing_empty_section_omitted` | Section with no items not included in message |
| `test_commitment_alert_due_today` | due_date=today triggers alert |
| `test_commitment_alert_due_tomorrow` | due_date=tomorrow triggers alert |
| `test_commitment_alert_deduplication` | Same commitment not re-alerted on next 60s cycle |
| `test_commitment_alert_not_for_completed` | completed/dismissed commitments not alerted |
| `test_commitment_alerts_pruned_by_age` | sent_commitment_alerts entries > 1 day past due removed |
| `test_pre_meeting_in_window` | Event starting in 8–12 min triggers pre-meeting push |
| `test_pre_meeting_outside_window` | Event starting in 5 min or 15 min → no push |
| `test_pre_meeting_all_day_skipped` | `all_day: true` events never trigger pre-meeting |
| `test_pre_meeting_deduplication` | Same event not pushed again on next 60s cycle |
| `test_pre_meeting_includes_contacts` | Contact file info shown for attendees |
| `test_pre_meeting_includes_open_commitments` | Active commitments with attendees shown |
| `test_pre_meeting_missing_contact_graceful` | No contact file → attendee shown without score |
| `test_pre_meeting_sent_alerts_pruned` | Entries for past events removed from state |
| `test_message_chunking_at_4000_chars` | Message > 4096 chars split into multiple sends |
| `test_message_chunking_at_line_boundary` | Split at paragraph break, not mid-sentence |
| `test_loop_exits_on_stop_event` | Clean shutdown when stop_event is set |
| `test_exception_does_not_kill_loop` | RuntimeError in _check_and_send → loop continues |
| `test_state_file_write_atomic` | No .tmp file left after state write |
| `test_cmd_mute_sets_state` | `/mute` writes `muted: true` |
| `test_cmd_unmute_clears_state` | `/unmute` writes `muted: false` |
