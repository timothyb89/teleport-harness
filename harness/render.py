"""Jinja2 cluster renderer — composes ONE docker-compose from a base scaffold +
shared components + one-or-more module fragments, so multiple modules can share a
cluster (and shared components like the oidc-server).

Layout it renders from:
  harness/templates/base.compose.yml.j2   auth+proxy service, networks, base volumes
  harness/templates/auth.yaml.j2          shared teleport config
  harness/templates/scripts/…             shared auth-entrypoint
  components/<name>/services.yml.j2        a shared service fragment (services: / volumes:)
  modules/<name>/services.yml.j2           a module's bots/agents fragment
  {components,modules}/<name>/config/*.j2  configs rendered into $OUT/config
  {components,modules}/<name>/bootstrap/*  roles/tokens (.yaml copied, .yaml.j2 rendered)
  {components,modules}/<name>/render.yaml  context: components[], auth_env{}, bots[], vars…
  {components,modules}/<name>/prebuild.sh  optional imperative pre-step (build a side image)

Composition = deep-merge each fragment's `services`/`volumes` into the base, union all
`auth_env` onto the auth service, and collect every bootstrap resource + `bots` entry into
$OUT/bootstrap (a bots.manifest) which the shared auth-entrypoint applies.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

_HARNESS = Path(__file__).resolve().parent
TEMPLATES = _HARNESS / "templates"
SHARED_SCRIPTS = TEMPLATES / "scripts"


def _load_render_yaml(unit_dir: Path) -> dict:
    f = unit_dir / "render.yaml"
    if not f.is_file():
        return {}
    data = yaml.safe_load(f.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{f}: must be a mapping")
    return data


def _env(*search: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader([str(p) for p in search]),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_str(env: Environment, template_name: str, ctx: dict) -> str:
    return env.get_template(template_name).render(**ctx)


def _run_prebuild(unit_dir: Path, ctx: dict) -> None:
    pre = unit_dir / "prebuild.sh"
    if not pre.is_file():
        return
    env = dict(os.environ)
    for k, v in ctx.items():
        if isinstance(v, (str, int)):
            env[k.upper()] = str(v)
    subprocess.run(["bash", str(pre)], check=True, env=env)


def _render_unit_configs(unit_dir: Path, ctx: dict, cfg_out: Path) -> None:
    cdir = unit_dir / "config"
    if not cdir.is_dir():
        return
    env = _env(cdir, TEMPLATES)
    for tmpl in sorted(cdir.glob("*.j2")):
        (cfg_out / tmpl.name[:-3]).write_text(_render_str(env, tmpl.name, ctx))


def _collect_bootstrap(unit_dir: Path, ctx: dict, boot_out: Path, origin: str) -> None:
    """Render/copy a unit's bootstrap resources into $OUT/bootstrap, prefixed by origin."""
    bdir = unit_dir / "bootstrap"
    if not bdir.is_dir():
        return
    env = _env(bdir, TEMPLATES)
    for f in sorted(bdir.iterdir()):
        if f.name.endswith(".yaml.j2"):
            (boot_out / f"{origin}__{f.name[:-3]}").write_text(_render_str(env, f.name, ctx))
        elif f.name.endswith(".yaml"):
            (boot_out / f"{origin}__{f.name}").write_text(f.read_text())


def _merge_fragment(compose: dict, env: Environment, unit_dir: Path, ctx: dict) -> None:
    frag_text = _render_str(env, "services.yml.j2", ctx)
    frag = yaml.safe_load(frag_text) or {}
    for svc, spec in (frag.get("services") or {}).items():
        if svc in compose["services"]:
            raise ValueError(f"service '{svc}' defined twice (from {unit_dir.name})")
        compose["services"][svc] = spec
    for vol, spec in (frag.get("volumes") or {}).items():
        compose["volumes"].setdefault(vol, spec)


def render_cluster(
    module_dirs: list[Path],
    base_ctx: dict,
    out_dir: Path,
    components_dir: Path,
    run_prebuild: bool = True,
) -> Path:
    """Compose + write a cluster from the given modules (+ their declared components)."""
    out_dir = Path(out_dir)
    (out_dir / "config").mkdir(parents=True, exist_ok=True)
    boot_out = out_dir / "bootstrap"
    boot_out.mkdir(parents=True, exist_ok=True)

    module_dirs = [Path(m) for m in module_dirs]

    # Resolve components (deduped, preserving first-seen order) from each module's render.yaml.
    module_rv = {m: _load_render_yaml(m) for m in module_dirs}
    comp_names: list[str] = []
    for rv in module_rv.values():
        for c in rv.get("components", []) or []:
            if c not in comp_names:
                comp_names.append(c)
    component_dirs = [components_dir / c for c in comp_names]
    for c, d in zip(comp_names, component_dirs):
        if not d.is_dir():
            raise ValueError(f"unknown component '{c}' (no {d})")
    comp_rv = {d: _load_render_yaml(d) for d in component_dirs}

    units = [(d, comp_rv[d]) for d in component_dirs] + [(m, module_rv[m]) for m in module_dirs]

    # Merge auth_env across every unit; render the base with it.
    merged_auth_env: dict = {}
    for _, rv in units:
        merged_auth_env.update(rv.get("auth_env", {}) or {})
    base_ctx = {**base_ctx, "auth_env": merged_auth_env, "shared_scripts": str(SHARED_SCRIPTS)}
    compose = yaml.safe_load(_render_str(_env(TEMPLATES), "base.compose.yml.j2", base_ctx))
    compose.setdefault("services", {})
    compose.setdefault("volumes", {})

    bots: list[dict] = []
    for unit_dir, rv in units:
        ctx = {**base_ctx, **rv, "module_dir": str(unit_dir)}
        if run_prebuild:
            _run_prebuild(unit_dir, ctx)
        env = _env(unit_dir, unit_dir / "config", TEMPLATES)
        _merge_fragment(compose, env, unit_dir, ctx)
        _render_unit_configs(unit_dir, ctx, out_dir / "config")
        _collect_bootstrap(unit_dir, ctx, boot_out, unit_dir.name)
        bots.extend(rv.get("bots", []) or [])

    # Shared teleport auth config (a unit may override via its own config/auth.yaml.j2).
    if not (out_dir / "config" / "auth.yaml").is_file():
        (out_dir / "config" / "auth.yaml").write_text(
            _render_str(_env(TEMPLATES), "auth.yaml.j2", base_ctx)
        )

    # bots.manifest: name<TAB>roles<TAB>token  (roles may be a list or comma string).
    lines = []
    for b in bots:
        roles = b["roles"]
        roles = ",".join(roles) if isinstance(roles, list) else str(roles)
        lines.append(f"{b['name']}\t{roles}\t{b['token']}")
    (boot_out / "bots.manifest").write_text("\n".join(lines) + ("\n" if lines else ""))

    compose_path = out_dir / "docker-compose.yml"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False, default_flow_style=False))
    return compose_path


def render_module(module_dir: Path, ctx: dict, out_dir: Path, run_prebuild: bool = True) -> Path:
    """Single-module convenience wrapper (a 1-module cluster + its declared components)."""
    module_dir = Path(module_dir)
    return render_cluster(
        [module_dir], ctx, out_dir,
        components_dir=module_dir.parent.parent / "components",
        run_prebuild=run_prebuild,
    )
