"""The cluster seam the verifier talks to.

Every way an assert touches a live cluster goes through a `Cluster` method, so
tests inject a `FakeCluster` (see tests/) instead of running anything. Which real
implementation a cluster gets is its BACKEND (recorded in meta.env by lib/cluster.sh, see
lib/backend.sh); `make_cluster` picks it. `DockerCluster` is the docker one — thin
shell-outs that mirror the exact commands the old lib/assert.sh used, keeping the
untestable surface tiny.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

# Where the k8s-runner component deploys the operator and the namespace its CRs live in.
# Fixed by convention (the component owns both sides), so the k8s_* verbs don't need to
# carry a namespace argument. `KUBE_CONTAINER` is the nodename suffix of the k3s service.
KUBE_NAMESPACE = "teleport"
KUBE_CONTAINER = "k3s"

# Separator in a scope-qualified name (`<scope>::<name>`, scopes.QualifiedName). A
# resource_* verb writes one as `kind//scope::name`; get_resource keys off this to pick
# tctl's two-argument form, the only one that can carry a scope.
SCOPE_SEP = "::"


class Cluster:
    """Interface used by harness/verify.py. Methods here are the test seam."""

    def __init__(self, cluster_id: str):
        self.id = cluster_id
        # Set by the verifier for the module currently being checked. Only the observation
        # verbs use it, to find a host actor's per-module records in the state dir (a plan
        # runs several modules against one cluster, so the filename has to be scoped).
        self.module = ""
        self.state_dir = None

    def container(self, suffix: str) -> str:
        return f"{self.id}-{suffix}"

    def get_nodes(self) -> list[dict]:  # pragma: no cover - overridden in tests
        raise NotImplementedError

    def get_resource(self, kind: str, name: str) -> dict | None:  # pragma: no cover
        """A single resource by kind+name, via `tctl get <kind>/<name>` — the seam
        the resource_present/resource_field verbs use to inspect cluster state (e.g.
        what a terraform apply created). None if absent/unreadable. Overridden in tests."""
        raise NotImplementedError

    def state_file(self, relpath: str) -> str | None:  # pragma: no cover
        """Read a file under the cluster's state dir (state/<id>/) as text, or None if
        absent/unreadable. The seam `agent_result` uses to read the agent's
        agent-result.json / transcript, which the workbench wrote to a state-dir bind
        mount. Overridden in tests."""
        raise NotImplementedError

    def clear_state_file(self, relpath: str) -> None:  # pragma: no cover
        """Remove a state-dir file a service writes (absent is fine) — e.g. a stale agent
        result, which would otherwise mask a run that silently wrote nothing."""
        raise NotImplementedError

    def kube_get(self, kind: str, name: str,
                 namespace: str = KUBE_NAMESPACE) -> dict | None:  # pragma: no cover
        """A single object from the in-cluster k8s API (the k8s-runner component's k3s),
        via `kubectl get <kind> <name> -n <ns> -o json`. None if absent/unreadable — the
        seam the k8s_* verbs use to inspect what the API server actually stored (e.g.
        whether a CR was accepted at all) and what the operator wrote back to its status.
        Overridden in tests."""
        raise NotImplementedError

    def logs(self, suffix: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def audit_events(self) -> list[dict]:
        """Structured audit events emitted to the JSON file backend (see auth.yaml.j2).
        Default empty so checks/fakes that don't provide events degrade gracefully
        (e.g. bot_joined falls back to scraping the text log)."""
        return []

    def exec_argv(self, suffix: str, argv: list[str], workdir: str | None = None) -> list[str]:  # pragma: no cover
        """The host command line that runs `argv` inside service `<id>-<suffix>`, without
        running it. The one place a backend spells out "exec into a service": exec_out runs
        it, and the agent's workbench tool (harness/agent_mcp.py) is handed it as a fixed
        prefix, so the model never chooses where its commands run."""
        raise NotImplementedError

    def exec_out(self, suffix: str, argv: list[str]) -> tuple[int, str]:  # pragma: no cover
        raise NotImplementedError

    def exec_rc(self, suffix: str, argv: list[str]) -> int:
        return self.exec_out(suffix, argv)[0]

    def file_nonempty(self, suffix: str, path: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def file_size(self, suffix: str, path: str) -> int | None:
        """Byte size of a file, or None if absent/unreadable (evidence for output_file)."""
        rc, out = self.exec_out(suffix, ["sh", "-c", f"wc -c < '{path}' 2>/dev/null"])
        if rc != 0:
            return None
        try:
            return int(out.strip())
        except ValueError:
            return None

    def read_json(self, suffix: str, path: str):
        """Parse a JSON file from inside a container, or None if absent/unreadable.

        The seam the observation_* verbs use. Defined in terms of exec_out so it works
        against a FakeCluster with no extra plumbing.
        """
        rc, out = self.exec_out(suffix, ["cat", path])
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    def tsh_ssh(self, host_suffix: str, login: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def proxy_addr(self) -> str:  # pragma: no cover
        """`<fqdn>:<port>` for tsh --proxy from inside a cluster container."""
        raise NotImplementedError

    def restart_auth(self, timeout: float = 150.0) -> bool:  # pragma: no cover
        """Restart the auth container and block until it serves /healthz again, i.e. the
        NEW teleport process finished init. The seam a check uses to exercise a
        startup-only code path (e.g. re-running `--apply-on-startup`): apply-on-startup
        runs during init BEFORE /healthz answers, so a successful poll means the re-apply
        completed. Returns False on restart failure or timeout. Overridden in tests."""
        raise NotImplementedError


class DockerCluster(Cluster):
    """Real cluster backed by docker. `state_dir` (state/<id>/) is read for meta
    when a check needs the image/proxy (only tsh_ssh does today)."""

    def __init__(self, cluster_id: str, state_dir: Path | None = None, module: str = ""):
        super().__init__(cluster_id)
        self.state_dir = state_dir
        self.module = module

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True)

    def get_nodes(self) -> list[dict]:
        # mirrors: docker exec <id>-auth tctl get nodes --format json  (|| '[]')
        cp = self._run(self.exec_argv("auth", ["tctl", "get", "nodes", "--format", "json"]))
        if cp.returncode != 0 or not cp.stdout.strip():
            return []
        try:
            data = json.loads(cp.stdout)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def get_resource(self, kind: str, name: str) -> dict | None:
        # mirrors: docker exec <id>-auth tctl get <kind>/<name> --format json
        # (--format json emits a JSON array even for a single resource).
        #
        # SCOPED resources are addressed differently: tctl parses the one-arg form by
        # splitting on '/' and DISCARDING empty fields (services.ParseRef ->
        # strings.FieldsFunc), so a scope's leading '/' is eaten and the ref comes back
        # scope-less — which a scoped kind rejects outright ("requires a scope-qualified
        # name"). Scope qualification is only expressible in the TWO-arg form, so a name
        # written as `<scope>::<name>` is passed as its own positional argument.
        ref = [kind, name] if SCOPE_SEP in name else [f"{kind}/{name}"]
        cp = self._run(self.exec_argv("auth", ["tctl", "get", *ref, "--format", "json"]))
        if cp.returncode != 0 or not cp.stdout.strip():
            return None
        try:
            data = json.loads(cp.stdout)
        except json.JSONDecodeError:
            return None
        docs = data if isinstance(data, list) else [data]
        # metadata.name is the BARE name, so compare against the unqualified part.
        bare = name.rpartition(SCOPE_SEP)[2]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("metadata", {}).get("name") == bare:
                return doc
        # tctl already filtered to this name; fall back to the sole element if present.
        return docs[0] if docs and isinstance(docs[0], dict) else None

    def state_file(self, relpath: str) -> str | None:
        if not self.state_dir:
            return None
        try:
            return (self.state_dir / relpath).read_text()
        except (FileNotFoundError, OSError):
            return None

    def clear_state_file(self, relpath: str) -> None:
        if self.state_dir:
            (Path(self.state_dir) / relpath).unlink(missing_ok=True)

    def kube_get(self, kind: str, name: str, namespace: str = KUBE_NAMESPACE) -> dict | None:
        # kubectl ships inside the k3s image, and the k3s container already holds the
        # admin kubeconfig at its default path — so no kubeconfig plumbing is needed to
        # inspect the cluster, exactly like `tctl` runs inside the auth container.
        cp = self._run([
            "docker", "exec", self.container(KUBE_CONTAINER),
            "kubectl", "get", kind, name, "-n", namespace, "-o", "json",
        ])
        if cp.returncode != 0 or not cp.stdout.strip():
            return None
        try:
            doc = json.loads(cp.stdout)
        except json.JSONDecodeError:
            return None
        return doc if isinstance(doc, dict) else None

    def logs(self, suffix: str) -> str:
        # mirrors: docker logs <id>-<suffix> 2>&1  (capture first — no pipefail trap)
        cp = self._run(["docker", "logs", self.container(suffix)])
        return (cp.stdout or "") + (cp.stderr or "")

    def audit_events(self) -> list[dict]:
        # cat every file the file audit backend wrote (NDJSON, one event per line);
        # parse defensively so non-JSON lines / index files are skipped.
        cp = self._run(self.exec_argv("auth", [
            "sh", "-c", "find /var/lib/teleport/audit/events -type f -exec cat {} + 2>/dev/null",
        ]))
        events: list[dict] = []
        for line in (cp.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
        return events

    def exec_argv(self, suffix: str, argv: list[str], workdir: str | None = None) -> list[str]:
        return ["docker", "exec", *(["-w", workdir] if workdir else []), self.container(suffix), *argv]

    def exec_out(self, suffix: str, argv: list[str]) -> tuple[int, str]:
        cp = self._run(self.exec_argv(suffix, argv))
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")

    def file_nonempty(self, suffix: str, path: str) -> bool:
        # mirrors: docker exec <c> test -s <path>
        return self.exec_rc(suffix, ["test", "-s", path]) == 0

    def _meta(self, key: str) -> str:
        return read_meta(self.state_dir).get(key, "")

    def tsh_ssh(self, host_suffix: str, login: str) -> bool:
        # mirrors lib/admin.sh cluster_tsh: run the cluster image with the admin bot
        # identity against the proxy, over the cluster's internal network.
        image, fqdn, port = self._meta("IMAGE"), self._meta("FQDN"), self._meta("PORT")
        if not image:
            return False
        host = self.container(host_suffix)
        cp = self._run([
            "docker", "run", "--rm",
            "--network", f"teleport-harness-{self.id}_internal",
            "-v", f"harness-admin-{self.id}:/id:ro",
            image, "tsh", "--proxy", f"{fqdn}:{port}", "--identity", "/id/identity",
            "ssh", f"{login}@{host}", "--", "echo", "harness-ok",
        ])
        return cp.returncode == 0 and "harness-ok" in (cp.stdout or "")

    def proxy_addr(self) -> str:
        return f"{self._meta('FQDN')}:{self._meta('PORT')}"

    def restart_auth(self, timeout: float = 150.0) -> bool:
        # `docker restart` keeps the container (writable layer + named volumes: backend
        # data + already-applied resources survive), only re-running the entrypoint —
        # which re-runs `--apply-on-startup`. Then poll /healthz from inside the container
        # until the fresh teleport answers (exec fails transiently while it's restarting;
        # treated as not-ready). /healthz answering ⇒ init done ⇒ re-apply completed.
        cp = self._run(["docker", "restart", self.container("auth")])
        if cp.returncode != 0:
            return False
        return self._wait_auth_healthz(time.monotonic() + timeout)

    def _wait_auth_healthz(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            rc, _ = self.exec_out("auth", ["sh", "-c",
                                           "curl -fsS http://localhost:3000/healthz >/dev/null 2>&1"])
            if rc == 0:
                return True
            time.sleep(3)
        return False


class KubeCluster(DockerCluster):
    """A cluster on the k8s backend: namespace th-<id>, one pod per compose service (or per
    netns group), the service's process in a container named after the service. Where each
    suffix runs comes from the plan harness/kube.py wrote (state/<id>/kube/plan.json).

    It inherits DockerCluster's tctl/audit/file logic, which is all phrased as exec_out
    into a service, and replaces only the backend-specific parts: exec, logs, and reading
    files the services write into the state dir (on this backend they stay in the pod's
    volume, so they are read where they are)."""

    def __init__(self, cluster_id: str, state_dir: Path | None = None, module: str = ""):
        super().__init__(cluster_id, state_dir=state_dir, module=module)
        self._plan: dict | None = None

    @property
    def plan(self) -> dict:
        if self._plan is None:
            if not self.state_dir:
                raise ValueError(f"cluster {self.id}: the k8s backend needs the state dir")
            self._plan = json.loads((Path(self.state_dir) / "kube" / "plan.json").read_text())
        return self._plan

    def _kubectl(self, *args: str) -> list[str]:
        # The connection this cluster was created with (meta.env), not the environment's:
        # the argv must reach THIS cluster even when handed elsewhere (the agent's MCP tool).
        from .kube_apply import Kubectl
        meta = read_meta(self.state_dir)
        if not meta.get("KUBE_CONTEXT"):
            raise ValueError(f"cluster {self.id}: meta.env records no KUBE_CONTEXT")
        return Kubectl(context=meta["KUBE_CONTEXT"], kubeconfig=meta.get("KUBECONFIG", "")).argv(*args)

    def _where(self, suffix: str) -> tuple[str, str]:
        svc = self.plan["services"].get(suffix)
        if not svc:
            raise ValueError(f"cluster {self.id}: no service {suffix!r} "
                             f"(have: {', '.join(sorted(self.plan['services']))})")
        return svc["pod"], svc["container"]

    def exec_argv(self, suffix: str, argv: list[str], workdir: str | None = None) -> list[str]:
        pod, container = self._where(suffix)
        if workdir:
            # kubectl exec has no -w; cd first, then exec whatever follows (the agent's MCP
            # tool appends its own argv to this prefix).
            argv = ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", workdir, *argv]
        return self._kubectl("exec", "-n", self.plan["namespace"], pod, "-c", container, "--", *argv)

    def logs(self, suffix: str) -> str:
        # `docker logs` spans container restarts; a restarted k8s container's earlier run
        # is only under --previous, so put it first, as docker would.
        pod, container = self._where(suffix)
        ns = self.plan["namespace"]
        prev = self._run(self._kubectl("logs", "-n", ns, pod, "-c", container, "--previous"))
        cur = self._run(self._kubectl("logs", "-n", ns, pod, "-c", container))
        return ((prev.stdout if prev.returncode == 0 else "")
                + (cur.stdout or "") + (cur.stderr if cur.returncode != 0 else ""))

    def _in_pod(self, relpath: str) -> tuple[str, str] | None:
        """(service, path inside it) for a state-dir path under a read-write mount, which on
        this backend lives in a pod volume (kube.py's pullbacks); None for a host-side file."""
        rel = relpath.strip("/")
        for pb in self.plan.get("pullbacks", []):
            src = pb["source"].strip("/")
            if rel == src or rel.startswith(src + "/"):
                return pb["container"], pb["path"].rstrip("/") + rel[len(src):]
        return None

    def state_file(self, relpath: str) -> str | None:
        where = self._in_pod(relpath)
        if where is None:
            return super().state_file(relpath)
        rc, out = self.exec_out(where[0], ["cat", where[1]])
        return out if rc == 0 else None

    def clear_state_file(self, relpath: str) -> None:
        where = self._in_pod(relpath)
        if where is None:
            return super().clear_state_file(relpath)
        self.exec_out(where[0], ["rm", "-f", where[1]])

    def kube_get(self, kind: str, name: str, namespace: str = KUBE_NAMESPACE) -> dict | None:
        return None   # the k8s-runner component is docker-only (requires_backend)

    def tsh_ssh(self, host_suffix: str, login: str) -> bool:
        return False  # needs admin access on this backend (Phase 5); no module uses it yet

    def _restart_count(self, pod: str, container: str) -> int | None:
        cp = self._run(self._kubectl("get", "pod", "-n", self.plan["namespace"], pod, "-o", "json"))
        if cp.returncode != 0:
            return None
        try:
            statuses = json.loads(cp.stdout).get("status", {}).get("containerStatuses") or []
        except json.JSONDecodeError:
            return None
        return next((c.get("restartCount", 0) for c in statuses if c.get("name") == container), None)

    def restart_auth(self, timeout: float = 150.0) -> bool:
        # The pod's restartPolicy is Always (kube.py), so ending the container makes the
        # kubelet start it again in place: same pod, same IP, and the PVCs behind auth's data
        # dir and the apply-on-startup files survive, the way a `docker restart` keeps its
        # volumes. Unlike docker, the container's own filesystem is NOT kept; the entrypoint
        # re-runs bootstrap on every start anyway, so that only resets /tmp.
        #
        # Kill teleport rather than PID 1: the entrypoint shell is PID 1, and the kernel drops
        # signals to a namespace's init that it has no handler for. With teleport gone, the
        # entrypoint's `wait` fails and the container exits.
        pod, container = self._where("auth")
        before = self._restart_count(pod, container)
        if before is None:
            return False
        self.exec_out("auth", ["sh", "-c",
                               'for p in /proc/[0-9]*; do [ "$(cat "$p/comm" 2>/dev/null)" = teleport ] '
                               '&& kill "${p#/proc/}"; done'])
        deadline = time.monotonic() + timeout
        while True:
            now = self._restart_count(pod, container)
            if now is not None and now > before:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(2)
        return self._wait_auth_healthz(deadline)


def read_meta(state_dir: Path | None) -> dict[str, str]:
    """state/<id>/meta.env as a dict ({} if absent). Written once by lib/cluster.sh."""
    if not state_dir:
        return {}
    f = Path(state_dir) / "meta.env"
    if not f.is_file():
        return {}
    meta: dict[str, str] = {}
    for line in f.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            meta[key] = value
    return meta


# BACKEND (from meta.env) -> implementation. A cluster from before backends existed has no
# BACKEND recorded, and was docker by construction.
BACKENDS: dict[str, type[Cluster]] = {"docker": DockerCluster, "k8s": KubeCluster}


def make_cluster(cluster_id: str, state_dir: Path | None = None, module: str = "") -> Cluster:
    """The Cluster for an existing cluster, chosen by the backend that created it."""
    backend = read_meta(state_dir).get("BACKEND") or "docker"
    impl = BACKENDS.get(backend)
    if impl is None:
        raise ValueError(f"cluster {cluster_id!r}: backend {backend!r} has no Cluster "
                         f"implementation (have: {', '.join(sorted(BACKENDS))})")
    return impl(cluster_id, state_dir=state_dir, module=module)
