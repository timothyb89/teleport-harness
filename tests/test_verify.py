"""Unit tests for the Python verifier (harness/verify.py) using a FakeCluster —
the docker seam that made the assert library testable at all (it never was in bash)."""

from __future__ import annotations

import json
from pathlib import Path

from harness.cluster import Cluster
from harness.models import load_module, parse_checks
from harness.verify import (
    IMPLS,
    CheckResult,
    ProofItem,
    collect_proofs,
    render,
    run_check,
    verb_impls_match_registry,
    verify,
)


def _proof_text(res) -> str:
    """All proof titles + content of a result, joined (proofs replaced inline evidence)."""
    return "\n".join(p.title + "\n" + p.content for p in res.proofs)

REPO = Path(__file__).resolve().parent.parent
MODULES = REPO / "modules"


class FakeCluster(Cluster):
    def __init__(self, cid="c1", nodes=None, logs=None, files=None, execs=None,
                 tsh_ok=False, events=None, resources=None, state_files=None,
                 kube=None):
        super().__init__(cid)
        self._nodes = nodes or []
        self._logs = logs or {}
        self._files = set(files or [])
        self._execs = execs or {}
        self._tsh_ok = tsh_ok
        self._events = events or []
        self._resources = resources or {}  # {"kind/name": {resource dict}}
        self._state_files = state_files or {}  # {relpath: text}
        self._kube = kube or {}  # {"kind/name": {k8s object dict}}

    def get_nodes(self):
        return self._nodes

    def get_resource(self, kind, name):
        return self._resources.get(f"{kind}/{name}")

    def kube_get(self, kind, name, namespace="teleport"):
        return self._kube.get(f"{kind}/{name}")

    def state_file(self, relpath):
        return self._state_files.get(relpath)

    def logs(self, suffix):
        return self._logs.get(suffix, "")

    def audit_events(self):
        return self._events

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

    def restart_auth(self, timeout=150.0):
        return True


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


# ---- proof capture (the "show your work" evidence, decoupled from the check) -----
def test_node_present_proof_is_a_node_record_with_hostname():
    c = FakeCluster(nodes=[_node("c1-agent-static", scope="/s")])
    res = _run(c, "node_present agent-static")
    assert res.status == "PASS"
    assert res.proofs and res.proofs[0].kind == "node-record"
    assert "c1-agent-static" in _proof_text(res)


def test_log_contains_proof_has_context_line_numbers_and_source():
    logs = "\n".join(f"line{i}" for i in range(1, 11))
    logs = logs.replace("line5", "error: unable to validate generic_oidc token")
    c = FakeCluster(logs={"agent-deny": logs})
    res = _run(c, "log_contains agent-deny unable to validate generic_oidc")
    assert res.status == "PASS"
    (proof,) = res.proofs
    assert proof.kind == "log-excerpt" and proof.source == "logs/agent-deny.log"
    body = proof.content
    assert "> [5] error: unable to validate generic_oidc token" in body
    assert "  [2] line2" in body and "  [8] line8" in body  # ±3 context
    assert not body.startswith("  [1]")  # line 1 outside the C3 window


def test_bot_joined_proof_marks_the_audit_line():
    lines = ["noise", "2026 audit bot.join bot_name:bk-bot method:bound_keypair success:true code:TJ001I", "more"]
    c = FakeCluster(logs={"auth": "\n".join(lines)})
    res = _run(c, "bot_joined bk-bot bound_keypair")
    assert res.status == "PASS"
    body = _proof_text(res)
    assert "> " in body and "bot_name:bk-bot" in body
    assert res.proofs[0].source == "logs/auth.log"


def test_output_file_proof_has_size():
    c = FakeCluster(files=[("tbot", "/out/id/identity")])
    res = _run(c, "output_file tbot /out/id/identity")
    assert res.status == "PASS" and "128 bytes" in _proof_text(res)


def test_identity_authorized_proof_has_command():
    argv = ("tctl", "--identity", "/out/id/identity", "--auth-server", "auth:3025", "tokens", "ls")
    c = FakeCluster(execs={("tbot", argv): (0, "token1\ntoken2\n")})
    res = _run(c, "identity_authorized tbot /out/id/identity")
    assert res.status == "PASS"
    body = _proof_text(res)
    assert "tokens ls" in body and "exit 0" in body


def test_proofitem_id_is_stable_and_dedupes():
    a = ProofItem("text", "t", "same content")
    b = ProofItem("text", "t", "same content")
    c = ProofItem("text", "t", "different")
    assert a.id == b.id and a.id != c.id
    assert a.id.startswith("text-")


def test_collect_proofs_dedupes_shared_proofs():
    shared = ProofItem("audit-event", "bot.join", "{...}")
    r1 = CheckResult("PASS", "a", proofs=[shared])
    r2 = CheckResult("PASS", "b", proofs=[ProofItem("audit-event", "bot.join", "{...}")])
    proofs = collect_proofs([r1, r2])
    assert len(proofs) == 1  # identical content -> one registry entry, cited by both


def test_render_includes_proof_sublines():
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


def test_log_must_contain_fails_on_miss_and_shows_the_tail():
    c = FakeCluster(logs={"tf-lb-blackhole": "line1\nremote error: tls: no application protocol\nline3"})
    assert _run(c, "log_must_contain tf-lb-blackhole tls: no application protocol").status == "PASS"
    # the whole point: a miss is a FAIL, where log_contains would SKIP and stay green
    miss = _run(c, "log_must_contain tf-lb-blackhole nonsense-pattern-xyz")
    assert miss.status == "FAIL"
    assert _run(c, "log_contains tf-lb-blackhole nonsense-pattern-xyz").status == "SKIP"
    # the failure proof carries the log tail, so a miss is diagnosable from the report
    (proof,) = miss.proofs
    assert proof.source == "logs/tf-lb-blackhole.log"
    assert "line3" in proof.content and "no application protocol" in proof.content


def test_log_must_contain_empty_log_is_a_fail_not_a_crash():
    c = FakeCluster(logs={"tf-lb-native": ""})
    res = _run(c, "log_must_contain tf-lb-native TF_APPLY_EXIT=0")
    assert res.status == "FAIL"
    assert "log is empty" in _proof_text(res)


def test_log_count_operators_and_tally():
    # an IdP-style request log: 3 token mints (workload), 1 discovery fetch (cached)
    log = "\n".join([
        "2026/07/13 00:00:01 10.0.0.5:1 GET /k8s/token?serviceaccount=cache-probe-sa",
        "2026/07/13 00:00:02 10.0.0.9:2 GET /.well-known/openid-configuration",
        "2026/07/13 00:00:02 10.0.0.9:3 GET /keys",
        "2026/07/13 00:00:03 10.0.0.5:4 GET /k8s/token?serviceaccount=cache-probe-sa",
        "2026/07/13 00:00:04 10.0.0.5:5 GET /k8s/token?serviceaccount=cache-probe-sa",
    ])
    c = FakeCluster(logs={"cache-idp": log})
    # workload lower bound: >= 3 token mints
    assert _run(c, "log_count cache-idp ge 3 GET /k8s/token").status == "PASS"
    assert _run(c, "log_count cache-idp ge 4 GET /k8s/token").status == "FAIL"
    # caching upper bound: discovery + JWKS fetched at most once each
    assert _run(c, r"log_count cache-idp le 1 GET /\.well-known/openid-configuration").status == "PASS"
    assert _run(c, "log_count cache-idp le 1 GET /keys").status == "PASS"
    # exact + zero
    assert _run(c, "log_count cache-idp eq 3 GET /k8s/token").status == "PASS"
    assert _run(c, "log_count cache-idp eq 0 GET /nonesuch").status == "PASS"


def test_log_count_proof_lists_matched_lines_with_source():
    log = "a\nGET /keys\nb\nGET /keys\n"
    c = FakeCluster(logs={"cache-idp": log})
    res = _run(c, "log_count cache-idp le 1 GET /keys")
    assert res.status == "FAIL"  # 2 > 1
    (proof,) = res.proofs
    assert proof.kind == "log-excerpt" and proof.source == "logs/cache-idp.log"
    assert "[2] GET /keys" in proof.content and "[4] GET /keys" in proof.content
    assert res.assertions == ["count(/GET /keys/) <= 1"]


def test_log_count_bad_operator_and_threshold_fail_cleanly():
    c = FakeCluster(logs={"cache-idp": "GET /keys"})
    assert _run(c, "log_count cache-idp between 1 GET /keys").status == "FAIL"
    assert _run(c, "log_count cache-idp le notanint GET /keys").status == "FAIL"


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


# ---- audit_event (structured event inspection) ------------------------------
_BOT_JOIN_EV = {"event": "bot.join", "code": "TJ001I", "bot_name": "gobot-disc",
                "method": "generic_oidc", "success": True, "time": "2026-07-13T00:00:00Z"}


def test_audit_event_matches_and_renders_full_json_proof():
    c = FakeCluster(events=[{"event": "noise"}, _BOT_JOIN_EV])
    res = _run(c, "audit_event bot.join bot_name=gobot-disc method=generic_oidc")
    assert res.status == "PASS"
    (p,) = res.proofs
    assert p.kind == "audit-event" and p.lang == "json"
    # the FULL event is preserved, pretty-printed
    assert '"bot_name": "gobot-disc"' in p.content and '"code": "TJ001I"' in p.content
    # the verb publishes the individual field assertions it made (shown under the proof)
    assert res.assertions == ["event = bot.join", "bot_name = gobot-disc", "method = generic_oidc"]


def test_audit_event_bool_and_case_insensitive_match():
    c = FakeCluster(events=[_BOT_JOIN_EV])
    assert _run(c, "audit_event bot.join success=true").status == "PASS"  # bool True -> "true"
    assert _run(c, "audit_event bot.join method=Generic_OIDC").status == "PASS"  # value case-insensitive


def test_audit_event_no_match_fails_with_candidate_proof():
    c = FakeCluster(events=[_BOT_JOIN_EV])
    res = _run(c, "audit_event bot.join bot_name=other")
    assert res.status == "FAIL"
    # surfaces the closest same-type event so a reader can see what WAS emitted
    assert res.proofs and "gobot-disc" in res.proofs[0].content


def test_audit_event_absent_type_fails_cleanly():
    res = _run(FakeCluster(events=[]), "audit_event bot.join bot_name=x")
    assert res.status == "FAIL" and not res.proofs


def test_two_audit_event_checks_share_one_proof():
    """The proof is the whole event, so two lines selecting it dedup to ONE proof
    that both checks cite — 'multiple checks against a given proof-item'."""
    c = FakeCluster(events=[_BOT_JOIN_EV])
    r1 = _run(c, "audit_event bot.join bot_name=gobot-disc")
    r2 = _run(c, "audit_event bot.join success=true")
    assert r1.status == r2.status == "PASS"
    assert len(collect_proofs([r1, r2])) == 1


def test_bot_joined_prefers_audit_event_json_over_text_log():
    c = FakeCluster(events=[_BOT_JOIN_EV],
                    logs={"auth": "bot.join bot_name:gobot-disc method:generic_oidc success:true"})
    res = _run(c, "bot_joined gobot-disc generic_oidc")
    assert res.status == "PASS"
    assert res.proofs[0].kind == "audit-event"  # structured proof wins


def test_bot_joined_falls_back_to_text_log_without_events():
    c = FakeCluster(logs={"auth": "bot.join bot_name:bk-bot method:token success:true"})
    res = _run(c, "bot_joined bk-bot token")
    assert res.status == "PASS" and res.proofs[0].kind == "log-excerpt"


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
    assert res.status == "PASS" and "/genericoidc-test" in _proof_text(res)
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


# ---- resource_present / resource_field (live cluster state after e.g. tf apply) ----
_TF_TOKEN = {"kind": "token", "version": "v2", "metadata": {"name": "tf-demo-token"},
             "spec": {"roles": ["Bot"], "bot_name": "tf-demo-bot", "join_method": "token"}}
_GORC_TOKEN = {"kind": "token", "version": "v2", "metadata": {"name": "tf-oidc-token"},
               "spec": {"roles": ["Bot"], "join_method": "generic_oidc",
                        "generic_oidc": {"issuer": "https://idp", "must_match_fields": {"sub": "ci"}}}}


def test_resource_present_pass_and_fail():
    c = FakeCluster(resources={"token/tf-demo-token": _TF_TOKEN})
    res = _run(c, "resource_present token/tf-demo-token")
    assert res.status == "PASS"
    (p,) = res.proofs
    assert p.kind == "resource" and p.lang == "json" and '"tf-demo-token"' in p.content
    # a resource terraform never created (e.g. apply aborted) -> FAIL
    assert _run(c, "resource_present bot/tf-demo-bot").status == "FAIL"


def test_resource_field_presence_and_value_match():
    c = FakeCluster(resources={"token/tf-oidc-token": _GORC_TOKEN})
    # presence-only (no expected value)
    assert _run(c, "resource_field token/tf-oidc-token spec.generic_oidc.must_match_fields").status == "PASS"
    # value match is case-insensitive substring, like audit_event
    ok = _run(c, "resource_field token/tf-oidc-token spec.join_method GENERIC_OIDC")
    assert ok.status == "PASS" and ok.assertions == ["token/tf-oidc-token.spec.join_method = GENERIC_OIDC"]
    # wrong value -> FAIL
    assert _run(c, "resource_field token/tf-oidc-token spec.join_method token").status == "FAIL"


def test_resource_field_not_rejects_a_known_bad_value():
    # The bound_keypair_status case: an update that must be IGNORED instead supplies its
    # own bound_public_key. Presence alone passes on that worst outcome...
    forged = {"metadata": {"name": "bks-token"},
              "status": {"bound_keypair": {"bound_public_key": "ssh-ed25519 AAAATamperedKey x"}}}
    c = FakeCluster(resources={"token/bks-token": forged})
    assert _run(c, "resource_field token/bks-token status.bound_keypair.bound_public_key").status == "PASS"
    # ...while naming the rejected sentinel catches it.
    bad = _run(c, "resource_field_not token/bks-token status.bound_keypair.bound_public_key TamperedKey")
    assert bad.status == "FAIL"
    assert bad.assertions == ["token/bks-token.status.bound_keypair.bound_public_key present and != TamperedKey"]
    # a genuine key (the bot's own) is present and differs -> PASS
    real = {"metadata": {"name": "bks-token"},
            "status": {"bound_keypair": {"bound_public_key": "ssh-ed25519 AAAARealBotKey x"}}}
    ok = _run(FakeCluster(resources={"token/bks-token": real}),
              "resource_field_not token/bks-token status.bound_keypair.bound_public_key TamperedKey")
    assert ok.status == "PASS" and ok.proofs


def test_resource_field_not_fails_when_absent_or_wiped():
    # A WIPED status is its own defect, so absent must not read as "not the bad value".
    wiped = {"metadata": {"name": "bks-token"}, "status": {"bound_keypair": {}}}
    c = FakeCluster(resources={"token/bks-token": wiped})
    assert _run(c, "resource_field_not token/bks-token status.bound_keypair.bound_public_key Tampered").status == "FAIL"
    # ...and so is a token that does not exist at all.
    assert _run(FakeCluster(), "resource_field_not token/bks-token status.bound_keypair.bound_public_key T").status == "FAIL"


def test_resource_field_missing_path_and_missing_resource_fail():
    # the must_match_fields bug today: the field is absent from the created token...
    no_mmf = {"metadata": {"name": "t"}, "spec": {"generic_oidc": {"issuer": "x"}}}
    c = FakeCluster(resources={"token/t": no_mmf})
    assert _run(c, "resource_field token/t spec.generic_oidc.must_match_fields").status == "FAIL"
    # ...or, more often today, apply aborts and the token is never created at all.
    assert _run(FakeCluster(), "resource_field token/t spec.generic_oidc.must_match_fields x").status == "FAIL"


# ---- k8s_resource_present / k8s_resource_field / k8s_condition ---------------
# The k8s side of an operator test: what the API server accepted + what the operator
# wrote back. `_CR_FLAT` is a TeleportProvisionToken the API server stored intact and
# the operator reconciled; the nested-field CR is ABSENT because admission rejected it
# (the crdgen google.protobuf.Struct bug), which is exactly a missing-object FAIL.
_CR_FLAT = {
    "apiVersion": "resources.teleport.dev/v2", "kind": "TeleportProvisionToken",
    "metadata": {"name": "op-oidc-token", "namespace": "teleport"},
    "spec": {"join_method": "generic_oidc",
             "generic_oidc": {"must_match_fields": {"org": "ethernet-fyi"}}},
    "status": {"conditions": [
        {"type": "TeleportResourceReconciled", "status": "True", "message": "OK"},
        {"type": "SuccessfullyReconciled", "status": "False",
         "message": "unsupported field"},
    ]},
}


def test_k8s_resource_present_pass_and_missing_fail():
    c = FakeCluster(kube={"teleportprovisiontoken/op-oidc-token": _CR_FLAT})
    res = _run(c, "k8s_resource_present teleportprovisiontoken/op-oidc-token")
    assert res.status == "PASS"
    (p,) = res.proofs
    assert p.kind == "k8s-resource" and p.lang == "json" and '"op-oidc-token"' in p.content
    # a CR the API server refused at admission never exists -> FAIL
    assert _run(c, "k8s_resource_present teleportprovisiontoken/op-oidc-nested").status == "FAIL"


def test_k8s_resource_field_presence_value_and_missing():
    c = FakeCluster(kube={"teleportprovisiontoken/op-oidc-token": _CR_FLAT})
    base = "k8s_resource_field teleportprovisiontoken/op-oidc-token"
    assert _run(c, f"{base} spec.generic_oidc.must_match_fields").status == "PASS"
    ok = _run(c, f"{base} spec.generic_oidc.must_match_fields.org ETHERNET-fyi")
    assert ok.status == "PASS"  # case-insensitive, like resource_field
    assert _run(c, f"{base} spec.generic_oidc.must_match_fields.org wrong-org").status == "FAIL"
    # a path the API server pruned away -> FAIL
    assert _run(c, f"{base} spec.generic_oidc.must_match_fields.nested.deep").status == "FAIL"
    # missing object -> FAIL (and no proof to cite)
    miss = _run(FakeCluster(), f"{base} spec.join_method")
    assert miss.status == "FAIL" and miss.proofs == []


def test_k8s_condition_defaults_to_true_and_surfaces_message():
    c = FakeCluster(kube={"teleportprovisiontoken/op-oidc-token": _CR_FLAT})
    ref = "k8s_condition teleportprovisiontoken/op-oidc-token"
    assert _run(c, f"{ref} TeleportResourceReconciled").status == "PASS"  # implicit True
    # a controller reporting failure -> FAIL, with its message in the msg
    bad = _run(c, f"{ref} SuccessfullyReconciled")
    assert bad.status == "FAIL" and "unsupported field" in bad.msg
    # ...unless that's what the module expects
    assert _run(c, f"{ref} SuccessfullyReconciled False").status == "PASS"
    # a condition the controller never set at all -> FAIL, listing what it did set
    absent = _run(c, f"{ref} NeverSet")
    assert absent.status == "FAIL" and "TeleportResourceReconciled" in absent.msg


# ---- agent_result (agent-driven tests: an AI agent's findings) ---------------
from harness.agent import RESULT_RELPATH, TRANSCRIPT_RELPATH  # noqa: E402

_AGENT_OK = (
    '{"task":"onboard docbot via bound_keypair","status":"partial",'
    '"summary":"onboarded, but the guide never says how to authenticate tctl",'
    '"steps":[{"n":1,"action":"read /docs/getting-started.mdx","expected":"4 steps",'
    '"observed":"followed","ok":true,"doc_ref":"getting-started.mdx"},'
    '{"n":2,"action":"tctl create token","expected":"created","observed":"ok","ok":true}],'
    '"issues":[{"severity":"major","area":"docs","description":"no auth step for a fresh user",'
    '"evidence":"Step 2 assumes a logged-in tctl","suggested_fix":"add a login step"}]}'
)


def test_agent_result_advisory_pass_surfaces_findings():
    c = FakeCluster(state_files={RESULT_RELPATH: _AGENT_OK,
                                 TRANSCRIPT_RELPATH: '{"type":"result","total_cost_usd":0.1}'})
    res = _run(c, "agent_result")
    assert res.status == "PASS" and "advisory" in res.msg
    assert "status=partial" in res.msg and "1 issue" in res.msg
    # both the structured verdict and the transcript are captured as proof
    kinds = {p.title for p in res.proofs}
    assert any("verdict" in t for t in kinds) and any("transcript" in t for t in kinds)
    assert any(p.lang == "json" for p in res.proofs)
    # each step + issue becomes an assertion shown under the proof
    assert any("step 1" in a for a in res.assertions)
    assert any("major/docs" in a for a in res.assertions)


def test_agent_result_missing_fails():
    res = _run(FakeCluster(), "agent_result")
    assert res.status == "FAIL" and "no result" in res.msg


def test_agent_result_invalid_json_fails_with_raw_proof():
    c = FakeCluster(state_files={RESULT_RELPATH: "not json at all {"})
    res = _run(c, "agent_result")
    assert res.status == "FAIL" and "invalid" in res.msg
    assert res.proofs and "unparseable" in res.proofs[0].title


def test_agent_result_expected_status_gates():
    c = FakeCluster(state_files={RESULT_RELPATH: _AGENT_OK})  # status == "partial"
    assert _run(c, "agent_result partial").status == "PASS"
    assert _run(c, "agent_result pass").status == "FAIL"


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
        _node("c1-agent-expr"),  # predicate-expression agent (contains(set(claims.groups),"dev"))
    ]
    bots = ["gobot-disc", "gobot-static", "gobot-scoped-disc", "gobot-scoped-static"]
    auth_log = "\n".join(
        f"event_type:bot.join bot_name:{b} method:generic_oidc success:true" for b in bots
    )
    logs = {
        "agent-deny": "unable to validate generic_oidc token",
        "agent-scoped-deny": "denied: unable to join via generic_oidc",
        # expr-deny: same expression token, groups omit "dev" -> expression false -> denied
        "agent-expr-deny": "denied: unable to validate generic_oidc token",
        "auth": auth_log,
    }
    # structured audit events: the join_token.create the impersonator check now inspects
    events = [{"event": "join_token.create", "code": "TJ002I", "name": "agent-token",
               "impersonator": "bot-token-manager", "success": True}]
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
    c = FakeCluster(nodes=nodes, logs=logs, files=files, execs=execs, events=events)
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


def test_docs_bound_keypair_all_pass_simulated():
    """The agent-driven module passes when the agent produced a valid result (advisory) AND
    the objective end-state holds: docbot joined via bound_keypair and both resources exist."""
    m = load_module(MODULES / "docs_bound_keypair")
    docbot_token = {"kind": "token", "metadata": {"name": "docbot-token"},
                    "spec": {"roles": ["Bot"], "bot_name": "docbot", "join_method": "bound_keypair"}}
    docbot = {"kind": "bot", "metadata": {"name": "docbot"}, "spec": {"roles": ["access"]}}
    c = FakeCluster(
        logs={"auth": "bot.join bot_name:docbot method:bound_keypair success:true"},
        resources={"token/docbot-token": docbot_token, "bot/docbot": docbot},
        state_files={RESULT_RELPATH: _AGENT_OK},
    )
    results = verify(c, m.checks, module_dir=MODULES / "docs_bound_keypair")
    text, passed = render(results)
    assert passed, text


def test_get_resource_uses_two_arg_form_for_scope_qualified_names():
    """tctl's ONE-arg ref form splits on '/' and drops empty fields (services.ParseRef ->
    strings.FieldsFunc), so a scope's leading '/' is eaten and a scoped kind then rejects
    the ref as unqualified. get_resource must therefore pass `<scope>::<name>` as its own
    positional argument — and match the returned doc on the BARE name."""
    import json as _json
    import subprocess as _sp

    from harness.cluster import DockerCluster

    token = {"kind": "scoped_token", "scope": "/opscope",
             "metadata": {"name": "op-scoped-token"}}
    seen: list[list[str]] = []

    class RecordingCluster(DockerCluster):
        def _run(self, argv):
            seen.append(argv)
            return _sp.CompletedProcess(argv, 0, _json.dumps([token]), "")

    c = RecordingCluster("cid")

    assert c.get_resource("scoped_token", "/opscope::op-scoped-token") == token
    assert "scoped_token" in seen[-1] and "/opscope::op-scoped-token" in seen[-1]
    assert "scoped_token//opscope::op-scoped-token" not in seen[-1]

    # unscoped kinds keep the one-arg form every other module relies on
    c.get_resource("token", "op-oidc-token")
    assert "token/op-oidc-token" in seen[-1]


# ---- claim roll-up ----------------------------------------------------------
# A claim has THREE outcomes. Collapsing UNTESTED into FAIL would assert a disproof the run
# cannot support; collapsing it into PASS would report a green claim nothing exercised.
class _FakeClaim:
    def __init__(self, cid, statement="s", why="w"):
        self.id, self.statement, self.why = cid, statement, why


class _FakeModule:
    def __init__(self, *claims):
        self.claims = list(claims)


def _res(status, claim="", role="evidence"):
    return CheckResult(status, "m", "v", [], claim=claim, role=role)


def test_claim_proven_when_all_its_checks_pass():
    from harness.verify import claim_verdicts
    mod = _FakeModule(_FakeClaim("a"))
    out = claim_verdicts(mod, [_res("PASS", "a"), _res("PASS", "a"),
                               _res("PASS", role="precondition")])
    assert out[0]["verdict"] == "PROVEN" and out[0]["passed"] == 2 and out[0]["total"] == 2


def test_claim_disproven_when_one_of_its_checks_fails():
    from harness.verify import claim_verdicts
    out = claim_verdicts(_FakeModule(_FakeClaim("a")), [_res("PASS", "a"), _res("FAIL", "a")])
    assert out[0]["verdict"] == "DISPROVEN" and out[0]["passed"] == 1


def test_failed_precondition_makes_every_claim_untested_not_disproven():
    from harness.verify import claim_verdicts
    mod = _FakeModule(_FakeClaim("a"), _FakeClaim("b"))
    # the bot never joined, so status was never populated: the claims were not exercised.
    results = [_res("FAIL", role="precondition"), _res("PASS", "a"), _res("FAIL", "b")]
    assert [c["verdict"] for c in claim_verdicts(mod, results)] == ["UNTESTED", "UNTESTED"]


def test_claim_rollup_ignores_checks_belonging_to_other_claims():
    from harness.verify import claim_verdicts
    out = claim_verdicts(_FakeModule(_FakeClaim("a")), [_res("PASS", "a"), _res("FAIL", "b")])
    assert out[0]["verdict"] == "PROVEN" and out[0]["total"] == 1


# ---- observation verbs (the actor records; these judge) ----------------------
_OBS = [
    {"case": "spec-only-reapply", "at": "2026-08-11T04:00:00Z", "token": "t",
     "applied": "config/token-spec-only.yaml",
     "before": {"status.k": "ssh-real", "spec.limit": "1"},
     "after": {"status.k": "ssh-real", "spec.limit": "7"}},
    {"case": "wiped", "at": "2026-08-11T04:01:00Z", "token": "t", "applied": "x.yaml",
     "before": {"status.k": "ssh-real"}, "after": {"status.k": ""}},
]


def _obs_cluster(records=None, rc=0):
    payload = json.dumps(_OBS if records is None else records)
    return FakeCluster(execs={("act", ("cat", "/out/observations.json")): (rc, payload)})


def test_observation_unchanged_passes_and_proves_with_the_record():
    c = _obs_cluster()
    res = _run(c, "observation_unchanged act spec-only-reapply status.k")
    assert res.status == "PASS"
    (p,) = res.proofs
    # the proof is the RECORDED values, not a log line whose meaning lives in the script
    assert p.kind == "observation" and p.lang == "json"
    assert "ssh-real" in p.content and "2026-08-11T04:00:00Z" in p.content


def test_observation_unchanged_fails_and_reports_the_actual_transition():
    res = _run(_obs_cluster(), "observation_unchanged act wiped status.k")
    assert res.status == "FAIL"
    assert "'ssh-real' -> ''" in res.msg  # generated from data, not author prose


def test_observation_unchanged_fails_on_an_unrecorded_field():
    # a field the actor never captured must not read as "unchanged"
    res = _run(_obs_cluster(), "observation_unchanged act spec-only-reapply status.missing")
    assert res.status == "FAIL" and "not recorded" in res.msg


def test_observation_unchanged_checks_every_field_given():
    c = _obs_cluster([{"case": "c", "before": {"a": "1", "b": "2"}, "after": {"a": "1", "b": "9"}}])
    res = _run(c, "observation_unchanged act c a b")
    assert res.status == "FAIL" and "b" in res.msg and "'2' -> '9'" in res.msg


def test_observation_equals_asserts_the_after_value():
    c = _obs_cluster()
    assert _run(c, "observation_equals act spec-only-reapply spec.limit 7").status == "PASS"
    # proves the write LANDED, so "unchanged" is distinguishable from "never happened"
    assert _run(c, "observation_equals act spec-only-reapply spec.limit 1").status == "FAIL"


def test_observation_missing_case_lists_what_was_recorded():
    res = _run(_obs_cluster(), "observation_unchanged act never-ran status.k")
    assert res.status == "FAIL"
    assert "spec-only-reapply" in res.msg and "wiped" in res.msg


def test_observation_missing_file_is_a_clear_failure():
    res = _run(_obs_cluster(rc=1), "observation_unchanged act spec-only-reapply status.k")
    assert res.status == "FAIL" and "observations.json" in res.msg


def test_host_actor_observations_come_from_the_state_dir():
    """A host act() hook records to the state dir; a container actor to /out. Same contract."""
    c = FakeCluster(state_files={"observations-mymod.json": json.dumps(
        [{"case": "reapply", "before": {"a": "1"}, "after": {"a": "1"}}])})
    c.module = "mymod"
    assert _run(c, "observation_unchanged host reapply a").status == "PASS"


def test_host_actor_missing_records_names_the_expected_file():
    c = FakeCluster(state_files={})
    c.module = "mymod"
    res = _run(c, "observation_unchanged host reapply a")
    assert res.status == "FAIL" and "observations-mymod.json" in res.msg


def test_observation_proof_links_back_to_its_actor():
    c = _obs_cluster([{"case": "c", "actor": "scripts/mutate.sh",
                       "before": {"a": "1"}, "after": {"a": "1"}}])
    (p,) = _run(c, "observation_unchanged act c a").proofs
    # module-relative here; cmd_verify maps it to the bundle path (it knows the module)
    assert p.source == "scripts/mutate.sh"


def test_artifact_link_maps_a_module_root_actor():
    from harness.verify import artifact_link
    assert artifact_link("m", "checks.py")["path"] == "rendered/scripts/m/checks.py"
    assert artifact_link("m", "scripts/x.sh")["path"] == "rendered/scripts/m/x.sh"
    assert artifact_link("m", "config/t.yaml.j2")["path"] == "rendered/config/t.yaml"
