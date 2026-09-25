#!/usr/bin/env bash
# run_mac.sh — one-shot bootstrap + install for macOS.
#
# Usage:
#   bash run_mac.sh                  # full install + start service
#   bash run_mac.sh --check          # only verify prerequisites, don't touch anything
#   bash run_mac.sh --no-service     # everything except LaunchAgent install (foreground: ./run.sh)
#
# After this script finishes, the bridge is running as a LaunchAgent and
# will auto-restart on crash + start when you log in to the console.
#
# NOTE: LaunchAgents only run while you're logged in on the Mac console. For
# a headless server that starts before login, you'd promote the plists to
# /Library/LaunchDaemons/ (requires sudo). This script does not do that.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

MODE="install"
for arg in "$@"; do
  case "$arg" in
    --check)      MODE="check" ;;
    --no-service) MODE="no-service" ;;
    -h|--help)    sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

say()  { printf '\n\033[1;36m▶\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ============================================================================
# 1. Prerequisites
# ============================================================================
say "checking prerequisites"

if [ "$(uname -s)" != "Darwin" ]; then
  die "this is the macOS installer. on Linux, use  bash run_linux.sh"
fi

# Homebrew (needed for tmux / python / claude on most Macs)
if ! command -v brew >/dev/null 2>&1; then
  warn "Homebrew not found. install per https://brew.sh (many prereqs below expect it)"
fi

# Python 3.10+
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 not found. install:  brew install python@3.12"
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')"
if [ "$PY_OK" != "1" ]; then
  die "python3 must be 3.10+ (found $PY_VER). upgrade:  brew upgrade python"
fi
ok "python3 $PY_VER"

# tmux
if ! command -v tmux >/dev/null 2>&1; then
  die "tmux not found. install:  brew install tmux"
fi
ok "tmux $(tmux -V | awk '{print $2}')"

# curl (ships with macOS)
command -v curl >/dev/null 2>&1 || die "curl not found (macOS ships with it — something's wrong)"
ok "curl"

# claude CLI (Claude Code) — REQUIRED
if ! command -v claude >/dev/null 2>&1; then
  die "claude CLI not found. install:  npm i -g @anthropic-ai/claude-code  (or per Anthropic docs)"
fi
ok "claude $(claude --version 2>/dev/null | head -1 || echo '(version unknown)')"

# launchctl (skip in --no-service mode)
if [ "$MODE" != "no-service" ]; then
  command -v launchctl >/dev/null 2>&1 || die "launchctl not found (macOS?)"
  ok "launchctl"
fi

if [ "$MODE" = "check" ]; then
  say "all prerequisites OK — nothing installed (--check mode)"
  exit 0
fi

# ============================================================================
# 2. Python venv + deps
# ============================================================================
say "setting up python venv"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ok "created .venv"
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
ok "requirements installed"

# ============================================================================
# 3. Runtime dirs + scripts +x
# ============================================================================
say "preparing runtime dirs + permissions"
mkdir -p logs "$HOME/.claude-slack-bridge"
chmod +x scripts/csb-bg-claude scripts/csb-bg-watchdog scripts/csb-notify scripts/csb-bg run.sh
ok "logs/, ~/.claude-slack-bridge/, scripts +x"

# ============================================================================
# 4. .env
# ============================================================================
say "verifying .env"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  warn ".env was missing — copied from .env.example. Edit .env now with your real tokens, then rerun this script."
  exit 1
fi
chmod 600 .env
if grep -qE '^SLACK_BOT_TOKEN=(xoxb-\.\.\.|)$' .env; then
  die ".env still has the placeholder SLACK_BOT_TOKEN. fill it in first."
fi
if grep -qE '^SLACK_APP_TOKEN=(xapp-\.\.\.|)$' .env; then
  die ".env still has the placeholder SLACK_APP_TOKEN. fill it in first."
fi
ok ".env present + tokens set"

# TRIGGER_USER_ID and AGENT_WORKSPACE have no defaults — the bridge refuses to
# start without them, so fail here with a readable message instead.
if ! grep -qE '^TRIGGER_USER_ID=U[A-Z0-9]{6,}' .env; then
  die ".env needs a real TRIGGER_USER_ID (your Slack member ID, e.g. Uxxx). This is the only authorization gate."
fi
if ! grep -qE '^AGENT_WORKSPACE=\S' .env || grep -qE '^AGENT_WORKSPACE=/path/to/' .env; then
  die ".env needs AGENT_WORKSPACE set to the directory the agent should run in."
fi
ok "TRIGGER_USER_ID + AGENT_WORKSPACE set"

# ============================================================================
# 5. Foreground fallback if --no-service
# ============================================================================
if [ "$MODE" = "no-service" ]; then
  say "installation complete (--no-service)"
  echo "  run manually: ./run.sh"
  exit 0
fi

# ============================================================================
# 6. launchd install (bridge + [bg] watchdog)
# ============================================================================
say "installing LaunchAgents"
mkdir -p "$HOME/Library/LaunchAgents"

# Detect Homebrew prefix for PATH (Intel: /usr/local, Apple Silicon: /opt/homebrew)
BREW_PREFIX="/usr/local"
[ -d /opt/homebrew ] && BREW_PREFIX="/opt/homebrew"

install_plist() {
  local plist_name="$1" label="$2"
  local dest="$HOME/Library/LaunchAgents/$plist_name"

  # Rewrite the shipped placeholders: the repo path first (more specific),
  # then the bare home dir, then the homebrew prefix.
  sed \
    -e "s|/Users/USERNAME/claude-slack-bridge|$REPO_DIR|g" \
    -e "s|/Users/USERNAME|$HOME|g" \
    -e "s|/opt/homebrew/bin:/usr/local/bin|$BREW_PREFIX/bin|g" \
    "deploy/launchd/$plist_name" > "$dest"

  # Idempotent: bootout first if already loaded, then bootstrap+enable+start
  launchctl bootout "gui/$(id -u)" "$dest" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dest"
  launchctl enable    "gui/$(id -u)/$label"
  launchctl kickstart -k "gui/$(id -u)/$label"
  ok "$label installed + started"
}

install_plist "io.claude-slack-bridge.bridge.plist" "io.claude-slack-bridge.bridge"
install_plist "io.claude-slack-bridge.watchdog.plist"      "io.claude-slack-bridge.watchdog"

# ============================================================================
# 7. Status
# ============================================================================
say "status"
launchctl print "gui/$(id -u)/io.claude-slack-bridge.bridge" 2>/dev/null | sed -n '1,6p' || true
echo
launchctl print "gui/$(id -u)/io.claude-slack-bridge.watchdog"      2>/dev/null | sed -n '1,6p' || true
echo
echo "──────────────────────────────────────────────────"
echo "Bridge logs:    tail -f logs/bot.log"
echo "Watchdog logs:  tail -f logs/watchdog.log"
echo "Restart bridge: launchctl kickstart -k gui/\$(id -u)/io.claude-slack-bridge.bridge"
echo "Stop all:       launchctl bootout gui/\$(id -u) ~/Library/LaunchAgents/io.claude-slack-bridge.bridge.plist"
echo "                launchctl bootout gui/\$(id -u) ~/Library/LaunchAgents/io.claude-slack-bridge.watchdog.plist"
echo "──────────────────────────────────────────────────"
