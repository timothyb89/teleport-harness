"""Carry a cluster's state dir between clients (docs/remote-kubernetes.md §6, Phase 5).

state/<id>/ is client-local, but a k8s cluster can outlive the client that made it (a
Beams session ends; the laptop takes over). So `up` packs what managing the cluster needs
into the namespace (ConfigMap harness-meta), and `attach` on another client unpacks it into
a fresh state/<id>/. What travels: meta.env, the render (compose, config, bootstrap,
scripts, apply-on-startup), setup.json and kube/plan.json. What doesn't: results, logs, the
admin identity (a credential — re-run `admin`), and big build outputs (tf-plugins) that the
cluster already has as artifact images.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

# Relative to the state dir; directories are taken whole.
CARRIED = ["meta.env", "setup.json", "docker-compose.yml", "kube/plan.json",
           "config", "bootstrap", "apply-on-startup", "scripts"]
# A ConfigMap is capped at 1 MiB including metadata; stay clear of it.
MAX_PACKED = 900 * 1024
# meta.env keys that describe the CLIENT, not the cluster: an attaching client sets its own.
CLIENT_KEYS = ("KUBECONFIG", "KUBE_CONTEXT")


def _info(name: str, size: int, is_dir: bool = False) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    ti.size, ti.mtime, ti.uid, ti.gid = size, 0, 0, 0
    ti.mode = 0o755 if is_dir else 0o644
    return ti


def pack(state_dir: Path) -> bytes:
    """A gzipped tar of the carried files. The client's own connection is dropped from
    meta.env (it names a path on THIS machine)."""
    state_dir = Path(state_dir)
    buf = io.BytesIO()
    # gzip's header carries a timestamp; pin it so equal state packs to equal bytes.
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tf:
        for rel in CARRIED:
            p = state_dir / rel
            files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else []
            for f in files:
                name = f.relative_to(state_dir).as_posix()
                data = f.read_bytes()
                if name == "meta.env":
                    data = b"".join(line for line in data.splitlines(keepends=True)
                                    if line.split(b"=", 1)[0].decode() not in CLIENT_KEYS)
                ti = _info(name, len(data))
                if f.stat().st_mode & 0o111:
                    ti.mode = 0o755
                tf.addfile(ti, io.BytesIO(data))
    out = buf.getvalue()
    if len(out) > MAX_PACKED:
        raise ValueError(f"packed state is {len(out)} bytes, over the {MAX_PACKED}-byte "
                         f"ConfigMap budget")
    return out


def unpack(blob: bytes, state_dir: Path, client: dict[str, str]) -> None:
    """Unpack into a NEW state dir and record this client's connection in meta.env."""
    state_dir = Path(state_dir)
    if state_dir.exists():
        raise ValueError(f"{state_dir} already exists")
    state_dir.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or m.name.startswith("/") or ".." in Path(m.name).parts:
                raise ValueError(f"refusing archive member {m.name!r}")
        tf.extractall(state_dir, filter="data")
    meta = state_dir / "meta.env"
    if not meta.is_file():
        raise ValueError("packed state has no meta.env")
    lines = [ln for ln in meta.read_text().splitlines() if ln.split("=", 1)[0] not in CLIENT_KEYS]
    lines += [f"{k}={client.get(k, '')}" for k in CLIENT_KEYS]
    meta.write_text("\n".join(lines) + "\n")
