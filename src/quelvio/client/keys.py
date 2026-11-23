"""keys.py — where the client finds the agent's key. The keychain item
quelvio/{agent}, read at each hook call; QUELVIO_KEY only where the machine has no keychain (Windows, Linux with no secret service).

A key reads qv_{kind}_{id}_{secret}: kind personal, schedule or service; an id with no underscore; a secret of 64 lower-case hex characters.
Anything else, the earlier platform's qlv_ and qma_ keys among them, is refused before any call. The key goes to the keychain on standard
input alone, never in a process argument, and is read back into the caller's hands only: never kept, printed or logged here."""
import re
import shutil
import subprocess

KEY = re.compile(r"qv_(?:personal|schedule|service)_[A-Za-z0-9-]+_[0-9a-f]{64}")
AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
NOT_A_KEY = "This is not a Quelvio key. Make one on Keys in the Lab."
TIMEOUT = 5
SYSTEM_BIN = "/usr/bin:/bin"                    # where the keychain's tool is taken from: never the caller's PATH


class NotAKey(ValueError):
    """Not shaped as a Quelvio key: nothing was sent or stored."""

    def __init__(self):
        super().__init__(NOT_A_KEY)


class NoKey(LookupError):
    """No key to be had; the message says where to put one, and never holds a key."""


class KeychainError(RuntimeError):
    """The keychain answered something other than the key or "not found"."""


def check(key):
    if not isinstance(key, str) or not KEY.fullmatch(key): raise NotAKey()
    return key


def item(agent):
    """The keychain item for an agent: quelvio/{agent}. A name that could be read as a path or an option is refused."""
    if not isinstance(agent, str) or not AGENT.fullmatch(agent):
        raise ValueError("an agent's name is letters, digits, dots, dashes or underscores, at most 64, starting with a letter or digit")
    return "quelvio/" + agent


def _run(argv, env, stdin=None):
    """One call of the keychain's own tool, with the key, if any, on standard input. Its environment is the caller's without QUELVIO_KEY."""
    child = {k: v for k, v in env.items() if k != "QUELVIO_KEY"}
    try: p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=TIMEOUT, env=child)
    except (OSError, subprocess.SubprocessError) as e: raise KeychainError("the keychain could not be asked (%s)" % type(e).__name__) from None
    return p.returncode, p.stdout


class MacKeychain:
    """macOS's login keychain, through security(1)."""

    def __init__(self, tool, env):
        self.tool, self.env = tool, env

    def read(self, agent):
        rc, out = _run([self.tool, "find-generic-password", "-s", item(agent), "-w"], self.env)
        if rc == 44: return None                                                      # errSecItemNotFound
        if rc != 0: raise KeychainError("the keychain answered %d" % rc)
        return out.strip()

    def store(self, agent, key):
        check(key)
        name = item(agent)
        rc, _ = _run([self.tool, "-i"], self.env, stdin='add-generic-password -U -s "%s" -a "%s" -w "%s"\n' % (name, agent, key))
        if rc != 0: raise KeychainError("the keychain answered %d" % rc)

    def remove(self, agent):
        """Take the agent's item out; none there is no error. Afterwards no item remains."""
        rc, _ = _run([self.tool, "delete-generic-password", "-s", item(agent)], self.env)
        if rc not in (0, 44): raise KeychainError("the keychain answered %d" % rc)


class SecretService:
    """A Linux desktop's secret service, through secret-tool(1)."""

    def __init__(self, tool, env):
        self.tool, self.env = tool, env

    def read(self, agent):
        rc, out = _run([self.tool, "lookup", "service", item(agent)], self.env)
        if rc == 1 and not out: return None
        if rc != 0: raise KeychainError("the keychain answered %d" % rc)
        return out.strip()

    def store(self, agent, key):
        check(key)
        name = item(agent)
        rc, _ = _run([self.tool, "store", "--label", "Quelvio key for " + agent, "service", name], self.env, stdin=key)
        if rc != 0: raise KeychainError("the keychain answered %d" % rc)

    def remove(self, agent):
        """Take the agent's item out; none there is no error. Afterwards no item remains."""
        rc, _ = _run([self.tool, "clear", "service", item(agent)], self.env)
        if rc != 0: raise KeychainError("the keychain answered %d" % rc)


def keychain_for(platform, env, search=SYSTEM_BIN):
    """The machine's keychain, or None where it has none: macOS with security(1); Linux with secret-tool(1) and a session bus. The tool is
    found in `search`, the system's own folders, never the caller's PATH: a `security` an agent puts first on its PATH never sees the key."""
    if platform == "darwin":
        tool = shutil.which("security", path=search)
        return MacKeychain(tool, env) if tool else None
    if platform.startswith("linux"):
        tool = shutil.which("secret-tool", path=search)
        return SecretService(tool, env) if tool and env.get("DBUS_SESSION_BUS_ADDRESS") else None
    return None


def read_key(agent, env, keychain):
    """The agent's key, read now: from the keychain item where the machine has a keychain (QUELVIO_KEY is then not read), else from
    QUELVIO_KEY. Raises NoKey or NotAKey; never returns anything but a key's shape."""
    if keychain is not None:
        key = keychain.read(agent)
        if not key: raise NoKey("no key for %s in the keychain: run `quelvio setup`" % agent)
        return check(key)
    key = (env.get("QUELVIO_KEY") or "").strip()
    if not key: raise NoKey("no key: this machine has no keychain, so set QUELVIO_KEY to the agent's key")
    return check(key)
