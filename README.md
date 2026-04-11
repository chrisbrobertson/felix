# Second Brain

Automatically captures and summarizes everything you read on the web. Stores summaries as flat markdown files in iCloud Drive. Query your accumulated knowledge through a Telegram bot.

**Philosophy:** No vector DB, no embeddings, no graph. Files + LLM = database.

---

## How it works

A daemon runs four async loops:

1. **Browser Watcher** — polls Chrome/Firefox history every 5 minutes, fetches pages you spent time on, summarizes them with Gemini Flash, writes a `.md` file to iCloud
2. **Telegram Bot** — answers questions about what you've read by loading relevant memory files into context
3. **Index Builder** — rebuilds a rolling 400-word synthesis of all your memories every hour
4. **Skill Optimizer** — nightly pass that rewrites underperforming prompt templates (v0.1 stub)

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

router_settings:
  num_retries: 2
EOF
```

### 7. Install dependencies

```bash
pip install -r requirements.txt
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

### Manual (dev / first run)

```bash
python3 daemon.py
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

## Multi-machine setup

The system supports two roles. Set `SECOND_BRAIN_ROLE` in each machine's launchd plist — do not set it in `config.yaml` (that file syncs via iCloud and would apply to all machines).

| Role | What runs | API keys needed |
|------|-----------|-----------------|
| `full` | All four loops | `GEMINI_API_KEY` + `ANTHROPIC_API_KEY` |
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
tail -f /tmp/second-brain.log
tail -f /tmp/second-brain.error.log
```

---

## Files never to commit

- `$BRAIN/config.yaml` — contains your bot token and user ID; lives in iCloud only
- `com.chrisrobertson.secondbrain.plist` with real API keys filled in
- `~/.second-brain-seen-urls` — runtime state
