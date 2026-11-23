"""configure.py — what `quelvio setup claude-code` and `quelvio setup mcp` write.

    launcher(python, root, module) -> [python, "-I", "-S", "-c", code]
    setup_claude_code(agent, run, settings, mcp=None, server=None, env=None, cwd=None, scope="local", dry=False) -> [line]
    remove_claude_code(settings, mcp=None, env=None, cwd=None, scope="local", dry=False) -> [line]
    mcp_entry(run, agent, hooks=False) -> {"mcpServers": {"quelvio": …}}

Every entry runs `<python> -I -S -c "import sys; sys.path.append(<root>); from <module>.cli import entry; entry()" …`: the python that
ran setup, by its absolute path, and the folder that holds this package added to its path, after the standard library, by the line itself.
-I keeps out PYTHONPATH, every other PYTHON* variable, the user's site and the folder it starts in; -S keeps out the site module, so no
.pth file and no sitecustomize or usercustomize runs, and no other folder is added (nothing of the agent's environment
may load code into the process that reads the key). Appended, not put first, so a module in that folder named as one of the standard
library's (a uuid.py beside a pip --user install) never stands in for it. That works in pipx's or uv's venv as
anywhere: the venv's own site-packages is the root.

- The settings file (by default the project's .claude/settings.local.json, which Claude Code keeps out of git; with --user the user's
  ~/.claude/settings.json) gets one hook per event in hooks.EVENTS, each running `… hook <EVENT>` with its timeout, and
  QUELVIO_AGENT=<agent> in its "env". The earlier client's entries (a hook running quelvio-memory, the MCP server quelvio-memory) are taken
  out and listed by event and name, never by their command line.
- The MCP server "quelvio" (`… mcp --hooks`, QUELVIO_AGENT in its env) is registered by Claude Code's own writer at the same scope:
  `claude mcp add --scope local|user …` as an argument list, no shell, after `claude mcp remove` of it and of quelvio-memory, its exit code
  checked. Where `claude` isn't on the environment's own PATH, or fails, the line is printed. With `mcp`, a file is written there instead.
- With `server`, the address goes to the pin, ~/.config/quelvio/server under the system's home, owner-only.
- Each file changed keeps one copy of what it was, outside the project and the home's dotfiles: under the client's state folder,
  ~/.local/state/quelvio/setup-copies/ under the system's home (0700), one file per path named by the path's SHA-256, written once, 0600,
  and the line names where (a copy beside settings.local.json is a file git doesn't ignore, holding what people keep
  there). The home may be a link; no folder below it may be: .local, .local/state, …/quelvio and setup-copies are each refused as a link
  before anything is made, and the copies folder's real path must then be the home's own (GNU stow links a whole
  ~/.local into a dotfiles repository). The pin's ~/.config/quelvio is held the same way. --remove names the copies it
  leaves.

The key is never an argument here, nor written to any file, the pin, an environment or a log. A file is merged, never replaced: what was
there stays, an earlier setup's entries are replaced, and running it twice changes nothing. A file that is a link, in a folder that is a
link, not a regular file, or not a JSON object, is refused with nothing changed; every check comes before the first write. Each file is
written whole (a temporary file beside it, then renamed), keeping its mode, or 0600 when new."""
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile

from quelvio.client import http, hooks, keys, state

SERVER_NAME = "quelvio"                                                             # mcp.json's server name
EARLIER = "quelvio-memory"                                                          # the earlier client: its hooks' program and its server
TIMEOUTS = {"SessionStart": 10, "UserPromptSubmit": 15, "SubagentStart": 10, "Stop": 10, "SessionEnd": 10,
            "PreToolUse": 10, "PostToolUse": 10}                                    # PreToolUse's above the client's own 3 s
TIMEOUT = 15                                                                        # an event added later
OUR_MODULES = {"quelvio", "quelvio.client", "quelvio.client"}
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
LAUNCH = re.compile(r"\bfrom (?:quelvio|agent_memory)\.client\.cli import entry; entry\(\)$")
HEX_RUN = re.compile(r"[0-9A-Fa-f]{32}")
COPIES = "setup-copies"                                                           # under the client's state folder
SCOPES = ("local", "user")
CLAUDE_SECONDS = 30
NAME_REFUSED = ("an agent's name is letters, digits, dots, dashes or underscores, at most 64, starting with a letter or digit, and never "
                "holds 32 hex characters in a row, as a key does")


class SetupError(Exception):
    """Nothing was changed; the message says why."""


def agent_name(name):
    """The agent's name, as core's shape has it and with no run of 32 hex characters (a key's secret typed as a name would
    be written into a settings file). ValueError, whose message never repeats the name."""
    try: keys.item(name)
    except ValueError: raise ValueError(NAME_REFUSED) from None
    if HEX_RUN.search(name): raise ValueError(NAME_REFUSED)
    return name


def launcher(python, root, module):
    """The command every entry starts with: this python, isolated (-I -S), its path given only `root`, running `module`.cli's entry."""
    if not (isinstance(python, str) and os.path.isabs(python)): raise SetupError("the python must be an absolute path")
    if not (isinstance(root, str) and os.path.isabs(root)): raise SetupError("the package's folder must be an absolute path")
    if not (isinstance(module, str) and MODULE.fullmatch(module)): raise SetupError("the module must be a dotted name")
    if any(c in root for c in "\n\r\0"): raise SetupError("the package's folder holds a line break")
    return [python, "-I", "-S", "-c", "import sys; sys.path.append(%r); from %s.cli import entry; entry()" % (root, module)]


def _words(command):
    try: return shlex.split(command) if isinstance(command, str) else []
    except ValueError: return []


def _ours(command):
    """A hook of ours: `… hook [EVENT]` started by our launcher (any python, any root), by `-m` one of OUR_MODULES, or by a `quelvio`
    program, so an entry left by another python or venv (the package reinstalled, uv in place of pipx) is replaced too."""
    words = _words(command)
    if "hook" not in words: return False
    i = len(words) - 1 - words[::-1].index("hook")
    if len(words) - i > 2: return False                                             # at most the event after it
    head = words[:i]
    if len(head) >= 2 and head[-2] == "-m" and head[-1] in OUR_MODULES: return True
    if len(head) >= 2 and head[-2] == "-c" and LAUNCH.search(head[-1]): return True
    return len(head) == 1 and os.path.basename(head[0]) == "quelvio"


def _earlier(command):
    """A hook of the earlier client: it runs quelvio-memory (a .pyz, a script in .claude/hooks/, or the module quelvio_memory)."""
    return isinstance(command, str) and (EARLIER in command or EARLIER.replace("-", "_") in command)


def _strip(table, gone):
    """Take out of a hooks table every hook whose command `gone` names; a group left empty goes, and an event left with no group. The
    events touched, in the table's order."""
    touched = []
    for event in list(table):
        groups = table[event]
        if not isinstance(groups, list): continue
        kept, hit = [], False
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list): kept.append(group); continue
            rest = [h for h in group["hooks"] if not (isinstance(h, dict) and gone(h.get("command")))]
            hit = hit or len(rest) != len(group["hooks"])
            if rest or not group["hooks"]: kept.append(dict(group, hooks=rest))
        if hit:
            touched.append(event)
            if kept: table[event] = kept
            else: del table[event]
    return touched


def _object(data, name):
    value = data.setdefault(name, {})
    if not isinstance(value, dict): raise SetupError('its "%s" is not an object, so nothing was changed' % name)
    return value


def _with_hooks(data, run, agent, notes):
    _object(data, "env")["QUELVIO_AGENT"] = agent
    table = _object(data, "hooks")
    for event in hooks.EVENTS:
        if not isinstance(table.get(event, []), list): raise SetupError("its hooks for %s are not a list, so nothing was changed" % event)
    old = _strip(table, _earlier)
    if old: notes.append("the earlier client's hooks for " + ", ".join(old))
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and EARLIER in servers:
        del servers[EARLIER]; notes.append("the earlier client's MCP server " + EARLIER)
    _strip(table, _ours)
    for event in hooks.EVENTS:
        table.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": shlex.join(run + ["hook", event]), "timeout": TIMEOUTS.get(event, TIMEOUT)}]})
    return data


def _without_hooks(data, notes):
    table, env = data.get("hooks"), data.get("env")
    if isinstance(table, dict):
        events = _strip(table, _ours)
        if events: notes.append("the hooks for " + ", ".join(events))
        if not table: del data["hooks"]
    if isinstance(env, dict) and "QUELVIO_AGENT" in env:
        del env["QUELVIO_AGENT"]; notes.append("QUELVIO_AGENT")
        if not env: del data["env"]
    return data


def mcp_entry(run, agent, hooks=False):
    """The MCP server's entry, as .mcp.json and Claude Code's mcpServers hold it: the shim, never the key or the address."""
    return {"mcpServers": {SERVER_NAME: {"command": run[0], "args": run[1:] + ["mcp"] + (["--hooks"] if hooks else []),
                                         "env": {"QUELVIO_AGENT": agent}}}}


def _with_server(data, run, agent, notes):
    servers = _object(data, "mcpServers")
    if EARLIER in servers:
        del servers[EARLIER]; notes.append("the earlier client's MCP server " + EARLIER)
    servers[SERVER_NAME] = mcp_entry(run, agent, hooks=True)["mcpServers"][SERVER_NAME]
    return data


def _without_server(data, notes):
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and SERVER_NAME in servers:
        del servers[SERVER_NAME]; notes.append("the MCP server " + SERVER_NAME)
    return data


def claude_line(run, agent, scope="local"):
    """The command that registers the MCP server with Claude Code: the agent's name and the paths, never a key."""
    return ["claude", "mcp", "add", "--scope", scope, SERVER_NAME, "-e", "QUELVIO_AGENT=" + agent, "--"] + run + ["mcp", "--hooks"]


def _claude(env):
    return shutil.which("claude", path=env.get("PATH", ""))                         # the environment's own PATH: none there, none found


def _call(tool, args, env, cwd):
    try: return subprocess.run([tool] + args, cwd=cwd, env=env, capture_output=True, timeout=CLAUDE_SECONDS).returncode
    except (OSError, subprocess.SubprocessError): return None


def _where(scope):
    return "for this project on this machine" if scope == "local" else "for you on this machine"


def _register(run, agent, env, cwd, scope):
    """Register the MCP server through `claude`, after taking out ours and the earlier client's; the line to run otherwise."""
    line = claude_line(run, agent, scope)
    tool = _claude(env)
    if not tool: return ["Register the MCP server %s: run %s" % (_where(scope), shlex.join(line))]
    done = []
    if _call(tool, ["mcp", "remove", "--scope", scope, EARLIER], env, cwd) == 0:
        done.append("Took out the earlier client's MCP server %s (claude, %s scope)." % (EARLIER, scope))
    _call(tool, ["mcp", "remove", "--scope", scope, SERVER_NAME], env, cwd)
    rc = _call(tool, line[1:], env, cwd)
    if rc is None: return done + ["Couldn't run claude to register the MCP server: run " + shlex.join(line)]
    if rc != 0: return done + ["claude answered %d registering the MCP server: run %s" % (rc, shlex.join(line))]
    return done + ["Registered the MCP server %s with claude, %s." % (SERVER_NAME, _where(scope))]


def _unregister(env, cwd, scope):
    line = ["claude", "mcp", "remove", "--scope", scope, SERVER_NAME]
    tool = _claude(env)
    if not tool: return "Take out the MCP server: run " + shlex.join(line)
    rc = _call(tool, line[1:], env, cwd)
    if rc == 0: return "Took out the MCP server %s (claude, %s scope)." % (SERVER_NAME, scope)
    return "claude answered %s taking out the MCP server; if it is there, run %s" % (rc, shlex.join(line))


def _write(path, text, mode):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".quelvio-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp): os.remove(tmp)
        raise


def _folder(path):
    """Refuse a file whose folder is a link (a linked .claude would send the write elsewhere), or isn't a folder."""
    folder = os.path.dirname(path)
    if os.path.islink(folder): raise SetupError(folder + " is a link, so nothing was changed")
    if os.path.lexists(folder) and not os.path.isdir(folder): raise SetupError(folder + " is not a folder, so nothing was changed")


def _read(path):
    """(the file's text, its JSON object, its mode), or (None, {}, None) where there is none; SetupError for one that can't be merged."""
    _folder(path)
    if not os.path.lexists(path): return None, {}, None
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode): raise SetupError(path + " is not a regular file, so nothing was changed")
    with open(path, encoding="utf-8") as f: text = f.read()
    try: data = json.loads(text) if text.strip() else {}
    except ValueError: raise SetupError(path + " is not valid JSON, so nothing was changed") from None
    if not isinstance(data, dict): raise SetupError(path + " is not a JSON object, so nothing was changed")
    return text, data, stat.S_IMODE(st.st_mode)


def copies_folder():
    """Where the copies go: the client's state folder's setup-copies, under the system's home, never HOME."""
    return os.path.join(state.system_home(), *state.STATE_DIR, COPIES)


def copy_of(path):
    """The copy of `path` as it was before setup first changed it: one per path, by the SHA-256 of its absolute path."""
    return os.path.join(copies_folder(), hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest() + ".json")


COPY_PARTS = (*state.STATE_DIR, COPIES)                                            # under the system's home
PIN_PARTS = tuple(http.PINNED[:-1])


def _walk(parts):
    """(the system's home, the folder `parts` names under it), after refusing a link, or something other than a folder, at any folder from
    the home down: the home itself may be a link, nothing below it (GNU stow links a whole ~/.local or ~/.config into
    a dotfiles repository). Nothing is made here."""
    home = state.system_home()
    path = home
    for part in parts:
        path = os.path.join(path, part)
        if os.path.islink(path): raise SetupError(path + " is a link, so nothing was changed: quelvio keeps its files out of where it leads")
        if os.path.lexists(path) and not os.path.isdir(path): raise SetupError(path + " is not a folder, so nothing was changed")
    return home, path


def _home_folder(parts, private):
    """The folder `parts` names under the system's home, made after _walk: the levels in `private` (counted from the home) are the user's
    own and 0700; then its real path must be the home's own. SetupError otherwise."""
    home, path = _walk(parts)
    try:
        for n in private: state.private_folder(os.path.join(home, *parts[:n]))
    except state.Unusable: raise SetupError(path + " is not a folder of yours, so nothing was changed") from None
    if os.path.realpath(path) != os.path.join(os.path.realpath(home), *parts):
        raise SetupError(path + " leads outside your home's own folders, so nothing was changed")
    return path


def _private_copies():
    """The copies' folder, and the state folder above it, the user's own and 0700, with no link below the home (_home_folder)."""
    return _home_folder(COPY_PARTS, (len(state.STATE_DIR), len(COPY_PARTS)))


class _Plan:
    """One file's change, read and made in memory with nothing written yet."""

    def __init__(self, path, change, what):
        self.path, self.what, self.notes = path, what, []
        self.old_text, old, mode = _read(path)
        new = change(json.loads(json.dumps(old)), self.notes)
        self.text = None if new == old else json.dumps(new, indent=2) + "\n"           # a file there is none of stays none
        self.mode = 0o600 if mode is None else mode

    @property
    def keeps(self):
        """Whether writing it keeps a copy first: it changes a file that is there, and no copy of that path is kept yet."""
        return self.text is not None and self.old_text is not None and not os.path.lexists(copy_of(self.path))

    def write(self):
        """Write the file, after its copy where it keeps one; the copy's path, or None."""
        if self.text is None: return None
        kept = None
        if self.keeps:
            kept = copy_of(self.path)
            _write(kept, self.old_text, 0o600)
        _write(self.path, self.text, self.mode)
        return kept


def _write_all(plans, dry, would, done):
    """Write each plan, the copies' folder made first where any keeps a copy, and say what was done, one line each."""
    if not dry and any(p.keeps for p in plans): _private_copies()
    for p in plans:
        if p.text is None: done.append(would[2] % p.path if p.what is None else "Already set up: %s (%s)." % (p.path, p.what)); continue
        kept = None if dry else p.write()
        done.append(would[0 if dry else 1] % (p.path, p.what if p.what is not None else ", ".join(p.notes)))
        if p.what is not None: done += [("Would take out " if dry else "Took out ") + note + "." for note in p.notes]
        if kept: done.append("Kept %s as it was in %s." % (p.path, kept))


def _pin_file():
    """Where the pin goes, ~/.config/quelvio/server under the system's home, checked and nothing made: no link below the home on its
    folder's path (_pinned holds that the address the key follows is the user's alone), and the file no link either."""
    _, folder = _walk(PIN_PARTS)
    path = os.path.join(folder, http.PINNED[-1])
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isfile(path)):
        raise SetupError(path + " is not a regular file, so nothing was changed")
    return path


def _pin(server):
    """Write the server's address to the pin: the folder 0700 and the user's own, the file 0600, never through a link."""
    _home_folder(PIN_PARTS, (len(PIN_PARTS),))
    path = _pin_file()
    _write(path, server + "\n", 0o600)
    return path


def _checked(run, scope):
    if not (isinstance(run, list) and run and all(isinstance(w, str) for w in run) and os.path.isabs(run[0])):
        raise SetupError("the command must start with an absolute path")
    if scope not in SCOPES: raise SetupError("the scope is local or user")


def setup_claude_code(agent, run, settings, mcp=None, server=None, env=None, cwd=None, scope="local", dry=False):
    """Write Claude Code's hooks for `agent` and register its MCP server, each entry starting with `run` (launcher()'s). Every check comes
    first, so a refusal changes nothing; with `dry`, nothing is written or run and the lines say what would be. Returns one line each."""
    env = {} if env is None else env
    try: agent_name(agent)
    except ValueError as e: raise SetupError(str(e) + "; nothing was changed") from None
    _checked(run, scope)
    if server is not None:
        try: server = http.address(server)
        except ValueError as e: raise SetupError(str(e) + "; nothing was changed") from None
    plans = [_Plan(settings, lambda d, n: _with_hooks(d, run, agent, n), "hooks for " + ", ".join(hooks.EVENTS) + " and QUELVIO_AGENT")]
    if mcp is not None: plans.append(_Plan(mcp, lambda d, n: _with_server(d, run, agent, n), "the MCP server " + SERVER_NAME))
    done = []
    if server is not None and not dry: _pin_file()                                   # checked before the first write, as everything else
    _write_all(plans, dry, ("Would update %s: %s.", "Updated %s: %s.", None), done)
    if mcp is None:
        if dry: done.append("Would run: " + shlex.join(claude_line(run, agent, scope)))
        else: done += _register(run, agent, env, cwd, scope)
    if server is not None:
        done.append("Would pin the server %s." % server if dry else "Pinned the server %s in %s." % (server, _pin(server)))
    return done


def remove_claude_code(settings, mcp=None, env=None, cwd=None, scope="local", dry=False):
    """Take out exactly what setup_claude_code wrote: our hooks, QUELVIO_AGENT, the MCP server "quelvio". The key and the pin stay."""
    env = {} if env is None else env
    if scope not in SCOPES: raise SetupError("the scope is local or user")
    plans = [_Plan(settings, _without_hooks, None)]
    if mcp is not None: plans.append(_Plan(mcp, _without_server, None))
    done = []
    _write_all(plans, dry, ("Would take out of %s: %s.", "Took out of %s: %s.", "Nothing of quelvio's in %s."), done)
    if mcp is None:
        line = ["claude", "mcp", "remove", "--scope", scope, SERVER_NAME]
        done.append("Would run: " + shlex.join(line) if dry else _unregister(env, cwd, scope))
    for p in plans:
        if os.path.lexists(copy_of(p.path)):
            done.append("%s as it was before setup is kept in %s; delete it when you no longer need it." % (p.path, copy_of(p.path)))
    return done
