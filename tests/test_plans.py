"""Tests for the multi-module plan model + composed rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from harness.models import Plan, load_plan
from harness.render import render_cluster

REPO = Path(__file__).resolve().parent.parent
MODULES = REPO / "modules"
COMPONENTS = REPO / "components"
PLANS = REPO / "plans"

CTX = {
    "cluster_id": "zz1",
    "fqdn": "zz1.lab.example.com",
    "port": "8443",
    "image": "teleport-harness:test",
    "harness_domain": "example.com",
    "lab_domain": "lab.example.com",
    "out": "/state/zz1",
}


def test_shipped_plans_load():
    for p in PLANS.glob("*.yaml"):
        plan = load_plan(p)
        assert plan.modules
        for m in plan.modules:
            assert (MODULES / m / "module.yaml").is_file(), f"{plan.name}: bad module {m}"


def test_plan_requires_modules():
    with pytest.raises(ValidationError):
        Plan(name="x", modules=[])


def test_plan_extra_key_rejected():
    with pytest.raises(ValidationError):
        Plan(name="x", modules=["tbot"], bogus=1)


def test_plan_name_must_match_filename(tmp_path):
    p = tmp_path / "myplan.yaml"
    p.write_text("name: notmyplan\nmodules: [tbot]\n")
    with pytest.raises(ValueError):
        load_plan(p)


def test_compose_two_modules_into_one_cluster(tmp_path):
    # the `bots` plan: tbot + bound_keypair share one auth; services + bootstrap merge.
    render_cluster([MODULES / "tbot", MODULES / "bound_keypair"], CTX, tmp_path,
                   components_dir=COMPONENTS, run_prebuild=False)
    compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    svcs = set(compose["services"])
    assert {"auth", "tbot", "tbot-deny", "bkbot", "bkbot-deny"} == svcs
    # both bots land in one manifest
    manifest = (tmp_path / "bootstrap" / "bots.manifest").read_text()
    assert "test-bot" in manifest and "bk-bot" in manifest
    # both token resources present
    tokens = "\n".join(f.read_text() for f in (tmp_path / "bootstrap").glob("*.yaml"))
    assert "bot_name: test-bot" in tokens and "bot_name: bk-bot" in tokens


def test_compose_shared_component_once(tmp_path):
    # generic_oidc pulls in the oidc-server component; a second module listing it too
    # must not duplicate the oidc service.
    render_cluster([MODULES / "generic_oidc"], CTX, tmp_path,
                   components_dir=COMPONENTS, run_prebuild=False)
    compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert "oidc" in compose["services"]
    assert "oidc-data" in compose["volumes"]


def test_duplicate_service_across_modules_raises(tmp_path):
    # two modules defining the same service name must fail loudly, not silently drop.
    with pytest.raises(ValueError):
        render_cluster([MODULES / "tbot", MODULES / "tbot"], CTX, tmp_path,
                       components_dir=COMPONENTS, run_prebuild=False)


# ---- exclusivity: a module that disrupts the cluster can't share one -------------
def _plan(tmp_path, name, modules):
    d = tmp_path / "plans"; d.mkdir(exist_ok=True)
    f = d / f"{name}.yaml"
    f.write_text(f"name: {name}\ndescription: x\nmodules: {json.dumps(modules)}\n")
    return f


def _mod(tmp_path, name, exclusive=False):
    d = tmp_path / "modules" / name
    d.mkdir(parents=True)
    (d / "module.yaml").write_text(
        f"name: {name}\n" + ("exclusive: true\n" if exclusive else "") +
        "checks: |\n  node_present a\n")
    (d / "services.yml.j2").write_text("services: {}\n")
    return d


def test_exclusive_module_cannot_be_composed_with_siblings(tmp_path):
    from harness.models import check_plan_exclusivity, load_plan
    _mod(tmp_path, "restarter", exclusive=True)
    _mod(tmp_path, "bystander")
    plan = load_plan(_plan(tmp_path, "mixed", ["restarter", "bystander"]))
    problems = check_plan_exclusivity(plan, tmp_path / "modules")
    # the failures it causes land on the SIBLING, so the plan must be refused by name
    assert len(problems) == 1
    assert "'restarter' is exclusive" in problems[0] and "bystander" in problems[0]


def test_exclusive_module_alone_in_a_plan_is_fine(tmp_path):
    from harness.models import check_plan_exclusivity, load_plan
    _mod(tmp_path, "restarter", exclusive=True)
    plan = load_plan(_plan(tmp_path, "solo", ["restarter"]))
    assert check_plan_exclusivity(plan, tmp_path / "modules") == []


def test_non_exclusive_modules_compose_freely(tmp_path):
    from harness.models import check_plan_exclusivity, load_plan
    _mod(tmp_path, "a"); _mod(tmp_path, "b")
    plan = load_plan(_plan(tmp_path, "pair", ["a", "b"]))
    assert check_plan_exclusivity(plan, tmp_path / "modules") == []


def test_shipped_plans_do_not_compose_an_exclusive_module():
    """Guards the real regression: plans/bound-keypair-status.yaml paired
    bound_keypair_status with the auth-restarting apply-on-startup module, and the
    restarts made the sibling's applies fail against an unavailable auth."""
    from harness.models import check_plan_exclusivity, load_plan
    root = Path(__file__).resolve().parent.parent
    for f in sorted((root / "plans").glob("*.yaml")):
        assert check_plan_exclusivity(load_plan(f), root / "modules") == [], f.name
