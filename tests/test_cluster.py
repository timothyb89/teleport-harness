"""The backend seam (harness/cluster.py, lib/backend.sh): which Cluster an existing cluster
gets, how an exec is spelled, and that the agent's workbench tool only ever runs commands
behind the exec prefix the harness hands it."""

from __future__ import annotations

import importlib
import subprocess

import pytest

from harness.cluster import DockerCluster, KubeCluster, make_cluster, read_meta


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
    with pytest.raises(ValueError, match="backend 'nomad' has no Cluster implementation"):
        make_cluster("c1", _state(tmp_path, "BACKEND=nomad\n"))


def test_make_cluster_k8s(tmp_path):
    assert isinstance(make_cluster("c1", _state(tmp_path, "BACKEND=k8s\n")), KubeCluster)


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


# ---- KubeCluster ---------------------------------------------------------------------------

PLAN = {
    "namespace": "th-c1",
    "services": {"auth": {"pod": "c1-auth", "container": "auth"},
                 "tbot": {"pod": "c1-shaper", "container": "tbot"},
                 "workbench": {"pod": "c1-workbench", "container": "workbench"}},
    "pullbacks": [{"source": "agent/out", "pod": "c1-workbench", "container": "workbench",
                   "path": "/out"}],
}


class RecordingKube(KubeCluster):
    def __init__(self, tmp_path, results=None, meta="BACKEND=k8s\nKUBE_CONTEXT=lab\n"):
        (tmp_path / "meta.env").write_text(meta)
        (tmp_path / "kube").mkdir(exist_ok=True)
        (tmp_path / "kube" / "plan.json").write_text(__import__("json").dumps(PLAN))
        super().__init__("c1", state_dir=tmp_path)
        self.seen, self.results = [], results or {}

    def _run(self, argv):
        self.seen.append(argv)
        rc, out, err = self.results.get(tuple(argv[-2:]), (0, "out", ""))
        return subprocess.CompletedProcess(argv, rc, out, err)


def test_kube_exec_targets_the_services_pod_and_container(tmp_path):
    c = RecordingKube(tmp_path)
    # a service sharing another's netns runs in THAT pod, in its own container
    assert c.exec_argv("tbot", ["ls"]) == \
        ["kubectl", "--context", "lab", "exec", "-n", "th-c1", "c1-shaper", "-c", "tbot", "--", "ls"]
    with pytest.raises(ValueError, match="no service 'nope'"):
        c.exec_argv("nope", ["ls"])


def test_kube_exec_uses_the_clusters_recorded_connection_not_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_KUBE_CONTEXT", "someone-elses-cluster")
    monkeypatch.setenv("KUBECONFIG", "/elsewhere")
    c = RecordingKube(tmp_path, meta="KUBECONFIG=/lima/kubeconfig.yaml\nKUBE_CONTEXT=default\n")
    assert c.exec_argv("auth", ["id"])[:5] == \
        ["kubectl", "--kubeconfig", "/lima/kubeconfig.yaml", "--context", "default"]


def test_kube_exec_refuses_a_cluster_with_no_recorded_context(tmp_path):
    c = RecordingKube(tmp_path, meta="BACKEND=k8s\n")
    with pytest.raises(ValueError, match="no KUBE_CONTEXT"):
        c.exec_argv("auth", ["id"])


def test_kube_workdir_prefix_still_takes_appended_argv(tmp_path):
    """agent_mcp appends ["sh", "-lc", cmd] to this prefix; the cd wrapper must exec it."""
    prefix = RecordingKube(tmp_path).exec_argv("workbench", [], workdir="/work")
    inner = prefix[prefix.index("--") + 1:] + ["echo", "hi"]
    assert inner[4] == "/work"
    out = subprocess.run(inner[:4] + [str(tmp_path)] + inner[5:], capture_output=True, text=True)
    assert out.stdout == "hi\n"


def test_kube_tctl_helpers_run_in_the_auth_pod(tmp_path):
    c = RecordingKube(tmp_path, {("--format", "json"): (0, '[{"metadata": {"name": "n1"}}]', "")})
    assert c.get_nodes() == [{"metadata": {"name": "n1"}}]
    assert c.seen[-1][:10] == ["kubectl", "--context", "lab", "exec", "-n", "th-c1", "c1-auth", "-c", "auth", "--"]


def test_kube_logs_put_the_previous_run_first(tmp_path):
    c = RecordingKube(tmp_path, {("-c", "auth"): (0, "second\n", ""),
                                 ("auth", "--previous"): (0, "first\n", "")})
    assert c.logs("auth") == "first\nsecond\n"
    c = RecordingKube(tmp_path, {("-c", "auth"): (0, "only\n", ""),
                                 ("auth", "--previous"): (1, "", "previous terminated container not found")})
    assert c.logs("auth") == "only\n"


def test_kube_state_file_reads_pullbacks_in_the_pod_and_the_rest_locally(tmp_path):
    (tmp_path / "observations.json").write_text("{}")
    c = RecordingKube(tmp_path, {("cat", "/out/agent-result.json"): (0, '{"status": "ok"}', "")})
    assert c.state_file("agent/out/agent-result.json") == '{"status": "ok"}'
    assert c.seen[-1][6:] == ["c1-workbench", "-c", "workbench", "--", "cat", "/out/agent-result.json"]
    assert c.state_file("observations.json") == "{}"
    assert c.state_file("agent/outside.json") is None     # not under the pullback, not local
