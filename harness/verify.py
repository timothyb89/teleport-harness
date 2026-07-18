"""The verifier — the single source of truth for what each `checks:` verb means
(replaces lib/assert.sh). Each impl takes the cluster + the cached node list + the
check's args and returns a structured CheckResult; the dispatcher renders the same
`  PASS|FAIL|SKIP <msg>` / `RESULT:` text the shell contract expects AND a JSON
report. Impls are thin over the `Cluster` seam, so they're unit-testable with a fake.

Evidence is a first-class `ProofItem` (Foundation A): a check no longer welds its
proof inline — it references one or more shared, run-level proof items (the matched
audit/log window, the node record, the command + its output, a file). Decoupling the
proof from the assertion lets several checks cite ONE proof, preserves the FULL
(untruncated) content for review, and gives each proof a stable id the markdown report
turns into a linkable heading. `collect_proofs` hoists every check's proofs into a
deduped registry for the report.

Behavior mirrors the old bash asserts exactly:
  - only FAIL fails the run; SKIP is neutral (a not-yet-satisfied soft check).
  - log_contains is case-insensitive and SKIPs (not FAILs) when there's no match.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .cluster import Cluster
from .checks import REGISTRY
from .models import Check

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class ProofItem:
    """A concrete piece of evidence a check relied on, decoupled from the check.

    `content` is kept in FULL (never truncated) so audit events / log windows can be
    reviewed in their entirety. `id` is a stable content hash so identical proofs from
    different checks collapse to one (enabling "N checks against one proof") and the
    report can mint a deterministic anchor. `source` is an optional bundle-relative
    link to the artifact that produced it (a per-service log, a rendered resource)."""

    kind: str            # log-excerpt | audit-event | node-record | command | file | text
    title: str
    content: str = ""    # FULL, untruncated
    lang: str = ""       # markdown code-fence hint: "json" or "" (plain)
    source: str = ""     # optional bundle-relative link (logs/<svc>.log, rendered/…)

    @property
    def id(self) -> str:
        h = hashlib.sha1(f"{self.kind}\0{self.title}\0{self.content}".encode()).hexdigest()[:10]
        return f"{self.kind}-{h}"

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "content": self.content, "lang": self.lang, "source": self.source}


@dataclass
class CheckResult:
    status: str
    msg: str
    verb: str = ""
    args: list[str] = field(default_factory=list)
    proofs: list[ProofItem] = field(default_factory=list)  # proof items this check cites
    assertions: list[str] = field(default_factory=list)     # the individual conditions asserted
    # (e.g. audit-event `field = value` pairs) — published by the verb, shown under the proof.

    def line(self) -> str:
        return f"  {self.status:<4} {self.msg}"

    def as_dict(self) -> dict:
        return {"status": self.status, "verb": self.verb, "args": self.args,
                "msg": self.msg, "proof_refs": [p.id for p in self.proofs],
                "assertions": self.assertions}


def collect_proofs(results: list[CheckResult]) -> list[ProofItem]:
    """Hoist every check's proofs into a deduped, run-level registry (first-seen wins).
    Identical proofs (same content hash) referenced by multiple checks collapse to one."""
    reg: dict[str, ProofItem] = {}
    for r in results:
        for p in r.proofs:
            reg.setdefault(p.id, p)
    return list(reg.values())


def _truncate(s: str, n: int = 240) -> str:
    """Console-display truncation ONLY. Proof `content` is always stored in full."""
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
             max_lines: int = 60) -> list[str]:
    """grep -C style: line-numbered context around each match, matched lines marked `>`,
    non-contiguous groups separated by `--`. Line numbers are 1-based positions in the
    container log. Lines are kept at FULL width (the whole point of a proof); only the
    number of context lines is bounded, and the full log is linked as the proof source."""
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
        out.append(f"{mark} [{str(j + 1).rjust(width)}] {lines[j].rstrip()}")
        if len(out) >= max_lines:
            out.append("… (truncated; see the linked full log)")
            break
        prev = j
    return out


def _log_proof(cname: str, suffix: str, title: str, excerpt: list[str]) -> ProofItem:
    return ProofItem("log-excerpt", title, "\n".join(excerpt), source=f"logs/{suffix}.log")


# --- node join outcomes -------------------------------------------------------
def _node_present(c, nodes, args):
    h = _hostname(c, args[0])
    node = _find_node(nodes, h)
    if node:
        return CheckResult(PASS, f"node {h} joined",
                           proofs=[ProofItem("node-record", f"node {h} (tctl get nodes)", _node_desc(node))])
    return CheckResult(FAIL, f"node {h} did not join",
                       proofs=[ProofItem("text", f"nodes present when {h} expected",
                                         f"present nodes: {', '.join(_hostnames(nodes)) or 'none'}")])


def _node_absent(c, nodes, args):
    h = _hostname(c, args[0])
    present = ", ".join(_hostnames(nodes)) or "none"
    if _find_node(nodes, h):
        return CheckResult(FAIL, f"node {h} present but expected absent (denied)")
    return CheckResult(PASS, f"node {h} absent (denied)",
                       proofs=[ProofItem("text", f"node {h} correctly absent",
                                         f"{len(nodes)} node(s) joined, none named {h}: {present}")])


def _node_scope(c, nodes, args):
    h, scope = _hostname(c, args[0]), args[1]
    node = _find_node(nodes, h)
    got = (node or {}).get("scope", "")
    if got == scope:
        return CheckResult(PASS, f"node {h} scope={scope}",
                           proofs=[ProofItem("node-record", f"node {h} (tctl get nodes)", _node_desc(node))])
    return CheckResult(FAIL, f"node {h} scope='{got}' expected '{scope}'")


def _node_count(c, nodes, args):
    want, got = int(args[0]), len(nodes)
    proof = ProofItem("text", f"node inventory ({got})",
                      f"{got} node(s): {', '.join(_hostnames(nodes)) or 'none'}")
    if got == want:
        return CheckResult(PASS, f"exactly {want} node(s) joined", proofs=[proof])
    return CheckResult(FAIL, f"expected {want} node(s), got {got}", proofs=[proof])


def _scoped_node_count(c, nodes, args):
    scope, want = args[0], int(args[1])
    scoped = [n.get("spec", {}).get("hostname", "?") for n in nodes if n.get("scope") == scope]
    proof = ProofItem("text", f"nodes in scope {scope} ({len(scoped)})",
                      f"in scope {scope}: {', '.join(scoped) or 'none'}")
    if len(scoped) == want:
        return CheckResult(PASS, f"exactly {want} node(s) in scope {scope}", proofs=[proof])
    return CheckResult(FAIL, f"expected {want} node(s) in scope {scope}, got {len(scoped)}", proofs=[proof])


# --- log / audit --------------------------------------------------------------
def _log_contains(c, nodes, args):
    suffix, pattern = args[0], " ".join(args[1:])
    cname = c.container(suffix)
    lines = c.logs(suffix).splitlines()
    rx = re.compile(pattern, re.IGNORECASE)
    idxs = [i for i, ln in enumerate(lines) if rx.search(ln)][:5]  # cap match windows
    if idxs:
        proof = _log_proof(cname, suffix, f"{cname} log matches /{pattern}/", _excerpt(lines, idxs))
        return CheckResult(PASS, f"{cname} log matches /{pattern}/", proofs=[proof])
    return CheckResult(SKIP, f"{cname} log has no match for /{pattern}/ yet")


# count comparators for log_count (test(1)-style, so no shell/markdown-special chars)
_OPS: dict[str, tuple[Callable[[int, int], bool], str]] = {
    "eq": (lambda a, b: a == b, "=="),
    "ne": (lambda a, b: a != b, "!="),
    "lt": (lambda a, b: a < b, "<"),
    "le": (lambda a, b: a <= b, "<="),
    "gt": (lambda a, b: a > b, ">"),
    "ge": (lambda a, b: a >= b, ">="),
}


def _log_count(c, nodes, args):
    """Count log lines matching a regex and assert the tally against <op> <n>.

    The workhorse for caching-style proofs: e.g. "≥3 joins drove traffic" and, on the
    SAME IdP log, "discovery was fetched ≤1×" — together they show the validator reused
    a cached provider instead of re-fetching per join. The proof lists the matched lines
    (line-numbered, bounded) so the tally is inspectable."""
    suffix, op = args[0], args[1]
    if op not in _OPS:
        return CheckResult(FAIL, f"log_count: unknown operator '{op}' (want {'/'.join(_OPS)})")
    try:
        want = int(args[2])
    except ValueError:
        return CheckResult(FAIL, f"log_count: threshold '{args[2]}' is not an integer")
    pattern = " ".join(args[3:])
    cname = c.container(suffix)
    lines = c.logs(suffix).splitlines()
    rx = re.compile(pattern, re.IGNORECASE)
    idxs = [i for i, ln in enumerate(lines) if rx.search(ln)]
    count = len(idxs)
    fn, sym = _OPS[op]
    shown = [f"[{i + 1}] {lines[i].rstrip()}" for i in idxs[:20]]
    if count > 20:
        shown.append(f"… (+{count - 20} more matching line(s); see the linked full log)")
    proof = ProofItem("log-excerpt", f"{cname} log: {count}× /{pattern}/",
                      "\n".join(shown) if shown else "(no matching lines)",
                      source=f"logs/{suffix}.log")
    status = PASS if fn(count, want) else FAIL
    return CheckResult(status, f"{cname} log has {count} match(es) for /{pattern}/ ({sym} {want})",
                       proofs=[proof], assertions=[f"count(/{pattern}/) {sym} {want}"])


def _parse_conds(args: list[str]) -> list[tuple[str, str]]:
    """`field=value` selectors from an audit_event line (non-`k=v` args are ignored)."""
    return [(a.split("=", 1)[0], a.split("=", 1)[1]) for a in args if "=" in a]


def _event_matches(ev: dict, etype: str, conds: list[tuple[str, str]]) -> bool:
    if ev.get("event") != etype:
        return False
    return all(str(ev.get(k, "")).lower() == v.lower() for k, v in conds)


def _audit_proof(ev: dict) -> ProofItem:
    """The FULL audit event as pretty JSON — the untruncated proof a reader inspects."""
    title = f"{ev.get('event', 'event')} audit event"
    if ev.get("code"):
        title += f" ({ev['code']})"
    return ProofItem("audit-event", title, json.dumps(ev, indent=2, sort_keys=True), lang="json")


def _audit_event(c, nodes, args):
    """Inspect a structured audit event: find one of <event-type> matching every
    field=value selector, and render its FULL JSON as proof. Because the proof is the
    whole event (independent of which fields a line asserts), two audit_event lines
    selecting the same event dedup to ONE proof section that both checks link to."""
    etype = args[0]
    conds = _parse_conds(args[1:])
    sel = " ".join(args[1:])
    asserts = [f"event = {etype}"] + [f"{k} = {v}" for k, v in conds]
    events = c.audit_events()
    for ev in events:
        if _event_matches(ev, etype, conds):
            return CheckResult(PASS, f"audit event '{etype}' present" + (f" ({sel})" if sel else ""),
                               proofs=[_audit_proof(ev)], assertions=asserts)
    # no full match: surface the closest same-type event (if any) to aid debugging
    candidates = [ev for ev in events if ev.get("event") == etype]
    if candidates:
        return CheckResult(FAIL, f"no '{etype}' audit event matched {sel}",
                           proofs=[_audit_proof(candidates[0])], assertions=asserts)
    return CheckResult(FAIL, f"no '{etype}' audit event found", assertions=asserts)


def _bot_joined(c, nodes, args):
    name = args[0]
    method = args[1] if len(args) > 1 else ""
    suffix = f" via {method}" if method else ""
    # prefer the structured audit event (full JSON proof); fall back to the text log
    # for clusters without the JSON audit backend (older bundles).
    conds = [("bot_name", name), ("success", "true")] + ([("method", method)] if method else [])
    asserts = ["event = bot.join"] + [f"{k} = {v}" for k, v in conds]
    for ev in c.audit_events():
        if _event_matches(ev, "bot.join", conds):
            return CheckResult(PASS, f"bot '{name}' joined{suffix}",
                               proofs=[_audit_proof(ev)], assertions=asserts)
    lines = c.logs("auth").splitlines()
    for i, ln in enumerate(lines):
        if ("bot.join" in ln and f"bot_name:{name}" in ln and "success:true" in ln
                and (not method or f"method:{method}" in ln)):
            proof = _log_proof(c.container("auth"), "auth",
                               f"bot.join audit event for '{name}'", _excerpt(lines, [i]))
            return CheckResult(PASS, f"bot '{name}' joined{suffix}", proofs=[proof], assertions=asserts)
    return CheckResult(FAIL, f"bot '{name}' did not join{suffix} (no successful bot.join event)")


# --- tbot output artifacts ----------------------------------------------------
def _output_file(c, nodes, args):
    suffix, path = args[0], args[1]
    cname = c.container(suffix)
    if c.file_nonempty(suffix, path):
        size = c.file_size(suffix, path)
        detail = f"{path}: {size} bytes" if size is not None else f"{path}: present"
        return CheckResult(PASS, f"{cname}:{path} present",
                           proofs=[ProofItem("file", f"{cname}:{path}", detail)])
    return CheckResult(FAIL, f"{cname}:{path} missing")


def _no_output_file(c, nodes, args):
    suffix, path = args[0], args[1]
    cname = c.container(suffix)
    if c.file_nonempty(suffix, path):
        return CheckResult(FAIL, f"{cname}:{path} present but expected none")
    return CheckResult(PASS, f"{cname}:{path} absent",
                       proofs=[ProofItem("file", f"{cname}:{path} (absent)", f"{path} not present (as expected)")])


# --- identity usability -------------------------------------------------------
def _identity_authorized(c, nodes, args):
    suffix, ident = args[0], args[1]
    auth_server = args[2] if len(args) > 2 else "auth:3025"
    argv = ["tctl", "--identity", ident, "--auth-server", auth_server, "tokens", "ls"]
    rc, out = c.exec_out(suffix, argv)
    cmd = f"$ {' '.join(argv)}\nexit {rc}"
    if rc == 0:
        first = next((ln for ln in out.splitlines() if ln.strip()), "")
        content = cmd + (f"\n{first}" if first else "")
        return CheckResult(PASS, f"{c.container(suffix)} identity authenticates + is authorized",
                           proofs=[ProofItem("command", f"{c.container(suffix)}: authorized tctl call", content)])
    return CheckResult(FAIL, f"{c.container(suffix)} identity could not perform an authorized action",
                       proofs=[ProofItem("command", f"{c.container(suffix)}: failed tctl call", cmd)])


def _tsh_ssh(c, nodes, args):
    suffix = args[0]
    login = args[1] if len(args) > 1 else "root"
    h = _hostname(c, suffix)
    if c.tsh_ssh(suffix, login):
        return CheckResult(PASS, f"tsh ssh {login}@{h} works",
                           proofs=[ProofItem("command", f"tsh ssh {login}@{h}",
                                             f"$ tsh ssh {login}@{h} -- echo harness-ok\nharness-ok")])
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
        return CheckResult(PASS, f"{cname} identity is scope-pinned to {scope}",
                           proofs=[ProofItem("command", f"{cname}: tsh status --identity", line)])
    return CheckResult(FAIL, f"{cname} identity is not scope-pinned to {scope} (exit {rc})",
                       proofs=[ProofItem("command", f"{cname}: tsh status --identity", line or _truncate(out, 140))])


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
    cmd = f"$ tsh ssh {login}@{node} (identity {ident})\nexit {rc}"
    if rc == 0 and "harness-ok" in out:
        return CheckResult(PASS, f"{c.container(suffix)} identity can tsh ssh {login}@{node}",
                           proofs=[ProofItem("command", f"{c.container(suffix)} → tsh ssh {login}@{node}",
                                             cmd + "\nstdout: harness-ok")])
    return CheckResult(FAIL, f"{c.container(suffix)} identity could NOT tsh ssh {login}@{node}",
                       proofs=[ProofItem("command", f"{c.container(suffix)} → tsh ssh {login}@{node} (failed)",
                                         cmd + f"\n{_truncate(out, 160)}")])


# --- live resource state (what a terraform apply, or any actor, created) ------
def _split_ref(ref: str) -> tuple[str, str]:
    """`kind/name` -> (kind, name). Name may itself contain '/' (rare); split once."""
    kind, _, name = ref.partition("/")
    return kind, name


def _resource_proof(kind: str, name: str, doc: dict) -> ProofItem:
    return ProofItem("resource", f"{kind}/{name} (tctl get)",
                     json.dumps(doc, indent=2, sort_keys=True), lang="json")


def _dig(doc: dict, path: str):
    """Walk a dotted path through nested dicts; return (found, value)."""
    cur = doc
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _resource_present(c, nodes, args):
    kind, name = _split_ref(args[0])
    doc = c.get_resource(kind, name)
    if doc:
        return CheckResult(PASS, f"resource {kind}/{name} present",
                           proofs=[_resource_proof(kind, name, doc)])
    return CheckResult(FAIL, f"resource {kind}/{name} not found")


def _resource_field(c, nodes, args):
    """Assert a field (dotted path) on a live resource. With <expected>, its value must
    match (case-insensitive string compare); without, the path must merely be present.
    A missing resource OR missing path FAILs — this is how terraform_generic_oidc
    surfaces the must_match_fields bug (apply aborts, token never created)."""
    kind, name = _split_ref(args[0])
    path = args[1]
    expected = args[2] if len(args) > 2 else None
    asserts = [f"{kind}/{name}.{path}" + (f" = {expected}" if expected is not None else " present")]
    doc = c.get_resource(kind, name)
    if not doc:
        return CheckResult(FAIL, f"resource {kind}/{name} not found (cannot read {path})",
                           assertions=asserts)
    found, value = _dig(doc, path)
    proof = _resource_proof(kind, name, doc)
    if not found:
        return CheckResult(FAIL, f"{kind}/{name}: field {path} absent",
                           proofs=[proof], assertions=asserts)
    if expected is not None and expected.lower() not in str(value).lower():
        return CheckResult(FAIL, f"{kind}/{name}: {path}={value!r} != {expected!r}",
                           proofs=[proof], assertions=asserts)
    detail = f"= {value}" if expected is None else f"= {value} (matches {expected})"
    return CheckResult(PASS, f"{kind}/{name}: {path} {detail}",
                       proofs=[proof], assertions=asserts)


# verb -> impl. Kept in lockstep with harness/checks.REGISTRY (test enforces it).
Impl = Callable[[Cluster, list[dict], list[str]], CheckResult]
IMPLS: dict[str, Impl] = {
    "node_present": _node_present,
    "node_absent": _node_absent,
    "node_scope": _node_scope,
    "node_count": _node_count,
    "scoped_node_count": _scoped_node_count,
    "log_contains": _log_contains,
    "log_count": _log_count,
    "audit_event": _audit_event,
    "bot_joined": _bot_joined,
    "output_file": _output_file,
    "no_output_file": _no_output_file,
    "identity_authorized": _identity_authorized,
    "identity_scope": _identity_scope,
    "tsh_ssh": _tsh_ssh,
    "tsh_ssh_as": _tsh_ssh_as,
    "resource_present": _resource_present,
    "resource_field": _resource_field,
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
    """Return (human text incl. proof sub-lines + RESULT line, passed?)."""
    passed = not any(r.status == FAIL for r in results)
    lines: list[str] = []
    for r in results:
        lines.append(r.line())
        for p in r.proofs:
            lines.append(f"       ↳ {p.title}")
            for cl in p.content.splitlines():
                lines.append(f"         {_truncate(cl, 200)}")
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
