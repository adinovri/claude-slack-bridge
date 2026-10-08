# claude-slack-bridge

Run [Claude Code](https://docs.claude.com/en/docs/claude-code) from Slack.

A thin listener: when **the one Slack account you authorize** (`TRIGGER_USER_ID`)
messages the bot — @-mentioning it in a channel, or DMing it directly — this
process spawns a `claude` CLI session in a workspace you choose and posts the
result back in-thread. Follow-ups in the same thread resume the same session.

Three runners, selected by a prefix on your message: a one-shot `claude -p`
(default), a live-updating tmux TUI (`[alt]`), and a detached background task
that survives a bridge restart (`[bg]`).

Optionally, an allowlisted **bot** (for example a Slack Workflow Builder
workflow) can trigger read-only, sandboxed runs as well — see
[Bot triggers](#bot-triggers-optional-untrusted).

Connects over Slack **Socket Mode**, so it needs no public URL, no inbound
firewall rule, and no reverse proxy.

> ⚠️ **Read [SECURITY.md](SECURITY.md) before deploying.** The default
> configuration gives the spawned agent full shell access on the host, gated
> only by one Slack user ID. That is a deliberate design, and it has sharp
> edges. Install it on a machine you would be willing to hand that account a
> terminal on.

## Architecture

```
Slack message (any channel/DM the bot is in)
  ├─> channel/group/mpim: bot @-mentioned  → app_mention event
  └─> DM (im)            : any message     → message event (channel_type=im)
        │
        ├─> posted by a bot in TRIGGER_BOT_IDS?   (optional, off by default)
        │     └─> yes → _dispatch(untrusted)   → always [bg], restricted (see "Bot triggers")
        │
        └─> sender == TRIGGER_USER_ID?   (the only authorization check for humans)
              ├─> no  → ignore silently
              └─> yes → _dispatch()
                          │
                          ├─> detect markers "[alt]" and "[bg]" (any order, case-insensitive)
                          │     ├─> [bg]        → spawn csb-bg-claude subprocess    (fire-and-forget)
                          │     ├─> [alt]       → alt_runner.run_alt()               (tmux + JSONL tail)
                          │     └─> (none)      → claude_runner.run()                (claude -p, ephemeral)
                          │
                          └─> post result back to thread via chat_update / chat_postMessage
                              ([bg]: the subprocess posts directly via notify-json config)
```

### Three runners

| | Default | `[alt]` | `[bg]` |
|---|---|---|---|
| **Trigger** | any message | prefix `[alt]` | prefix `[bg]` (wins if both markers present) |
| **How** | `claude -p --output-format json` in-process subprocess | `claude` TUI in tmux, JSONL transcript tailed | detached `csb-bg-claude` subprocess, watchdog-managed |
| **Bridge behavior** | blocks worker until done | blocks worker until done | fire-and-forget — returns immediately |
| **Session** | ephemeral per request | persistent tmux session per thread | persistent tmux session per task |
| **Live updates** | none | progressive Slack edits every `ALT_FLUSH_SECS` | none — one ack, then final result when done |
| **Tool crumbs** | none | `🔧 tool_name…` shown while Claude works | none |
| **Concurrency per thread** | queued (default bolt behavior) | second request rejected while one is running | multiple parallel tasks allowed; siblings listed in prompt |
| **Survives bridge restart** | no — ack marked "retrigger" on SIGTERM | no — tmux killed on shutdown | **yes** — subprocess is detached, watchdog updates Slack when done |
| **Ack text** | `⏳ thinking…` | `⏳ thinking…` (edited live) | `⏳ running in the background…` |
| **Best for** | short questions, one-shot | long interactive task where you want to watch progress | long-running deploys / batch scans / research you can walk away from |

### `[alt]` runner — per-request flow

```
[alt] prefix detected
  │
  └─> TmuxSession.ensure()
        ├─> session alive? → reuse
        └─> dead / new    → spawn claude TUI (--resume <uuid> or --session-id <uuid>)
                             poll pane for "❯" / "? for shortcuts" → TUI ready
  │
  └─> snapshot JSONL transcript byte offset (before send)
  │
  └─> paste prompt via tmux buffer → Enter
  │
  └─> tail JSONL transcript from offset, polling every 0.4s:
        ├─> collect text blocks + tool_use crumbs
        ├─> throttled chat_update to Slack every ALT_FLUSH_SECS
        ├─> stop on end_turn (only if that record carries visible text)
        ├─> fail-fast if session dies + QUIESCE elapsed
        └─> quiescence fallback: pane idle + no tool_use in-flight → break
  │
  └─> final chat_update (full result, chunked if > 3800 chars)
  │
  └─> touch() session TTL; reaper kills idle sessions after ALT_IDLE_TTL
```

### `[bg]` runner — per-request flow

```
[bg] prefix detected  (or [bg][alt] / [alt][bg] — bg wins)
  │
  ├─> read BG_REGISTRY (~/.claude-slack-bridge/bg_registry.json) for sibling bg tasks
  │   in the SAME Slack thread; inject their descriptions into the prompt
  │   so Claude knows what other bg work is already running here
  │
  ├─> post ack: "⏳ running in the background — I'll update this message when it's done."
  │   (NOT tracked in _pending, so a bridge restart won't touch it)
  │
  └─> subprocess.Popen(["python3", csb-bg-claude, "slack:<thread>", prompt,
                        "--notify-json", {channel, thread_ts, ack_ts, bot_token}])
        │
        └─> csb-bg-claude
              ├─> registers task in BG_REGISTRY with description + started_at
              ├─> spawns its OWN tmux session (independent of bridge's alt sessions)
              ├─> runs the Claude turn to completion — bridge is long gone
              └─> on finish: posts result to Slack via chat_update on ack_ts
                    (posting is done by the subprocess, not the bridge)
        │
        └─> csb-bg-watchdog (systemd-managed sibling service)
              reaps stale registry entries, kills orphaned tmux sessions
```

Because the subprocess is detached and posts its own result, a bridge restart
mid-run does not interrupt or notify. The bg task keeps going and updates Slack
when it finishes — exactly as if the bridge were still up.

### Bot triggers (optional, untrusted)

By default every bot-authored message is ignored. Setting `TRIGGER_BOT_IDS`
lets specific bots — typically a Slack **Workflow Builder** workflow that posts
a message @-mentioning this app — start a run. Leave it empty to keep the
feature off; the bridge then behaves exactly as without it.

Text from a bot is **untrusted**: a workflow can carry form input, alert
payloads or anything else someone outside your control wrote. So a bot-
triggered run is never a normal run:

| | Operator (`TRIGGER_USER_ID`) | Bot (`TRIGGER_BOT_IDS`) |
|---|---|---|
| Runner | default / `[alt]` / `[bg]` by prefix | always `[bg]`; prefixes are ignored |
| Prompt | the message | thread + recent `BOT_CONTEXT_CHANNEL` history, wrapped in `<untrusted_slack_data>` |
| Permission mode | `CLAUDE_PERMISSION_MODE` | `dontAsk` |
| Tools | everything | `Bash` only (`--tools Bash`, skills disabled, no MCP) |
| Allowed commands | everything | `BOT_ALLOWED_TOOLS` (read-only `gcloud` by default) |
| Settings loaded | all | `user` from `BOT_CLAUDE_CONFIG_DIR` only (`local` if unset) |
| Config dir / cwd | `CLAUDE_CONFIG_DIR` / `AGENT_WORKSPACE` | `BOT_CLAUDE_CONFIG_DIR` / `BOT_WORKSPACE` |
| Environment | bridge env minus Slack tokens | `env -i` + `HOME`, `PATH`, locale, `CLAUDE_CONFIG_DIR` |
| Session resume | yes | never resumes an operator session (and vice versa) |

Why a separate config dir: `--allowed-tools` only *adds* to the allow rules in
settings files. If the operator's settings allow plain `Bash` (common for an
agent workspace), passing an allowlist on the command line restricts nothing.
A dedicated `BOT_CLAUDE_CONFIG_DIR` with its own, minimal `settings.json` (and
no hooks) avoids that.

**Finding the bot ID.** Either of:
- In Slack, open the bot's profile from one of its messages → *Copy member ID*
  (`U…`). Put that in `TRIGGER_BOT_IDS`; the bridge resolves it to the bot ID
  (`B…`) at startup via `users.info`.
- Let it mention the app once with the feature off. The bridge logs
  `ignore app_mention from bot: bot_id=B… app_id=A… …`.

**One-time setup** (example paths):

```bash
# 1. a dedicated config dir, logged in to the account bot runs should use
mkdir -p ~/claude-bot-harness/workspace
CLAUDE_CONFIG_DIR=~/claude-bot-harness claude      # then /login, /exit

# 2. minimal settings: dontAsk + the same allow/deny lists as the bridge
cat > ~/claude-bot-harness/settings.json <<'JSON'
{
  "permissions": {
    "defaultMode": "dontAsk",
    "allow": ["Bash(gcloud logging read *)", "Bash(gcloud projects list *)"],
    "deny":  ["Read", "Grep", "Glob", "Write", "Edit", "NotebookEdit",
              "WebFetch", "WebSearch", "Bash(* --log-http*)"]
  }
}
JSON

# 3. mark the workspace trusted, or the [bg] TUI stops at the
#    "trust this folder" dialog and the prompt never lands
python3 - <<'PY'
import json, os
cfg = os.path.expanduser("~/claude-bot-harness/.claude.json")
ws = os.path.expanduser("~/claude-bot-harness/workspace")
d = json.load(open(cfg))
d.setdefault("projects", {}).setdefault(ws, {})["hasTrustDialogAccepted"] = True
json.dump(d, open(cfg, "w"), indent=2)
PY
```

Then set `TRIGGER_BOT_IDS`, `BOT_CLAUDE_CONFIG_DIR`, `BOT_WORKSPACE` (and
optionally `BOT_CONTEXT_CHANNEL`) and restart. The startup log confirms it:
`bot triggers enabled (untrusted mode): ['B…']`. Invite the bot that posts the
workflow messages and this app to the same channels.

Caveats:
- Commands in the allowlist run with **the host's** credentials (e.g. the
  active `gcloud` account). Prefer a dedicated read-only service account.
- Keep allowlist rules as an exact subcommand plus a trailing ` *`. A wildcard
  in the middle (`gcloud * list *`) also matches e.g.
  `gcloud secrets versions access … list`.
- Claude Code auto-allows a few read-only commands (`ls`, `pwd`, `whoami`, …)
  inside the working directory, even in `dontAsk`. Keep `BOT_WORKSPACE` free
  of anything sensitive.
- The prompt asks the model to summarize rather than paste raw logs, but what
  ends up in the thread is ultimately model output — mind data you would not
  want in that channel.

#### Revoking a bot

`TRIGGER_BOT_IDS` is resolved once at startup, so revoking means changing it
and restarting:

```bash
# remove the bot from TRIGGER_BOT_IDS (or delete the variable to turn the
# feature off entirely) wherever you set it — .env or a systemd override — then:
systemctl --user daemon-reload                       # only if you edited a unit
systemctl --user restart claude-slack-bridge.service
```

Verify: the startup log no longer prints `bot triggers enabled`, and the next
mention from that bot logs `ignore app_mention from bot: bot_id=B…`.

A restart does **not** stop bot tasks already running — `[bg]` tasks are
detached on purpose. To stop them too:

```bash
tmux ls | grep csb-bg-                   # bot tasks are the registry entries whose
                                         # jsonl_path is under BOT_CLAUDE_CONFIG_DIR
tmux kill-session -t csb-bg-XXXXXXXX
```

Other levers that don't need the bridge at all:
- **Claude credentials:** `CLAUDE_CONFIG_DIR=<BOT_CLAUDE_CONFIG_DIR> claude /logout`
  (or delete its `.credentials.json`). Every bot run then fails to
  authenticate, including retriggers.
- **Slack:** remove the bot (or this app) from the channel, or disable the
  workflow.

### Graceful shutdown

On `SIGTERM`/`SIGINT`:
1. Flush all pending `"thinking…"` acks → update to restart message
   (only default/`[alt]` acks; `[bg]` acks are intentionally not tracked)
2. `alt_runner.kill_all()` → kill all `csb_*` tmux sessions
   (bg tasks run in their OWN tmux sessions, not touched by this)
3. Exit — any `[bg]` subprocess keeps running and posts when done

## Slack app setup (one-time, in api.slack.com)

Create a new Slack app (or reuse one you control) at
[api.slack.com/apps](https://api.slack.com/apps).

1. **Socket Mode** → Enable
2. **App-Level Tokens** → Generate a token with scope `connections:write` → copy
   the `xapp-...` value into `SLACK_APP_TOKEN`
3. **OAuth & Permissions** → Bot Token Scopes, ensure these are present:
   - `chat:write`
   - `app_mentions:read`
   - `im:history` (for DMs from the authorized user)
   - `users:read`
4. **Event Subscriptions** → Enable Events, subscribe bot to:
   - `app_mention`           (channel/group/mpim @-mentions of the bot)
   - `message.im`            (the authorized user DMs the bot)
5. **Reinstall App** to workspace after scope changes
6. **Invite the bot** to the channels you want it reachable from (`/invite @your-bot`)

> Note: `message.channels`/`message.groups`/`message.mpim` are intentionally NOT
> subscribed — channel mentions go through `app_mention` only.

### Finding your Slack user ID (for `TRIGGER_USER_ID`)

You'll set `TRIGGER_USER_ID` in `.env` to a `U…` string that identifies
whichever Slack account should be allowed to talk to the bridge (usually
your own).

**Easiest — from the Slack app:** click your avatar (top-right) → **View
profile** → in the panel that opens, click the **⋮** menu next to *Edit
Profile* → **Copy member ID**. Works on desktop, web, and mobile.

**From a Slack profile URL:** open anyone's profile in the Slack web app;
the URL is `https://<workspace>.slack.com/team/Uxxx` — the last segment
is that person's UID.

**Via the API (needs a bot token that has `users:read.email` for lookup, or
`users:read` for the list):**

```bash
# by email
curl -sH "Authorization: Bearer $SLACK_BOT_TOKEN" \
  --data-urlencode "email=you@company.com" \
  https://slack.com/api/users.lookupByEmail | jq '.user.id'

# or grep the full member list
curl -sH "Authorization: Bearer $SLACK_BOT_TOKEN" \
  https://slack.com/api/users.list | \
  jq -r '.members[] | "\(.id)\t\(.real_name)\t\(.name)"' | \
  grep -i "your name"
```

**Once the bridge is already running with someone else's UID:** DM the bot
from your own account (bridge will ignore silently), then:

```bash
journalctl --user -fu claude-slack-bridge | grep trigger
# → trigger: channel=Dxxx thread_ts=… user=Uxxx   ← that's you
```

## Prerequisites

Both `run_linux.sh` and `run_mac.sh` check these for you and print an
install hint if anything is missing. If you'd rather install first, here's the
full list:

| tool | version | why |
|---|---|---|
| **python3** | **≥ 3.10** | source uses PEP 585 generics (`dict[str, tuple[...]]`) |
| python3-venv | matching | to create `.venv/` |
| **tmux** | any recent | `[alt]` and `[bg]` runners drive Claude in a tmux TUI |
| **curl** | any | Telegram notify (`csb-notify`) uses it |
| **claude** CLI | latest | the actual worker — see [Claude Code install docs](https://docs.claude.com/en/docs/claude-code) or `npm i -g @anthropic-ai/claude-code` |
| **systemctl** (Linux) | any | to run as a user service — skip with `--no-service` |
| **launchctl** (macOS) | any | to install as a LaunchAgent — skip with `--no-service` |
| jq (optional) | any | pretty-print `~/.claude-slack-bridge/bg_registry.json` in debug commands |

Quick install lines:

- **Ubuntu/Debian:** `sudo apt-get install -y python3 python3-venv tmux curl jq`
- **macOS (Homebrew):** `brew install python@3.12 tmux jq`  (curl ships with macOS)
- **claude CLI:** `npm i -g @anthropic-ai/claude-code` (needs Node 18+) — or follow the official doc

On Linux, also enable **linger** once so your user services survive logout and
start on boot:

```bash
sudo loginctl enable-linger "$USER"
```

## Quickstart — fresh clone to running service

Same three steps on both platforms. The bootstrap script does the venv + deps +
runtime dirs + systemd/launchd install for you.

```bash
git clone https://github.com/adinovri/claude-slack-bridge ~/claude-slack-bridge
cd ~/claude-slack-bridge

# 1. copy the env template + fill in your real values
cp .env.example .env
chmod 600 .env
$EDITOR .env
#   SLACK_BOT_TOKEN   ← xoxb-... from OAuth & Permissions
#   SLACK_APP_TOKEN   ← xapp-... from App-Level Tokens
#   TRIGGER_USER_ID   ← your Slack member ID  (required — this is the auth gate)
#   AGENT_WORKSPACE   ← the directory the agent should run in  (required)

# 2. one-shot install (Linux)
bash run_linux.sh
#    …or macOS
bash run_mac.sh
```

After the installer finishes, the bridge is running as a user-level service
that auto-restarts on crash and starts on boot. Tokens in `.env` are used
via `run.sh` (which the service invokes).

What each script installs:

| | Linux (`run_linux.sh`) | macOS (`run_mac.sh`) |
|---|---|---|
| bridge | systemd user unit `claude-slack-bridge.service` (enabled + started) | LaunchAgent `io.claude-slack-bridge.bridge` (bootstrapped + started) |
| `[bg]` watchdog | systemd oneshot `csb-bg-watchdog.service` + `.timer` (fires every 15s) | LaunchAgent `io.claude-slack-bridge.watchdog` with `StartInterval=15` (bootstrapped + started) |
| Python venv | `.venv/` under repo root | same |
| script permissions | `chmod +x scripts/*` | same |
| `.env` guard | rejects the sample placeholders; refuses to enable service until real tokens are in place | same |

### Modes

```bash
bash run_linux.sh --check        # verify prerequisites, do nothing else
bash run_linux.sh --no-service   # everything except systemd install (run foreground with ./run.sh)
bash run_linux.sh                # full install + start service (default)
```

`run_mac.sh` takes the same three modes.

### Service ops (after install)

**Linux — systemd:**

```bash
# logs
journalctl --user -fu claude-slack-bridge
journalctl --user -fu csb-bg-watchdog

# lifecycle
systemctl --user restart claude-slack-bridge
systemctl --user disable --now claude-slack-bridge csb-bg-watchdog.timer
```

**macOS — launchd:**

```bash
# logs
tail -f logs/bot.log         # bridge
tail -f logs/watchdog.log    # [bg] watchdog (fires every 15s)

# lifecycle — bridge
launchctl kickstart -k gui/$(id -u)/io.claude-slack-bridge.bridge
launchctl bootout   gui/$(id -u) \
  ~/Library/LaunchAgents/io.claude-slack-bridge.bridge.plist

# lifecycle — [bg] watchdog
launchctl kickstart -k gui/$(id -u)/io.claude-slack-bridge.watchdog
launchctl bootout   gui/$(id -u) \
  ~/Library/LaunchAgents/io.claude-slack-bridge.watchdog.plist
```

### Uninstall

Same two-script split, same three modes. Both are **safe by default** — they
stop the service and remove the OS-side unit/plist files, and touch nothing
else. `.env`, the repo itself, `~/.claude-slack-bridge/`, and (macOS) the optional
Telegram env file all stay put; delete by hand if you want them gone.

```bash
bash uninstall_linux.sh            # stop + disable services, remove systemd units
bash uninstall_linux.sh --purge    # + wipe .venv/, logs/, src/**/__pycache__
bash uninstall_linux.sh --dry-run  # preview what would happen

bash uninstall_mac.sh              # bootout + remove both LaunchAgents
bash uninstall_mac.sh --purge      # + wipe .venv/, logs/, src/**/__pycache__
bash uninstall_mac.sh --dry-run    # preview
```

To fully wipe the machine, run `--purge` and then `rm -rf` the repo — that's
all that's left.

### Templates you may need to inspect

Everything the installer copies lives in the repo under version control:

```
deploy/systemd/
  claude-slack-bridge.service    # bridge — uses %h for $HOME; installer rewrites the repo path
  csb-bg-watchdog.service        # oneshot — reads $BG_REGISTRY + optional Telegram env file
  csb-bg-watchdog.timer          # 15-second cadence
deploy/launchd/
  io.claude-slack-bridge.bridge.plist   # bridge — installer substitutes repo dir, $HOME, brew prefix
  io.claude-slack-bridge.watchdog.plist        # [bg] watchdog — StartInterval=15s, sources optional Telegram env file
```

Change these in the repo, rerun `bash run_linux.sh` / `bash run_mac.sh`, and
they're re-copied.

### Optional — Telegram notify for `[bg]` fallback

> **If you only use the Slack bridge, SKIP this whole section.** Nothing to
> install, nothing to configure. Every Slack path (default, `[alt]`, `[bg]`)
> notifies Slack — Telegram is never touched. `csb-notify` gracefully exits
> when the env vars are absent, so unused code stays quiet.

Telegram is only used when:
1. You run `scripts/csb-bg <desc> <cmd> …` directly — the generic "run any
   shell command in background" helper always notifies via Telegram.
2. You manually invoke `scripts/csb-bg-claude` with
   `--notify-json '{"type":"telegram"}'` (or with no `--notify-json`, since
   telegram is the default there when called outside the bridge).

To enable it, drop a private env file at `~/.config/claude-slack-bridge.env`:

```bash
umask 077
cat > ~/.config/claude-slack-bridge.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456:AA…      # from @BotFather
TELEGRAM_CHAT_ID=1234567890         # your chat id
EOF
```

The watchdog systemd unit reads this file via
`EnvironmentFile=-%h/.config/claude-slack-bridge.env` (the `-` prefix means
the file is optional — no error if it's absent).

## File layout

```
src/                                    Python source (bridge process)
  app.py            slack-bolt listener + dispatch (three runners, reaper loop, shutdown flush)
  claude_runner.py  default runner: spawns `claude -p --output-format json [--resume]`
  alt_runner.py     [alt] runner: TmuxSession per thread, JSONL transcript tail, live updates
  thread_store.py   read/write threads/<thread_ts>.json (session_id + runner type)
  config.py         env var parsing

scripts/                                [bg] runtime — bridge spawns csb-bg-claude; watchdog polls
  csb-bg-claude    launch a Claude task in a detached tmux session, append to $BG_REGISTRY
  csb-bg-watchdog  poll the registry every 15s, notify (Slack/Telegram) on end_turn, reap orphans
  csb-bg           run an arbitrary shell command in background via systemd-run + Telegram notify
  csb-notify       curl → Telegram (reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID; NO hardcoded secrets)

deploy/                                 templates copied by run_linux.sh / run_mac.sh
  systemd/          bridge service, watchdog service, watchdog timer (installer
                    rewrites the repo path, so you can clone anywhere)
  launchd/          bridge LaunchAgent, watchdog LaunchAgent (StartInterval=15s)

run.sh              foreground entrypoint (sources .env, execs `python -m src.app`) — used by systemd/launchd
run_linux.sh        one-shot: prereq check + venv + systemd install + start
run_mac.sh          one-shot: prereq check + venv + LaunchAgent install + start
uninstall_linux.sh  reverse of run_linux.sh — --purge also wipes .venv/logs, --dry-run to preview
uninstall_mac.sh    reverse of run_mac.sh   — --purge also wipes .venv/logs, --dry-run to preview
```

Per-thread state lives at `$THREAD_STORE_DIR/<thread_ts>.json`:

```json
{ "session_id": "uuid", "channel": "Cxxx", "last_user": "Uxxx", "runner": "alt" }
```

The `runner` field ensures session IDs are never crossed between runners on resume.

## Environment variables

```
# Slack
SLACK_BOT_TOKEN      required    xoxb-... (bot OAuth token). Also read by the
                                 watchdog unit via EnvironmentFile= so it can
                                 post [bg] results — keep it in .env only.
SLACK_APP_TOKEN      required    xapp-... (socket mode app-level token)
TRIGGER_USER_ID      required    the ONE Slack member ID allowed to trigger a run.
                                 No default — see SECURITY.md.
TRIGGER_BOT_IDS      (empty)     comma-separated bot IDs (B…) or bot member IDs
                                 (U…, resolved at startup) whose @-mentions start
                                 a restricted [bg] run. Empty = feature off.
                                 See "Bot triggers".

# Claude
CLAUDE_CLI           claude      path to claude binary
CLAUDE_CONFIG_DIR    ~/.claude   claude CLI config dir (OAuth creds + JSONL transcripts)
                                 Resolution order (first wins):
                                   1. systemd unit Environment=/launchd EnvironmentVariables
                                   2. this .env file
                                   3. shell env
                                   4. default (~/.claude)
                                 Multi-account setups pin this per-service in the
                                 systemd unit, not in .env.
CLAUDE_MODEL         claude-sonnet-4-6
CLAUDE_PERMISSION_MODE  bypassPermissions
CLAUDE_TIMEOUT       600         max seconds to wait for any response

# Paths
AGENT_WORKSPACE      required    working directory for the spawned claude CLI
THREAD_STORE_DIR     ~/.claude-slack-bridge/threads
AGENT_NAME           Claude      how the agent names itself in the preamble (cosmetic)
CSB_ORPHAN_PATTERNS  csb-:csb-bg-  orphan tmux reaper prefixes ("match:skip", comma-separated)

# [alt] runner
ALT_MARKER           [alt]       prefix to select the alt runner
ALT_TMUX_SOCKET      claude-bridge    tmux -L socket name (isolates bridge sessions)
ALT_IDLE_TTL         1800        seconds before idle tmux session is reaped
ALT_TUI_BOOT_SECS    8           max seconds to wait for TUI to show prompt
ALT_PASTE_SETTLE_SECS 0.6        pause between paste-buffer and Enter key
ALT_FLUSH_SECS       1.5         interval for progressive Slack updates
ALT_QUIESCE_SECS     10.0        silence before quiescence fallback kicks in
ALT_QUIESCE_STABLE_POLLS 3       consecutive idle pane polls to confirm turn closed

# [bg] runner
BG_MARKER            [bg]        prefix to select the bg runner (case-insensitive)
BG_REGISTRY          ~/.claude-slack-bridge/bg_registry.json
                                 shared registry file — bridge, csb-bg-claude,
                                 and csb-bg-watchdog MUST agree on this path.
                                 Bridge passes it through to the subprocess.

# Bot triggers (only used when TRIGGER_BOT_IDS is set)
BOT_CLAUDE_CONFIG_DIR (empty)    config dir for bot runs, passed to claude as
                                 CLAUDE_CONFIG_DIR. Empty = reuse CLAUDE_CONFIG_DIR
                                 and load only "local" settings.
BOT_WORKSPACE        (empty)     cwd for bot runs. Empty = AGENT_WORKSPACE.
BOT_ALLOWED_TOOLS    read-only gcloud rules
                                 comma-separated permission rules, e.g.
                                 "Bash(gcloud logging read *),Bash(gcloud projects list *)"
BOT_CONTEXT_CHANNEL  (empty)     channel ID whose recent history is added to the
                                 prompt (e.g. an alert channel). Empty = thread only.
BOT_CONTEXT_LIMIT    30          how many messages of BOT_CONTEXT_CHANNEL to include

# Optional — Telegram fallback for csb-bg / non-Slack csb-bg-claude callers.
# Keep these OUT of .env; put them in ~/.config/claude-slack-bridge.env
# (mode 600). The watchdog systemd unit reads that file via EnvironmentFile=-.
TELEGRAM_BOT_TOKEN    (unset)     Telegram bot token (from @BotFather)
TELEGRAM_CHAT_ID      (unset)     Telegram chat id

LOG_LEVEL            INFO
```

## Debugging

```bash
# live logs (systemd)
journalctl --user -fu claude-slack-bridge

# active [alt] tmux sessions (bridge-owned)
tmux -L claude-bridge ls

# thread state files
ls ~/.claude-slack-bridge/threads/

# attach to a running [alt] session (read-only)
tmux -L claude-bridge attach -t csb_<thread_ts> -r

# --- [bg] runner ---

# in-flight bg tasks (task id, slack thread, description, started_at, pid)
cat ~/.claude-slack-bridge/bg_registry.json | jq .

# watchdog logs (if installed as a sibling systemd service)
journalctl --user -fu csb-bg-watchdog

# bg tasks run in their OWN tmux sessions — list them
tmux ls 2>/dev/null | grep -E '^bg_|^csb-bg-'
```

## Security

Full model, threat cases and a hardening checklist: **[SECURITY.md](SECURITY.md)**.
The short version:

- The spawned agent runs with `--permission-mode bypassPermissions` — full
  `Bash`/`Edit`/MCP access. The sender check (`event.user == TRIGGER_USER_ID`)
  is the **only** gate. Anyone who can post as that account can run commands
  on this host.
- Senders whose ID doesn't match are dropped silently, so the bot stays quiet
  in shared channels.
- Tokens live in `.env` (mode 600) and are never committed, never put on a
  command line, and never written into `BG_REGISTRY`.

### Trust boundary

The sender gate controls **who can start a run**, not **what the run then
reads**. Once a task is going, anything it pulls in — other people's messages
in the thread via a Slack MCP server, a ticket description, a web page, a PR
diff — reaches a model running with `bypassPermissions` on a host that may hold
your cloud credentials, password-manager session and VPN. Treat every
`[bg]`/`[alt]` task as running with your own privileges against untrusted
input, and narrow `CLAUDE_PERMISSION_MODE` / the tool denylist for anything
that reads widely. The sender check does not mitigate this, and no additional
sender check would.

### Credential handling

- All three runners (default, `[alt]`, `[bg]`) pass `--disallowed-tools`. The
  list lives in `src/claude_runner.py:DISALLOWED_TOOLS` and is mirrored in
  `scripts/csb-bg-claude` (override with `BG_DISALLOWED_TOOLS`). It blocks
  Slack write tools that act as the *human user's* OAuth identity — without it
  a task can post to Slack as you rather than as the bot.
- `SLACK_BOT_TOKEN` is never put on a command line and never written to
  `BG_REGISTRY`. The bridge passes only channel/thread/ack ids to
  `csb-bg-claude`; the watchdog reads the token from its own unit environment
  (`EnvironmentFile=` → the bridge's `.env`). Anything on a command line is
  readable by any local user via `ps`; anything in the registry sits on disk
  until the task is reaped.
- The bg task's own environment has `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`
  stripped — it runs with full Bash access and has no need for them.
- `BG_REGISTRY` is chmod 600 on every write (it names channels, threads and
  task descriptions).
- Claude transcripts under `CLAUDE_CONFIG_DIR/projects/*.jsonl` record full
  tool output — including anything a task read from a database or a secret
  store — unencrypted and unrotated. Task results are also posted into Slack,
  where they fall under workspace retention. Both are worth knowing before
  running a task against production data.

## License

MIT — see [LICENSE](LICENSE).
