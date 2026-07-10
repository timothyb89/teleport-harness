"""Tests for the multi-module plan model + composed rendering."""

from __future__ import annotations

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
