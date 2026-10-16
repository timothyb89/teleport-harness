"""The k8s executor (harness/kube_apply.py): apply order, waits, alias IPs, secrets — all
against a fake kubectl, so no cluster is needed."""

from __future__ import annotations

import base64
import json

import pytest
import yaml

from harness.kube_apply import (ApplyError, Kubectl, apply_cluster, condition_met,
                                secret_objects, with_host_aliases)


def _pod(name, phase="Pending", ready=False, ip=None, waiting=None):
    st = {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}
    if ip:
        st["podIP"] = ip
    if waiting:
        st["containerStatuses"] = [{"name": "main", "state": {"waiting": {"reason": waiting}}}]
    return {"metadata": {"name": name}, "status": st}


class FakeKubectl(Kubectl):
    """Pods come up when created: after `polls_to_ready` reads they are Running+Ready with an
    IP. Records every apply."""

    def __init__(self, existing=None, polls_to_ready=2, fail=None):
        super().__init__(context="fake")
        self.applied: list[list[dict]] = []
        self.created: dict[str, int] = {}
        self.existing = existing or {}
        self.polls_to_ready = polls_to_ready
        self.fail = fail or {}          # pod -> phase it ends in

    def apply(self, objects):
        self.applied.append(objects)
        for o in objects:
            if o["kind"] == "Pod":
                self.created[o["metadata"]["name"]] = 0

    def get_json(self, *args):
        kind, name = args[0], args[1]
        if (kind, name) in self.existing:
            return self.existing[(kind, name)]
        if kind != "pod" or name not in self.created:
            return None
        self.created[name] += 1
        if name in self.fail and self.created[name] >= self.polls_to_ready:
            return _pod(name, self.fail[name])
        if self.created[name] >= self.polls_to_ready:
            return _pod(name, "Running", True, ip=f"10.42.0.{len(name)}")
        return _pod(name)


def _state(tmp_path, plan, pods):
    objs = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": plan["namespace"]}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "auth"}}]
    objs += [{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": p},
              "spec": {"containers": [{"name": "main"}], **extra}} for p, extra in pods.items()]
    (tmp_path / "kube").mkdir()
    (tmp_path / "kube" / "manifests.yaml").write_text(yaml.safe_dump_all(objs))
    (tmp_path / "kube" / "plan.json").write_text(json.dumps(plan))
    return tmp_path


PLAN = {
    "namespace": "th-c1",
    "order": [["c1-auth"], ["c1-bot"]],
    "pods": {"c1-auth": {"needs": [], "host_aliases": {}},
             "c1-bot": {"needs": [{"pod": "c1-auth", "condition": "ready"}],
                        "host_aliases": {"c1.lab.example.com": "c1-auth"}}},
    "secrets": [],
}


def _run(tmp_path, kc, plan=PLAN, pods=None):
    pods = pods if pods is not None else {
        "c1-auth": {"hostAliases": [{"ip": "127.0.0.1", "hostnames": ["c1.lab.example.com"]}]},
        "c1-bot": {}}
    logs = []
    apply_cluster(_state(tmp_path, plan, pods), kc, timeout=60, log=logs.append,
                  sleep=lambda s: None, clock=iter(range(0, 10_000)).__next__)
    return logs


def test_applies_namespace_then_objects_then_pods_by_wave(tmp_path):
    kc = FakeKubectl()
    logs = _run(tmp_path, kc)
    kinds = [[o["kind"] for o in batch] for batch in kc.applied]
    assert kinds == [["Namespace"], ["Service"], ["Pod"], ["Pod"]]
    assert [b[0]["metadata"]["name"] for b in kc.applied[2:]] == ["c1-auth", "c1-bot"]
    assert logs[-1] == "wave 2/2: c1-bot"


def test_alias_is_pinned_to_the_owners_ip_once_it_has_one(tmp_path):
    kc = FakeKubectl()
    _run(tmp_path, kc)
    bot = kc.applied[-1][0]
    assert bot["spec"]["hostAliases"] == [{"ip": "10.42.0.7", "hostnames": ["c1.lab.example.com"]}]
    # the owner keeps its own loopback entry untouched
    assert kc.applied[2][0]["spec"]["hostAliases"] == [{"ip": "127.0.0.1", "hostnames": ["c1.lab.example.com"]}]


def test_with_host_aliases_merges_by_ip():
    pod = {"metadata": {"name": "p"},
           "spec": {"hostAliases": [{"ip": "127.0.0.1", "hostnames": ["self.lab"]}]}}
    got = with_host_aliases(pod, {"a.lab": "o1", "b.lab": "o1", "c.lab": "o2"},
                            {"o1": "10.0.0.1", "o2": "10.0.0.2"})
    assert got["spec"]["hostAliases"] == [
        {"ip": "10.0.0.1", "hostnames": ["a.lab", "b.lab"]},
        {"ip": "10.0.0.2", "hostnames": ["c.lab"]},
        {"ip": "127.0.0.1", "hostnames": ["self.lab"]}]
    assert pod["spec"]["hostAliases"] == [{"ip": "127.0.0.1", "hostnames": ["self.lab"]}]  # not mutated
    with pytest.raises(ApplyError, match="needs o3's IP"):
        with_host_aliases(pod, {"d.lab": "o3"}, {})


def test_refuses_an_existing_namespace(tmp_path):
    kc = FakeKubectl(existing={("namespace", "th-c1"): {"metadata": {"name": "th-c1"}}})
    with pytest.raises(ApplyError, match="already exists"):
        _run(tmp_path, kc)
    assert kc.applied == []


def test_a_needed_pod_that_fails_stops_the_apply(tmp_path):
    kc = FakeKubectl(fail={"c1-auth": "Failed"})
    with pytest.raises(ApplyError, match="c1-auth failed before becoming ready"):
        _run(tmp_path, kc)
    assert [b[0]["metadata"]["name"] for b in kc.applied if b[0]["kind"] == "Pod"] == ["c1-auth"]


def test_wait_times_out_with_the_waiting_reason(tmp_path):
    class Stuck(FakeKubectl):
        def get_json(self, *args):
            if args[0] == "pod" and args[1] in self.created:
                return _pod(args[1], waiting="ImagePullBackOff")
            return super().get_json(*args)
    with pytest.raises(ApplyError, match=r"timed out .* c1-auth to be ready .*ImagePullBackOff"):
        _run(tmp_path, Stuck())


def test_conditions():
    assert condition_met(_pod("p", "Running"), "started")
    assert not condition_met(_pod("p", "Running"), "ready")
    assert condition_met(_pod("p", "Running", ready=True), "ready")
    assert condition_met(_pod("p", "Succeeded"), "succeeded")
    assert not condition_met(_pod("p", "Running"), "succeeded")
    with pytest.raises(ApplyError, match="complete successfully"):
        condition_met(_pod("p", "Failed"), "succeeded")
    with pytest.raises(ApplyError, match="cannot start: main: InvalidImageName"):
        condition_met(_pod("p", waiting="InvalidImageName"), "started")


def test_secrets_copy_from_namespace_and_read_files(tmp_path):
    lic = tmp_path / "license.pem"
    lic.write_bytes(b"LICENSE")
    kc = FakeKubectl(existing={("secret", "wildcard-cert"): {
        "type": "kubernetes.io/tls", "data": {"tls.crt": "Y3J0"},
        "metadata": {"name": "wildcard-cert", "namespace": "harness-system", "uid": "x"}}})
    plan = {"namespace": "th-c1", "secrets": [
        {"name": "license", "file": str(lic), "key": "license.pem"},
        {"name": "wildcard-cert", "from": "harness-system"}]}
    lic_s, cert_s = secret_objects(plan, kc)
    assert base64.b64decode(lic_s["data"]["license.pem"]) == b"LICENSE"
    assert cert_s["type"] == "kubernetes.io/tls" and cert_s["data"] == {"tls.crt": "Y3J0"}
    assert cert_s["metadata"] == {"name": "wildcard-cert", "namespace": "th-c1",
                                  "labels": {"app.kubernetes.io/part-of": "teleport-harness"}}
    with pytest.raises(ApplyError, match="import-cert.sh"):
        secret_objects({"namespace": "th-c1", "secrets": [{"name": "nope", "from": "harness-system"}]},
                       FakeKubectl())


def test_secrets_are_applied_before_pods(tmp_path):
    lic = tmp_path / "l.pem"
    lic.write_text("L")
    plan = {**PLAN, "secrets": [{"name": "license", "file": str(lic), "key": "license.pem"}]}
    kc = FakeKubectl()
    _run(tmp_path, kc, plan)
    assert [b[0]["kind"] for b in kc.applied][:3] == ["Namespace", "Secret", "Service"]


def test_kubectl_is_always_pinned(monkeypatch):
    monkeypatch.setenv("HARNESS_KUBE_CONTEXT", "lab")
    monkeypatch.setenv("HARNESS_KUBECONFIG", "/k.yaml")
    assert Kubectl().argv("get", "ns") == \
        ["kubectl", "--kubeconfig", "/k.yaml", "--context", "lab", "get", "ns"]
    monkeypatch.delenv("HARNESS_KUBECONFIG")
    assert Kubectl().argv("get", "ns") == ["kubectl", "--context", "lab", "get", "ns"]
    monkeypatch.delenv("HARNESS_KUBE_CONTEXT")
    with pytest.raises(ApplyError, match="refusing to use the kubeconfig's current one"):
        Kubectl().argv("get", "ns")
