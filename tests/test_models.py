"""Unit tests for the harness model + gating layer — the correctness bar that
did not exist while this logic lived in grep/sed/awk."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.models import (
    Module,
    discover_modules,
    gate,
    load_module,
    parse_checks,
    repo_requirement,
    version_num,
)

REPO = Path(__file__).resolve().parent.parent
MODULES = REPO / "modules"


# ---- version parsing (was `_vnum`) ------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [("v18", 18), ("v18.2.1", 18), ("18", 18), ("v0", 0), ("", None), (None, None),
     ("main", None), ("vX", None)],
)
def test_version_num(raw, expected):
    assert version_num(raw) == expected


# ---- checks block parsing (was the awk dedent in verify.sh) -----------------
def test_parse_checks_skips_comments_and_blanks():
    block = "# a comment\n\nnode_present agent-static\n  # indented comment\nnode_absent agent-deny\n"
    checks = parse_checks(block)
    assert [c.verb for c in checks] == ["node_present", "node_absent"]
    assert checks[0].args == ["agent-static"]


def test_parse_checks_keeps_regex_args_split():
    # log_contains rejoins trailing args into one regex at runtime; parsing keeps them split.
    block = "log_contains agent-deny unable to (join via|validate) generic_oidc|denied\n"
    (chk,) = parse_checks(block)
    assert chk.verb == "log_contains"
    assert chk.args[0] == "agent-deny"
    assert " ".join(chk.args[1:]) == "unable to (join via|validate) generic_oidc|denied"


def test_parse_checks_none():
    assert parse_checks(None) == []


# ---- semantic validation ----------------------------------------------------
def test_unknown_verb_is_flagged():
    m = Module(name="x", checks=parse_checks("frobnicate foo\n"), has_render_sh=True)
    problems = m.validate_semantics()
    assert any("unknown check verb 'frobnicate'" in p for p in problems)


def test_bad_arity_is_flagged():
    m = Module(name="x", checks=parse_checks("node_scope only-one-arg\n"), has_render_sh=True)
    problems = m.validate_semantics()
    assert any("node_scope" in p and "usage" in p for p in problems)


def test_variadic_verb_ok():
    m = Module(name="x", checks=parse_checks("log_contains c a|b|c d\n"), has_render_sh=True)
    assert m.validate_semantics() == []


def test_missing_render_sh_flagged():
    m = Module(name="x", checks=[], has_render_sh=False)
    assert any("render.sh" in p for p in m.validate_semantics())


def test_extra_yaml_key_rejected():
    with pytest.raises(ValidationError):
        Module(name="x", bogus_key=1)


def test_bad_min_version_rejected():
    with pytest.raises(ValidationError):
        Module(name="x", min_version="notaversion")


# ---- gating (was the inline logic in plan.sh) -------------------------------
def _mod(**kw):
    kw.setdefault("name", "m")
    kw.setdefault("has_render_sh", True)
    return Module(**kw)


def test_gate_missing_feature_skips():
    m = _mod(requires_features=["generic_oidc"])
    res = gate(m, features=["something_else"], version=None)
    assert res.skip and "generic_oidc" in res.reason


def test_gate_feature_present_runs():
    m = _mod(requires_features=["generic_oidc"])
    assert not gate(m, features=["generic_oidc", "x"], version=None).skip


def test_gate_no_features_assumes_provided():
    m = _mod(requires_features=["generic_oidc"])
    assert not gate(m, features=None, version=None).skip


def test_gate_version_below_min_skips():
    m = _mod(min_version="v18")
    res = gate(m, features=None, version="v17")
    assert res.skip and "v17" in res.reason


def test_gate_version_at_or_above_min_runs():
    m = _mod(min_version="v18")
    assert not gate(m, features=None, version="v18").skip
    assert not gate(m, features=None, version="v19.1.0").skip


# ---- real modules on disk must all load + validate cleanly ------------------
def test_all_shipped_modules_valid():
    mods = discover_modules(MODULES)
    assert {m.name for m in mods} >= {"generic_oidc", "tbot", "bound_keypair"}
    for m in mods:
        assert m.validate_semantics() == [], f"{m.name}: {m.validate_semantics()}"


def test_shipped_module_gating_matches_yaml():
    oidc = load_module(MODULES / "generic_oidc")
    assert oidc.provides_feature == "generic_oidc"
    assert oidc.requires_features == ["generic_oidc"]
    assert oidc.min_version == "v18"


# ---- claims + preconditions -------------------------------------------------
# The structured form: a module says WHAT it proves and WHY the evidence establishes it,
# instead of emitting a flat list of verdicts a reader has to reverse-engineer.
_CLAIM_YAML = """
name: demo
description: demo
claims:
  - id: preserved
    statement: status survives an update
    why: a bot bound a key first, then an accepted update left it unchanged
    checks: |
      resource_field token/t status.x
      log_count svc ge 1 RESULT foo: PASS
  - id: honored
    statement: status is honored on create
    checks: |
      resource_field token/u status.y
preconditions: |
  bot_joined b bound_keypair
"""


def _demo_module(tmp_path: Path, body: str) -> Module:
    d = tmp_path / "demo"
    d.mkdir()
    (d / "module.yaml").write_text(body)
    (d / "services.yml.j2").write_text("services: {}\n")
    return load_module(d)


def test_claims_parse_with_nested_checks(tmp_path):
    m = _demo_module(tmp_path, _CLAIM_YAML)
    assert [c.id for c in m.claims] == ["preserved", "honored"]
    assert m.claims[0].why.startswith("a bot bound a key")
    assert len(m.claims[0].checks) == 2
    assert m.validate_semantics() == []


def test_all_checks_tags_role_and_claim_in_report_order(tmp_path):
    m = _demo_module(tmp_path, _CLAIM_YAML)
    got = [(c.verb, c.role, c.claim) for c in m.all_checks()]
    # preconditions first — a broken setup should surface before the failures it caused
    assert got == [
        ("bot_joined", "precondition", ""),
        ("resource_field", "evidence", "preserved"),
        ("log_count", "evidence", "preserved"),
        ("resource_field", "evidence", "honored"),
    ]


def test_claim_without_checks_is_rejected(tmp_path):
    # would render as vacuously PROVEN, which is worse than not claiming it at all
    m = _demo_module(tmp_path, "name: demo\nclaims:\n  - id: empty\n    statement: nothing\n")
    assert any("has no checks" in p for p in m.validate_semantics())


def test_duplicate_claim_ids_rejected(tmp_path):
    body = ("name: demo\nclaims:\n"
            "  - {id: dup, statement: a, checks: 'node_present x'}\n"
            "  - {id: dup, statement: b, checks: 'node_present y'}\n")
    assert any("duplicate id 'dup'" in p for p in _demo_module(tmp_path, body).validate_semantics())


def test_bad_verb_inside_a_claim_is_flagged_with_its_claim_id(tmp_path):
    body = "name: demo\nclaims:\n  - {id: c1, statement: s, checks: 'nope_verb x'}\n"
    problems = _demo_module(tmp_path, body).validate_semantics()
    assert any("claims[c1]" in p and "unknown check verb" in p for p in problems)


def test_legacy_flat_checks_still_work_and_stay_ungrouped(tmp_path):
    m = _demo_module(tmp_path, "name: demo\nchecks: |\n  node_present agent\n")
    assert not m.claims
    assert [(c.role, c.claim) for c in m.all_checks()] == [("evidence", "")]


# ---- source gating: no clone (a --package/--binary run) --------------------------
# Modules whose composition BUILDS from the teleport clone (the terraform provider, the
# k8s operator, the docs tree) cannot run against a prebuilt package or binary. They gate
# out with a reason rather than failing deep in render — see harness/models.repo_requirement.
COMPONENTS = REPO / "components"


def test_repo_requirement_direct_on_the_module():
    # docs_bound_keypair's workbench mounts {{ repo }}/docs
    assert repo_requirement(MODULES / "docs_bound_keypair", COMPONENTS) == "module docs_bound_keypair"


def test_repo_requirement_inherited_from_a_component():
    # terraform_bot itself says nothing; terraform-runner's prebuild.sh builds from $REPO
    assert repo_requirement(MODULES / "terraform_bot", COMPONENTS) == "component terraform-runner"
    assert repo_requirement(MODULES / "operator_generic_oidc", COMPONENTS) == "component k8s-runner"


def test_repo_requirement_absent_for_a_plain_module():
    for name in ("tbot", "bound_keypair", "generic_oidc", "kubernetes", "oidc_caching"):
        assert repo_requirement(MODULES / name, COMPONENTS) == "", name


def test_gate_skips_when_the_run_has_no_clone():
    res = gate(_mod(), features=None, version=None, repo_unit="component terraform-runner")
    assert res.skip
    assert "terraform-runner" in res.reason and "--repo" in res.reason


def test_gate_repo_unit_empty_does_not_skip():
    assert not gate(_mod(), features=None, version=None, repo_unit="").skip
