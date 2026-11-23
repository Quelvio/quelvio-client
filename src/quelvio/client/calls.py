"""calls.py — a route's body, built under the limit, and what the client does with the answer.

build(): the body of one call, in the API contract's order. First the call's own fields with the common ones (client_version,
tz, session, ack); then the reports of risky actions the client couldn't check; then the spooled turns of this key, oldest first, each that
fits (one that doesn't is passed over, the next that fits rides); then the instruction files the last answer wanted, whole, in the order
listed, while the body stays under 1 MiB (the rest wait for the call after). A file too large for the limit alone goes as too_long with no
text. A wanted file never rides on /v1/action. The id-only session/start carries client_version, tz and session alone; /v1/trace and
/v1/handoff carry client_version and tz and their own fields, and none of every route's others. /v1/serve, /v1/turn, /v1/action and
/v1/session/end take session_id (the Quelvio session id, among the call's own fields) in place of session, in session's place, exactly one
of the two; /v1/session/start takes session alone. A call whose own content alone passes the limit is TooLarge:
the hook sends its marker instead.

settle(): the errors table (policy.py) applied to the spool, and the rest handed back for the hook, which keeps the session's state:
  · a 200 delivers the turns that rode, and the reports;
  · the off answer is a 200 too: the server discarded the body unread, and nothing is spooled, since the call was answered;
  · a failure spools the call's own turn (a prompt to /v1/serve, or a reply to /v1/turn), and the turns that rode stay spooled;
  · a 401, 403 or 426 deletes this key's spool;
  · a 422 drops the call's own turn and the turns that rode with it, each counted: the answer can't say which was refused, and a turn the
    server refuses would otherwise ride, and be refused, with every call for a day;
  · a 402 "plan ended" or "plan limit reached" keeps the spool as it is."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from quelvio.client import policy
from quelvio.client.http import LIMIT, ROUTES

START, SERVE, TURN, ACTION, END, TRACE, HANDOFF = ROUTES
OWN_ONLY = (TRACE, HANDOFF)                         # client_version, tz and their own fields
SESSION_IN_PLACE = (SERVE, TURN, ACTION, END)       # session_id in place of session
COMMON = ("client_version", "tz", "session", "ack")
ID_ONLY = ("client_version", "tz", "session")
TRACE_COMMON = ("client_version", "tz")


class TooLarge(ValueError):
    """The call's own content alone passes the body limit."""


def wire(d):
    """The body as sent: compact JSON, every character past ASCII escaped, so a lone surrogate is still valid JSON and a size is the spool's."""
    return json.dumps(d, separators=(",", ":")).encode("ascii")


def _size(d): return len(wire(d))


@dataclass(frozen=True)
class Built:
    data: bytes
    body: dict
    riding: list = field(default_factory=list)      # the spooled items this call carries, as they went
    files: list = field(default_factory=list)       # the instruction files it carries, by path, sent whole or as too_long


def instruction_entry(file, kind, raw):
    """One wanted instruction file as the body carries it: its text and the SHA-256 of the bytes sent."""
    return {"file": file, "kind": kind, "hash": hashlib.sha256(raw).hexdigest(), "text": raw.decode("utf-8", "replace")}


def _session(route, common, own):
    """The common fields with the session named once: session, or on the routes that take it, session_id in session's place."""
    if "session_id" in own and route not in SESSION_IN_PLACE: raise ValueError("%s takes session alone" % route)
    if ("session_id" in own) == (common["session"] is not None): raise ValueError("a call names its session exactly one way: session or session_id")
    if "session_id" not in own: return common
    return {("session_id" if k == "session" else k): (own["session_id"] if k == "session" else v) for k, v in common.items()}


def build(route, own, *, session, ack, client_version, tz, id_only=False, reports=(), spool=None, files=(), now=0.0):
    """The body of one call to `route`. `own`: the call's own fields. `spool`: the key's Spool, or None. `files`: the wanted instruction
    files as instruction_entry() gives them, in the order the answer listed them."""
    if route not in ROUTES: raise ValueError("not one of memory's routes")
    common = {"client_version": client_version, "tz": tz, "session": session, "ack": ack}
    if route in OWN_ONLY: body = {**{k: common[k] for k in TRACE_COMMON}, **own}
    else:
        common = _session(route, common, own)
        body = {k: common[k] for k in ID_ONLY} if route == START and id_only else {**common, **own}
    if _size(body) > LIMIT: raise TooLarge("the call's own content passes the body limit")
    if route in OWN_ONLY or (route == START and id_only): return Built(wire(body), body)
    if reports:
        with_reports = {**body, "reports": list(reports)}
        if _size(with_reports) <= LIMIT: body = with_reports
    riding = []
    if spool is not None:
        room = LIMIT - _size({**body, "spool": []})
        riding = spool.fitting(room, now) if room > 0 else []
        if riding: body = {**body, "spool": riding}
    sent = []
    if route != ACTION and files:
        carried = []
        for f in files:
            alone = _size({**common, **own, "instructions": [f]}) <= LIMIT                # the session named as the body names it
            entry = f if alone else {**{k: f[k] for k in ("file", "kind", "hash")}, "too_long": True}
            if _size({**body, "instructions": carried + [entry]}) > LIMIT: break
            carried.append(entry); sent.append(f["file"])
        if carried: body = {**body, "instructions": carried}
    return Built(wire(body), body, riding, sent)


def _turn(route, body):
    """Whether the call carries a turn the spool keeps: a prompt to /v1/serve, or a reply (or a prompt) to /v1/turn."""
    return route == TURN or (route == SERVE and body.get("kind") == "prompt")


@dataclass(frozen=True)
class Settled:
    outcome: policy.Outcome
    off: bool = False
    answer: dict | None = None                     # a 200's body, the off answer's included
    delivered: bool = False                        # the turns and reports that rode reached the server


def settle(reply, route, built, *, spool=None, session=None, prompt_id=None, event=None, now=0.0):
    """Apply the errors table's row for `reply` to the spool, and hand back the outcome for the hook (notice, stop, repeat_start,
    client_match, report)."""
    o = policy.outcome(route, reply.status, reply.word, event)
    if o.ok:
        if spool is not None and built.riding: spool.sent(built.riding)
        return Settled(o, reply.off, reply.body, True)
    if spool is not None:
        if o.delete_key_spool: spool.delete()
        elif reply.status == 422:
            spool.drop(built.riding, also=1 if _turn(route, built.body) else 0)
        elif o.spool and _turn(route, built.body):
            spool.add({"route": route, "body": {k: v for k, v in built.body.items() if k not in ("spool", "reports")}}, session, prompt_id, now)
    return Settled(o)
