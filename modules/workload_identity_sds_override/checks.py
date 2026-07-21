"""Custom checks for workload_identity_sds_override (escape hatch — run after the declarative
`checks:` and merged into the results). Two things the declarative verbs can't express:

  * control        — parse the file-output svid.pem and confirm it's a valid MULTI-cert chain
                     (the override chain serialized correctly, one PEM block per cert). Proves
                     issuance + the override are fine, so the bug is isolated to the SDS handler.
                     Also guards a false-green: on an OSS build there's no chain -> 1 block -> FAIL.
  * SDS probe      — read Envoy's own SDS accounting (admin :9901 /stats) to see whether Envoy
                     ACCEPTED or REJECTED the served secrets. `update_rejected == 0` is the flip
                     (FAIL today, PASS once the handler is fixed); a positive `update_success`
                     guards against a false-green where Envoy never reached tbot at all.

Both degrade to SKIP (neutral) if the probe can't run, so they never fail the run spuriously —
the declarative log_count checks remain the hard gate.
"""

from __future__ import annotations

from harness.verify import CheckResult, ProofItem, PASS, FAIL, SKIP

SVID = "/out/x509/svid.pem"
ENVOY_ADMIN = "http://wi-envoy:9901"  # wi-envoy service name resolves on the internal network


def _sh(cluster, suffix: str, script: str) -> tuple[int, str]:
    return cluster.exec_out(suffix, ["sh", "-c", script])


def _control_file_chain(cluster) -> CheckResult:
    """The x509 FILE output writes the SAME SVID + override chain, one PEM block per cert."""
    _, count_out = _sh(cluster, "wi-tbot",
                       f"grep -c '^-----BEGIN CERTIFICATE-----' {SVID} 2>/dev/null || echo 0")
    try:
        n = int(count_out.strip().splitlines()[-1]) if count_out.strip() else 0
    except (ValueError, IndexError):
        n = 0
    # Best-effort human-readable chain listing for the proof.
    _, listing = _sh(cluster, "wi-tbot",
                     f"openssl crl2pkcs7 -nocrl -certfile {SVID} 2>/dev/null "
                     f"| openssl pkcs7 -print_certs -noout 2>/dev/null")
    proof = ProofItem("command", f"wi-tbot:{SVID} — {n} PEM CERTIFICATE block(s)",
                      listing.strip() or f"(svid.pem: {n} block(s); openssl listing unavailable)")
    asserts = ["file-output svid.pem CERTIFICATE blocks >= 2"]
    if n >= 2:
        return CheckResult(PASS, f"control: file-output svid.pem is a valid {n}-cert chain "
                           "(override chain serialized correctly — one PEM block per cert)",
                           proofs=[proof], assertions=asserts)
    return CheckResult(FAIL, f"control: file-output svid.pem has {n} cert block(s), expected >= 2 "
                       "(no override chain present — needs --ent + the override)",
                       proofs=[proof], assertions=asserts)


def _envoy_sds_stats(cluster) -> tuple[dict, str]:
    """Return ({success,rejected,attempt}, raw) summed across SDS secret stats, or ({}, raw)
    if the admin endpoint couldn't be reached."""
    _, raw = _sh(cluster, "wi-tbot",
                 f"curl -s --max-time 5 {ENVOY_ADMIN}/stats 2>/dev/null "
                 "| grep -Ei 'update_(success|rejected|attempt|failure)' || true")
    lines = [ln for ln in raw.splitlines() if ":" in ln]
    if not lines:
        return {}, raw
    totals = {"success": 0, "rejected": 0, "attempt": 0}
    for ln in lines:
        name, _, val = ln.partition(":")
        try:
            v = int(val.strip())
        except ValueError:
            continue
        if "update_success" in name:
            totals["success"] += v
        elif "update_rejected" in name or "update_failure" in name:
            totals["rejected"] += v
        elif "update_attempt" in name:
            totals["attempt"] += v
    return totals, raw


def _sds_probe(cluster) -> list[CheckResult]:
    totals, raw = _envoy_sds_stats(cluster)
    if not totals:
        skip = CheckResult(SKIP, "Envoy SDS stats not reachable yet (admin :9901); relying on the "
                           "log_count checks", proofs=[ProofItem("command",
                           "wi-tbot -> curl wi-envoy:9901/stats", raw.strip() or "(no output)")])
        return [skip]
    proof = ProofItem("command", "Envoy SDS update stats (admin :9901/stats)", raw.strip())
    reached = totals["success"] > 0 or totals["attempt"] > 0
    connected = CheckResult(
        PASS if reached else FAIL,
        f"Envoy reached tbot's SDS endpoint (update_success={totals['success']}, "
        f"update_attempt={totals['attempt']})" if reached else
        "Envoy never completed an SDS exchange with tbot (0 success/attempt) — SDS channel broken",
        proofs=[proof], assertions=["Envoy SDS update_success or update_attempt > 0"])
    accepted = CheckResult(
        PASS if totals["rejected"] == 0 else FAIL,
        f"Envoy accepted every served SDS secret (update_rejected={totals['rejected']})"
        if totals["rejected"] == 0 else
        f"Envoy REJECTED {totals['rejected']} SDS update(s) — the malformed cert chain "
        "(the bug; flips to PASS when the SDS handler serializes each cert in its own PEM block)",
        proofs=[proof], assertions=["Envoy SDS update_rejected == 0"])
    return [connected, accepted]


def checks(cluster, nodes) -> list[CheckResult]:
    return [_control_file_chain(cluster), *_sds_probe(cluster)]
