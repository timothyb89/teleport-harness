"""The verifier — the single source of truth for what each `checks:` verb means
(replaces lib/assert.sh). Each impl takes the cluster + the cached node list + the
check's args and returns a structured CheckResult; the dispatcher renders the same
`  PASS|FAIL|SKIP <msg>` / `RESULT:` text the shell contract expects AND a JSON
report. Impls are thin over the `Cluster` seam, so they're unit-testable with a fake.

Each result also carries `evidence` — the concrete proof the check relied on (the
matched log line, the node record, the command + exit status). It's shown indented in
the console, in the JSON, and in the markdown report, so a reader can see WHY a check
passed, not just that it did.

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
    evidence: list[str] = field(default_factory=list)  # short inline proofs (node/file/identity)
    excerpt: list[str] = field(default_factory=list)    # line-numbered log context → code block

    def line(self) -> str:
        return f"  {self.status:<4} {self.msg}"

    def as_dict(self) -> dict:
        return {"status": self.status, "verb": self.verb, "args": self.args,
                "msg": self.msg, "evidence": self.evidence, "excerpt": self.excerpt}


def _truncate(s: str, n: int = 240) -> str:
    s = s.rstrip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _hostname(c: Cluster, suffix: str) -> str:
    return f"{c.id}-{suffix}"


def _find_node(nodes: list[dict], hostname: str) -> dict | None:
    return next((n for n in nodes if n.get("spec", {}).get("hostname") == hostname), None)


def _node_desc(node: dict) -> str:
    spec = node.get("spec", {})
    parts = [f"hostname={spec.get('hostname', '?')}"]
    if node.get("scope"):
        parts.append(f"scope={node['scope']}")
    if spec.get("addr"):
        parts.append(f"addr={spec['addr']}")
    labels = (node.get("metadata", {}) or {}).get("labels") or {}
    if labels:
        parts.append("labels={" + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}")
    return " ".join(parts)


def _hostnames(nodes: list[dict]) -> list[str]:
    return [n.get("spec", {}).get("hostname", "?") for n in nodes]


def _excerpt(lines: list[str], match_idxs: list[int], context: int = 3,
             max_lines: int = 25, width_cap: int = 200) -> list[str]:
    """grep -C style: line-numbered context around each match, matched lines marked `>`,
    non-contiguous groups separated by `--`. Line numbers are 1-based positions in the
    container log. Long lines (audit events) are truncated — the full line is in logs/."""
    if not match_idxs:
        return []
    matchset = set(match_idxs)
    show: set[int] = set()
    for m in match_idxs:
        show.update(range(max(0, m - context), min(len(lines), m + context + 1)))
    ordered = sorted(show)
    width = len(str(ordered[-1] + 1))
    out: list[str] = []
    prev: int | None = None
    for j in ordered:
        if prev is not None and j != prev + 1:
            out.append("--")
        mark = ">" if j in matchset else " "
        out.append(f"{mark} [{str(j + 1).rjust(width)}] {_truncate(lines[j], width_cap)}")
        if len(out) >= max_lines:
            out.append("… (truncated; see logs/)")
            break
        prev = j
    return out


# --- node join outcomes -------------------------------------------------------
def _node_present(c, nodes, args):
    h = _hostname(c, args[0])
    node = _find_node(nodes, h)
    if node:
        return CheckResult(PASS, f"node {h} joined", evidence=[_node_desc(node)])
    return CheckResult(FAIL, f"node {h} did not join",
                       evidence=[f"present nodes: {', '.join(_hostnames(nodes)) or 'none'}"])


def _node_absent(c, nodes, args):
    h = _hostname(c, args[0])
    present = ", ".join(_hostnames(nodes)) or "none"
    if _find_node(nodes, h):
        return CheckResult(FAIL, f"node {h} present but expected absent (denied)")
    return CheckResult(PASS, f"node {h} absent (denied)",
                       evidence=[f"{len(nodes)} node(s) joined, none named {h}: {present}"])


def _node_scope(c, nodes, args):
    h, scope = _hostname(c, args[0]), args[1]
    node = _find_node(nodes, h)
    got = (node or {}).get("scope", "")
    if got == scope:
        return CheckResult(PASS, f"node {h} scope={scope}", evidence=[_node_desc(node)])
    return CheckResult(FAIL, f"node {h} scope='{got}' expected '{scope}'")


def _node_count(c, nodes, args):
    want, got = int(args[0]), len(nodes)
    ev = [f"{got} node(s): {', '.join(_hostnames(nodes)) or 'none'}"]
    if got == want:
        return CheckResult(PASS, f"exactly {want} node(s) joined", evidence=ev)
    return CheckResult(FAIL, f"expected {want} node(s), got {got}", evidence=ev)


def _scoped_node_count(c, nodes, args):
    scope, want = args[0], int(args[1])
    scoped = [n.get("spec", {}).get("hostname", "?") for n in nodes if n.get("scope") == scope]
    ev = [f"in scope {scope}: {', '.join(scoped) or 'none'}"]
    if len(scoped) == want:
        return CheckResult(PASS, f"exactly {want} node(s) in scope {scope}", evidence=ev)
    return CheckResult(FAIL, f"expected {want} node(s) in scope {scope}, got {len(scoped)}", evidence=ev)


# --- log / audit --------------------------------------------------------------
def _log_contains(c, nodes, args):
    suffix, pattern = args[0], " ".join(args[1:])
    cname = c.container(suffix)
    lines = c.logs(suffix).splitlines()
    rx = re.compile(pattern, re.IGNORECASE)
    idxs = [i for i, ln in enumerate(lines) if rx.search(ln)][:5]  # cap match windows
    if idxs:
        return CheckResult(PASS, f"{cname} log matches /{pattern}/", excerpt=_excerpt(lines, idxs))
    return CheckResult(SKIP, f"{cname} log has no match for /{pattern}/ yet")


def _bot_joined(c, nodes, args):
    name = args[0]
    method = args[1] if len(args) > 1 else ""
    suffix = f" via {method}" if method else ""
    lines = c.logs("auth").splitlines()
    for i, ln in enumerate(lines):
        if ("bot.join" in ln and f"bot_name:{name}" in ln and "success:true" in ln
                and (not method or f"method:{method}" in ln)):
            return CheckResult(PASS, f"bot '{name}' joined{suffix}", excerpt=_excerpt(lines, [i]))
    return CheckResult(FAIL, f"bot '{name}' did not join{suffix} (no successful bot.join event)")


# --- tbot output artifacts ----------------------------------------------------
def _output_file(c, nodes, args):
    suffix, path = args[0], args[1]
    cname = c.container(suffix)
    if c.file_nonempty(suffix, path):
        size = c.file_size(suffix, path)
        ev = [f"{path}: {size} bytes"] if size is not None else [f"{path}: present"]
        return CheckResult(PASS, f"{cname}:{path} present", evidence=ev)
    return CheckResult(FAIL, f"{cname}:{path} missing")


def _no_output_file(c, nodes, args):
    suffix, path = args[0], args[1]
    cname = c.container(suffix)
    if c.file_nonempty(suffix, path):
        return CheckResult(FAIL, f"{cname}:{path} present but expected none")
    return CheckResult(PASS, f"{cname}:{path} absent", evidence=[f"{path} not present (as expected)"])


# --- identity usability -------------------------------------------------------
def _identity_authorized(c, nodes, args):
    suffix, ident = args[0], args[1]
    auth_server = args[2] if len(args) > 2 else "auth:3025"
    argv = ["tctl", "--identity", ident, "--auth-server", auth_server, "tokens", "ls"]
    rc, out = c.exec_out(suffix, argv)
    cmd = f"$ {' '.join(argv)}  → exit {rc}"
    if rc == 0:
        first = next((ln for ln in out.splitlines() if ln.strip()), "")
        ev = [cmd] + ([_truncate(first, 120)] if first else [])
        return CheckResult(PASS, f"{c.container(suffix)} identity authenticates + is authorized", evidence=ev)
    return CheckResult(FAIL, f"{c.container(suffix)} identity could not perform an authorized action",
                       evidence=[cmd])


def _tsh_ssh(c, nodes, args):
    suffix = args[0]
    login = args[1] if len(args) > 1 else "root"
    h = _hostname(c, suffix)
    if c.tsh_ssh(suffix, login):
        return CheckResult(PASS, f"tsh ssh {login}@{h} works",
                           evidence=[f"tsh ssh {login}@{h} -- echo harness-ok → harness-ok"])
    return CheckResult(FAIL, f"tsh ssh {login}@{h} failed")


def _identity_scope(c, nodes, args):
    """Inspect a written identity's scope via `tsh status --identity`. Proves a scoped
    bot's identity is pinned to the expected scope (not just that it joined)."""
    suffix, ident, scope = args[0], args[1], args[2]
    cname = c.container(suffix)
    argv = ["tsh", "status", "--identity", ident, "--proxy", c.proxy_addr()]
    rc, out = c.exec_out(suffix, argv)
    # tsh status prints a line like:  "  Scope:              /genericoidc-test"
    line = next((ln.strip() for ln in out.splitlines() if ln.strip().lower().startswith("scope:")), "")
    if rc == 0 and line and scope in line:
        return CheckResult(PASS, f"{cname} identity is scope-pinned to {scope}", evidence=[line])
    return CheckResult(FAIL, f"{cname} identity is not scope-pinned to {scope} (exit {rc})",
                       evidence=[line or _truncate(out, 140)])


def _tsh_ssh_as(c, nodes, args):
    """Practical identity test: run `tsh ssh` from inside <suffix>'s container using its
    OWN identity file into <node>, executing `echo harness-ok`. Proves the identity
    actually grants session access to that node (for a scoped bot, within its scope)."""
    suffix, ident, node_suffix = args[0], args[1], args[2]
    login = args[3] if len(args) > 3 else "root"
    node = c.container(node_suffix)
    argv = ["tsh", "ssh", "--identity", ident, "--proxy", c.proxy_addr(),
            f"{login}@{node}", "--", "echo", "harness-ok"]
    rc, out = c.exec_out(suffix, argv)
    cmd = f"$ tsh ssh {login}@{node} (identity {ident}) → exit {rc}"
    if rc == 0 and "harness-ok" in out:
        return CheckResult(PASS, f"{c.container(suffix)} identity can tsh ssh {login}@{node}",
                           evidence=[cmd, "stdout: harness-ok"])
    return CheckResult(FAIL, f"{c.container(suffix)} identity could NOT tsh ssh {login}@{node}",
                       evidence=[cmd, _truncate(out, 160)])


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
    "identity_scope": _identity_scope,
    "tsh_ssh": _tsh_ssh,
    "tsh_ssh_as": _tsh_ssh_as,
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


def verify(cluster: Cluster, checks: list[Check], module_dir: Path | None = None,
           nodes: list[dict] | None = None) -> list[CheckResult]:
    """Run every declarative check, then a module's optional Python escape hatch.
    `nodes` may be passed in (the caller often already fetched them for the report)."""
    if nodes is None:
        nodes = cluster.get_nodes()
    results = [run_check(cluster, nodes, chk) for chk in checks]
    if module_dir is not None:
        hatch = _load_escape_hatch(module_dir)
        if hatch is not None:
            extra = hatch(cluster, nodes) or []
            results.extend(extra)
    return results


def render(results: list[CheckResult]) -> tuple[str, bool]:
    """Return (human text incl. evidence sub-lines + RESULT line, passed?)."""
    passed = not any(r.status == FAIL for r in results)
    lines: list[str] = []
    for r in results:
        lines.append(r.line())
        for ev in r.evidence:
            lines.append(f"       ↳ {_truncate(ev, 200)}")
        for ex in r.excerpt:
            lines.append(f"       {ex}")
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


def node_summary(nodes: list[dict]) -> list[dict]:
    """Compact node inventory for the report (captured at verify time)."""
    out = []
    for n in nodes:
        spec = n.get("spec", {})
        out.append({
            "hostname": spec.get("hostname", "?"),
            "scope": n.get("scope", ""),
            "addr": spec.get("addr", ""),
            "labels": (n.get("metadata", {}) or {}).get("labels") or {},
        })
    return out
