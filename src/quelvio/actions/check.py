"""check.py — the risky-action check on the server.

Every rule in audience and up to 20 flagged turns of the session's group go to J17, each in its own request, never cached, in this order:
rules with a subject of the action's class; flagged turns that no end has ended, newest first; the other rules; flagged turns at end 4,
newest first. J17 below 0.30 drops the item; from 0.30 up J18 decides, by zones that move with its bar. The strictest outcome wins: a
breach, then borderline, then needs_unseen_step, then allowed.

A request with no answer 1.5 s after the hook call has failed (as has a 402 or another error from Jev). A rule whose request failed is
decided by the match: a subject of the action's Push, Delete or Deploy or publish class makes it ask when attended and deny in the
background, as a breach does; a rule with no subjects is never a match. A flagged turn whose request failed drops out. When any request
failed and the action ran, the outcome is "check unavailable".

The hook asks when the session is attended, and denies in the background; needs_unseen_step asks when attended and is allowed with a notice
in the background. A permission mode where an "ask" doesn't reach the person counts as background (a framework
package's framework_background is one). J17 and J18 are asked through `ask(item, action) -> (j17, j18)`: the adapter and S5's
state wire in there. Each request is timed to what is left of the deadline, at most POOL run at once, and one that would
start after the deadline fails without being sent."""
import concurrent.futures as futures
import time
from dataclasses import dataclass, field

from quelvio.text import subjects as subject_rules

J17_GATE = 0.30
J18_BAR = 0.70
DEADLINE = 1.5
MAX_TURNS = 20
POOL = 8                                        # requests in flight at once; the adapter's pacer holds the rate
BLOCKING = ("push", "delete", "deploy")
RANK = {"breach": 0, "match": 0, "borderline": 1, "needs_unseen_step": 2, "allowed": 3}
CLASS_WORDS = {"push": "push", "delete": "delete", "deploy": "deploy or publish", "write_outside": "write outside the work folder",
               "send": "send"}
NO_ASK_MODES = frozenset({"framework_background"})     # the Claude Code modes where an ask doesn't reach the person

ASK = 'Quelvio: this {cls} may go against a rule. {named}. Allow it?'
DENY = ('Quelvio blocked this {cls}. It may go against {named}. Do not try it again. Go on with the rest, and say in your reply that you '
        'skipped it and why.')
UNSEEN = 'Quelvio: this rule asks for a step first that Quelvio can\'t see. [{label}] "{words}". Allow it if the step is done.'
MATCH = 'Quelvio could not check this {cls}. A rule names {subject}: "{words}". Allow it?'


@dataclass(frozen=True)
class Item:
    """A rule in audience, or a flagged turn checked as a rule (with no exceptions and no subjects)."""
    kind: str                                   # "rule" or "turn"
    id: str
    words: str
    subjects: tuple = ()
    number: int = None
    kind_label: str = None                      # a rule's kind, as its card shows it
    authority: str = None                       # a rule's authority label
    date: str = None
    label: str = None
    ended: bool = False                         # a flagged turn at end 4
    at: float = 0.0                             # a flagged turn's time, for newest first
    person: str = None


class Failed(Exception):
    """A request that gave no answer: its cause is "timeout", "jev_402" or "jev_error"."""

    def __init__(self, cause):
        super().__init__(cause); self.cause = cause


def rule_outcome(j17, j18, bar=J18_BAR):
    """One item's outcome from its J17 and J18 answers, at J18's bar (the zones move with it)."""
    if j17 < J17_GATE: return "dropped"
    breaks = j18.get("breaks", 0.0)
    if breaks >= round(bar + 0.10, 9): return "breach"
    if breaks >= round(bar - 0.10, 9): return "borderline"
    if max(j18, key=j18.get) == "needs_unseen_step": return "needs_unseen_step"
    if j18.get("allowed", 0.0) >= bar: return "allowed"
    return "borderline"


def requests_per_action(rules, turns):
    """The J17 requests one risky action can make, J18 in the same request: every rule in audience and up to 20 flagged turns. For the spend
    line before a live J17 or J18 call."""
    return rules + min(turns, MAX_TURNS)


def _timed(ask, item, action, deadline_at):
    left = deadline_at - time.monotonic()
    if left <= 0: raise Failed("timeout")                                     # never sent: the hook has had its answer
    return ask(item, action, timeout=left)


def _of_class(item, action):
    return any(subject_rules.matches(list(item.subjects), c) for c in action.classes if c in subject_rules.CLASSES)


def order(items, action):
    """The requests' order, with at most 20 flagged turns: those no end has ended first, then those at end 4, each newest first."""
    rules = [i for i in items if i.kind == "rule"]
    turns = sorted((i for i in items if i.kind == "turn"), key=lambda i: -i.at)
    open_turns, ended = [t for t in turns if not t.ended], [t for t in turns if t.ended]
    kept = (open_turns + ended)[:MAX_TURNS]
    open_turns, ended = [t for t in open_turns if t in kept], [t for t in ended if t in kept]
    return [r for r in rules if _of_class(r, action)] + open_turns + [r for r in rules if not _of_class(r, action)] + ended


def _matching_subject(item, action):
    """The rule's first subject of the action's Push, Delete or Deploy or publish class, or None."""
    for s in item.subjects:
        if any(c in BLOCKING and subject_rules.matches([s], c) for c in action.classes): return s
    return None


@dataclass
class Decision:
    outcome: str
    hook_answer: str
    item: Item = None
    reason: str = None
    cause: str = None
    notice: str = None
    gives_label: bool = True
    permission_mode: str = None
    answered: tuple = ()                        # the items decided by their answers, in the requests' order
    failed: dict = field(default_factory=dict)  # item id -> the cause of a request that failed

    def record(self):
        """action_check's fields this check fills: the rule's outcome and the hook's answer apart, with the permission mode."""
        return {"outcome": self.outcome, "hook_answer": self.hook_answer, "permission_mode": self.permission_mode,
                "item_kind": self.item.kind if self.item else None, "item_id": self.item.id if self.item else None,
                "cause": self.cause, "gives_label": self.gives_label}


def _named(item):
    if item.kind == "turn": return '%s said in this task: "%s"' % (item.person, item.words)
    return '[%s · %s · %s] "%s"' % (item.kind_label, item.authority, item.date, item.words)


def check(action, items, attended, permission_mode, ask, bar=J18_BAR, no_ask_modes=NO_ASK_MODES, deadline=DEADLINE, started=None):
    started = time.monotonic() if started is None else started
    background = not attended or permission_mode in set(no_ask_modes) | NO_ASK_MODES
    ordered = order(items, action)
    results, failures = {}, {}
    if ordered:
        deadline_at = started + deadline
        pool = futures.ThreadPoolExecutor(max_workers=min(POOL, len(ordered)))
        try:
            pending = {pool.submit(_timed, ask, item, action, deadline_at): item for item in ordered}
            done, late = futures.wait(pending, timeout=max(0.0, deadline_at - time.monotonic()))
            for f in done:
                item = pending[f]
                try: results[item.id] = rule_outcome(*f.result(), bar=bar)
                except Failed as e: failures[item.id] = e.cause
                except Exception: failures[item.id] = "jev_error"
            for f in late: failures[pending[f].id] = "timeout"
        finally: pool.shutdown(wait=False, cancel_futures=True)
    d = _decide(action, ordered, results, failures, background, permission_mode)
    d.answered, d.failed = tuple(i.id for i in ordered if i.id in results), dict(failures)
    return d


def _decide(action, ordered, results, failures, background, permission_mode):
    decided = []
    for item in ordered:
        if item.id in results:
            if results[item.id] != "dropped": decided.append((results[item.id], item))
        elif item.kind == "rule" and _matching_subject(item, action):
            decided.append(("match", item))
    cls = CLASS_WORDS.get(action.cls, action.cls)
    cause = next((failures[i.id] for i in ordered if i.id in failures), None)
    if not decided or all(o == "allowed" for o, _ in decided):
        if failures: return Decision("check unavailable", "allow", cause=cause, notice="check unavailable", permission_mode=permission_mode)
        return Decision("allowed", "allow", permission_mode=permission_mode)
    outcome, item = min(decided, key=lambda d: RANK[d[0]])
    if outcome == "match":
        subject = _matching_subject(item, action)
        reason = DENY.format(cls=cls, named=_named(item)) if background else MATCH.format(cls=cls, subject=subject, words=item.words)
        return Decision("match", "deny" if background else "ask", item, reason, cause=failures.get(item.id), gives_label=False,
                        permission_mode=permission_mode)
    if outcome == "needs_unseen_step":
        reason = UNSEEN.format(label=item.label or item.number, words=item.words)
        if background:
            return Decision(outcome, "allow", item, reason, cause=cause if failures else None, notice="needs_unseen_step",
                            permission_mode=permission_mode)
        return Decision(outcome, "ask", item, reason, permission_mode=permission_mode)
    if outcome == "allowed":
        return Decision("allowed", "allow", permission_mode=permission_mode)
    reason = (DENY if background else ASK).format(cls=cls, named=_named(item))
    return Decision(outcome, "deny" if background else "ask", item, reason, permission_mode=permission_mode)
