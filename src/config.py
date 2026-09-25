import os
from pathlib import Path

from dotenv import load_dotenv

# Pin the dotenv file to THIS repo. Bare load_dotenv() walks up the directory
# tree until it finds any `.env`, so a stray file in a parent directory (or in
# $HOME) would silently supply this bridge's Slack tokens and its one
# authorization gate. Load only our own, and never let it override a value the
# service unit already set.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_FILE, override=False)


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val  # type: ignore[return-value]


SLACK_BOT_TOKEN = _env("SLACK_BOT_TOKEN", required=True)
SLACK_APP_TOKEN = _env("SLACK_APP_TOKEN", required=True)

# The ONLY authorization gate. Messages authored by anyone else are dropped.
# Deliberately has no default: a shipped default would mean a half-configured
# install silently trusts a Slack account the operator has never heard of.
TRIGGER_USER_ID = _env("TRIGGER_USER_ID", required=True)

# Working directory for the spawned `claude` CLI. Required, because guessing
# it wrong means running an agent with full tool access in the wrong tree.
AGENT_WORKSPACE = Path(_env("AGENT_WORKSPACE", required=True)).expanduser()
THREAD_STORE_DIR = Path(
    _env("THREAD_STORE_DIR", str(Path.home() / ".claude-slack-bridge" / "threads"))
).expanduser()
# Cosmetic: how the agent refers to itself in the prompt preamble.
AGENT_NAME = _env("AGENT_NAME", "Claude")

CLAUDE_CLI = _env("CLAUDE_CLI", "claude")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_PERMISSION_MODE = _env("CLAUDE_PERMISSION_MODE", "bypassPermissions")
CLAUDE_TIMEOUT = int(_env("CLAUDE_TIMEOUT", "600"))

# --- alt runner (tmux-hosted TUI + tail the JSONL transcript) ---
ALT_MARKER        = _env("ALT_MARKER",        "[alt]")
BG_MARKER         = _env("BG_MARKER",         "[bg]")
# Shared bg-task registry — the bridge writes acks, csb-bg-claude appends new
# tasks, csb-bg-watchdog polls and reaps. All three MUST agree on this path.
BG_REGISTRY       = Path(_env(
    "BG_REGISTRY",
    str(Path.home() / ".claude-slack-bridge" / "bg_registry.json"),
)).expanduser()
ALT_TMUX_SOCKET   = _env("ALT_TMUX_SOCKET",   "claude-bridge")
ALT_IDLE_TTL      = int(_env("ALT_IDLE_TTL",      "1800"))
ALT_QUIESCE_SECS  = float(_env("ALT_QUIESCE_SECS",  "10.0"))
# how many consecutive idle polls (×0.4s) confirm a turn is closed before we
# fall back to quiescence-break — debounces transient tool-latency gaps
ALT_QUIESCE_STABLE_POLLS = int(_env("ALT_QUIESCE_STABLE_POLLS", "3"))
ALT_FLUSH_SECS    = float(_env("ALT_FLUSH_SECS",    "1.5"))
ALT_TUI_BOOT_SECS = float(_env("ALT_TUI_BOOT_SECS", "10"))
ALT_PASTE_SETTLE_SECS = float(_env("ALT_PASTE_SETTLE_SECS", "0.6"))  # paste→Enter gap
# after submit, how long to wait for the user turn to land in the transcript
# (proof the paste was accepted) before resending
ALT_SUBMIT_VERIFY_SECS = float(_env("ALT_SUBMIT_VERIFY_SECS", "8"))
ALT_SUBMIT_RETRIES     = int(_env("ALT_SUBMIT_RETRIES", "2"))
# `claude --resume <uuid>` forks history into a NEW <uuid>.jsonl rather than
# appending to the resumed file. After a resume spawn, how long to wait for that
# forked transcript to appear before falling back to tailing the original.
ALT_RESUME_FORK_SECS   = float(_env("ALT_RESUME_FORK_SECS", "10"))
# injected by the service unit; falls back to ~/.claude for local dev
CLAUDE_CONFIG_DIR = Path(
    _env("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
).expanduser()

LOG_LEVEL = _env("LOG_LEVEL", "INFO")
