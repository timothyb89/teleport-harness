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
    boot = tmp_path / "bootstrap"
    boot.mkdir()
    (boot / "kube__role.yaml").write_text("kind: role\nmetadata: {name: kube-tester}\n")
    (boot / "kube__token.yaml").write_text(
        "kind: token\nmetadata: {name: kube-oidc-token}\nspec: {join_method: kubernetes}\n")
    (boot / "bots.manifest").write_text("kube-oidc-bot\tkube-tester\t\n")
    (tmp_path / "results-kubernetes.json").write_text(json.dumps({
        "module": "kubernetes", "cluster_id": "c1", "passed": True,
        "nodes": [{"hostname": "c1-agent-x", "scope": "/s", "labels": {"env": "test"}}],
        "results": [
            {"status": "PASS", "verb": "bot_joined", "args": ["kube-oidc-bot", "kubernetes"],
             "msg": "bot 'kube-oidc-bot' joined via kubernetes", "evidence": [],
             "excerpt": ["  [11] preceding line",
                         "> [12] audit bot.join bot_name:kube-oidc-bot method:kubernetes success:true",
                         "  [13] following line"]},
            {"status": "PASS", "verb": "output_file", "args": ["kube-oidc", "/out/id/identity"],
             "msg": "present", "evidence": ["/out/id/identity: 128 bytes"], "excerpt": []},
        ],
    }))
    return tmp_path


def test_build_markdown_has_all_sections(tmp_path):
    md = build_markdown(_state_dir(tmp_path))
    for section in ["# Test run: c1", "## Summary", "## Cluster setup",
                    "## Nodes joined", "## Checks", "## Inspect"]:
        assert section in md, f"missing {section}"
    # overall pass, target line, service + bootstrap summaries, node, and evidence all present
    assert "✅ PASS" in md
    assert "features `kubernetes`" in md and "`v18`" in md
    assert "`oidc` — image `oidc:1`" in md
    assert "kube-oidc-token" in md and "kube-oidc-bot" in md
    assert "c1-agent-x" in md
    # log excerpt rendered as a fenced code block (indented into the list item)
    assert "  ```" in md
    assert "  > [12] audit bot.join bot_name:kube-oidc-bot" in md
    # inline proof for non-log checks stays a sub-bullet
    assert "proof: `/out/id/identity: 128 bytes`" in md
    # bundle-relative links
    assert "(rendered/docker-compose.yml)" in md


def test_build_markdown_overall_fail(tmp_path):
    sd = _state_dir(tmp_path)
    (sd / "results-kubernetes.json").write_text(json.dumps({
        "module": "kubernetes", "passed": False,
        "results": [{"status": "FAIL", "verb": "bot_joined", "args": ["x"], "msg": "nope", "evidence": []}],
    }))
    md = build_markdown(sd)
    assert "❌ FAIL" in md
