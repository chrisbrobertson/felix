#!/usr/bin/env bash
# install.sh — idempotent setup for second-brain on a new machine
# Safe to run multiple times. Reads existing configuration and skips
# steps that are already up to date.
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

# SYS_PYTHON: used to create/recreate the venv — must never point inside the venv
# (the venv binary can't recreate itself after rm -rf).
SYS_PYTHON="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"

# For all other operations prefer the venv Python (idempotent re-runs)
if [ -x "$VENV/bin/python3" ]; then
    PYTHON="$VENV/bin/python3"
else
    PYTHON="$SYS_PYTHON"
fi

_upgrade_python() {
    if command -v brew &>/dev/null; then
        echo ""
        read -r -p "  Install Python 3.13 via Homebrew now? [Y/n]: " CONFIRM
        CONFIRM="${CONFIRM:-Y}"
        if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
            info "Running: brew install python@3.13"
            brew install python@3.13
            SYS_PYTHON="$(command -v python3.13 \
                         || brew --prefix python@3.13 2>/dev/null | xargs -I{} echo {}/bin/python3.13 \
                         || command -v python3)"
            PYTHON="$SYS_PYTHON"
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

if [ -z "$SYS_PYTHON" ]; then
    printf "${YELLOW}  !${NC}  python3 not found.\n"
    _upgrade_python
fi

PY_MAJOR="$("$SYS_PYTHON" -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$("$SYS_PYTHON" -c 'import sys; print(sys.version_info.minor)')"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    printf "${YELLOW}  !${NC}  Python 3.11+ required, found $PY_MAJOR.$PY_MINOR.\n"
    _upgrade_python
    PY_MAJOR="$("$SYS_PYTHON" -c 'import sys; print(sys.version_info.major)')"
    PY_MINOR="$("$SYS_PYTHON" -c 'import sys; print(sys.version_info.minor)')"
    { [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; } && \
        die "Still on Python $PY_MAJOR.$PY_MINOR after upgrade attempt. Fix manually and re-run."
fi
ok "Python $PY_MAJOR.$PY_MINOR"

ICLOUD_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
[ ! -d "$ICLOUD_ROOT" ] && die "iCloud Drive not found. Enable iCloud Drive in System Settings → Apple ID first."
ok "iCloud Drive"

# ── Read existing configuration ───────────────────────────────────────────────
# Detect values already stored in the launchd plist and config.yaml so
# re-runs don't ask for credentials that are already configured.

_plist_env_val() {
    # Read an EnvironmentVariables key from the plist using plistlib (stdlib).
    local key="$1"
    "$PYTHON" - "$PLIST_DEST" "$key" 2>/dev/null << 'PYEOF'
import plistlib, sys
try:
    with open(sys.argv[1], 'rb') as f:
        p = plistlib.load(f)
    val = p.get('EnvironmentVariables', {}).get(sys.argv[2], '')
    print(val if val else '', end='')
except Exception:
    pass
PYEOF
}

_config_yaml_val() {
    # Read a dotted key path (e.g. telegram.bot_token) from config.yaml via awk.
    # Avoids requiring pyyaml in the system Python.
    local section="$1" key="$2" config="$3"
    awk -v sec="$section" -v k="$key" '
        /^[^ ]/ { in_sec = ($0 ~ "^" sec ":") }
        in_sec && /^  / {
            split($0, a, /: */)
            gsub(/^ +| +$/, "", a[1])
            gsub(/^ +| +$/, "", a[2])
            gsub(/^["'"'"']|["'"'"']$/, "", a[2])
            if (a[1] == k) { print a[2]; exit }
        }
    ' "$config" 2>/dev/null || echo ""
}

EXISTING_ROLE=""
EXISTING_GEMINI_KEY=""
EXISTING_ANTHROPIC_KEY=""
EXISTING_TELEGRAM_TOKEN=""
EXISTING_TELEGRAM_USER_ID=""

if [ -f "$PLIST_DEST" ]; then
    EXISTING_ROLE="$(_plist_env_val SECOND_BRAIN_ROLE)"
    EXISTING_GEMINI_KEY="$(_plist_env_val GEMINI_API_KEY)"
    EXISTING_ANTHROPIC_KEY="$(_plist_env_val ANTHROPIC_API_KEY)"
fi

CONFIG_DEST="$BRAIN_DIR/config.yaml"
if [ -f "$CONFIG_DEST" ]; then
    _tok="$(_config_yaml_val telegram bot_token "$CONFIG_DEST")"
    _uid="$(_config_yaml_val user telegram_user_id "$CONFIG_DEST")"
    # Only use if they look like real values, not the template placeholders
    [[ "$_tok" != "YOUR_BOT_TOKEN" && -n "$_tok" ]] && EXISTING_TELEGRAM_TOKEN="$_tok"
    [[ "$_uid" != "YOUR_TELEGRAM_USER_ID" && -n "$_uid" ]] && EXISTING_TELEGRAM_USER_ID="$_uid"
fi

# ── 2. Role ───────────────────────────────────────────────────────────────────
echo ""
if [ -n "$EXISTING_ROLE" ]; then
    ROLE="$EXISTING_ROLE"
    skip "Role: $ROLE  (from existing config — press Enter to keep, or type to change)"
    read -r -p "  Role [$ROLE]: " NEW_ROLE
    [ -n "$NEW_ROLE" ] && ROLE="$NEW_ROLE"
    [[ "$ROLE" == "full" || "$ROLE" == "watcher" ]] || die "Role must be 'full' or 'watcher'."
    ok "Role: $ROLE"
else
    echo "  Deployment role:"
    echo "    watcher  browser watcher only — captures pages you read (laptop)"
    echo "    full     watcher + Telegram bot + index builder (always-on machine)"
    echo ""
    read -r -p "  Role for this machine [watcher]: " ROLE
    ROLE="${ROLE:-watcher}"
    [[ "$ROLE" == "full" || "$ROLE" == "watcher" ]] || die "Role must be 'full' or 'watcher'."
    ok "Role: $ROLE"
fi

# ── 3. API keys ───────────────────────────────────────────────────────────────
echo ""
echo "API keys"
echo "────────"

if [ -n "${GEMINI_API_KEY:-}" ]; then
    ok "GEMINI_API_KEY (from environment)"
    GEMINI_KEY="$GEMINI_API_KEY"
elif [ -n "$EXISTING_GEMINI_KEY" ]; then
    ok "Gemini API key (from existing config)"
    GEMINI_KEY="$EXISTING_GEMINI_KEY"
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
    elif [ -n "$EXISTING_ANTHROPIC_KEY" ]; then
        ok "Anthropic API key (from existing config)"
        ANTHROPIC_KEY="$EXISTING_ANTHROPIC_KEY"
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
    if [ -n "$EXISTING_TELEGRAM_TOKEN" ] && [ -n "$EXISTING_TELEGRAM_USER_ID" ]; then
        ok "Telegram bot token (from existing config)"
        ok "Telegram user ID: $EXISTING_TELEGRAM_USER_ID (from existing config)"
        TELEGRAM_TOKEN="$EXISTING_TELEGRAM_TOKEN"
        TELEGRAM_USER_ID="$EXISTING_TELEGRAM_USER_ID"
    else
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
    # --copies is required so ~/secondbrain/venv/bin/python3 is a real executable
    # that macOS FDA drag-and-drop accepts. Symlinks into .framework are rejected.
    if [ "$VENV_MAJOR" = "$PY_MAJOR" ] && [ "$VENV_MINOR" = "$PY_MINOR" ] && \
       [ ! -L "$VENV/bin/python3" ]; then
        skip "$VENV  (Python $VENV_MAJOR.$VENV_MINOR)"
    elif [ "$VENV_MAJOR" != "$PY_MAJOR" ] || [ "$VENV_MINOR" != "$PY_MINOR" ]; then
        info "Recreating venv (was $VENV_MAJOR.$VENV_MINOR → $PY_MAJOR.$PY_MINOR)"
        rm -rf "$VENV" "$DEPLOY_DIR/.requirements-hash"
        "$SYS_PYTHON" -m venv --copies "$VENV"
        PYTHON="$VENV/bin/python3"
        ok "Venv recreated at $VENV  (Python $PY_MAJOR.$PY_MINOR)"
    else
        info "Recreating venv with --copies (needed for Full Disk Access)"
        rm -rf "$VENV" "$DEPLOY_DIR/.requirements-hash"
        "$SYS_PYTHON" -m venv --copies "$VENV"
        PYTHON="$VENV/bin/python3"
        ok "Venv recreated at $VENV  (Python $PY_MAJOR.$PY_MINOR)"
    fi
else
    "$SYS_PYTHON" -m venv --copies "$VENV"
    PYTHON="$VENV/bin/python3"
    ok "Venv created at $VENV  (Python $PY_MAJOR.$PY_MINOR)"
fi

# ── 7. Python dependencies ────────────────────────────────────────────────────
echo ""
echo "Checking Python dependencies..."

REQS_HASH_FILE="$DEPLOY_DIR/.requirements-hash"
if [ "$ROLE" = "watcher" ]; then
    # Fixed set for watcher — hash the package names directly
    REQS_HASH="$(echo 'litellm httpx beautifulsoup4 lxml pyyaml' | shasum -a 256 | cut -d' ' -f1)"
else
    REQS_HASH="$(shasum -a 256 "$REPO_DIR/requirements.txt" | cut -d' ' -f1)"
fi

if [ -f "$REQS_HASH_FILE" ] && [ "$(cat "$REQS_HASH_FILE")" = "$REQS_HASH" ]; then
    skip "Dependencies unchanged"
else
    info "Installing dependencies..."
    "$VENV/bin/pip" install -q --upgrade pip
    if [ "$ROLE" = "watcher" ]; then
        "$VENV/bin/pip" install -q litellm httpx beautifulsoup4 lxml pyyaml
    else
        "$VENV/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
    fi
    echo "$REQS_HASH" > "$REQS_HASH_FILE"
    ok "Dependencies installed"
fi

# ── 8. Deploy source files ────────────────────────────────────────────────────
echo ""
echo "Deploying source files to $DEPLOY_DIR..."

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
if [ -f "$CONFIG_DEST" ]; then
    skip "$CONFIG_DEST  (preserving existing — edit manually to change settings)"
else
    cp "$REPO_DIR/config.yaml.template" "$CONFIG_DEST"
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

PLIST_TMP="$(mktemp)"
cat > "$PLIST_TMP" << PLIST_EOF
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

PLIST_CHANGED=false
if [ -f "$PLIST_DEST" ] && cmp -s "$PLIST_TMP" "$PLIST_DEST"; then
    skip "launchd plist unchanged"
    rm "$PLIST_TMP"
else
    mv "$PLIST_TMP" "$PLIST_DEST"
    ok "Wrote $PLIST_DEST"
    PLIST_CHANGED=true
fi

# ── 15. Load (or reload) the agent ────────────────────────────────────────────
NEEDS_RELOAD=false
[ "$deployed" -gt 0 ] && NEEDS_RELOAD=true
[ "$PLIST_CHANGED" = "true" ] && NEEDS_RELOAD=true

if [ "$NEEDS_RELOAD" = "true" ]; then
    if launchctl list "$PLIST_NAME" &>/dev/null; then
        info "Reloading daemon (source or config changed)..."
        launchctl unload "$PLIST_DEST" 2>/dev/null || true
    fi
    launchctl load "$PLIST_DEST"
    ok "Daemon reloaded"
else
    if launchctl list "$PLIST_NAME" &>/dev/null; then
        skip "Daemon already running — nothing changed, no reload needed"
    else
        launchctl load "$PLIST_DEST"
        ok "Daemon loaded"
    fi
fi

# ── 16. Full Disk Access check (full role only) ───────────────────────────────
if [ "$ROLE" = "full" ]; then
    echo ""
    echo "Checking Full Disk Access for email scanner..."

    # NOTE: We cannot reliably test FDA from within the installer. A subprocess
    # spawned by Terminal inherits Terminal's responsible-process chain, so it
    # gets Terminal's FDA status — not the binary's own grant. The daemon (run
    # by launchd) is the true test: it runs the binary directly and will have
    # FDA if the binary was added to the list.
    #
    # Instead, check whether the Envelope Index is accessible from this process.
    # If this terminal has FDA (e.g. Terminal.app is in the FDA list), we can
    # confirm. If not, we show instructions and trust that launchd will have access.

    ENVELOPE_INDEX="$(ls "$HOME/Library/Mail"/V*/Envelope\ Index 2>/dev/null | sort -V | tail -1)"

    if [ -n "$ENVELOPE_INDEX" ] && [ -r "$ENVELOPE_INDEX" ]; then
        ok "Envelope Index readable — Full Disk Access confirmed"
    elif [ -n "$ENVELOPE_INDEX" ]; then
        # Path exists but not readable from this terminal — may still work from launchd
        printf "${YELLOW}  –${NC}  Envelope Index found but not readable from this terminal.\n"
        echo "     If you already added python3 to Full Disk Access, the daemon"
        echo "     (run by launchd) will have access — check the logs after restart:"
        echo "       tail -f ~/secondbrain/logs/error.log | grep email"
        echo "     If no FDA yet, grant it now:"
        echo "       System Settings → Privacy & Security → Full Disk Access"
        echo "       Drag $VENV/bin/python3 into the list"
    else
        printf "${YELLOW}  –${NC}  No Envelope Index found — Mail.app may not be set up.\n"
        echo "     The email scanner will use the AppleScript fallback (requires Mail.app open)."
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
