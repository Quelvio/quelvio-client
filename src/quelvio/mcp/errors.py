"""errors.py — what a tool says when it can't answer: one sentence per case, per product, read from quelvio/contract/mcp.json. A handler raises ToolFailure
with the status and error word its route answered; the server turns it into the product's sentence. The model reads the sentence; code reads
the error word, never the sentence."""
from quelvio.mcp.registry import contract

CUT_AT = 75000                                             # characters: under the 25,000 tokens a Claude connector takes in one result
CUT_LINE = "\n[cut: the rest is too long for one answer]"


class ToolFailure(Exception):
    """A route's answer that isn't a result: its HTTP status (None when nothing answered), its error word and, for 413 and 422, the field."""

    def __init__(self, status=None, word=None, field=None):
        super().__init__(status, word, field)
        self.status, self.word, self.field = status, word, field


class Refused(Exception):
    """The request's key or agent name was refused (401 or 403): nothing of the request is read."""

    def __init__(self, status=401):
        super().__init__(status)
        self.status = status


def sentences(product):
    return dict(contract()["sentences"][product])


def sentence_for(product, failure):
    """The sentence a tool of `product` returns for a failure. Anything not named, a bug included, is "not available"."""
    said = sentences(product)
    status = getattr(failure, "status", None) if isinstance(failure, ToolFailure) else None
    word = getattr(failure, "word", None)
    if status in (401, 403): return said["refused"]
    if status == 402 and word == "plan ended": return said["plan_ended"]
    if status == 402 and word == "plan limit reached" and "plan_limit" in said: return said["plan_limit"]
    if status == 426: return said["upgrade"]
    if status == 429: return said["too_many_calls"]
    if status in (413, 422) and word: return word + (": " + failure.field if failure.field else "")
    return said["unavailable"]


def cut(text):
    """A tool's text, cut at a whole line when it is longer than a connector takes, with a line that says so."""
    if len(text) <= CUT_AT: return text
    head = text[:CUT_AT - len(CUT_LINE)]
    return (head[:head.rindex("\n")] if "\n" in head else head) + CUT_LINE


def good_id(message):
    """JSON-RPC's id: a string, a whole number or null. Anything else is refused and never echoed."""
    v = message.get("id")
    return v is None or isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool))
