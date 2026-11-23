"""The speaker labeller (2.6).

    P = load()                                                         # patterns/claude-code.v1.json
    P.rows(prompt, interactive=False, parent=None, children=())        # the prompt's rows, one per part, in order
    P.reply(text)                                                      # the agent's own reply: one row, speaker "agent"

Five speakers: person, relayed, agent, quoted, harness. One versioned pattern file per runtime holds every tag and wrapper line the harness
writes, each matched by its shape and none a list of words, each with its own tests. Harness parts are dropped wherever they stand, in a
prompt, a brief or a reply; Quelvio's own blocks (every element whose tag starts with "quelvio-") are harness. A relayed part keeps its
sender's name, id and kind (a child session's agent, the parent session's, or another), and says whether an agent-message pattern matched
it, for the server's turn.relayed_by_agent. Text no pattern matches is the person's only when the runtime proves the session interactive;
in a headless run it is relayed at most, from the parent session's agent when the session has one, and from no agent when it has none.
Quoted parts are found by shape alone: fenced blocks, lines starting with ">", mail headers, and any paragraph over 2,000 characters. In a
typed prompt each is kept as a marker such as "[pasted: 312 lines, log]"; inside a relayed part its text stays, for the relay check.
Quoted and harness parts add no row; a prompt that is only pasted is one row, speaker "quoted", whose text is the marker.

A relayed tag anywhere in a prompt, an end tag or one inside an attribute or a reminder included, leaves nothing in it the person's: its
untagged words are relayed from no known sender and counted as an agent's (client 1.5.0's rule), since a sender can close its
own tag early and write on. A
relayed message the harness delivers inside a reminder is relayed, with its sender, only in a reminder whose first line is the desktop's own
lead-in and right after a lead-in line (a reminder also shows files read or changed, and a tag in such a file is dropped); inside any other harness element (command output, a task
notice, Quelvio's own blocks) it is dropped with the rest.

Standard library only, because the client bundles it. One pass over the tags and one over the lines: linear on any text."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

SPEAKERS = ("person", "relayed", "agent", "quoted", "harness")
AUTHORITY = ("harness", "quoted", "relayed", "agent", "person")                              # least first: where two labels disagree, the lesser wins
SENDER_KINDS = ("child", "parent", "other")
QUOTED_PARAGRAPH = 2000                                                                     # characters: a longer paragraph is a paste
TAG_CHARS = 2000                                                                            # how much of an open tag is read for its sender

TAG_NAME = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")
TAG_PREFIX = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*-\Z")
ATTR = re.compile(r"([A-Za-z_][\w:.-]{0,40})\s*=\s*(?:\"([^\"]{0,400})\"|'([^']{0,400})')")
FENCE = re.compile(r"[ ]{0,3}(`{3,}|~{3,})(.*)")
FENCE_END = re.compile(r"[ ]{0,3}(`{3,}|~{3,})[ \t]*\Z")
INFO = re.compile(r"[A-Za-z0-9_+-]{1,20}\Z")
QUOTE_LINE = re.compile(r"[ ]{0,3}>")
HEADER_LINE = re.compile(r"[^\W\d_][\w-]{0,40}:[ \t]+\S")
EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
MARKER = re.compile(r"\[pasted: \d+ lines?, [A-Za-z0-9_+-]{1,20}\]|\[too long: \d+ characters\]")   # the oversized-turn marker too


@dataclass(frozen=True)
class Row:
    speaker: str
    text: str
    sender: str | None = None
    sender_id: str | None = None
    sender_kind: str | None = None
    by_agent: bool = False                                                                  # an agent-message pattern matched, or the parent's agent
    pattern: str | None = None


def weaker(a: str, b: str) -> str:
    """The label with less authority: the server's check and the client's, where they disagree."""
    for s in (a, b):
        if s not in AUTHORITY: raise ValueError(f"{s!r} is not one of the five speakers")
    return min(a, b, key=AUTHORITY.index)


def _check(p) -> dict:
    pid = p.get("id") if isinstance(p, dict) else None
    if not isinstance(pid, str) or not pid: raise ValueError("a pattern has an id")
    shapes = [k for k in ("element", "element_prefix", "line_prefix") if k in p]
    if len(shapes) != 1: raise ValueError(f"pattern {pid!r} has one shape: element, element_prefix or line_prefix")
    v = p[shapes[0]]
    if shapes[0] == "element" and not (isinstance(v, str) and TAG_NAME.match(v)):
        raise ValueError(f"pattern {pid!r}: an element is one tag's name, never a list of words")
    if shapes[0] == "element_prefix" and not (isinstance(v, str) and TAG_PREFIX.match(v)):
        raise ValueError(f"pattern {pid!r}: an element prefix is the start of a tag's name, ending in '-'")
    if shapes[0] == "line_prefix" and not (isinstance(v, str) and v.strip() and v.lstrip() == v and "\n" not in v):
        raise ValueError(f"pattern {pid!r}: a line prefix is a wrapper's own fixed line")
    if p.get("speaker") not in ("harness", "relayed"): raise ValueError(f"pattern {pid!r}: its speaker is harness or relayed")
    if p.get("sender_kind") not in (None, "parent"): raise ValueError(f"pattern {pid!r}: its sender kind is parent or left to the tag")
    if "delivers" in p and not (p["delivers"] is True and p["speaker"] == "harness" and "element" in p):
        raise ValueError(f"pattern {pid!r}: delivers is true, on a harness element only")
    if ("delivers" in p) != ("delivers_after" in p) or ("delivers_after" in p and not (
            isinstance(p["delivers_after"], str) and p["delivers_after"].strip() == p["delivers_after"] and p["delivers_after"] and "\n" not in p["delivers_after"])):
        raise ValueError(f"pattern {pid!r}: delivers goes with delivers_after, the one line the harness writes right before a message it delivers")
    for k in ("sender", "sender_id"):
        if k in p and not (isinstance(p[k], str) and ATTR.match(p[k] + '=""')): raise ValueError(f"pattern {pid!r}: {k} names one attribute")
    if not (isinstance(p.get("tests"), list) and p["tests"]): raise ValueError(f"pattern {pid!r} has no test")
    return p


class Patterns:
    def __init__(self, raw: dict):
        self.runtime, self.version = raw.get("runtime"), raw.get("version")
        if not isinstance(self.runtime, str) or not isinstance(self.version, int) or isinstance(self.version, bool):
            raise ValueError("a pattern file names its runtime and its version")
        self.patterns = [_check(p) for p in raw.get("patterns") or []]
        if len({p["id"] for p in self.patterns}) != len(self.patterns): raise ValueError("pattern ids are unique")
        self._elements = {p["element"]: p for p in self.patterns if "element" in p}
        self._prefixes = [(p["element_prefix"], p) for p in self.patterns if "element_prefix" in p]
        self._lines = [p for p in self.patterns if "line_prefix" in p]
        alts = [re.escape(n) for n in sorted(self._elements, key=len, reverse=True)] + [re.escape(x) + r"[a-z0-9_-]*" for x, _ in self._prefixes]
        self._tag = re.compile(r"<(/?)(" + "|".join(alts) + r")(?=[\s>/])") if alts else None
        rel = [re.escape(p["element"]) for p in self.patterns if p["speaker"] == "relayed" and "element" in p] + \
              [re.escape(p["element_prefix"]) + r"[a-z0-9_-]*" for p in self.patterns if p["speaker"] == "relayed" and "element_prefix" in p]
        self._relayed_tag = re.compile(r"</?(?:" + "|".join(rel) + r")(?=[\s>/])") if rel else None

    def _pattern(self, name):
        p = self._elements.get(name)
        if p is not None: return p
        return next(p for x, p in self._prefixes if name.startswith(x))

    # ------------------------------------------------------------------ the tags
    def _scan(self, t: str, transparent: bool = False) -> list:
        """The text's top-level pieces in order: ("text", start, end) and ("el", pattern, start, end, attrs) for an element's inside. Nested
        elements of the same name are counted, so an end ends only its own; one left open runs to the end; an end with nothing open takes
        the words since the last cut as that element's (it errs only by giving words less authority). With `transparent`, a relayed tag is
        cut and its words kept as text, and only harness elements are pieces of their own (a relayed part's inside, a reply)."""
        if self._tag is None or "<" not in t: return [("text", 0, len(t))]
        out, start, depth, cur, inner, attrs, pat, opened = [], 0, 0, None, 0, {}, None, 0
        nxt = [-2]

        def gt(pos):                                                                        # the next ">" at or after pos, each looked for once
            if nxt[0] == -1 or nxt[0] >= pos: return nxt[0]
            nxt[0] = t.find(">", pos); return nxt[0]

        def after(pos):
            g = gt(pos); return pos if g < 0 else g + 1

        for m in self._tag.finditer(t):
            if m.start() < start: continue                                                  # inside a tag already cut
            closing, name = m.group(1) == "/", m.group(2)
            if depth:
                if name == cur:
                    depth += -1 if closing else 1
                    if depth == 0:
                        out.append(("el", pat, inner, m.start(), attrs, opened)); start = after(m.end())
                continue
            p = self._pattern(name)
            if transparent and p["speaker"] == "relayed":
                out.append(("text", start, m.start())); start = after(m.end()); continue
            if closing:
                out.append(("el", p, start, m.start(), {}, start)); start = after(m.end()); continue
            out.append(("text", start, m.start()))
            g = gt(m.end())
            if g < 0:                                                                       # an open tag never ended: the rest is its
                out.append(("el", p, len(t), len(t), {}, m.start())); start = len(t); break
            head = t[m.end():min(g, m.end() + TAG_CHARS)]
            a = {k: v1 or v2 for k, v1, v2 in ATTR.findall(head)}
            if g - m.end() <= TAG_CHARS and head.rstrip().endswith("/"):                    # <tag ... />: empty
                out.append(("el", p, g + 1, g + 1, a, m.start())); start = g + 1; continue
            cur, depth, inner, attrs, pat, opened = name, 1, g + 1, a, p, m.start()
        if depth: out.append(("el", pat, inner, len(t), attrs, opened))
        elif start < len(t): out.append(("text", start, len(t)))
        return out

    def _inside(self, t: str) -> str:
        """A relayed part's or a reply's words: harness elements cut, relayed tags cut with their words kept."""
        return "".join(t[pc[1]:pc[2]] for pc in self._scan(t, transparent=True) if pc[0] == "text")

    # ------------------------------------------------------------------ the wrapper lines
    def _split_lines(self, s: str):
        """The text's pieces with each harness wrapper line cut: ("text", words) and ("lead", pattern) where a brief's lead-in starts."""
        if not self._lines: yield ("text", s); return
        buf = []
        for ln in s.splitlines(keepends=True):
            bare = ln.lstrip()
            p = next((p for p in self._lines if bare.startswith(p["line_prefix"])), None)
            if p is None: buf.append(ln); continue
            if p["speaker"] == "harness": continue
            if buf: yield ("text", "".join(buf)); buf = []
            yield ("lead", p)
        if buf: yield ("text", "".join(buf))

    # ------------------------------------------------------------------ rows
    def rows(self, text: str, *, interactive: bool = False, parent: str | None = None, children=()) -> list:
        """The prompt's rows, one per part in order, sharing the caller's prompt id. `interactive` only when the runtime proves it;
        `parent` names the parent session's agent, if the session has one; `children` the registered names of the agents whose
        sessions' parent is this session."""
        t, kids, parts, brief = text or "", set(children or ()), [], None
        tagged = bool(self._relayed_tag and self._relayed_tag.search(t))
        for pc in self._scan(t):
            if pc[0] == "el":
                _, p, s, e, a, _opened = pc
                if p["speaker"] == "harness":                                               # a message delivered inside a reminder alone
                    if not p.get("delivers"): continue
                    inner = t[s:e]
                    if not inner.lstrip().startswith(p["delivers_after"]): continue        # the desktop's lead-in is the reminder's first line
                    for q in self._scan(inner):                                             # only right after the desktop's own lead-in
                        if q[0] == "el" and q[1]["speaker"] == "relayed" and _right_after(inner, q[5], p["delivers_after"]):
                            parts.append(self._relayed(q[1], q[4], inner[q[2]:q[3]], kids, len(parts)))
                    continue
                parts.append(self._relayed(p, a, t[s:e], kids, len(parts)))
                continue
            for k, v in self._split_lines(t[pc[1]:pc[2]]):
                if k == "lead": brief = v; continue
                if brief is not None: parts.append(["relayed", v, parent, None, "parent", True, brief["id"], None])
                elif interactive and not tagged: parts.append(["person", v, None, None, None, False, None, None])
                elif interactive: parts.append(["relayed", v, None, None, None, True, None, None])   # beside a relayed tag: an agent's
                elif parent and not tagged: parts.append(["relayed", v, parent, None, "parent", True, None, None])   # beside a tag: no known sender
                else: parts.append(["relayed", v, None, None, None, tagged, None, None])             # no tag, no parent: from no agent
        merged = []                                                                         # untagged text cut by harness is one part again;
        for x in parts:                                                                     # each relayed element stays a part of its own
            if merged and x[7] is None and merged[-1][7] is None and merged[-1][0] == x[0] and merged[-1][2:7] == x[2:7]: merged[-1][1] += x[1]
            else: merged.append(list(x))
        out, marks = [], []
        for x in merged:
            words = x[1]
            if x[0] == "person":
                words, m = _quote(words)
                if not MARKER.sub("", words).strip(): marks += m; continue
            words = words.strip()
            if words: out.append(Row(x[0], words, *x[2:7]))
        if not out and marks: return [Row("quoted", "\n".join(marks))]
        return out

    def _relayed(self, p, a, inner, kids, idx):
        name = a.get(p.get("sender")) or None
        kind = "parent" if p.get("sender_kind") == "parent" else "child" if name in kids else "other"
        return ["relayed", self._inside(inner), name, a.get(p.get("sender_id")) or None, kind, bool(p.get("agent")), p["id"], idx]

    def reply(self, text: str) -> list:
        """The session's own agent's reply: harness cut, quoted parts as markers; one row, speaker "agent", or none."""
        words = _quote(self._inside(text or ""))[0].strip()
        return [Row("agent", words)] if words else []


def _right_after(text: str, at: int, lead: str) -> bool:
    """Whether the line just before position `at` (or the start of its own line) is `lead`, alone: read in a bounded window, so linear."""
    lo = max(0, at - len(lead) - 400)
    seg = text[lo:at].rstrip()
    if not seg.endswith(lead): return False
    line = seg.rsplit("\n", 1)[-1]
    return line.strip() == lead and ("\n" in seg or lo == 0)


def _marker(n: int, kind: str) -> str:
    return f"[pasted: {n} line{'' if n == 1 else 's'}, {kind}]"


def _runs(text: str) -> list:
    """The quoted runs of the text's lines, in order, never overlapping: (first line, line after the last, marker). Shapes alone, in any
    language: a fenced block (one left open runs to the end), a run of lines starting with ">", two or more header lines holding an
    address, and a paragraph (lines between blank lines and other runs) over 2,000 characters."""
    lines, runs, plain, i = text.split("\n"), [], [], 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        f = FENCE.match(ln)
        if f and not (f.group(1)[0] == "`" and "`" in f.group(2)):
            ch, run, info = f.group(1)[0], len(f.group(1)), f.group(2).split()
            j = i + 1
            while j < n:
                e = FENCE_END.match(lines[j])
                if e and e.group(1)[0] == ch and len(e.group(1)) >= run: break
                j += 1
            end = min(j, n - 1)
            runs.append((i, end + 1, _marker(end - i + 1, info[0] if info and INFO.match(info[0]) else "code"))); i = end + 1; continue
        if QUOTE_LINE.match(ln):
            j = i
            while j < n and QUOTE_LINE.match(lines[j]): j += 1
            runs.append((i, j, _marker(j - i, "quote"))); i = j; continue
        if HEADER_LINE.match(ln):
            j, has = i, False
            while j < n and HEADER_LINE.match(lines[j]):
                has = has or bool(EMAIL.search(lines[j])); j += 1
            if j - i >= 2 and has: runs.append((i, j, _marker(j - i, "mail")))
            else: plain.extend(range(i, j))
            i = j; continue
        plain.append(i); i += 1
    para = []

    def flush():
        if para and sum(len(lines[k]) for k in para) + len(para) - 1 > QUOTED_PARAGRAPH:
            runs.append((para[0], para[-1] + 1, _marker(len(para), "text")))
        para.clear()

    for k in plain:
        if not lines[k].strip() or (para and k != para[-1] + 1): flush()
        if lines[k].strip(): para.append(k)
    flush()
    return sorted(runs)


def quotes(text: str) -> list:
    """Each quoted part of the text as (start, end, marker), character offsets into the text as given: from the start of the run's first
    line to the end of its last, its newline left out. In order, never overlapping."""
    text = text or ""
    lines = text.split("\n")
    at, pos = [], 0
    for ln in lines: at.append(pos); pos += len(ln) + 1
    return [(at[a], at[b - 1] + len(lines[b - 1]), m) for a, b, m in _runs(text)]


def _quote(text: str):
    """The text with each quoted part as its marker, and the markers, built on _runs."""
    lines, runs = text.split("\n"), _runs(text)
    out, i, r = [], 0, 0
    while i < len(lines):
        if r < len(runs) and runs[r][0] == i:
            out.append(runs[r][2]); i = runs[r][1]; r += 1
        else:
            out.append(lines[i]); i += 1
    return "\n".join(out), [m for _, _, m in runs]


def load(runtime: str = "claude-code", version: int = 1) -> Patterns:
    """The runtime's pattern file, next to this module."""
    if not (isinstance(runtime, str) and TAG_NAME.match(runtime)) or not isinstance(version, int): raise ValueError("a runtime's name and a version")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "patterns", f"{runtime}.v{version}.json")
    with open(path, encoding="utf-8") as f: return Patterns(json.load(f))
