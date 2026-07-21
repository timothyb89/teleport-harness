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

import json
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
    """Render/copy a unit's bootstrap resources into $OUT/bootstrap, prefixed by origin.
    A bootstrap/hooks/*.sh[.j2] subdir becomes $OUT/bootstrap/hooks/ (local-admin scripts
    the shared auth-entrypoint runs after static resources — e.g. runtime token creation)."""
    bdir = unit_dir / "bootstrap"
    if not bdir.is_dir():
        return
    env = _env(bdir, bdir / "hooks", TEMPLATES)
    for f in sorted(bdir.iterdir()):
        if f.name.endswith(".yaml.j2"):
            (boot_out / f"{origin}__{f.name[:-3]}").write_text(_render_str(env, f.name, ctx))
        elif f.name.endswith(".yaml"):
            (boot_out / f"{origin}__{f.name}").write_text(f.read_text())

    hdir = bdir / "hooks"
    if hdir.is_dir():
        hooks_out = boot_out / "hooks"
        hooks_out.mkdir(exist_ok=True)
        for f in sorted(hdir.iterdir()):
            if f.name.endswith(".sh.j2"):
                (hooks_out / f"{origin}__{f.name[:-3]}").write_text(_render_str(env, f.name, ctx))
            elif f.name.endswith(".sh"):
                (hooks_out / f"{origin}__{f.name}").write_text(f.read_text())


def _collect_apply_on_startup(unit_dir: Path, ctx: dict, apply_out: Path, origin: str) -> None:
    """Render/copy a unit's `apply_on_startup/*.yaml[.j2]` into $OUT/apply-on-startup.

    UNLIKE bootstrap (which the shared auth-entrypoint applies via LOCAL-ADMIN `tctl create`,
    the user-facing path), these resources are handed to `teleport start --apply-on-startup`
    so teleport itself applies them during init on EVERY startup — the code path being
    exercised. The shared entrypoint concatenates every collected file into one multi-doc
    YAML and passes it via the flag; if a unit contributes none, the flag is omitted."""
    adir = unit_dir / "apply_on_startup"
    if not adir.is_dir():
        return
    env = _env(adir, TEMPLATES)
    for f in sorted(adir.iterdir()):
        if f.name.endswith(".yaml.j2"):
            (apply_out / f"{origin}__{f.name[:-3]}").write_text(_render_str(env, f.name, ctx))
        elif f.name.endswith(".yaml"):
            (apply_out / f"{origin}__{f.name}").write_text(f.read_text())


def _merge_fragment(compose: dict, env: Environment, unit_dir: Path, ctx: dict,
                    origins: dict[str, str], label: str) -> None:
    frag_text = _render_str(env, "services.yml.j2", ctx)
    frag = yaml.safe_load(frag_text) or {}
    for svc, spec in (frag.get("services") or {}).items():
        if svc in compose["services"]:
            raise ValueError(f"service '{svc}' defined twice (from {unit_dir.name})")
        compose["services"][svc] = spec
        origins[svc] = label
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
    # Always present (even if empty) so the base compose can unconditionally mount it;
    # the entrypoint globs it and only passes --apply-on-startup when it's non-empty.
    apply_out = out_dir / "apply-on-startup"
    apply_out.mkdir(parents=True, exist_ok=True)

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

    # label each unit for provenance in setup.json ("proof = the source that made it").
    unit_labels = {d: f"component:{d.name}" for d in component_dirs}
    unit_labels.update({m: f"module:{m.name}" for m in module_dirs})
    units = [(d, comp_rv[d]) for d in component_dirs] + [(m, module_rv[m]) for m in module_dirs]

    # Merge auth_env across every unit; render the base with it.
    merged_auth_env: dict = {}
    for _, rv in units:
        merged_auth_env.update(rv.get("auth_env", {}) or {})
    base_ctx = {**base_ctx, "auth_env": merged_auth_env, "shared_scripts": str(SHARED_SCRIPTS)}
    compose = yaml.safe_load(_render_str(_env(TEMPLATES), "base.compose.yml.j2", base_ctx))
    compose.setdefault("services", {})
    compose.setdefault("volumes", {})

    origins: dict[str, str] = {svc: "base" for svc in compose["services"]}
    bots: list[dict] = []
    for unit_dir, rv in units:
        ctx = {**base_ctx, **rv, "module_dir": str(unit_dir)}
        if run_prebuild:
            _run_prebuild(unit_dir, ctx)
        env = _env(unit_dir, unit_dir / "config", TEMPLATES)
        _merge_fragment(compose, env, unit_dir, ctx, origins, unit_labels[unit_dir])
        _render_unit_configs(unit_dir, ctx, out_dir / "config")
        _collect_bootstrap(unit_dir, ctx, boot_out, unit_dir.name)
        _collect_apply_on_startup(unit_dir, ctx, apply_out, unit_dir.name)
        for b in (rv.get("bots", []) or []):
            bots.append({**b, "_origin": unit_labels[unit_dir]})

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
        # token may be empty: the bot is created, then authorized by a separately-created
        # join token (e.g. a kubernetes-method token whose bot_name matches).
        lines.append(f"{b['name']}\t{roles}\t{b.get('token', '')}")
    (boot_out / "bots.manifest").write_text("\n".join(lines) + ("\n" if lines else ""))

    compose_path = out_dir / "docker-compose.yml"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False, default_flow_style=False))

    # setup.json — provenance manifest the report renders directly (no retroactive
    # scraping). Each entry links to the rendered source that produced it.
    _write_setup(out_dir, boot_out, apply_out, compose, origins, bots,
                 [c.name for c in component_dirs], [m.name for m in module_dirs])
    return compose_path


def _compact(v) -> str:
    """One-line human summary of an RBAC allow value (list/dict/scalar)."""
    if isinstance(v, list):
        return "[" + ", ".join(_compact(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_compact(val)}" for k, val in v.items()) + "}"
    return str(v)


def _summarize_allow(spec: dict) -> str:
    """Compact one-line summary of a role's `spec.allow` — the 'special permissions'."""
    allow = (spec or {}).get("allow") or {}
    if not allow:
        return ""
    return "; ".join(f"{k}={_compact(v)}" for k, v in allow.items())


def _summarize_role(doc: dict) -> str:
    """Permissions summary for either a classic `role` (grants under `spec.allow`) or a
    `scoped_role` (grants under `spec.ssh`/`spec.kubernetes`/… + `assignable_scopes`, with
    no `allow` block). Falls back to compacting the whole spec so nothing is lost."""
    spec = doc.get("spec") or {}
    if spec.get("allow"):
        return _summarize_allow(spec)
    return "; ".join(f"{k}={_compact(v)}" for k, v in spec.items()) if spec else ""


def _docs(path: Path):
    """Yield each YAML document in a bootstrap file (resources may be multi-doc)."""
    try:
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict):
                yield doc
    except yaml.YAMLError:
        return


def _write_setup(out_dir: Path, boot_out: Path, apply_out: Path, compose: dict,
                 origins: dict[str, str], bots: list[dict], components: list[str],
                 modules: list[str]) -> None:
    services = [
        {"name": name, "image": (spec or {}).get("image", "?"), "origin": origins.get(name, "")}
        for name, spec in (compose.get("services") or {}).items()
    ]

    roles, tokens, token_idx, boot_bots = [], [], {}, {}
    # Resources applied two ways: bootstrap (LOCAL-ADMIN `tctl create` via the entrypoint) and
    # apply-on-startup (`teleport start --apply-on-startup`, applied by teleport on every boot).
    # Both feed the same tables; `apply_on_startup: true` marks the latter so the report can note it.
    sources = ([("rendered/bootstrap", f, False) for f in sorted(boot_out.glob("*.yaml"))]
               + [("rendered/apply-on-startup", f, True) for f in sorted(apply_out.glob("*.yaml"))])
    for prefix, f, on_startup in sources:
        origin = f.name.split("__", 1)[0] if "__" in f.name else ""
        src = f"{prefix}/{f.name}"
        for doc in _docs(f):
            kind = doc.get("kind", "")
            meta = doc.get("metadata") or {}
            name = meta.get("name", "?")
            spec = doc.get("spec") or {}
            if kind in ("role", "scoped_role"):
                roles.append({"name": name, "kind": kind, "description": meta.get("description", ""),
                              "allow": _summarize_role(doc), "scope": doc.get("scope", ""),
                              "origin": origin, "source": src})
            elif kind in ("token", "scoped_token"):
                jm = spec.get("join_method", "")
                tokens.append({"name": name, "kind": kind, "join_method": jm,
                               "origin": origin, "source": src, "apply_on_startup": on_startup})
                token_idx[name] = (jm, src)
            elif kind in ("bot", "scoped_bot"):
                boot_bots[name] = {"roles": spec.get("roles") or [], "source": src, "origin": origin}

    # tbot outputs/services parsed from rendered configs (what a bot is configured to produce).
    configs = []
    for f in sorted((out_dir / "config").glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict) or not ("onboarding" in doc or "outputs" in doc):
            continue  # not a tbot config
        configs.append({
            "file": f.name, "source": f"rendered/config/{f.name}",
            "join_method": (doc.get("onboarding") or {}).get("join_method", ""),
            "outputs": [o.get("type", "output") for o in (doc.get("outputs") or []) if isinstance(o, dict)],
        })

    def _config_join_method(bot_name: str) -> str:
        """A bot's join method may live in its tbot config's onboarding (when the
        bootstrap token is empty, e.g. a runtime-created generic_oidc provision token)."""
        for cfg in configs:
            if bot_name in cfg["file"] and cfg["join_method"]:
                return cfg["join_method"]
        return ""

    bot_entries, seen = [], set()
    for b in bots:
        r = b["roles"]
        r = r if isinstance(r, list) else [x for x in str(r).split(",") if x]
        tok = b.get("token", "")
        jm, tsrc = token_idx.get(tok, ("token" if tok else "", ""))
        jm = jm or _config_join_method(b["name"])
        boot = boot_bots.get(b["name"], {})
        bot_entries.append({"name": b["name"], "roles": r, "token": tok, "join_method": jm,
                            "origin": b.get("_origin", ""), "source": boot.get("source", tsrc)})
        seen.add(b["name"])
    for name, boot in boot_bots.items():  # scoped_bot resources aren't in the bots manifest
        if name not in seen:
            bot_entries.append({"name": name, "roles": boot["roles"], "token": "",
                                "join_method": _config_join_method(name),
                                "origin": f"module:{boot['origin']}", "source": boot["source"]})

    setup = {"modules": modules, "components": components, "services": services,
             "roles": roles, "tokens": tokens, "bots": bot_entries, "configs": configs}
    (out_dir / "setup.json").write_text(json.dumps(setup, indent=2) + "\n")


def render_module(module_dir: Path, ctx: dict, out_dir: Path, run_prebuild: bool = True) -> Path:
    """Single-module convenience wrapper (a 1-module cluster + its declared components)."""
    module_dir = Path(module_dir)
    return render_cluster(
        [module_dir], ctx, out_dir,
        components_dir=module_dir.parent.parent / "components",
        run_prebuild=run_prebuild,
    )
