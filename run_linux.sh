#!/usr/bin/env bash
# run_linux.sh — one-shot bootstrap + install for Linux (Ubuntu/Debian-tested).
#
# Usage:
#   bash run_linux.sh                  # full install + start service
#   bash run_linux.sh --check          # only verify prerequisites, don't touch anything
#   bash run_linux.sh --no-service     # everything except systemd install (foreground: ./run.sh)
#
# After this script finishes, the bridge is running as a systemd user service
# and will auto-restart on crash + start on boot (linger is enabled).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

MODE="install"
for arg in "$@"; do
  case "$arg" in
    --check)      MODE="check" ;;
    --no-service) MODE="no-service" ;;
    -h|--help)    sed -n '2,10p' "$0"; exit 0 ;;
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

# Python 3.10+ (src/ uses `dict[str, tuple[str, str]]` PEP 585 syntax)
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 not found. install:  sudo apt-get install python3 python3-venv"
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')"
if [ "$PY_OK" != "1" ]; then
  die "python3 must be 3.10+ (found $PY_VER). upgrade or use pyenv."
fi
ok "python3 $PY_VER"

# venv module
if ! python3 -c "import venv" 2>/dev/null; then
  die "python3-venv missing. install:  sudo apt-get install python3-venv"
fi
ok "python3-venv"

# tmux (needed by [alt] and [bg] runners)
if ! command -v tmux >/dev/null 2>&1; then
  die "tmux not found. install:  sudo apt-get install tmux"
fi
ok "tmux $(tmux -V | awk '{print $2}')"

# curl (needed by csb-notify for Telegram)
if ! command -v curl >/dev/null 2>&1; then
  die "curl not found. install:  sudo apt-get install curl"
fi
ok "curl $(curl --version | head -1 | awk '{print $2}')"

# claude CLI (Claude Code) — REQUIRED
if ! command -v claude >/dev/null 2>&1; then
  die "claude CLI not found. install per https://docs.claude.com/en/docs/claude-code (npm i -g @anthropic-ai/claude-code)"
fi
ok "claude $(claude --version 2>/dev/null | head -1 || echo '(version unknown)')"

# systemctl (skip in --no-service mode)
if [ "$MODE" != "no-service" ]; then
  command -v systemctl >/dev/null 2>&1 \
    || die "systemctl not found. use --no-service to run in foreground, or install systemd."
  ok "systemctl"
fi

# jq (optional — debug commands)
if command -v jq >/dev/null 2>&1; then
  ok "jq (optional) $(jq --version)"
else
  warn "jq not installed (optional — used by debug commands). install:  sudo apt-get install jq"
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
# Sanity check: reject if the sample placeholders are still there
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

# ----------------------------------------------------------------------------
# Optional: bot triggers. Only checked when TRIGGER_BOT_IDS is set in .env.
# Each mistake here fails silently at runtime (auth error, or the [bg] TUI
# stuck on the "trust this folder" dialog), so catch them at install time.
# ----------------------------------------------------------------------------
env_val() {  # value of KEY in .env, unquoted, with a leading ~ expanded
  local v
  v="$(grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"
  printf '%s' "${v/#\~/$HOME}"
}
if [ -n "$(env_val TRIGGER_BOT_IDS)" ]; then
  say "verifying bot-trigger setup (TRIGGER_BOT_IDS is set)"
  BOT_CFG="$(env_val BOT_CLAUDE_CONFIG_DIR)"
  BOT_WS="$(env_val BOT_WORKSPACE)"
  if [ -z "$BOT_CFG" ]; then
    warn "BOT_CLAUDE_CONFIG_DIR is empty — bot runs reuse the operator config dir with only"
    warn "'local' settings. A dedicated dir is recommended (README → Bot triggers)."
  else
    [ -d "$BOT_CFG" ] || die "BOT_CLAUDE_CONFIG_DIR=$BOT_CFG does not exist. create it and log in:  CLAUDE_CONFIG_DIR=$BOT_CFG claude  (then /login)"
    grep -q '"oauthAccount"' "$BOT_CFG/.claude.json" 2>/dev/null \
      || die "$BOT_CFG is not logged in. run:  CLAUDE_CONFIG_DIR=$BOT_CFG claude  (then /login)"
    ok "bot config dir logged in: $BOT_CFG"
    if [ -f "$BOT_CFG/settings.json" ]; then
      ok "bot settings.json present"
    else
      warn "no $BOT_CFG/settings.json — the CLI flags still restrict bot runs, but a"
      warn "settings.json with dontAsk + allow/deny is recommended (README → Bot triggers)."
    fi
  fi
  if [ -n "$BOT_WS" ]; then
    mkdir -p "$BOT_WS"
    if [ -n "$BOT_CFG" ] && [ -f "$BOT_CFG/.claude.json" ]; then
      # Pre-accept the workspace trust dialog, or the [bg] TUI never takes the prompt.
      python3 - "$BOT_CFG/.claude.json" "$BOT_WS" <<'PY'
import json, os, sys
path = sys.argv[1]
ws = os.path.realpath(sys.argv[2])  # Claude keys projects by the real cwd
d = json.load(open(path))
proj = d.setdefault("projects", {}).setdefault(ws, {})
if not proj.get("hasTrustDialogAccepted"):
    proj["hasTrustDialogAccepted"] = True
    json.dump(d, open(path, "w"), indent=2)
PY
      ok "bot workspace present + trusted: $BOT_WS"
    fi
  else
    warn "BOT_WORKSPACE is empty — bot runs use AGENT_WORKSPACE as cwd."
  fi
  if grep -q gcloud <<<"$(env_val BOT_ALLOWED_TOOLS)" || [ -z "$(env_val BOT_ALLOWED_TOOLS)" ]; then
    if command -v gcloud >/dev/null 2>&1; then
      ok "gcloud found at $(command -v gcloud) — make sure that dir is on the service PATH (Environment=PATH= in the unit or an override)"
    else
      warn "gcloud not found — the default BOT_ALLOWED_TOOLS are gcloud commands, so bot runs can't do much."
    fi
  fi
fi

# ============================================================================
# 5. Optional Telegram env file (for [bg] Telegram notify path)
# ============================================================================
TG_FILE="$HOME/.config/claude-slack-bridge.env"
if [ -f "$TG_FILE" ]; then
  chmod 600 "$TG_FILE"
  ok "Telegram notify env file present: $TG_FILE"
else
  warn "no Telegram notify env at $TG_FILE (optional). to enable, create it (mode 600) with:"
  echo "     TELEGRAM_BOT_TOKEN=<bot-token-from-BotFather>"
  echo "     TELEGRAM_CHAT_ID=<your-chat-id>"
fi

# ============================================================================
# 6. Foreground fallback if --no-service
# ============================================================================
if [ "$MODE" = "no-service" ]; then
  say "installation complete (--no-service)"
  echo "  run manually: ./run.sh"
  exit 0
fi

# ============================================================================
# 7. systemd install
# ============================================================================
say "installing systemd user units"
mkdir -p "$HOME/.config/systemd/user"
# The shipped units assume the repo lives at ~/claude-slack-bridge.
# Rewrite that to wherever this clone actually is, so the install works from
# any directory.
install_unit() {
  sed "s|%h/claude-slack-bridge|${REPO_DIR}|g" \
    "deploy/systemd/$1" > "$HOME/.config/systemd/user/$1"
}
install_unit claude-slack-bridge.service
install_unit csb-bg-watchdog.service
install_unit csb-bg-watchdog.timer
ok "units installed to ~/.config/systemd/user/ (repo dir: $REPO_DIR)"

# linger — required for user services to keep running after logout AND start on boot
if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q Linger=yes; then
  ok "linger enabled"
else
  warn "linger is NOT enabled — user services will stop at logout."
  warn "run:  sudo loginctl enable-linger \"$USER\""
fi

systemctl --user daemon-reload
systemctl --user enable --now claude-slack-bridge.service
systemctl --user enable --now csb-bg-watchdog.timer
ok "services enabled + started"

# ============================================================================
# 8. Status
# ============================================================================
say "status"
systemctl --user --no-pager status claude-slack-bridge.service | sed -n '1,4p' || true
echo
systemctl --user --no-pager list-timers csb-bg-watchdog.timer | sed -n '1,3p' || true
echo
echo "──────────────────────────────────────────────────"
echo "Logs:            journalctl --user -fu claude-slack-bridge"
echo "Watchdog logs:   journalctl --user -fu csb-bg-watchdog"
echo "Restart bridge:  systemctl --user restart claude-slack-bridge"
echo "Stop everything: systemctl --user disable --now claude-slack-bridge csb-bg-watchdog.timer"
echo "──────────────────────────────────────────────────"
