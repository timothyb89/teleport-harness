"""The compose -> Kubernetes translator (harness/kube.py).

Two layers. Focused tests pin each translation rule on a small synthetic compose. Golden
tests translate EVERY shipped module's real render and compare against a snapshot in
tests/golden/kube/, so any change to what a module becomes on the k8s backend shows up as
a reviewable diff. Regenerate after an intended change with:

    HARNESS_UPDATE_GOLDEN=1 uv run --extra dev pytest tests/test_kube.py

Snapshots keep ConfigMap KEYS but not contents (contents are the rendered files, already
covered by test_render.py, and would churn on every script edit), and replace the temp
state dir and the harness checkout with $STATE / $HARNESS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from harness.cli import main as cli_main
from harness.kube import CONFIGMAP_BUDGET, REGISTRY, TranslateError, translate
from harness.models import backend_requirement, gate, load_module
from harness.render import render_module

REPO = Path(__file__).resolve().parent.parent
MODULES = REPO / "modules"
COMPONENTS = REPO / "components"
GOLDEN = Path(__file__).resolve().parent / "golden" / "kube"
FQDN = "zz1.lab.example.com"


def _auth(**extra) -> dict:
    return {"image": "teleport-harness:test", "container_name": "zz1-auth", **extra}


def _t(services: dict, tmp_path: Path, **compose_extra):
    compose = {"name": "teleport-harness-zz1", "services": services,
               "volumes": {"auth-data": None}, **compose_extra}
    return translate(compose, cluster_id="zz1", state_dir=tmp_path, fqdn=FQDN, port=8443)


def _obj(t, kind: str, name: str) -> dict:
    return next(o for o in t.objects if o["kind"] == kind and o["metadata"]["name"] == name)


# ---- strictness ----------------------------------------------------------------------------

@pytest.mark.parametrize("services,extra,match", [
    ({"auth": _auth(ports=["80:80"])}, {}, r"service auth: unsupported compose keys \['ports'\]"),
    ({"auth": _auth()}, {"configs": {}}, r"unsupported top-level compose keys: \['configs'\]"),
    ({"auth": _auth()}, {"networks": {"lb": {"internal": True}}}, r"network lb: unsupported keys"),
    ({"auth": _auth(environment={"X": "${HOME}"})}, {}, r"contains '\$'"),
    ({"auth": _auth(environment=["X"])}, {}, r"environment X has no value"),
    ({"auth": _auth(user="teleport")}, {}, r"numeric uid"),
    ({"auth": _auth(volumes=["/dev/null:/dev/kmsg"])}, {}, r"device bind"),
    ({"auth": _auth(volumes=["nope:/x"])}, {}, r"'nope' is not declared"),
    ({"auth": {"image": "x"}}, {}, r"no container_name"),
    ({"auth": _auth(), "a": {"image": "x", "container_name": "zz1-b"}}, {}, r"'zz1-b' is not zz1-a"),
    ({"auth": _auth(), "a": {"image": "x", "container_name": "zz1-a", "depends_on": ["b"]},
      "b": {"image": "x", "container_name": "zz1-b", "depends_on": ["a"]}}, {}, r"dependency cycle"),
])
def test_untranslatable_compose_is_an_error_naming_what(services, extra, match, tmp_path):
    with pytest.raises(TranslateError, match=match):
        _t(services, tmp_path, **extra)


# ---- pods, services, ordering --------------------------------------------------------------

def test_service_becomes_a_named_pod_with_two_portless_headless_names(tmp_path):
    t = _t({"auth": _auth(), "tbot": {"image": "teleport-harness:test", "container_name": "zz1-tbot",
                                      "command": "tbot start -c /etc/tbot.yaml"}}, tmp_path)
    pod = _obj(t, "Pod", "zz1-tbot")
    assert pod["spec"]["containers"][0]["name"] == "tbot"
    assert pod["spec"]["containers"][0]["args"] == ["tbot", "start", "-c", "/etc/tbot.yaml"]
    for name in ("tbot", "zz1-tbot"):
        spec = _obj(t, "Service", name)["spec"]
        assert spec["clusterIP"] == "None" and spec["publishNotReadyAddresses"] is True
        assert "ports" not in spec and spec["selector"] == {"harness.teleport/pod": "zz1-tbot"}
    # compose injects no per-service env vars, and test workloads get no API credentials
    assert pod["spec"]["enableServiceLinks"] is False
    assert pod["spec"]["automountServiceAccountToken"] is False


def test_harness_images_go_to_the_registry_and_public_ones_are_untouched(tmp_path):
    t = _t({"auth": _auth(), "oidc": {"image": "teleport-harness-oidc:v4", "container_name": "zz1-oidc"},
            "lb": {"image": "nginx:1.27-alpine", "container_name": "zz1-lb"}}, tmp_path)
    assert _obj(t, "Pod", "zz1-auth")["spec"]["containers"][0]["image"] == f"{REGISTRY}/teleport-harness:test"
    assert _obj(t, "Pod", "zz1-oidc")["spec"]["containers"][0]["image"] == f"{REGISTRY}/teleport-harness-oidc:v4"
    assert _obj(t, "Pod", "zz1-lb")["spec"]["containers"][0]["image"] == "nginx:1.27-alpine"
    assert t.plan["local_images"] == ["teleport-harness-oidc:v4", "teleport-harness:test"]


def test_restart_policies_and_auth_always_restarts_in_place(tmp_path):
    t = _t({"auth": _auth(restart="no"),
            "a": {"image": "x", "container_name": "zz1-a"},
            "b": {"image": "x", "container_name": "zz1-b", "restart": "on-failure"}}, tmp_path)
    assert _obj(t, "Pod", "zz1-auth")["spec"]["restartPolicy"] == "Always"   # restart_auth
    assert _obj(t, "Pod", "zz1-a")["spec"]["restartPolicy"] == "Never"
    assert _obj(t, "Pod", "zz1-b")["spec"]["restartPolicy"] == "OnFailure"


def test_depends_on_becomes_waves_and_needs_keeping_the_strongest_wait(tmp_path):
    t = _t({"auth": _auth(healthcheck={"test": ["CMD-SHELL", "curl -f localhost:3000/healthz"],
                                       "interval": "5s", "timeout": "3s", "retries": 40}),
            "setup": {"image": "x", "container_name": "zz1-setup",
                      "depends_on": {"auth": {"condition": "service_started"}}},
            "bot": {"image": "x", "container_name": "zz1-bot",
                    "depends_on": {"auth": {"condition": "service_healthy"},
                                   "setup": {"condition": "service_completed_successfully"}}}},
           tmp_path)
    assert t.plan["order"] == [["zz1-auth"], ["zz1-setup"], ["zz1-bot"]]
    assert t.plan["pods"]["zz1-bot"]["needs"] == [{"pod": "zz1-auth", "condition": "ready"},
                                                  {"pod": "zz1-setup", "condition": "succeeded"}]
    probe = _obj(t, "Pod", "zz1-auth")["spec"]["containers"][0]["readinessProbe"]
    assert probe == {"exec": {"command": ["sh", "-c", "curl -f localhost:3000/healthz"]},
                     "periodSeconds": 5, "timeoutSeconds": 3, "failureThreshold": 40}


def test_dotted_aliases_resolve_through_the_namespace_resolver_with_no_start_order(tmp_path):
    # terraform_native_join_lb's shape: oidc waits (through lb) on auth, while auth needs
    # oidc's alias. hostAliases could not express that; a resolver answering at lookup
    # time, as docker's aliases do, needs no ordering at all.
    t = _t({"auth": _auth(networks={"internal": {"aliases": [FQDN]}}),
            "lb": {"image": "x", "container_name": "zz1-lb", "depends_on": ["auth"]},
            "oidc": {"image": "x", "container_name": "zz1-oidc", "depends_on": ["lb"],
                     "networks": {"internal": {"aliases": ["oidc.lab.example.com"]}}},
            "agent": {"image": "x", "container_name": "zz1-agent"}}, tmp_path)
    # waves are depends_on alone: aliases add none
    assert t.plan["order"] == [["zz1-agent", "zz1-auth"], ["zz1-lb"], ["zz1-oidc"]]
    assert t.plan["pods"]["zz1-agent"]["needs"] == []
    assert t.plan["dns"]["aliases"] == {FQDN: "zz1-auth.th-zz1.svc.cluster.local",
                                        "oidc.lab.example.com": "zz1-oidc.th-zz1.svc.cluster.local"}
    corefile = _obj(t, "ConfigMap", "harness-dns")["data"]["Corefile"]
    assert ("rewrite stop name exact oidc.lab.example.com zz1-oidc.th-zz1.svc.cluster.local "
            "answer auto") in corefile
    assert "forward . /etc/resolv.conf" in corefile
    for name in ("zz1-auth", "zz1-oidc", "zz1-agent"):
        spec = _obj(t, "Pod", name)["spec"]
        assert "hostAliases" not in spec
        assert spec["dnsPolicy"] == "None"
        assert spec["dnsConfig"]["searches"][0] == "th-zz1.svc.cluster.local"
    assert _obj(t, "Service", "harness-dns")["spec"]["ports"][0] == {"name": "dns", "port": 53, "protocol": "UDP"}


def test_a_service_named_like_the_resolver_is_refused(tmp_path):
    with pytest.raises(TranslateError, match="collides with the resolver"):
        _t({"auth": _auth(), "harness-dns": {"image": "x", "container_name": "zz1-harness-dns"}}, tmp_path)


def test_network_mode_service_becomes_a_native_sidecar_gating_the_dependent(tmp_path):
    t = _t({"auth": _auth(),
            "shaper": {"image": "alpine:3.20", "container_name": "zz1-shaper", "cap_add": ["NET_ADMIN"],
                       "healthcheck": {"test": ["CMD-SHELL", "test -f /tmp/shaped"], "interval": "2s",
                                       "retries": 60}},
            "tbot": {"image": "teleport-harness:test", "container_name": "zz1-tbot",
                     "network_mode": "service:shaper",
                     "depends_on": {"shaper": {"condition": "service_healthy"},
                                    "auth": {"condition": "service_healthy"}}}}, tmp_path)
    pod = _obj(t, "Pod", "zz1-shaper")
    side = pod["spec"]["initContainers"][-1]
    assert side["name"] == "shaper" and side["restartPolicy"] == "Always"
    assert side["startupProbe"]["exec"]["command"] == ["sh", "-c", "test -f /tmp/shaped"]
    assert side["securityContext"] == {"capabilities": {"add": ["NET_ADMIN"]}}
    assert [c["name"] for c in pod["spec"]["containers"]] == ["tbot"]
    assert not any(o["kind"] == "Pod" and o["metadata"]["name"] == "zz1-tbot" for o in t.objects)
    assert t.plan["services"]["tbot"] == {"pod": "zz1-shaper", "container": "tbot"}
    assert t.plan["pods"]["zz1-shaper"]["needs"] == [{"pod": "zz1-auth", "condition": "ready"}]
    assert _obj(t, "Service", "zz1-tbot")["spec"]["selector"] == {"harness.teleport/pod": "zz1-shaper"}


# ---- mounts -------------------------------------------------------------------------------

def test_small_binds_become_configmaps_with_modes_and_subpaths(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "tbot.yaml").write_text("version: v2\n")
    scripts = tmp_path / "scripts" / "m"; (scripts / "lib").mkdir(parents=True)
    (scripts / "run.sh").write_text("#!/bin/sh\n"); (scripts / "run.sh").chmod(0o755)
    (scripts / "lib" / "x.bin").write_bytes(b"\xff\x00")
    t = _t({"auth": _auth(),
            "tbot": {"image": "x", "container_name": "zz1-tbot",
                     "volumes": [f"{cfg}/tbot.yaml:/etc/tbot.yaml:ro", f"{scripts}:/scripts:ro"]}},
           tmp_path)
    mounts = _obj(t, "Pod", "zz1-tbot")["spec"]["containers"][0]["volumeMounts"]
    assert mounts[0]["mountPath"] == "/etc/tbot.yaml" and mounts[0]["subPath"] == "tbot.yaml"
    cm = _obj(t, "ConfigMap", mounts[1]["name"])
    assert cm["data"] == {"run.sh": "#!/bin/sh\n"} and list(cm["binaryData"]) == ["lib_x.bin"]
    vol = next(v for v in _obj(t, "Pod", "zz1-tbot")["spec"]["volumes"] if v["name"] == mounts[1]["name"])
    assert {"key": "run.sh", "path": "run.sh", "mode": 0o755} in vol["configMap"]["items"]
    assert {"key": "lib_x.bin", "path": "lib/x.bin"} in vol["configMap"]["items"]


def test_large_or_absent_binds_become_artifact_images(tmp_path):
    big = tmp_path / "tf-plugins"; big.mkdir()
    (big / "provider").write_bytes(b"\0" * (CONFIGMAP_BUDGET + 1))
    t = _t({"auth": _auth(),
            "tf": {"image": "x", "container_name": "zz1-tf",
                   "volumes": [f"{big}:/plugins:ro", "/nowhere/docs:/docs:ro"]}}, tmp_path)
    assert [a["source"] for a in t.plan["artifacts"]] == ["/nowhere/docs", str(big)] or \
        sorted(a["source"] for a in t.plan["artifacts"]) == sorted(["/nowhere/docs", str(big)])
    assert all(a["image"].startswith(f"{REGISTRY}/harness-artifact:zz1-") for a in t.plan["artifacts"])
    pod = _obj(t, "Pod", "zz1-tf")
    inits = pod["spec"]["initContainers"]
    assert {i["image"] for i in inits} == {a["image"] for a in t.plan["artifacts"]}
    assert not any(o["kind"] == "ConfigMap" and "plugins" in o["metadata"]["name"] for o in t.objects)


def test_rw_bind_is_a_seeded_pvc_and_is_pulled_back_from_the_state_dir(tmp_path):
    aos = tmp_path / "apply-on-startup"; aos.mkdir()
    (aos / "token.yaml").write_text("kind: token\n")
    out = tmp_path / "agent" / "out"; out.mkdir(parents=True)
    t = _t({"auth": _auth(volumes=[f"{aos}:/apply-on-startup"]),
            "workbench": {"image": "x", "container_name": "zz1-workbench", "volumes": [f"{out}:/out"]}},
           tmp_path)
    auth = _obj(t, "Pod", "zz1-auth")
    seed = auth["spec"]["initContainers"][0]
    assert "..data" in seed["command"][-1] and 'ls -A /dst' in seed["command"][-1]   # seed ONCE
    assert any("persistentVolumeClaim" in v for v in auth["spec"]["volumes"])
    # an empty source needs no seed
    assert "initContainers" not in _obj(t, "Pod", "zz1-workbench")["spec"]
    assert t.plan["pullbacks"] == [
        {"source": "apply-on-startup", "pod": "zz1-auth", "container": "auth", "path": "/apply-on-startup"},
        {"source": "agent/out", "pod": "zz1-workbench", "container": "workbench", "path": "/out"}]


def test_credentials_become_secrets_whose_contents_never_reach_the_manifests(tmp_path):
    lic = tmp_path / "license.pem"; lic.write_text("SECRET-LICENSE")
    t = _t({"auth": _auth(volumes=["harness-certs:/certs:ro", f"{lic}:/etc/teleport/license.pem:ro"])},
           tmp_path, volumes={"harness-certs": {"external": True, "name": "harness-certs"}})
    assert "SECRET-LICENSE" not in yaml.safe_dump_all(t.objects)
    assert t.plan["secrets"] == [{"name": "license", "file": str(lic), "key": "license.pem"},
                                 {"name": "wildcard-cert", "from": "harness-system"}]
    mounts = _obj(t, "Pod", "zz1-auth")["spec"]["containers"][0]["volumeMounts"]
    assert {"name": "secret-license", "mountPath": "/etc/teleport/license.pem",
            "subPath": "license.pem", "readOnly": True} in mounts


def test_ingress_routes_the_proxy_and_everything_below_it(tmp_path):
    t = translate({"services": {"auth": _auth()}}, cluster_id="zz1", state_dir=tmp_path,
                  fqdn=FQDN, port=8443, expires_at=1792200000)
    route = _obj(t, "IngressRouteTCP", "auth")["spec"]
    assert route["tls"] == {"passthrough": True} and route["entryPoints"] == ["harness"]
    assert f"HostSNI(`{FQDN}`)" in route["routes"][0]["match"]
    assert _obj(t, "Service", "ingress")["spec"]["ports"] == [{"name": "web", "port": 8443, "targetPort": 8443}]
    ns = _obj(t, "Namespace", "th-zz1")["metadata"]["labels"]
    assert ns["harness.teleport/expires-at"] == "1792200000"


# ---- gating + CLI ---------------------------------------------------------------------------

def test_docker_only_units_gate_out_on_k8s_with_their_name():
    assert backend_requirement(MODULES / "tpm", COMPONENTS, "k8s") == "module tpm (requires_backend: docker)"
    assert backend_requirement(MODULES / "operator_generic_oidc", COMPONENTS, "k8s") == \
        "component k8s-runner (requires_backend: docker)"
    assert backend_requirement(MODULES / "tpm", COMPONENTS, "docker") == ""
    res = gate(load_module(MODULES / "tpm"), None, None,
               backend_unit=backend_requirement(MODULES / "tpm", COMPONENTS, "k8s"), backend="k8s")
    assert res.skip and res.reason == "module tpm (requires_backend: docker) cannot run on the k8s backend"


def _render(module: str, out: Path) -> Path:
    ctx = {"cluster_id": "zz1", "fqdn": FQDN, "port": "8443", "image": "teleport-harness:test",
           "harness_domain": "example.com", "lab_domain": "lab.example.com", "out": str(out),
           "repo": "/fake/teleport"}
    render_module(MODULES / module, ctx, out, run_prebuild=False)
    (out / "meta.env").write_text(f"CLUSTER_ID=zz1\nBACKEND=k8s\nFQDN={FQDN}\nPORT=8443\nMODULES={module}\n")
    return out


def test_kube_render_cli_writes_manifests_and_plan(tmp_path, capsys):
    out = _render("tbot", tmp_path)
    assert cli_main(["kube-render", "--state-dir", str(out), "--expires-at", "1"]) == 0
    objs = list(yaml.safe_load_all((out / "kube" / "manifests.yaml").read_text()))
    assert objs[0]["kind"] == "Namespace" and objs[-1]["kind"] == "Pod"
    assert json.loads((out / "kube" / "plan.json").read_text())["namespace"] == "th-zz1"


def test_kube_render_cli_refuses_a_docker_only_module(tmp_path, capsys):
    out = _render("tpm", tmp_path)
    assert cli_main(["kube-render", "--state-dir", str(out)]) != 0
    assert "module tpm (requires_backend: docker) cannot run on the k8s backend" in capsys.readouterr().err


# ---- golden: every shipped module ---------------------------------------------------------

def _all_modules() -> list[str]:
    return sorted(p.name for p in MODULES.iterdir() if (p / "module.yaml").is_file())


def _normalize(t, state: Path) -> str:
    def scrub(o):
        if isinstance(o, dict):
            return {k: scrub(v) for k, v in o.items()}
        if isinstance(o, list):
            return [scrub(v) for v in o]
        if isinstance(o, str):
            return o.replace(str(state), "$STATE").replace(str(REPO), "$HARNESS")
        return o

    objs = []
    for o in t.objects:
        if o["kind"] == "ConfigMap":
            o = {**o, "data": sorted(o.get("data", {})), "binaryData": sorted(o.get("binaryData", {}))}
        objs.append(o)
    return yaml.safe_dump({"objects": scrub(objs), "plan": scrub(t.plan)}, sort_keys=False)


@pytest.mark.parametrize("module", _all_modules())
def test_golden_translation(module, tmp_path):
    out = _render(module, tmp_path / module)
    unit = backend_requirement(MODULES / module, COMPONENTS, "k8s")
    golden = GOLDEN / f"{module}.yaml"
    if unit:
        # A docker-only module is gated out before translation; it must not have a snapshot
        # pretending it runs.
        assert not golden.exists(), f"{module} is docker-only ({unit}) but has a golden file"
        return
    compose = yaml.safe_load((out / "docker-compose.yml").read_text())
    got = _normalize(translate(compose, cluster_id="zz1", state_dir=out, fqdn=FQDN, port=8443), out)
    if os.environ.get("HARNESS_UPDATE_GOLDEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(got)
    assert golden.is_file(), f"no snapshot for {module}: run with HARNESS_UPDATE_GOLDEN=1"
    assert got == golden.read_text(), (f"{module} translates differently from {golden.name}; "
                                       f"if intended, regenerate with HARNESS_UPDATE_GOLDEN=1")


def test_teleport_sni_matches_what_tsh_sends():
    # Observed from a real `tsh ls` through Traefik (the SNI it asked for), and
    # api/utils/cluster.go EncodeClusterName.
    from harness.kube import teleport_sni
    assert teleport_sni("p5a.lab.ethernet.fyi") == \
        "7035612e6c61622e65746865726e65742e667969.teleport.cluster.local"
