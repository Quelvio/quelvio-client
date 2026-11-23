"""state.py — what the client keeps between its hook runs: the hook's input, the client session id, the prompt id, and
one private state file per session, under a lock. Ported from the old client's hook.py (read_input, State, turn_of); the old QUELVIO_MEMORY_* names are not carried.

The state folder is ~/.local/state/quelvio/, mode 700, a folder of the user's own and never a link. Each session's file is named by a hash
of its id, mode 600, replaced whole under the session's lock. No key is ever written here."""
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import time

try: import fcntl
except ImportError: fcntl = None                                     # no file lock where there is none (Windows)

INPUT_BYTES = 4 * 1024 * 1024
STATE_DIR = (".local", "state", "quelvio")
SESSION = re.compile(r"[A-Za-z0-9._:-]{1,128}")                    # the contract's client session id: at most 128 characters
TOKEN_PREFIXES = ("qv_", "qma_", "qlv_")                            # Quelvio's token prefixes
STALE_DAYS = 2
STATE_NAME = re.compile(r"^[0-9a-f]{32}\.(json(\.[0-9a-f]{8})?|lock)$")


class Unusable(ValueError):
    """The hook's input or the state folder can't be used: the hook adds nothing and exits 0."""


def read_input(stdin):
    """The hook's JSON object on stdin; {} when there is none. Over INPUT_BYTES it is refused unread."""
    raw = stdin.buffer.read(INPUT_BYTES + 1) if hasattr(stdin, "buffer") else stdin.read(INPUT_BYTES + 1).encode("utf-8")
    if len(raw) > INPUT_BYTES: raise Unusable("the hook's input is too large")
    try: text = raw.decode("utf-8")
    except UnicodeDecodeError: raise Unusable("the hook's input is not UTF-8") from None
    if not text.strip(): return {}
    try: d = json.loads(text)
    except ValueError: raise Unusable("the hook's input is not JSON") from None
    if not isinstance(d, dict): raise Unusable("the hook's input is not a JSON object")
    return d


def session_of(d):
    """The client session id: Claude Code's session_id, a plain word of at most 128 characters that holds no token's prefix."""
    sid = d.get("session_id")
    if not isinstance(sid, str) or not SESSION.fullmatch(sid) or any(p in sid for p in TOKEN_PREFIXES):
        raise Unusable("the hook's input has no usable session_id")
    return sid


def prompt_id_of(d):
    """Claude Code's own prompt_id where it gives a plain one; a stable hash of an odd one; else a new id the client makes."""
    v = d.get("prompt_id")
    if isinstance(v, str) and v:
        return v if SESSION.fullmatch(v) else hashlib.sha256(v.encode("utf-8")).hexdigest()[:32]
    return secrets.token_hex(16)


def _from_system():
    """The user's home as the system records it: the password database on Linux and macOS, the known folder on Windows. Never HOME or
    USERPROFILE: a project's settings can set either for a session's hooks, and the client's state, its spool and the pinned server
    address must not follow them into a folder the project chose. Unusable where the system can't say, or names an empty or relative
    home."""
    try: import pwd
    except ImportError: home = _windows_home()
    else:
        try: home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError: raise Unusable("the system names no home for this user") from None
    if not isinstance(home, str) or not os.path.isabs(home):                         # an empty or relative home would be the project's folder
        raise Unusable("the system's home for this user is empty or relative")
    return home


def _windows_home():
    """FOLDERID_Profile through SHGetKnownFolderPath (Windows reads QUELVIO_KEY alone)."""
    try:
        import ctypes, uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]
        folder = GUID.from_buffer_copy(uuid.UUID("5E6C858F-0E22-4760-9AFE-EA3317B67173").bytes_le)
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(path)) != 0: raise OSError
        try: return path.value
        finally: ctypes.windll.ole32.CoTaskMemFree(path)
    except Exception: raise Unusable("the system names no home for this user") from None   # noqa: BLE001


def system_home():
    """Where the client keeps what is its own (tests point it at a folder of theirs)."""
    return _from_system()


def _folder(env):
    return os.path.join(system_home(), *STATE_DIR)


def walk_home(parts, make=False):
    """system_home()/parts[0]/…/parts[-1], every part a real folder, never a link (the home itself may be one), and the whole resolving
    under the real home (GNU stow links a whole ~/.local or ~/.config into a dotfiles repository, and makedirs would
    follow it, putting the state and spool, or reading a pin, from a folder others can change). make: a missing part is made, 0700.
    None where a part is missing and not made; Unusable where a part is a link or not a folder, or the path leaves the real home, its
    message naming that part (a person whose stow linked the folder can find it)."""
    home = system_home()
    path = home
    for part in parts:
        path = os.path.join(path, part)
        try: st = os.lstat(path)
        except FileNotFoundError:
            if not make: return None
            try: os.mkdir(path, 0o700)
            except FileExistsError: pass
            st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode): raise Unusable(path + " is a link")
        if not stat.S_ISDIR(st.st_mode): raise Unusable(path + " is not a folder")
    if os.path.realpath(path) != os.path.join(os.path.realpath(home), *parts): raise Unusable(path + " leads out of the home")
    return path


def private_folder(d):
    """Make d mode 700 if needed; refuse one that is a link or not the user's own. Under the system's home every folder between the
    home and d is walked first (walk_home), made where missing: none may be a link."""
    home = system_home()
    rel = os.path.relpath(d, home)
    if rel != os.curdir and rel != os.pardir and not rel.startswith(os.pardir + os.sep):
        os.makedirs(home, mode=0o700, exist_ok=True)                                 # as before: a home not made yet is made
        try: walk_home(tuple(rel.split(os.sep))[:-1], make=True)
        except Unusable as e: raise Unusable("%s, so Quelvio keeps nothing under it" % e) from None
    else: os.makedirs(os.path.dirname(d), mode=0o700, exist_ok=True)
    try: os.mkdir(d, 0o700)
    except FileExistsError: pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        raise Unusable(d + " must be a folder of yours")
    if stat.S_IMODE(st.st_mode) != 0o700: os.chmod(d, 0o700)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)


class State:
    """One session's state: one file named by a hash of its client session id, mode 600, in the private state folder."""

    def __init__(self, env, sid):
        self.dir = _folder(env)
        self.path = os.path.join(self.dir, hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32] + ".json")

    def load(self):
        try: fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError: return {}
        try:
            with os.fdopen(fd, encoding="utf-8") as f: d = json.load(f)
        except ValueError: return {}
        return d if isinstance(d, dict) else {}

    @contextlib.contextmanager
    def locked(self):
        """One hook run at a time per session: Stop runs in the background, beside the next prompt's serve."""
        private_folder(self.dir)
        fd = os.open(self.path[:-len(".json")] + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if fcntl: fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally: os.close(fd)

    def save(self, d):
        """Under the lock: the whole file replaced at once, mode 600."""
        tmp = self.path + "." + secrets.token_hex(4)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(d, f)
        os.replace(tmp, self.path)

    def started_at(self, now=None):
        """When this session's SessionStart hook first ran, in UTC with milliseconds: kept, and sent again on a repeated session/start."""
        with self.locked():
            d = self.load()
            if not d.get("started_at"):
                d["started_at"] = iso(time.time() if now is None else now)
                self.save(d)
            return d["started_at"]

    def prune(self):
        """Files left by a session that never ended are deleted after two days."""
        try: names = os.listdir(self.dir)
        except OSError: return
        cutoff = time.time() - STALE_DAYS * 86400
        for n in names:
            p = os.path.join(self.dir, n)
            try:
                if STATE_NAME.fullmatch(n) and os.lstat(p).st_mtime < cutoff: os.remove(p)
            except OSError: pass
