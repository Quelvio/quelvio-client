"""policy.py — what the client does with each answer. It holds the errors
table, row by row, and says nothing about how a call is made.

| Answer | What the hook adds | The turn | Next calls |
|---|---|---|---|
| No answer, a timeout, an unreadable body, 429, any 5xx, anything else | Nothing (a failed session/start: one line) | Spooled | session/start is repeated before the next call |
| 401 or 403 | One notice per session: "Quelvio refused this key or agent name" | Not spooled; the key's spool is deleted | None in this session |
| 402 "plan ended" | One notice per session: "Quelvio's plan has ended. Memory is paused." | Not spooled; the spool stays | Only /v1/action in this session |
| 402 "plan limit reached" (/v1/turn) | Nothing | Dropped, not sent again; the spool rides as usual | As usual |
| 426 | One notice: "Update the Quelvio client" | Not spooled; the spool is emptied | None until the client's version changes |
| 422 | Nothing | That item is dropped and counted | As usual |

PreToolUse is the one exception in every row but the first: on anything but a 200 from /v1/action, the client makes its own match, and the
action is reported with the next call that reaches the server."""
from dataclasses import dataclass

REFUSED = "Quelvio refused this key or agent name"
PLAN_ENDED = "Quelvio's plan has ended. Memory is paused."
UPGRADE = "Update the Quelvio client"
START_FAILED = "Quelvio memory is not available right now. Work as usual."
START = "/v1/session/start"
ACTION = "/v1/action"


@dataclass(frozen=True)
class Outcome:
    ok: bool = False
    spool: bool = False
    drop: bool = False
    notice: str = None
    delete_key_spool: bool = False
    stop: str = None                               # None, "session", "all but action", "until the version changes"
    repeat_start: bool = False
    client_match: bool = False
    report: bool = False


def outcome(route, status, word, event=None):
    """The errors table's row for one answer: `status` None when nothing came back; `word` the body's error word, or "unreadable"."""
    if route == ACTION and event == "pre":
        if status == 200 and word != "unreadable": return Outcome(ok=True)
        return Outcome(client_match=True, report=True)
    if status == 200 and word != "unreadable": return Outcome(ok=True)
    if status in (401, 403): return Outcome(notice=REFUSED, delete_key_spool=True, stop="session")
    if status == 402 and word == "plan ended": return Outcome(notice=PLAN_ENDED, stop="all but action")
    if status == 402 and word == "plan limit reached": return Outcome(drop=True)
    if status == 426: return Outcome(notice=UPGRADE, delete_key_spool=True, stop="until the version changes")
    if status == 422: return Outcome(drop=True)
    return Outcome(spool=route != ACTION, repeat_start=True, notice=START_FAILED if route == START else None)


def may_call(session_state, route, version):
    """Whether the client calls `route` now, after the stops the session (or a 426) left."""
    if session_state.get("stopped_version") == version: return False
    stop = session_state.get("stop")
    if stop == "session": return False
    if stop == "all but action": return route == ACTION
    return True


def notice_once(shown, notice):
    """A notice the session hasn't shown yet, recorded as shown; None otherwise."""
    if not notice or notice in shown: return None
    shown.add(notice)
    return notice
