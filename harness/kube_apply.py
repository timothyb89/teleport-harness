"""Bring a translated cluster up on the k8s backend (docs/remote-kubernetes.md §3, Phase 4).

harness/kube.py is pure: it turns the rendered compose into objects plus a plan. This is
the other half, and the only part that talks to the API server: create the namespace and
everything that isn't a pod, fill the Secrets the plan names (from the host, never from
the manifests), then create the pods wave by wave. Before each pod is created it waits for
what compose's depends_on asked for and pins every dotted alias it needs (the auth FQDN,
oidc.<lab>) to the owning pod's IP with hostAliases. That IP only exists once the owner has
started, which is why the translator made every alias user wait for it.

All kubectl calls go through `Kubectl`, so tests drive this with a fake.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

import yaml

FIELD_MANAGER = "teleport-harness"
SYSTEM_NS = "harness-system"
# A first pull of a fresh teleport image is a few hundred MB; through a home uplink from
# Beams it is the registry's pull, not ours, but the node still has to fetch it.
DEFAULT_TIMEOUT = 600.0
# Waiting reasons that will never resolve by themselves.
FATAL_WAITING = {"InvalidImageName", "ErrImageNeverPull", "CreateContainerConfigError"}


class ApplyError(RuntimeError):
    """The cluster could not be brought up; the message says which pod and why."""


class Kubectl:
    """kubectl pinned to one cluster: an explicit kubeconfig (optional) and context
    (required). Defaults come from HARNESS_KUBECONFIG / HARNESS_KUBE_CONTEXT, which the shell
    resolves from the target for a new cluster and from meta.env for an existing one
    (lib/backend/k8s.sh). Never the ambient current context: that is how a harness command
    lands on whatever cluster the user's kubeconfig happens to point at."""

    def __init__(self, context: str | None = None, kubeconfig: str | None = None):
        self.context = context if context is not None else os.environ.get("HARNESS_KUBE_CONTEXT", "")
        self.kubeconfig = (kubeconfig if kubeconfig is not None
                           else os.environ.get("HARNESS_KUBECONFIG", ""))

    def argv(self, *args: str) -> list[str]:
        if not self.context:
            raise ApplyError("no kube context (HARNESS_KUBE_CONTEXT); refusing to use the "
                             "kubeconfig's current one")
        return ["kubectl", *(["--kubeconfig", self.kubeconfig] if self.kubeconfig else []),
                "--context", self.context, *args]

    def run(self, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(self.argv(*args), input=input, capture_output=True, text=True)

    def get_json(self, *args: str) -> dict | None:
        cp = self.run("get", *args, "-o", "json")
        if cp.returncode != 0:
            return None
        try:
            return json.loads(cp.stdout)
        except json.JSONDecodeError:
            return None

    def apply(self, objects: list[dict]) -> None:
        # Server-side: client-side apply stores a last-applied annotation, capped at 256 KiB,
        # and a ConfigMap may be up to ~900 KiB (kube.CONFIGMAP_BUDGET).
        cp = self.run("apply", "--server-side", f"--field-manager={FIELD_MANAGER}",
                      "--force-conflicts", "-f", "-", input=yaml.safe_dump_all(objects))
        if cp.returncode != 0:
            names = ", ".join(f"{o['kind']}/{o['metadata']['name']}" for o in objects[:5])
            raise ApplyError(f"kubectl apply failed for {names}{'…' if len(objects) > 5 else ''}:\n"
                             f"{cp.stderr.strip()}")


def load(state_dir: Path) -> tuple[list[dict], dict]:
    kdir = Path(state_dir) / "kube"
    objects = [o for o in yaml.safe_load_all((kdir / "manifests.yaml").read_text()) if o]
    plan = json.loads((kdir / "plan.json").read_text())
    return objects, plan


# ---- secrets ------------------------------------------------------------------------------

def secret_objects(plan: dict, kc: Kubectl) -> list[dict]:
    """The Secrets the plan names, with their data. A `from` entry is copied from another
    namespace (the wildcard cert, from harness-system); a `file` entry is read from the host
    (the license). Either way the content travels to the API server on stdin, never argv."""
    ns = plan["namespace"]
    out = []
    for s in plan.get("secrets", []):
        if "from" in s:
            src = kc.get_json("secret", s["name"], "-n", s["from"])
            if not src:
                raise ApplyError(f"secret {s['from']}/{s['name']} not found "
                                 f"(infra/k8s/import-cert.sh creates the wildcard cert)")
            data, stype = src.get("data") or {}, src.get("type", "Opaque")
        else:
            path = Path(s["file"])
            if not path.is_file():
                raise ApplyError(f"secret {s['name']}: {path} is not a file")
            data = {s["key"]: base64.b64encode(path.read_bytes()).decode()}
            stype = "Opaque"
        out.append({"apiVersion": "v1", "kind": "Secret", "type": stype,
                    "metadata": {"name": s["name"], "namespace": ns,
                                 "labels": {"app.kubernetes.io/part-of": "teleport-harness"}},
                    "data": data})
    return out


# ---- pods ---------------------------------------------------------------------------------

def _waiting_reasons(pod: dict) -> list[str]:
    st = pod.get("status") or {}
    out = []
    for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
        w = (cs.get("state") or {}).get("waiting")
        if w and w.get("reason"):
            out.append(f"{cs['name']}: {w['reason']}" + (f" ({w['message']})" if w.get("message") else ""))
    return out


def _ready(pod: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (pod.get("status") or {}).get("conditions") or [])


def condition_met(pod: dict, condition: str) -> bool:
    """Whether a pod satisfies a translated depends_on condition. Raises ApplyError when it
    never can (the pod failed, or is stuck on something that won't fix itself)."""
    name = pod["metadata"]["name"]
    phase = (pod.get("status") or {}).get("phase")
    for r in _waiting_reasons(pod):
        if r.split(":", 1)[1].strip().split(" ", 1)[0] in FATAL_WAITING:
            raise ApplyError(f"pod {name} cannot start: {r}")
    if condition == "succeeded":
        if phase == "Failed":
            raise ApplyError(f"pod {name} failed, and others need it to complete successfully")
        return phase == "Succeeded"
    if phase == "Failed":
        raise ApplyError(f"pod {name} failed before becoming {condition}")
    if condition == "started":
        # Running = every container was created and at least one is running; with the
        # sidecar layout that also means the netns owner passed its startupProbe.
        return phase in ("Running", "Succeeded")
    if condition == "ready":
        return _ready(pod)
    raise ApplyError(f"unknown condition {condition!r}")


def wait_for(kc: Kubectl, ns: str, name: str, condition: str, timeout: float,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> dict:
    deadline = clock() + timeout
    pod: dict | None = None
    while True:
        pod = kc.get_json("pod", name, "-n", ns)
        if pod and condition_met(pod, condition):
            return pod
        if clock() >= deadline:
            why = "; ".join(_waiting_reasons(pod)) if pod else "pod not found"
            phase = (pod or {}).get("status", {}).get("phase", "?")
            raise ApplyError(f"timed out after {int(timeout)}s waiting for pod {name} to be "
                             f"{condition} (phase {phase}{'; ' + why if why else ''})")
        sleep(2)


def with_host_aliases(pod: dict, aliases: dict[str, str], ips: dict[str, str]) -> dict:
    """pod with every `alias -> owner pod` pinned to the owner's IP (merged with the
    127.0.0.1 entries the translator wrote for aliases the pod owns itself)."""
    if not aliases:
        return pod
    by_ip: dict[str, set[str]] = {}
    for entry in pod["spec"].get("hostAliases", []):
        by_ip.setdefault(entry["ip"], set()).update(entry["hostnames"])
    for alias, owner in aliases.items():
        ip = ips.get(owner)
        if not ip:
            raise ApplyError(f"pod {pod['metadata']['name']}: alias {alias} needs {owner}'s IP, "
                             f"which it does not have yet")
        by_ip.setdefault(ip, set()).add(alias)
    spec = {**pod["spec"], "hostAliases": [{"ip": ip, "hostnames": sorted(h)}
                                           for ip, h in sorted(by_ip.items())]}
    return {**pod, "spec": spec}


# ---- the whole thing ----------------------------------------------------------------------

def apply_cluster(state_dir: Path, kc: Kubectl, *, timeout: float = DEFAULT_TIMEOUT,
                  log: Callable[[str], None] = print,
                  sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> None:
    objects, plan = load(state_dir)
    ns = plan["namespace"]
    if kc.get_json("namespace", ns) is not None:
        # Ids are random, but state dirs are per-client: another client (Beams vs laptop)
        # may already own this namespace.
        raise ApplyError(f"namespace {ns} already exists — another client's cluster? "
                         f"(pick another --id, or tear that one down)")

    pods = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Pod"}
    rest = [o for o in objects if o["kind"] != "Pod"]
    namespace = [o for o in rest if o["kind"] == "Namespace"]
    log(f"namespace {ns}: {len(rest) - len(namespace)} objects, "
        f"{len(plan.get('secrets', []))} secrets, {len(pods)} pods in {len(plan['order'])} waves")
    kc.apply(namespace)
    secrets = secret_objects(plan, kc)
    if secrets:
        kc.apply(secrets)
    kc.apply([o for o in rest if o["kind"] != "Namespace"])

    ips: dict[str, str] = {}
    for i, wave in enumerate(plan["order"], 1):
        for name in wave:
            spec = plan["pods"].get(name, {})
            for need in spec.get("needs", []):
                if need["pod"] in ips and need["condition"] == "started":
                    continue
                got = wait_for(kc, ns, need["pod"], need["condition"], timeout, sleep, clock)
                ip = (got.get("status") or {}).get("podIP")
                if ip:
                    ips[need["pod"]] = ip
        log(f"wave {i}/{len(plan['order'])}: {', '.join(wave)}")
        kc.apply([with_host_aliases(pods[n], plan["pods"].get(n, {}).get("host_aliases", {}), ips)
                  for n in wave])
