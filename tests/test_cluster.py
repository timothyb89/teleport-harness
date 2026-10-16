"""The backend seam (harness/cluster.py, lib/backend.sh): which Cluster an existing cluster
gets, how an exec is spelled, and that the agent's workbench tool only ever runs commands
behind the exec prefix the harness hands it."""

from __future__ import annotations

import importlib
import subprocess

import pytest

from harness.cluster import DockerCluster, make_cluster, read_meta


def _state(tmp_path, meta: str):
    (tmp_path / "meta.env").write_text(meta)
    return tmp_path


def test_make_cluster_uses_the_backend_recorded_in_meta(tmp_path):
    c = make_cluster("c1", _state(tmp_path, "CLUSTER_ID=c1\nBACKEND=docker\n"), module="m")
    assert isinstance(c, DockerCluster) and c.module == "m" and c.state_dir == tmp_path


def test_make_cluster_defaults_to_docker_for_clusters_from_before_backends(tmp_path):
    assert isinstance(make_cluster("c1", _state(tmp_path, "CLUSTER_ID=c1\n")), DockerCluster)
    assert isinstance(make_cluster("c1", None), DockerCluster)


def test_make_cluster_refuses_a_backend_with_no_implementation(tmp_path):
    with pytest.raises(ValueError, match="backend 'k8s' has no Cluster implementation"):
        make_cluster("c1", _state(tmp_path, "BACKEND=k8s\n"))


def test_read_meta_keeps_values_containing_equals(tmp_path):
    meta = read_meta(_state(tmp_path, "A=1\nSOURCE_REF=x=y\nnot a pair\n"))
    assert meta == {"A": "1", "SOURCE_REF": "x=y"}


def test_docker_exec_argv_and_exec_out_agree():
    seen = []

    class Recording(DockerCluster):
        def _run(self, argv):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "out", "")

    c = Recording("c1")
    assert c.exec_argv("tbot", ["ls", "/"]) == ["docker", "exec", "c1-tbot", "ls", "/"]
    assert c.exec_argv("workbench", [], workdir="/work") == \
        ["docker", "exec", "-w", "/work", "c1-workbench"]
    assert c.exec_out("tbot", ["ls", "/"]) == (0, "out")
    assert seen == [["docker", "exec", "c1-tbot", "ls", "/"]]


def _load_mcp(monkeypatch, raw: str | None):
    if raw is None:
        monkeypatch.delenv("WORKBENCH_EXEC", raising=False)
    else:
        monkeypatch.setenv("WORKBENCH_EXEC", raw)
    import harness.agent_mcp as mod
    return importlib.reload(mod)


def test_workbench_tool_runs_only_behind_the_configured_prefix(monkeypatch):
    mod = _load_mcp(monkeypatch, '["docker","exec","-w","/work","c1-workbench"]')
    seen = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    mod.run("cat /etc/hostname; docker ps")
    # the model's whole string is ONE argument to `sh -lc` inside the workbench
    assert seen == [["docker", "exec", "-w", "/work", "c1-workbench",
                     "sh", "-lc", "cat /etc/hostname; docker ps"]]


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '"docker exec x"', '["docker", 1]'])
def test_workbench_tool_refuses_without_a_valid_prefix(monkeypatch, raw):
    mod = _load_mcp(monkeypatch, raw)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("must not exec"))
    assert mod.run("id").startswith("error: no workbench configured")
