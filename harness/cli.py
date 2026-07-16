"""CLI shim the shell layer (bin/cluster, lib/*.sh) calls into for anything that
used to be grep/sed/awk over YAML. Subcommands are deliberately small and
script-friendly (stable stdout, meaningful exit codes).

Exit codes:
  0  ok / run
  1  usage or load/validation error
  3  gated out (skip) — distinct from error so callers can branch cleanly
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .models import discover_modules, gate, load_module

EXIT_OK = 0
EXIT_ERR = 1
EXIT_SKIP = 3


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _modules_dir(arg: str | None) -> Path:
    return Path(arg) if arg else _root() / "modules"


def _csv(s: str | None) -> list[str] | None:
    if s is None:
        return None
    return [x for x in (p.strip() for p in s.split(",")) if x]


def cmd_validate(args: argparse.Namespace) -> int:
    mdir = _modules_dir(args.modules_dir)
    if args.module:
        try:
            mods = [load_module(mdir / args.module)]
        except (ValidationError, ValueError, FileNotFoundError) as e:
            print(f"FAIL {args.module}: {e}", file=sys.stderr)
            return EXIT_ERR
    else:
        mods = []
        rc = EXIT_OK
        for d in sorted(p for p in mdir.iterdir() if p.is_dir()):
            if not (d / "module.yaml").is_file():
                continue
            try:
                mods.append(load_module(d))
            except (ValidationError, ValueError) as e:
                print(f"FAIL {d.name}: {e}", file=sys.stderr)
                rc = EXIT_ERR
        if rc != EXIT_OK:
            return rc

    rc = EXIT_OK
    for m in mods:
        problems = m.validate_semantics()
        if problems:
            rc = EXIT_ERR
            for p in problems:
                print(f"FAIL {m.name}: {p}", file=sys.stderr)
        else:
            n = len(m.checks)
            print(f"ok   {m.name}: {n} check(s), gates={m.requires_features or '[]'}"
                  f" min_version={m.min_version or '-'}")
    return rc


def cmd_meta(args: argparse.Namespace) -> int:
    m = load_module(_modules_dir(args.modules_dir) / args.module)
    field = args.field
    if field == "requires_features":
        print(" ".join(m.requires_features))
    elif field == "provides_feature":
        print(m.provides_feature or "")
    elif field == "min_version":
        print(m.min_version or "")
    elif field == "description":
        print(m.description)
    else:
        print(f"unknown field '{field}'", file=sys.stderr)
        return EXIT_ERR
    return EXIT_OK


def cmd_checks(args: argparse.Namespace) -> int:
    """Emit validated checks, one normalized `verb arg arg...` line each — the
    replacement for the awk block-extraction in lib/verify.sh."""
    m = load_module(_modules_dir(args.modules_dir) / args.module)
    problems = m.validate_semantics()
    if problems:
        for p in problems:
            print(f"FAIL {m.name}: {p}", file=sys.stderr)
        return EXIT_ERR
    for chk in m.checks:
        print(" ".join([chk.verb, *chk.args]))
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    """Run a module's checks against a live cluster; print the same
    `PASS/FAIL/SKIP` + `RESULT:` text lib/verify.sh used, optionally write JSON.
    Exit 1 on any FAIL (so plan.sh's retry loop keeps working)."""
    from .cluster import DockerCluster
    from .verify import collect_proofs, node_summary, render, verify

    mdir = _modules_dir(args.modules_dir) / args.module
    m = load_module(mdir)
    problems = m.validate_semantics()
    if problems:
        for p in problems:
            print(f"FAIL {m.name}: {p}", file=sys.stderr)
        return EXIT_ERR

    state_dir = Path(args.state_dir) if args.state_dir else None
    cluster = DockerCluster(args.cluster_id, state_dir=state_dir)
    nodes = cluster.get_nodes()  # captured once for both verification + the report inventory
    results = verify(cluster, m.checks, module_dir=mdir, nodes=nodes)
    text, passed = render(results)
    print(text)

    if args.json_out:
        payload = {
            "module": m.name,
            "cluster_id": args.cluster_id,
            "passed": passed,
            "nodes": node_summary(nodes),
            "results": [r.as_dict() for r in results],
            "proofs": [p.as_dict() for p in collect_proofs(results)],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n")
    return EXIT_OK if passed else EXIT_ERR


def cmd_report_md(args: argparse.Namespace) -> int:
    """Emit the rich markdown report for a cluster's state dir (to stdout)."""
    from .report import build_markdown

    print(build_markdown(Path(args.state_dir)))
    return EXIT_OK


def cmd_gist_stage(args: argparse.Namespace) -> int:
    """Stage a report bundle for `gh gist create` (flatten paths, rewrite links to
    gist anchors). Prints the staged filenames, results.md first."""
    from .share import stage_gist

    for name in stage_gist(Path(args.bundle), Path(args.out)):
        print(name)
    return EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    """Compose + render one or more modules (+ their shared components) into --out."""
    from .render import render_cluster

    mods = _csv(args.modules) or []
    if not mods:
        print("error: --modules is required", file=sys.stderr)
        return EXIT_ERR
    mdirs = []
    for m in mods:
        d = _modules_dir(args.modules_dir) / m
        if not (d / "services.yml.j2").is_file():
            print(f"error: module '{m}' has no services.yml.j2", file=sys.stderr)
            return EXIT_ERR
        mdirs.append(d)
    ctx = {
        "cluster_id": args.cluster_id,
        "fqdn": args.fqdn,
        "port": args.port,
        "image": args.image,
        "harness_domain": args.harness_domain,
        "lab_domain": args.lab_domain,
        "license_file": args.license_file,
        "out": args.out,
    }
    compose = render_cluster(mdirs, ctx, Path(args.out), components_dir=_root() / "components")
    print(f"[render] wrote {compose}", file=sys.stderr)
    return EXIT_OK


def cmd_plan_resolve(args: argparse.Namespace) -> int:
    """Load a plan, gate each module, emit JSON {name, run:[…], skip:[{module,reason}]}."""
    from .models import load_plan

    plan = load_plan(_root() / "plans" / f"{args.plan}.yaml")
    result: dict = {"name": plan.name, "run": [], "skip": []}
    for m in plan.modules:
        mod = load_module(_modules_dir(args.modules_dir) / m)
        problems = mod.validate_semantics()
        if problems:
            print(f"FAIL {m}: {'; '.join(problems)}", file=sys.stderr)
            return EXIT_ERR
        res = gate(mod, _csv(args.features), args.version)
        if res.skip:
            result["skip"].append({"module": m, "reason": res.reason})
        else:
            result["run"].append(m)
    print(json.dumps(result))
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    m = load_module(_modules_dir(args.modules_dir) / args.module)
    res = gate(m, _csv(args.features), args.version)
    if res.skip:
        print(res.reason)
        return EXIT_SKIP
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="harness", description=__doc__)
    p.add_argument("--modules-dir", help="override modules/ location")
    sub = p.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser("validate", help="load + schema/semantics-check module(s)")
    sv.add_argument("module", nargs="?", help="one module, or omit for all")
    sv.set_defaults(fn=cmd_validate)

    sm = sub.add_parser("meta", help="read a gating field (drop-in for module_meta)")
    sm.add_argument("module")
    sm.add_argument("field")
    sm.set_defaults(fn=cmd_meta)

    sc = sub.add_parser("checks", help="emit validated `verb args` check lines")
    sc.add_argument("module")
    sc.set_defaults(fn=cmd_checks)

    sf = sub.add_parser("verify", help="run a module's checks against a live cluster")
    sf.add_argument("module")
    sf.add_argument("--cluster-id", required=True)
    sf.add_argument("--state-dir", help="state/<id>/ (for meta needed by tsh_ssh)")
    sf.add_argument("--json-out", help="also write a JSON report to this path")
    sf.set_defaults(fn=cmd_verify)

    srm = sub.add_parser("report-md", help="build the rich markdown report from a state dir")
    srm.add_argument("--state-dir", required=True)
    srm.set_defaults(fn=cmd_report_md)

    sgs = sub.add_parser("gist-stage", help="stage a report bundle for `gh gist create`")
    sgs.add_argument("--bundle", required=True, help="a runs/<ts>-<id>/ report bundle")
    sgs.add_argument("--out", required=True, help="staging dir to write flattened files into")
    sgs.set_defaults(fn=cmd_gist_stage)

    sr = sub.add_parser("render", help="compose + render one or more modules into --out")
    sr.add_argument("--modules", required=True, help="comma-separated module names")
    sr.add_argument("--cluster-id", required=True)
    sr.add_argument("--fqdn", required=True)
    sr.add_argument("--port", required=True)
    sr.add_argument("--image", required=True)
    sr.add_argument("--harness-domain", default="")
    sr.add_argument("--lab-domain", default="")
    sr.add_argument("--license-file", default="",
                    help="host path to an enterprise license; mounted into the auth "
                         "container and referenced by auth_service.license_file (ent builds)")
    sr.add_argument("--out", required=True)
    sr.set_defaults(fn=cmd_render)

    sp = sub.add_parser("plan-resolve", help="gate a plan's modules; emit run/skip JSON")
    sp.add_argument("plan")
    sp.add_argument("--features")
    sp.add_argument("--version")
    sp.set_defaults(fn=cmd_plan_resolve)

    sg = sub.add_parser("gate", help="feature/version gate; exit 3 == skip")
    sg.add_argument("module")
    sg.add_argument("--features")
    sg.add_argument("--version")
    sg.set_defaults(fn=cmd_gate)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError, ValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERR


if __name__ == "__main__":
    raise SystemExit(main())
