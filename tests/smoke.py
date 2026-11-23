"""smoke.py — the PyPI package `quelvio`'s check before it is published (tests/smoke.py in Quelvio/quelvio-client).
Standard library only and no network: it makes a venv of its own, installs the built wheel there with `pip install --no-index --no-deps`,
and runs the installed `quelvio` from an empty environment. No command here reads or writes a keychain, and no key is used.

- `quelvio --version` prints `quelvio <the wheel's version>`;
- `quelvio hook …` is silent and exits 0, whatever its input;
- `quelvio setup claude-code --dry-run` writes nothing and names the line it would register;
- the lines setup writes (configure.setup_claude_code, as the installed package runs it) start with that venv's python, -I -S, and its
  site-packages alone added to the path, after the standard library; run with a sitecustomize, a usercustomize and a `quelvio` on
  PYTHONPATH, a .pth file and a uuid.py in the venv's site-packages, and a `quelvio` in the folder it starts in, none of them loads (the
  PYTHONPATH and .pth ones do load in a plain run of that python, first);
- `quelvio mcp` with no message exits 0 and sends nothing; with no agent it says so and exits 2.

    python3 tests/smoke.py dist/quelvio-X.Y.Z-py3-none-any.whl          exit 0 when every check holds"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import venv

WRITE = ("import sys; from quelvio.client import cli, configure; "
         "configure.setup_claude_code('smoke', configure.launcher(sys.executable, cli.ROOT, 'quelvio.client'), sys.argv[1], mcp=sys.argv[2])")


def check(cond, what):
    if not cond: raise SystemExit("smoke: " + what)


def run(argv, home, stdin=b"", extra=None, cwd=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": home, **(extra or {})}                       # an empty environment, as env -i gives
    return subprocess.run(argv, input=stdin, capture_output=True, env=env, cwd=cwd, timeout=120)


def plant(folder, name, mark):
    path = os.path.join(folder, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f: f.write("open(%r, 'w').close()\n" % mark if not name.endswith(".pth") else "import os; open(%r, 'w').close()\n" % mark)


def main(wheel):
    wheel = os.path.abspath(wheel)
    version = os.path.basename(wheel).split("-")[1]
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home"); os.makedirs(home)
        venv.EnvBuilder(with_pip=True, clear=True).create(os.path.join(tmp, "venv"))
        python = os.path.join(tmp, "venv", "bin", "python")
        quelvio = os.path.join(tmp, "venv", "bin", "quelvio")
        p = run([python, "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", "-q", wheel], home)
        check(p.returncode == 0, "the wheel didn't install: " + p.stderr.decode()[-500:])
        site = run([python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"], home).stdout.decode().strip()
        p = run([quelvio, "--version"], home)
        check((p.returncode, p.stdout) == (0, ("quelvio %s\n" % version).encode()), "--version: %r" % (p.stdout,))
        for argv in (["hook", "SessionStart"], ["hook"], ["hook", "Nothing"]):
            for stdin in (b"{}", b"not json", b"", b'{"hook_event_name": "Nothing"}'):
                p = run([quelvio] + argv, home, stdin)
                check((p.returncode, p.stdout, p.stderr) == (0, b"", b""), "hook wasn't silent on %r %r" % (argv, stdin))

        settings, mcp = os.path.join(tmp, "proj", ".claude", "settings.local.json"), os.path.join(tmp, "proj", "mcp.json")
        p = run([quelvio, "setup", "claude-code", "--agent", "smoke", "--dry-run"], home, cwd=tmp)
        check(p.returncode == 0 and p.stdout.startswith(b"Would update ") and not os.path.exists(os.path.join(tmp, ".claude")),
              "the dry run: " + (p.stdout + p.stderr).decode()[-500:])
        would = shlex.split(p.stdout.decode().splitlines()[-1][len("Would run: "):])
        p = run([python, "-c", WRITE, settings, mcp], home)
        check(p.returncode == 0, "setup's writer failed: " + p.stderr.decode()[-500:])
        line = shlex.split(json.load(open(settings))["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        launch = line[:-2]
        check(line[-2:] == ["hook", "SessionStart"] and launch[1:4] == ["-I", "-S", "-c"], "the hook line: %r" % (line,))
        check(os.path.samefile(launch[0], python), "the hook line's python isn't the venv's: %r" % (launch[0],))
        check(launch[4] == "import sys; sys.path.append(%r); from quelvio.client.cli import entry; entry()" % site, "the hook line's code: " + launch[4])
        args = json.load(open(mcp))["mcpServers"]["quelvio"]["args"]
        check(args == launch[1:] + ["mcp", "--hooks"] and would[would.index("--") + 1:] == launch + ["mcp", "--hooks"], "the MCP args: %r" % (args,))

        marks, planted, here = (os.path.join(tmp, d) for d in ("marks", "planted", "here"))
        os.makedirs(marks); os.makedirs(here)
        usersite = run([python, "-c", "import site; print(site.getusersitepackages())"], home).stdout.decode().strip()
        plants = [(planted, "sitecustomize.py"), (planted, "usercustomize.py"), (planted, "quelvio/__init__.py"), (site, "zz_smoke_plant.pth"),
                  (usersite, "usercustomize.py"), (usersite, "zz_smoke_user.pth"), (here, "quelvio/__init__.py"), (site, "uuid.py")]
        for i, (folder, name) in enumerate(plants): plant(folder, name, os.path.join(marks, str(i)))
        extra = {"PYTHONPATH": planted}
        p = run([python, "-c", "pass"], home, extra=extra, cwd=here)                       # the plants work: a plain run loads them
        check({"0", "3"} <= set(os.listdir(marks)), "the plants didn't load in a plain run: %r" % (os.listdir(marks),))
        for m in os.listdir(marks): os.remove(os.path.join(marks, m))
        p = run(launch + ["--version"], home, extra=extra, cwd=here)
        check((p.returncode, p.stdout) == (0, ("quelvio %s\n" % version).encode()), "the launcher's --version: %r" % ((p.stdout + p.stderr)[-500:],))
        p = run(line, home, b'{"hook_event_name": "Nothing"}', extra=extra, cwd=here)
        check((p.returncode, p.stdout, p.stderr) == (0, b"", b""), "the written hook line wasn't silent")
        check(os.listdir(marks) == [], "a planted module loaded beside the hook: %r" % (os.listdir(marks),))
        os.remove(os.path.join(site, "zz_smoke_plant.pth")); os.remove(os.path.join(site, "uuid.py"))

        p = run([quelvio, "mcp", "--hooks"], home, b"", {"QUELVIO_AGENT": "smoke"})
        check((p.returncode, p.stdout, p.stderr) == (0, b"", b""), "mcp with no message")
        p = run([quelvio, "mcp"], home)
        check(p.returncode == 2 and b"QUELVIO_AGENT" in p.stderr, "mcp with no agent")
    print("smoke: quelvio %s holds" % version)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2: raise SystemExit("usage: smoke.py <the built wheel>")
    sys.exit(main(sys.argv[1]))
