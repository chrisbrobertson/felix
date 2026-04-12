# Second Brain

A personal knowledge system that automatically captures everything you interact with — web pages, emails, meetings, calendar events, Slack threads, code projects — summarizes it all with LLMs, and makes it queryable through a Telegram bot that acts as your extended memory.

Ask questions like "What did I read about Rust async last week?" or "Who was on that call about the API redesign?" and get instant answers pulled from your accumulated context.

**Philosophy:** No vector DB, no embeddings, no graph. Files + LLM = database.

---

## What you can do with it

**Never lose track of what you've read.** Browse an article on your laptop, summarize it automatically, ask your bot about it days later from your phone. Works across all your devices via iCloud sync.

**Search your entire work context in one place.** Find people, commitments, projects, meetings, email threads, and calendar events through natural language queries. The bot loads relevant memories into Claude's context and answers from your accumulated knowledge.

**Get proactive notifications.** Morning briefing with today's calendar and due commitments. Context push 10 minutes before each meeting (who's attending, related commitments, recent threads). Deadline alerts for commitments due today or tomorrow.

**Track commitments automatically.** The system extracts action items from meetings and emails, writes them as structured memory files, and surfaces them via `/commitments`. Mark them `/complete` or `/dismiss` as you work through them. Get accuracy stats with `/accuracy`.

**Build a living contact graph.** Every participant in every email, meeting, calendar event, or Slack thread becomes a contact with a relationship score, interaction history, and links to related threads. Search with `/contacts` or `/contact <name>`.

**Control what gets captured.** Skip noisy domains with `/skip reddit.com`, purge unwanted memories with `/purge <domain>`, and manage your ignore list with `/skiplist` and `/unskip`.

**Get better summaries as the system learns.** Second Brain routes each captured page to a specialized summarizer — research papers, API docs, code repos, and video transcripts each get their own prompt. A nightly optimizer scores past runs, catches declining skills early, and rewrites the weakest ones. Check skill health any time with `/skill-health`.

---

## Architecture

Second Brain runs on two kinds of machines: a **full node** (always-on Mac like a Mac Studio or Mac Mini) that runs all the processing, and optional **watcher nodes** (laptops) that capture browser history while you're traveling. Both sync through iCloud Drive.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            iCloud Drive                                 │
│  ~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/          │
│                                                                         │
│  memories/        ← all knowledge files (.md per item)                 │
│  skills/          ← LLM prompt templates with execution history        │
│  index.md         ← hourly rolling synthesis of all memories           │
│  config.yaml      ← shared config (DO NOT put role here)               │
└─────────────────────────────────────────────────────────────────────────┘
         ▲                                              ▲
         │                                              │
         │ write memories                               │ write memories
         │ read config                                  │ read config
         │                                              │
┌────────┴────────────────────┐            ┌───────────┴──────────────────┐
│   WATCHER NODE (MacBook)    │            │   FULL NODE (Mac Studio)     │
│                             │            │                              │
│  Role: watcher              │            │  Role: full                  │
│  Runs: Browser Watcher only │            │  Runs: All 12 loops          │
│                             │            │                              │
│  • Polls Chrome/Firefox     │            │  1. Browser Watcher          │
│  • Summarizes pages         │            │  2. Telegram Bot ◄───────────┼─── YOU
│  • Writes to iCloud         │            │  3. Index Builder            │
│                             │            │  4. Skill Optimizer          │
│  Needs: GEMINI_API_KEY      │            │  5. Project Scanner          │
│                             │            │  6. Email Scanner            │
│                             │            │  7. Zoom Scanner             │
│                             │            │  8. Commitment Tracker       │
│                             │            │  9. Calendar Scanner         │
│                             │            │ 10. Contact Tracker          │
│                             │            │ 11. Slack Scanner            │
│                             │            │ 12. Notification Manager     │
│                             │            │                              │
│                             │            │  Needs: GEMINI_API_KEY       │
│                             │            │         ANTHROPIC_API_KEY    │
└─────────────────────────────┘            └──────────────────────────────┘

Data flows (full node):

  Browser history ──► Browser Watcher ──► memories/ ──┬──► Index Builder ──► index.md
                                                       │
  Apple Mail ────────► Email Scanner ──► email-thread-*.md ──┬──► Commitment Tracker ──► commitment-*.md
                                                              │
  Zoom API ───────────► Zoom Scanner ──► meeting-*.md ───────┤
                                                              │
  git repos ──────────► Project Scanner ──► project-*.md     │
                                                              ├──► Contact Tracker ──► contact-*.md
  Apple Calendar ─────► Calendar Scanner ──► calendar-event-*.md
                                                              │
  Slack API ──────────► Slack Scanner ──► slack-thread-*.md ─┘

  All memories ──► Telegram Bot (keyword relevance) ──► Claude API ──► answers
  index.md ─────────────────────────────┘

  calendar-event-*.md + commitment-*.md ──► Notification Manager ──► Telegram (proactive)
```

**Key design:** iCloud Drive is the shared bus. Watcher nodes write memories, full node picks them up and indexes them. Config is shared via iCloud; per-machine role is set via `SECOND_BRAIN_ROLE` env var in the launchd plist (NOT in config.yaml, which would sync everywhere).

**Specialized summarizers.** The browser watcher doesn't use one generic prompt for everything. It classifies each URL into one of five content types (research paper, API docs, code repo, video transcript, or default web page) and routes to a specialized skill — `summarize-paper.md`, `summarize-docs.md`, `summarize-repo.md`, `summarize-transcript.md`, or `summarize-webpage.md`. When a new content type appears that has no matching skill, the daemon can draft one automatically. See *Skill optimization and quality* below.

---

## How to use it

The bot is your main interface. All commands are sent via Telegram.

### Finding information about a person

```
/contacts                # list everyone you've interacted with, sorted by recency
/contacts 50             # show more results (max 50)
/contact Alice Chen      # detailed view: interaction history, relationship score, related threads
/contact 3               # show contact #3 from the last /contacts list
```

Contacts are deduplicated by email address. The system picks the longest display name it's seen (e.g. "Alice Chen" beats "Alice"). Relationship score is recency-weighted: yesterday's interaction contributes 1.0, last week's contributes ~0.5, 10 days ago contributes 0.1.

### Tracking commitments

```
/commitments             # show all active commitments (outbound, inbound, waiting-on)
/commitments outbound    # filter to just things you committed to do
/commitments inbound     # filter to things others committed to you
/commitments waiting     # filter to things you're waiting on

/complete 3              # mark commitment #3 done
/dismiss 3               # dismiss #3 (false positive or no longer relevant)

/wrong 3                 # mark extracted commitment as false positive (improves accuracy stats)
/missed                  # manually add a commitment the bot missed
/accuracy                # show extraction precision per source type (email, meeting, etc.)
```

Items with low confidence (0.5–0.69) show a ⚠️ indicator. The default threshold is 0.7 for auto-active, configurable via `commitment_tracker.min_confidence` in `config.yaml`.

### Browsing recent activity

```
/memories [N]            # list your N most recent web captures (default 10, max 50)
/memory <N>              # show full detail of memory N from the last list

/meetings [N]            # list meeting transcripts, newest first
/meeting <N>             # show meeting detail: attendees, summary, transcript

/comms [N]               # unified email + Slack threads, most recent first
/comms email [N]         # filter to email only
/comms slack [N]         # filter to Slack only
/comm <N>                # show thread detail

/events [N]              # calendar events in a ±7-day window, sorted by start time
/event <N>               # event detail: time, location, attendees, description, related commitments

/projects [N]            # list git repos, sorted by last commit
/projects code 50        # filter by category (currently only `code` exists)
/project <N>             # full project detail: description, languages, commits, README summary
```

All list commands accept an optional count (default 10, max 50). The `<N>` argument in detail commands refers to the index from the last list or search.

### Searching across all your knowledge

```
/search rust async       # search ALL memory types — grouped results: Contacts, Commitments, Projects, Meetings, Emails, Slack, Events, Web
/search email rust async # filter to one type: email, slack, meeting, project, commitment, event, contact, web
```

Results are grouped by type with up to 5 per group. If a group has more than 5, you'll see a hint like "12 more — try `/search email rust async`" to filter.

### Controlling what gets captured

```
/skip reddit.com         # add domain to ignore list
/skiplist                # show all skipped domains
/unskip reddit.com       # remove domain from ignore list

/purge reddit.com        # delete all memories whose URL contains "reddit.com"
/purgeall                # delete memories for every domain on the skip list

/delete 3                # delete memory #3 from the last list or search
```

The skip list is stored in `config.yaml` under `browser_watcher.skip_domains`. Changes take effect within 5 minutes (next watcher poll).

### Managing summarizer quality

Second Brain runs a pool of specialized summarizer skills and improves them automatically. You can check their health, review auto-drafted skills before they go live, and control the approval workflow.

```
/skill-health             # utility scores and trend arrows for every skill

/skill-drafts             # list drafts the daemon has queued for your review
/skill-draft <N>          # show the full markdown of draft N
/approve-skill <N>        # accept draft N — it enters 5-run probation
/reject-skill <N>         # discard draft N; its content type is on a 24h cooldown

/skill-approval on        # require approval before any new skill goes live
/skill-approval off       # let the daemon auto-create skills (probation only)
/skill-approval status    # show effective mode
```

`/skill-health` shows a utility score for each skill — a recency-weighted mean of its recent execution scores — plus a trend arrow: `▲` improving, `▼` declining, `◆` stable, `—` new. A `⚠` flag marks skills below the underperformance threshold (default 0.70) or on a declining trend. Skills with fewer than three scored runs show `—` until they accumulate enough history.

When the browser watcher sees a URL whose content type has no matching skill, the daemon drafts a new one using Claude Sonnet. By default the draft is written directly to `skills/` and runs in **probation mode** — its output is discarded for the first five executions while the optimizer scores it. After probation, a daily graduation check promotes the skill to active if its utility score ≥ 0.6, or triggers a rewrite and re-probation if not (maximum three attempts before the skill is marked failed).

If you'd rather review drafts before they run, send `/skill-approval on`. New drafts land in `$BRAIN/skill-drafts/` and the bot sends you a Telegram notification; nothing executes until you `/approve-skill`. The runtime override survives daemon restarts.

### Managing proactive notifications

```
/briefing                # trigger today's briefing now (works even when muted)
/mute                    # suppress all proactive notifications
/unmute                  # resume proactive notifications
```

When unmuted, the bot sends:
- **Daily briefing** at the configured time (default 7:30 AM): today's calendar, due/overdue commitments, new memories since yesterday
- **Pre-meeting context** 10 minutes before each calendar event: attendees, related commitments, recent email/Slack threads
- **Commitment deadline alerts** when items are due today or tomorrow

Muted state persists across daemon restarts. `/briefing` works even when muted — useful for manually checking in without turning on auto-notifications.

### Getting help

```
/help                    # show all commands grouped by task (alias: /commands)
```

---

## Complete command reference

| Command | Effect |
|---------|--------|
| **Meta** | |
| `/help`, `/commands` | Show all available commands grouped by category |
| **Skill management** | |
| `/skill-health` | Utility scores and trend arrows for every skill. Sorted worst-first; `▲` improving, `▼` declining, `◆` stable, `—` insufficient data. `⚠` = below underperformance threshold or declining. |
| `/skill-drafts` | List auto-drafted skill files awaiting approval (only populated when `/skill-approval on`). |
| `/skill-draft <N>` | Show the full markdown of draft N from the last `/skill-drafts` list. |
| `/approve-skill <N>` | Promote draft N into `skills/`. Enters 5-run probation (output discarded while being scored). |
| `/reject-skill <N>` | Delete draft N. Its content type is blocked from re-triggering skill creation for 24 hours. |
| `/skill-approval on\|off\|status` | Runtime HITL toggle. `on` = require `/approve-skill` before new skills run. Overrides `config.yaml`; persists across restarts. |
| **People & contacts** | |
| `/contacts [N]` | List contacts sorted by most recent interaction (default 20, max 50). Deduplicated by email, display name is longest seen version, recency-weighted relationship score. |
| `/people [N]` | Alias for `/contacts` |
| `/contact <name\|N>` | Detailed contact view: interaction history, related threads, relationship score |
| **Projects** | |
| `/projects [category] [N]` | List git repos (default 10, max 50). Optional category filter (currently `code`). Sorted by last commit. |
| `/project <N>` | Full project detail: description, languages, recent commits, README summary, related projects |
| **Calendar & meetings** | |
| `/events [N]` | Calendar events in ±7-day window, sorted by start time (default 10, max 50) |
| `/event <N>` | Event detail: time, location, attendees, description, related commitments |
| `/meetings [N]` | Zoom meeting transcripts, newest first (default 10, max 50) |
| `/meeting <N>` | Meeting detail: date, attendees, summary, transcript |
| **Email & Slack** | |
| `/comms [email\|slack] [N]` | Unified email + Slack threads, most recent first (default 10, max 50). Optional filter arg. |
| `/messages [email\|slack] [N]` | Alias for `/comms` |
| `/communications [email\|slack] [N]` | Alias for `/comms` |
| `/comm <N>` | Thread detail (email-shaped or Slack-shaped based on type) |
| `/message <N>` | Alias for `/comm` |
| `/communication <N>` | Alias for `/comm` |
| **Commitments** | |
| `/commitments [type]` | Active commitments. Optional type filter: `outbound`, `inbound`, `waiting`. Items with confidence 0.5–0.69 show ⚠️. |
| `/complete <N>` | Mark commitment N completed |
| `/dismiss <N>` | Dismiss commitment N (false positive or no longer relevant) |
| `/wrong <N>` | Mark extracted commitment as false positive (feeds accuracy stats) |
| `/missed` | Manually add a commitment the bot missed |
| `/accuracy` | Show extraction precision per source type |
| **Memory browsing** | |
| `/memories [N]` | List N most recent web captures (default 10, max 50) |
| `/search <query>` | Search across ALL memory types. Results grouped by type: Contacts, Commitments, Projects, Meetings, Email threads, Slack threads, Calendar events, Web memories. Up to 5 per group, overflow hint shows `/search <type> <query>`. |
| `/search <type> <query>` | Filter to one type: `email`, `slack`, `meeting`, `project`, `commitment`, `event`, `contact`, `web` |
| `/memory <N>` | Show full detail of item N from last list or search |
| `/delete <N>` | Delete item N from last list or search |
| **Proactive notifications** | |
| `/briefing` | Trigger today's briefing now (works even when muted): today's calendar, due/overdue commitments, new memories |
| `/mute` | Suppress all proactive notifications (briefings, pre-meeting pushes, deadline alerts) |
| `/unmute` | Resume proactive notifications |
| **Domain filter** | |
| `/skip <domain>` | Add domain to ignore list (e.g. `/skip reddit.com`) |
| `/unskip <domain>` | Remove domain from ignore list |
| `/skiplist` | Show all currently skipped domains |
| `/purge <domain>` | Delete all captured memories whose URL contains domain |
| `/purgeall` | Delete memories for every domain on the skip list |

---

## Quick install

```bash
./install.sh
```

The installer is idempotent — safe to run again after a key rotation, repo move, or on a second machine. It skips any step already completed (existing config, existing skill files with execution history, etc.) and reloads the launchd agent if it was already running.

---

## What gets installed where

```
Runtime state (local per machine):
~/secondbrain/
├── venv/                           # Python virtual environment
├── logs/                           # out.log, error.log (written by launchd)
├── seen-urls                       # processed URLs (browser watcher)
├── errors.log                      # LLM API errors
├── execution-log.jsonl             # watcher-node skill execution log
├── email-scanner-state.json        # high-water ROWID for email scanner
├── zoom-scanner-state.json         # processed meeting UUIDs
├── commitment-scanner-state.json   # processed file mtimes
├── calendar-scanner-state.json     # processed event modification timestamps
├── contact-tracker-state.json      # processed file mtimes and interaction timestamps
├── slack-scanner-state.json        # processed Slack thread timestamps
├── commitment-corrections.jsonl    # /wrong and /missed feedback log
├── commitment-accuracy.json        # extraction precision stats per source type
└── notification-state.json         # chat_id, mute state, sent alerts

iCloud (shared across all machines):
~/Library/Mobile Documents/com~apple~CloudDocs/second-brain/
├── memories/       # All knowledge files — one .md per item
├── skills/                 # LLM prompt templates with embedded execution history
│   ├── summarize-webpage.md    # default (all URLs not matched below)
│   ├── summarize-paper.md      # research papers, PDFs, arxiv/doi/semanticscholar
│   ├── summarize-docs.md       # API docs, readthedocs, developer portals
│   ├── summarize-repo.md       # GitHub/GitLab/Bitbucket repos
│   ├── summarize-transcript.md # YouTube, video transcripts
│   ├── chat.md                 # Telegram Q&A
│   ├── skill-optimizer.md      # meta-skill for critique-then-edit rewrites
│   └── *.md.1 .. *.md.5        # rolling backups written by the nightly optimizer
├── skill-drafts/           # auto-drafted skills awaiting /approve-skill (HITL mode only)
├── skills-registry.json    # skill lifecycle ledger: status, probation counts, approval mode
├── inbox/          # Raw captures pending processing
├── index.md        # Hourly rolling synthesis (~400-500 words)
└── config.yaml     # Shared config (DO NOT put machine-specific settings here)
```

---

## Prerequisites

- macOS (uses iCloud Drive and launchd)
- Python 3.11+
- A [Gemini API key](https://aistudio.google.com/app/apikey) (all nodes)
- An [Anthropic API key](https://console.anthropic.com/) (full node only)
- A Telegram account

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

This copies all seven skill files — `chat.md`, `skill-optimizer.md`, plus the five content-type summarizers (`summarize-webpage`, `summarize-paper`, `summarize-docs`, `summarize-repo`, `summarize-transcript`). The skill router selects the right one for each captured URL automatically; adding a custom specialization later is as simple as dropping a new `summarize-*.md` file into `skills/` and running `./install.sh`.

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

## Running the daemon

### Production (launchd — runs on login, restarts on crash)

After running `./install.sh`, the daemon is configured to start automatically via launchd. Logs go to `~/secondbrain/logs/out.log` and `~/secondbrain/logs/error.log`.

To manually control the daemon:

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist

# Start
launchctl load ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist

# Reload after code changes (or just run ./install.sh)
launchctl unload ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist
launchctl load ~/Library/LaunchAgents/com.chrisrobertson.secondbrain.plist
```

### Dev mode (manual run)

```bash
~/secondbrain/venv/bin/python3 ~/secondbrain/daemon.py
```

You should see log output like:
```
2026-04-11 12:00:00 [second-brain] INFO Starting second-brain daemon — role: full
2026-04-11 12:00:00 [chat-handler] INFO Telegram bot polling started
2026-04-11 12:00:05 [browser-watcher] INFO Browser watcher started
```

Browse to a page, wait up to 5 minutes, then check `$BRAIN/memories/` for a new `.md` file. Message your bot to query it.

### Deploying code changes

After pulling or editing source files, re-run the installer to push changes to the deploy directory and restart the daemon:

```bash
./install.sh
```

The installer is idempotent — it skips unchanged files and only copies what has changed. The daemon is reloaded automatically at the end.

---

## Verifying it works

1. Browse to any article and spend 30+ seconds on it
2. Wait up to 5 minutes for the watcher to poll
3. Check `$BRAIN/memories/` — a new `YYYY-MM-DD-*.md` file should appear
4. Message your Telegram bot with a question related to what you read
5. After an hour, `$BRAIN/index.md` will contain a synthesis of all your memories

Check the daemon log for errors:
```bash
tail -f ~/secondbrain/logs/error.log
tail -f ~/secondbrain/logs/out.log
```

Key log lines to watch for:
- `[calendar-scanner] INFO Calendar scan complete — N event(s) updated`
- `[email-scanner] WARNING Envelope Index unavailable — falling back to AppleScript`
- `[project-scanner] INFO Project scan complete — 29 repos processed`
- `[notification-manager] INFO Daily briefing sent`

---

## Multi-machine setup (watcher node)

The system supports two roles. Set `SECOND_BRAIN_ROLE` in each machine's launchd plist — do NOT set it in `config.yaml` (that file syncs via iCloud and would apply to all machines).

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

The full node picks up memory files written by watcher nodes and indexes them automatically.

---

## LLM routing (LiteLLM)

Create `~/.litellm/config.yaml`:

```yaml
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
```

Routes:
- `summarize` → `gemini/gemini-2.0-flash` (high volume, cheap — browser watcher, scanners)
- `chat` → `claude-sonnet-4-20250514` (Telegram Q&A)
- `optimizer` → `claude-sonnet-4-20250514` (skill optimizer)
- `judge` → `claude-haiku-4-5-20251001` (skill scoring)

---

## Skill optimization and quality

### What the optimizer does

Once per day at 3 AM (configurable via `skill_optimizer.run_hour`), the daemon runs an optimization pass over every skill file. It scores pending execution rows using an LLM-as-judge (Claude Haiku 4.5 by default), then applies a **critique-then-edit** approach to any skill that's underperforming: a first call identifies failure patterns in low-scoring runs; a second call rewrites only the `## Instructions` section to address them. Every rewrite is preceded by a rolling backup (`.1` through `.5`), and if the rewrite makes things worse, the file is automatically restored and the rollback recorded in the skill's `## Evolution Log`. The Evolution Log is append-only — it's the full audit trail of why each version changed.

`skill-optimizer.md` itself is never optimized — it's the meta-skill that drives rewrites, and it's managed manually.

### Content-type routing

The browser watcher classifies each URL before summarizing it. Detection cascades through three layers in priority order:

1. **URL pattern** — domain and path regex (fastest, covers ≈80% of cases)
2. **Content-Type header** — catches PDFs and MIME-typed content the URL alone doesn't identify
3. **Content signals** — substring search in the first 3,000 characters of the page body

| Content type | Skill file | Detected by (examples) |
|---|---|---|
| `research-paper` | `summarize-paper.md` | `arxiv.org`, `doi.org`, `.pdf` extension; "Abstract" + "Methodology" + "References" in body |
| `documentation` | `summarize-docs.md` | `docs.*`, `readthedocs.io`, `developer.*`, `/docs/`, `/api/` in path; API method patterns in body |
| `code-repo` | `summarize-repo.md` | `github.com`, `gitlab.com`, `bitbucket.org` |
| `video-transcript` | `summarize-transcript.md` | `youtube.com`, `youtu.be`, `rev.com`, `vimeo.com`, `/transcript` in path; speaker attribution or timestamps in body |
| `default` | `summarize-webpage.md` | Everything else |

Detection never raises — if all rules fail or an exception occurs, the URL falls back to `summarize-webpage`. If the specialized skill file doesn't exist in `skills/`, the watcher logs a warning and also falls back to the default. The `content_type` value is written to each memory file's frontmatter for downstream filtering.

### Utility scoring and trends

Each skill's execution history is scored with a **recency-weighted utility score** instead of a simple mean. Scores from recent runs contribute more:

| Age of execution | Weight (half-life = 14 days) |
|---|---|
| Today | 1.00 |
| 14 days ago | 0.50 |
| 28 days ago | 0.33 |
| 60 days ago | 0.19 |
| 90 days ago | 0.13 |

The raw `success_rate` (arithmetic mean of all scores) is still stored for reference, but `utility_score` drives all optimization gates.

Skills are also assigned a **trend** by comparing the mean of the 10 most recent scores to the 10 before that:

- `▲` **improving** — recent mean > previous + 0.05
- `▼` **declining** — recent mean < previous − 0.05
- `◆` **stable** — within ±0.05
- `—` **insufficient-data** — fewer than 5 scores in either window

Two gates trigger optimization:
1. **Underperformance** — `utility_score < underperformance_threshold` (default 0.70)
2. **Declining-trend early intervention** — `score_trend == declining` AND `utility_score < 0.80` (catches degradation before it falls below the main threshold)

Skills with fewer than three scored runs have `utility_score: null` and are excluded from all optimization gates.

The `half_life_days` parameter can be overridden per skill in its frontmatter if a skill runs at very high or very low frequency and the global default doesn't reflect its natural cadence.

### Auto-created skills and probation

When the browser watcher encounters a URL whose content type has no skill file, it calls the skill creator. The flow:

1. Daemon generates a seed skill using Claude Sonnet, using `summarize-webpage.md` as the structural template
2. If `skill_creation.require_approval: false` (default): seed is written to `skills/` with `status: probation`
3. **Probation** — the skill runs normally but its output is **not written to memories** for the first `probation_executions` runs (default 5). Execution rows are still appended and scored.
4. After probation, the nightly optimizer runs a graduation check:
   - `utility_score ≥ graduation_utility_threshold` (default 0.6) → `status: active`, Telegram notification: "✓ New skill graduated"
   - Below threshold → pre-graduation rewrite, reset probation, retry (max 3 attempts)
   - After 3 failures → `status: failed`, excluded from routing permanently
5. A rejected content type (`/reject-skill`) is blocked from re-triggering for `rejection_cooldown_hours` (default 24)

If `skill_creation.require_approval: true` (or runtime override `/skill-approval on`): the seed is written to `$BRAIN/skill-drafts/` instead, you receive a Telegram notification, and nothing runs until you `/approve-skill <N>`. See *Managing summarizer quality* above for the full command set.

### Real-time reflection

In addition to the nightly pass, the optimizer runs an **hourly urgent loop** that responds to bad executions within the same hour rather than waiting until 3 AM.

Every skill execution runs through a fast synchronous heuristic pre-filter (zero LLM cost) that flags outputs matching any of these patterns:

| Flag | Condition |
|---|---|
| `too_short` | Output < 100 characters |
| `error_output` | Output contains "I cannot", "I'm unable", "Error:", "Failed to", "I don't have access" |
| `unstructured` | No markdown structure (`#`, `-`, `**`, numbered list) AND length < 300 chars |
| `verbatim_copy` | Output is >90% identical to the input |

Flagged runs land in an in-memory urgent queue (capped at 20 entries). Every 60 minutes, the queue is grouped by skill, and any skill with ≥2 flagged executions triggers an immediate critique-then-edit rewrite (same logic as the nightly pass, capped at 3 skills per hour to limit cost). Memory write is never blocked — the pipeline always completes.

Optional: set `optimizer.realtime_judge: true` to additionally call the judge model on flagged outputs. A score ≤ 2/5 fast-tracks to the queue; a score ≥ 4/5 dismisses the heuristic flag as a false positive. Judge calls are rate-limited to 5 per hour.

### Backups and rollback

Before every rewrite, the optimizer rotates backup files in iCloud `skills/`:

```
summarize-webpage.md          ← current version
summarize-webpage.md.1        ← before last rewrite
summarize-webpage.md.2        ← before that
...
summarize-webpage.md.5        ← oldest backup kept
```

After a rewrite, the optimizer compares the new `utility_score` to the pre-rewrite score on the next daily pass. If the score dropped by more than `regression_tolerance` (default 0.05), it rolls back to `*.md.1` and logs the event in the skill's Evolution Log. To manually restore any backup, rename it over the live file — the next optimizer run picks up the change.

### Dry-run mode

Set `skill_optimizer.dry_run: true` in `config.yaml` to make the optimizer log every proposed change without writing anything. Useful for verifying thresholds or testing a new judge prompt before committing. The flag is hot-reloaded — change it in `config.yaml` and the next daily run picks it up without a daemon restart.

### Configuration

```yaml
skill_optimizer:
  enabled: true                     # master switch (both nightly and hourly loops)
  run_hour: 3                       # hour-of-day for the daily pass (0–23, local time)
  half_life_days: 14                # recency half-life for utility score (override per skill in frontmatter)
  underperformance_threshold: 0.70  # optimize if utility_score < this
  skip_above_threshold: 0.90        # skip if utility_score >= this (working well)
  regression_tolerance: 0.05        # roll back if new utility_score drops by more than this
  min_runs_before_optimize: 10      # minimum scored runs before optimizing
  max_exemplars: 2                  # top-N examples injected as few-shot exemplars
  max_history_rows: 100             # prune execution history beyond this many rows
  max_skill_backups: 5              # rolling backup files to keep (.1 through .N)
  judge_model: judge                # LiteLLM route for scoring (default: Haiku 4.5)
  dry_run: false                    # log changes without writing files
  realtime_judge: false             # enable per-execution judge calls (rate-limited to 5/hr)

skill_creation:
  enabled: true                     # enable gap detection and auto-drafting
  require_approval: false           # HITL — require /approve-skill before new skills run
  probation_executions: 5           # shadow runs before graduation check
  graduation_utility_threshold: 0.6 # minimum utility score to graduate probation
  model_route: chat                 # LiteLLM route for seed generation (claude-sonnet)
  rejection_cooldown_hours: 24      # hours before a rejected content type can re-trigger
  max_graduation_attempts: 3        # skill marked 'failed' after this many failed graduations
```

---

## macOS permissions

### Full Disk Access (for Email Scanner SQLite path)

The email scanner reads Apple Mail.app's Envelope Index database directly for fast, offline access. This requires **Full Disk Access** for `~/secondbrain/venv/bin/python3`.

Grant it in **System Settings → Privacy & Security → Full Disk Access**. Re-run `./install.sh` — it opens System Settings to the FDA pane and a Finder window showing `~/secondbrain/venv/bin/`. **Drag `python3` from that Finder window into the FDA list.** Do not use the + button; it filters for app bundles and rejects plain executables.

**Known limitation:** On macOS Sonoma (and later), ad-hoc signed binaries (e.g., Homebrew Python) can appear in the FDA list but fail silently at runtime. The scanner detects this and falls back to AppleScript. If you see "AppleScript fallback" warnings in logs despite granting FDA, your Python binary is ad-hoc signed. The AppleScript path is slower and requires Mail.app to be running, but works reliably once configured.

If Full Disk Access is not granted, the scanner falls back to AppleScript (requires Mail.app to be running, no conversation threading, slower). A warning is logged at each scan cycle until access is granted.

To force a full re-scan of all email threads (e.g. after granting FDA for the first time), set `full_rescan: true` in `config.yaml` under `email_scanner:`. The flag is automatically cleared after the scan completes.

### Calendar access (for Calendar Scanner EventKit)

The calendar scanner uses EventKit (PyObjC) as the primary path, with fallback to SQLite Calendar Cache and AppleScript.

**Calendar permission** is required for EventKit. Grant in **System Settings → Privacy & Security → Calendars**. The system prompts automatically on first run. If denied, run `tccutil reset Calendar` and restart the daemon.

**Automation permission** for Calendar.app is only needed if the AppleScript fallback is used. Grant in **System Settings → Privacy & Security → Automation** if prompted. The scanner logs a warning if this path is taken without permission.

**Configuration:** Set `skip_calendars: ["Birthdays", "Holidays"]` in `config.yaml` under `calendar_scanner:` to exclude noise calendars. Events in skipped calendars are never written to memory files.

### Automation permission for Mail.app (Email Scanner AppleScript fallback)

If the Email Scanner falls back to AppleScript (due to missing Full Disk Access or ad-hoc signed Python), it needs **Automation permission** for Mail.app. Grant in **System Settings → Privacy & Security → Automation**. The scanner logs a warning until the permission is granted.

---

## Optional integrations

### Zoom Scanner

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

### Slack Scanner

The Slack scanner requires a **Slack bot token** and your **user ID**.

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps)
2. Add bot token scopes: `channels:history`, `groups:history`, `users:read`
3. Install the app to your workspace
4. Copy the **Bot User OAuth Token** (starts with `xoxb-`)
5. Get your user ID: send a message in Slack, right-click your name, "Copy member ID"
6. Run `./install.sh` — it prompts for `SLACK_BOT_TOKEN` and `SLACK_USER_ID` and writes them to the launchd plist

Configure monitored channels in `config.yaml`:

```yaml
slack_scanner:
  channels:
    - engineering
    - product
```

The scanner is skipped gracefully if credentials are missing.

---

## Files never to commit

- `$BRAIN/config.yaml` — contains your bot token and user ID; lives in iCloud only
- `com.chrisrobertson.secondbrain.plist` with real API keys filled in
- `~/.second-brain-seen-urls` — runtime state
