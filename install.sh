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
VERSION=$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo "unknown")
echo ""
echo "  Second Brain — Installer  (v$VERSION)"
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
EXISTING_PROVIDER=""
EXISTING_GEMINI_KEY=""
EXISTING_ANTHROPIC_KEY=""
EXISTING_TELEGRAM_TOKEN=""
EXISTING_TELEGRAM_USER_ID=""
EXISTING_ZOOM_ACCOUNT_ID=""
EXISTING_ZOOM_CLIENT_ID=""
EXISTING_ZOOM_CLIENT_SECRET=""
EXISTING_SLACK_USER_TOKEN=""
EXISTING_SLACK_BOT_TOKEN=""
EXISTING_GITHUB_PAT=""
EXISTING_GITHUB_REPO=""

if [ -f "$PLIST_DEST" ]; then
    EXISTING_ROLE="$(_plist_env_val SECOND_BRAIN_ROLE)"
    EXISTING_PROVIDER="$(_plist_env_val SECOND_BRAIN_PROVIDER)"
    EXISTING_GEMINI_KEY="$(_plist_env_val GEMINI_API_KEY)"
    EXISTING_ANTHROPIC_KEY="$(_plist_env_val ANTHROPIC_API_KEY)"
    EXISTING_ZOOM_ACCOUNT_ID="$(_plist_env_val ZOOM_ACCOUNT_ID)"
    EXISTING_ZOOM_CLIENT_ID="$(_plist_env_val ZOOM_CLIENT_ID)"
    EXISTING_ZOOM_CLIENT_SECRET="$(_plist_env_val ZOOM_CLIENT_SECRET)"
    EXISTING_SLACK_USER_TOKEN="$(_plist_env_val SLACK_USER_TOKEN)"
    EXISTING_SLACK_BOT_TOKEN="$(_plist_env_val SLACK_BOT_TOKEN)"
    EXISTING_GITHUB_PAT="$(_plist_env_val GITHUB_PAT)"
    EXISTING_GITHUB_REPO="$(_plist_env_val GITHUB_REPO)"
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

# ── 3. LLM provider choice ────────────────────────────────────────────────────
echo ""
echo "LLM provider"
echo "────────────"
printf "  Which LLM providers do you want this daemon to use?\n"
printf "    gemini  Gemini only (cheap, fast; no Anthropic key needed)\n"
printf "    claude  Claude only (higher quality; no Gemini key needed)\n"
printf "    both    Prefer Claude, fall back to Gemini on errors (recommended)\n"
echo ""
DEFAULT_PROVIDER="${EXISTING_PROVIDER:-both}"
read -r -p "  Provider [$DEFAULT_PROVIDER]: " PROVIDER
PROVIDER="${PROVIDER:-$DEFAULT_PROVIDER}"
[[ "$PROVIDER" == "gemini" || "$PROVIDER" == "claude" || "$PROVIDER" == "both" ]] \
    || die "Provider must be 'gemini', 'claude', or 'both'."
ok "Provider: $PROVIDER"

# ── 4. API keys ───────────────────────────────────────────────────────────────
echo ""
echo "API keys"
echo "────────"

GEMINI_KEY=""
if [ "$PROVIDER" != "claude" ]; then
    if [ -n "${GEMINI_API_KEY:-}" ]; then
        ok "GEMINI_API_KEY (from environment)"
        GEMINI_KEY="$GEMINI_API_KEY"
    elif [ -n "$EXISTING_GEMINI_KEY" ]; then
        ok "Gemini API key (from existing config)"
        GEMINI_KEY="$EXISTING_GEMINI_KEY"
    else
        echo "  Get one at: https://aistudio.google.com/app/apikey"
        read -r -p "  Gemini API key: " GEMINI_KEY
        [ -z "$GEMINI_KEY" ] && die "Gemini API key is required for provider '$PROVIDER'."
    fi
fi

ANTHROPIC_KEY=""
if [ "$PROVIDER" != "gemini" ]; then
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        ok "ANTHROPIC_API_KEY (from environment)"
        ANTHROPIC_KEY="$ANTHROPIC_API_KEY"
    elif [ -n "$EXISTING_ANTHROPIC_KEY" ]; then
        ok "Anthropic API key (from existing config)"
        ANTHROPIC_KEY="$EXISTING_ANTHROPIC_KEY"
    else
        echo "  Get one at: https://console.anthropic.com/"
        read -r -p "  Anthropic API key: " ANTHROPIC_KEY
        [ -z "$ANTHROPIC_KEY" ] && die "Anthropic API key is required for provider '$PROVIDER'."
    fi
fi

# ── 4. Zoom credentials (full role only, optional) ────────────────────────────
ZOOM_ACCOUNT_ID=""
ZOOM_CLIENT_ID=""
ZOOM_CLIENT_SECRET=""
if [ "$ROLE" = "full" ]; then
    if [ -n "$EXISTING_ZOOM_ACCOUNT_ID" ] && [ -n "$EXISTING_ZOOM_CLIENT_ID" ]; then
        ok "Zoom credentials (from existing config)"
        ZOOM_ACCOUNT_ID="$EXISTING_ZOOM_ACCOUNT_ID"
        ZOOM_CLIENT_ID="$EXISTING_ZOOM_CLIENT_ID"
        ZOOM_CLIENT_SECRET="$EXISTING_ZOOM_CLIENT_SECRET"
    elif [ -n "${ZOOM_ACCOUNT_ID:-}" ] && [ -n "${ZOOM_CLIENT_ID:-}" ]; then
        ok "Zoom credentials (from environment)"
        ZOOM_CLIENT_SECRET="${ZOOM_CLIENT_SECRET:-}"
    else
        echo ""
        echo "Zoom (optional — leave blank to skip Zoom transcript scanning)"
        echo "────────────────────────────────────────────────────────────────"
        echo "  Create a Server-to-Server OAuth app at https://marketplace.zoom.us/"
        echo "  Required scopes: recording:read:admin, meeting:read:admin, user:read:admin"
        echo ""
        read -r -p "  Zoom Account ID (Enter to skip): " ZOOM_ACCOUNT_ID
        if [ -n "$ZOOM_ACCOUNT_ID" ]; then
            read -r -p "  Zoom Client ID: " ZOOM_CLIENT_ID
            read -r -p "  Zoom Client Secret: " ZOOM_CLIENT_SECRET
            ok "Zoom credentials configured"
        else
            skip "Zoom credentials skipped — transcript scanning disabled"
        fi
    fi
fi

# ── 4b. Telegram (full role only) ─────────────────────────────────────────────
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

# ── 4c. Slack credentials (optional — both roles) ─────────────────────────────
SLACK_USER_TOKEN=""
# Migration detection: if old bot token exists but no user token, force re-prompt
FORCE_SLACK_PROMPT=false
if [ -n "$EXISTING_SLACK_BOT_TOKEN" ] && [ -z "$EXISTING_SLACK_USER_TOKEN" ]; then
    printf "${YELLOW}  ⚠${NC}  Slack scanner now uses a user token (xoxp-...) instead of a bot token. Re-prompting.\n"
    FORCE_SLACK_PROMPT=true
fi

if [ "$FORCE_SLACK_PROMPT" = "false" ] && [ -n "$EXISTING_SLACK_USER_TOKEN" ]; then
    ok "Slack user token (from existing config)"
    SLACK_USER_TOKEN="$EXISTING_SLACK_USER_TOKEN"
elif [ "$FORCE_SLACK_PROMPT" = "false" ] && [ -n "${SLACK_USER_TOKEN:-}" ]; then
    ok "Slack user token (from environment)"
else
    echo ""
    echo "Slack (optional — leave blank to skip Slack thread scanning)"
    echo "────────────────────────────────────────────────────────────"
    echo "  Create a Slack app at https://api.slack.com/apps"
    echo "  Required user scopes: channels:history, channels:read, groups:history,"
    echo "                        groups:read, users:read"
    echo ""
    read -r -p "  Slack User Token (xoxp-..., Enter to skip): " SLACK_USER_TOKEN
    if [ -n "$SLACK_USER_TOKEN" ]; then
        ok "Slack credentials configured"
    else
        skip "Slack credentials skipped — thread scanning disabled"
    fi
fi

# ── 4d. GitHub credentials (full role only, optional) ─────────────────────────
GITHUB_PAT=""
GITHUB_REPO=""
if [ "$ROLE" = "full" ]; then
    if [ -n "$EXISTING_GITHUB_PAT" ] && [ -n "$EXISTING_GITHUB_REPO" ]; then
        ok "GitHub credentials (from existing config)"
        GITHUB_PAT="$EXISTING_GITHUB_PAT"
        GITHUB_REPO="$EXISTING_GITHUB_REPO"
    elif [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
        ok "GitHub credentials (from environment)"
    else
        echo ""
        echo "GitHub (optional — leave blank to keep using local files)"
        echo "────────────────────────────────────────────────────────"
        echo "  Create a Personal Access Token at https://github.com/settings/tokens"
        echo "  Required scope: repo (full control of private repositories)"
        echo ""
        read -r -p "  Personal access token with 'repo' scope (Enter to skip): " GITHUB_PAT
        if [ -n "$GITHUB_PAT" ]; then
            read -r -p "  Repository (owner/name, e.g. chrisrobertson/secondbrain): " GITHUB_REPO
            ok "GitHub credentials configured"
        else
            skip "GitHub credentials skipped — /feature and /bug will use local files"
        fi
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

VENV_RECREATED=0
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
        VENV_RECREATED=1
        ok "Venv recreated at $VENV  (Python $PY_MAJOR.$PY_MINOR)"
    else
        info "Recreating venv with --copies (needed for Full Disk Access)"
        rm -rf "$VENV" "$DEPLOY_DIR/.requirements-hash"
        "$SYS_PYTHON" -m venv --copies "$VENV"
        PYTHON="$VENV/bin/python3"
        VENV_RECREATED=1
        ok "Venv recreated at $VENV  (Python $PY_MAJOR.$PY_MINOR)"
    fi
else
    "$SYS_PYTHON" -m venv --copies "$VENV"
    PYTHON="$VENV/bin/python3"
    VENV_RECREATED=1
    ok "Venv created at $VENV  (Python $PY_MAJOR.$PY_MINOR)"
fi

# ── 7. Python dependencies ────────────────────────────────────────────────────
echo ""
echo "Checking Python dependencies..."

REQS_HASH_FILE="$DEPLOY_DIR/.requirements-hash"
if [ "$ROLE" = "watcher" ]; then
    # Fixed set for watcher — hash the package names directly
    REQS_HASH="$(echo 'litellm httpx beautifulsoup4 lxml pyyaml pyobjc-framework-EventKit' | shasum -a 256 | cut -d' ' -f1)"
else
    REQS_HASH="$(shasum -a 256 "$REPO_DIR/requirements.txt" | cut -d' ' -f1)"
fi

if [ -f "$REQS_HASH_FILE" ] && [ "$(cat "$REQS_HASH_FILE")" = "$REQS_HASH" ]; then
    skip "Dependencies unchanged"
else
    info "Installing dependencies..."
    "$VENV/bin/pip" install -q --upgrade pip
    if [ "$ROLE" = "watcher" ]; then
        "$VENV/bin/pip" install -q litellm httpx beautifulsoup4 lxml pyyaml pyobjc-framework-EventKit
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
    calendar_scanner.py
    content_fetcher.py
    chat_handler.py
    chat_tools.py
    commitment_tracker.py
    contact_tracker.py
    email_scanner.py
    index_builder.py
    llm_routes.py
    memory_writer.py
    notification_manager.py
    code_scanner.py
    goals_tracker.py
    project_inference_scanner.py
    goal_project_agent.py
    report_scheduler.py
    skill_creator.py
    skill_executor.py
    skill_optimizer.py
    skill_router.py
    slack_scanner.py
    utils.py
    zoom_scanner.py
    github_client.py
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

# ── 12b. Apply LLM provider preference to deployed skills ─────────────────────
echo ""
echo "Applying LLM provider preference to skill files..."
PROVIDER="$PROVIDER" "$VENV/bin/python3" "$REPO_DIR/scripts/apply_skill_provider.py" \
    "$BRAIN_DIR/skills"

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
    <key>SECOND_BRAIN_PROVIDER</key>
    <string>${PROVIDER}</string>
    <key>SECOND_BRAIN_DIR</key>
    <string>${DEPLOY_DIR}</string>
    <key>GEMINI_API_KEY</key>
    <string>${GEMINI_KEY}</string>
    <key>ANTHROPIC_API_KEY</key>
    <string>${ANTHROPIC_KEY}</string>
    <key>ZOOM_ACCOUNT_ID</key>
    <string>${ZOOM_ACCOUNT_ID}</string>
    <key>ZOOM_CLIENT_ID</key>
    <string>${ZOOM_CLIENT_ID}</string>
    <key>ZOOM_CLIENT_SECRET</key>
    <string>${ZOOM_CLIENT_SECRET}</string>
    <key>SLACK_USER_TOKEN</key>
    <string>${SLACK_USER_TOKEN}</string>
    <key>GITHUB_PAT</key>
    <string>${GITHUB_PAT}</string>
    <key>GITHUB_REPO</key>
    <string>${GITHUB_REPO}</string>
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

# ── 16. Full Disk Access check (both roles) ───────────────────────────────────
echo ""
echo "Checking Full Disk Access for email scanner..."

# NOTE: macOS Sonoma (14+) does not reliably grant Full Disk Access to
# ad-hoc signed binaries (no Team Identifier), which includes all Homebrew
# Python builds. The System Settings UI accepts the drag-and-drop but TCC
# silently ignores it at runtime. Confirming FDA from inside the installer
# is also unreliable — a subprocess inherits Terminal's TCC context, not
# the binary's own grant.
#
# If this terminal can read the Envelope Index (i.e. Terminal.app has FDA),
# we confirm it. Otherwise we explain the limitation and note that the
# AppleScript fallback will be used instead.

ENVELOPE_INDEX="$(ls "$HOME/Library/Mail"/V*/Envelope\ Index 2>/dev/null | sort -V | tail -1)"

if [ -n "$ENVELOPE_INDEX" ] && [ -r "$ENVELOPE_INDEX" ]; then
    ok "Envelope Index readable — Full Disk Access confirmed"
elif [ -n "$ENVELOPE_INDEX" ]; then
    # Homebrew Python is ad-hoc signed (no Team ID); macOS Sonoma silently
    # rejects FDA grants for unsigned binaries. The AppleScript fallback
    # handles this case — it requires Mail.app to be running.
    printf "${YELLOW}  –${NC}  Full Disk Access not available for Homebrew Python on macOS Sonoma.\n"
    echo "     Email scanner will use the AppleScript fallback (Mail.app must be open)."
    echo "     The fallback scans your last 500 Inbox/Sent messages per account."
else
    printf "${YELLOW}  –${NC}  No Envelope Index found — Mail.app may not be set up.\n"
    echo "     The email scanner will use the AppleScript fallback (requires Mail.app open)."
fi

# ── Calendar.app Automation permission ───────────────────────────────────────
echo ""
info "Calendar scanner will request Automation permission for Calendar.app on first scan — allow it when prompted."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete — v$VERSION"
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
echo "  Tag this release:"
echo "    git -C \"$REPO_DIR\" tag v$VERSION && git -C \"$REPO_DIR\" push --tags"
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
