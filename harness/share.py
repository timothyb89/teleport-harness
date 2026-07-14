"""Prepare a report bundle for sharing as a GitHub gist.

Gists are flat (no directories) and don't resolve relative links between files, so
`stage_gist` copies a bundle's files into a staging dir with path-flattened names
(`rendered/config/x.yaml` → `rendered--config--x.yaml`) and rewrites `results.md`'s
bundle-relative links to the gist's per-file anchors (`#file-<slug>`), which DO work
within the single-page gist view. Directory links (no single target file) are demoted
to plain text so nothing renders as a dead link. Returns the ordered list of staged
filenames (results.md first) for `gh gist create` to push.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

# markdown links whose target is relative (not http(s) and not an in-page #anchor)
_LINK = re.compile(r"\[([^\]]+)\]\((?!https?://|#)([^)]+)\)")


def _flat(rel: str) -> str:
    """Flatten a bundle-relative path to a single gist filename. Underscores are
    normalized to hyphens so the derived gist anchor is unambiguous (GitHub's handling
    of `_` in file anchors is inconsistent); `/` → `--` keeps the path readable."""
    return rel.replace("/", "--").replace("_", "-")


def gist_anchor(flatname: str) -> str:
    """GitHub's per-file gist anchor for a filename: lowercase, every run of
    non-alphanumerics → one hyphen, prefixed `#file-`."""
    slug = re.sub(r"[^a-z0-9]+", "-", flatname.lower()).strip("-")
    return f"#file-{slug}"


def _collect(bundle: Path) -> list[Path]:
    """Bundle files to attach (results.md is handled separately)."""
    files: list[Path] = []
    for pat in ("setup.json", "console.txt", "results-*.json"):
        files += sorted(bundle.glob(pat))
    for sub in ("rendered", "logs"):
        d = bundle / sub
        if d.is_dir():
            files += sorted(p for p in d.rglob("*") if p.is_file())
    return files


def stage_gist(bundle: Path, stage: Path) -> list[str]:
    bundle, stage = Path(bundle), Path(stage)
    stage.mkdir(parents=True, exist_ok=True)

    mapping: dict[str, str] = {}   # bundle-relative path -> gist anchor
    staged: list[str] = []
    for f in _collect(bundle):
        rel = f.relative_to(bundle).as_posix()
        flat = _flat(rel)
        mapping[rel] = gist_anchor(flat)
        shutil.copyfile(f, stage / flat)
        staged.append(flat)

    md_path = bundle / "results.md"
    md = md_path.read_text() if md_path.is_file() else "# (no results.md in bundle)\n"

    def _rewrite(m: re.Match) -> str:
        text, target = m.group(1), m.group(2).rstrip("/")
        anchor = mapping.get(target)
        return f"[{text}]({anchor})" if anchor else text  # unknown/dir target → plain text

    md = _LINK.sub(_rewrite, md)
    # Gists list files alphabetically, so a `00-` prefix + the bundle name makes the report
    # sort first (the gist's identifying file, instead of an alphabetical `console.txt`).
    report_name = f"00-{_flat(bundle.name)}.md"
    (stage / report_name).write_text(md)
    return [report_name] + staged
