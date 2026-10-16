"""Deterministic image layers for the k8s backend (docs/remote-kubernetes.md §4).

With no docker daemon, the k8s backend publishes images by appending layer tarballs onto a
base with `crane append`. The registry is the cache that every client shares, so the SAME
input must always produce the SAME bytes: then a teleport binary staged on the laptop and
the same binary staged in Beams are one blob, and the second push uploads nothing. A plain
`tar` can't promise that. It records mtimes, the local uid/gid and user names, and on macOS
xattrs and AppleDouble files. So this writes the tar itself: sorted entries, mtime 0,
root:root, fixed modes, GNU format (long docs-tree paths need its long-name entries).
"""

from __future__ import annotations

import hashlib
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DIR_MODE = 0o755


@dataclass(frozen=True)
class Entry:
    """One regular file in a layer: `src` on disk lands at `dst` (relative, no leading /)."""
    src: Path
    dst: str
    mode: int


def _clean_dst(dst: str) -> str:
    p = PurePosixPath(dst.lstrip("/"))
    if not p.parts or ".." in p.parts:
        raise ValueError(f"bad layer path {dst!r}")
    return str(p)


def file_entry(src: str | Path, dst: str, mode: int | None = None) -> Entry:
    """A single file. Without an explicit mode it is 0755 if executable on disk, else 0644."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"no such file: {src}")
    if mode is None:
        mode = 0o755 if src.stat().st_mode & 0o111 else 0o644
    return Entry(src, _clean_dst(dst), mode)


def dir_entries(src: str | Path, prefix: str) -> list[Entry]:
    """Every regular file under the directory `src`, placed under `prefix`. Symlinks are
    followed (a layer of a docs tree should carry the content, not a dangling link)."""
    src = Path(src)
    if not src.is_dir():
        raise FileNotFoundError(f"no such directory: {src}")
    prefix = _clean_dst(prefix)
    return [file_entry(p, f"{prefix}/{p.relative_to(src).as_posix()}")
            for p in sorted(src.rglob("*")) if p.is_file()]


def _info(name: str, *, is_dir: bool, mode: int, size: int = 0) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    ti.mode = mode
    ti.size = size
    ti.mtime = 0
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def write_layer(entries: list[Entry], out: str | Path) -> str:
    """Write `entries` (plus every parent directory, 0755) as an uncompressed tar at `out`
    and return its sha256 hex digest. The bytes depend only on the paths, modes and file
    contents."""
    by_dst: dict[str, Entry] = {}
    for e in entries:
        if e.dst in by_dst and by_dst[e.dst].src != e.src:
            raise ValueError(f"two sources for {e.dst}: {by_dst[e.dst].src} and {e.src}")
        by_dst[e.dst] = e
    dirs = {str(p) for e in by_dst.values() for p in PurePosixPath(e.dst).parents if str(p) != "."}
    clash = dirs & by_dst.keys()
    if clash:
        raise ValueError(f"{sorted(clash)[0]} is both a file and a directory")

    with tarfile.open(out, "w", format=tarfile.GNU_FORMAT) as tf:
        for name in sorted(dirs | by_dst.keys()):
            if name in dirs:
                tf.addfile(_info(name + "/", is_dir=True, mode=DIR_MODE))
                continue
            e = by_dst[name]
            with e.src.open("rb") as f:
                tf.addfile(_info(name, is_dir=False, mode=e.mode, size=e.src.stat().st_size), f)

    h = hashlib.sha256()
    with open(out, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
