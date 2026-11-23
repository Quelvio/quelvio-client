"""normalize_sdist.py — the PyPI package `quelvio`'s sdist made byte-reproducible (tools/normalize_sdist.py in
Quelvio/quelvio-client). setuptools writes the sdist's members with the files' own times and the builder's user, and the gzip header with
the build's time; the wheel needs nothing (SOURCE_DATE_EPOCH is enough). This rewrites the .tar.gz in place with the same contents: members
sorted, each time SOURCE_DATE_EPOCH, owner 0:0 with no names, modes 0644 and 0755 for folders, no pax extras, gzip time 0 and no name.
Standard library only.

    SOURCE_DATE_EPOCH=<the tagged commit's time> python3 tools/normalize_sdist.py dist/quelvio-X.Y.Z.tar.gz"""
import gzip
import io
import os
import sys
import tarfile


def normalize(path, epoch):
    with tarfile.open(path, "r:gz") as t:
        members = [(m, t.extractfile(m).read() if m.isfile() else None) for m in t.getmembers()]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as out:
        for m, data in sorted(members, key=lambda x: x[0].name):
            if not (m.isfile() or m.isdir()): raise ValueError("the sdist holds something that is neither a file nor a folder: " + m.name)
            m.mtime, m.uid, m.gid, m.uname, m.gname, m.pax_headers = epoch, 0, 0, "", "", {}
            m.mode = 0o755 if m.isdir() else 0o644
            out.addfile(m, io.BytesIO(data) if data is not None else None)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0, compresslevel=9) as g:
        g.write(buf.getvalue())
    os.replace(tmp, path)


if __name__ == "__main__":
    if len(sys.argv) != 2 or not os.environ.get("SOURCE_DATE_EPOCH", "").isdigit():
        raise SystemExit("usage: SOURCE_DATE_EPOCH=<seconds> normalize_sdist.py <the sdist>")
    normalize(sys.argv[1], int(os.environ["SOURCE_DATE_EPOCH"]))
