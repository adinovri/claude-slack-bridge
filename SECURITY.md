# Security model

Read this before you deploy. The design is deliberate, but it is not a
least-privilege one, and the trade-off is yours to accept.

## What this thing is

A Slack message from one authorized account causes a `claude` CLI process to
run on the host, by default with `--permission-mode bypassPermissions`: no
approval prompts, full `Bash`, full filesystem read/write, and every MCP server
configured for the account under `CLAUDE_CONFIG_DIR`.

**Practical consequence:** anyone who can post to Slack as `TRIGGER_USER_ID`
can run arbitrary commands on the machine, as the user the service runs as.
Deploy it only on a host you would be willing to give that person shell on.

## The trust boundary

There is exactly one authorization check, in `src/app.py`:

```python
if sender != TRIGGER_USER_ID:
    return
```

It runs on both event paths (`app_mention` for channels, `message` with
`channel_type == "im"` for DMs). Non-matching senders are dropped with no
reply, so the bot is quiet in shared channels.

What that check does **not** cover:

- **Compromise of the authorized Slack account** is full compromise of the
  host. Slack account security (SSO, MFA, session hygiene) is load-bearing here.
- **A Slack workspace admin** can generally impersonate or act on behalf of
  members. If your workspace admins are not in your trust set, this bridge is
  not for you.
- **What the run reads afterwards.** The gate controls who can *start* a run,
  not what that run then ingests. Once a task is going, anything it pulls in —
  other people's messages in the thread, a ticket description, a web page, a PR
  diff — reaches a model with `bypassPermissions` on a host that may hold cloud
  credentials, a password-manager session, and VPN access. Prompt injection in
  any of that content is indistinguishable from an instruction you typed.
  **This is the sharpest edge in the design, and no additional sender check
  mitigates it.**

If a task will read widely from sources you do not control, narrow
`CLAUDE_PERMISSION_MODE` (to `default` or `acceptEdits`) and extend the tool
denylist for that deployment.

## Identity: the tool denylist

All three runners pass `--disallowed-tools`. The canonical list is
`src/claude_runner.py:DISALLOWED_TOOLS`, mirrored in `scripts/csb-bg-claude`
(override with `BG_DISALLOWED_TOOLS`). It blocks Slack **write** tools that act
under a *human user's* OAuth identity, as opposed to the bot token.

Without it, an agent asked to "reply in Slack" may post as the operator instead
of as the bot — messages that look like they came from a person, but did not.
If you add user-OAuth MCP servers for other services, add their write tools to
this list too.

## Credential handling

- `SLACK_BOT_TOKEN` is never placed on a command line and never written to
  `BG_REGISTRY`. The bridge passes only channel/thread/ack ids to
  `csb-bg-claude`; the watchdog reads the token from its own unit environment.
  Command lines are world-readable via `ps` and `/proc`; the registry persists
  on disk until the task is reaped.
- A `[bg]` task's environment has `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`
  stripped. It runs with full Bash access and has no need for them.
- `BG_REGISTRY` is written mode 600 — it names channels, threads, and task
  descriptions.
- `.env` must be mode 600. The installers enforce this; check it after any
  manual edit, especially under a permissive `umask` (`umask 0002` will leave a
  hand-created file group-writable).
- Service unit files are world-readable. Keep secrets in `.env` /
  `~/.config/claude-slack-bridge.env`, referenced via `EnvironmentFile=`, never
  in an `Environment=` line.

## Data at rest and in transit

- **Transcripts.** The `claude` CLI writes full JSONL transcripts under
  `CLAUDE_CONFIG_DIR/projects/*.jsonl`, including complete tool output — so
  anything a task read from a database or a secret store lands there in
  plaintext, unrotated.
- **Slack.** Task results are posted into Slack and inherit your workspace's
  retention, export, and eDiscovery settings.
- **Thread store.** `$THREAD_STORE_DIR/<thread_ts>.json` holds only ids
  (thread, channel, user, claude session) — no message text.
- **Logs.** `logs/bot.log` records channel/thread/user ids and reply sizes, not
  message content. It is not rotated; add logrotate if the bridge is long-lived.

Both transcripts and Slack history are worth thinking about before pointing a
task at production data.

## Hardening checklist

- [ ] `TRIGGER_USER_ID` is a single human account you control — never a shared
      or bot account.
- [ ] `.env` is mode 600, owned by the service user.
- [ ] The host is one you accept giving that Slack account shell on.
- [ ] `AGENT_WORKSPACE` is a directory you intend the agent to operate in — it
      inherits that tree's `CLAUDE.md` and MCP configuration.
- [ ] Consider a dedicated OS user for the service, with only the credentials
      the tasks actually need.
- [ ] Review `CLAUDE_PERMISSION_MODE` against your threat model rather than
      keeping `bypassPermissions` by default.
- [ ] Slack app tokens are rotated if they ever land in shell history, a shared
      file, or a paste.

## Reporting a vulnerability

Open a GitHub security advisory on this repository. Please do not file a public
issue for anything exploitable.
