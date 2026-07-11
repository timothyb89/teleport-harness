"""Unit tests for the Python verifier (harness/verify.py) using a FakeCluster —
the docker seam that made the assert library testable at all (it never was in bash)."""

from __future__ import annotations

from pathlib import Path

from harness.cluster import Cluster
from harness.models import load_module, parse_checks
from harness.verify import (
    IMPLS,
    CheckResult,
    render,
    run_check,
    verb_impls_match_registry,
    verify,
)

REPO = Path(__file__).resolve().parent.parent
MODULES = REPO / "modules"


class FakeCluster(Cluster):
    def __init__(self, cid="c1", nodes=None, logs=None, files=None, execs=None, tsh_ok=False):
        super().__init__(cid)
        self._nodes = nodes or []
        self._logs = logs or {}
        self._files = set(files or [])
        self._execs = execs or {}
        self._tsh_ok = tsh_ok

    def get_nodes(self):
        return self._nodes

    def logs(self, suffix):
        return self._logs.get(suffix, "")

    def exec_out(self, suffix, argv):
        v = self._execs.get((suffix, tuple(argv)), 1)
        return v if isinstance(v, tuple) else (v, "")

    def file_nonempty(self, suffix, path):
        return (suffix, path) in self._files

    def file_size(self, suffix, path):
        return 128 if (suffix, path) in self._files else None

    def tsh_ssh(self, host_suffix, login):
        return self._tsh_ok

    def proxy_addr(self):
        return "c1.lab.example.com:8443"


def _node(hostname, scope=None):
    n = {"spec": {"hostname": hostname}}
    if scope is not None:
        n["scope"] = scope
    return n


def _run(cluster, line):
    (chk,) = parse_checks(line + "\n")
    return run_check(cluster, cluster.get_nodes(), chk)


# ---- drift guard ------------------------------------------------------------
def test_impls_match_registry():
    assert verb_impls_match_registry() == []


# ---- evidence capture (the "show your work" proof) --------------------------
def test_node_present_evidence_has_hostname():
    c = FakeCluster(nodes=[_node("c1-agent-static", scope="/s")])
    res = _run(c, "node_present agent-static")
    assert res.status == "PASS"
    assert any("c1-agent-static" in e for e in res.evidence)


def test_log_contains_excerpt_has_context_and_line_numbers():
    logs = "\n".join(f"line{i}" for i in range(1, 11))
    logs = logs.replace("line5", "error: unable to validate generic_oidc token")
    c = FakeCluster(logs={"agent-deny": logs})
    res = _run(c, "log_contains agent-deny unable to validate generic_oidc")
    assert res.status == "PASS"
    # matched line marked with '>', context lines present, 1-based line numbers
    joined = "\n".join(res.excerpt)
    assert "> [5] error: unable to validate generic_oidc token" in joined
    assert "  [2] line2" in joined and "  [8] line8" in joined  # ±3 context
    assert "line1\n" not in joined and not joined.startswith("  [1]")  # line 1 outside the C3 window


def test_bot_joined_excerpt_marks_the_audit_line():
    lines = ["noise", "2026 audit bot.join bot_name:bk-bot method:bound_keypair success:true code:TJ001I", "more"]
    c = FakeCluster(logs={"auth": "\n".join(lines)})
    res = _run(c, "bot_joined bk-bot bound_keypair")
    assert res.status == "PASS"
    assert any(e.startswith("> ") and "bot_name:bk-bot" in e for e in res.excerpt)


def test_output_file_evidence_has_size():
    c = FakeCluster(files=[("tbot", "/out/id/identity")])
    res = _run(c, "output_file tbot /out/id/identity")
    assert res.status == "PASS" and any("128 bytes" in e for e in res.evidence)


def test_identity_authorized_evidence_has_command():
    argv = ("tctl", "--identity", "/out/id/identity", "--auth-server", "auth:3025", "tokens", "ls")
    c = FakeCluster(execs={("tbot", argv): (0, "token1\ntoken2\n")})
    res = _run(c, "identity_authorized tbot /out/id/identity")
    assert res.status == "PASS"
    assert any("tokens ls" in e and "exit 0" in e for e in res.evidence)


def test_render_includes_evidence_sublines():
    c = FakeCluster(nodes=[_node("c1-a")])
    text, _ = render([_run(c, "node_present a")])
    assert "↳" in text and "c1-a" in text


# ---- node verbs -------------------------------------------------------------
def test_node_present_absent():
    c = FakeCluster(nodes=[_node("c1-agent-static")])
    assert _run(c, "node_present agent-static").status == "PASS"
    assert _run(c, "node_present agent-missing").status == "FAIL"
    assert _run(c, "node_absent agent-missing").status == "PASS"
    assert _run(c, "node_absent agent-static").status == "FAIL"


def test_node_scope():
    c = FakeCluster(nodes=[_node("c1-a", scope="/genericoidc-test"), _node("c1-b")])
    assert _run(c, "node_scope a /genericoidc-test").status == "PASS"
    assert _run(c, "node_scope a /wrong").status == "FAIL"
    assert _run(c, "node_scope b /genericoidc-test").status == "FAIL"  # empty scope


def test_node_count_and_scoped_count():
    c = FakeCluster(nodes=[
        _node("c1-a", scope="/s"), _node("c1-b", scope="/s"),
        _node("c1-c"), _node("c1-d"),
    ])
    assert _run(c, "node_count 4").status == "PASS"
    assert _run(c, "node_count 3").status == "FAIL"
    assert _run(c, "scoped_node_count /s 2").status == "PASS"
    assert _run(c, "scoped_node_count /s 1").status == "FAIL"


# ---- log verbs --------------------------------------------------------------
def test_log_contains_match_and_skip():
    c = FakeCluster(logs={"agent-deny": "error: unable to validate generic_oidc token"})
    assert _run(c, "log_contains agent-deny unable to (join via|validate) generic_oidc|denied").status == "PASS"
    # case-insensitive, like grep -qiE
    assert _run(c, "log_contains agent-deny UNABLE TO VALIDATE").status == "PASS"
    # no match -> SKIP (soft), never FAIL
    assert _run(c, "log_contains agent-deny nonsense-pattern-xyz").status == "SKIP"


def test_bot_joined_with_and_without_method():
    line = "audit bot.join bot_name:bk-bot method:bound_keypair success:true"
    c = FakeCluster(logs={"auth": line})
    assert _run(c, "bot_joined bk-bot").status == "PASS"
    assert _run(c, "bot_joined bk-bot bound_keypair").status == "PASS"
    assert _run(c, "bot_joined bk-bot token").status == "FAIL"  # wrong method
    assert _run(c, "bot_joined other-bot").status == "FAIL"


def test_bot_joined_requires_success():
    c = FakeCluster(logs={"auth": "bot.join bot_name:x method:token success:false"})
    assert _run(c, "bot_joined x").status == "FAIL"


# ---- file verbs -------------------------------------------------------------
def test_output_file_verbs():
    c = FakeCluster(files=[("tbot", "/out/id/identity")])
    assert _run(c, "output_file tbot /out/id/identity").status == "PASS"
    assert _run(c, "output_file tbot /out/id/missing").status == "FAIL"
    assert _run(c, "no_output_file tbot-deny /out/id/identity").status == "PASS"
    assert _run(c, "no_output_file tbot /out/id/identity").status == "FAIL"


# ---- identity_authorized ----------------------------------------------------
def test_identity_authorized():
    argv = ("tctl", "--identity", "/out/id/identity", "--auth-server", "auth:3025", "tokens", "ls")
    c = FakeCluster(execs={("tbot", argv): 0})
    assert _run(c, "identity_authorized tbot /out/id/identity").status == "PASS"
    # a container whose exec returns nonzero -> FAIL
    assert _run(c, "identity_authorized bkbot /out/id/identity").status == "FAIL"


def test_identity_authorized_custom_auth_server():
    argv = ("tctl", "--identity", "/id", "--auth-server", "other:3025", "tokens", "ls")
    c = FakeCluster(execs={("tbot", argv): 0})
    assert _run(c, "identity_authorized tbot /id other:3025").status == "PASS"


# ---- tsh_ssh ----------------------------------------------------------------
def test_tsh_ssh():
    assert _run(FakeCluster(tsh_ok=True), "tsh_ssh node1").status == "PASS"
    assert _run(FakeCluster(tsh_ok=False), "tsh_ssh node1 ubuntu").status == "FAIL"


# ---- identity_scope ---------------------------------------------------------
def _status_argv(ident):
    return ("tsh", "status", "--identity", ident, "--proxy", "c1.lab.example.com:8443")


def test_identity_scope_pass_and_fail():
    argv = _status_argv("/out/id/identity")
    ok = FakeCluster(execs={("gobot", argv): (0, "  Logged in as: bot\n  Scope:  /genericoidc-test\n")})
    res = _run(ok, "identity_scope gobot /out/id/identity /genericoidc-test")
    assert res.status == "PASS" and any("/genericoidc-test" in e for e in res.evidence)
    # wrong/absent scope -> FAIL (an unscoped identity prints no Scope line)
    bad = FakeCluster(execs={("gobot", argv): (0, "  Logged in as: bot\n")})
    assert _run(bad, "identity_scope gobot /out/id/identity /genericoidc-test").status == "FAIL"


# ---- tsh_ssh_as -------------------------------------------------------------
def _ssh_as_argv(ident, node, login):
    return ("tsh", "ssh", "--identity", ident, "--proxy", "c1.lab.example.com:8443",
            f"{login}@{node}", "--", "echo", "harness-ok")


def test_tsh_ssh_as_pass_and_fail():
    argv = _ssh_as_argv("/out/id/identity", "c1-agent-scoped-discovery", "root")
    ok = FakeCluster(execs={("gobot", argv): (0, "harness-ok\n")})
    assert _run(ok, "tsh_ssh_as gobot /out/id/identity agent-scoped-discovery root").status == "PASS"
    # access denied / no OS user -> nonzero + no marker -> FAIL
    bad = FakeCluster(execs={("gobot", argv): (255, "access denied to root\n")})
    assert _run(bad, "tsh_ssh_as gobot /out/id/identity agent-scoped-discovery root").status == "FAIL"


# ---- render / RESULT --------------------------------------------------------
def test_render_fail_and_pass():
    text, passed = render([CheckResult("PASS", "ok"), CheckResult("SKIP", "later")])
    assert passed and text.endswith("RESULT: PASS")
    text, passed = render([CheckResult("PASS", "ok"), CheckResult("FAIL", "boom")])
    assert not passed and text.endswith("RESULT: FAIL")


def test_unknown_verb_fails_gracefully():
    from harness.models import Check
    res = run_check(FakeCluster(), [], Check(verb="frob", args=[], raw="frob", lineno=1))
    assert res.status == "FAIL" and "unknown check verb" in res.msg


# ---- full-module simulations (the converted declarative checks) --------------
def test_generic_oidc_all_pass_simulated():
    m = load_module(MODULES / "generic_oidc")
    scope = "/genericoidc-test"
    nodes = [
        _node("c1-agent-discovery"), _node("c1-agent-static"),
        _node("c1-agent-scoped-discovery", scope=scope),
        _node("c1-agent-scoped-static", scope=scope),
    ]
    bots = ["gobot-disc", "gobot-static", "gobot-scoped-disc", "gobot-scoped-static"]
    auth_log = "\n".join(
        ["audit join_token.create ... impersonator:bot-token-manager ..."]
        + [f"event_type:bot.join bot_name:{b} method:generic_oidc success:true" for b in bots]
    )
    logs = {
        "agent-deny": "unable to validate generic_oidc token",
        "agent-scoped-deny": "denied: unable to join via generic_oidc",
        "auth": auth_log,
    }
    # every bot wrote an identity; the two unscoped bots can list tokens.
    files = [(b, "/out/id/identity") for b in bots]
    id_argv = ("tctl", "--identity", "/out/id/identity", "--auth-server", "auth:3025", "tokens", "ls")
    execs = {("gobot-disc", id_argv): 0, ("gobot-static", id_argv): 0}
    # scoped bots: tsh status shows the scope, and tsh ssh (as root) into their scoped
    # agent works. proxy_addr() is the FakeCluster default (c1.lab.example.com:8443).
    proxy = "c1.lab.example.com:8443"
    for b, node in [("gobot-scoped-disc", "c1-agent-scoped-discovery"),
                    ("gobot-scoped-static", "c1-agent-scoped-static")]:
        status_argv = ("tsh", "status", "--identity", "/out/id/identity", "--proxy", proxy)
        execs[(b, status_argv)] = (0, f"  Scope:  {scope}\n")
        ssh_argv = ("tsh", "ssh", "--identity", "/out/id/identity", "--proxy", proxy,
                    f"root@{node}", "--", "echo", "harness-ok")
        execs[(b, ssh_argv)] = (0, "harness-ok\n")
    c = FakeCluster(nodes=nodes, logs=logs, files=files, execs=execs)
    results = verify(c, m.checks, module_dir=MODULES / "generic_oidc")
    text, passed = render(results)
    assert passed, text


def test_tbot_identity_check_is_declarative_now():
    # the old checks.sh escape hatch is gone; identity_authorized covers it.
    m = load_module(MODULES / "tbot")
    assert any(chk.verb == "identity_authorized" for chk in m.checks)
    assert not (MODULES / "tbot" / "checks.sh").exists()
    argv = ("tctl", "--identity", "/out/id/identity", "--auth-server", "auth:3025", "tokens", "ls")
    c = FakeCluster(
        logs={"auth": "bot.join bot_name:test-bot method:token success:true"},
        files=[("tbot", "/out/id/identity")],
        execs={("tbot", argv): 0},
    )
    results = verify(c, m.checks, module_dir=MODULES / "tbot")
    _, passed = render(results)
    assert passed
