"""project.py — the project id.

The client takes the origin remote, or else the first remote. It lower-cases the host, drops the scheme, the user, a port and a trailing
.git, and turns ":" after the host into "/": host:org/repo and host/org/repo are the same id. With no remote, the id is "local:" and a hash
of the host name and the folder's path (the repository's top where there is one), so no path leaves the machine.

git is run read-only, as an argv list, with no optional locks and a short timeout; where it can't run, the id is local."""
import hashlib
import os
import re
import socket
import subprocess
import urllib.parse

TIMEOUT = 3
SCP = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>(?!//).+)$")


def from_remote(url):
    """One id for a remote, however it is written; None for what isn't a remote."""
    url = url.strip()
    if "://" in url:
        u = urllib.parse.urlsplit(url)
        host, path = (u.hostname or ""), u.path
    else:
        m = SCP.match(url)
        if not m: return None
        host, path = m.group("host"), m.group("path")
    path = path.strip("/")
    if path.endswith(".git"): path = path[:-4].rstrip("/")
    if not host or not path: return None
    return host.lower() + "/" + path


def _git(cwd, *args):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_OPTIONAL_LOCKS": "0", "HOME": os.environ.get("HOME", "")}
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError): return None
    return p.stdout if p.returncode == 0 else None


def project_id(cwd, hostname=None):
    names = (_git(cwd, "remote") or "").split()
    for name in (["origin"] if "origin" in names else []) + [n for n in names if n != "origin"]:
        url = _git(cwd, "config", "--get", "remote.%s.url" % name)
        pid = from_remote(url) if url else None
        if pid: return pid
    top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip() or os.path.realpath(cwd)
    host = hostname if hostname is not None else socket.gethostname()
    return "local:" + hashlib.sha256((host + "\0" + top).encode("utf-8")).hexdigest()[:32]
