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

# --- bot triggers (e.g. a Slack Workflow Builder workflow) ---
# Comma-separated bot IDs (B…) or bot member IDs (U…, resolved to B… at
# startup) whose @-mentions may trigger a run. Empty = feature OFF and the
# bridge behaves exactly as before. Bot-triggered runs are UNTRUSTED: their
# text can carry input from anyone who can feed the workflow, so they run in a
# restricted mode (dontAsk + the allowlist below, no MCP, no [alt]/[bg]).
TRIGGER_BOT_IDS = frozenset(
    s.strip() for s in _env("TRIGGER_BOT_IDS", "").split(",") if s.strip()
)
# Read-only tools for bot runs, one rule per entry (comma-separated in env).
# Keep every rule an exact subcommand + trailing " *": a mid-command wildcard
# like "gcloud * list *" also matches "gcloud secrets versions access ... list".
BOT_ALLOWED_TOOLS = [
    s.strip() for s in _env(
        "BOT_ALLOWED_TOOLS",
        "Bash(gcloud logging read *),"
        "Bash(gcloud projects list *),"
        "Bash(gcloud projects describe *),"
        "Bash(gcloud projects get-iam-policy *),"
        "Bash(gcloud compute instances list *),"
        "Bash(gcloud compute instances describe *),"
        "Bash(gcloud compute firewall-rules list *),"
        "Bash(gcloud compute firewall-rules describe *),"
        "Bash(gcloud compute addresses list *),"
        "Bash(gcloud container clusters list *),"
        "Bash(gcloud container clusters describe *),"
        "Bash(gcloud storage buckets list *),"
        "Bash(gcloud storage buckets describe *),"
        "Bash(gcloud iam service-accounts list *),"
        "Bash(gcloud iam service-accounts describe *),"
        "Bash(gcloud sql instances list *)",
    ).split(",") if s.strip()
]
# Always denied for bot runs, on top of dontAsk. Read/Grep/Glob would expose
# local files (this repo's .env holds the Slack tokens); --log-http prints the
# Authorization header.
BOT_DISALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "Write", "Edit", "NotebookEdit",
    "WebFetch", "WebSearch", "Bash(* --log-http*)",
]
# Persistent memory for bot runs (needs BOT_CLAUDE_CONFIG_DIR). The bot gets
# Read/Write/Edit confined (by --restricted) to its workspace + memory dir, and
# its MEMORY.md is injected into each prompt so verified patterns can be
# referenced instead of re-analyzed. Memory is written from untrusted input:
# expect poisoning attempts and review it now and then.
BOT_MEMORY = _env("BOT_MEMORY", "").lower() in ("1", "true", "yes", "on")
# Mention (e.g. "<!subteam^S0123ABCD>" or "<@U0123ABCD>") the bot puts on the
# first line of its reply when it judges an event a likely real attack. The
# [bg] watchdog moves it out of the analysis into a NEW message, since Slack
# does not notify on mentions added by an edit. Empty = never escalate.
BOT_ESCALATION_MENTION = _env("BOT_ESCALATION_MENTION", "")
# Separate Claude config dir + cwd for bot runs, so the operator's settings
# (blanket "Bash" allow rules, hooks), memory and CLAUDE.md never apply to an
# untrusted run. Empty = fall back to CLAUDE_CONFIG_DIR / AGENT_WORKSPACE and
# load only "local" setting sources.
BOT_CLAUDE_CONFIG_DIR = _env("BOT_CLAUDE_CONFIG_DIR", "")
BOT_WORKSPACE = _env("BOT_WORKSPACE", "")
# Channel whose recent history is injected as context for bot runs (e.g. the
# alert channel). Empty = only the triggering thread is injected.
# Optional fixed task for bot runs, set by the operator (e.g. "Analyze the
# security alert in this thread: real attack or expected activity, severity,
# evidence, recommended action."). Empty = the bot's own message is the task.
BOT_TASK_PROMPT = _env("BOT_TASK_PROMPT", "")
BOT_CONTEXT_CHANNEL = _env("BOT_CONTEXT_CHANNEL", "")
BOT_CONTEXT_LIMIT = int(_env("BOT_CONTEXT_LIMIT", "30"))
