"""Drop repeated bot triggers for the same Slack thread.

A Slack workflow that matches on alert text can re-mention the bridge in a
thread it already answered (its own reply quotes the alert), and every
mention would start another full [bg] run. A bot (untrusted) trigger is
skipped when, for the same thread:
  - a bot run was dispatched in this process within the window (covers the
    seconds before the ack is posted and the registry entry is written), or
  - a bg task for the thread is still in the registry, or
  - the bridge posted a reply in the thread within the window, unless that
    reply is a failure notice (":warning: …"): a failed run may be retried.
Operator triggers are never deduped.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_recent: dict[str, float] = {}


def _is_failure(text: str) -> bool:
    return text.lstrip().startswith(":warning:")


def skip_reason(
    thread_ts: str,
    window_secs: int,
    registry_entries: list[dict],
    thread_messages: list[dict],
    bot_user_id: str | None,
    now: float | None = None,
) -> str | None:
    """Return why this trigger should be skipped, or None to run it.
    When it returns None the thread is marked as just dispatched."""
    if window_secs <= 0:
        return None
    now = time.time() if now is None else now
    with _lock:
        for ts, at in list(_recent.items()):
            if now - at > window_secs:
                del _recent[ts]
        last = _recent.get(thread_ts)
        if last is not None:
            return f"bot run dispatched {now - last:.0f}s ago in this process"
        running = [e for e in registry_entries if e.get("slack_thread_ts") == thread_ts]
        if running:
            return f"bg task still running ({running[0].get('tmux_name', '?')})"
        if bot_user_id:
            for m in thread_messages:
                if m.get("user") != bot_user_id or m.get("ts") == thread_ts:
                    continue
                age = now - float(m.get("ts", 0))
                if 0 <= age <= window_secs and not _is_failure(m.get("text") or ""):
                    return f"bridge already replied {age:.0f}s ago ({m.get('ts')})"
        _recent[thread_ts] = now
        return None
