"""cli.py — the program `quelvio`, the agent's commands: the PyPI package `quelvio`'s console script, and `python -m quelvio.client` here.

    quelvio setup claude-code [--agent NAME] [--user] [--dry-run] [--remove]   the key asked for where none is stored, checked, stored;
                                         Claude Code's hooks and MCP entry written (configure.py); then check's lines
    quelvio setup mcp [--agent NAME]     the same key steps, then the MCP entry to add by hand, then check's lines
    quelvio check [--agent NAME]         Program, Server, Agent and Memory, from the id-only session/start with a fresh session id
    quelvio key set [--agent NAME]       the key on standard input (typed unseen at a terminal), checked, into the keychain item
    quelvio key remove [--agent NAME]    the keychain item taken out
    quelvio hook EVENT                   one Claude Code hook: its JSON on stdin, hooks.run's answer on stdout
    quelvio mcp [--hooks]                the stdio shim (quelvio/mcp/shim.py) to Quelvio's MCP server
    quelvio --version | --help

The agent's name is --agent, else QUELVIO_AGENT; a name with 32 hex characters in a row is refused, as a key's secret would be. The key is
the keychain item quelvio/{agent}, and QUELVIO_KEY only where the machine has no keychain (keys.read_key). It is never a process argument,
a setting, a file, a file name or a line printed: `key set` and `setup` take it on standard input only, and a command line that can't be
used is answered with one fixed usage line, its words never repeated. Every answer other than a readable 200 goes through exits.py's table,
with its retries and exit code; the off answer prints "Memory: off…" and exits 10.

`hook` prints only what hooks.run gives and exits with its code; on anything else (an event it doesn't know, input it can't use, an event
other than the one named, any failure) it prints nothing and exits 0, and nothing ever goes to stderr. `mcp` reads the agent from
QUELVIO_AGENT and the key for each message, and the key goes in the Authorization header alone; `--hooks` adds X-Quelvio-Hooks: 1."""
import argparse
import getpass
import json
import os
import shutil
import sys
import time
import uuid

from quelvio.client import VERSION, configure, exits, http, keys, state

MCP_HOSTED = "https://mcp.quelvio.com/http"                                         # mcp.json's server.addresses.hosted
ROOT = os.path.abspath(__file__)
for _ in range(len(__package__.split(".")) + 1): ROOT = os.path.dirname(ROOT)      # the folder that holds this package
KEY_CHARS = 4096
CHECK_SECONDS = 10.0
START = http.ROUTES[0]
NO_AGENT = "quelvio: QUELVIO_AGENT is not set: run quelvio setup claude-code --agent NAME."
NAME_AGENT = "quelvio: name the agent: --agent NAME."
NO_KEYCHAIN = ("No keychain here. Put QUELVIO_KEY in the environment that starts the agent, in a file only you can read. "
               "Then run quelvio check.")
NO_KEYCHAIN_REMOVE = "quelvio: no keychain here. QUELVIO_KEY, where it is set, is the environment's to take out."
REMOVED = "Removed from this machine. The key still works elsewhere."
MEMORY_ON = "Memory: on"
MEMORY_OFF = "Memory: off: memory is not on, or the agreement is not recorded"
USAGE = ("usage: quelvio setup claude-code [--agent NAME] [--user] [--dry-run] [--remove] | quelvio setup mcp [--agent NAME] | "
         "quelvio check [--agent NAME] | quelvio key set [--agent NAME] | quelvio key remove [--agent NAME] | quelvio hook EVENT | "
         "quelvio mcp [--hooks] | quelvio --version")


class _Exit(Exception):
    """A command's end: its exit code, and the line for stderr, which never holds a key."""

    def __init__(self, code, line=None):
        super().__init__(line)
        self.code, self.line = code, line


class _Usage(Exception):
    """A command line that can't be used. Its words are never repeated: one could be a key typed in the wrong place."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Usage()


class _Ctx:
    def __init__(self, **kw): self.__dict__.update(kw)

    def out(self, line): self.stdout.write(line + "\n")


# ---------------------------------------------------------------- hook, mcp
def hook(rest, stdin, stdout, env, cwd):
    """One hook run for the event named. Exits 0 with nothing printed unless hooks.run answers; never raises."""
    try:
        if len(rest) != 1: return 0
        from quelvio.client import hooks
        if rest[0] not in hooks.EVENTS: return 0
        try: data = state.read_input(stdin)
        except state.Unusable: return 0
        if data.get("hook_event_name") != rest[0]: return 0
        out = hooks.run(data, env, cwd)
        if out.stdout:
            stdout.write(out.stdout); stdout.flush()
        return out.code if isinstance(out.code, int) and not isinstance(out.code, bool) else 0
    except BaseException:                                                              # noqa: BLE001 — a hook's failure adds nothing
        return 0


def mcp_address(env):
    """The MCP server's address: {server}/mcp beside the routes where a server was pinned (or QUELVIO_URL names the pinned one), else the
    hosted one. ValueError, with nothing sent, where QUELVIO_URL names a server the key wasn't set up for."""
    server = http.server(env)
    return MCP_HOSTED if server == http.ADDRESS else server + "/mcp"


def mcp(hooks_flag, ctx):
    agent = ctx.env.get("QUELVIO_AGENT") or ""
    if not agent: raise _Exit(exits.NO_KEY, NO_AGENT)
    agent = _named(agent)
    try: url = mcp_address(ctx.env)
    except ValueError as e: raise _Exit(exits.REFUSED, "quelvio: %s." % e)
    from quelvio.mcp.shim import Shim
    chain = ctx.keychain_for(ctx.platform, ctx.env)
    return Shim(url, lambda: keys.read_key(agent, ctx.env, chain), agent, hooks=hooks_flag).run(ctx.stdin, ctx.stdout, ctx.stderr)


# ---------------------------------------------------------------- the agent and its key
def _named(name):
    try: return configure.agent_name(name)
    except ValueError as e: raise _Exit(exits.REFUSED, "quelvio: %s." % e)


def _agent(given, env):
    name = given if given is not None else env.get("QUELVIO_AGENT")
    if not name: raise _Exit(exits.USAGE, NAME_AGENT)
    return _named(name)


def _secret(stdin):
    """The key: typed unseen where standard input is a terminal, else its first line; never an argument."""
    try:
        if hasattr(stdin, "isatty") and stdin.isatty(): return getpass.getpass("Quelvio key for this agent: ").strip()
        return stdin.readline(KEY_CHARS).strip()
    except (EOFError, KeyboardInterrupt):
        raise _Exit(exits.NO_KEY, "quelvio: no key was given.") from None


def _shaped(key):
    """A key of Quelvio's shape, its id the identity service's: the only kind ever sent or stored."""
    return isinstance(key, str) and bool(keys.KEY.fullmatch(key)) and bool(http.KEY.fullmatch(key))


def _asked(ctx):
    key = _secret(ctx.stdin)
    if not _shaped(key): raise _Exit(exits.NO_KEY, "quelvio: " + keys.NOT_A_KEY)
    return key


def _found(ctx, agent, chain):
    """(the key, where it was found), or (None, where it would be): from the keychain where there is one, else QUELVIO_KEY."""
    where = "the keychain" if chain is not None else "QUELVIO_KEY"
    try: key = keys.read_key(agent, ctx.env, chain)
    except keys.NoKey: return None, where
    except keys.NotAKey: raise _Exit(exits.NO_KEY, "quelvio: the key found in %s is not a Quelvio key. Run quelvio key set." % where)
    except keys.KeychainError: raise _Exit(exits.NO_KEY, "quelvio: the keychain could not be read.")
    if not _shaped(key): raise _Exit(exits.NO_KEY, "quelvio: the key found in %s is not a Quelvio key. Run quelvio key set." % where)
    return key, where


def _server(ctx):
    try: return http.server(ctx.env)
    except ValueError as e: raise _Exit(exits.REFUSED, "quelvio: %s." % e)


def _start(ctx, agent, key, base):
    """The id-only session/start, with a fresh client session id never used again: it opens no session. Retried as exits.py's table
    says; the readable 200 (the off answer included), or _Exit with the table's line and code."""
    from quelvio.client import calls, hooks
    data = calls.build(START, {}, session=uuid.uuid4().hex, ack=None, client_version=VERSION, tz=hooks.local_tz(ctx.env), id_only=True).data
    waits = None
    while True:
        reply = ctx.send(base, START, data, key, agent=agent, timeout=CHECK_SECONDS)
        failed = exits.failure(reply, base)
        if failed is None: return reply
        waits = list(failed.waits) if waits is None else waits
        if not waits: raise _Exit(failed.code, failed.line)
        ctx.sleep(waits.pop(0))


def _store(chain, agent, key):
    try: chain.store(agent, key)
    except keys.KeychainError: raise _Exit(exits.NO_KEY, "quelvio: the keychain refused it; nothing was stored.")


# ---------------------------------------------------------------- check
def _program(ctx):
    line = "Program: quelvio %s, %s" % (VERSION, ctx.program)
    first = shutil.which("quelvio", path=ctx.env.get("PATH", ""))
    if first and os.path.realpath(first) != os.path.realpath(ctx.program): line += "; another quelvio comes first on PATH: " + first
    return line


def _state_folder():
    """The state folder's path (~/.local, ~/.local/state, ~/.local/state/quelvio) walked, nothing made. A link there, as GNU stow makes,
    or something that isn't a folder, refuses (REFUSED) in private_folder's words: the hooks would keep nothing there and say nothing. check walks it, and setup claude-code before it writes anything."""
    try: state.walk_home(state.STATE_DIR)
    except state.Unusable as e: raise _Exit(exits.REFUSED, "quelvio: %s, so Quelvio keeps nothing under it." % e) from None


def check(ctx, agent, key=None, where=None, reply=None, base=None):
    """check's lines, up to four until Knowledge's two come: Program, Server, Agent, Memory; after Server, a line where the pin's
    folder is a link with nothing behind it (http.pin_note). A link on the state folder's path ends it with
    REFUSED (_state_folder), as a refused key ends it with its exit."""
    ctx.out(_program(ctx))
    base = base or _server(ctx)
    ctx.out("Server: " + base + ("" if base == http.ADDRESS else ", set by QUELVIO_URL" if ctx.env.get("QUELVIO_URL") else ", pinned at setup"))
    note = http.pin_note()
    if note is not None: ctx.out(note + ".")
    _state_folder()
    if key is None: key, where = _found(ctx, agent, ctx.keychain_for(ctx.platform, ctx.env))
    if key is None:
        ctx.out("Agent: %s, no key here" % agent)
        raise _Exit(exits.NO_KEY, exits.no_key(agent))
    m = http.KEY.fullmatch(key)
    ctx.out("Agent: %s, %s key %s, from %s" % (agent, m.group(1), m.group(2), where))
    reply = reply or _start(ctx, agent, key, base)
    if reply.off:
        ctx.out(MEMORY_OFF); return exits.OFF
    ctx.out(MEMORY_ON); return exits.OK


# ---------------------------------------------------------------- key
def key_set(ctx, agent):
    chain = ctx.keychain_for(ctx.platform, ctx.env)
    if chain is None: raise _Exit(exits.NO_KEY, NO_KEYCHAIN)
    key = _asked(ctx)
    reply = _start(ctx, agent, key, _server(ctx))
    _store(chain, agent, key)
    ctx.out("Stored for %s." % agent)
    if reply.off:
        ctx.out(MEMORY_OFF); return exits.OFF
    return exits.OK


def key_remove(ctx, agent):
    chain = ctx.keychain_for(ctx.platform, ctx.env)
    if chain is None: raise _Exit(exits.NO_KEY, NO_KEYCHAIN_REMOVE)
    try: chain.remove(agent)
    except keys.KeychainError: raise _Exit(exits.NO_KEY, "quelvio: the keychain could not take it out.")
    ctx.out(REMOVED)
    return exits.OK


# ---------------------------------------------------------------- setup
def _run(ctx):
    return configure.launcher(ctx.python, ROOT, __package__)


def _key_for_setup(ctx, agent, base, moved=False):
    """(key, where, the check's reply or None): the key stored for the agent; else, where there is a keychain, one asked for, checked
    with the id-only session/start and stored, before anything is written; else QUELVIO_KEY, or (None, …) where it isn't set. Where
    setup moves the pin to another server (`moved`), no key already here is sent to it: one is asked for, or with no keychain none is
    sent."""
    chain = ctx.keychain_for(ctx.platform, ctx.env)
    if chain is None:
        if moved: return None, "QUELVIO_KEY", None
        key, where = _found(ctx, agent, None)
        return key, where, None
    try: stored = None if moved else chain.read(agent)
    except keys.KeychainError: raise _Exit(exits.NO_KEY, "quelvio: the keychain could not be read.")
    if _shaped(stored): return stored, "the keychain", None
    key = _asked(ctx)
    reply = _start(ctx, agent, key, base)
    _store(chain, agent, key)
    return key, "the keychain", reply


def _pinned():
    """The server the key was set up for now: the pin, else Quelvio's own; None where the pin can't be read."""
    try: return http.server({})
    except ValueError: return None


def _after_setup(ctx, agent, key, where, reply, base):
    if key is None:
        ctx.out(NO_KEYCHAIN); return exits.OK
    return check(ctx, agent, key, where, reply, base)


def setup_claude_code(ctx, args):
    scope = "user" if args.user else "local"
    home = ctx.env.get("HOME") or state.system_home()
    default = os.path.join(home, ".claude", "settings.json") if args.user else os.path.join(ctx.cwd, ".claude", "settings.local.json")
    settings = os.path.abspath(args.settings or default)
    mcp_file = os.path.abspath(args.mcp) if args.mcp else None
    if args.remove:
        for line in configure.remove_claude_code(settings, mcp_file, ctx.env, ctx.cwd, scope, dry=args.dry_run): ctx.out(line)
        return exits.OK
    agent, run = _agent(args.agent, ctx.env), _run(ctx)
    planned = configure.setup_claude_code(agent, run, settings, mcp_file, args.server, ctx.env, ctx.cwd, scope, dry=True)   # every check
    _state_folder()                                                                   # the hooks' folder, before anything is asked or written
    if args.dry_run:
        for line in planned: ctx.out(line)
        return exits.OK
    base = http.address(args.server) if args.server else _server(ctx)
    key, where, reply = _key_for_setup(ctx, agent, base, moved=args.server is not None and base != _pinned())
    for line in configure.setup_claude_code(agent, run, settings, mcp_file, args.server, ctx.env, ctx.cwd, scope): ctx.out(line)
    return _after_setup(ctx, agent, key, where, reply, base)


def setup_mcp(ctx, args):
    agent, run = _agent(args.agent, ctx.env), _run(ctx)
    base = _server(ctx)
    key, where, reply = _key_for_setup(ctx, agent, base)
    ctx.out("Add this MCP server to the agent's settings:")
    ctx.out(json.dumps(configure.mcp_entry(run, agent), indent=2))
    return _after_setup(ctx, agent, key, where, reply, base)


# ---------------------------------------------------------------- the command line
def _parser():
    p = _Parser(prog="quelvio", allow_abbrev=False)
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sub.add_parser("mcp", allow_abbrev=False).add_argument("--hooks", action="store_true")
    tools = sub.add_parser("setup", allow_abbrev=False).add_subparsers(dest="tool", required=True, parser_class=_Parser)
    c = tools.add_parser("claude-code", allow_abbrev=False)
    c.add_argument("--agent", metavar="NAME")
    c.add_argument("--user", action="store_true", help="the user's ~/.claude/settings.json and Claude Code's user scope")
    c.add_argument("--dry-run", action="store_true", help="print what would change; write and send nothing")
    c.add_argument("--remove", action="store_true", help="take out exactly what setup wrote; the key stays until key remove")
    c.add_argument("--settings", help="another settings file in place of .claude/settings.local.json")
    c.add_argument("--mcp", help="write the MCP server to this file instead of registering it with claude")
    c.add_argument("--server", help="pin this server's address; by default Quelvio's own stands")
    tools.add_parser("mcp", allow_abbrev=False).add_argument("--agent", metavar="NAME")
    sub.add_parser("check", allow_abbrev=False).add_argument("--agent", metavar="NAME")
    actions = sub.add_parser("key", allow_abbrev=False).add_subparsers(dest="action", required=True, parser_class=_Parser)
    actions.add_parser("set", allow_abbrev=False).add_argument("--agent", metavar="NAME")
    actions.add_parser("remove", allow_abbrev=False).add_argument("--agent", metavar="NAME")
    return p


def _program_path():
    first = sys.argv[0] if sys.argv else ""
    return os.path.abspath(first) if first and os.path.isfile(first) else sys.executable


def main(argv=None, stdin=None, stdout=None, stderr=None, env=None, cwd=None, platform=None, keychain_for=None, python=None, send=None,
         sleep=None, program=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    env = dict(os.environ) if env is None else env
    cwd = os.getcwd() if cwd is None else cwd
    if argv[:1] == ["hook"]: return hook(argv[1:], stdin, stdout, env, cwd)                 # before argparse: nothing may print
    if argv == ["--version"]:
        stdout.write("quelvio %s\n" % VERSION); return exits.OK
    try: args = _parser().parse_args(argv)
    except _Usage:
        stderr.write(USAGE + "\n"); return exits.USAGE
    except SystemExit as e: return e.code if isinstance(e.code, int) else exits.USAGE           # --help
    ctx = _Ctx(stdin=stdin, stdout=stdout, stderr=stderr, env=env, cwd=cwd, platform=platform or sys.platform,
               keychain_for=keychain_for or keys.keychain_for, python=os.path.abspath(python or sys.executable), send=send or http.send,
               sleep=time.sleep if sleep is None else sleep, program=program or _program_path())
    try:
        if args.command == "mcp": return mcp(args.hooks, ctx)
        if args.command == "check": return check(ctx, _agent(args.agent, env))
        if args.command == "key":
            agent = _agent(args.agent, env)
            return key_set(ctx, agent) if args.action == "set" else key_remove(ctx, agent)
        return setup_claude_code(ctx, args) if args.tool == "claude-code" else setup_mcp(ctx, args)
    except _Exit as e:
        if e.line: stderr.write(e.line + "\n")
        return e.code
    except (configure.SetupError, state.Unusable, ValueError, OSError) as e:
        stderr.write("quelvio: %s\n" % e); return exits.REFUSED


def entry():
    """The console script's and the launcher's start. A hook prints nothing on stderr, ever."""
    if sys.argv[1:2] == ["hook"]:
        try: sys.stderr = open(os.devnull, "w")
        except OSError: pass
        code = hook(sys.argv[2:], sys.stdin, sys.stdout, dict(os.environ), os.getcwd())
        try: sys.stdout.flush()
        except BaseException: pass                                                  # noqa: BLE001
        os._exit(code)
    sys.exit(main())
