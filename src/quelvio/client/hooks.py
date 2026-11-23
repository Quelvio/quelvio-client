"""hooks.py — Claude Code's hooks on memory's routes.

    hooks.run(data, env, cwd) -> Out(stdout, code)       # one hook run, its JSON input already read (state.read_input)

The events it wires:

| Event | Call | Sends | Prints | Client timeout |
|---|---|---|---|---|
| SessionStart | session/start twice: the id-only body, then (unless the off answer) the full one | source, attended, project, parent, started_at | the block, as additionalContext | 3 s for both |
| UserPromptSubmit | serve, kind prompt | prompt_id and the prompt's parts, labelled by the speaker patterns | the context block | 10 s first, 7 s after |
| SubagentStart | serve, kind subagent_start | | the rules block, to the subagent | 3 s |
| Stop | turn, kind reply, in the background | the reply, under the last prompt's id | nothing | tried twice, 2 s apart, then spooled |
| SessionEnd | session/end, in the background | reason, at most 64 characters | nothing | |

Every run exits 0 and adds nothing on a failure; the rows of the errors table that add a notice add it once per session, to the next block
the agent reads (a background run's notice waits for the next prompt). The client session id goes into Claude Code's environment file at
every SessionStart, whether or not session/start worked. After a failed session/start the client repeats it before its next call; after a
401, 403 or a 402 "plan ended" it makes no call in this session but the next SessionStart's (and /v1/action, after a 402); after a 426, none
until the client's version changes, in any session. The session's state is read and written under its lock, never held across a call, so
a background reply never holds up the next prompt. A session counts as attended only when the runtime proves it, and none does yet:
every session counts as not interactive. The key is read at each run and goes nowhere but
http.send."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass

from quelvio.actions.check import CLASS_WORDS, MATCH
from quelvio.client import VERSION, actions, calls, environment, http, keys, policy, project, spool, state
from quelvio.client.actions import _holds_at, _no_login, _no_userinfo     # the passes the action's own host takes too
from quelvio.text import speakers, subjects

START, SERVE, TURN, ACTION, END, TRACE = http.ROUTES[:6]                         # /v1/handoff is memory_handoff's, never a hook's
SOURCES = ("startup", "clear", "resume", "compact", "fork")
TIMEOUT = {"start": 3.0, "first": 10.0, "prompt": 7.0, "subagent": 3.0, "action": 3.0, "background": 10.0}
MATCH_BACKGROUND = ('Quelvio could not check this {cls}, and a rule names {subject}: "{words}". It was not run. Say in your reply that you '
                    'skipped it and why.')                                            # the text an agent receives
REPORTS_MAX = 50                                   # risky actions the client couldn't check, kept for the next call that reaches the server
SEND_COMMAND = False                               # blank() misses some credentials a command states (*_PASSWORD=, -p, --token, glpat-…)
                                                   # so /v1/action gets no command, only the safe fields
WS = re.compile(r"\s+")
STOP_RETRY = 2.0
REASON_CHARS = 64
TOO_LONG = "This message was too long for Quelvio to read."
CLIENT = "client/all-sessions"                     # the client's own state: a session id never holds "/"
ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}")


@dataclass(frozen=True)
class Out:
    stdout: str = ""
    code: int = 0


def interactive(data, env):
    """Whether the runtime proves this session interactive. Nothing in Claude Code 2.1.311's payloads was found to, so nothing is: anything unknown is "not interactive"."""
    return False


def local_tz(env):
    """The client's IANA time zone: TZ where it names one, else /etc/localtime's link, else UTC."""
    tz = (env.get("TZ") or "").lstrip(":")
    if ZONE.fullmatch(tz): return tz
    try: target = os.readlink("/etc/localtime")
    except OSError: return "UTC"
    name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
    return name if ZONE.fullmatch(name) else "UTC"


def _detach(fn):
    """Run fn in a process of its own that outlives the hook, its standard streams on /dev/null, so Claude Code isn't held up."""
    try: pid = os.fork()
    except OSError: fn(); return
    if pid: return
    try:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2): os.dup2(null, fd)
        fn()
    except BaseException: pass                                                         # noqa: BLE001 — a background call says nothing
    finally: os._exit(0)


def escape(words):
    """shared.served's escape, which the client can't import (it needs regex): white space and every line break collapsed to one space;
    &, <, > and the double quote as entities, so a rule's words can't pass for the client's own sentence (a test holds the two equal)."""
    s = WS.sub(" ", words or "").strip()
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _safe(value, login=False):
    """A field of the action as the body may carry it: its URLs' user info out (a remote's or a host's without a scheme too), then
    unchanged by blank(); "unknown" where blank() would change it, or can't run (a remote, a branch, a path or a host is the
    command's own words, and can hold a secret). A field that still holds an "@" after both passes goes as "unknown" too: a
    password holding a raw "/" or "#" ends the authority early, so neither pass finds its "@", and blank() doesn't know a password's
    shape. A real remote keeps its form: git@github.com:o/r loses git@ to the login pass. An "@" in another form (full-width, small)
    counts the same (NFKC). The passes and the rule are actions.py's, where a deploy's host takes them before it is cut."""
    plain = _no_userinfo(value)
    if login: plain = _no_login(plain)
    if _holds_at(plain): return actions.UNKNOWN
    return plain if _blank(plain) == plain else actions.UNKNOWN


def _fields(action):
    """action.fields() with every string in it made _safe; a list of paths with any path not safe goes as "unknown" whole."""
    out = action.fields()
    for k in ("remote", "branch", "host"):
        if isinstance(out.get(k), str) and out[k] != actions.UNKNOWN: out[k] = _safe(out[k], login=k in ("remote", "host"))
    if isinstance(out.get("paths"), list):
        paths = [_safe(p) if isinstance(p, str) else actions.UNKNOWN for p in out["paths"]]
        out["paths"] = actions.UNKNOWN if actions.UNKNOWN in paths else paths
    return out


def _blank(command):
    """The command with its secrets blanked by shared.text's blank(), the one blanker the server shares; None, so that the command is left
    out and never sent as it stands, where blank() can't be imported or fails."""
    try:
        from quelvio.not_packed.shared_text import blank
        return blank(command)[0]
    except Exception: return None                                                      # noqa: BLE001 — no blanker, no command


class WorkTree:
    """The session's working folder as actions.read asks git about it: the current branch, its upstream and a config value."""

    def __init__(self, cwd): self.cwd = cwd

    def root(self):
        return (project._git(self.cwd, "rev-parse", "--show-toplevel") or "").strip() or os.path.realpath(self.cwd)

    def current_branch(self):
        out = (project._git(self.cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        return out if out and out != "HEAD" else None

    def upstream(self):
        out = (project._git(self.cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "").strip()
        return tuple(out.split("/", 1)) if "/" in out else None

    def config(self, key):
        out = project._git(self.cwd, "config", "--get", key)
        return out.strip() if out else None


def _decision(decision, reason):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}})


def _context(event, text):
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}) if text else ""


def _part(row):
    return {"speaker": row.speaker, "text": row.text, "sender": row.sender, "agent_tag": row.pattern if row.by_agent else None}


class Hook:
    def __init__(self, data, env, cwd, *, send=http.send, keychain=None, now=time.time, clock=time.monotonic, sleep=time.sleep,
                 detach=_detach, patterns=None, platform=sys.platform):
        self.data, self.env, self.cwd = data, env, cwd
        self.sid = state.session_of(data)
        self.state, self.all = state.State(env, self.sid), state.State(env, CLIENT)
        self.agent = env.get("QUELVIO_AGENT") or None
        try: self.base = http.server(env)                                             # the pinned server; this machine only when the pin names it
        except ValueError: self.base = None                                            # QUELVIO_URL names another server: no call at all
        self.tz = local_tz(env)
        self.parent = env.get("QUELVIO_PARENT_SESSION") if env.get("QUELVIO_PARENT_SESSION") != self.sid else None
        self.attended = interactive(data, env)
        self._send, self._keychain, self._platform = send, keychain, platform
        self.now, self.clock, self.sleep, self.detach = now, clock, sleep, detach
        self.patterns = patterns
        self.key = self.key_id = None
        self.last = None                                   # the last call's Reply, for a report's cause

    # ---------------------------------------------------------------- the key, the state
    def read_key(self):
        kc = self._keychain if self._keychain is not None else keys.keychain_for(self._platform, self.env)
        try: key = keys.read_key(self.agent, self.env, kc)
        except (keys.NoKey, keys.NotAKey, keys.KeychainError, ValueError, OSError): return False
        m = http.KEY.fullmatch(key)
        if not m: return False
        self.key, self.key_id = key, m.group(2)
        return True

    def update(self, fn):
        """fn(state) under the session's lock, the file read fresh and written back whole."""
        with self.state.locked():
            d = self.state.load(); out = fn(d); self.state.save(d)
        return out

    def stopped_version(self):
        return self.all.load().get("stopped_version")

    # ---------------------------------------------------------------- one call
    def call(self, route, own, timeout, *, id_only=False, prompt_id=None, final=True, event=None):
        """One call, if the session's stops allow it, with a failed session/start repeated first. None when no call was made, or when a
        call that isn't `final` failed in the way a later try may mend (nothing is then settled); else calls.Settled."""
        if self.base is None: return None
        snap = self.state.load()
        if not policy.may_call({**snap, "stopped_version": self.stopped_version()}, route, VERSION): return None
        if route not in (START, ACTION) and snap.get("repeat_start"):                 # the action call waits on nothing (Hooks table)
            self.start(snap.get("source") or "startup")
            snap = self.state.load()
            if not policy.may_call({**snap, "stopped_version": self.stopped_version()}, route, VERSION): return None
        sp = spool.Spool(self.env, self.key_id)
        reports = list(snap.get("reports") or ())
        built = calls.build(route, own, session=self.sid, ack=snap.get("ack"), client_version=VERSION, tz=self.tz, id_only=id_only,
                            reports=reports, spool=sp, now=self.now())
        reply = self.last = self._send(self.base, route, built.data, self.key, agent=self.agent, timeout=timeout)
        if not final and policy.outcome(route, reply.status, reply.word, event).repeat_start: return None
        s = calls.settle(reply, route, built, spool=sp, session=self.sid, prompt_id=prompt_id, event=event, now=self.now())
        self.update(lambda d: self.apply(d, s, route, built))
        return s

    def apply(self, d, s, route, built):
        o, answer = s.outcome, s.answer or {}
        if o.ok and not s.off:
            d["open"] = True
            if isinstance(answer.get("subjects"), list): d["subjects"] = answer["subjects"]
            if route == START and isinstance(answer.get("session_id"), str):
                d["quelvio_session"], d["block_id"] = answer["session_id"], answer.get("block_id")
        if o.ok and route == START: d.pop("repeat_start", None)                      # the off answer too: nothing more to learn
        if s.delivered and "reports" in built.body:
            sent = built.body["reports"]
            d["reports"] = [r for r in d.get("reports") or [] if r not in sent]
        if route == START and o.repeat_start: d["repeat_start"] = True
        if o.stop == "until the version changes":
            with self.all.locked():
                a = self.all.load(); a["stopped_version"] = VERSION; self.all.save(a)
        elif o.stop: d["stop"] = o.stop
        if o.notice: self.queue(d, o.notice)

    @staticmethod
    def queue(d, notice):
        if notice not in (d.get("shown") or []) and notice not in (d.get("notices") or []):
            d.setdefault("notices", []).append(notice)

    def take_notices(self, d):
        out = d.pop("notices", None) or []
        d["shown"] = (d.get("shown") or []) + out
        return out

    def printed(self, event, s, extra=()):
        """The block the agent reads: a 200's text (its id then acknowledged), then the notices the session hasn't shown yet."""
        def take(d):
            text = ""
            if s is not None and s.outcome.ok and not s.off and isinstance((s.answer or {}).get("text"), str):
                text = s.answer["text"]; d["ack"] = s.answer["id"]
            for n in list(extra): self.queue(d, n)
            notes = self.take_notices(d) if event != "SubagentStart" else []
            return "\n".join([t for t in [text] + notes if t])
        return _context(event, self.update(take))

    # ---------------------------------------------------------------- the events
    def start(self, source):
        """The id-only session/start, then the full one unless the answer was off, within the hook's one budget. The full answer, or None."""
        started = self.update(lambda d: d.setdefault("started_at", state.iso(self.now())))
        t0 = self.clock()
        first = self.call(START, {}, TIMEOUT["start"], id_only=True)
        if first is None or not first.outcome.ok or first.off: return None
        own = {"source": source, "attended": self.attended, "project": project.project_id(self.cwd), "parent": self.parent,
               "started_at": started}
        return self.call(START, own, max(0.5, TIMEOUT["start"] - (self.clock() - t0)))

    def session_start(self):
        source = self.data.get("source") if self.data.get("source") in SOURCES else "startup"
        try: environment.add_to_env_file(self.env, "QUELVIO_PARENT_SESSION", self.sid)
        except (OSError, ValueError): pass
        self.state.prune(); spool.prune(self.env, self.now())

        def fresh(d):
            d.pop("stop", None)                                                        # one session/start at each SessionStart
            d.pop("repeat_start", None); d["source"] = source
        self.update(fresh)
        if not self.read_key(): return Out()
        return Out(self.printed("SessionStart", self.start(source)))

    def prompt(self):
        if not self.read_key(): return Out()
        text = self.data.get("prompt") if isinstance(self.data.get("prompt"), str) else ""
        pid = state.prompt_id_of(self.data)
        rows = (self.patterns or speakers.load()).rows(text, interactive=self.attended, parent=self.agent if self.parent else None)
        own, extra = {"kind": "prompt", "prompt_id": pid, "parts": [_part(r) for r in rows]}, []
        try: calls.build(SERVE, own, session=self.sid, ack=None, client_version=VERSION, tz=self.tz)
        except calls.TooLarge:
            own["parts"], extra = [{"speaker": "quoted", "text": "[too long: %d characters]" % len(text), "sender": None, "agent_tag": None}], [TOO_LONG]

        def count(d):
            n = d.get("prompts", 0); d["prompts"], d["last_prompt"] = n + 1, pid
            return n == 0
        first = self.update(count)
        s = self.call(SERVE, own, TIMEOUT["first" if first else "prompt"], prompt_id=pid)
        return Out(self.printed("UserPromptSubmit", s, extra))

    def subagent(self):
        if not self.read_key(): return Out()
        return Out(self.printed("SubagentStart", self.call(SERVE, {"kind": "subagent_start"}, TIMEOUT["subagent"])))

    def stop(self):
        rows = (self.patterns or speakers.load()).reply(self.data.get("last_assistant_message") or "")
        if not rows or not self.read_key(): return Out()
        pid = self.state.load().get("last_prompt") or state.prompt_id_of({})
        own = {"prompt_id": pid, "kind": "reply", "speaker": "agent", "text": rows[0].text}
        try: calls.build(TURN, own, session=self.sid, ack=None, client_version=VERSION, tz=self.tz)
        except calls.TooLarge:
            own["text"] = "[too long: %d characters]" % len(rows[0].text)
            self.update(lambda d: self.queue(d, TOO_LONG))

        def reply():
            if self.call(TURN, own, TIMEOUT["background"], prompt_id=pid, final=False) is None and \
                    policy.may_call({**self.state.load(), "stopped_version": self.stopped_version()}, TURN, VERSION):
                self.sleep(STOP_RETRY)
                self.call(TURN, own, TIMEOUT["background"], prompt_id=pid)
        self.detach(reply)
        return Out()

    def _action(self):
        tool, given = self.data.get("tool_name"), self.data.get("tool_input")
        if not isinstance(tool, str): return None, None
        tree = WorkTree(self.cwd)
        try: action = actions.read(tool, given, self.cwd, tree.root(), tree)
        except Exception:                                                              # noqa: BLE001 — never allowed unchecked
            text = given.get("command") if isinstance(given, dict) and isinstance(given.get("command"), str) else tool
            found = actions._text_classes(text)                                        # the parser failed: every class its words name
            action = actions.Action(actions._ordered(found), tool=tool) if found else None
            self.unread = action is not None
        call_id = self.data.get("tool_use_id") if isinstance(self.data.get("tool_use_id"), str) else ""
        mode = self.data.get("permission_mode") if isinstance(self.data.get("permission_mode"), str) else "default"
        return action, {"tool": tool, "tool_call_id": call_id, "permission_mode": mode}

    def pre_tool_use(self):
        """A risky call goes to /v1/action (3 s); the server's ask or deny is the hook's answer, its allow none. On anything but a 200, the
        client makes its own match on the subjects it holds, and the action is reported with the next call that reaches the server. The
        off answer allows with no check."""
        action, call = self._action()
        if action is None or not self.read_key(): return Out()
        if getattr(self, "unread", False): return self.client_check(action, call, "not_reached")      # a call it couldn't read isn't sent
        own = {"event": "pre", **call, **_fields(action)}                             # no field of the command's words unblanked
        given = self.data.get("tool_input") if isinstance(self.data.get("tool_input"), dict) else {}
        whole = given.get("command") if call["tool"] == "Bash" and isinstance(given.get("command"), str) else action.command
        command = _blank(_no_userinfo(whole)) if SEND_COMMAND else None   # whole, its URLs' user info out; an MCP tool's name and argument names
        if command is not None:
            own["command"] = command
            try: calls.build(ACTION, own, session=self.sid, ack=None, client_version=VERSION, tz=self.tz)
            except calls.TooLarge: own["command"] = "[too long: %d characters]" % len(command)
        s = self.call(ACTION, own, TIMEOUT["action"], event="pre")
        if s is not None and s.outcome.ok:
            if s.off: return Out()
            decision, reason = (s.answer or {}).get("decision"), (s.answer or {}).get("reason")
            return Out(_decision(decision, reason)) if decision in ("ask", "deny") and isinstance(reason, str) else Out()
        return self.client_check(action, call, (self.last.cause if s is not None and self.last else None) or "not_reached")

    def client_check(self, action, call, cause):
        """The check couldn't run: the client's own match (actions.client_match) on the rules' subjects the last answer gave, and a report."""
        rules = self.state.load().get("subjects")
        rules = rules if isinstance(rules, list) else None
        m = actions.client_match(action, [r.get("subjects") if isinstance(r, dict) else None for r in rules] if rules is not None else None,
                                 self.attended)
        report = {**call, "class": action.cls, "hook_answer": m.decision, "cause": cause, "at": state.iso(self.now())}
        self.update(lambda d: d.__setitem__("reports", ((d.get("reports") or []) + [report])[-REPORTS_MAX:]))
        if m.decision == "allow": return Out()
        rule = rules[m.rule]
        subject = next((x for x in rule.get("subjects") or [] if isinstance(x, str) and m.cls in subjects.classes([x])), "")
        text = (MATCH if m.decision == "ask" else MATCH_BACKGROUND).format(cls=CLASS_WORDS.get(m.cls, m.cls), subject=escape(subject),
                                                                            words=escape(rule.get("words") or ""))
        return Out(_decision(m.decision, text))

    def post_tool_use(self):
        """A risky call that ran: /v1/action with event post, in the background (after an ask, it means the person allowed it)."""
        action, call = self._action()
        if action is None or not self.read_key(): return Out()
        self.detach(lambda: self.call(ACTION, {"event": "post", **call}, TIMEOUT["background"], event="post"))
        return Out()

    def session_end(self):
        if not self.read_key(): return Out()
        reason = self.data.get("reason") if isinstance(self.data.get("reason"), str) else "other"
        self.detach(lambda: self.call(END, {"reason": reason[:REASON_CHARS]}, TIMEOUT["background"]))
        return Out()


EVENTS = {"SessionStart": Hook.session_start, "UserPromptSubmit": Hook.prompt, "SubagentStart": Hook.subagent, "Stop": Hook.stop,
          "SessionEnd": Hook.session_end, "PreToolUse": Hook.pre_tool_use, "PostToolUse": Hook.post_tool_use}


def run(data, env, cwd, **kw):
    """One hook run. Whatever happens, it exits 0, and on any failure it prints nothing."""
    try:
        event = EVENTS.get(data.get("hook_event_name"))
        return event(Hook(data, env, cwd, **kw)) if event else Out()
    except Exception:                                                                  # noqa: BLE001 — a hook's failure adds nothing
        return Out()
