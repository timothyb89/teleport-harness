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

import yaml
from pydantic import ValidationError

from .models import backend_requirement, discover_modules, gate, load_module, repo_requirement

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


def _gate(mod, mdir: Path, args: argparse.Namespace):
    """Gate one module: features/version, plus (with --no-repo) whether its composition
    needs a teleport clone this run doesn't have."""
    unit = repo_requirement(mdir, _root() / "components") if getattr(args, "no_repo", False) else ""
    backend = getattr(args, "backend", None) or "docker"
    bunit = backend_requirement(mdir, _root() / "components", backend)
    return gate(mod, _csv(args.features), args.version, repo_unit=unit,
                backend_unit=bunit, backend=backend)


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
            n = len(m.all_checks())
            claims = f", {len(m.claims)} claim(s)" if m.claims else ""
            print(f"ok   {m.name}: {n} check(s){claims}, gates={m.requires_features or '[]'}"
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
    for chk in m.all_checks():
        print(" ".join([chk.verb, *chk.args]))
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    """Run a module's checks against a live cluster; print the same
    `PASS/FAIL/SKIP` + `RESULT:` text lib/verify.sh used, optionally write JSON.
    Exit 1 on any FAIL (so plan.sh's retry loop keeps working)."""
    from .cluster import make_cluster
    from .verify import (artifact_link, claim_verdicts, collect_proofs, node_summary,
                         render, verify)

    mdir = _modules_dir(args.modules_dir) / args.module
    m = load_module(mdir)
    problems = m.validate_semantics()
    if problems:
        for p in problems:
            print(f"FAIL {m.name}: {p}", file=sys.stderr)
        return EXIT_ERR

    state_dir = Path(args.state_dir) if args.state_dir else None
    cluster = make_cluster(args.cluster_id, state_dir=state_dir, module=m.name)
    nodes = cluster.get_nodes()  # captured once for both verification + the report inventory
    results = verify(cluster, m.all_checks(), module_dir=mdir, nodes=nodes)
    # An observation proof records its actor MODULE-relatively (the actor knows its own
    # filename, not the bundle layout); resolve those to bundle paths here, where the module
    # name is known, so the proof links to the script a reviewer can actually open.
    for r in results:
        for p in r.proofs:
            if p.kind == "observation" and p.source and not p.source.startswith("rendered/"):
                p.source = artifact_link(m.name, p.source)["path"]
    text, passed = render(results)
    print(text)

    if args.json_out:
        payload = {
            "module": m.name,
            "description": m.description,
            "cluster_id": args.cluster_id,
            "passed": passed,
            "nodes": node_summary(nodes),
            "claims": claim_verdicts(m, results),
            "results": [r.as_dict() for r in results],
            "proofs": [p.as_dict() for p in collect_proofs(results)],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n")
    return EXIT_OK if passed else EXIT_ERR


def cmd_agent_run(args: argparse.Namespace) -> int:
    """Host step (run by lib/plan.sh after the cluster is healthy, before verify): drive an
    agent-driven module's workbench with a locked-down `claude -p`. No-op (exit 0) for modules
    without an `agent:` block. Returns EXIT_ERR only on infrastructural failure (claude missing /
    timeout / crash); the agent's own verdict is advisory and surfaced later by agent_result."""
    from .agent import run_agent

    mdir = _modules_dir(args.modules_dir) / args.module
    state_dir = Path(args.state_dir) if args.state_dir else _root() / "state" / args.cluster_id
    ok, msg = run_agent(mdir, args.cluster_id, state_dir)
    print(msg, file=sys.stderr if not ok else sys.stdout)
    return EXIT_OK if ok else EXIT_ERR


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
        "repo": args.repo,
    }
    compose = render_cluster(mdirs, ctx, Path(args.out), components_dir=_root() / "components")
    print(f"[render] wrote {compose}", file=sys.stderr)
    return EXIT_OK


def cmd_plan_resolve(args: argparse.Namespace) -> int:
    """Load a plan, gate each module, emit JSON {name, run:[…], skip:[{module,reason}]}."""
    from .models import check_plan_exclusivity, load_plan

    plan = load_plan(_root() / "plans" / f"{args.plan}.yaml")
    # Refuse an unrunnable composition up front, before anything is built or brought up: the
    # failures it causes land on the SIBLING modules, so the plan would otherwise look like a
    # bug in whichever module happened to be reading auth at the wrong moment.
    for problem in check_plan_exclusivity(plan, _modules_dir(args.modules_dir)):
        print(f"FAIL {plan.name}: {problem}", file=sys.stderr)
        return EXIT_ERR
    result: dict = {"name": plan.name, "run": [], "skip": []}
    for m in plan.modules:
        mdir = _modules_dir(args.modules_dir) / m
        mod = load_module(mdir)
        problems = mod.validate_semantics()
        if problems:
            print(f"FAIL {m}: {'; '.join(problems)}", file=sys.stderr)
            return EXIT_ERR
        res = _gate(mod, mdir, args)
        if res.skip:
            result["skip"].append({"module": m, "reason": res.reason})
        else:
            result["run"].append(m)
    print(json.dumps(result))
    return EXIT_OK


def cmd_kube_render(args: argparse.Namespace) -> int:
    """Translate state/<id>/docker-compose.yml into k8s objects: writes kube/manifests.yaml
    and kube/plan.json (harness/kube.py). Refuses a composition with a docker-only unit."""
    from .cluster import read_meta
    from .kube import translate

    state_dir = Path(args.state_dir)
    meta = read_meta(state_dir)
    for key in ("CLUSTER_ID", "FQDN", "PORT", "MODULES"):
        if not meta.get(key):
            raise ValueError(f"{state_dir}/meta.env has no {key}")
    for m in _csv(meta["MODULES"]) or []:
        unit = backend_requirement(_modules_dir(args.modules_dir) / m, _root() / "components", "k8s")
        if unit:
            raise ValueError(f"{unit} cannot run on the k8s backend")
    compose = yaml.safe_load((state_dir / "docker-compose.yml").read_text())
    t = translate(compose, cluster_id=meta["CLUSTER_ID"], state_dir=state_dir,
                  fqdn=meta["FQDN"], port=int(meta["PORT"]), expires_at=args.expires_at)
    out = state_dir / "kube"
    out.mkdir(exist_ok=True)
    (out / "manifests.yaml").write_text(yaml.safe_dump_all(t.objects, sort_keys=False))
    (out / "plan.json").write_text(json.dumps(t.plan, indent=2) + "\n")
    print(f"{out}: {len(t.objects)} objects, {len(t.plan['order'])} waves, "
          f"{len(t.plan['artifacts'])} artifacts, {len(t.plan['local_images'])} local images")
    return EXIT_OK


def cmd_kube_up(args: argparse.Namespace) -> int:
    """Bring a kube-rendered cluster up (harness/kube_apply.py): namespace, secrets, objects,
    then pods wave by wave. Progress on stderr; exit 1 with the reason on failure."""
    from .kube_apply import ApplyError, Kubectl, apply_cluster

    try:
        apply_cluster(Path(args.state_dir), Kubectl(), timeout=args.timeout,
                      log=lambda m: print(f"[kube] {m}", file=sys.stderr))
    except ApplyError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERR
    return EXIT_OK


def cmd_state_pack(args: argparse.Namespace) -> int:
    """Pack what another client needs to manage this cluster (harness/kube_state.py)."""
    from .kube_state import pack
    Path(args.out).write_bytes(pack(Path(args.state_dir)))
    return EXIT_OK


def cmd_state_unpack(args: argparse.Namespace) -> int:
    """Unpack a packed state into a NEW state dir, recording THIS client's connection."""
    from .kube_state import CLIENT_KEYS, unpack
    client = {}
    for kv in args.set:
        k, sep, v = kv.partition("=")
        if not sep or k not in CLIENT_KEYS:
            raise ValueError(f"--set takes one of {', '.join(CLIENT_KEYS)} as KEY=VALUE, got {kv!r}")
        client[k] = v
    unpack(Path(args.input).read_bytes(), Path(args.state_dir), client)
    return EXIT_OK


def cmd_relay(args: argparse.Namespace) -> int:
    """Relay 127.0.0.1:<port> to <host[:port]> until killed (cluster forward, lan mode)."""
    from .relay import run
    run(args.port, args.target)
    return EXIT_OK


def cmd_layer_tar(args: argparse.Namespace) -> int:
    """Write a deterministic image layer (harness/images.py) and print its sha256. The k8s
    backend `crane append`s these, so equal inputs are equal blobs in the shared registry."""
    from .images import dir_entries, file_entry, write_layer

    def pair(spec: str, flag: str) -> tuple[str, str]:
        src, sep, dst = spec.partition("=")
        if not sep or not src or not dst:
            raise ValueError(f"{flag} wants SRC=DEST, got {spec!r}")
        return src, dst

    entries = [file_entry(*pair(f, "--file")) for f in args.file]
    for d in args.dir:
        entries += dir_entries(*pair(d, "--dir"))
    if not entries:
        raise ValueError("nothing to put in the layer (give --file and/or --dir)")
    print(write_layer(entries, args.out))
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    mdir = _modules_dir(args.modules_dir) / args.module
    res = _gate(load_module(mdir), mdir, args)
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

    sar = sub.add_parser("agent-run", help="drive an agent-driven module's workbench (host step)")
    sar.add_argument("module")
    sar.add_argument("--cluster-id", required=True)
    sar.add_argument("--state-dir", help="state/<id>/ (meta.env + where the agent result lands)")
    sar.add_argument("--repo", default="", help="host path to the teleport clone (symmetry with render)")
    sar.set_defaults(fn=cmd_agent_run)

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
    sr.add_argument("--repo", default="",
                    help="host path to the teleport clone; exposed to prebuild.sh as $REPO "
                         "(e.g. the terraform-runner component builds the provider from it)")
    sr.add_argument("--out", required=True)
    sr.set_defaults(fn=cmd_render)

    _no_repo_help = ("this run has no teleport clone (source is --package/--binary), so "
                     "modules whose composition builds from one gate out")
    _backend_help = ("the backend clusters run on (lib/backend.sh); modules whose composition "
                     "declares a different requires_backend gate out")
    sp = sub.add_parser("plan-resolve", help="gate a plan's modules; emit run/skip JSON")
    sp.add_argument("plan")
    sp.add_argument("--features")
    sp.add_argument("--version")
    sp.add_argument("--no-repo", action="store_true", help=_no_repo_help)
    sp.add_argument("--backend", default="docker", help=_backend_help)
    sp.set_defaults(fn=cmd_plan_resolve)

    sg = sub.add_parser("gate", help="feature/version gate; exit 3 == skip")
    sg.add_argument("module")
    sg.add_argument("--features")
    sg.add_argument("--version")
    sg.add_argument("--no-repo", action="store_true", help=_no_repo_help)
    sg.add_argument("--backend", default="docker", help=_backend_help)
    sg.set_defaults(fn=cmd_gate)

    sk = sub.add_parser("kube-render", help="translate a rendered cluster for the k8s backend")
    sk.add_argument("--state-dir", required=True, help="state/<id>/ (render output + meta.env)")
    sk.add_argument("--expires-at", type=int, help="unix time the janitor may delete it after")
    sk.set_defaults(fn=cmd_kube_render)

    su = sub.add_parser("kube-up", help="apply a kube-rendered cluster, wave by wave")
    su.add_argument("--state-dir", required=True)
    su.add_argument("--timeout", type=float, default=600.0,
                    help="seconds to wait for any one dependency condition")
    su.set_defaults(fn=cmd_kube_up)

    spk = sub.add_parser("state-pack", help="pack a cluster's state for another client")
    spk.add_argument("--state-dir", required=True)
    spk.add_argument("--out", required=True)
    spk.set_defaults(fn=cmd_state_pack)

    sun = sub.add_parser("state-unpack", help="unpack a packed state into a new state dir")
    sun.add_argument("--in", dest="input", required=True)
    sun.add_argument("--state-dir", required=True)
    sun.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="this client's connection (KUBECONFIG, KUBE_CONTEXT)")
    sun.set_defaults(fn=cmd_state_unpack)

    srl = sub.add_parser("relay", help="TCP relay 127.0.0.1:<port> -> <host[:port]>")
    srl.add_argument("--port", type=int, required=True)
    srl.add_argument("target")
    srl.set_defaults(fn=cmd_relay)

    sl = sub.add_parser("layer-tar", help="write a deterministic image layer; print its sha256")
    sl.add_argument("--out", required=True)
    sl.add_argument("--file", action="append", default=[], metavar="SRC=DEST",
                    help="a file at DEST in the layer (mode 0755 if executable, else 0644)")
    sl.add_argument("--dir", action="append", default=[], metavar="SRC=PREFIX",
                    help="every file under SRC, placed under PREFIX")
    sl.set_defaults(fn=cmd_layer_tar)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError, ValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERR


if __name__ == "__main__":
    raise SystemExit(main())
