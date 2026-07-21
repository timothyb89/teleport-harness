"""The docker seam the verifier talks to.

Every way an assert touches a live cluster goes through a `Cluster` method, so
tests inject a `FakeCluster` (see tests/) instead of running docker. `DockerCluster`
is the real impl — thin shell-outs that mirror the exact commands the old
lib/assert.sh used (so behavior is unchanged), keeping the untestable surface tiny.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


class Cluster:
    """Interface used by harness/verify.py. Methods here are the test seam."""

    def __init__(self, cluster_id: str):
        self.id = cluster_id

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

    def logs(self, suffix: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def audit_events(self) -> list[dict]:
        """Structured audit events emitted to the JSON file backend (see auth.yaml.j2).
        Default empty so checks/fakes that don't provide events degrade gracefully
        (e.g. bot_joined falls back to scraping the text log)."""
        return []

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

    def __init__(self, cluster_id: str, state_dir: Path | None = None):
        super().__init__(cluster_id)
        self.state_dir = state_dir

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True)

    def get_nodes(self) -> list[dict]:
        # mirrors: docker exec <id>-auth tctl get nodes --format json  (|| '[]')
        cp = self._run(
            ["docker", "exec", self.container("auth"),
             "tctl", "get", "nodes", "--format", "json"]
        )
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
        cp = self._run(
            ["docker", "exec", self.container("auth"),
             "tctl", "get", f"{kind}/{name}", "--format", "json"]
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            return None
        try:
            data = json.loads(cp.stdout)
        except json.JSONDecodeError:
            return None
        docs = data if isinstance(data, list) else [data]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("metadata", {}).get("name") == name:
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

    def logs(self, suffix: str) -> str:
        # mirrors: docker logs <id>-<suffix> 2>&1  (capture first — no pipefail trap)
        cp = self._run(["docker", "logs", self.container(suffix)])
        return (cp.stdout or "") + (cp.stderr or "")

    def audit_events(self) -> list[dict]:
        # cat every file the file audit backend wrote (NDJSON, one event per line);
        # parse defensively so non-JSON lines / index files are skipped.
        cp = self._run([
            "docker", "exec", self.container("auth"),
            "sh", "-c", "find /var/lib/teleport/audit/events -type f -exec cat {} + 2>/dev/null",
        ])
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

    def exec_out(self, suffix: str, argv: list[str]) -> tuple[int, str]:
        cp = self._run(["docker", "exec", self.container(suffix), *argv])
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")

    def file_nonempty(self, suffix: str, path: str) -> bool:
        # mirrors: docker exec <c> test -s <path>
        return self.exec_rc(suffix, ["test", "-s", path]) == 0

    def _meta(self, key: str) -> str:
        if not self.state_dir:
            return ""
        f = self.state_dir / "meta.env"
        if not f.is_file():
            return ""
        for line in f.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        return ""

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
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, _ = self.exec_out("auth", ["sh", "-c",
                                           "curl -fsS http://localhost:3000/healthz >/dev/null 2>&1"])
            if rc == 0:
                return True
            time.sleep(3)
        return False
