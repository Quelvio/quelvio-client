"""exits.py — the program `quelvio`'s errors and exit codes, for every command but `hook`: one function from an answer to its stderr line, the waits before each further try, and the exit code.

| Case | On stderr | Tries again | Exit |
|---|---|---|---|
| An answer | Nothing | | 0 |
| 413, 415, 422 | `quelvio:` and the body's error word, with its field | No | 1 |
| 401 | `quelvio: the key was refused. It is unknown or was ended.` | No | 2 |
| 429 | `quelvio: too many calls.` | Once, after 1 s | 4 |
| 403 | `quelvio: the key belongs to another agent than QUELVIO_AGENT names.` | No | 6 |
| No answer: DNS, TLS, a timeout, a redirect, an unreadable body | `quelvio: no answer from {address}.` | No | 7 |
| 500, and any other status | `quelvio: server.` | Once, after 2 s | 7 |
| 503 | `quelvio: unavailable.` | After 2 s, 4 s and 8 s | 7 |
| 402 | The server's own words | No | 8 |
| 426 | `quelvio: upgrade. Install the newer client.` | No | 9 |

The off answer (200 with its id alone) is no failure: its command prints its sentence on stdout and exits OFF. Words that come from the server
are printed only as letters, digits, spaces and plain punctuation, at most 200 characters, so an answer can't move a terminal's cursor or
colour it. Nothing here ever holds a key."""
import re
from dataclasses import dataclass

OK, REFUSED, NO_KEY, TOO_MANY, OTHER_AGENT, NO_ANSWER, PLAN, UPGRADE, OFF, USAGE = 0, 1, 2, 4, 6, 7, 8, 9, 10, 64
KEY_REFUSED = "quelvio: the key was refused. It is unknown or was ended."
OTHER = "quelvio: the key belongs to another agent than QUELVIO_AGENT names."
SHOWN = re.compile(r"[^A-Za-z0-9 .,:;'()/_-]")
WORDS = 200


@dataclass(frozen=True)
class Failure:
    line: str                                      # for stderr
    code: int
    waits: tuple = ()                              # seconds before each further try


def shown(text):
    """Server words as they may reach a terminal."""
    return SHOWN.sub("", text if isinstance(text, str) else "")[:WORDS].strip()


def no_key(agent):
    return "quelvio: no key for %s. Make one on Keys, then: quelvio key set" % agent


def failure(reply, address):
    """The row for an http.Reply that isn't a readable 200; None for one that is (the off answer included)."""
    status, word, body = reply.status, reply.word, reply.body or {}
    if status == 200 and word is None: return None
    if status is None or status == 200 or 300 <= status < 400: return Failure("quelvio: no answer from %s." % address, NO_ANSWER)
    if status in (413, 415, 422):
        said = shown(word) or "refused"
        field = shown(body.get("field"))
        return Failure("quelvio: %s%s" % (said, ", " + field if field else ""), REFUSED)
    if status == 401: return Failure(KEY_REFUSED, NO_KEY)
    if status == 403: return Failure(OTHER, OTHER_AGENT)
    if status == 429: return Failure("quelvio: too many calls.", TOO_MANY, (1,))
    if status == 402: return Failure("quelvio: %s" % (shown(word) or "the plan"), PLAN)
    if status == 426: return Failure("quelvio: upgrade. Install the newer client.", UPGRADE)
    if status == 503: return Failure("quelvio: unavailable.", NO_ANSWER, (2, 4, 8))
    return Failure("quelvio: server.", NO_ANSWER, (2,))
