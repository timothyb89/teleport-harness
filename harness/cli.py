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
