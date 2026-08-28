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
               "kubernetes", "terraform_bot", "terraform_generic_oidc", "docs_bound_keypair",
               "scoped_app_access", "terraform_native_join_lb"]

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
    "scoped_app_access": {
        "auth", "httpbin", "httpbin-decoy", "app-agent", "app-agent-unscoped",
        "appbot", "appbot-unscoped", "appbot-notfound",
        "cfg-scoped-bare", "cfg-unscoped-sqn", "cfg-scoped-proxy", "probe",
    },
    # the L7 balancer + its blackhole, the SA minter, and one runner per variable under
    # test; oidc + tf-idbot come from the oidc-server / terraform-runner components
    "terraform_native_join_lb": {
        "auth", "oidc", "tf-idbot", "lb", "tarpit", "sa-minter",
        "tf-lb-native", "tf-lb-blackhole", "tf-lb-idfile", "tf-proxy-native",
    },
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
    # only the UNSCOPED control bot is a manifest entry: a scoped bot is a `bot` resource
    # with a `scope` (bootstrap/2-scoped-bots.yaml), which `tctl bots add` cannot create
    "scoped_app_access": {"unscoped-app-bot"},
    # tf-admin from the terraform-runner component + one bot per natively-joining runner
    "terraform_native_join_lb": {"tf-admin", "tf-lbnative-bot", "tf-blackhole-bot",
                                 "tf-proxynative-bot"},
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


def test_b64url_filter_encodes_the_scoped_token_join_string():
    """A scoped token joined with the `token` method is presented as
    `<scope>::<name>:<base64url(secret)>`, while the token resource carries the raw secret.
    The filter keeps the two derived from ONE value instead of hand-copied side by side."""
    from harness.render import b64url
    assert b64url("harness-scoped-app-agent-secret") == "aGFybmVzcy1zY29wZWQtYXBwLWFnZW50LXNlY3JldA"
    assert "=" not in b64url("a")  # unpadded: the join parser uses RawURLEncoding


def test_scoped_app_access_join_string_matches_the_token_secret(tmp_path):
    """The app agent's join string and the token's `status.secret` must stay in agreement —
    a mismatch fails at join time as a bare "token not found", which reads like a missing
    backport rather than a typo."""
    import base64
    render_module(MODULES / "scoped_app_access", CTX, tmp_path, run_prebuild=False)
    token = next(d for d in yaml.safe_load_all(
        (tmp_path / "bootstrap" / "scoped_app_access__4-scoped-tokens.yaml").read_text())
        if d["metadata"]["name"] == "app-agent-token")
    agent = yaml.safe_load((tmp_path / "config" / "app-agent.yaml").read_text())
    scope, _, rest = agent["teleport"]["join_params"]["token_name"].partition("::")
    name, _, secret_b64 = rest.partition(":")
    assert scope == token["scope"] and name == token["metadata"]["name"]
    padded = secret_b64 + "=" * (-len(secret_b64) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == token["status"]["secret"]
    # a scoped app must not carry a public_addr: it is derived from the app name + scope
    assert "public_addr" not in agent["app_service"]["apps"][0]


def test_mounted_configs_exist(rendered):
    """Every `{{ out }}/config/<name>` bind in a fragment must name a file the renderer wrote.

    A renamed or typo'd `config/*.j2` otherwise renders and validates cleanly, then docker
    silently creates a DIRECTORY at the mount point and the container fails at runtime with
    "is a directory" — several minutes into a cluster bring-up, far from the cause.
    """
    _, out, compose = rendered
    prefix = CTX["out"] + "/config/"
    for name, spec in compose["services"].items():
        for vol in (spec or {}).get("volumes", []) or []:
            if not isinstance(vol, str) or not vol.startswith(prefix):
                continue
            host = vol.split(":", 1)[0]
            assert (out / "config" / Path(host).name).is_file(), \
                f"{name} mounts {host}, which the renderer did not write"


def test_proxy_public_addrs_are_additive_and_fqdn_stays_first(rendered):
    """A unit may put another hostname in front of the proxy (an L7 balancer) via
    `proxy_public_addrs:` in its render.yaml.

    Two properties are load-bearing. The proxy must RECOGNISE the extra name or it treats
    that Host as application access and 302s (lib/web/apiserver.go), which surfaces as
    webclient.Find failing to parse HTML rather than as a routing error. And the cluster
    FQDN must stay FIRST, because PublicAddrs[0] is the address the proxy ADVERTISES
    (lib/web/proxy_settings.go) — displacing it would move every client's reverse tunnel.
    """
    mod, out, _ = rendered
    addrs = yaml.safe_load((out / "config" / "auth.yaml").read_text())["proxy_service"]["public_addr"]
    assert addrs[0] == "zz1.lab.example.com:8443"
    if mod == "terraform_native_join_lb":
        # jinja-rendered against the cluster context + the unit's own render.yaml
        assert addrs == ["zz1.lab.example.com:8443", "lb.lab.example.com:443"]
    else:
        assert addrs == ["zz1.lab.example.com:8443"]  # unchanged for every other module
