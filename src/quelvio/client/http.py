"""http.py — one call to one of memory's six routes.

Standard library only, like the rest of the client. Each call is one POST to {address}{route} with a JSON body of at most 1 MiB, the key as a
Bearer token in the Authorization header and nowhere else, and X-Quelvio-Agent when the client has QUELVIO_AGENT. The client speaks HTTP
itself, through http.client, so no proxy is ever taken from the environment and no redirect is ever followed (a redirect could carry the key
elsewhere: it is an answer like any other, and the call failed). TLS is checked against the system's certificates and the host's name.

The whole call keeps one deadline: every read and every write waits only for what is left of it, the headers' and the TLS handshake's
included, so a server that drips its answer gets no more time than one that sends nothing. It all runs in the caller's thread.

`send` never raises on what the network or the server does: it gives a Reply. Its status is None when nothing came back (`cause` "timeout"
or "not_reached", as a report names it); its `word` is a refusal's error word, or "unreadable" for an answer the client can't read. What the
client then does is policy.py's errors table, and calls.py's. Nothing here keeps, logs or repeats the key."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
import threading
import time
import urllib.parse
from dataclasses import dataclass

from quelvio.client import state
from quelvio.client.keys import AGENT                                         # core's shape for an agent's name

ADDRESS = "https://api.quelvio.com"
ROUTES = ("/v1/session/start", "/v1/serve", "/v1/turn", "/v1/action", "/v1/session/end", "/v1/trace", "/v1/handoff")
EVERY_ROUTE = ROUTES[:5]                           # their 200 carries instructions_wanted
LIMIT = 1_048_576                                  # a request body's limit, in bytes
ANSWER_BYTES = 2 * 1024 * 1024                     # the most of an answer read; a longer one is unreadable
ERROR_BYTES = 64 * 1024
LOOPBACK = ("127.0.0.1", "localhost", "::1")
KEY = re.compile(r"qv_(personal|schedule|service)_([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})_([0-9a-f]{64})")   # a Quelvio key's shape
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Reply:
    status: int | None                             # None: nothing came back
    body: dict | None = None                       # the answer, when it could be read
    word: str | None = None                        # a refusal's error word; "unreadable" for an answer that can't be read
    cause: str | None = None                       # "timeout", "not_reached", or "status <code>": a report's words

    @property
    def off(self):
        """The off answer: 200 with {id} alone (off)."""
        return self.status == 200 and self.word is None and set(self.body or {}) == {"id"}

    @property
    def id(self):
        return (self.body or {}).get("id")


def address(url):
    """The server's address: https://…, or http:// to this machine alone, with no user, query, fragment, whitespace or control character
    (urlsplit drops a tab or a line break silently, so the string is checked first)."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("the server's address holds no whitespace or control character")
    try: u = urllib.parse.urlsplit(url); host = u.hostname; u.port
    except (ValueError, TypeError, AttributeError): u = host = None
    if not u or u.scheme not in ("http", "https") or not host or u.username or u.password or u.query or u.fragment or \
            (u.scheme == "http" and host not in LOOPBACK):
        raise ValueError("the server's address must be https://…, or http:// to this machine, with no user, query or fragment")
    return url.rstrip("/")


PINNED = (".config", "quelvio", "server")                                         # under the system's home: the server the key was set up for


def _loopback(url):
    try: host = urllib.parse.urlsplit(url).hostname
    except (ValueError, TypeError, AttributeError): return False
    if host in LOOPBACK: return True
    try: return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError): return False


def _pin_folder():
    """(the pin's folder, None) as walked from the system's home, never HOME; (None, the walk's refusal) where a link, or a file,
    stands in the way with nothing behind it; ValueError, naming the link, where something is behind it."""
    try: return state.walk_home(PINNED[:-1]), None
    except state.Unusable as e:
        if os.path.lexists(os.path.join(state.system_home(), *PINNED)):
            raise ValueError("%s, so the server address found through it isn't used" % e) from None
        return None, e


def pin_note():
    """For `quelvio check`: where the pin's folder is a link, or a file, with nothing
    behind it, the sentence naming it and saying Quelvio's own address is used, the one server() allows with no pin, so someone who had
    pinned another server sees it. None otherwise: a refused pin is server()'s to say."""
    try: _, passed = _pin_folder()
    except ValueError: return None
    return None if passed is None else "%s, and no server address was found through it, so Quelvio's own address is used" % passed


def _pinned(env):
    """The server address written at install, one line in ~/.config/quelvio/server: None where there is none. A file that is a link, not
    the user's own, or writable by others is refused (ValueError): an address the key would follow must be the user's alone. So is one
    reached through a linked .config or .config/quelvio (a stow-linked ~/.config lets whoever can change that repository
    choose where the key goes), a dangling link included; the home itself may be a link. With nothing behind such a link there is no pin,
    as with no .config at all: stow folds a whole ~/.config into the repository where there was none.
    Each refusal names the link or the file."""
    folder, _ = _pin_folder()
    if folder is None: return None
    path = os.path.join(folder, PINNED[-1])
    try: fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError: return None
    except OSError: raise ValueError(path + " can't be read safely") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()) or st.st_mode & 0o022:
            raise ValueError("the pinned server address must be a file of the user's own that no one else can write")
        text = os.read(fd, 4096).decode("utf-8", "replace").strip()
    finally: os.close(fd)
    return address(text)


def server(env):
    """Where the key may go: the address pinned at install, else Quelvio's own. QUELVIO_URL is honoured only where it equals that
    address, or where both it and the pin name this machine (a developer's machine pins its local server at setup). A project's settings
    can set the variable for the session's hooks with no code, so it never sends the key to another server, nor to a listener on this
    machine that the pin didn't name, which could log it. ValueError, with nothing sent,
    otherwise."""
    pinned = _pinned(env)
    target = pinned or ADDRESS
    named = env.get("QUELVIO_URL")
    if not named: return target
    named = address(named)
    if named == target or (pinned is not None and _loopback(pinned) and _loopback(named)): return named
    raise ValueError("QUELVIO_URL names a server this key wasn't set up for: nothing is sent")


def _json(raw):
    try: d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError): return None
    return d if isinstance(d, dict) else None


def _readable(route, d):
    """A 200 the client can use: an id, and on the five session routes instructions_wanted as a list of strings, unless it is the off answer."""
    if not d or not isinstance(d.get("id"), str): return False
    if set(d) == {"id"} or route not in EVERY_ROUTE: return True
    wanted = d.get("instructions_wanted")
    return isinstance(wanted, list) and all(isinstance(w, str) for w in wanted)


def _timed_out(e):
    return isinstance(e, (socket.timeout, TimeoutError))


def _left(deadline):
    """What is left of the call's deadline, in seconds; past it, a timeout."""
    t = deadline - time.monotonic()
    if t <= 0: raise socket.timeout("the call's deadline has passed")
    return t


class _Socket(socket.socket):
    """A socket whose every read and write waits only for what is left of the call's deadline."""
    deadline = 0.0

    def recv_into(self, buffer, nbytes=0, flags=0):
        self.settimeout(_left(self.deadline)); return super().recv_into(buffer, nbytes, flags)

    def recv(self, bufsize, flags=0):
        self.settimeout(_left(self.deadline)); return super().recv(bufsize, flags)

    def sendall(self, data, flags=0):                                                  # a timeout bounds the whole of sendall
        self.settimeout(_left(self.deadline)); return super().sendall(data, flags)


class _TLSSocket(ssl.SSLSocket):
    """The same over TLS: SSLSocket's recv and recv_into read through read(), and its sendall writes through send()."""
    deadline = 0.0

    def read(self, len=1024, buffer=None):
        self.settimeout(_left(self.deadline)); return super().read(len, buffer)

    def send(self, data, flags=0):
        self.settimeout(_left(self.deadline)); return super().send(data, flags)


def _tls_context():
    """The system's certificates, the host's name checked."""
    return ssl.create_default_context()


class _Connection(http.client.HTTPConnection):
    def __init__(self, scheme, host, port, deadline):
        super().__init__(host, port, timeout=_left(deadline))
        self.scheme, self.deadline = scheme, deadline

    def connect(self):
        failed = None
        for family, kind, proto, _, where in _resolve(self.host, self.port, self.deadline):
            sock = _Socket(family, kind, proto)
            sock.deadline = self.deadline
            try:
                sock.settimeout(_left(self.deadline)); sock.connect(where)
                break
            except OSError as e:
                sock.close(); failed = e
                if isinstance(e, socket.timeout): raise
        else: raise failed or OSError("no address to reach")
        if self.scheme == "https":
            ctx = _tls_context(); ctx.sslsocket_class = _TLSSocket
            sock = ctx.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            sock.deadline = self.deadline
            sock.settimeout(_left(self.deadline)); sock.do_handshake()
        self.sock = sock


def _resolve(host, port, deadline):
    """host's addresses, the lookup within what is left of the deadline: getaddrinfo has no timeout of its own, so a real name is
    looked up in a thread of its own and given up at the deadline. An IP literal or a loopback name is answered at once, in the caller's
    thread."""
    try: ipaddress.ip_address(host); at_once = True
    except ValueError: at_once = host in LOOPBACK
    if at_once: return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    box = {}

    def look():
        try: box["found"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except Exception as e: box["failed"] = e                                       # noqa: BLE001 — handed to the caller as it came
    t = threading.Thread(target=look, daemon=True)
    t.start(); t.join(_left(deadline))
    if t.is_alive(): raise socket.timeout("the name lookup passed the call's deadline")
    if "failed" in box: raise box["failed"]
    return box["found"]


def _exchange(url, data, headers, timeout):
    """(status, the answer's first bytes): one POST and its answer, within `timeout` seconds in all."""
    deadline = time.monotonic() + timeout
    u = urllib.parse.urlsplit(url)
    conn = _Connection(u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80), deadline)
    try:
        conn.request("POST", u.path, body=data, headers=headers)
        r = conn.getresponse()
        return r.status, r.read((ANSWER_BYTES if r.status == 200 else ERROR_BYTES) + 1)
    finally: conn.close()


def send(base, route, data, key, agent=None, timeout=3.0):
    """POST `data` (bytes: the JSON body, at most LIMIT) to base + route, the whole call within `timeout` seconds. Refuses, before anything
    is sent, a route not memory's, a body over the limit, a key not of Quelvio's shape and an agent name not of core's. Otherwise a Reply,
    whatever happened."""
    if route not in ROUTES: raise ValueError("not one of memory's routes")
    if not isinstance(data, (bytes, bytearray)) or len(data) > LIMIT: raise ValueError("a body is bytes, at most 1 MiB")
    if not isinstance(key, str) or not KEY.fullmatch(key): raise ValueError("this is not a Quelvio key")
    if agent is not None and (not isinstance(agent, str) or not AGENT.fullmatch(agent)):
        raise ValueError("an agent's name is letters, digits, dots, dashes or underscores, at most 64, starting with a letter or digit")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key, "User-Agent": "quelvio"}
    if agent is not None: headers["X-Quelvio-Agent"] = agent
    url = address(base) + route
    try: status, raw = _exchange(url, bytes(data), headers, timeout)
    except Exception as e:                                                             # noqa: BLE001 — never the network's words
        return Reply(None, None, None, "timeout" if _timed_out(e) else "not_reached")
    finally: del headers, key
    d = _json(raw) if len(raw) <= (ANSWER_BYTES if status == 200 else ERROR_BYTES) else None
    if status == 200: return Reply(200, d) if _readable(route, d) else Reply(200, d, UNREADABLE, "status 200")
    if status < 300: return Reply(status, d, UNREADABLE, "status %d" % status)
    word = d.get("error") if d and isinstance(d.get("error"), str) else None
    return Reply(status, d, word, "status %d" % status)
