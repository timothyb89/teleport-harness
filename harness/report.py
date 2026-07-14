"""Rich markdown report generation from the structured run data.

Reads the cluster's state/<id>/ (meta.env, setup.json, the rendered docker-compose,
and the per-module results-*.json the verifier wrote) and emits a markdown report that
makes the test transparent: what was deployed (from the renderer's setup.json
provenance manifest — no retroactive scraping), what joined, and the concrete PROOF
each check relied on.

Two report foundations show up here:
  * setup.json (Foundation B) — roles/tokens/bots/services each with a source link, so
    "what was created" is rendered as tables that point at the exact rendered resource.
  * proof items (Foundation A) — evidence is decoupled from checks: each module's
    results carry a `proofs` registry, and checks reference proofs by id. The report
    renders each proof ONCE as a linkable, untruncated section; the check table links
    to it. Several checks can cite one proof.

Links are bundle-relative (report.sh copies state → bundle/{rendered,logs,setup.json}).
The reader falls back to the legacy inline-evidence shape for older bundles.
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


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _load_results(state_dir: Path) -> list[dict]:
    out = []
    for f in sorted(state_dir.glob("results-*.json")):
        data = _load_json(f)
        if data is not None:
            out.append(data)
    return out


def _compose_services(state_dir: Path) -> list[dict]:
    """Fallback service list when setup.json is absent (older bundles)."""
    f = state_dir / "docker-compose.yml"
    if not f.is_file():
        return []
    try:
        c = yaml.safe_load(f.read_text()) or {}
    except yaml.YAMLError:
        return []
    return [{"name": n, "image": (s or {}).get("image", "?"), "origin": ""}
            for n, s in (c.get("services") or {}).items()]


def _counts(results: list[dict]) -> dict:
    c = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        c[r.get("status", "SKIP")] = c.get(r.get("status", "SKIP"), 0) + 1
    return c


def _link(text: str, href: str) -> str:
    return f"[{text}]({href})" if href else text


def _src(href: str) -> str:
    """A source-link table cell, or an em-dash when there's no linkable source."""
    return _link("yaml", href) if href else "—"


def _cell(s: str) -> str:
    """Make a value safe for a markdown table cell (no raw pipes / newlines)."""
    return str(s).replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- setup
def _setup_section(L: list[str], setup: dict | None, state_dir: Path) -> None:
    L.append("## Cluster setup")
    L.append("")

    services = (setup or {}).get("services") or _compose_services(state_dir)
    if services:
        L.append("### Services")
        L.append("")
        L.append("| service | image | from |")
        L.append("|---------|-------|------|")
        for s in services:
            L.append(f"| `{_cell(s['name'])}` | `{_cell(s.get('image', '?'))}` | {_cell(s.get('origin', '') or '—')} |")
        L.append("")

    roles = (setup or {}).get("roles") or []
    if roles:
        has_scope = any(r.get("scope") for r in roles)
        L.append("### Roles")
        L.append("")
        head = "| role | " + ("scope | " if has_scope else "") + "permissions | from | source |"
        sep = "|------|" + ("-------|" if has_scope else "") + "-------------|------|--------|"
        L.append(head)
        L.append(sep)
        for r in roles:
            desc = r.get("description", "").strip()
            allow = r.get("allow", "")
            # prose description stays plain; a structured allow-summary is wrapped in a code
            # span so `*` / `[` / `_` in RBAC values don't render as markdown.
            perms = _cell(desc) if desc else (f"`{_cell(allow)}`" if allow else "—")
            scope_cell = f"`{_cell(r['scope'])}` | " if has_scope and r.get("scope") else ("— | " if has_scope else "")
            L.append(f"| `{_cell(r['name'])}` | {scope_cell}{perms} "
                     f"| {_cell(r.get('origin', '') or '—')} | {_src(r.get('source', ''))} |")
        L.append("")

    tokens = (setup or {}).get("tokens") or []
    if tokens:
        L.append("### Join tokens")
        L.append("")
        L.append("| token | join method | from | source |")
        L.append("|-------|-------------|------|--------|")
        for t in tokens:
            L.append(f"| `{_cell(t['name'])}` | {_cell(t.get('join_method', '') or '—')} "
                     f"| {_cell(t.get('origin', '') or '—')} | {_src(t.get('source', ''))} |")
        L.append("")

    bots = (setup or {}).get("bots") or []
    if bots:
        cfg_by_name = {c["file"]: c for c in ((setup or {}).get("configs") or [])}
        L.append("### Bots")
        L.append("")
        L.append("| bot | join method | roles | services | source |")
        L.append("|-----|-------------|-------|----------|--------|")
        for b in bots:
            # attach the bot's configured tbot outputs when a config file names it
            outs: list[str] = []
            for fname, cfg in cfg_by_name.items():
                if b["name"] in fname:
                    outs = cfg.get("outputs", [])
                    break
            roles = ", ".join(b.get("roles", [])) or "—"
            L.append(f"| `{_cell(b['name'])}` | {_cell(b.get('join_method', '') or '—')} "
                     f"| {_cell(roles)} | {_cell(', '.join(outs) or '—')} | {_src(b.get('source', ''))} |")
        L.append("")

    L.append("Rendered inputs: [docker-compose.yml](rendered/docker-compose.yml) · "
             "[config/](rendered/config) · [bootstrap/](rendered/bootstrap)")
    L.append("")


# --------------------------------------------------------------------------- proofs
def _proofs_for_module(m: dict) -> tuple[dict, dict]:
    """Return (proof_by_id, refs_by_check_index). Supports the new proof-registry shape
    and synthesizes proofs from the legacy inline evidence/excerpt of older bundles."""
    results = m.get("results", [])
    if "proofs" in m or any("proof_refs" in r for r in results):
        proofs = {p["id"]: p for p in m.get("proofs", [])}
        refs = {i: r.get("proof_refs", []) for i, r in enumerate(results)}
        return proofs, refs
    # legacy: fold each check's evidence + excerpt into a synthetic proof
    proofs, refs = {}, {}
    for i, r in enumerate(results):
        ev, ex = r.get("evidence", []), r.get("excerpt", [])
        if not ev and not ex:
            refs[i] = []
            continue
        content = "\n".join(list(ev) + list(ex))
        pid = f"legacy-{m.get('module', '?')}-{i}"
        kind = "log-excerpt" if ex else "text"
        proofs[pid] = {"id": pid, "kind": kind, "title": " ".join(r.get("args", [])) or r.get("verb", ""),
                       "content": content, "lang": "", "source": ""}
        refs[i] = [pid]
    return proofs, refs


def _anchor(module: str, pid: str) -> str:
    return f"proof-{module}-{pid}"


def _checks_section(L: list[str], modules: list[dict]) -> None:
    L.append("## Checks")
    L.append("")
    for m in modules:
        module = m.get("module", "?")
        results = m.get("results", [])
        c = _counts(results)
        badge = _BADGE["PASS"] if m.get("passed") else _BADGE["FAIL"]
        L.append(f"### {module} — {badge}  ({c['PASS']} pass / {c['FAIL']} fail / {c['SKIP']} skip)")
        L.append("")

        proofs, refs = _proofs_for_module(m)
        # check table, linking each check to its proof section(s)
        L.append("| status | check | detail | proof |")
        L.append("|--------|-------|--------|-------|")
        for i, r in enumerate(results):
            verb_args = " ".join([r.get("verb", "")] + r.get("args", [])).strip()
            links = " ".join(
                _link("↳", f"#{_anchor(module, pid)}") for pid in refs.get(i, []) if pid in proofs
            ) or "—"
            L.append(f"| {_BADGE.get(r['status'], r['status'])} | `{_cell(verb_args)}` "
                     f"| {_cell(r.get('msg', ''))} | {links} |")
        L.append("")

        # proof sections — each rendered once, in first-reference order, untruncated,
        # with the checks made against it spelled out (audit-event field=value assertions).
        ordered: list[str] = []
        citing: dict[str, list[int]] = {}
        for i in range(len(results)):
            for pid in refs.get(i, []):
                if pid not in proofs:
                    continue
                citing.setdefault(pid, []).append(i)
                if pid not in ordered:
                    ordered.append(pid)
        if ordered:
            L.append(f"#### Proofs — {module}")
            L.append("")
            for pid in ordered:
                p = proofs[pid]
                L.append(f'<a id="{_anchor(module, pid)}"></a>')
                L.append("")
                L.append(f"**{p.get('title', pid)}** · `{p.get('kind', '')}`"
                         + (f" · {_link('source', p['source'])}" if p.get("source") else ""))
                L.append("")
                # the check(s) made against this proof, and what each asserted
                L.append("Checks against this proof:")
                L.append("")
                for i in citing[pid]:
                    r = results[i]
                    verb_args = " ".join([r.get("verb", "")] + r.get("args", [])).strip()
                    L.append(f"- {_BADGE.get(r['status'], r['status'])} `{_cell(verb_args)}`")
                    asserts = r.get("assertions") or []
                    if asserts:
                        for a in asserts:
                            L.append(f"  - `{_cell(a)}`")
                    else:
                        L.append(f"  - {_cell(r.get('msg', ''))}")
                L.append("")
                content = p.get("content", "")
                if content:
                    lang = p.get("lang", "")
                    L.append(f"```{lang}")
                    L.extend(content.splitlines())
                    L.append("```")
                    L.append("")


# --------------------------------------------------------------------------- main
def build_markdown(state_dir: Path) -> str:
    state_dir = Path(state_dir)
    meta = _read_meta(state_dir)
    modules = _load_results(state_dir)
    setup = _load_json(state_dir / "setup.json")
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

    _setup_section(L, setup, state_dir)

    # ---- nodes joined (captured at verify time) ----
    nodes = next((m.get("nodes") for m in modules if m.get("nodes")), None)
    if nodes:
        L.append("## Nodes joined (`tctl get nodes`)")
        L.append("")
        L.append("| hostname | scope | labels |")
        L.append("|----------|-------|--------|")
        for n in nodes:
            labels = ", ".join(f"{k}={v}" for k, v in sorted((n.get("labels") or {}).items()))
            L.append(f"| {_cell(n.get('hostname', '?'))} | {_cell(n.get('scope', '') or '—')} "
                     f"| {_cell(labels or '—')} |")
        L.append("")

    _checks_section(L, modules)

    # ---- inspect ----
    L.append("## Inspect")
    L.append("")
    L.append(f"- live cluster: `cluster logs {cid} [service]`" + (f" · web UI: https://{fqdn}:{port}" if fqdn else ""))
    L.append("- rendered: [docker-compose.yml](rendered/docker-compose.yml) · "
             "[config/](rendered/config) · [bootstrap/](rendered/bootstrap)")
    L.append("- per-service logs: [logs/](logs) · structured results: `results-*.json` · provenance: `setup.json`")
    L.append(f"- teardown when done: `cluster teardown {cid}`")
    L.append("")
    return "\n".join(L)
