#!/usr/bin/env bash
# install.sh — idempotent setup for second-brain on a new machine
# Safe to run multiple times. Skips steps already completed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$HOME/secondbrain"
VENV="$DEPLOY_DIR/venv"
BRAIN_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/second-brain"
PLIST_NAME="com.chrisrobertson.secondbrain"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
LITELLM_CONFIG="$HOME/.litellm/config.yaml"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { printf "${GREEN}  ✓${NC}  %s\n" "$1"; }
skip() { printf "${YELLOW}  –${NC}  %s\n" "$1"; }
info() { printf "${BLUE}  →${NC}  %s\n" "$1"; }
die()  { printf "\n${RED}  ✗  %s${NC}\n\n" "$1" >&2; exit 1; }

# ── Header ────────────────────────────────────────────────────────────────────
echo ""
echo "  Second Brain — Installer"
echo "  ════════════════════════"
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
echo "Checking prerequisites..."

PYTHON="$(command -v python3 || true)"

_upgrade_python() {
    if command -v brew &>/dev/null; then
        echo ""
        read -r -p "  Install Python 3.13 via Homebrew now? [Y/n]: " CONFIRM
        CONFIRM="${CONFIRM:-Y}"
        if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
            info "Running: brew install python@3.13"
            brew install python@3.13
            PYTHON="$(command -v python3.13 \
                     || brew --prefix python@3.13 2>/dev/null | xargs -I{} echo {}/bin/python3.13 \
                     || command -v python3)"
            ok "Python upgraded via Homebrew"
        else
            die "Python 3.11+ is required. Install it and re-run."
        fi
    else
        echo ""
        echo "  Install options:"
        echo "    Homebrew (recommended):  brew install python@3.13"
        echo "    Direct download:         https://www.python.org/downloads/"
        echo ""
        die "Python 3.11+ required. Install it and re-run."
    fi
}

if [ -z "$PYTHON" ]; then
    printf "${YELLOW}  !${NC}  python3 not found.\n"
    _upgrade_python
fi

PY_MAJOR="$("$PYTHON" -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    printf "${YELLOW}  !${NC}  Python 3.11+ required, found $PY_MAJOR.$PY_MINOR.\n"
    _upgrade_python
    PY_MAJOR="$("$PYTHON" -c 'import sys; print(sys.version_info.major)')"
    PY_MINOR="$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')"
    { [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; } && \
        die "Still on Python $PY_MAJOR.$PY_MINOR after upgrade attempt. Fix manually and re-run."
fi
ok "Python $PY_MAJOR.$PY_MINOR"

ICLOUD_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
[ ! -d "$ICLOUD_ROOT" ] && die "iCloud Drive not found. Enable iCloud Drive in System Settings → Apple ID first."
ok "iCloud Drive"

# ── 2. Role ───────────────────────────────────────────────────────────────────
echo ""
echo "  Deployment role:"
echo "    watcher  browser watcher only — captures pages you read (laptop)"
echo "    full     watcher + Telegram bot + index builder (always-on machine)"
echo ""
read -r -p "  Role for this machine [watcher]: " ROLE
ROLE="${ROLE:-watcher}"
[[ "$ROLE" == "full" || "$ROLE" == "watcher" ]] || die "Role must be 'full' or 'watcher'."
ok "Role: $ROLE"

# ── 3. API keys ───────────────────────────────────────────────────────────────
echo ""
echo "API keys"
echo "────────"

if [ -n "${GEMINI_API_KEY:-}" ]; then
    ok "GEMINI_API_KEY (from environment)"
    GEMINI_KEY="$GEMINI_API_KEY"
else
    echo "  Get one at: https://aistudio.google.com/app/apikey"
    read -r -p "  Gemini API key: " GEMINI_KEY
    [ -z "$GEMINI_KEY" ] && die "Gemini API key is required."
fi

ANTHROPIC_KEY=""
if [ "$ROLE" = "full" ]; then
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        ok "ANTHROPIC_API_KEY (from environment)"
        ANTHROPIC_KEY="$ANTHROPIC_API_KEY"
    else
        echo "  Get one at: https://console.anthropic.com/"
        read -r -p "  Anthropic API key: " ANTHROPIC_KEY
        [ -z "$ANTHROPIC_KEY" ] && die "Anthropic API key is required for the full role."
    fi
fi

# ── 4. Telegram (full role only) ──────────────────────────────────────────────
TELEGRAM_TOKEN=""
TELEGRAM_USER_ID=""
if [ "$ROLE" = "full" ]; then
    echo ""
    echo "Telegram"
    echo "────────"
    echo "  Bot token:  message @BotFather → /newbot"
    echo "  User ID:    message @userinfobot"
    echo ""
    read -r -p "  Bot token: " TELEGRAM_TOKEN
    [ -z "$TELEGRAM_TOKEN" ] && die "Telegram bot token is required for the full role."
    read -r -p "  Your numeric user ID: " TELEGRAM_USER_ID
    [ -z "$TELEGRAM_USER_ID" ] && die "Telegram user ID is required for the full role."
fi

# ── 5. Deploy directory ───────────────────────────────────────────────────────
echo ""
echo "Setting up deploy directory..."

mkdir -p "$DEPLOY_DIR/logs"
ok "$DEPLOY_DIR"

# ── 6. Python virtual environment ─────────────────────────────────────────────
echo ""
echo "Setting up Python virtual environment..."

if [ -f "$VENV/bin/python3" ]; then
    VENV_MAJOR="$("$VENV/bin/python3" -c 'import sys; print(sys.version_info.major)')"
    VENV_MINOR="$("$VENV/bin/python3" -c 'import sys; print(sys.version_info.minor)')"
    if [ "$VENV_MAJOR" = "$PY_MAJOR" ] && [ "$VENV_MINOR" = "$PY_MINOR" ]; then
        skip "$VENV  (Python $VENV_MAJOR.$VENV_MINOR)"
    else
        info "Recreating venv (was $VENV_MAJOR.$VENV_MINOR → $PY_MAJOR.$PY_MINOR)"
        rm -rf "$VENV"
        "$PYTHON" -m venv "$VENV"
        ok "Venv recreated at $VENV  (Python $PY_MAJOR.$PY_MINOR)"
    fi
else
    "$PYTHON" -m venv "$VENV"
    ok "Venv created at $VENV  (Python $PY_MAJOR.$PY_MINOR)"
fi

# ── 7. Python dependencies ────────────────────────────────────────────────────
echo ""
echo "Installing Python dependencies into venv..."
"$VENV/bin/pip" install -q --upgrade pip
if [ "$ROLE" = "watcher" ]; then
    # python-telegram-bot not needed on watcher nodes — keep the venv lean
    "$VENV/bin/pip" install -q litellm httpx beautifulsoup4 lxml pyyaml
else
    "$VENV/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
fi
ok "Dependencies installed"

# ── 8. Deploy source files ────────────────────────────────────────────────────
echo ""
echo "Deploying source files to $DEPLOY_DIR..."

# Daemon source files — only the modules the daemon actually imports.
# Tests, migration scripts, and repo tooling stay in the repo.
DAEMON_FILES=(
    daemon.py
    browser_watcher.py
    chat_handler.py
    email_scanner.py
    index_builder.py
    memory_writer.py
    project_scanner.py
    skill_executor.py
    skill_optimizer.py
    utils.py
)

deployed=0
for FILE in "${DAEMON_FILES[@]}"; do
    SRC="$REPO_DIR/$FILE"
    DST="$DEPLOY_DIR/$FILE"
    if [ ! -f "$SRC" ]; then
        skip "$FILE  (not found in repo — skipping)"
        continue
    fi
    if [ -f "$DST" ] && cmp -s "$SRC" "$DST"; then
        skip "$FILE  (unchanged)"
    else
        cp "$SRC" "$DST"
        ok "$FILE"
        deployed=$((deployed + 1))
    fi
done
[ "$deployed" -eq 0 ] && info "All source files already up to date" || ok "$deployed file(s) deployed"

# ── 10. iCloud directory structure ────────────────────────────────────────────
echo ""
echo "Setting up iCloud directories..."
for DIR in memories skills inbox; do
    TARGET="$BRAIN_DIR/$DIR"
    if [ -d "$TARGET" ]; then
        skip "$TARGET"
    else
        mkdir -p "$TARGET"
        ok "Created $TARGET"
    fi
done

# ── 11. config.yaml ───────────────────────────────────────────────────────────
echo ""
echo "Writing config.yaml..."
CONFIG_DEST="$BRAIN_DIR/config.yaml"
if [ -f "$CONFIG_DEST" ]; then
    skip "$CONFIG_DEST  (preserving existing — edit manually to change settings)"
else
    cp "$REPO_DIR/config.yaml.template" "$CONFIG_DEST"
    # Use Python for substitution — avoids sed escaping issues with special chars in tokens
    ROLE="$ROLE" \
    TELEGRAM_TOKEN="$TELEGRAM_TOKEN" \
    TELEGRAM_USER_ID="$TELEGRAM_USER_ID" \
    "$VENV/bin/python3" - "$CONFIG_DEST" << 'PYEOF'
import os, re, sys
path = sys.argv[1]
text = open(path).read()
text = re.sub(r'(?m)^  role: \w+', '  role: ' + os.environ['ROLE'], text)
if os.environ.get('TELEGRAM_TOKEN'):
    text = text.replace('YOUR_BOT_TOKEN', os.environ['TELEGRAM_TOKEN'])
if os.environ.get('TELEGRAM_USER_ID'):
    text = text.replace('YOUR_TELEGRAM_USER_ID', os.environ['TELEGRAM_USER_ID'])
open(path, 'w').write(text)
PYEOF
    ok "Created $CONFIG_DEST"
fi

# ── 12. Skill files ───────────────────────────────────────────────────────────
echo ""
echo "Installing skill files..."
for SKILL in "$REPO_DIR/skills/"*.md; do
    DEST="$BRAIN_DIR/skills/$(basename "$SKILL")"
    if [ -f "$DEST" ]; then
        skip "$(basename "$SKILL")  (preserving existing — may contain execution history)"
    else
        cp "$SKILL" "$DEST"
        ok "Copied $(basename "$SKILL")"
    fi
done

# ── 13. LiteLLM config ────────────────────────────────────────────────────────
echo ""
echo "Setting up LiteLLM config..."
if [ -f "$LITELLM_CONFIG" ]; then
    skip "$LITELLM_CONFIG"
else
    mkdir -p "$(dirname "$LITELLM_CONFIG")"
    cat > "$LITELLM_CONFIG" << 'LITELLM_EOF'
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
  fallbacks:
    - summarize: [local]
    - chat: [local]
LITELLM_EOF
    ok "Created $LITELLM_CONFIG"
fi

# ── 14. launchd plist ─────────────────────────────────────────────────────────
echo ""
echo "Configuring launchd agent..."

# Always write — picks up role/key/path changes on re-run
cat > "$PLIST_DEST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_NAME}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VENV}/bin/python3</string>
    <string>${DEPLOY_DIR}/daemon.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${DEPLOY_DIR}/logs/out.log</string>
  <key>StandardErrorPath</key>
  <string>${DEPLOY_DIR}/logs/error.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SECOND_BRAIN_ROLE</key>
    <string>${ROLE}</string>
    <key>SECOND_BRAIN_DIR</key>
    <string>${DEPLOY_DIR}</string>
    <key>GEMINI_API_KEY</key>
    <string>${GEMINI_KEY}</string>
    <key>ANTHROPIC_API_KEY</key>
    <string>${ANTHROPIC_KEY}</string>
  </dict>
</dict>
</plist>
PLIST_EOF
ok "Wrote $PLIST_DEST"

# ── 15. Load (or reload) the agent ────────────────────────────────────────────
if launchctl list "$PLIST_NAME" &>/dev/null; then
    info "Reloading existing agent..."
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi
launchctl load "$PLIST_DEST"
ok "launchd agent loaded"

# ── 16. Full Disk Access check (full role only) ───────────────────────────────
if [ "$ROLE" = "full" ]; then
    echo ""
    echo "Checking Full Disk Access for email scanner..."

    FDA_OK=false
    if "$VENV/bin/python3" -c \
        "import os, sys; os.listdir(os.path.expanduser('~/Library/Mail/')); sys.exit(0)" \
        2>/dev/null; then
        FDA_OK=true
    fi

    if [ "$FDA_OK" = "true" ]; then
        ok "Full Disk Access granted — email scanner can read Mail.app data"
    else
        printf "${YELLOW}  !${NC}  Full Disk Access not granted.\n"
        echo ""
        echo "  The email scanner reads Apple Mail.app data via SQLite."
        echo "  Without Full Disk Access it falls back to AppleScript"
        echo "  (slower, no conversation threading, requires Mail.app open)."
        echo ""
        echo "  To grant access:"
        echo "    1. System Settings will open to Privacy & Security → Full Disk Access"
        echo "    2. Click the + button and add:"
        printf "       %s\n" "$VENV/bin/python3"
        echo "    3. Re-run ./install.sh to verify"
        echo ""
        read -r -p "  Open System Settings → Full Disk Access now? [Y/n]: " OPEN_FDA
        OPEN_FDA="${OPEN_FDA:-Y}"
        if [[ "$OPEN_FDA" =~ ^[Yy]$ ]]; then
            open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
            echo ""
            printf "${YELLOW}  →${NC}  Add %s in the Full Disk Access list,\n" "$VENV/bin/python3"
            echo "     then re-run ./install.sh to reload the daemon with access."
        else
            info "Skipped — re-run ./install.sh after granting access to reload the daemon"
        fi
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete"
echo ""
echo "  Logs     tail -f $DEPLOY_DIR/logs/out.log"
echo "  Stop     launchctl unload \"$PLIST_DEST\""
echo "  Restart  launchctl load   \"$PLIST_DEST\""
echo ""
if [ "$ROLE" = "watcher" ]; then
    echo "  This machine captures browser history and writes memory"
    echo "  files to iCloud. No Telegram bot runs on this node."
else
    echo "  Browse to any article, wait ~5 minutes, then check:"
    printf "  %s/memories/\n" "$BRAIN_DIR"
    echo ""
    echo "  Message your Telegram bot to query your memories."
    echo "  After one hour, check index.md for a synthesis."
fi
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
