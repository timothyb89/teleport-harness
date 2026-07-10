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
