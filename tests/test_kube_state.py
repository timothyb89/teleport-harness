"""Moving a cluster's state between clients (harness/kube_state.py) and the LAN relay."""

from __future__ import annotations

import asyncio
import io
import tarfile

import pytest

from harness.cli import main
from harness.kube_state import MAX_PACKED, pack, unpack
from harness.relay import serve


def _state(d):
    (d / "config").mkdir(parents=True)
    (d / "kube").mkdir()
    (d / "scripts" / "m").mkdir(parents=True)
    (d / "meta.env").write_text("CLUSTER_ID=c1\nBACKEND=k8s\nKUBECONFIG=/home/me/k.yaml\n"
                                "KUBE_CONTEXT=mine\nFQDN=c1.lab.x\n")
    (d / "config" / "auth.yaml").write_text("auth: 1\n")
    (d / "kube" / "plan.json").write_text("{}")
    (d / "kube" / "manifests.yaml").write_text("big: 1\n")
    (d / "scripts" / "m" / "run.sh").write_text("#!/bin/sh\n")
    (d / "scripts" / "m" / "run.sh").chmod(0o755)
    (d / "identity").write_text("SECRET CREDENTIAL")
    (d / "results-tbot.json").write_text("{}")
    return d


def test_pack_carries_what_managing_needs_and_nothing_else(tmp_path):
    blob = pack(_state(tmp_path / "a"))
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        names = sorted(tf.getnames())
        meta = tf.extractfile("meta.env").read().decode()
        assert tf.getmember("scripts/m/run.sh").mode == 0o755
    assert names == ["config/auth.yaml", "kube/plan.json", "meta.env", "scripts/m/run.sh"]
    assert "KUBECONFIG" not in meta and "KUBE_CONTEXT" not in meta and "FQDN=c1.lab.x" in meta


def test_pack_is_deterministic(tmp_path, monkeypatch):
    a = pack(_state(tmp_path / "a"))
    import time
    later = time.time() + 3600
    monkeypatch.setattr(time, "time", lambda: later)   # gzip stamps time.time() by default
    assert pack(_state(tmp_path / "b")) == a


def test_unpack_records_the_attaching_clients_connection(tmp_path):
    blob = pack(_state(tmp_path / "a"))
    dest = tmp_path / "b" / "c1"
    unpack(blob, dest, {"KUBE_CONTEXT": "beams", "KUBECONFIG": ""})
    meta = (dest / "meta.env").read_text()
    assert "KUBE_CONTEXT=beams\n" in meta and "KUBECONFIG=\n" in meta
    assert "mine" not in meta
    assert (dest / "config" / "auth.yaml").read_text() == "auth: 1\n"
    with pytest.raises(ValueError, match="already exists"):
        unpack(blob, dest, {})


def test_unpack_refuses_path_escapes(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("../evil")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="refusing archive member"):
        unpack(buf.getvalue(), tmp_path / "s", {})


def test_pack_refuses_more_than_a_configmap_holds(tmp_path):
    d = _state(tmp_path / "a")
    (d / "config" / "noise.bin").write_bytes(__import__("os").urandom(MAX_PACKED + 1024))
    with pytest.raises(ValueError, match="ConfigMap budget"):
        pack(d)


def test_cli_round_trip_and_set_validation(tmp_path):
    src = _state(tmp_path / "a")
    blob = tmp_path / "s.tgz"
    assert main(["state-pack", "--state-dir", str(src), "--out", str(blob)]) == 0
    assert main(["state-unpack", "--in", str(blob), "--state-dir", str(tmp_path / "c"),
                 "--set", "KUBE_CONTEXT=x"]) == 0
    assert main(["state-unpack", "--in", str(blob), "--state-dir", str(tmp_path / "d"),
                 "--set", "FQDN=evil"]) == 1


def test_relay_passes_bytes_both_ways():
    async def scenario():
        async def echo(r, w):
            w.write(b"pong:" + await r.read(4))
            await w.drain()
            w.close()
        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        relay = asyncio.create_task(serve(port, "127.0.0.1", up_port))
        await asyncio.sleep(0.1)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"ping")
        await w.drain()
        got = await asyncio.wait_for(r.read(100), 5)
        relay.cancel()
        upstream.close()
        return got
    assert asyncio.run(scenario()) == b"pong:ping"
