"""shim.py — the stdio shim: `quelvio mcp` for a runtime that runs its MCP servers over stdio. Each line on stdin is one JSON-RPC
message; it goes to the one server over HTTP, and the answer goes back on stdout, one line each. A notification gets no line.

The key comes from the `key` the shim is given (the keychain item quelvio/{agent}, or QUELVIO_KEY where the machine has no
keychain), read for each message and never kept. It goes in the Authorization header alone: never in the address, the body, another header,
stdout, stderr or a process argument. A hook agent's shim (`quelvio mcp --hooks`) adds X-Quelvio-Hooks: 1. No key to be had reads to the agent
as a refused key does, "Quelvio refused this key or agent name": both exit 2, with one sentence for both.
No proxy is taken from the environment and no redirect is followed: a redirect is no answer. When the server can't be used, a tool gets its
product's sentence as an error result, so the agent goes on; any other request gets a JSON-RPC error."""
import json
import urllib.error
import urllib.parse
import urllib.request

from quelvio.mcp.errors import ToolFailure, good_id, sentence_for, sentences

LOOPBACK = ("127.0.0.1", "localhost", "::1")
NOT_AVAILABLE = "Quelvio is not available right now."
ANSWER_BYTES = 4 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open


def _address(url):
    try: u = urllib.parse.urlsplit(url); host = u.hostname; u.port
    except ValueError: u = host = None
    if not u or u.scheme not in ("http", "https") or not host or u.username or u.password or u.query or u.fragment or \
            (u.scheme == "http" and host not in LOOPBACK):
        raise ValueError("the server's address must be https://…, or http:// to this machine, with no user, query or fragment")
    return url


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class Shim:
    def __init__(self, url, key, agent, timeout=30.0, hooks=False):
        """`hooks` is `quelvio mcp --hooks`, for a hook agent: each message then says so in X-Quelvio-Hooks (Agent connections)."""
        self.url, self._key, self.agent, self.timeout, self.hooks = _address(url), key, agent, timeout, bool(hooks)

    def _post(self, message):
        """The server's answer to one message: its JSON, or None for 202. Raises ToolFailure when there is no answer to give."""
        try: key = self._key()
        except Exception: raise ToolFailure(401) from None                    # no key to be had: nothing is sent
        req = urllib.request.Request(self.url, data=json.dumps(message).encode("utf-8"), method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + key,
            "X-Quelvio-Agent": self.agent, "User-Agent": "quelvio", **({"X-Quelvio-Hooks": "1"} if self.hooks else {})})
        del key
        try:
            with _OPEN(req, timeout=self.timeout) as r:
                if r.status == 202: return None
                return self._read(r.read(ANSWER_BYTES), r.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as e: raise ToolFailure(e.code) from None
        except ToolFailure: raise
        except Exception: raise ToolFailure(None) from None

    @staticmethod
    def _read(body, kind):
        text = body.decode("utf-8")
        if kind.startswith("text/event-stream"):
            data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
            text = data[-1] if data else ""
        answer = json.loads(text)
        if not isinstance(answer, dict): raise ToolFailure(None)
        return answer

    def answer(self, message):
        """One message in, its answer out (None for a notification)."""
        notify = "id" not in message
        try: got = self._post(message)
        except (ToolFailure, ValueError) as f:
            if notify: return None
            failure = f if isinstance(f, ToolFailure) else ToolFailure(None)
            if message.get("method") == "tools/call":
                name = (message.get("params") or {}).get("name")
                product = "knowledge" if isinstance(name, str) and name.startswith("knowledge_") else "memory"
                return {"jsonrpc": "2.0", "id": message["id"], "result": {
                    "content": [{"type": "text", "text": sentence_for(product, failure)}], "isError": True}}
            said = sentence_for("memory", failure)
            return _error(message["id"], -32603, NOT_AVAILABLE if said == sentences("memory")["unavailable"] else said)
        return None if notify else got

    def run(self, stdin, stdout, stderr):
        for line in stdin:
            line = line.strip()
            if not line: continue
            try: message = json.loads(line)
            except (ValueError, RecursionError): out = _error(None, -32700, "not JSON")                    # nested too deep, too
            else:
                if not isinstance(message, dict): out = _error(None, -32600, "one message per line")
                elif not good_id(message): out = _error(None, -32600, "an id is a string, a whole number or null")
                else: out = self.answer(message)
            if out is not None:
                stdout.write(json.dumps(out) + "\n"); stdout.flush()
        return 0
