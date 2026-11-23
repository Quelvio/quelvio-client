"""environment.py — what the client writes into Claude Code's environment file, CLAUDE_ENV_FILE. Every process the
agent starts from its shell inherits that file, so it holds the parent session's id and nothing else: never the key, nor any value shaped as a
token, nor anything a shell would read as more than one word."""
import re
import shlex

ENV_NAMES = ("QUELVIO_PARENT_SESSION",)
VALUE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
TOKEN_PREFIXES = ("qv_", "qma_", "qlv_")                 # Quelvio's token prefixes


def add_to_env_file(env, name, value):
    """Append `export NAME=value` to the file CLAUDE_ENV_FILE names, keeping what is there. False when no file is named. Raises ValueError,
    writing nothing, for any other name, for a value that holds a token's prefix, and for one that isn't a single plain word."""
    if name not in ENV_NAMES: raise ValueError("the client writes only %s to the environment file" % ", ".join(ENV_NAMES))
    if not isinstance(value, str) or not VALUE.fullmatch(value) or any(p in value for p in TOKEN_PREFIXES):
        raise ValueError("not a session id the environment file may hold")
    path = env.get("CLAUDE_ENV_FILE")
    if not path: return False
    with open(path, "a", encoding="utf-8") as f: f.write("export %s=%s\n" % (name, shlex.quote(value)))
    return True
