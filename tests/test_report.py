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


# ---- agent-driven modules: dedicated, formatted findings section ----
_AGENT_RESULT = {
    "task": "onboard docbot via bound_keypair",
    "status": "partial",
    "summary": "Onboarded docbot, but the guide's `tctl bots add` overlaps Step 3.",
    "steps": [
        {"n": 1, "action": "Create the bot", "ok": True, "doc_ref": "getting-started.mdx Step 2/4"},
        {"n": 2, "action": "Start tbot", "ok": False, "doc_ref": "Step 4/4"},
    ],
    "issues": [
        {"severity": "minor", "area": "docs", "description": "`services: []` vs `outputs: []` mismatch",
         "evidence": "both under version: v2", "suggested_fix": "use `services:` consistently"},
        {"severity": "major", "area": "docs", "description": "Step 2 overlaps Step 3",
         "evidence": "`tctl bots add` prints a token", "suggested_fix": "reconcile the steps"},
    ],
}


def _agent_bundle(tmp_path):
    (tmp_path / "meta.env").write_text("CLUSTER_ID=zz1\nMODULE=docs_bound_keypair\n")
    (tmp_path / "results-docs_bound_keypair.json").write_text(json.dumps({
        "module": "docs_bound_keypair", "passed": True, "nodes": [],
        "results": [
            {"status": "PASS", "verb": "agent_result", "args": [], "msg": "agent status=partial",
             "proof_refs": ["agent-result-abc", "agent-transcript-xyz"],
             "assertions": ["[major/docs] Step 2 overlaps Step 3"]},
            {"status": "PASS", "verb": "bot_joined", "args": ["docbot", "bound_keypair"],
             "msg": "bot 'docbot' joined", "proof_refs": ["audit-1"], "assertions": []},
        ],
        "proofs": [
            {"id": "agent-result-abc", "kind": "agent-result", "title": "agent verdict: onboard docbot",
             "content": json.dumps(_AGENT_RESULT), "lang": "json", "source": ""},
            {"id": "agent-transcript-xyz", "kind": "agent-transcript", "title": "agent transcript",
             "content": '{"type":"result","total_cost_usd":1.6}', "lang": "json", "source": ""},
            {"id": "audit-1", "kind": "audit-event", "title": "bot.join", "content": "{}", "lang": "json"},
        ],
    }))
    return tmp_path


def test_agent_findings_section_renders_formatted(tmp_path):
    md = build_markdown(_agent_bundle(tmp_path))
    # a dedicated section exists, anchored so the check table can link to it
    assert "## Agent findings" in md
    assert '<a id="agent-findings-docs_bound_keypair"></a>' in md
    assert "docs_bound_keypair — ⚠️ partial" in md
    # issues sorted most-severe-first, rendered as formatted list items (NOT one escaped code span)
    assert md.index("Step 2 overlaps Step 3") < md.index("mismatch")
    assert "**Suggested fix:** reconcile the steps" in md
    # description markdown is preserved (backticks stay as code, not escaped away)
    assert "`tctl bots add` prints a token" in md
    # steps render with ok/fail badges
    assert "1. ✅ **Create the bot**" in md and "2. ❌ **Start tbot**" in md
    # raw record available but collapsed
    assert "<details><summary>raw agent-result.json</summary>" in md


def test_checks_table_links_agent_result_to_findings_and_collapses_transcript(tmp_path):
    md = build_markdown(_agent_bundle(tmp_path))
    # the agent_result check's proof cell points at the findings section, not a JSON dump
    assert "[↳ findings](#agent-findings-docs_bound_keypair)" in md
    # the (large) transcript proof is collapsed; the objective check's audit proof still renders
    assert "<details><summary>full transcript</summary>" in md
    assert "#proof-docs_bound_keypair-audit-1" in md
    # the agent_result proof is NOT dumped as raw JSON under a "Proofs" heading
    assert "- see [Agent findings](#agent-findings-docs_bound_keypair)" in md


def test_no_agent_findings_section_without_agent_module(tmp_path):
    # the standard (non-agent) bundle must not grow an empty Agent findings section
    md = build_markdown(_state_dir(tmp_path))
    assert "## Agent findings" not in md
