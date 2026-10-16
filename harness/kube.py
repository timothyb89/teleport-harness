"""Translate a rendered cluster into Kubernetes objects for the k8s backend.

Input is what the docker backend runs: state/<id>/docker-compose.yml, rendered by
harness/render.py, plus the files it bind-mounts. Compose stays the module authoring
format; this is the only place that knows how compose maps onto Kubernetes
(docs/remote-kubernetes.md §3). It is PURE — it reads files and returns objects and a plan;
it never talks to a cluster. Applying them in order (and filling in the pod IPs that
`hostAliases` need) is the executor's job.

Translation is deliberately strict. A compose key, mount form or network shape without a
rule below is an error naming the service and the key — a module using something new fails
here, at render time, rather than half-working on a cluster.

Mapping in brief:
  project                    -> Namespace th-<id> (+ expiry label for the janitor)
  service                    -> a bare Pod named by its container_name, one container named
                                by the service; bare because compose semantics are "one
                                named container", and a Deployment would rename it
  name resolution            -> two portless headless Services per service (<svc> and
                                <id>-<svc>), resolving even before the pod is Ready, as docker
                                DNS does; dotted network aliases (<x>.lab.<domain>) -> hostAliases
                                to the owning pod's IP, filled in by the executor
  depends_on                 -> plan["order"] (apply waves) + per-pod `needs`
  healthcheck                -> readinessProbe
  network_mode: service:X    -> the dependent joins X's pod; X becomes a native sidecar whose
                                startupProbe (from its healthcheck) gates the dependent, which
                                is exactly compose's service_healthy ordering inside one netns
  named volume               -> PVC; the external harness-certs volume -> Secret wildcard-cert
  small read-only bind       -> ConfigMap
  large read-only bind       -> an artifact image (published by the backend), copied into an
                                emptyDir by an init container
  read-write bind            -> PVC, seeded from the source on first start; if the source is
                                in the state dir, the plan records it for pull-back
"""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

# Every pod references harness-built images by this name; the node's registries.yaml maps it
# to the in-cluster registry (infra/k3s/registries.yaml, docs/remote-kubernetes.md §4).
REGISTRY = "registry.harness:5000"
# Images the harness builds itself (the teleport image, side images like the oidc-server)
# are named teleport-harness*. Everything else is pulled from upstream unchanged.
LOCAL_IMAGE_PREFIX = "teleport-harness"
# Copies seeds and artifacts into volumes; multi-arch, so it runs natively on any node.
HELPER_IMAGE = "busybox:1.37"
# Stay well under the 1 MiB object limit (the ConfigMap's own metadata counts too).
CONFIGMAP_BUDGET = 900 * 1024

TOP_LEVEL_KEYS = {"name", "services", "networks", "volumes"}
SERVICE_KEYS = {
    "image", "platform", "container_name", "entrypoint", "command", "environment",
    "volumes", "networks", "depends_on", "healthcheck", "restart", "hostname", "user",
    "cap_add", "network_mode", "privileged", "tmpfs",
}
NETWORK_KEYS = {"external", "name"}
HEALTHCHECK_KEYS = {"test", "interval", "timeout", "retries", "start_period"}
# `platform` is accepted and DROPPED: a node pulls its own architecture from a multi-arch
# image (what an arm64 pin on a public image asks for on the docker side), and the
# harness's own images are single-platform amd64 manifests, which pull as-is anyway.

# Bind mounts that carry a credential become Secrets, filled by the executor from the
# source path the plan records — never embedded in the manifests. The license mount is
# the base scaffold's contract (harness/templates/base.compose.yml.j2).
SECRET_TARGETS = {"/etc/teleport/license.pem": ("license", "license.pem")}
# External named volumes that exist on the k8s side as Secrets in the namespace (copied
# in from harness-system by the executor; infra/k8s/import-cert.sh writes the keys the
# templates mount: wildcard.crt / wildcard.key).
EXTERNAL_VOLUME_SECRETS = {"harness-certs": "wildcard-cert"}

# compose depends_on condition -> what the executor waits for on the dependency's pod.
CONDITIONS = {
    "service_started": "started",
    "service_healthy": "ready",
    "service_completed_successfully": "succeeded",
}
RESTART = {"no": "Never", "on-failure": "OnFailure", "always": "Always",
           "unless-stopped": "Always"}

_DNS_LABEL = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
_CM_KEY_BAD = re.compile(r"[^-._a-zA-Z0-9]")


class TranslateError(ValueError):
    """A compose feature with no translation rule, or a shape the k8s backend can't run."""


@dataclass
class Translation:
    objects: list[dict]           # apply-ready manifests, in a sensible apply order
    plan: dict                    # what the executor needs beyond the manifests
    configmap_sources: dict[str, str] = field(default_factory=dict)  # cm name -> source


def translate(compose: dict, *, cluster_id: str, state_dir: Path, fqdn: str, port: int,
              expires_at: int | None = None) -> Translation:
    """Translate one rendered cluster. Raises TranslateError on anything without a rule."""
    return _Translator(compose, cluster_id, Path(state_dir), fqdn, int(port), expires_at).run()


# ---- helpers ------------------------------------------------------------------------------

def _dns_label(name: str, what: str) -> str:
    if not _DNS_LABEL.match(name):
        raise TranslateError(f"{what} {name!r} is not a valid DNS label "
                             f"(lowercase alphanumerics and '-', starting with a letter, ≤63)")
    return name


def _slug(src: str, prefix: str) -> str:
    """A stable, readable, unique object name for a mount source. Callers pass the source
    as _Translator._stable() gives it, so names don't change with the state dir's path."""
    tail = re.sub(r"[^a-z0-9]+", "-", src.lower().rstrip("/").rsplit("/", 2)[-1]).strip("-")
    digest = hashlib.sha1(src.encode()).hexdigest()[:8]
    return f"{prefix}-{tail[:40].strip('-') or 'root'}-{digest}"


def _duration_s(value, default: int) -> int:
    """compose duration ("5s", "1m30s", "500ms") -> whole seconds, at least 1."""
    if value is None:
        return default
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(us|ms|s|m|h)", str(value)):
        total += float(num) * {"us": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}[unit]
    return max(1, round(total))


def _argv(value, svc: str, key: str) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list):
        return [str(a) for a in value]
    raise TranslateError(f"service {svc}: {key} must be a string or a list")


def _local_image(image: str) -> bool:
    first = image.split("/", 1)[0]
    has_registry = "/" in image and ("." in first or ":" in first or first == "localhost")
    return not has_registry and image.startswith(LOCAL_IMAGE_PREFIX)


def _files_under(src: Path) -> list[tuple[str, Path]]:
    if src.is_file():
        return [(src.name, src)]
    return sorted((str(p.relative_to(src)), p) for p in src.rglob("*") if p.is_file())


def _size(src: Path) -> int:
    return sum(p.stat().st_size for _, p in _files_under(src))


def _no_dollar(value, where: str):
    """compose interpolates `$VAR` when it LOADS the file and Kubernetes expands `$(VAR)` in
    command/args/env, so a `$` means something different on each side. Rendered composes
    contain none today (scripts live in mounted files); refuse one rather than guess."""
    if isinstance(value, str) and "$" in value:
        raise TranslateError(f"{where}: contains '$' ({value[:60]!r}); compose and Kubernetes "
                             f"interpolate differently — move it into a mounted script")
    if isinstance(value, list):
        for v in value:
            _no_dollar(v, where)
    if isinstance(value, dict):
        for k, v in value.items():
            _no_dollar(v, f"{where}.{k}")


# ---- the translator -----------------------------------------------------------------------

class _Translator:
    def __init__(self, compose: dict, cluster_id: str, state_dir: Path, fqdn: str, port: int,
                 expires_at: int | None):
        self.c = compose or {}
        self.id = cluster_id
        self.state_dir = state_dir
        self.fqdn = fqdn
        self.port = port
        self.expires_at = expires_at
        self.ns = _dns_label(f"th-{cluster_id}", "namespace")
        self.services: dict[str, dict] = self.c.get("services") or {}
        self.volumes: dict[str, dict] = self.c.get("volumes") or {}
        self.configmaps: dict[str, dict] = {}       # name -> object
        self.cm_sources: dict[str, str] = {}
        self.pvcs: dict[str, dict] = {}
        self.secrets: dict[str, dict] = {}          # name -> plan entry
        self.artifacts: dict[str, dict] = {}        # image -> plan entry
        self.pullbacks: list[dict] = []
        self.local_images: set[str] = set()

    # -- validation ----------------------------------------------------------------------

    def _validate(self):
        extra = set(self.c) - TOP_LEVEL_KEYS
        if extra:
            raise TranslateError(f"unsupported top-level compose keys: {sorted(extra)}")
        for net, spec in (self.c.get("networks") or {}).items():
            bad = set(spec or {}) - NETWORK_KEYS
            if bad:
                # Isolation (internal: true, separate subnets) has no rule yet: one namespace
                # is one flat network. NetworkPolicy is the planned route if a module needs it.
                raise TranslateError(f"network {net}: unsupported keys {sorted(bad)} "
                                     f"(the k8s backend runs one flat network per cluster)")
        for name, s in self.services.items():
            bad = set(s) - SERVICE_KEYS
            if bad:
                raise TranslateError(f"service {name}: unsupported compose keys {sorted(bad)}")
            _dns_label(name, "service name")
            cn = s.get("container_name")
            if not cn:
                raise TranslateError(f"service {name}: no container_name (the harness names "
                                     f"every container <id>-<service>)")
            _dns_label(cn, f"service {name}: container_name")
            if cn != f"{self.id}-{name}":
                # Checks address a service by the suffix after "<id>-" (Cluster.container),
                # and on this backend that suffix is looked up as the service name.
                raise TranslateError(f"service {name}: container_name {cn!r} is not "
                                     f"{self.id}-{name}")
            for key in ("environment", "command", "entrypoint", "healthcheck"):
                _no_dollar(s.get(key), f"service {name}: {key}")
            hc = s.get("healthcheck")
            if hc and set(hc) - HEALTHCHECK_KEYS:
                raise TranslateError(f"service {name}: unsupported healthcheck keys "
                                     f"{sorted(set(hc) - HEALTHCHECK_KEYS)}")
            r = s.get("restart", "no")
            if r not in RESTART:
                raise TranslateError(f"service {name}: unsupported restart {r!r}")

    # -- pods: which services share one -------------------------------------------------

    def _group(self) -> dict[str, list[str]]:
        """owner service -> [owner, *services that joined its netns]."""
        groups: dict[str, list[str]] = {n: [n] for n in self.services}
        for name, s in self.services.items():
            nm = s.get("network_mode")
            if nm is None:
                continue
            if not (isinstance(nm, str) and nm.startswith("service:")):
                raise TranslateError(f"service {name}: network_mode {nm!r} (only service:<name>)")
            owner = nm.split(":", 1)[1]
            if owner not in self.services:
                raise TranslateError(f"service {name}: network_mode names unknown service {owner}")
            if self.services[owner].get("network_mode"):
                raise TranslateError(f"service {name}: network_mode chains through {owner}")
            if s.get("networks"):
                raise TranslateError(f"service {name}: network_mode and networks together")
            groups[owner].append(name)
            del groups[name]
        return groups

    # -- volumes ----------------------------------------------------------------------------

    def _configmap(self, src: Path, name: str) -> tuple[dict, list[dict]]:
        """A ConfigMap holding src (a file or a tree); returns it and the volume `items`."""
        data, binary, items, used = {}, {}, [], set()
        for rel, p in _files_under(src):
            key = _CM_KEY_BAD.sub("_", rel)
            while key in used:
                key = "_" + key
            used.add(key)
            raw = p.read_bytes()
            try:
                data[key] = raw.decode("utf-8")
            except UnicodeDecodeError:
                import base64
                binary[key] = base64.b64encode(raw).decode()
            item = {"key": key, "path": rel}
            if p.stat().st_mode & 0o111:
                item["mode"] = 0o755
            items.append(item)
        obj = {"apiVersion": "v1", "kind": "ConfigMap",
               "metadata": {"name": name, "namespace": self.ns, "labels": self._labels()}}
        if data:
            obj["data"] = data
        if binary:
            obj["binaryData"] = binary
        return obj, items

    def _pvc(self, name: str):
        self.pvcs.setdefault(name, {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": self.ns, "labels": self._labels()},
            "spec": {"accessModes": ["ReadWriteOnce"],
                     "resources": {"requests": {"storage": "1Gi"}}},
        })

    def _mount(self, svc: str, spec: str, pod: dict):
        """Translate one compose volume entry into pod volumes + a container mount (+ any
        init containers it needs). Returns the container's volumeMount."""
        parts = spec.split(":")
        if len(parts) not in (2, 3):
            raise TranslateError(f"service {svc}: volume {spec!r} is not src:dst[:mode]")
        src, dst = parts[0], parts[1]
        mode = parts[2] if len(parts) == 3 else "rw"
        if mode not in ("ro", "rw"):
            raise TranslateError(f"service {svc}: volume {spec!r}: mode {mode!r} (ro|rw only)")
        ro = mode == "ro"

        if not src.startswith(("/", ".")):                                   # named volume
            if src not in self.volumes:
                raise TranslateError(f"service {svc}: volume {src!r} is not declared at top level")
            vspec = self.volumes[src] or {}
            if vspec.get("external"):
                secret = EXTERNAL_VOLUME_SECRETS.get(vspec.get("name", src))
                if not secret:
                    raise TranslateError(f"service {svc}: external volume {src!r} has no k8s "
                                         f"equivalent (known: {sorted(EXTERNAL_VOLUME_SECRETS)})")
                self.secrets.setdefault(secret, {"name": secret, "from": "harness-system"})
                vol = {"name": f"secret-{secret}", "secret": {"secretName": secret}}
                return self._add_volume(pod, vol, {"mountPath": dst, "readOnly": True})
            name = _dns_label(f"vol-{src}", f"service {svc}: volume")
            self._pvc(name)
            vol = {"name": name, "persistentVolumeClaim": {"claimName": name}}
            return self._add_volume(pod, vol, {"mountPath": dst, "readOnly": ro})

        if src.startswith("/dev/"):
            raise TranslateError(f"service {svc}: device bind {src} (docker-only)")

        if dst in SECRET_TARGETS:                                            # credential
            name, key = SECRET_TARGETS[dst]
            self.secrets.setdefault(name, {"name": name, "file": src, "key": key})
            vol = {"name": f"secret-{name}", "secret": {"secretName": name}}
            return self._add_volume(pod, vol, {"mountPath": dst, "subPath": key, "readOnly": True})

        path = Path(src)
        if not ro:                                                           # rw bind -> PVC
            name = _slug(self._stable(src), "bind")
            self._pvc(name)
            vol = {"name": name, "persistentVolumeClaim": {"claimName": name}}
            mount = self._add_volume(pod, vol, {"mountPath": dst})
            if path.is_dir() and any(path.iterdir()):
                # Seed once: a PVC outlives container restarts, and a restart must not
                # overwrite what the service wrote (bound_keypair_apply_on_startup rewrites
                # these files on purpose, then restarts auth to see them re-applied).
                cm_name = _slug(self._stable(src), "seed")
                seed_vol = self._ro_volume(src, cm_name, svc, pod)
                pod["spec"].setdefault("initContainers", []).append({
                    "name": f"seed-{len(pod['spec'].get('initContainers', []))}",
                    "image": HELPER_IMAGE,
                    # A ConfigMap volume's root also holds the kubelet's `..data` /
                    # `..<timestamp>` bookkeeping; the files themselves are under ..data.
                    "command": ["sh", "-c", 'if [ -z "$(ls -A /dst)" ]; then cp -R /src/..data/. /dst/; fi'],
                    "volumeMounts": [{"name": seed_vol, "mountPath": "/src", "readOnly": True},
                                     {"name": name, "mountPath": "/dst"}],
                })
            if src.startswith(str(self.state_dir) + "/"):
                self.pullbacks.append({"source": str(path.relative_to(self.state_dir)),
                                       "pod": pod["metadata"]["name"], "container": svc,
                                       "path": dst})
            return mount

        vol_name = self._ro_volume(src, _slug(self._stable(src), "cm"), svc, pod)          # ro bind
        mount = {"name": vol_name, "mountPath": dst, "readOnly": True}
        if path.is_file():
            mount["subPath"] = path.name     # the item's path, not its (sanitized) key
        return mount

    def _ro_volume(self, src: str, cm_name: str, svc: str, pod: dict) -> str:
        """A pod volume carrying src read-only: a ConfigMap if it is present and small, else
        an artifact image copied into an emptyDir. Returns the pod volume's name. A source
        that doesn't exist at translate time is produced by a prebuild (tf-plugins) or lives
        outside the harness (a docs tree) — either way an artifact the backend publishes."""
        path = Path(src)
        if path.exists() and _size(path) <= CONFIGMAP_BUDGET:
            if cm_name not in self.configmaps:
                obj, items = self._configmap(path, cm_name)
                self.configmaps[cm_name] = obj
                self.cm_sources[cm_name] = src
                obj["_items"] = items
            items = self.configmaps[cm_name]["_items"]
            vol = {"name": cm_name, "configMap": {"name": cm_name, "items": items}}
            self._add_volume(pod, vol, None)
            return cm_name

        art = _slug(self._stable(src), "art")
        image = f"{REGISTRY}/harness-artifact:{self.id}-{art}"
        self.artifacts.setdefault(image, {"image": image, "source": src,
                                          "kind": "file" if path.is_file() else "dir"})
        if not any(v["name"] == art for v in pod["spec"].get("volumes", [])):
            self._add_volume(pod, {"name": art, "emptyDir": {}}, None)
            pod["spec"].setdefault("initContainers", []).append({
                "name": f"artifact-{len(pod['spec'].get('initContainers', []))}",
                "image": image,
                "command": ["sh", "-c", "cp -R /artifact/. /dst/"],
                "volumeMounts": [{"name": art, "mountPath": "/dst"}],
            })
        return art

    def _stable(self, src: str) -> str:
        """src relative to the state dir when it lives there (object names derived from it
        must not depend on where the state dir happens to be), else unchanged."""
        if src.startswith(str(self.state_dir) + "/"):
            return "$STATE/" + src[len(str(self.state_dir)) + 1:]
        return src

    @staticmethod
    def _add_volume(pod: dict, vol: dict, mount: dict | None):
        vols = pod["spec"].setdefault("volumes", [])
        if not any(v["name"] == vol["name"] for v in vols):
            vols.append(vol)
        return {"name": vol["name"], **mount} if mount is not None else None

    # -- containers ---------------------------------------------------------------------

    def _probe(self, svc: str, hc: dict) -> dict:
        test = hc.get("test")
        if isinstance(test, str):
            cmd = ["sh", "-c", test]
        elif isinstance(test, list) and test and test[0] == "CMD-SHELL":
            cmd = ["sh", "-c", " ".join(test[1:])]
        elif isinstance(test, list) and test and test[0] == "CMD":
            cmd = [str(a) for a in test[1:]]
        else:
            raise TranslateError(f"service {svc}: healthcheck test {test!r}")
        # start_period has no direct equivalent: compose ignores failures during it but still
        # turns healthy on the first success, which is what probing from t=0 does anyway.
        return {"exec": {"command": cmd},
                "periodSeconds": _duration_s(hc.get("interval"), 30),
                "timeoutSeconds": _duration_s(hc.get("timeout"), 30),
                "failureThreshold": int(hc.get("retries", 3))}

    def _container(self, svc: str, pod: dict) -> dict:
        s = self.services[svc]
        image = s["image"]
        if _local_image(image):
            self.local_images.add(image)
            image = f"{REGISTRY}/{image}"
        c: dict = {"name": svc, "image": image}
        if "entrypoint" in s:
            c["command"] = _argv(s["entrypoint"], svc, "entrypoint")
        if "command" in s:
            c["args"] = _argv(s["command"], svc, "command")
        env = s.get("environment")
        if env:
            pairs = env.items() if isinstance(env, dict) else (
                (e.split("=", 1) + [None])[:2] for e in env)
            c["env"] = []
            for k, v in pairs:
                if v is None:
                    raise TranslateError(f"service {svc}: environment {k} has no value "
                                         f"(would be taken from the host's environment)")
                c["env"].append({"name": str(k),
                                 "value": str(v).lower() if isinstance(v, bool) else str(v)})
        mounts = [self._mount(svc, v, pod) for v in s.get("volumes") or []]
        for i, t in enumerate(s.get("tmpfs") or []):
            vol = {"name": f"tmpfs-{svc}-{i}", "emptyDir": {"medium": "Memory"}}
            mounts.append(self._add_volume(pod, vol, {"mountPath": t}))
        if mounts:
            c["volumeMounts"] = mounts
        if s.get("healthcheck"):
            c["readinessProbe"] = self._probe(svc, s["healthcheck"])
        sc: dict = {}
        if s.get("privileged"):
            sc["privileged"] = True
        if s.get("cap_add"):
            sc["capabilities"] = {"add": [str(x) for x in s["cap_add"]]}
        if "user" in s:
            uid, _, gid = str(s["user"]).partition(":")
            if not uid.isdigit() or (gid and not gid.isdigit()):
                raise TranslateError(f"service {svc}: user {s['user']!r} (numeric uid[:gid] only)")
            sc["runAsUser"] = int(uid)
            if gid:
                sc["runAsGroup"] = int(gid)
        if sc:
            c["securityContext"] = sc
        return c

    def _labels(self, **extra) -> dict:
        return {"app.kubernetes.io/part-of": "teleport-harness",
                "harness.teleport/cluster-id": self.id, **extra}

    # -- the whole thing ------------------------------------------------------------------

    def run(self) -> Translation:
        self._validate()
        groups = self._group()
        svc_pod = {m: self.services[owner]["container_name"]
                   for owner, members in groups.items() for m in members}

        pods: dict[str, dict] = {}
        for owner, members in sorted(groups.items()):
            pname = self.services[owner]["container_name"]
            o = self.services[owner]
            # auth is restarted in place by checks (Cluster.restart_auth), which on k8s means
            # killing its process and letting the kubelet restart the container — that only
            # happens under restartPolicy Always.
            policy = "Always" if owner == "auth" else RESTART[o.get("restart", "no")]
            pod = {"apiVersion": "v1", "kind": "Pod",
                   "metadata": {"name": pname, "namespace": self.ns,
                                "labels": self._labels(**{"harness.teleport/pod": pname})},
                   "spec": {"restartPolicy": policy,
                            # compose injects no per-service env; k8s would add <SVC>_PORT=…
                            # for every Service in the namespace.
                            "enableServiceLinks": False,
                            # test workloads get no credentials to the cluster running them
                            "automountServiceAccountToken": False,
                            "terminationGracePeriodSeconds": 10}}
            hostnames = {self.services[m]["hostname"] for m in members if "hostname" in self.services[m]}
            if len(hostnames) > 1:
                raise TranslateError(f"pod {pname}: members set different hostnames {sorted(hostnames)}")
            if hostnames:
                pod["spec"]["hostname"] = _dns_label(hostnames.pop(), f"pod {pname}: hostname")
            containers = []
            for m in members:
                containers.append(self._container(m, pod))
            if len(members) > 1:
                # The netns owner runs as a native sidecar (an init container that keeps
                # running); its startupProbe must pass before the others start — compose's
                # service_healthy, inside one pod.
                side = containers[0]
                side["restartPolicy"] = "Always"
                if "readinessProbe" in side:
                    side["startupProbe"] = side.pop("readinessProbe")
                pod["spec"].setdefault("initContainers", []).append(side)
                containers = containers[1:]
            pod["spec"]["containers"] = containers
            pods[pname] = pod

        # depends_on -> needs on other pods (a dependency inside the same pod is the sidecar
        # ordering above).
        needs: dict[str, dict[str, str]] = {p: {} for p in pods}
        for name, s in self.services.items():
            dd = s.get("depends_on") or {}
            if isinstance(dd, list):
                dd = {d: {"condition": "service_started"} for d in dd}
            for dep, spec in dd.items():
                if dep not in self.services:
                    raise TranslateError(f"service {name}: depends_on unknown service {dep}")
                cond = (spec or {}).get("condition", "service_started")
                if cond not in CONDITIONS:
                    raise TranslateError(f"service {name}: depends_on condition {cond!r}")
                me, them = svc_pod[name], svc_pod[dep]
                if me == them:
                    continue
                prev = needs[me].get(them)
                # keep the strongest wait if two members of one pod depend on the same pod
                rank = ["started", "ready", "succeeded"]
                new = CONDITIONS[cond]
                if prev is None or rank.index(new) > rank.index(prev):
                    needs[me][them] = new

        # Dotted network aliases resolve publicly to 127.0.0.1 (the *.lab wildcard), so every
        # other pod needs a hostAlias to the owner's IP. The owner has to have an IP first,
        # so each pod also waits for every alias owner to have STARTED — except owners that
        # (transitively) wait on this pod themselves, which would be a cycle; such a pod is
        # one of the owner's own dependencies and does not get the alias.
        owners: dict[str, str] = {}
        for name, s in self.services.items():
            for net, cfg in (s.get("networks") or {}).items() if isinstance(s.get("networks"), dict) else []:
                for a in (cfg or {}).get("aliases", []) or []:
                    if "." not in a:
                        raise TranslateError(f"service {name}: alias {a!r} (only dotted "
                                             f"<x>.<lab domain> aliases are supported)")
                    prev = owners.setdefault(a, svc_pod[name])
                    if prev != svc_pod[name]:
                        raise TranslateError(f"alias {a} is claimed by both {prev} and {svc_pod[name]}")

        def waits_on(a: str, b: str) -> bool:      # does pod a (transitively) wait on pod b?
            seen, stack = set(), [a]
            while stack:
                x = stack.pop()
                for y in needs[x]:
                    if y == b:
                        return True
                    if y not in seen:
                        seen.add(y)
                        stack.append(y)
            return False

        host_aliases: dict[str, dict[str, str]] = {p: {} for p in pods}
        for alias, owner_pod in sorted(owners.items()):
            for p in pods:
                if p == owner_pod:
                    host_aliases[p][alias] = "127.0.0.1"
                elif not waits_on(owner_pod, p):
                    host_aliases[p][alias] = owner_pod
                    needs[p].setdefault(owner_pod, "started")
        for p, aliases in host_aliases.items():
            local = sorted(a for a, t in aliases.items() if t == "127.0.0.1")
            if local:
                pods[p]["spec"]["hostAliases"] = [{"ip": "127.0.0.1", "hostnames": local}]

        order = self._layers(needs)

        # Services: two portless headless names per compose service, both selecting its pod.
        services, seen = [], set()
        for name in sorted(self.services):
            for sname in (name, self.services[name]["container_name"]):
                if sname in seen:
                    raise TranslateError(f"Service name {sname!r} would be created twice")
                seen.add(sname)
                services.append({
                    "apiVersion": "v1", "kind": "Service",
                    "metadata": {"name": sname, "namespace": self.ns, "labels": self._labels()},
                    "spec": {"clusterIP": "None", "publishNotReadyAddresses": True,
                             "selector": {"harness.teleport/pod": svc_pod[name]}},
                })
        if "auth" not in self.services:
            raise TranslateError("no auth service (the base scaffold always renders one)")
        ingress_svc = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "ingress", "namespace": self.ns, "labels": self._labels()},
            "spec": {"selector": {"harness.teleport/pod": svc_pod["auth"]},
                     "ports": [{"name": "web", "port": self.port, "targetPort": self.port}]},
        }
        route = {
            "apiVersion": "traefik.io/v1alpha1", "kind": "IngressRouteTCP",
            "metadata": {"name": "auth", "namespace": self.ns, "labels": self._labels()},
            "spec": {"entryPoints": ["harness"],
                     # the proxy, and anything below it (scoped apps' derived addresses)
                     "routes": [{"match": f"HostSNI(`{self.fqdn}`) || "
                                          f"HostSNIRegexp(`^.+\\.{re.escape(self.fqdn)}$`)",
                                 "services": [{"name": "ingress", "port": self.port}]}],
                     "tls": {"passthrough": True}},
        }

        ns_labels = self._labels(**{"pod-security.kubernetes.io/enforce": "privileged"})
        if self.expires_at is not None:
            ns_labels["harness.teleport/expires-at"] = str(int(self.expires_at))
        namespace = {"apiVersion": "v1", "kind": "Namespace",
                     "metadata": {"name": self.ns, "labels": ns_labels}}

        for cm in self.configmaps.values():
            cm.pop("_items", None)

        objects = ([namespace]
                   + [self.configmaps[k] for k in sorted(self.configmaps)]
                   + [self.pvcs[k] for k in sorted(self.pvcs)]
                   + services + [ingress_svc, route]
                   + [pods[p] for layer in order for p in layer])
        plan = {
            "namespace": self.ns,
            "order": order,
            "pods": {p: {"needs": [{"pod": d, "condition": c} for d, c in sorted(needs[p].items())],
                         "host_aliases": {a: t for a, t in sorted(host_aliases[p].items())
                                          if t != "127.0.0.1"}}
                     for p in sorted(pods)},
            # Cluster seam: a service suffix -> where its process runs.
            "services": {s: {"pod": svc_pod[s], "container": s} for s in sorted(self.services)},
            "secrets": [self.secrets[k] for k in sorted(self.secrets)],
            "artifacts": [self.artifacts[k] for k in sorted(self.artifacts)],
            "pullbacks": self.pullbacks,
            "local_images": sorted(self.local_images),
        }
        return Translation(objects=objects, plan=plan, configmap_sources=dict(self.cm_sources))

    @staticmethod
    def _layers(needs: dict[str, dict[str, str]]) -> list[list[str]]:
        """Apply waves: each pod lands in the first wave after everything it needs."""
        remaining = {p: set(d) for p, d in needs.items()}
        layers: list[list[str]] = []
        done: set[str] = set()
        while remaining:
            ready = sorted(p for p, d in remaining.items() if d <= done)
            if not ready:
                raise TranslateError(f"dependency cycle among pods {sorted(remaining)}")
            layers.append(ready)
            done.update(ready)
            for p in ready:
                del remaining[p]
        return layers
