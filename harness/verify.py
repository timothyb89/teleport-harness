"""The verifier — the single source of truth for what each `checks:` verb means
(replaces lib/assert.sh). Each impl takes the cluster + the cached node list + the
check's args and returns a structured CheckResult; the dispatcher renders the same
`  PASS|FAIL|SKIP <msg>` / `RESULT:` text the shell contract expects AND a JSON
report. Impls are thin over the `Cluster` seam, so they're unit-testable with a fake.

Behavior mirrors the old bash asserts exactly:
  - only FAIL fails the run; SKIP is neutral (a not-yet-satisfied soft check).
  - log_contains is case-insensitive and SKIPs (not FAILs) when there's no match.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .cluster import Cluster
from .checks import REGISTRY
from .models import Check

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class CheckResult:
    status: str
    msg: str
    verb: str = ""
    args: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"  {self.status:<4} {self.msg}"

    def as_dict(self) -> dict:
        return {"status": self.status, "verb": self.verb, "args": self.args, "msg": self.msg}


def _hostname(c: Cluster, suffix: str) -> str:
    return f"{c.id}-{suffix}"


def _find_node(nodes: list[dict], hostname: str) -> dict | None:
    return next((n for n in nodes if n.get("spec", {}).get("hostname") == hostname), None)


# --- node join outcomes -------------------------------------------------------
def _node_present(c, nodes, args):
    h = _hostname(c, args[0])
    if _find_node(nodes, h):
        return CheckResult(PASS, f"node {h} joined")
    return CheckResult(FAIL, f"node {h} did not join")


def _node_absent(c, nodes, args):
    h = _hostname(c, args[0])
    if _find_node(nodes, h):
        return CheckResult(FAIL, f"node {h} present but expected absent (denied)")
    return CheckResult(PASS, f"node {h} absent (denied)")


def _node_scope(c, nodes, args):
    h, scope = _hostname(c, args[0]), args[1]
    node = _find_node(nodes, h)
    got = (node or {}).get("scope", "")
    if got == scope:
        return CheckResult(PASS, f"node {h} scope={scope}")
    return CheckResult(FAIL, f"node {h} scope='{got}' expected '{scope}'")


def _node_count(c, nodes, args):
    want = int(args[0])
    got = len(nodes)
    if got == want:
        return CheckResult(PASS, f"exactly {want} node(s) joined")
    return CheckResult(FAIL, f"expected {want} node(s), got {got}")


def _scoped_node_count(c, nodes, args):
    scope, want = args[0], int(args[1])
    got = sum(1 for n in nodes if n.get("scope") == scope)
    if got == want:
        return CheckResult(PASS, f"exactly {want} node(s) in scope {scope}")
    return CheckResult(FAIL, f"expected {want} node(s) in scope {scope}, got {got}")


# --- log / audit --------------------------------------------------------------
def _log_contains(c, nodes, args):
    suffix, pattern = args[0], " ".join(args[1:])
    logs = c.logs(suffix)
    cname = c.container(suffix)
    if re.search(pattern, logs, re.IGNORECASE):
        return CheckResult(PASS, f"{cname} log matches /{pattern}/")
    return CheckResult(SKIP, f"{cname} log has no match for /{pattern}/ yet")


def _bot_joined(c, nodes, args):
    name = args[0]
    method = args[1] if len(args) > 1 else ""
    logs = c.logs("auth")
    # a successful bot.join audit event for this bot (+ optional method), per-line —
    # same predicate the old awk one-pass used.
    for ln in logs.splitlines():
        if ("bot.join" in ln and f"bot_name:{name}" in ln and "success:true" in ln
                and (not method or f"method:{method}" in ln)):
            suffix = f" via {method}" if method else ""
            return CheckResult(PASS, f"bot '{name}' joined{suffix}")
    suffix = f" via {method}" if method else ""
    return CheckResult(FAIL, f"bot '{name}' did not join{suffix} (no successful bot.join event)")


# --- tbot output artifacts ----------------------------------------------------
def _output_file(c, nodes, args):
    suffix, path = args[0], args[1]
    if c.file_nonempty(suffix, path):
        return CheckResult(PASS, f"{c.container(suffix)}:{path} present")
    return CheckResult(FAIL, f"{c.container(suffix)}:{path} missing")


def _no_output_file(c, nodes, args):
    suffix, path = args[0], args[1]
    if c.file_nonempty(suffix, path):
        return CheckResult(FAIL, f"{c.container(suffix)}:{path} present but expected none")
    return CheckResult(PASS, f"{c.container(suffix)}:{path} absent")


# --- identity usability -------------------------------------------------------
def _identity_authorized(c, nodes, args):
    suffix, ident = args[0], args[1]
    auth_server = args[2] if len(args) > 2 else "auth:3025"
    rc = c.exec_rc(suffix, ["tctl", "--identity", ident, "--auth-server", auth_server,
                            "tokens", "ls"])
    if rc == 0:
        return CheckResult(PASS, f"{c.container(suffix)} identity authenticates + is authorized")
    return CheckResult(FAIL, f"{c.container(suffix)} identity could not perform an authorized action")


def _tsh_ssh(c, nodes, args):
    suffix = args[0]
    login = args[1] if len(args) > 1 else "root"
    h = _hostname(c, suffix)
    if c.tsh_ssh(suffix, login):
        return CheckResult(PASS, f"tsh ssh {login}@{h} works")
    return CheckResult(FAIL, f"tsh ssh {login}@{h} failed")


# verb -> impl. Kept in lockstep with harness/checks.REGISTRY (test enforces it).
Impl = Callable[[Cluster, list[dict], list[str]], CheckResult]
IMPLS: dict[str, Impl] = {
    "node_present": _node_present,
    "node_absent": _node_absent,
    "node_scope": _node_scope,
    "node_count": _node_count,
    "scoped_node_count": _scoped_node_count,
    "log_contains": _log_contains,
    "bot_joined": _bot_joined,
    "output_file": _output_file,
    "no_output_file": _no_output_file,
    "identity_authorized": _identity_authorized,
    "tsh_ssh": _tsh_ssh,
}


def run_check(cluster: Cluster, nodes: list[dict], chk: Check) -> CheckResult:
    impl = IMPLS.get(chk.verb)
    if impl is None:
        return CheckResult(FAIL, f"unknown check verb '{chk.verb}'", chk.verb, chk.args)
    res = impl(cluster, nodes, chk.args)
    res.verb, res.args = chk.verb, chk.args
    return res


def _load_escape_hatch(module_dir: Path):
    """A module may add arbitrary custom checks in checks.py exposing
    `def checks(cluster, nodes) -> list[CheckResult]`. Returns the callable or None."""
    f = module_dir / "checks.py"
    if not f.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"harness_module_{module_dir.name}", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return getattr(mod, "checks", None)


def verify(cluster: Cluster, checks: list[Check], module_dir: Path | None = None) -> list[CheckResult]:
    """Run every declarative check, then a module's optional Python escape hatch."""
    nodes = cluster.get_nodes()
    results = [run_check(cluster, nodes, chk) for chk in checks]
    if module_dir is not None:
        hatch = _load_escape_hatch(module_dir)
        if hatch is not None:
            extra = hatch(cluster, nodes) or []
            results.extend(extra)
    return results


def render(results: list[CheckResult]) -> tuple[str, bool]:
    """Return (human text incl. RESULT line, passed?). passed == no FAIL."""
    passed = not any(r.status == FAIL for r in results)
    lines = [r.line() for r in results]
    lines.append(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return "\n".join(lines), passed


def verb_impls_match_registry() -> list[str]:
    """Drift guard used by tests: every registry verb has an impl and vice versa."""
    problems = []
    for v in REGISTRY:
        if v not in IMPLS:
            problems.append(f"registry verb '{v}' has no impl in verify.IMPLS")
    for v in IMPLS:
        if v not in REGISTRY:
            problems.append(f"verify.IMPLS verb '{v}' missing from checks.REGISTRY")
    return problems
