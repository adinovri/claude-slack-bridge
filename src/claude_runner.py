"""Default runner: spawn a `claude -p` subprocess and parse its JSON result."""
import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .config import (
    BOT_ALLOWED_TOOLS,
    BOT_CLAUDE_CONFIG_DIR,
    BOT_DISALLOWED_TOOLS,
    BOT_FALLBACK_CHECK,
    BOT_FALLBACK_MCP_CONFIG,
    BOT_FALLBACK_MCP_TOOLS,
    BOT_MEMORY,
    BOT_MODEL,
    BOT_WORKSPACE,
    CLAUDE_CLI,
    CLAUDE_MODEL,
    CLAUDE_PERMISSION_MODE,
    CLAUDE_TIMEOUT,
    AGENT_WORKSPACE,
)

log = logging.getLogger(__name__)

# Environment passed to restricted (bot-triggered) runs. run.sh exports the
# whole .env into the bridge, so an untrusted run gets only what claude and
# gcloud need — never the bridge's own secrets.
_BOT_ENV_KEYS = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR")

# Slack write tools that act as the human operator (user-OAuth MCP servers,
# as opposed to the bot token). The bridge already posts every reply via
# SLACK_BOT_TOKEN, so the spawned agent must never call these — otherwise its
# messages appear under the operator's own Slack identity instead of the bot's.
# Project instructions can ask for this, but the model occasionally forgets;
# this denylist is the harness-level enforcement.
DISALLOWED_TOOLS = [
    "mcp__claude_ai_Slack__slack_send_message",
    "mcp__claude_ai_Slack__slack_send_message_draft",
    "mcp__claude_ai_Slack__slack_schedule_message",
    "mcp__claude_ai_Slack__slack_add_reaction",
    "mcp__claude_ai_Slack__slack_create_canvas",
    "mcp__claude_ai_Slack__slack_update_canvas",
]


def bot_memory_dir() -> Path | None:
    """Memory dir for bot runs, or None when memory is off. Lives in the bot
    config dir, keyed like Claude Code's own project dirs (real cwd path with
    every non-alphanumeric char replaced by "-")."""
    if not (BOT_MEMORY and BOT_CLAUDE_CONFIG_DIR):
        return None
    cwd = os.path.realpath(restricted_cwd())
    mangled = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    return Path(BOT_CLAUDE_CONFIG_DIR).expanduser() / "projects" / mangled / "memory"


_fallback_cache: tuple[float, bool] | None = None
_FALLBACK_CACHE_SECS = 60


def fallback_needed() -> bool:
    """True when bot runs should use the fallback MCP tools: a fallback is
    configured and BOT_FALLBACK_CHECK fails (e.g. gcloud auth expired). Cached
    for a minute so a burst of alerts runs the check once."""
    global _fallback_cache
    if not BOT_FALLBACK_MCP_CONFIG:
        return False
    now = time.monotonic()
    if _fallback_cache and now - _fallback_cache[0] < _FALLBACK_CACHE_SECS:
        return _fallback_cache[1]
    try:
        ok = subprocess.run(
            shlex.split(BOT_FALLBACK_CHECK),
            env=restricted_env(os.environ),
            capture_output=True,
            timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        log.warning("BOT_FALLBACK_CHECK failed (%s); bot runs use the fallback MCP tools",
                    BOT_FALLBACK_CHECK)
    _fallback_cache = (now, not ok)
    return not ok


def restricted_args(fallback: bool = False) -> list[str]:
    """CLI flags for an untrusted (bot-triggered) run. Shared by the default
    runner and the [bg] runner (passed to csb-bg-claude) so both enforce the
    same limits.

    --restricted ignores user/project/local settings (--allowed-tools only ADDS
    to their allow rules, and the operator's settings allow plain "Bash"),
    drops code-running tools not named in --tools, and confines file tools to
    the cwd plus --add-dir — here only the bot memory dir, if enabled.
    With fallback, the BOT_FALLBACK_MCP_CONFIG servers are loaded and
    BOT_FALLBACK_MCP_TOOLS allowed; everything else on them stays denied.
    """
    memory = bot_memory_dir()
    tools = "Bash,Read,Write,Edit" if memory else "Bash"
    denied = list(BOT_DISALLOWED_TOOLS)
    allowed = list(BOT_ALLOWED_TOOLS)
    extra: list[str] = []
    if memory:
        memory.mkdir(parents=True, exist_ok=True)
        # Deny beats allow, so the file tools must leave the denylist; the
        # sandbox from --restricted is what keeps them inside these dirs.
        denied = [t for t in denied if t not in ("Read", "Write", "Edit")]
        allowed.append(f"Edit(/{memory}/**)")  # "//abs/path"; covers Write too
        extra = ["--add-dir", str(memory)]
    mcp_config = '{"mcpServers":{}}'
    if fallback and BOT_FALLBACK_MCP_CONFIG:
        mcp_config = str(Path(BOT_FALLBACK_MCP_CONFIG).expanduser())
        allowed += BOT_FALLBACK_MCP_TOOLS
    return [
        "--restricted",
        "--tools", tools,
        "--disable-slash-commands",
        "--permission-mode", "dontAsk",
        *extra,
        "--allowed-tools", *allowed,
        "--disallowed-tools", *DISALLOWED_TOOLS, *denied,
        "--strict-mcp-config", "--mcp-config", mcp_config,
    ]


def restricted_env(base: dict[str, str]) -> dict[str, str]:
    """Minimal environment for an untrusted run (see _BOT_ENV_KEYS)."""
    env = {k: v for k, v in base.items() if k in _BOT_ENV_KEYS or k == "CLAUDE_CONFIG_DIR"}
    if BOT_CLAUDE_CONFIG_DIR:
        env["CLAUDE_CONFIG_DIR"] = str(Path(BOT_CLAUDE_CONFIG_DIR).expanduser())
    return env


def restricted_cwd() -> Path:
    return Path(BOT_WORKSPACE).expanduser() if BOT_WORKSPACE else AGENT_WORKSPACE


def run(
    prompt: str, session_id: str | None = None, restricted: bool = False
) -> tuple[str, str]:
    """Run claude CLI with the given prompt. Returns (result_text, new_session_id).

    restricted=True is for untrusted (bot-triggered) prompts: dontAsk denies
    every tool outside BOT_ALLOWED_TOOLS, and an empty strict MCP config keeps
    the operator's MCP servers (user-OAuth Slack, Atlassian, ...) unloaded.
    """
    cmd = [
        CLAUDE_CLI,
        "-p",
        prompt,
        "--model",
        BOT_MODEL if restricted else CLAUDE_MODEL,
        "--output-format",
        "json",
    ]
    if restricted:
        cmd += restricted_args()
    else:
        cmd += [
            "--disallowed-tools", *DISALLOWED_TOOLS,
            "--permission-mode", CLAUDE_PERMISSION_MODE,
        ]
    if session_id:
        cmd += ["--resume", session_id]
    cwd = restricted_cwd() if restricted else AGENT_WORKSPACE

    log.info(
        "spawning claude (cwd=%s, resume=%s, timeout=%s, restricted=%s)",
        cwd,
        session_id,
        CLAUDE_TIMEOUT,
        restricted,
    )
    # CLAUDE_CONFIG_DIR must point at the dir holding valid OAuth creds for
    # the account this bridge runs as. It is injected by the service unit
    # (Environment=CLAUDE_CONFIG_DIR=...), which is the canonical launch path —
    # run via `systemctl --user` / launchd, never a manual nohup, or the CLI
    # will fall back to ~/.claude and may pick up a different account.
    env = restricted_env(os.environ) if restricted else os.environ.copy()

    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        env=env,
    )

    if proc.returncode != 0:
        log.error(
            "claude exited %s; stderr=%s; stdout=%s",
            proc.returncode,
            proc.stderr[:1000],
            proc.stdout[:1000],
        )
        # claude CLI writes its API-error payload to stdout as JSON even when
        # exiting non-zero. Surface the human-readable `result` field instead of
        # raw JSON so Slack doesn't get a wall of `{"is_error":true,...}`.
        try:
            err_payload = json.loads(proc.stdout)
            human = (err_payload.get("result") or "").strip()
            api_status = err_payload.get("api_error_status")
            if human and api_status:
                msg = f"[API {api_status}] {human}"
            elif human:
                msg = human
            else:
                msg = (proc.stderr or proc.stdout or "").strip()[:500] or "unknown error"
        except json.JSONDecodeError:
            msg = (proc.stderr or proc.stdout or "").strip()[:500] or "unknown error"
        raise RuntimeError(msg)

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        log.error("invalid JSON from claude: %s | stdout head=%s", e, proc.stdout[:500])
        raise

    result = payload.get("result") or payload.get("response") or ""
    new_session_id = payload.get("session_id") or session_id or ""
    return result, new_session_id
