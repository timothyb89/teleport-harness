"""Smoke test for the markdown report builder (harness/report.py)."""

from __future__ import annotations

import json
from pathlib import Path

from harness.report import build_markdown


def _state_dir(tmp_path: Path) -> Path:
    (tmp_path / "meta.env").write_text(
        "CLUSTER_ID=c1\nFQDN=c1.lab.example.com\nPORT=8443\nMODULE=oidc-caching\n"
        "MODULES=generic_oidc,kubernetes\nREPO=/repo\nSHA=abc123\n"
        "FEATURES=kubernetes\nVERSION=v18\nCREATED=2026-07-10T00:00:00Z\n"
    )
    (tmp_path / "docker-compose.yml").write_text(
        "name: teleport-harness-c1\nservices:\n"
        "  auth: {image: img:1}\n  oidc: {image: oidc:1}\n  kube-oidc: {image: img:1}\n"
    )
    # setup.json (Foundation B): provenance the report renders as tables with links
    (tmp_path / "setup.json").write_text(json.dumps({
        "modules": ["kubernetes"], "components": ["oidc-server"],
        "services": [
            {"name": "auth", "image": "img:1", "origin": "base"},
            {"name": "oidc", "image": "oidc:1", "origin": "component:oidc-server"},
            {"name": "kube-oidc", "image": "img:1", "origin": "module:kubernetes"},
        ],
        "roles": [{"name": "kube-tester", "kind": "role", "description": "join + list tokens",
                   "allow": "rules=[{resources: [token]}]", "scope": "", "origin": "kubernetes",
                   "source": "rendered/bootstrap/kubernetes__role.yaml"},
                  {"name": "scoped-tester", "kind": "scoped_role", "description": "",
                   "allow": "assignable_scopes=[/team]; ssh={logins: [root], labels: [{name: *}]}",
                   "scope": "/team", "origin": "kubernetes",
                   "source": "rendered/bootstrap/kubernetes__scoped-role.yaml"}],
        "tokens": [{"name": "kube-oidc-token", "kind": "token", "join_method": "kubernetes",
                    "origin": "kubernetes", "source": "rendered/bootstrap/kubernetes__token.yaml"}],
        "bots": [{"name": "kube-oidc-bot", "roles": ["kube-tester"], "token": "",
                  "join_method": "kubernetes", "origin": "module:kubernetes",
                  "source": "rendered/bootstrap/kubernetes__token.yaml"}],
        "configs": [{"file": "tbot-kube-oidc-bot.yaml", "source": "rendered/config/tbot-kube-oidc-bot.yaml",
                     "join_method": "kubernetes", "outputs": ["identity"]}],
    }))
    (tmp_path / "results-kubernetes.json").write_text(json.dumps({
        "module": "kubernetes", "cluster_id": "c1", "passed": True,
        "nodes": [{"hostname": "c1-agent-x", "scope": "/s", "labels": {"env": "test"}}],
        "results": [
            {"status": "PASS", "verb": "bot_joined", "args": ["kube-oidc-bot", "kubernetes"],
             "msg": "bot 'kube-oidc-bot' joined via kubernetes", "proof_refs": ["log-excerpt-aaa111"],
             "assertions": ["event = bot.join", "bot_name = kube-oidc-bot", "success = true",
                            "method = kubernetes"]},
            {"status": "PASS", "verb": "output_file", "args": ["kube-oidc", "/out/id/identity"],
             "msg": "present", "proof_refs": ["file-bbb222"], "assertions": []},
        ],
        "proofs": [
            {"id": "log-excerpt-aaa111", "kind": "log-excerpt", "title": "bot.join audit event",
             "content": "  [11] preceding line\n"
                        "> [12] audit bot.join bot_name:kube-oidc-bot method:kubernetes success:true\n"
                        "  [13] following line",
             "lang": "", "source": "logs/auth.log"},
            {"id": "file-bbb222", "kind": "file", "title": "c1-kube-oidc:/out/id/identity",
             "content": "/out/id/identity: 128 bytes", "lang": "", "source": ""},
        ],
    }))
    return tmp_path


def test_build_markdown_has_all_sections(tmp_path):
    md = build_markdown(_state_dir(tmp_path))
    for section in ["# Test run: c1", "## Summary", "## Cluster setup", "### Services",
                    "### Roles", "### Bots", "## Nodes joined", "## Checks", "## Inspect"]:
        assert section in md, f"missing {section}"
    assert "✅ PASS" in md
    assert "features `kubernetes`" in md and "`v18`" in md
    # services rendered as a table with provenance
    assert "| `oidc` | `oidc:1` | component:oidc-server |" in md
    # role permissions + linked source
    assert "kube-tester" in md and "join + list tokens" in md
    assert "(rendered/bootstrap/kubernetes__role.yaml)" in md
    # scoped_role: scope column + grants summarized (was a bare "—" before), and the
    # structured allow-summary is wrapped in a code span so `*` doesn't render as markdown
    assert "`/team`" in md
    assert "`assignable_scopes=[/team]; ssh={logins: [root], labels: [{name: *}]}`" in md
    # token + bot tables
    assert "kube-oidc-token" in md and "kube-oidc-bot" in md
    assert "c1-agent-x" in md
    # a check links to its proof anchor, and the proof is rendered once as a code block
    assert "#proof-kubernetes-log-excerpt-aaa111" in md
    assert '<a id="proof-kubernetes-log-excerpt-aaa111"></a>' in md
    assert "> [12] audit bot.join bot_name:kube-oidc-bot" in md
    assert "(logs/auth.log)" in md  # proof source link
    # a distinct proof for the file check
    assert "/out/id/identity: 128 bytes" in md
    # each proof spells out the checks made against it + their field=value assertions
    assert "Checks against this proof:" in md
    assert "`bot_name = kube-oidc-bot`" in md and "`method = kubernetes`" in md


def test_build_markdown_overall_fail(tmp_path):
    sd = _state_dir(tmp_path)
    (sd / "results-kubernetes.json").write_text(json.dumps({
        "module": "kubernetes", "passed": False,
        "results": [{"status": "FAIL", "verb": "bot_joined", "args": ["x"], "msg": "nope",
                     "proof_refs": []}],
        "proofs": [],
    }))
    md = build_markdown(sd)
    assert "❌ FAIL" in md


def test_build_markdown_legacy_evidence_shape(tmp_path):
    """Older bundles (inline evidence/excerpt, no proof registry) still render."""
    sd = _state_dir(tmp_path)
    (sd / "results-kubernetes.json").write_text(json.dumps({
        "module": "kubernetes", "passed": True,
        "results": [{"status": "PASS", "verb": "output_file", "args": ["kube", "/id"],
                     "msg": "present", "evidence": ["/id: 128 bytes"], "excerpt": []}],
    }))
    md = build_markdown(sd)
    assert "❌ FAIL" not in md and "/id: 128 bytes" in md


def test_build_markdown_falls_back_without_setup_json(tmp_path):
    sd = _state_dir(tmp_path)
    (sd / "setup.json").unlink()
    md = build_markdown(sd)
    # services still come from the compose fallback
    assert "### Services" in md and "`oidc`" in md
