"""Unit tests for the jinja compose renderer (harness/render.py). Renders every shipped
module to a temp dir with a fake context and checks the output is valid, fully substituted,
and structurally sound — the safety net for the render.sh -> jinja migration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harness.render import render_module

REPO = Path(__file__).resolve().parent.parent
MODULES = REPO / "modules"

CTX = {
    "cluster_id": "zz1",
    "fqdn": "zz1.lab.example.com",
    "port": "8443",
    "image": "teleport-harness:test",
    "harness_domain": "example.com",
    "lab_domain": "lab.example.com",
    "out": "/state/zz1",
    "repo": "/fake/teleport",  # docs_bound_keypair's workbench mounts {{ repo }}/docs
}

ALL_MODULES = ["tbot", "bound_keypair", "bound_keypair_apply_on_startup", "generic_oidc",
               "kubernetes", "terraform_bot", "terraform_generic_oidc", "docs_bound_keypair"]

EXPECTED_SERVICES = {
    "tbot": {"auth", "tbot", "tbot-deny"},
    "bound_keypair": {"auth", "bkbot", "bkbot-deny"},
    "bound_keypair_apply_on_startup": {"auth", "bkbot", "bkbot-deny"},
    "generic_oidc": {
        "auth", "oidc", "oidc-ca", "tbot", "token-manager",
        "agent-discovery", "agent-static", "agent-scoped-discovery",
        "agent-scoped-static", "agent-deny", "agent-scoped-deny",
        "agent-expr", "agent-expr-deny",
        "gobot-disc", "gobot-static", "gobot-scoped-disc", "gobot-scoped-static",
    },
    "kubernetes": {"auth", "oidc", "kube-oidc", "kube-jwks"},  # oidc from the shared component
    # tf-idbot from the shared terraform-runner component; the runner container per module
    "terraform_bot": {"auth", "tf-idbot", "tf-bot"},
    # + oidc (oidc-server component) and the two join-test agents
    "terraform_generic_oidc": {"auth", "oidc", "tf-idbot", "tf-oidc",
                               "tf-agent-ok", "tf-agent-badorg"},
    # agent-idbot from the shared agent-runner component; workbench is the module's runner
    "docs_bound_keypair": {"auth", "agent-idbot", "workbench"},
}


@pytest.fixture(params=ALL_MODULES)
def rendered(request, tmp_path):
    mod = request.param
    render_module(MODULES / mod, CTX, tmp_path, run_prebuild=False)
    compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    return mod, tmp_path, compose


def test_compose_is_valid_yaml_with_expected_services(rendered):
    mod, _, compose = rendered
    assert compose["name"] == "teleport-harness-zz1"
    assert set(compose["services"]) == EXPECTED_SERVICES[mod]


def test_auth_service_shape(rendered):
    _, _, compose = rendered
    auth = compose["services"]["auth"]
    assert auth["container_name"] == "zz1-auth"
    assert auth["image"] == "teleport-harness:test"
    # both networks aliased to the FQDN (east-west TLS) — a load-bearing invariant
    assert auth["networks"]["internal"]["aliases"] == ["zz1.lab.example.com"]
    assert auth["networks"]["teleport-harness"]["aliases"] == ["zz1.lab.example.com"]
    assert compose["networks"]["teleport-harness"]["external"] is True


def test_no_unrendered_template_markers(rendered):
    """Catches missing context vars / stray envsubst syntax across compose + configs."""
    _, out, _ = rendered
    for f in [out / "docker-compose.yml", *(out / "config").glob("*")]:
        text = f.read_text()
        assert "{{" not in text and "{%" not in text, f"unrendered jinja in {f.name}"
        assert "${" not in text, f"leftover envsubst syntax in {f.name}"


def test_shared_auth_yaml_rendered(rendered):
    _, out, _ = rendered
    auth = yaml.safe_load((out / "config" / "auth.yaml").read_text())
    assert auth["auth_service"]["cluster_name"] == "zz1.lab.example.com"
    assert auth["proxy_service"]["web_listen_addr"] == "0.0.0.0:8443"
    # JSON audit backend so audit_event checks can read structured events off disk
    assert auth["teleport"]["storage"]["audit_events_uri"] == ["file:///var/lib/teleport/audit/events"]


def test_auth_env_is_union_of_unit_auth_env(rendered):
    # auth_env is now only for things auth itself needs at runtime; join secrets moved
    # to the declarative bootstrap (tokens + bots.manifest), not auth env vars.
    mod, _, compose = rendered
    env = compose["services"]["auth"].get("environment", {})
    if mod == "generic_oidc":
        assert env["TELEPORT_UNSTABLE_SCOPES"] == "yes"
    else:
        assert "BOT_TOKEN" not in env and "REG_SECRET" not in env


EXPECTED_BOTS = {
    "tbot": {"test-bot"},
    "bound_keypair": {"bk-bot"},
    "bound_keypair_apply_on_startup": {"bk-bot"},
    # token-manager (token method) + the two unscoped generic_oidc bots (empty token,
    # authorized by runtime-created provision tokens). Scoped bots are scoped_bot
    # bootstrap resources, not `bots add` manifest entries.
    "generic_oidc": {"token-manager", "gobot-disc", "gobot-static"},
    "kubernetes": {"kube-oidc-bot", "kube-jwks-bot"},
    # the privileged identity bot the terraform-runner component contributes
    "terraform_bot": {"tf-admin"},
    "terraform_generic_oidc": {"tf-admin"},
    # the privileged admin identity bot the agent-runner component contributes
    "docs_bound_keypair": {"agent-admin"},
}


def test_bootstrap_bots_manifest_and_tokens(rendered):
    mod, out, _ = rendered
    manifest = (out / "bootstrap" / "bots.manifest").read_text().strip().splitlines()
    names = {line.split("\t")[0] for line in manifest if line.strip()}
    assert names == EXPECTED_BOTS[mod]
    # every manifest token must correspond to a rendered bootstrap token resource
    boot = list((out / "bootstrap").glob("*.yaml"))
    tokens = "\n".join(f.read_text() for f in boot)
    for line in manifest:
        parts = line.split("\t")
        token = parts[2] if len(parts) > 2 else ""  # empty => bot authorized by a separate token (e.g. kube)
        if token:
            assert token in tokens, f"{mod}: manifest token {token} has no bootstrap resource"
    # no unrendered markers leaked into bootstrap
    assert "{{" not in tokens and "${" not in tokens


def test_setup_json_provenance(rendered):
    """setup.json (Foundation B): the renderer publishes what it created + source links,
    so the report renders tables instead of re-scraping bootstrap YAML."""
    import json
    mod, out, compose = rendered
    setup = json.loads((out / "setup.json").read_text())
    # services carry provenance; every compose service is accounted for
    svc_names = {s["name"] for s in setup["services"]}
    assert svc_names == set(compose["services"])
    assert next(s for s in setup["services"] if s["name"] == "auth")["origin"] == "base"
    # bots the renderer created appear with a source link
    bot_names = {b["name"] for b in setup["bots"]}
    assert EXPECTED_BOTS[mod] <= bot_names
    for b in setup["bots"]:
        assert b["source"].startswith("rendered/") or b["source"] == ""
    # roles/tokens link to the rendered resource that defined them (bootstrap, or — for
    # tokens teleport applies itself — apply-on-startup)
    for r in setup["roles"]:
        assert r["source"].startswith("rendered/bootstrap/")
    for t in setup["tokens"]:
        assert t["source"].startswith(("rendered/bootstrap/", "rendered/apply-on-startup/"))


def test_setup_json_token_join_methods(tmp_path):
    import json
    render_module(MODULES / "generic_oidc", CTX, tmp_path, run_prebuild=False)
    setup = json.loads((tmp_path / "setup.json").read_text())
    tok = next(t for t in setup["tokens"] if t["join_method"])
    assert tok["join_method"]  # e.g. token / generic_oidc
    # the token-manager bot resolves its join method from its bootstrap token
    tm = next(b for b in setup["bots"] if b["name"] == "token-manager")
    assert tm["join_method"] == "token"


def test_generic_oidc_agent_configs_and_volumes(tmp_path):
    render_module(MODULES / "generic_oidc", CTX, tmp_path, run_prebuild=False)
    compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    # extra volumes from the {% block volumes %}
    assert {"bot-data", "idents", "oidc-data"} <= set(compose["volumes"])
    # audience is derived from the fqdn
    assert compose["services"]["oidc"]["command"][2] == "-audience=zz1.lab.example.com/generic-oidc"
    # each declared agent got a config file
    for name in ["discovery", "static", "scoped-discovery", "scoped-static", "deny", "scoped-deny"]:
        cfg = yaml.safe_load((tmp_path / "config" / f"agent-{name}.yaml").read_text())
        assert cfg["teleport"]["nodename"].endswith(f"agent-{name}") or name in ("discovery", "static", "deny")


def test_apply_on_startup_collected_and_surfaced(tmp_path):
    """A module's apply_on_startup/*.yaml[.j2] is rendered into $OUT/apply-on-startup (for
    `teleport start --apply-on-startup`) and its tokens appear in setup.json flagged as such."""
    import json
    render_module(MODULES / "bound_keypair_apply_on_startup", CTX, tmp_path, run_prebuild=False)
    applied = list((tmp_path / "apply-on-startup").glob("*.yaml"))
    assert len(applied) == 1
    body = applied[0].read_text()
    assert "{{" not in body and "${" not in body  # fully rendered
    doc = yaml.safe_load(body)
    assert doc["kind"] == "token" and doc["spec"]["join_method"] == "bound_keypair"
    assert doc["spec"]["bound_keypair"]["onboarding"]["registration_secret"] == "harness-bk-regsecret"
    # surfaced in setup.json, flagged apply_on_startup so the report can distinguish it
    setup = json.loads((tmp_path / "setup.json").read_text())
    tok = next(t for t in setup["tokens"] if t["name"] == "bk-token")
    assert tok["apply_on_startup"] is True
    assert tok["source"] == "rendered/apply-on-startup/bound_keypair_apply_on_startup__token.yaml"


def test_apply_on_startup_dir_always_present(tmp_path):
    """The dir is created even for a module with no apply-on-startup resources, so the base
    compose can unconditionally mount it (the entrypoint globs + only passes the flag if non-empty)."""
    render_module(MODULES / "tbot", CTX, tmp_path, run_prebuild=False)
    apply_dir = tmp_path / "apply-on-startup"
    assert apply_dir.is_dir()
    assert list(apply_dir.glob("*.yaml")) == []


def test_missing_context_var_raises(tmp_path):
    # StrictUndefined => a template referencing an unset var fails loudly, not silently blank.
    from jinja2 import UndefinedError
    with pytest.raises((UndefinedError, KeyError)):
        render_module(MODULES / "tbot", {k: v for k, v in CTX.items() if k != "image"}, tmp_path, run_prebuild=False)
