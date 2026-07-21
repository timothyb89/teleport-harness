"""Restart-based checks for the apply-on-startup fix — the properties that only appear
when `teleport start --apply-on-startup` RE-RUNS on a restart (the plain declarative
verbs prove the first-apply/join). All expressed against the live token status the seam
reads via `tctl get token/bk-token`.

Flow (idempotent, so the plan's verify-retry loop can re-run it safely):
  1. wait for the positive bot to finish its bound_keypair registration (status.bound_keypair
     .bound_public_key becomes non-empty) — the real, server-owned join state.
  2. rewrite the applied token YAML IN PLACE with (a) a CHANGED spec (recovery.limit 1 -> 5)
     and (b) a BOGUS status (fake bound_public_key / recovery_count / registration_secret),
     then restart auth so teleport re-applies it.
  3. re-read the token and assert:
       - status-preserved-across-restart: bound_public_key unchanged (not wiped),
       - config-supplied-status-discarded: the bogus status did NOT land,
       - spec-updated-on-reapply: the changed spec field DID land,
       - registration-secret-unchanged: status registration secret not overwritten.

This mirrors lib/auth's TestInit_ApplyOnStartup_BoundKeypair but end-to-end: a real tbot
join populates the status and a real `teleport` process restart runs the re-apply path.
"""

from __future__ import annotations

import base64

from harness.verify import FAIL, PASS, CheckResult, ProofItem

TOKEN = "bk-token"
# must match render.yaml `reg_secret` (the value tbot presents + the token's spec onboarding).
REG_SECRET = "harness-bk-regsecret"

# The bogus status + changed spec we re-apply. If the fix regresses (re-apply overwrites
# status), the bogus key/count would land and/or the real bound key would be wiped.
BOGUS_KEY = "ssh-ed25519 AAAAbogusbogusbogusconfigsuppliedkey"
BOGUS_COUNT = 99
BOGUS_SECRET = "bogus-config-supplied-secret"
NEW_LIMIT = 5  # spec.bound_keypair.recovery.limit is 1 in apply_on_startup/token.yaml.j2

# How long to wait for the positive bot to finish its first join (bind a key). Module-level
# so a unit test can shrink it (a never-bound fake would otherwise poll for the full window).
BOUND_WAIT_TIMEOUT = 120.0
BOUND_WAIT_INTERVAL = 3.0

MODIFIED_TOKEN_YAML = f"""kind: token
version: v2
metadata:
  name: {TOKEN}
  expires: "3000-01-01T00:00:00Z"
spec:
  roles: [Bot]
  bot_name: bk-bot
  join_method: bound_keypair
  bound_keypair:
    onboarding:
      registration_secret: {REG_SECRET}
    recovery:
      limit: {NEW_LIMIT}
      mode: insecure
status:
  bound_keypair:
    bound_public_key: "{BOGUS_KEY}"
    recovery_count: {BOGUS_COUNT}
    registration_secret: {BOGUS_SECRET}
"""


def _dig(doc, path):
    """Walk a dotted path through nested dicts; return (found, value)."""
    cur = doc
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _bk_status(cluster):
    """(status.bound_keypair dict, full token doc) — {} / None if the token is absent."""
    doc = cluster.get_resource("token", TOKEN)
    if not doc:
        return {}, None
    _, st = _dig(doc, "status.bound_keypair")
    return (st if isinstance(st, dict) else {}), doc


def _wait_bound(cluster, timeout=None, interval=None):
    """Poll until the positive bot has bound a key (or timeout). Returns the status dict."""
    import time
    timeout = BOUND_WAIT_TIMEOUT if timeout is None else timeout
    interval = BOUND_WAIT_INTERVAL if interval is None else interval
    deadline = time.monotonic() + timeout
    while True:
        st, _ = _bk_status(cluster)
        if st.get("bound_public_key"):
            return st
        if time.monotonic() >= deadline:
            return st
        time.sleep(interval)


def _status_proof(title, st):
    return ProofItem("text", title,
                     f"bound_public_key={st.get('bound_public_key', '')!r}\n"
                     f"recovery_count={st.get('recovery_count', '')!r}\n"
                     f"registration_secret={st.get('registration_secret', '')!r}")


def checks(cluster, nodes):
    results = []

    # 1) The real, server-owned join state produced against the apply-on-startup token.
    before = _wait_bound(cluster)
    real_key = before.get("bound_public_key", "")
    if not real_key:
        results.append(CheckResult(
            FAIL,
            f"positive bot never bound a key against the apply-on-startup token "
            f"(status.bound_keypair.bound_public_key empty on token/{TOKEN}) — the "
            f"apply-on-startup token was unusable",
            verb="bk_reapply", args=["bound"],
            proofs=[_status_proof(f"token/{TOKEN} status.bound_keypair (never bound)", before)]))
        return results  # nothing downstream is meaningful without a real bound key
    real_count = before.get("recovery_count")
    real_secret = before.get("registration_secret", "")
    results.append(CheckResult(
        PASS,
        f"apply-on-startup initialized a usable status.bound_keypair: a real tbot bound a "
        f"key against token/{TOKEN}",
        verb="bk_reapply", args=["bound"],
        proofs=[_status_proof(f"token/{TOKEN} status.bound_keypair after the real join", before)]))

    # 2) Re-apply a variant with a CHANGED spec + a BOGUS status, then restart auth so
    #    teleport re-runs --apply-on-startup. Written into the read-write apply-on-startup
    #    mount via docker exec (base64 to avoid any quoting hazards), replacing whatever is
    #    there so the combined doc is exactly ours (idempotent across verify retries).
    b64 = base64.b64encode(MODIFIED_TOKEN_YAML.encode()).decode()
    # Overwrite the applied token file IN PLACE (preserving its rendered name, so the
    # report's setup.json source link still resolves) rather than adding a second doc.
    rc, out = cluster.exec_out("auth", ["sh", "-c",
        'f="$(ls /apply-on-startup/*.yaml 2>/dev/null | head -1)"; '
        '[ -n "$f" ] || f=/apply-on-startup/token.yaml; '
        f"printf %s '{b64}' | base64 -d > \"$f\""])
    if rc != 0:
        results.append(CheckResult(
            FAIL, f"could not rewrite the apply-on-startup token in the auth container (exit {rc})",
            verb="bk_reapply", args=["rewrite"],
            proofs=[ProofItem("command", "rewrite /apply-on-startup/override.yaml", out)]))
        return results

    if not cluster.restart_auth():
        results.append(CheckResult(
            FAIL, "auth did not come back healthy after restart (could not exercise re-apply)",
            verb="bk_reapply", args=["restart"]))
        return results

    after, after_doc = _bk_status(cluster)
    if after_doc is None:
        results.append(CheckResult(
            FAIL, f"token/{TOKEN} missing after restart", verb="bk_reapply", args=["reread"]))
        return results
    after_key = after.get("bound_public_key", "")
    after_count = after.get("recovery_count")
    after_secret = after.get("registration_secret", "")
    _, new_limit = _dig(after_doc, "spec.bound_keypair.recovery.limit")
    reapply_proof = _status_proof(
        f"token/{TOKEN} status.bound_keypair after re-apply + restart", after)

    # status preserved: the real bound key survived the restart's re-apply.
    if after_key == real_key:
        results.append(CheckResult(
            PASS, "re-apply on restart PRESERVED the bound public key (status not reset)",
            verb="bk_reapply", args=["status-preserved"], proofs=[reapply_proof]))
    else:
        results.append(CheckResult(
            FAIL, f"re-apply reset the bound public key: was {real_key!r}, now {after_key!r}",
            verb="bk_reapply", args=["status-preserved"], proofs=[reapply_proof]))

    # config-supplied status discarded: none of the bogus values landed.
    landed = [f"{n}={v!r}" for n, v, bogus in
              (("bound_public_key", after_key, BOGUS_KEY),
               ("recovery_count", after_count, BOGUS_COUNT),
               ("registration_secret", after_secret, BOGUS_SECRET))
              if str(v) == str(bogus)]
    if not landed:
        results.append(CheckResult(
            PASS, "config-supplied status in the re-applied YAML was silently discarded "
                  "(bogus bound_public_key / recovery_count / registration_secret ignored)",
            verb="bk_reapply", args=["status-discarded"], proofs=[reapply_proof]))
    else:
        results.append(CheckResult(
            FAIL, f"config-supplied status leaked into the stored token: {', '.join(landed)}",
            verb="bk_reapply", args=["status-discarded"], proofs=[reapply_proof]))

    # spec freely updated on re-apply: the changed recovery.limit landed.
    spec_proof = ProofItem("text", f"token/{TOKEN} spec.bound_keypair.recovery.limit after re-apply",
                           f"limit={new_limit!r} (was 1, re-applied as {NEW_LIMIT})")
    if str(new_limit) == str(NEW_LIMIT):
        results.append(CheckResult(
            PASS, f"re-apply UPDATED spec (recovery.limit 1 -> {NEW_LIMIT}) while leaving status intact",
            verb="bk_reapply", args=["spec-updated"], proofs=[spec_proof]))
    else:
        results.append(CheckResult(
            FAIL, f"re-apply did not update spec.recovery.limit (got {new_limit!r}, expected {NEW_LIMIT})",
            verb="bk_reapply", args=["spec-updated"], proofs=[spec_proof]))

    # registration secret in status is authoritative: unchanged by the re-apply.
    if after_secret == real_secret:
        results.append(CheckResult(
            PASS, "re-apply did not modify the stored registration secret",
            verb="bk_reapply", args=["secret-unchanged"], proofs=[reapply_proof]))
    else:
        results.append(CheckResult(
            FAIL, f"re-apply changed the stored registration secret: was {real_secret!r}, now {after_secret!r}",
            verb="bk_reapply", args=["secret-unchanged"], proofs=[reapply_proof]))

    return results
