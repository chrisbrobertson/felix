# Second Brain

Automatically captures and summarizes everything you read on the web. Stores summaries as flat markdown files in iCloud Drive. Query your accumulated knowledge through a Telegram bot.

**Philosophy:** No vector DB, no embeddings, no graph. Files + LLM = database.

---

## How it works

A daemon runs twelve async loops:

1. **Browser Watcher** — polls Chrome/Firefox history every 5 minutes, fetches pages you spent time on, summarizes them with Gemini Flash, writes a `.md` file to iCloud
2. **Telegram Bot** — answers questions about what you've read by loading relevant memory files into context
3. **Index Builder** — rebuilds a rolling 400-word synthesis of all your memories every hour
4. **Skill Optimizer** — nightly pass that rewrites underperforming prompt templates (v0.1 stub)
5. **Project Scanner** — scans `~/repos/` and `~/repo/` every 5 minutes for git repositories, writes a living `project-{name}.md` memory file per repo with recent commits, languages, and related projects
6. **Email Scanner** — reads Apple Mail.app data every 5 minutes, writes a living `email-thread-*.md` memory file per conversation thread. Requires Full Disk Access (see below).
7. **Zoom Scanner** — polls Zoom Cloud Recordings every 5 minutes, downloads VTT transcripts, parses speaker-attributed segments, generates a summary, writes `meeting-*.md` memory files. Requires Zoom Server-to-Server OAuth credentials (see below).
8. **Commitment Tracker** — scans meeting and email memory files every 5 minutes, uses LLM to extract commitments and waiting-on items, writes one `commitment-*.md` file per extracted item. Surface via `/commitments` in Telegram.
9. **Calendar Scanner** — reads Apple Calendar.app data every 5 minutes, writes a living `calendar-event-*.md` memory file per event in a rolling ±7-day window. No special permissions required for SQLite path; AppleScript fallback requires Automation permission to Calendar.app (see below).
10. **Contact Tracker** — scans email, meeting, calendar, and Slack memory files every 5 minutes, extracts participant names and emails, writes one `contact-*.md` file per person with relationship scoring and interaction history. Surface via `/contacts` and `/contact <name>` in Telegram.
11. **Slack Scanner** — polls Slack channels every 5 minutes, writes `slack-thread-*.md` memory files. Requires a Slack bot token (see below).
12. **Notification Manager** — checks every 60 seconds for proactive messages to send: daily morning briefing, pre-meeting context pushes (10 min before events), commitment deadline alerts. Controlled via `/briefing`, `/mute`, `/unmute` commands.

---

## Prerequisites

- macOS (uses iCloud Drive and launchd)
- Python 3.11+
- A [Gemini API key](https://aistudio.google.com/app/apikey) (for summarization)
- An [Anthropic API key](https://console.anthropic.com/) (for chat, full node only)
- A Telegram account

---

## Quick install

```bash
./install.sh
```

The installer is idempotent — safe to run again after a key rotation, repo move, or on a second machine. It skips any step already completed (existing config, existing skill files with execution history, etc.) and reloads the launchd agent if it was already running.

---

## What gets installed where

```
~/secondbrain/          deploy dir — venv, logs, runtime state
├── venv/               isolated Python environment (no system pollution)
├── logs/               out.log, error.log
├── seen-urls           processed URL list
├── errors.log          LLM API errors
├── execution-log.jsonl watcher skill execution history
├── email-scanner-state.json
├── zoom-scanner-state.json
├── commitment-scanner-state.json
└── contact-tracker-state.json

~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/
├── memories/           one .md file per captured webpage
├── skills/             prompt templates with execution history
├── inbox/              raw captures pending processing
├── index.md            LLM-maintained rolling summary
└── config.yaml         daemon role, thresholds, API routing
```

---

## Manual setup

If you prefer to set up steps individually:

### 1. Create the iCloud directory structure

```bash
BRAIN="$HOME/Library/Mobile Documents/com~apple~CloudDocs/second-brain"
mkdir -p "$BRAIN/memories" "$BRAIN/skills" "$BRAIN/inbox"
```

### 2. Copy and configure `config.yaml`

```bash
cp config.yaml.template "$BRAIN/config.yaml"
```

Open `$BRAIN/config.yaml` and fill in:

```yaml
user:
  telegram_user_id: YOUR_NUMERIC_USER_ID   # see step 4
  name: Your Name

telegram:
  bot_token: YOUR_BOT_TOKEN                # see step 3
```

Leave everything else as-is to start. Adjust `skip_domains` to taste.

### 3. Create a Telegram bot

1. Open Telegram, message `@BotFather`
2. Send `/newbot` and follow the prompts
3. Copy the token it gives you into `config.yaml` → `telegram.bot_token`

### 4. Get your Telegram user ID

1. Message `@userinfobot` on Telegram
2. It replies with your numeric user ID
3. Copy it into `config.yaml` → `user.telegram_user_id`

The bot ignores all messages from users not on this whitelist.

### 5. Copy skill files to iCloud

```bash
BRAIN="$HOME/Library/Mobile Documents/com~apple~CloudDocs/second-brain"
cp skills/*.md "$BRAIN/skills/"
```

### 6. Create the LiteLLM routing config

```bash
mkdir -p ~/.litellm
cat > ~/.litellm/config.yaml << 'EOF'
model_list:
  - model_name: summarize
    litellm_params:
      model: gemini/gemini-2.0-flash
      api_key: os.environ/GEMINI_API_KEY

  - model_name: chat
    litellm_params:
      model: claude-sonnet-4-20250514
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: optimizer
    litellm_params:
      model: claude-sonnet-4-20250514
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: judge
    litellm_params:
      model: claude-haiku-4-5-20251001
      api_key: os.environ/ANTHROPIC_API_KEY

router_settings:
  num_retries: 2
EOF
```

### 7. Create the virtual environment and install dependencies

```bash
python3 -m venv ~/secondbrain/venv
mkdir -p ~/secondbrain/logs

# Full node
~/secondbrain/venv/bin/pip install -r requirements.txt

# Watcher node (leaner — no Telegram)
~/secondbrain/venv/bin/pip install litellm httpx beautifulsoup4 lxml pyyaml
```

### 8. Set API keys

Add to your shell profile (`~/.zshrc` or `~/.bash_profile`):

```bash
export GEMINI_API_KEY="your-key-here"
export ANTHROPIC_API_KEY="your-key-here"
```

Then reload: `source ~/.zshrc`

---

## Running

### Deploying code changes

After pulling or editing source files, re-run the installer to push changes to
the deploy directory and restart the daemon:

```bash
./install.sh
```

The installer is idempotent — it skips unchanged files and only copies what has
changed. The daemon is reloaded automatically at the end.

### Manual (dev / first run)

```bash
~/secondbrain/venv/bin/python3 ~/secondbrain/daemon.py
```

You should see log output like:
```
2026-04-11 12:00:00 [second-brain] INFO Starting second-brain daemon — role: full
2026-04-11 12:00:00 [chat-handler] INFO Telegram bot polling started
```

Browse to a page, wait up to 5 minutes, then check `$BRAIN/memories/` for a new `.md` file. Message your bot to query it.

### Production (launchd — runs on login, restarts on crash)

1. Edit `com.chrisrobertson.secondbrain.plist` and fill in your API keys in the `EnvironmentVariables` block
2. Copy it to the LaunchAgents directory:

```bash
cp com.chrisrobertson.secondbrain.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist
```

Logs go to `/tmp/second-brain.log` and `/tmp/second-brain.error.log`.

To stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist
```

---

## Email Scanner: Full Disk Access

The email scanner reads Apple Mail.app's Envelope Index database directly for fast, offline access. This requires **Full Disk Access** for the process running the daemon.

Grant it once in **System Settings → Privacy & Security → Full Disk Access**. Re-run `./install.sh` — it opens System Settings to the FDA pane and a Finder window showing `~/secondbrain/venv/bin/`. **Drag `python3` from that Finder window into the FDA list.** Do not use the + button; it filters for app bundles and rejects plain executables.

The installer creates the venv with `--copies` so `~/secondbrain/venv/bin/python3` is a real executable (not a symlink into a `.framework` bundle). macOS accepts it for FDA; framework-internal binaries and symlinks are rejected.

**Known limitation:** On macOS Sonoma (and later), ad-hoc signed binaries (e.g., Homebrew Python) can appear in the FDA list but fail silently at runtime. The scanner detects this and falls back to AppleScript. If you see "AppleScript fallback" warnings in logs despite granting FDA, your Python binary is ad-hoc signed. The AppleScript path is slower and requires Mail.app to be running, but works reliably once configured.

If Full Disk Access is not granted, the scanner falls back to AppleScript (requires Mail.app to be running, no conversation threading, slower). A warning is logged at each scan cycle until access is granted.

To force a full re-scan of all email threads (e.g. after granting FDA for the first time), set `full_rescan: true` in `$BRAIN/config.yaml` under `email_scanner:`. The flag is automatically cleared after the scan completes.

---

## Calendar Scanner: Automation Permission

The calendar scanner reads Apple Calendar.app data to create memory files for events in a rolling ±7-day window.

**Primary path:** SQLite Calendar Cache at `~/Library/Calendars/Calendar Cache` — no permissions required.

**Fallback:** AppleScript to Calendar.app when the SQLite database is absent. This requires **Automation permission** for Calendar.app.

If prompted during first run, grant Automation permission in **System Settings → Privacy & Security → Automation → Terminal (or iTerm) → Calendar**.

The scanner logs a warning if the AppleScript path is taken without permission. If you see "Calendar.app Automation permission denied (error -1743)" in logs, grant the permission and the scanner will work on the next cycle.

**Configuration:** Set `skip_calendars: ["Birthdays", "Holidays"]` in `config.yaml` under `calendar_scanner:` to exclude noise calendars. Events in skipped calendars are never written to memory files.

---

## Multi-machine setup

The system supports two roles. Set `SECOND_BRAIN_ROLE` in each machine's launchd plist — do not set it in `config.yaml` (that file syncs via iCloud and would apply to all machines).

| Role | What runs | API keys needed |
|------|-----------|-----------------|
| `full` | All twelve loops | `GEMINI_API_KEY` + `ANTHROPIC_API_KEY` |
| `watcher` | Browser watcher only | `GEMINI_API_KEY` only |

Run `full` on your always-on machine (Mac Studio / Mac Mini). Run `watcher` on your MacBook — it captures pages you read while traveling and syncs memories to iCloud automatically.

On the watcher machine, install only the watcher dependencies:
```bash
pip install litellm httpx beautifulsoup4 lxml pyyaml
```
(`python-telegram-bot` is not needed and will not be imported.)

---

## Verifying it works

1. Browse to any article and spend 30+ seconds on it
2. Wait up to 5 minutes for the watcher to poll
3. Check `$BRAIN/memories/` — a new `YYYY-MM-DD-*.md` file should appear
4. Message your Telegram bot with a question related to what you read
5. After an hour, `$BRAIN/index.md` will contain a synthesis of all your memories

Check the daemon log for errors:
```bash
tail -f ~/secondbrain/logs/out.log
tail -f ~/secondbrain/logs/error.log
```

---

## Zoom Scanner Setup

The Zoom scanner requires a **Server-to-Server OAuth app** (also called an M2M app) in the Zoom Marketplace. This gives the daemon a long-lived credential that does not expire when a user logs out.

1. Go to [marketplace.zoom.us](https://marketplace.zoom.us/) → Develop → Build App → **Server-to-Server OAuth**
2. Add the following scopes:
   - `recording:read:admin`
   - `meeting:read:admin`
   - `user:read:admin`
3. Copy **Account ID**, **Client ID**, and **Client Secret**
4. In Zoom account settings → **Recording → Cloud Recording**, enable transcription
5. Run `./install.sh` — it prompts for the three credentials and writes them to the launchd plist as `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`

The scanner is skipped gracefully (one WARNING logged) if any credential is missing, so leaving them blank does not break the daemon.

---

## Commitment Tracker

The commitment tracker reads memory files written by the email and Zoom scanners and extracts commitments using LLM. No extra setup is needed — it runs automatically on the `full` role once the source scanners have written memory files.

**Confidence thresholds** (configurable via `commitment_tracker.min_confidence` in `config.yaml`):

| Confidence | Outcome |
|------------|---------|
| ≥ 0.7 | Written as `active` |
| 0.5 – 0.69 | Written with `needs-review` tag |
| < 0.5 | Discarded |

**Extending to new source types** — add a type string to `commitment_tracker.source_types` in `config.yaml`. No code change required.

---

## Telegram Commands

Send these slash commands to your bot:

**Meta:**

| Command | Effect |
|---------|--------|
| `/help` | Show all available commands grouped by category (alias: `/commands`) |

**Memory browsing:**

| Command | Effect |
|---------|--------|
| `/memories [N]` | List N most recent web memories (default 10, max 50) |
| `/search <query>` | Keyword search across ALL memory types |
| `/memory <N>` | View full details of memory at index N from last list/search |
| `/delete <N>` | Delete memory at index N from last list/search |

**Knowledge listings:**

| Command | Effect |
|---------|--------|
| `/contacts [N]` | List contacts sorted by most recent interaction (default 20, max 50) |
| `/contact <name\|N>` | Show detailed contact view (alias: `/people [N]`, `/people <name>`) |
| `/projects [category] [N]` | List projects, optionally filtered by category (e.g. `code`). Default 10, max 50. |
| `/project <N>` | Show project N from last `/projects` list |
| `/events [N]` | List calendar events in the ±7-day window, sorted by start time. Default 10, max 50. |
| `/event <N>` | Show event N from last `/events` list |
| `/meetings [N]` | List meeting transcripts, newest first. Default 10, max 50. |
| `/meeting <N>` | Show meeting N from last `/meetings` list |
| `/comms [email\|slack] [N]` | List email threads and Slack threads, most recent first. Optional source filter. (aliases: `/messages`, `/communications`) |
| `/comm <N>` | Show comm N from last `/comms` list (aliases: `/message`, `/communication`) |

Contacts are deduplicated by email address. Display names are normalized to the longest version seen. Relationship score is recency-weighted: recent interactions contribute more than old ones (1.0 for yesterday, 0.1 for 10 days ago, etc.).

Projects use `type: project` + `category: code` in frontmatter. Future scanners may write `category: person` or `category: work`. Use `/projects person` to filter once those exist.

**Commitment tracker:**

| Command | Effect |
|---------|--------|
| `/commitments [type]` | List active commitments. Optional type filter: `outbound`, `inbound`, `waiting` |
| `/complete <N>` | Mark commitment N as completed |
| `/dismiss <N>` | Mark commitment N as dismissed (false positive or no longer relevant) |

Items with low confidence (0.5–0.69) are shown with a ⚠️ indicator.

**Proactive notifications:**

| Command | Effect |
|---------|--------|
| `/briefing` | Trigger the daily briefing immediately (today's calendar, due/overdue commitments, new memories) |
| `/mute` | Suppress all proactive notifications (briefings, pre-meeting pushes, deadline alerts) |
| `/unmute` | Resume proactive notifications |

The notification manager sends unsolicited messages when enabled:
- **Daily briefing** at the configured time (default 7:30 AM local time)
- **Pre-meeting context** 10 minutes before calendar events (attendees, related commitments, recent threads)
- **Commitment deadline alerts** when commitments are due today or tomorrow

Muted state persists across daemon restarts. `/briefing` works even when muted.

**Domain skip filter:**

| Command | Effect |
|---------|--------|
| `/skip <domain>` | Add a domain to the ignore list (takes effect within 5 min) |
| `/unskip <domain>` | Remove a domain from the ignore list |
| `/skiplist` | Show all currently skipped domains |
| `/purge <domain>` | Delete all captured memories whose URL contains `<domain>` |
| `/purgeall` | Delete memories for every domain currently on the skip list |

The skip list is stored in `$BRAIN/config.yaml` under `browser_watcher.skip_domains`. You can also edit it manually — changes are picked up on the next watcher poll cycle.

---

## Files never to commit

- `$BRAIN/config.yaml` — contains your bot token and user ID; lives in iCloud only
- `com.chrisrobertson.secondbrain.plist` with real API keys filled in
- `~/.second-brain-seen-urls` — runtime state
