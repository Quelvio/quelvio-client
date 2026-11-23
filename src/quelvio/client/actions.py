"""actions.py — risky calls, client side. Which risky class a tool call is in, and what
the client fills in action: the branch, the remote, the paths and the host, each only when the call itself states it. What the call does
not state is "unknown", which is always safe: J17 and J18 count a rule's target as the action's when the action leaves it unknown. The
table "Commands and what the client fills" is normative; every row of it is a test.

A shell command is split on &&, ||, ;, |, & and new lines; sudo, env and bash -c are unwrapped. A command the client can't parse is
matched by its text. A script or a make target or package script inside the project root is read, one level, at most 64 KB, never run, and
gives classes only. What the working tree gives (the current branch, the upstream, a relative path) is filled only for a command of one
part, and not in a git part with an option before push, a GIT_ variable or a flag off the list that leaves what is pushed unchanged.

The client's own match, for when the check can't run, is client_match: only for it, a command is in every class whose trigger it holds.
Standard library only, over the subject patterns (quelvio/text/subjects.py)."""
import json
import os
import re
import shlex
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

from quelvio.text import subjects

UNKNOWN = "unknown"
ORDER = ("push", "deploy", "delete", "write_outside", "send")       # action.class: the first a row lists
BLOCKING = ("push", "delete", "deploy")                              # only these block or ask when the check can't run
SAFE_PUSH_FLAGS = {"--force", "--force-with-lease", "-u", "--set-upstream", "--no-verify", "--dry-run", "-q", "-v"}
SIMPLE_DEFAULTS = (None, "simple", "current", "upstream")
SCRIPT_BYTES = 64 * 1024
PARSE_CAP = 16 * 1024                                                # longer than this, a command is scanned for triggers, never parsed
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "function", "time"}
LOOP_HEADS = {"for", "case", "select", "esac"}
WRAPPERS = {"timeout": {"-s", "-k", "--signal", "--kill-after"}, "nice": {"-n", "--adjustment"}, "stdbuf": set(),
            "ionice": {"-c", "-n", "-p", "--class", "--classdata"}}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
BUILT_IN = {"Bash", "Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "ExitPlanMode", *WRITE_TOOLS}
MCP_WORDS = {"push": "push", "merge": "push", "delete": "delete", "deploy": "deploy", "publish": "deploy", "send": "send", "post": "send",
             "reply": "send", "comment": "send", "message": "send", "email": "send"}
SQL = (("drop", "table", "delete"), ("drop", "database", "delete"))
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
SPECIAL = re.compile(r"[$*?\[\]`]")
SEPARATORS = ("&&", "||", ";", "|", "&", "\n", "(", ")")


@dataclass
class Action:
    classes: list
    branch: str = UNKNOWN
    remote: str = UNKNOWN
    paths: object = UNKNOWN                                          # a list of absolute paths, or "unknown"
    host: str = UNKNOWN
    inside_project: object = None                                    # True, False or "unknown"; None for a class with no path
    tool: str = "Bash"
    command: str = ""

    @property
    def cls(self): return self.classes[0]

    @property
    def deletes(self): return "delete" in self.classes

    def fields(self):
        """The action as /v1/action takes it: what the call does not state is "unknown"; what its classes don't have is left out."""
        out = {"class": self.cls, "deletes": self.deletes}
        if "push" in self.classes: out.update(branch=self.branch, remote=self.remote)
        if {"delete", "write_outside"} & set(self.classes): out.update(paths=self.paths, inside_project=self.inside_project)
        if {"deploy", "send"} & set(self.classes): out["host"] = self.host
        return out


@dataclass
class Part:
    """One risky part of a command: its classes and what it states."""
    classes: set
    branch: str = UNKNOWN
    remote: str = UNKNOWN
    paths: list = field(default_factory=list)                        # each absolute, or UNKNOWN
    host: str = UNKNOWN
    pathful: bool = False


# ------------------------------------------------------------ splitting a command
def split_parts(command):
    """The command's parts, split on its separators outside quotes. A part shlex can't read is matched by its text."""
    parts, cur, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(command): cur.append(command[i + 1]); i += 1
            elif c == quote: quote = None
        elif c == "\\" and i + 1 < len(command): cur.append(c + command[i + 1]); i += 1
        elif c in "'\"": quote = c; cur.append(c)
        else:
            sep = next((s for s in SEPARATORS if command.startswith(s, i)), None)
            if sep:
                parts.append("".join(cur)); cur = []; i += len(sep); continue
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}


READERS = {"cat", "tee"}                         # programs that only read a heredoc: its body is data


def _heredoc_goes_to(before, after):
    """Where a heredoc goes: "shell" (the segment's program, or a shell its output is piped into), "data" (cat, tee, or git commit -F -
    and --file=-, which only read it), or "code" (anything else: an interpreter runs it)."""
    words = re.split(r"&&|\|\||;|\||\$\(|`|\(", before)[-1].split()
    prog = os.path.basename(words[0]) if words else ""
    piped = re.search(r"\|\s*(\S+)", after)
    if prog in SHELLS or (piped and os.path.basename(piped.group(1)) in SHELLS): return "shell"
    if prog in READERS: return "data"
    if prog == "git" and "commit" in words and any(w in ("-F", "--file", "-F-", "--file=-") or w.startswith("--file=") for w in words):
        return "data"
    return "code"


def heredoc_bodies(text):
    """The text with each heredoc that isn't a shell's taken out, in one pass, and the bodies to scan. A shell's heredoc stays, to be read as
    commands; a reader's (cat, tee, git commit -F -) is data and dropped, which keeps Claude Code's own `git commit -m "$(cat <<'EOF' …)"`
    plain; an interpreter's is code, scanned as eval's text is, targets unknown."""
    if "<<" not in text: return text, []
    lines, out, held, i = text.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]; out.append(line); i += 1
        m = HEREDOC.search(line)
        if not m: continue
        tag, tabs = m.group(3), m.group(1) == "-"
        body = []
        while i < len(lines) and (lines[i].lstrip("\t") if tabs else lines[i]) != tag:
            body.append(lines[i]); i += 1
        goes = _heredoc_goes_to(line[:m.start()], line[m.end():])
        if goes == "shell": out.extend(body)
        elif goes == "code": held.append("\n".join(body))
        if i < len(lines): out.append(lines[i]); i += 1
    return "\n".join(out), held


def substitutions(command):
    """The command with each $( … ) and `…` outside single quotes put as $SUB, and the texts they held, in one linear pass (depth counted,
    an unclosed one running to the end). What they held is scanned for triggers, never parsed."""
    out, inner, i, quote, n = [], [], 0, None, len(command)
    while i < n:
        c = command[i]
        if quote == "'":
            out.append(c); quote = None if c == "'" else quote; i += 1; continue
        if c == "\\" and i + 1 < n: out.append(command[i:i + 2]); i += 2; continue
        if c == "'" and quote is None: quote = "'"; out.append(c); i += 1; continue
        if c == '"': quote = None if quote == '"' else '"'; out.append(c); i += 1; continue
        if command.startswith("$(", i) or c == "`":
            close = ")" if c == "$" else "`"
            j, depth = i + (2 if c == "$" else 1), 1
            start = j
            while j < n and depth:
                if close == ")" and command.startswith("$(", j): depth += 1; j += 2; continue
                if close == ")" and command[j] == "(": depth += 1
                elif command[j] == close: depth -= 1
                j += 1
            inner.append(command[start:j - (1 if depth == 0 else 0)])
            out.append("$SUB"); i = j; continue
        out.append(c); i += 1
    return "".join(out), inner


def _unwrap(argv):
    """Leading VAR=value assignments, the shell's keywords, sudo, env, timeout, nice, stdbuf, ionice and their options taken off; returns
    (argv, the assigned names)."""
    names = []
    while argv:
        if argv[0] in KEYWORDS: argv = argv[1:]; continue
        if argv[0] in WRAPPERS:
            takes, argv = WRAPPERS[argv[0]], argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[2:] if argv[0] in takes else argv[1:]
            if argv and argv[0:1] and prog_is_duration(argv[0]) and takes is WRAPPERS["timeout"]: argv = argv[1:]
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]): names.append(argv[0].split("=", 1)[0]); argv = argv[1:]; continue
        if argv[0] in ("sudo", "env", "command", "exec", "nohup", "time"):
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                argv = argv[2:] if argv[0] in ("-u", "-g", "-U", "-C") else argv[1:]
            continue
        break
    return argv, names


def prog_is_duration(word):
    return bool(re.fullmatch(r"[0-9.]+[smhd]?", word))


def _shell_c(argv):
    """The command inside bash -c / sh -c / zsh -c, or None."""
    if argv and os.path.basename(argv[0]) in ("bash", "sh", "zsh", "dash") and "-c" in argv[1:]:
        i = argv.index("-c")
        if i + 1 < len(argv): return argv[i + 1]
    return None


def _words(text):
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text or "")]


# ------------------------------------------------------------ what a part is, and what it states
def _git(argv):
    """git's global options before the subcommand, the subcommand, and its arguments."""
    i, before = 1, []
    while i < len(argv) and argv[i].startswith("-"):
        before.append(argv[i])
        if argv[i] in GIT_VALUE_OPTIONS: i += 1
        i += 1
    return before, (argv[i] if i < len(argv) else None), argv[i + 1:]


def _in_order(args, words):
    """The words stand among the arguments in this order (flags and other words may come between)."""
    it = iter(a.lower() for a in args)
    return all(any(a == w for a in it) for w in words)


AUTHORITY_ENDS = frozenset("/?#")


def _no_userinfo(text):
    """Every URL's user and password taken out of the text (scheme://user:password@host… becomes scheme://host…): blank() knows a key's
    shape, not a password's. One pass, each authority read once, up to its last "@", so a password holding "@" goes whole."""
    out, i, n = [], 0, len(text)
    while True:
        j = text.find("://", i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        start = end = j + 3
        while end < n and text[end] not in AUTHORITY_ENDS and not text[end].isspace(): end += 1
        at = text.rfind("@", start, end)
        out.append(text[i:start])
        i = at + 1 if at >= 0 else start


def _no_login(value):
    """A remote's or a host's user info out where it has no scheme (u:pw@registry.example/x, user@host:path): what follows the last "@"
    before the first "/"."""
    slash = value.find("/")
    at = (value if slash < 0 else value[:slash]).rfind("@")
    return value[at + 1:] if at >= 0 else value


def _holds_at(value):
    """An "@" left in the value, in any form NFKC reads as one (full-width, small): user info neither pass could find."""
    return "@" in unicodedata.normalize("NFKC", value)


def _host(value):
    """The host a deploy names, or UNKNOWN. The whole value first loses its user info by both passes, and a value that still holds an "@"
    anywhere is UNKNOWN, before any cut: a password holding a raw "/" ends the cut inside the user info, and a token standing as
    the user would be read as the host. A docker digest (img@sha256:…) is UNKNOWN too, a test holding the choice."""
    if not value: return UNKNOWN
    value = _no_login(_no_userinfo(value))
    if _holds_at(value): return UNKNOWN
    if "://" in value: return urllib.parse.urlsplit(value).hostname or UNKNOWN
    first = value.split("/", 1)[0]
    return first if ("." in first or ":" in first) and "/" in value else UNKNOWN


def _path(target, cwd, home, one_part):
    """A target as an absolute path, or UNKNOWN: a variable, a glob or a substitution is unknown; ~/ is the user's home; ~name/ is unknown;
    a relative path only for a command of one part."""
    if SPECIAL.search(target): return UNKNOWN
    if target.startswith("~/") or target == "~": return os.path.normpath(home + target[1:])
    if target.startswith("~"): return UNKNOWN
    if os.path.isabs(target): return os.path.normpath(target)
    return os.path.normpath(os.path.join(cwd, target)) if one_part else UNKNOWN


def _push(args, git, one_part, restricted):
    """git push's branch and remote, as the table fills them."""
    flags = [a for a in args if a.startswith("-")]
    positional = [a for a in args if not a.startswith("-")]
    restricted = restricted or not one_part or any(f.split("=", 1)[0] not in SAFE_PUSH_FLAGS for f in flags)
    remote, refspecs = (positional[0], positional[1:]) if positional else (None, [])
    deleting = "--delete" in flags or "-d" in flags
    classes = {"push"}
    if deleting or any(r.lstrip("+").startswith(":") for r in refspecs): classes.add("delete")
    branch = UNKNOWN
    if any(f in ("--all", "--mirror", "--branches", "--tags") for f in flags) or len(refspecs) > 1: branch = UNKNOWN
    elif refspecs:
        spec = refspecs[0].lstrip("+")
        src, dst = spec.split(":", 1) if ":" in spec else (spec, spec)
        dst = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
        if SPECIAL.search(dst): branch = UNKNOWN
        elif ":" in spec or deleting: branch = dst or UNKNOWN
        elif dst == "HEAD": branch = (git.current_branch() or UNKNOWN) if not restricted else UNKNOWN
        else: branch = dst
    elif not restricted:
        current, upstream = git.current_branch(), git.upstream()
        name = remote or (upstream[0] if upstream else None)
        if (current and upstream and upstream[1] == current and git.config("push.default") in SIMPLE_DEFAULTS
                and not (name and git.config("remote.%s.push" % name))):
            branch = current
    if remote is None and not restricted:
        upstream = git.upstream()
        remote = upstream[0] if upstream else None
    if remote and SPECIAL.search(remote): remote = None
    return Part(classes, branch=branch, remote=remote or UNKNOWN)


def _part(argv, cwd, home, git, one_part, git_vars):
    """One parsed part: its classes and what it states; None when it is not risky."""
    if not argv: return None
    prog = os.path.basename(argv[0])
    args, words = argv[1:], [w for a in argv for w in _words(a)]
    classes, part = set(), None
    if prog in LOOP_HEADS: return None
    if prog in ("eval", "watch"):                                                   # a command given as text: scanned, its targets unknown
        rest = [x for x in args if not x.startswith("-")]
        found = _text_classes(" ".join(rest))
        return Part(found, pathful=bool({"delete", "write_outside"} & found), paths=[UNKNOWN]) if found else None
    if prog == "find":
        found = {"delete"} if "-delete" in args else set()
        for j, x in enumerate(args):
            if x in ("-exec", "-execdir", "-ok", "-okdir"):
                sub = []
                for y in args[j + 1:]:
                    if y in (";", "+", "\\;"): break
                    sub.append(y)
                p = _part(sub, cwd, home, git, one_part=False, git_vars=[])
                if p: found |= p.classes
        return Part(found, pathful=bool({"delete", "write_outside"} & found), paths=[UNKNOWN]) if found else None
    if prog == "git":
        before, sub, rest = _git(argv)
        if sub == "push": part = _push(rest, git, one_part, restricted=bool(before) or any(n.startswith("GIT_") for n in git_vars))
        elif sub == "clean": classes.add("delete")
        elif sub == "reset" and "--hard" in rest: classes.add("delete")
        elif sub == "branch" and "-D" in rest: classes.add("delete")
    elif prog == "gh" and _in_order(args, ("pr", "merge")): classes.add("push")
    elif prog in ("rm", "rmdir"):
        targets = [a for a in args if not a.startswith("-")]
        part = Part({"delete"}, paths=[_path(t, cwd, home, one_part) for t in targets] or [UNKNOWN], pathful=True)
    elif prog == "kubectl" and _in_order(args, ("delete",)): classes |= {"deploy", "delete"}
    elif prog == "kubectl" and _in_order(args, ("apply",)): classes.add("deploy")
    elif prog == "terraform" and _in_order(args, ("destroy",)): classes |= {"deploy", "delete"}
    elif prog == "terraform" and _in_order(args, ("apply",)): classes.add("deploy")
    elif prog == "fly" and _in_order(args, ("deploy",)): classes.add("deploy")
    elif prog == "vercel" and "--prod" in args: classes.add("deploy")
    elif prog == "twine" and _in_order(args, ("upload",)): classes.add("deploy")
    elif prog == "docker" and _in_order(args, ("push",)):
        image = next((a for a in args[args.index("push") + 1:] if not a.startswith("-")), None) if "push" in args else None
        part = Part({"deploy"}, host=_host(image))
    elif prog == "npm" and _in_order(args, ("publish",)):
        reg = next((a.split("=", 1)[1] for a in args if a.startswith("--registry=")), None)
        if reg is None and "--registry" in args and args.index("--registry") + 1 < len(args): reg = args[args.index("--registry") + 1]
        part = Part({"deploy"}, host=_host(reg))
    if any(w in ("deploy",) for w in [prog.split(".")[0]] + [a.lower() for a in args if not a.startswith("-")]): classes.add("deploy")
    for a, b, c in SQL:
        if any(words[i:i + 2] == [a, b] for i in range(len(words) - 1)): classes.add(c)
    if "truncate" in words and prog not in ("truncate",) and any(" " in x or x.lower() == "truncate" for x in args): classes.add("delete")
    if part is not None:
        part.classes |= classes
        return part
    return Part(classes) if classes else None


def _text_classes(text):
    """For a command the client can't parse: every class whose trigger words stand anywhere in its text."""
    w, out = _words(text), set()
    runs = set(w) | set(zip(w, w[1:])) | set(zip(w, w[1:], w[2:]))                  # built once: each check is a lookup, linear in all
    def has(*seq): return (seq[0] if len(seq) == 1 else seq) in runs
    w = set(w)
    if has("git", "push") or has("gh", "pr", "merge"): out.add("push")
    if has("rm") or has("rmdir") or has("git", "clean") or has("drop", "table") or has("drop", "database") or has("truncate") or \
            has("kubectl", "delete") or has("terraform", "destroy") or ("git" in w and "push" in w and "delete" in w): out.add("delete")
    if has("deploy") or has("kubectl", "apply") or has("kubectl", "delete") or has("terraform", "apply") or has("terraform", "destroy") or \
            has("docker", "push") or has("npm", "publish") or has("twine", "upload") or ("vercel" in w and "prod" in w): out.add("deploy")
    return out


# ------------------------------------------------------------ scripts and targets inside the project root
def _inside(path, root):
    real, top = os.path.realpath(path), os.path.realpath(root)
    return real == top or real.startswith(top.rstrip(os.sep) + os.sep)


def _read_inside(path, root):
    """A file's text when it lies inside the project root (a link resolved first) and is at most 64 KB; else None."""
    if not _inside(path, root) or not os.path.isfile(path): return None
    try:
        if os.path.getsize(path) > SCRIPT_BYTES: return None
        with open(path, encoding="utf-8", errors="replace") as f: return f.read(SCRIPT_BYTES)
    except OSError: return None


def _script_text(argv, cwd, root, home):
    """The lines a part runs from inside the project root: a script file, a make target or a package script. None otherwise."""
    prog = os.path.basename(argv[0])
    target = argv[1] if prog in ("sh", "bash", "zsh", "dash") and len(argv) > 1 and not argv[1].startswith("-") else \
        argv[0] if "/" in argv[0] or argv[0].endswith(".sh") else None
    if target:
        path = os.path.normpath(home + target[1:]) if target.startswith("~/") else os.path.normpath(os.path.join(cwd, target))
        return _read_inside(path, root)
    if prog == "make" and len(argv) > 1:
        text = _read_inside(os.path.join(cwd, "Makefile"), root)
        if text is None: return None
        lines, on = [], False
        for line in text.splitlines():
            if re.match(r"^%s\s*:(?!=)" % re.escape(argv[1]), line): on = True; continue
            if on and line.startswith("\t"): lines.append(line.strip())
            elif on and line.strip(): break
        return "\n".join(lines) or None
    if prog in ("npm", "pnpm", "yarn") and len(argv) > 1:
        name = argv[2] if argv[1] == "run" and len(argv) > 2 else (argv[1] if prog == "yarn" else None)
        text = _read_inside(os.path.join(cwd, "package.json"), root) if name else None
        try: return (json.loads(text).get("scripts") or {}).get(name) if text else None
        except ValueError: return None
    return None


def _command_classes(text, cwd, home, git):
    """The classes of every part of a script's or a target's lines: one level, targets never filled."""
    out = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        for raw in split_parts(line):
            try: argv, names = _unwrap(shlex.split(raw))
            except ValueError: out |= _text_classes(raw); continue
            p = _part(argv, cwd, home, git, one_part=False, git_vars=names)
            if p: out |= p.classes
    return out


# ------------------------------------------------------------ one value, or unknown
def _one(values):
    vals = set(values)
    return vals.pop() if len(vals) == 1 and UNKNOWN not in vals else UNKNOWN


def _ordered(classes):
    return [c for c in ORDER if c in classes]


def _bash(command, cwd, root, home, git):
    command, code = heredoc_bodies(command)
    if len(command) > PARSE_CAP:
        found = _text_classes(command).union(*(_text_classes(c) for c in code))
        return Action(_ordered(found), command=command[:PARSE_CAP], inside_project=UNKNOWN if {"delete", "write_outside"} & found else None) \
            if found else None
    command_text, held = substitutions(command)
    held = held + code                                                   # an interpreter's heredoc is scanned as substitutions are
    parts = [p for p in split_parts(command_text) if p.strip() and p.strip() not in KEYWORDS]
    expanded = [(Part(found, pathful=bool({"delete", "write_outside"} & found), paths=[UNKNOWN]), "$SUB")
                for found in (_text_classes(h) for h in held) if found]
    for raw in parts:
        try: argv, names = _unwrap(shlex.split(raw))
        except ValueError:
            found = _text_classes(raw)
            if found: expanded.append((Part(found), raw))
            continue
        inner = _shell_c(argv)
        if inner is not None and _text_classes(inner) and not _bash(inner, cwd, root, home, git):     # scanned too, never only parsed
            expanded.append((Part(_text_classes(inner), pathful=True, paths=[UNKNOWN]), raw)); continue
        if inner is not None:
            sub = _bash(inner, cwd, root, home, git) if len(parts) == 1 else None
            if sub: return Action(sub.classes, sub.branch, sub.remote, sub.paths, sub.host, sub.inside_project, command=command)
            if inner and len(parts) > 1:
                found = _command_classes(inner, cwd, home, git)
                if found: expanded.append((Part(found), raw))
            continue
        p = _part(argv, cwd, home, git, one_part=len(parts) == 1, git_vars=names)        # a substitution's pieces hold $ or `: unknown
        if p is None and argv:
            text = _script_text(argv, cwd, root, home)
            found = _command_classes(text, cwd, home, git) if text else set()
            if not found and argv[0] not in LOOP_HEADS:                                     # its words, a quoted message apart
                found = _text_classes(" ".join(x for x in argv if not any(ch.isspace() for ch in x)))
            if found: p = Part(found, pathful=bool({"delete", "write_outside"} & found), paths=[UNKNOWN])
        if p is not None: expanded.append((p, raw))
    if not expanded: return None
    classes = set().union(*(p.classes for p, _ in expanded))
    a = Action(_ordered(classes), command=command)
    pushes = [p for p, _ in expanded if "push" in p.classes]
    a.branch, a.remote = _one(p.branch for p in pushes), _one(p.remote for p in pushes)
    hosts = [p for p, _ in expanded if {"deploy", "send"} & p.classes]
    a.host = _one(p.host for p in hosts) if hosts else UNKNOWN
    pathful = [p for p, _ in expanded if {"delete", "write_outside"} & p.classes]
    paths = [x for p in pathful for x in (p.paths or [UNKNOWN])]
    a.paths = paths if paths and UNKNOWN not in paths else UNKNOWN
    if {"delete", "write_outside"} & classes:
        a.inside_project = UNKNOWN if a.paths == UNKNOWN else all(_inside(x, root) for x in a.paths)
    return a


# ------------------------------------------------------------ tools other than Bash
def _name_tokens(name):
    tool = name.rsplit("__", 1)[-1]
    return subjects._tokens(tool)


def _mcp(name, args):
    tokens = _name_tokens(name)
    if not tokens or tokens[0] in subjects.READ_VERBS: return None
    classes = set()
    for t in tokens:
        for word, cls in MCP_WORDS.items():
            if t in subjects._forms(word): classes.add(cls)
    if not classes: return None
    a = Action(_ordered(classes), tool=name, command=name + "(" + ", ".join(sorted(args)) + ")")
    if "push" in classes:
        given = [args.get(k) for k in ("base", "into", "target") if isinstance(args.get(k), str)]
        if any(t in subjects._forms("push") for t in tokens):
            given += [args.get(k) for k in ("branch", "ref") if isinstance(args.get(k), str)]
        a.branch = given[0] if len(set(given)) == 1 else UNKNOWN
    if "send" in classes:
        to = next((args[k] for k in ("to", "recipient", "address", "email") if isinstance(args.get(k), str)), None)
        a.host = to or UNKNOWN
    if "deploy" in classes:
        a.host = _host(next((args[k] for k in ("url", "host", "registry") if isinstance(args.get(k), str)), None)) if "send" not in classes else a.host
    if "delete" in classes: a.inside_project = UNKNOWN
    return a


def read(tool, tool_input, cwd, root, git, home=None):
    """The action a tool call is, or None when it is in no risky class."""
    home = home or os.path.expanduser("~")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        command = tool_input.get("command")
        return _bash(command, cwd, root, home, git) if isinstance(command, str) and command.strip() else None
    if tool in WRITE_TOOLS:
        target = tool_input.get(WRITE_TOOLS[tool])
        if not isinstance(target, str) or not target: return None
        path = _path(target, cwd, home, one_part=True)
        if path != UNKNOWN and _inside(path, root): return None
        return Action(["write_outside"], paths=[path] if path != UNKNOWN else UNKNOWN,
                      inside_project=False if path != UNKNOWN else UNKNOWN, tool=tool, command=tool)
    if tool in BUILT_IN: return None
    return _mcp(tool, tool_input)


# ------------------------------------------------------------ the client's match, when the check can't run
@dataclass(frozen=True)
class Match:
    decision: str                       # "allow", "ask", or "deny" (in the background: the step is skipped and reported)
    unavailable: bool = True            # the outcome check is unavailable
    report: bool = True                 # reported with the next call that reaches the server
    rule: int = None                    # which of the rules matched (its index), for the sentence the person reads
    cls: str = None                     # the action's class that rule names


def client_match(action, rules, attended):
    """On the subjects the client holds for every rule in audience: a rule of the action's Push, Delete or Deploy or publish class asks when
    attended and denies in the background. With no subjects received, or no such rule, the action is allowed, with the check unavailable."""
    if rules is not None:
        for i, subs in enumerate(rules):
            for c in action.classes:
                if c in BLOCKING and subjects.matches(subs or [], c): return Match("ask" if attended else "deny", rule=i, cls=c)
    return Match("allow")
