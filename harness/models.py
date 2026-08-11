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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .checks import REGISTRY


class Check(BaseModel):
    """One parsed line of a module's declarative `checks:` block."""

    verb: str
    args: list[str]
    raw: str
    lineno: int  # 1-based line within the checks block
    # Set by Module.all_checks(), not by the YAML: which claim this check serves (empty for
    # preconditions and for the flat legacy `checks:` block) and whether it is evidence or
    # scaffolding. Carried through to CheckResult so the report can group by claim.
    claim: str = ""
    role: str = "evidence"  # evidence | precondition
    note: str = ""  # author's plain-language meaning; replaces the verb's msg in the report

    def tagged(self, role: str, claim: str = "") -> "Check":
        return self.model_copy(update={"role": role, "claim": claim})

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
        # A trailing ` # ...` is the check's NOTE — what the report shows in the detail
        # column instead of the verb's own message. Some verbs cannot say anything useful
        # there on their own: `log_count svc ge 1 RESULT foo: PASS` reports "1 match(es) for
        # /RESULT foo: PASS/", which just restates the check. The note lets the author say
        # what the match MEANS. Split on " # " (spaces both sides) so a '#' inside a regex
        # or an argument is untouched.
        note = ""
        head, sep, tail = stripped.partition(" # ")
        if sep:
            stripped, note = head.rstrip(), tail.strip()
        parts = stripped.split()
        checks.append(
            Check(verb=parts[0], args=parts[1:], raw=stripped, lineno=i, note=note)
        )
    return checks


class Claim(BaseModel):
    """A single thing a module asserts about the system, and the checks that establish it.

    The layer the report was missing. A check on its own rarely means anything to a reader
    without context — `resource_field_not …bound_bot_instance_id 000…` is only meaningful
    beside "a real bot bound a key first" and "an apply carrying that nil UUID was accepted".
    The argument is a property of the GROUP, so the group is the thing that gets a name, a
    statement and a rationale.

    Checks nest inside the claim rather than being cross-referenced by id: there is no way to
    reference a claim that does not exist, and no way to leave a check orphaned.
    """

    model_config = ConfigDict(extra="forbid")

    id: str                       # short slug; becomes the report anchor
    statement: str                # what is being claimed, in plain prose
    why: str = ""                 # HOW the checks below establish it — the logical chain
    checks: list[Check] = Field(default_factory=list)
    # MODULE-RELATIVE paths to the things a reader would want to open: the script that drove
    # the mutation, the resource that was applied. Written as they appear in the module
    # (`scripts/mutate.sh`, `config/token.yaml.j2`) and mapped to their bundle locations at
    # report time, so a typo is a validation error rather than a dead link in a shared gist.
    artifacts: list[str] = Field(default_factory=list)

    @field_validator("checks", mode="before")
    @classmethod
    def _parse(cls, v):
        return parse_checks(v) if isinstance(v, str) else v


class Module(BaseModel):
    """A test module's gating metadata + parsed verification checks.

    Two shapes are supported. The flat `checks:` block is the original and still works
    unchanged. `preconditions:` + `claims:` is the structured form: it says WHAT the module
    proves and WHY the evidence suffices, which is what a reader without context needs and
    what a bare list of verdicts cannot convey. Modules may use either or both.
    """

    model_config = ConfigDict(extra="forbid")  # typo'd keys are errors, not silently ignored

    name: str
    description: str = ""
    provides_feature: str | None = None
    requires_features: list[str] = Field(default_factory=list)
    min_version: str | None = None
    checks: list[Check] = Field(default_factory=list)
    # A module that DISRUPTS the shared cluster — today, one that restarts auth to exercise a
    # startup-only code path. It cannot share a cluster: while it runs, auth is unavailable
    # every ~30s, so any sibling that reads or writes through auth sees failed calls and a
    # moving target. Composing one into a multi-module plan is refused outright rather than
    # left to surface as unexplained sibling failures — which is exactly what it did before
    # this existed (a mutator's applies silently failed against a restarting auth, and only a
    # barrier timeout hinted at why).
    exclusive: bool = False
    # Scaffolding, not evidence: these establish that the scenario was actually set up
    # (a bot really joined, an identity was really written). A failing PRECONDITION means the
    # claims below were never exercised — untested, which is not the same as disproven — so
    # the report says so instead of reporting a claim failure it cannot support.
    preconditions: list[Check] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)

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

    def all_checks(self) -> list[Check]:
        """Every check to run, in report order, tagged with its role and owning claim.

        Preconditions first: they are what makes the claims meaningful, and running them
        first means a broken setup surfaces before a pile of claim failures it caused.
        """
        out: list[Check] = []
        for chk in self.preconditions:
            out.append(chk.tagged(role="precondition"))
        for claim in self.claims:
            for chk in claim.checks:
                out.append(chk.tagged(role="evidence", claim=claim.id))
        out.extend(chk.tagged(role="evidence") for chk in self.checks)
        return out

    def validate_semantics(self) -> list[str]:
        """Problems beyond schema/type validity: bad verbs, arity, missing files."""
        problems: list[str] = []
        for chk in self.checks:
            for msg in chk.validate_against_registry():
                problems.append(f"checks[{chk.lineno}] {msg}: '{chk.raw}'")
        for chk in self.preconditions:
            for msg in chk.validate_against_registry():
                problems.append(f"preconditions[{chk.lineno}] {msg}: '{chk.raw}'")
        seen: set[str] = set()
        for claim in self.claims:
            if claim.id in seen:
                problems.append(f"claims: duplicate id '{claim.id}'")
            seen.add(claim.id)
            if not claim.checks:
                # A claim with no evidence would render as vacuously PROVEN, which is worse
                # than not making the claim at all.
                problems.append(f"claims[{claim.id}]: has no checks (a claim needs evidence)")
            for chk in claim.checks:
                for msg in chk.validate_against_registry():
                    problems.append(f"claims[{claim.id}][{chk.lineno}] {msg}: '{chk.raw}'")
            for art in claim.artifacts:
                if self.path is not None and not (self.path / art).is_file():
                    problems.append(
                        f"claims[{claim.id}]: artifact '{art}' not found in the module "
                        f"(paths are module-relative, e.g. scripts/mutate.sh)")
        if not (self.has_compose_template or self.has_render_sh):
            problems.append("missing services.yml.j2 (or a legacy render.sh)")
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
    precond_block = raw.pop("preconditions", None)
    mod = Module(**raw, checks=parse_checks(checks_block),
                 preconditions=parse_checks(precond_block))
    mod.path = module_dir
    mod.has_checks_sh = (module_dir / "checks.sh").is_file()
    mod.has_render_sh = (module_dir / "render.sh").is_file()
    mod.has_compose_template = (module_dir / "services.yml.j2").is_file()
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


class Plan(BaseModel):
    """A multi-module plan (plans/<name>.yaml): several modules composed into ONE
    cluster, each independently gated, verified + reported together."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    modules: list[str] = Field(min_length=1)

    path: Path | None = Field(default=None, exclude=True)


def load_plan(plan_path: Path) -> Plan:
    """Load + parse a plans/<name>.yaml. Raises on schema errors."""
    if not plan_path.is_file():
        raise FileNotFoundError(f"no such plan: {plan_path}")
    raw = yaml.safe_load(plan_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{plan_path}: top level must be a mapping")
    plan = Plan(**raw)
    plan.path = plan_path
    if plan.name != plan_path.stem:
        raise ValueError(f"{plan_path}: name '{plan.name}' != file '{plan_path.stem}'")
    return plan


def check_plan_exclusivity(plan: Plan, modules_dir: Path) -> list[str]:
    """Problems from composing an `exclusive: true` module with siblings (see Module)."""
    problems: list[str] = []
    if len(plan.modules) < 2:
        return problems
    for name in plan.modules:
        d = modules_dir / name
        if not (d / "module.yaml").is_file():
            continue
        try:
            if load_module(d).exclusive:
                others = [m for m in plan.modules if m != name]
                problems.append(
                    f"module '{name}' is exclusive (it disrupts the shared cluster — e.g. by "
                    f"restarting auth) and cannot be composed with {others}. Run it as its own "
                    f"plan/module; siblings would see failed auth calls and a moving target.")
        except (ValidationError, ValueError):
            continue  # a broken module surfaces via validate, not here
    return problems


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
