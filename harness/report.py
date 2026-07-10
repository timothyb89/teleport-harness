"""Rich markdown report generation from the structured run data.

Reads the cluster's state/<id>/ (meta.env, rendered docker-compose.yml + bootstrap/,
and the per-module results-*.json the verifier wrote) and emits a markdown report that
makes the test transparent: what was deployed, what joined, and the concrete evidence
each check relied on. Links are bundle-relative (report.sh copies state → bundle/rendered).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

_BADGE = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "SKIP": "⏭️ SKIP"}


def _read_meta(state_dir: Path) -> dict:
    meta: dict = {}
    f = state_dir / "meta.env"
    if f.is_file():
        for line in f.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                meta[k] = v
    return meta


def _load_results(state_dir: Path) -> list[dict]:
    out = []
    for f in sorted(state_dir.glob("results-*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def _summarize_compose(state_dir: Path) -> list[tuple[str, str]]:
    f = state_dir / "docker-compose.yml"
    if not f.is_file():
        return []
    try:
        c = yaml.safe_load(f.read_text()) or {}
    except yaml.YAMLError:
        return []
    return [(name, spec.get("image", "?")) for name, spec in (c.get("services") or {}).items()]


def _summarize_bootstrap(state_dir: Path) -> dict:
    bdir = state_dir / "bootstrap"
    roles, tokens = [], []
    bots: list[tuple[str, str]] = []
    if not bdir.is_dir():
        return {"roles": roles, "tokens": tokens, "bots": bots}
    for f in sorted(bdir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            continue
        kind = doc.get("kind")
        name = (doc.get("metadata") or {}).get("name", "?")
        if kind == "role":
            roles.append(name)
        elif kind == "token":
            method = (doc.get("spec") or {}).get("join_method", "")
            tokens.append(f"{name} ({method})" if method else name)
    manifest = bdir / "bots.manifest"
    if manifest.is_file():
        for line in manifest.read_text().splitlines():
            parts = line.split("\t")
            if parts and parts[0].strip():
                bots.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return {"roles": roles, "tokens": tokens, "bots": bots}


def _counts(results: list[dict]) -> dict:
    c = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        c[r.get("status", "SKIP")] = c.get(r.get("status", "SKIP"), 0) + 1
    return c


def build_markdown(state_dir: Path) -> str:
    state_dir = Path(state_dir)
    meta = _read_meta(state_dir)
    modules = _load_results(state_dir)
    cid = meta.get("CLUSTER_ID", state_dir.name)

    overall = "PASS" if all(m.get("passed") for m in modules) and modules else \
              ("FAIL" if modules else "UNKNOWN")
    tot = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for m in modules:
        for k, v in _counts(m.get("results", [])).items():
            tot[k] += v

    L: list[str] = []
    L.append(f"# Test run: {cid} — {_BADGE.get(overall, overall)}")
    L.append("")
    label = meta.get("MODULE", "?")
    mods = meta.get("MODULES", "")
    L.append(f"- **plan/module:** {label}" + (f" (modules: {mods})" if mods and mods != label else ""))
    L.append(f"- **repo:** `{meta.get('REPO', '?')}` @ `{meta.get('SHA', '?')}`")
    feats, ver = meta.get("FEATURES", ""), meta.get("VERSION", "")
    if feats or ver:
        L.append(f"- **target:** version `{ver or '-'}`, features `{feats or '-'}`")
    L.append(f"- **created:** {meta.get('CREATED', '?')}")
    fqdn, port = meta.get("FQDN", ""), meta.get("PORT", "")
    if fqdn:
        L.append(f"- **web UI:** https://{fqdn}:{port}  (`cluster web {cid}` for admin login)")
    L.append("")

    # ---- summary ----
    L.append("## Summary")
    L.append("")
    L.append("| module | ✅ pass | ❌ fail | ⏭️ skip |")
    L.append("|--------|-----:|-----:|-----:|")
    for m in modules:
        c = _counts(m.get("results", []))
        L.append(f"| {m.get('module', '?')} | {c['PASS']} | {c['FAIL']} | {c['SKIP']} |")
    L.append("")
    L.append(f"**Overall: {_BADGE.get(overall, overall)}** — "
             f"{tot['PASS']} passed, {tot['FAIL']} failed, {tot['SKIP']} skipped.")
    L.append("")

    # ---- cluster setup ----
    L.append("## Cluster setup")
    L.append("")
    services = _summarize_compose(state_dir)
    if services:
        L.append("**Services** (rendered [docker-compose.yml](rendered/docker-compose.yml)):")
        L.append("")
        for name, image in services:
            L.append(f"- `{name}` — image `{image}`")
        L.append("")
    boot = _summarize_bootstrap(state_dir)
    if any(boot.values()):
        L.append("**Bootstrap** (rendered [bootstrap/](rendered/bootstrap)):")
        L.append("")
        if boot["roles"]:
            L.append(f"- roles: {', '.join(f'`{r}`' for r in boot['roles'])}")
        if boot["tokens"]:
            L.append(f"- tokens: {', '.join(f'`{t}`' for t in boot['tokens'])}")
        if boot["bots"]:
            L.append("- bots: " + ", ".join(f"`{n}` (roles: {r})" for n, r in boot["bots"]))
        L.append("")
    L.append("Configs: [rendered/config/](rendered/config)")
    L.append("")

    # ---- nodes joined (captured at verify time) ----
    nodes = next((m.get("nodes") for m in modules if m.get("nodes")), None)
    if nodes:
        L.append("## Nodes joined (`tctl get nodes`)")
        L.append("")
        L.append("| hostname | scope | labels |")
        L.append("|----------|-------|--------|")
        for n in nodes:
            labels = ", ".join(f"{k}={v}" for k, v in sorted((n.get("labels") or {}).items()))
            L.append(f"| {n.get('hostname', '?')} | {n.get('scope', '') or '—'} | {labels or '—'} |")
        L.append("")

    # ---- checks with evidence ----
    L.append("## Checks")
    L.append("")
    for m in modules:
        c = _counts(m.get("results", []))
        badge = _BADGE["PASS"] if m.get("passed") else _BADGE["FAIL"]
        L.append(f"### {m.get('module', '?')} — {badge}  "
                 f"({c['PASS']} pass / {c['FAIL']} fail / {c['SKIP']} skip)")
        L.append("")
        for r in m.get("results", []):
            verb_args = " ".join([r.get("verb", "")] + r.get("args", [])).strip()
            L.append(f"- {_BADGE.get(r['status'], r['status'])} `{verb_args}` — {r.get('msg', '')}")
            for ev in r.get("evidence", []):
                L.append(f"  - proof: `{ev}`")
        L.append("")

    # ---- inspect ----
    L.append("## Inspect")
    L.append("")
    L.append(f"- live cluster: `cluster logs {cid} [service]`" + (f" · web UI: https://{fqdn}:{port}" if fqdn else ""))
    L.append("- rendered: [docker-compose.yml](rendered/docker-compose.yml) · "
             "[config/](rendered/config) · [bootstrap/](rendered/bootstrap)")
    L.append("- per-service logs: [logs/](logs) · structured results: `results-*.json`")
    L.append(f"- teardown when done: `cluster teardown {cid}`")
    L.append("")
    return "\n".join(L)
