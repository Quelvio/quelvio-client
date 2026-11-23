"""spool.py — the turns a failed call couldn't deliver.

One file in the client's private state folder, spool.json, mode 600, read and written under a lock, since every session's hooks share it.
Each item keeps the key id and the client session id it was spooled under, its prompt id and its time. It rides the next call made under
its own key, whatever that call's session: a session whose last calls failed still gets its turns delivered by a later one.

The file holds at most 50 turns and 1 MB, and no turn older than 24 hours; the oldest drop first, and are counted. A prune bounds it by its
24 hours even when no call comes. A turn larger than the spool can hold is dropped and counted. An item never holds instruction files: they
are taken out of a failed call's body before it is spooled.

Items ride oldest first, only as far as the call's body allows: one that doesn't fit is passed over, and the next that fits rides. An item
leaves the spool only once the call that carried it succeeded. A 401, 403 or 426 deletes every item of that key (policy.py), and a 422
drops the items the call carried, each counted (calls.py)."""
import contextlib
import json
import os
import re
import secrets
import time

from quelvio.client.state import _folder, fcntl, private_folder

MAX_TURNS, MAX_BYTES, MAX_AGE = 50, 1_000_000, 24 * 3600
NEVER_SPOOLED = ("instructions", "instruction_files")
NAME = "spool.json"
TEMPORARY = re.compile(r"^spool\.json\.[0-9a-f]{8}$")          # what a hook killed between write and rename leaves
STALE_TEMPORARY = 60


def _size(entry):
    return len(json.dumps(entry, separators=(",", ":")).encode("utf-8")) + 1


def _wire(i):
    return {"route": i["route"], "body": i["body"]}


class _File:
    """spool.json and its lock."""

    def __init__(self, env):
        self.dir = _folder(env)
        self.path = os.path.join(self.dir, NAME)

    @contextlib.contextmanager
    def locked(self):
        private_folder(self.dir)
        fd = os.open(os.path.join(self.dir, "spool.lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if fcntl: fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally: os.close(fd)

    def load(self):
        try: fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError: return {"items": [], "dropped": 0}
        try:
            with os.fdopen(fd, encoding="utf-8") as f: d = json.load(f)
        except ValueError: return {"items": [], "dropped": 0}
        if not isinstance(d, dict) or not isinstance(d.get("items"), list): return {"items": [], "dropped": 0}
        d.setdefault("dropped", 0)
        return d

    def save(self, d):
        tmp = self.path + "." + secrets.token_hex(4)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(d, f, separators=(",", ":"))
        os.replace(tmp, self.path)


def _bound(d, now):
    """The 24 hours first, then the oldest out while over 50 turns or 1 MB; every drop counted."""
    items = d["items"]
    fresh = [i for i in items if now - i.get("t", 0) <= MAX_AGE]
    d["dropped"] += len(items) - len(fresh)
    while fresh and (len(fresh) > MAX_TURNS or _size(d | {"items": fresh}) > MAX_BYTES):
        fresh.pop(0); d["dropped"] += 1
    d["items"] = fresh
    return d


class Spool:
    """The spool as one key sees it: it adds under this key, and only this key's items ride its calls."""

    def __init__(self, env, key_id):
        self._env, self._file, self.key_id = env, _File(env), key_id
        self.path = self._file.path

    def add(self, call, session, prompt_id, now):
        """Spool a failed call, {"route", "body"}, made in `session`: its instruction files taken out first."""
        body = {k: v for k, v in (call.get("body") or {}).items() if k not in NEVER_SPOOLED}
        entry = {"route": call.get("route"), "body": body, "key_id": self.key_id, "session": session, "prompt_id": prompt_id, "t": now}
        with self._file.locked():
            d = self._file.load()
            if _size(entry) > MAX_BYTES: d["dropped"] += 1
            else: d["items"].append(entry)
            self._file.save(_bound(d, now))

    def fitting(self, room, now):
        """This key's items that ride the next call, oldest first, whose sizes fit in `room` bytes, whatever session they came from."""
        with self._file.locked():
            d = self._file.load()
            before = (len(d["items"]), d["dropped"])
            d = _bound(d, now)
            if (len(d["items"]), d["dropped"]) != before: self._file.save(d)
        out, used = [], 0
        for i in d["items"]:
            if i.get("key_id") != self.key_id: continue
            w = _wire(i)
            if used + _size(w) <= room:
                out.append(w); used += _size(w)
        return out

    def sent(self, riding):
        """The call that carried these items succeeded: they leave the spool."""
        gone = [json.dumps(r, sort_keys=True) for r in riding]
        with self._file.locked():
            d, keep = self._file.load(), []
            for i in d["items"]:
                k = json.dumps(_wire(i), sort_keys=True)
                if i.get("key_id") == self.key_id and k in gone: gone.remove(k)
                else: keep.append(i)
            d["items"] = keep
            self._file.save(d)

    def drop(self, riding, also=0):
        """A 422: these items leave the spool, and they and `also` more (the call's own turn) are counted as dropped."""
        gone = [json.dumps(r, sort_keys=True) for r in riding]
        with self._file.locked():
            d, keep = self._file.load(), []
            for i in d["items"]:
                k = json.dumps(_wire(i), sort_keys=True)
                if i.get("key_id") == self.key_id and k in gone: gone.remove(k); d["dropped"] += 1
                else: keep.append(i)
            d["items"], d["dropped"] = keep, d["dropped"] + also
            self._file.save(d)

    def delete(self):
        """A 401, 403 or 426: every item of this key leaves the spool."""
        delete_key(self._env, self.key_id)

    def dropped(self):
        return self._file.load().get("dropped", 0)


def delete_key(env, key_id):
    """Every item of this key leaves the spool, whatever its session (a 401, 403 or 426)."""
    f = _File(env)
    with f.locked():
        d = f.load()
        d["items"] = [i for i in d["items"] if i.get("key_id") != key_id]
        f.save(d)


def prune(env, now):
    """Bound the spool by its 24 hours, its 50 turns and its 1 MB, whether or not a call comes; and remove a temporary file older than a
    minute, which only a hook killed between its write and its rename leaves."""
    f = _File(env)
    if not os.path.isdir(f.dir): return
    with f.locked():
        if os.path.exists(f.path): f.save(_bound(f.load(), now))
        for name in os.listdir(f.dir):
            p = os.path.join(f.dir, name)
            try:
                if TEMPORARY.match(name) and time.time() - os.lstat(p).st_mtime > STALE_TEMPORARY: os.remove(p)
            except OSError: pass
