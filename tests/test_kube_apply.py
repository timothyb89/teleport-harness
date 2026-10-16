"""The k8s executor (harness/kube_apply.py): apply order, waits, alias IPs, secrets — all
against a fake kubectl, so no cluster is needed."""

from __future__ import annotations

import base64
import json

import pytest
import yaml

from harness.kube_apply import (ApplyError, Kubectl, apply_cluster, condition_met,
                                secret_objects, with_nameserver)


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
        self.existing = {**SVC, **(existing or {})}
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
    "pods": {"c1-auth": {"needs": []},
             "c1-bot": {"needs": [{"pod": "c1-auth", "condition": "ready"}]}},
    "dns": {"pod": "harness-dns", "service": "harness-dns",
            "aliases": {"c1.lab.example.com": "c1-auth.th-c1.svc.cluster.local"}},
    "secrets": [],
}
DNS_CFG = {"dnsPolicy": "None", "dnsConfig": {"nameservers": [], "searches": ["th-c1.svc.cluster.local"]}}
SVC = {("service", "harness-dns"): {"spec": {"clusterIP": "10.43.0.53"}}}


def _run(tmp_path, kc, plan=PLAN, pods=None):
    pods = pods if pods is not None else {"harness-dns": {}, "c1-auth": DNS_CFG, "c1-bot": DNS_CFG}
    logs = []
    apply_cluster(_state(tmp_path, plan, pods), kc, timeout=60, log=logs.append,
                  sleep=lambda s: None, clock=iter(range(0, 10_000)).__next__)
    return logs


def test_applies_namespace_objects_resolver_then_pods_by_wave(tmp_path):
    kc = FakeKubectl()
    logs = _run(tmp_path, kc)
    kinds = [[o["kind"] for o in batch] for batch in kc.applied]
    assert kinds == [["Namespace"], ["Service"], ["Pod"], ["Pod"], ["Pod"]]
    assert [b[0]["metadata"]["name"] for b in kc.applied[2:]] == ["harness-dns", "c1-auth", "c1-bot"]
    assert logs[-1] == "wave 2/2: c1-bot"
    assert "resolver ready at 10.43.0.53 (1 aliases)" in logs


def test_every_pod_is_pointed_at_the_resolver(tmp_path):
    kc = FakeKubectl()
    _run(tmp_path, kc)
    for batch in kc.applied[3:]:
        assert batch[0]["spec"]["dnsConfig"]["nameservers"] == ["10.43.0.53"]
        assert batch[0]["spec"]["dnsConfig"]["searches"] == ["th-c1.svc.cluster.local"]


def test_with_nameserver_leaves_other_dns_policies_alone():
    pod = {"metadata": {"name": "p"}, "spec": {**DNS_CFG}}
    got = with_nameserver(pod, "10.0.0.9")
    assert got["spec"]["dnsConfig"]["nameservers"] == ["10.0.0.9"]
    assert pod["spec"]["dnsConfig"]["nameservers"] == []          # not mutated
    plain = {"metadata": {"name": "q"}, "spec": {}}
    assert with_nameserver(plain, "10.0.0.9") is plain


def test_a_resolver_service_without_an_ip_stops_the_apply(tmp_path):
    kc = FakeKubectl(existing={("service", "harness-dns"): {"spec": {"clusterIP": "None"}}})
    with pytest.raises(ApplyError, match="no ClusterIP"):
        _run(tmp_path, kc)


def test_refuses_an_existing_namespace(tmp_path):
    kc = FakeKubectl(existing={("namespace", "th-c1"): {"metadata": {"name": "th-c1"}}})
    with pytest.raises(ApplyError, match="already exists"):
        _run(tmp_path, kc)
    assert kc.applied == []


def test_a_needed_pod_that_fails_stops_the_apply(tmp_path):
    kc = FakeKubectl(fail={"c1-auth": "Failed"})
    with pytest.raises(ApplyError, match="c1-auth failed before becoming ready"):
        _run(tmp_path, kc)
    assert [b[0]["metadata"]["name"] for b in kc.applied if b[0]["kind"] == "Pod"] == ["harness-dns", "c1-auth"]


def test_wait_times_out_with_the_waiting_reason(tmp_path):
    class Stuck(FakeKubectl):
        def get_json(self, *args):
            if args[0] == "pod" and args[1] in self.created:
                return _pod(args[1], waiting="ImagePullBackOff")
            return super().get_json(*args)
    with pytest.raises(ApplyError, match=r"timed out .* harness-dns to be ready .*ImagePullBackOff"):
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


def _running(name, ready=False):
    p = _pod(name, "Running", ready, ip="10.42.0.9")
    p["status"]["containerStatuses"] = [{"name": "main", "state": {"running": {}}}]
    return p


def test_a_dependency_that_never_passes_its_healthcheck_fails_like_compose(tmp_path):
    class NeverHealthy(FakeKubectl):
        def get_json(self, *args):
            if args[0] == "pod" and args[1] == "c1-auth" and "c1-auth" in self.created:
                return _running("c1-auth")
            return super().get_json(*args)
    plan = {**PLAN, "pods": {**PLAN["pods"], "c1-auth": {"needs": [], "unhealthy_after": 30}}}
    with pytest.raises(ApplyError, match=r"c1-auth is unhealthy: running for \d+s .* after 30s"):
        _run(tmp_path, NeverHealthy(), plan)


def test_the_healthcheck_budget_starts_when_the_containers_run_not_at_creation(tmp_path):
    """Image pulls don't count against the budget: a pod that takes long to START still gets
    its whole budget once running."""
    polls = {"n": 0}

    class SlowPull(FakeKubectl):
        def get_json(self, *args):
            if args[0] == "pod" and args[1] == "c1-auth" and "c1-auth" in self.created:
                polls["n"] += 1
                if polls["n"] < 40:                      # ~80s "pulling"
                    return _pod("c1-auth", waiting="ContainerCreating")
                return _running("c1-auth", ready=polls["n"] > 45)
            return super().get_json(*args)
    plan = {**PLAN, "pods": {**PLAN["pods"], "c1-auth": {"needs": [], "unhealthy_after": 30}}}
    _run(tmp_path, SlowPull(), plan)                     # no error
