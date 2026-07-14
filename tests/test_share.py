"""Unit tests for gist staging (harness/share.py) — path flattening + link rewriting."""

from __future__ import annotations

from pathlib import Path

from harness.share import gist_anchor, stage_gist


def _bundle(tmp_path: Path) -> Path:
    b = tmp_path / "bundle"
    (b / "rendered" / "config").mkdir(parents=True)
    (b / "rendered" / "bootstrap").mkdir(parents=True)
    (b / "logs").mkdir(parents=True)
    (b / "setup.json").write_text("{}")
    (b / "console.txt").write_text("run log")
    (b / "results-generic_oidc.json").write_text("{}")
    (b / "rendered" / "docker-compose.yml").write_text("services: {}")
    (b / "rendered" / "config" / "tbot.yaml").write_text("version: v2")
    (b / "rendered" / "bootstrap" / "generic_oidc__role-x.yaml").write_text("kind: role")
    (b / "logs" / "auth.log").write_text("audit ...")
    (b / "results.md").write_text(
        "# Report\n"
        "web UI: https://c1.lab.example.com:8443\n"
        "- [docker-compose.yml](rendered/docker-compose.yml)\n"
        "- [config/](rendered/config)\n"          # directory link
        "- [logs/](logs)\n"                        # directory link
        "- role source [yaml](rendered/bootstrap/generic_oidc__role-x.yaml)\n"
        "- proof [source](logs/auth.log)\n"
    )
    return b


def test_gist_anchor_slug():
    assert gist_anchor("rendered--docker-compose.yml") == "#file-rendered-docker-compose-yml"
    assert gist_anchor("logs--auth.log") == "#file-logs-auth-log"


def test_stage_flattens_files_and_lists_results_first(tmp_path):
    stage = tmp_path / "stage"
    files = stage_gist(_bundle(tmp_path), stage)
    # the report sorts first (00- prefix) and is named after the bundle, not console.txt
    assert files[0] == "00-bundle.md" and files[0] == min(files)
    # directory structure flattened into single filenames
    assert "rendered--docker-compose.yml" in files
    assert "rendered--config--tbot.yaml" in files
    assert "logs--auth.log" in files
    assert "setup.json" in files and "results-generic-oidc.json" in files
    # every listed file actually exists in the staging dir
    for f in files:
        assert (stage / f).is_file()


def test_stage_rewrites_links(tmp_path):
    stage = tmp_path / "stage"
    stage_gist(_bundle(tmp_path), stage)
    md = (stage / "00-bundle.md").read_text()
    # file links become gist anchors
    assert "[docker-compose.yml](#file-rendered-docker-compose-yml)" in md
    assert "[source](#file-logs-auth-log)" in md
    assert "[yaml](#file-rendered-bootstrap-generic-oidc-role-x-yaml)" in md
    # directory links (no single target) demote to plain text — no dead links
    assert "config/" in md and "(rendered/config)" not in md
    assert "logs/" in md and "](logs)" not in md
    # external links are left untouched
    assert "https://c1.lab.example.com:8443" in md
