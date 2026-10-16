"""Deterministic image layers (harness/images.py): the shared registry is only a cache if
equal inputs produce equal bytes, whichever client staged them."""

from __future__ import annotations

import os
import tarfile

import pytest

from harness.cli import main
from harness.images import dir_entries, file_entry, write_layer


def _bins(d, content=b"ELF"):
    d.mkdir(parents=True, exist_ok=True)
    for b in ("teleport", "tctl"):
        (d / b).write_bytes(content + b.encode())
        (d / b).chmod(0o755)
    return d


def _layer(tmp_path, d, name="l.tar"):
    out = tmp_path / name
    digest = write_layer([file_entry(d / "teleport", "/usr/local/bin/teleport"),
                          file_entry(d / "tctl", "usr/local/bin/tctl")], out)
    return out, digest


def test_same_content_same_digest_regardless_of_metadata(tmp_path):
    a = _bins(tmp_path / "a")
    b = _bins(tmp_path / "b")
    os.utime(b / "teleport", (1, 1))          # different mtime
    (b / "tctl").chmod(0o775)                 # still executable, different bits
    la, da = _layer(tmp_path, a, "a.tar")
    lb, db = _layer(tmp_path, b, "b.tar")
    assert da == db
    assert la.read_bytes() == lb.read_bytes()


def test_content_change_changes_digest(tmp_path):
    _, da = _layer(tmp_path, _bins(tmp_path / "a"), "a.tar")
    _, db = _layer(tmp_path, _bins(tmp_path / "b", b"ELF2"), "b.tar")
    assert da != db


def test_entries_are_sorted_root_owned_and_epoch(tmp_path):
    out, _ = _layer(tmp_path, _bins(tmp_path / "a"))
    with tarfile.open(out) as tf:
        members = tf.getmembers()
    assert [m.name for m in members] == [
        "usr", "usr/local", "usr/local/bin", "usr/local/bin/tctl", "usr/local/bin/teleport"]
    for m in members:
        assert (m.uid, m.gid, m.uname, m.gname, m.mtime) == (0, 0, "", "", 0)
        assert m.mode == 0o755
    assert members[0].isdir() and members[-1].isfile()


def test_file_mode_defaults_from_exec_bit(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("x")
    assert file_entry(f, "a/notes.txt").mode == 0o644
    assert file_entry(f, "a/notes.txt", 0o600).mode == 0o600


def test_dir_entries_keep_tree_under_prefix_with_long_paths(tmp_path):
    src = tmp_path / "docs"
    deep = src / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    (deep / "page.mdx").write_text("hi")
    (src / "top.md").write_text("top")
    out = tmp_path / "docs.tar"
    write_layer(dir_entries(src, "artifact"), out)
    with tarfile.open(out) as tf:
        names = tf.getnames()
        assert f"artifact/{'d' * 60}/{'e' * 60}/page.mdx" in names
        assert tf.extractfile("artifact/top.md").read() == b"top"


@pytest.mark.parametrize("dst", ["", "/", "a/../../etc"])
def test_bad_destinations_refused(tmp_path, dst):
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(ValueError):
        file_entry(f, dst)


def test_file_dir_clash_refused(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(ValueError, match="both a file and a directory"):
        write_layer([file_entry(f, "a/b"), file_entry(f, "a/b/c")], tmp_path / "o.tar")


def test_cli_prints_digest(tmp_path, capsys):
    d = _bins(tmp_path / "a")
    out = tmp_path / "cli.tar"
    rc = main(["layer-tar", "--out", str(out), "--file", f"{d / 'teleport'}=usr/local/bin/teleport",
               "--dir", f"{d}=opt/x"])
    assert rc == 0
    digest = capsys.readouterr().out.strip()
    assert len(digest) == 64
    with tarfile.open(out) as tf:
        assert "opt/x/tctl" in tf.getnames()


def test_cli_refuses_empty_and_malformed(tmp_path, capsys):
    assert main(["layer-tar", "--out", str(tmp_path / "o.tar")]) == 1
    assert main(["layer-tar", "--out", str(tmp_path / "o.tar"), "--file", "nodest"]) == 1
    assert "SRC=DEST" in capsys.readouterr().err
