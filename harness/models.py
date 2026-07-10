"""Typed models for `modules/<name>/module.yaml`, loaded with a real YAML parser
(replacing the grep/sed/awk extraction in lib/plan.sh + lib/verify.sh).

A `Module` bundles the gating metadata and the parsed `checks:` block. Loading is
strict: unknown top-level keys, bad types, or malformed check lines surface as
errors at load time instead of failing deep in an 8x8s verification retry loop.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .checks import REGISTRY


class Check(BaseModel):
    """One parsed line of a module's declarative `checks:` block."""

    verb: str
    args: list[str]
    raw: str
    lineno: int  # 1-based line within the checks block

    def validate_against_registry(self) -> list[str]:
        """Return human-readable problems (empty == ok)."""
        spec = REGISTRY.get(self.verb)
        if spec is None:
            known = ", ".join(sorted(REGISTRY))
            return [f"unknown check verb '{self.verb}' (known: {known})"]
        if not spec.arity_ok(len(self.args)):
            return [
                f"'{self.verb}' got {len(self.args)} arg(s); usage: {spec.usage}"
            ]
        return []


def parse_checks(block: str | None) -> list[Check]:
    """Parse a `checks:` literal block into Check rows.

    Mirrors lib/verify.sh's runtime split exactly: left-trim, skip blank and
    '#'-comment lines, then whitespace-split (no shell quote handling — a verb
    like `log_contains` rejoins its trailing args into one regex, spaces and all).
    """
    checks: list[Check] = []
    if not block:
        return checks
    for i, line in enumerate(block.splitlines(), start=1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        checks.append(
            Check(verb=parts[0], args=parts[1:], raw=stripped, lineno=i)
        )
    return checks


class Module(BaseModel):
    """A test module's gating metadata + parsed verification checks."""

    model_config = ConfigDict(extra="forbid")  # typo'd keys are errors, not silently ignored

    name: str
    description: str = ""
    provides_feature: str | None = None
    requires_features: list[str] = Field(default_factory=list)
    min_version: str | None = None
    checks: list[Check] = Field(default_factory=list)

    # populated by load_module, not from YAML
    path: Path | None = Field(default=None, exclude=True)
    has_checks_sh: bool = Field(default=False, exclude=True)
    has_render_sh: bool = Field(default=False, exclude=True)
    has_compose_template: bool = Field(default=False, exclude=True)

    @field_validator("min_version")
    @classmethod
    def _check_version(cls, v: str | None) -> str | None:
        if v is not None and version_num(v) is None:
            raise ValueError(f"min_version '{v}' is not a vNN[.x.y] version")
        return v

    def validate_semantics(self) -> list[str]:
        """Problems beyond schema/type validity: bad verbs, arity, missing files."""
        problems: list[str] = []
        for chk in self.checks:
            for msg in chk.validate_against_registry():
                problems.append(f"checks[{chk.lineno}] {msg}: '{chk.raw}'")
        if not (self.has_compose_template or self.has_render_sh):
            problems.append("missing compose.yml.j2 (or a legacy render.sh)")
        return problems


_VER_RE = re.compile(r"^v?(\d+)(?:\.|$)")


def version_num(v: str | None) -> int | None:
    """v18 / v18.2.1 -> 18 ; None/'' or unparseable -> None. (was `_vnum` in bash)"""
    if not v:
        return None
    m = _VER_RE.match(v.strip())
    return int(m.group(1)) if m else None


def load_module(module_dir: Path) -> Module:
    """Load + parse modules/<name>/module.yaml. Raises on schema errors."""
    yaml_path = module_dir / "module.yaml"
    if not yaml_path.is_file():
        raise FileNotFoundError(f"no module.yaml in {module_dir}")
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{yaml_path}: top level must be a mapping")

    checks_block = raw.pop("checks", None)
    mod = Module(**raw, checks=parse_checks(checks_block))
    mod.path = module_dir
    mod.has_checks_sh = (module_dir / "checks.sh").is_file()
    mod.has_render_sh = (module_dir / "render.sh").is_file()
    mod.has_compose_template = (module_dir / "compose.yml.j2").is_file()
    # keep the declared name aligned with the directory name
    if mod.name != module_dir.name:
        raise ValueError(
            f"{yaml_path}: name '{mod.name}' != directory '{module_dir.name}'"
        )
    return mod


def discover_modules(modules_dir: Path) -> list[Module]:
    """Load every modules/<name>/ that has a module.yaml. Raises on the first bad one."""
    out: list[Module] = []
    for d in sorted(p for p in modules_dir.iterdir() if p.is_dir()):
        if (d / "module.yaml").is_file():
            out.append(load_module(d))
    return out


class GateResult(BaseModel):
    """Outcome of feature/version gating for a run."""

    skip: bool
    reason: str = ""


def gate(
    module: Module,
    features: list[str] | None,
    version: str | None,
) -> GateResult:
    """Decide whether to run `module` given the target's features/version.

    Mirrors lib/plan.sh: if `features` is None the caller warns and assumes the
    target provides everything (no skip). A missing required feature or a version
    below min_version => skip with a reason.
    """
    if features is not None:
        have = set(features)
        for feat in module.requires_features:
            if feat not in have:
                return GateResult(skip=True, reason=f"target lacks feature '{feat}'")
    if version and module.min_version:
        tv, mv = version_num(version), version_num(module.min_version)
        if tv is not None and mv is not None and tv < mv:
            return GateResult(
                skip=True,
                reason=f"target version {version} < module min_version {module.min_version}",
            )
    return GateResult(skip=False)
