"""A memory's subjects (the table of classes, Matching names, Subjects, The subject filter).

    subjects(text) -> ["git push", "email", ...]       found when the memory is written: memory.subjects
    classes(subjects) -> {"push", "send", ...}          a rule covers every action of each class its subjects name
    matches(subjects, action_class) -> bool             at least one subject is a trigger or a verb of the action's class

Only the trigger words of the risky classes, from the table of classes, and the class verbs (push, delete, deploy, publish, write, send),
matched by stem; no path, branch or remote pattern, and no other word list. Words are split into tokens as names are: at white space and
punctuation, at "_", "-", ".", "/" and at a change of case; a token starting with "-" is a flag, kept whole and matched exactly as
written. A trigger matches whole tokens, never a part of one, so "postgres" is not "post"; the longest trigger at a place wins, and each
subject is listed once, in order. The patterns are one file, patterns/subjects.v1.json, which the client and the server share.
Standard library only."""
from __future__ import annotations

import json
import os
import re

_RAW = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "patterns", "subjects.v1.json"), encoding="utf-8"))
VERSION = _RAW["version"]
CLASSES = tuple(_RAW["classes"])
READ_VERBS = tuple(_RAW["read_verbs"])
RAW_TOKEN = re.compile(r"[^\s,;:!?()\[\]{}\"'`<>|&=*#@]+")
CASE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
NAME_SPLIT = re.compile(r"[_\-./~]+")


def _tokens(text: str) -> list:
    out = []
    for m in RAW_TOKEN.finditer(text or ""):
        raw = m.group()
        if raw.startswith("-"):
            if raw.strip("-"): out.append(raw)                                              # a flag: whole, as written
            continue
        for part in NAME_SPLIT.split(CASE.sub(" ", raw).replace(" ", "_")):
            if part and any(c.isalpha() for c in part): out.append(part.lower())
    return out


def canon(trigger: str) -> str:
    """A trigger as a subject: its words in lower case, its flags as written."""
    return " ".join(t if t.startswith("-") else t.lower() for t in trigger.split())


def _forms(verb: str) -> set:
    e = _RAW["stem_endings"]
    if verb.endswith("e"): return {verb} | {verb + x for x in e["final_e"]} | {verb[:-1] + x for x in e["final_e_dropped"]}
    return {verb} | {verb + x for x in e["plain"]}


_TRIGGERS = {}                                                                              # first token -> [(tokens, canonical)], longest first
for _c, _v in _RAW["classes"].items():
    for _tr in _v["triggers"]:
        _toks = tuple(canon(_tr).split())
        _TRIGGERS.setdefault(_toks[0], set()).add((_toks, " ".join(_toks)))
_TRIGGERS = {k: sorted(v, key=lambda x: -len(x[0])) for k, v in _TRIGGERS.items()}
_VERB_OF = {f: v for _c, _v in _RAW["classes"].items() for v in _v["verbs"] for f in _forms(v)}
_CLASS_TRIGGERS = {c: [tuple(canon(t).split()) for t in v["triggers"]] for c, v in _RAW["classes"].items()}
_CLASS_VERBS = {c: set(v["verbs"]) for c, v in _RAW["classes"].items()}


def subjects(text: str) -> list:
    """The trigger words and class verbs in the memory's words, each once, in the order they first stand."""
    toks, out, seen, i = _tokens(text), [], set(), 0
    while i < len(toks):
        hit = next(((n, c) for n, c in _TRIGGERS.get(toks[i], ()) if tuple(toks[i:i + len(n)]) == n), None)
        if hit: subject, i = hit[1], i + len(hit[0])
        else:
            subject, i = _VERB_OF.get(toks[i]), i + 1
            if subject is None: continue
        if subject not in seen: seen.add(subject); out.append(subject)
    return out


def _holds(tokens: tuple, trigger: tuple) -> bool:
    n = len(trigger)
    return any(tokens[i:i + n] == trigger for i in range(len(tokens) - n + 1))


def classes(subs) -> set:
    """Every class whose trigger a subject holds or whose verb it is: a command is in every class whose trigger it holds."""
    out = set()
    for s in subs or ():
        toks = tuple(s.split())
        for c in CLASSES:
            if s in _CLASS_VERBS[c] or any(_holds(toks, tr) for tr in _CLASS_TRIGGERS[c]): out.add(c)
    return out


def matches(subs, action_class: str) -> bool:
    """A rule's subjects match an action when at least one is a trigger word or a verb of the action's class."""
    if action_class not in CLASSES: raise ValueError(f"{action_class!r} is not a risky class: {', '.join(CLASSES)}")
    return action_class in classes(subs)
