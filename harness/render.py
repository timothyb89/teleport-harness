"""Jinja2 compose renderer — replaces the per-module `render.sh` (envsubst + heredoc)
that duplicated ~90% of the docker-compose across modules.

A module now ships:
  - compose.yml.j2   — `{% extends "base.compose.yml.j2" %}`, fills the services/volumes
                        blocks with just its bots/agents.
  - config/*.j2      — teleport/tbot configs (jinja, was envsubst .tmpl).
  - render.yaml      — (optional) extra template context, e.g. auth_env / agent lists.
  - prebuild.sh      — (optional) imperative pre-step (e.g. build a side image), run with
                       the context exported as UPPER_CASE env vars.
The shared auth+proxy config + compose scaffolding live in harness/templates/.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES = Path(__file__).resolve().parent / "templates"


def _env(module_dir: Path) -> Environment:
    # search order: the module dir, its config/, then the shared templates.
    return Environment(
        loader=FileSystemLoader([str(module_dir), str(module_dir / "config"), str(TEMPLATES)]),
        undefined=StrictUndefined,  # a missing var is an error, not a silent blank
        autoescape=False,           # YAML, not HTML
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _run_prebuild(module_dir: Path, ctx: dict) -> None:
    pre = module_dir / "prebuild.sh"
    if not pre.is_file():
        return
    env = dict(os.environ)
    for k, v in ctx.items():
        if isinstance(v, (str, int)):
            env[k.upper()] = str(v)
    subprocess.run(["bash", str(pre)], check=True, env=env)


def _render_configs(env: Environment, module_dir: Path, ctx: dict, cfg_out: Path) -> None:
    cfg_out.mkdir(parents=True, exist_ok=True)
    provided: set[str] = set()
    cdir = module_dir / "config"
    if cdir.is_dir():
        for tmpl in sorted(cdir.glob("*.j2")):
            name = tmpl.name[:-3]  # strip .j2
            provided.add(name)
            (cfg_out / name).write_text(env.get_template(tmpl.name).render(**ctx))
    # shared auth.yaml unless the module supplies its own
    if "auth.yaml" not in provided:
        (cfg_out / "auth.yaml").write_text(env.get_template("auth.yaml.j2").render(**ctx))


def render_module(module_dir: Path, ctx: dict, out_dir: Path, run_prebuild: bool = True) -> Path:
    """Render a module's configs + docker-compose.yml into out_dir. Returns the compose path.
    run_prebuild=False skips the imperative prebuild.sh (tests render without docker)."""
    module_dir = Path(module_dir)
    out_dir = Path(out_dir)
    ctx = dict(ctx)
    ctx["module_dir"] = str(module_dir)

    rf = module_dir / "render.yaml"
    if rf.is_file():
        extra = yaml.safe_load(rf.read_text()) or {}
        if not isinstance(extra, dict):
            raise ValueError(f"{rf}: must be a mapping")
        ctx.update(extra)

    if run_prebuild:
        _run_prebuild(module_dir, ctx)

    env = _env(module_dir)
    _render_configs(env, module_dir, ctx, out_dir / "config")

    compose = out_dir / "docker-compose.yml"
    compose.write_text(env.get_template("compose.yml.j2").render(**ctx))
    return compose
