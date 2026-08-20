"""Host-side ACTOR for the TPM join matrix.

Every case here varies exactly ONE thing about a TPM join token and records what the
cluster did about it. The device never changes: prebuild.sh manufactured a single TPM whose
EKCert is signed by a CA we own, and each case asks "given THIS token, is that device
admitted?". Varying the token rather than the device is what makes the cases comparable —
and it means the whole matrix runs against one VM boot instead of one reprovision per case.

Two kinds of case:

* **join** — create the token, run the real `tbot` inside the VM against the real TPM, and
  record whether it joined. The interesting half is the denials: a token whose trust anchor
  is a CA that did not sign this EKCert, or whose allow rule pins a hash/serial the device
  does not have, must be refused.
* **create** — the token is never expected to exist. These cover admission-time validation
  (`ek_certificate_serial` without `ekcert_allowed_cas`), so the observation is whether
  `tctl create` was ACCEPTED, and no tbot runs at all.

It acts and records; module.yaml's `observation_*` checks judge. That split matters more
than usual here, because "denied" is only interesting if it was denied for the RIGHT
reason — so each record carries the denial text auth produced, and the report shows it
rather than a boolean this file decided.

Idempotent by caching: `run-plan` retries verify up to 8 times, and a matrix that re-ran
every attempt would turn one legitimate failure into eight full passes over the VM.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import time

ACTOR = "checks.py"  # recorded on each observation so its proof links back here

SCOPE = "/tpm-test"          # must match render.yaml `scope` + the scoped-* bootstrap
UNSCOPED_BOT = "tpm-bot"     # from render.yaml `bots:`
SCOPED_BOT = "tpm-scoped-bot"  # from bootstrap/scoped-2-bot.yaml

# Values the device provably does NOT have. Well-formed (so a rejection is about the VALUE
# not the syntax) and fixed, so a report reader can tell at a glance which field was
# falsified. The serial keeps Teleport's colon-delimited hex shape (lib/tpm.SerialString).
WRONG_HASH = "0000000000000000000000000000000000000000000000000000000000000000"
WRONG_SERIAL = "de:ad:be:ef:de:ad:be:ef:de:ad:be:ef:de:ad:be:ef"

CACHE = "tpm-cases.json"     # state-dir cache; see _cached/_store
AUTH_READY_TIMEOUT = 120.0
TBOT_TIMEOUT = 120.0


# --------------------------------------------------------------------------------------
# the matrix
# --------------------------------------------------------------------------------------
# Each shape isolates ONE decision in the join path:
#   ekcert  -> EKCert chain verification   (lib/tpm/validate.go verifyEKCert)
#   hash    -> allow-rule matching on ek_public_hash        (tpmjoin checkTPMAllowRules)
#   serial  -> allow-rule matching on ek_certificate_serial (tpmjoin checkTPMAllowRules)
#   rootca  -> the same verification as `ekcert`, but failing for a subtler reason: the
#              configured anchor IS in the device's chain, just not its immediate issuer.
#              Teleport verifies with Roots and no Intermediates pool, so a chain that
#              needs an intermediate cannot be built. Real manufacturer EK certs chain
#              through intermediates, so this is the mistake an admin makes when told to
#              "configure the manufacturer CA" and they reach for the root.
#
# `valid` says what SHOULD happen, not what did — the record carries what did.
JOIN_SHAPES = [
    ("ekcert", True), ("ekcert", False),
    ("hash", True), ("hash", False),
    ("serial", True), ("serial", False),
    ("rootca", False),
]

# Admission-time validation: a serial is a client-assertable value, so pinning one without
# a CA to verify the certificate it came from proves nothing. Both shapes must be REJECTED
# at create time. `serial-hash-no-cas` is the discriminating one — it is the only config
# the two candidate rules disagree about ("serial requires CAs" rejects it; "serial
# requires hash OR CAs" accepts it), so this case is what a rule change actually moves.
CREATE_SHAPES = ["serial-no-cas", "serial-hash-no-cas"]


def _case_id(scoped: bool, shape: str, valid: bool | None = None) -> str:
    prefix = "scoped" if scoped else "unscoped"
    if valid is None:
        return f"{prefix}-{shape}"
    return f"{prefix}-{shape}-{'valid' if valid else 'invalid'}"


def _token_name(case: str) -> str:
    return f"tpm-{case}"


def _tpm_block(shape: str, valid: bool, facts: dict) -> tuple[list[str], dict, str]:
    """(ekcert_allowed_cas PEMs, allow-rule fields, human note) for a join shape."""
    good, bad, root = facts["ca_good"], facts["ca_bad"], facts["ca_good_root"]
    real_hash, real_serial = facts["ek_public_hash"], facts["ek_certificate_serial"]

    if shape == "ekcert":
        # Rule held constant at the device's real hash so the ONLY difference between the
        # valid and invalid twin is which CA is trusted.
        return ([good] if valid else [bad], {"ek_public_hash": real_hash}, (
            "trust anchor is the CA that signed this EKCert" if valid
            else "trust anchor is an unrelated CA that signed nothing here"))
    if shape == "rootca":
        return ([root], {"ek_public_hash": real_hash},
                "trust anchor is the ROOT of the device's own chain, not its issuer")
    if shape == "hash":
        return ([good], {"ek_public_hash": real_hash if valid else WRONG_HASH},
                "allow rule pins the device's EK public hash" if valid
                else "allow rule pins a hash the device does not have")
    if shape == "serial":
        return ([good], {"ek_certificate_serial": real_serial if valid else WRONG_SERIAL},
                "allow rule pins the device's EKCert serial" if valid
                else "allow rule pins a serial the device does not have")
    raise ValueError(f"unknown shape {shape}")


def _create_shape(shape: str, facts: dict) -> tuple[list[str], dict, str]:
    """(cas, rule, note) for an admission-validation case — always CA-less by design."""
    if shape == "serial-no-cas":
        return ([], {"ek_certificate_serial": facts["ek_certificate_serial"]},
                "serial pinned with no CA to verify the certificate it came from")
    if shape == "serial-hash-no-cas":
        return ([], {"ek_certificate_serial": facts["ek_certificate_serial"],
                     "ek_public_hash": facts["ek_public_hash"]},
                "serial pinned alongside a hash, still with no CA")
    raise ValueError(f"unknown create shape {shape}")


# --------------------------------------------------------------------------------------
# resource YAML
# --------------------------------------------------------------------------------------
def _pem_list(pems: list[str]) -> str:
    if not pems:
        return "    ekcert_allowed_cas: []"
    out = ["    ekcert_allowed_cas:"]
    for pem in pems:
        out.append("      - |")
        out.extend(f"        {line}" for line in pem.strip().splitlines())
    return "\n".join(out)


def _rule_block(rule: dict) -> str:
    # Values are QUOTED, which is load-bearing rather than tidiness: an EKCert serial is
    # colon-delimited hex, and one that happens to be all digits (`12:34:56`) is a valid
    # YAML 1.1 sexagesimal integer — it would be parsed as 45296 and the rule would pin a
    # number rather than the serial. A pure-digit ek_public_hash has the same problem.
    keys = sorted(rule)  # stable output so a diff between cases is readable
    lines = [f'      - {keys[0]}: "{rule[keys[0]]}"']
    lines.extend(f'        {k}: "{rule[k]}"' for k in keys[1:])
    return "\n".join(lines)


def token_yaml(case: str, scoped: bool, cas: list[str], rule: dict) -> str:
    name = _token_name(case)
    tpm = f"  tpm:\n{_pem_list(cas)}\n    allow:\n{_rule_block(rule)}\n"
    if scoped:
        # A scoped token keeps its BARE metadata.name plus a separate `scope:`; only the
        # join reference is scope-qualified (see onboarding token below).
        return (f"kind: scoped_token\nversion: v1\nmetadata:\n  name: {name}\n"
                f"scope: {SCOPE}\nspec:\n  roles: [Bot]\n  join_method: tpm\n"
                f"  usage_mode: bot\n  bot: {SCOPE}::{SCOPED_BOT}\n{tpm}")
    return (f"kind: token\nversion: v2\nmetadata:\n  name: {name}\n"
            f'  expires: "3000-01-01T00:00:00Z"\n'
            f"spec:\n  roles: [Bot]\n  bot_name: {UNSCOPED_BOT}\n  join_method: tpm\n{tpm}")


def tbot_yaml(case: str, scoped: bool, proxy: str) -> str:
    # A scoped token is JOINED by its Scope-Qualified Name; a bare scoped name falls
    # through to the classic token lookup and comes back "token expired or not found",
    # which would read like a missing token rather than an addressing mistake.
    token = f"{SCOPE}::{_token_name(case)}" if scoped else _token_name(case)
    scoped_line = "scoped: true\n" if scoped else ""
    return (f"version: v2\n{scoped_line}proxy_server: {proxy}\n"
            f"onboarding:\n  join_method: tpm\n  token: {token}\n"
            f"storage: {{type: directory, path: /var/lib/tbot-{case}}}\n"
            f"outputs:\n  - type: identity\n"
            f"    destination: {{type: directory, path: /out/{case}}}\n")


# --------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------
def _auth(cluster, script: str) -> tuple[int, str]:
    """Run a shell snippet in the auth container with scopes enabled."""
    return cluster.exec_out("auth", ["sh", "-c", f"TELEPORT_UNSTABLE_SCOPES=yes {script}"])


def _wait_auth(cluster) -> bool:
    deadline = time.monotonic() + AUTH_READY_TIMEOUT
    while time.monotonic() < deadline:
        if _auth(cluster, "tctl status >/dev/null 2>&1")[0] == 0:
            return True
        time.sleep(3)
    return False


def _create_resource(cluster, yaml_text: str, name: str) -> tuple[int, str]:
    """`tctl create -f` a resource, returning (rc, combined output).

    Written via a file rather than stdin so a rejection's message is the server's, not a
    shell quoting artifact; base64 keeps the PEM bodies intact through two layers of shell.
    """
    b64 = base64.b64encode(yaml_text.encode()).decode()
    path = f"/tmp/tpm-{name}.yaml"
    return _auth(cluster, f"printf %s {shlex.quote(b64)} | base64 -d > {path} && "
                          f"tctl create -f {path} 2>&1")


def _vm(vm: str, argv: list[str], stdin: str | None = None,
        timeout: float = 60.0) -> tuple[int, str]:
    """Run a command inside the lima VM. The VM is host-side infrastructure, so this is a
    plain subprocess rather than anything on the Cluster seam."""
    try:
        cp = subprocess.run(["limactl", "shell", vm, "--", *argv],
                            input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


def _denial(text: str) -> str:
    """The most specific line explaining a refusal, for the record.

    Kept to the matched line (not the whole log) so the observation stays readable, but
    never summarised into a category — the point of recording it is that "denied" and
    "denied for the reason under test" are different findings. `requires Teleport
    Enterprise` is in the list because that is the one refusal that means the module ran
    against the wrong build rather than finding anything.
    """
    needles = ("ek cert", "ekcert", "allow rule", "unknown authority", "certificate",
               "access denied", "requires teleport enterprise", "not found", "denied")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    hits = [ln for ln in lines if any(n in ln.lower() for n in needles)]
    if hits:
        return hits[-1][:400]
    return lines[-1][:400] if lines else ""


def _record(case: str, before: dict, after: dict, note: str = "") -> dict:
    rec = {"case": case, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "actor": ACTOR, "before": before, "after": after}
    if note:
        rec["note"] = note
    return rec


def _cached(cluster) -> list[dict] | None:
    # TPM_FORCE=1 re-runs the matrix against an already-up cluster — the inner loop while
    # iterating on a case, since otherwise the cache makes every verify a replay.
    if os.environ.get("TPM_FORCE") == "1":
        return None
    raw = cluster.state_file(CACHE)
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, list) and doc else None


def _store(cluster, records: list[dict]) -> None:
    sd = getattr(cluster, "state_dir", None)
    if sd is not None:
        (sd / CACHE).write_text(json.dumps(records, indent=2) + "\n")


# --------------------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------------------
def _run_create_case(cluster, scoped: bool, shape: str, facts: dict) -> dict:
    case = _case_id(scoped, shape)
    cas, rule, note = _create_shape(shape, facts)
    rc, out = _create_resource(cluster, token_yaml(case, scoped, cas, rule), case)
    return _record(case, {
        "kind": "create",
        "scoped": str(scoped).lower(),
        "ekcert_allowed_cas": "none",
        "allow_rule": ", ".join(f"{k}={v}" for k, v in sorted(rule.items())),
        "expected": "rejected",
    }, {
        "accepted": "true" if rc == 0 else "false",
        "message": _denial(out) if rc != 0 else out.strip()[:400],
    }, note)


def _run_join_case(cluster, vm: str, proxy: str, scoped: bool, shape: str,
                   valid: bool, facts: dict) -> dict:
    case = _case_id(scoped, shape, valid)
    cas, rule, note = _tpm_block(shape, valid, facts)
    # Name the anchor rather than embedding a PEM: the record has to be readable in a
    # report, and which CA it was is the whole variable in half these cases.
    anchors = {facts["ca_good"]: "ca-good", facts["ca_bad"]: "ca-bad",
               facts["ca_good_root"]: "ca-good-root"}
    anchor = anchors.get(cas[0], "unknown") if cas else "none"
    before = {
        "kind": "join",
        "scoped": str(scoped).lower(),
        "ekcert_allowed_cas": anchor,
        "allow_rule": ", ".join(f"{k}={v}" for k, v in sorted(rule.items())),
        "expected": "join" if valid else "denied",
    }

    rc, out = _create_resource(cluster, token_yaml(case, scoped, cas, rule), case)
    if rc != 0:
        # The token itself was refused, so the join was never attempted. Recorded rather
        # than raised: a missing record is indistinguishable from a crashed actor, while a
        # recorded "token rejected" is a finding the checks can report precisely.
        return _record(case, before,
                       {"joined": "false", "tbot_exit": "-", "token_created": "false",
                        "message": _denial(out)},
                       note + " — token creation was REJECTED, so no join was attempted")

    # Fresh storage + output per attempt so a retry can never read a previous run's
    # identity and report a denial as a success.
    _vm(vm, ["sudo", "rm", "-rf", f"/var/lib/tbot-{case}", f"/out/{case}"])
    _vm(vm, ["sudo", "mkdir", "-p", f"/out/{case}"])
    rc, out = _vm(vm, ["sudo", "tee", f"/tmp/tbot-{case}.yaml"],
                  stdin=tbot_yaml(case, scoped, proxy))
    if rc != 0:
        return _record(case, before, {"joined": "false", "tbot_exit": "-",
                                      "token_created": "true", "message": f"config write failed: {out}"})

    rc, out = _vm(vm, ["sudo", "env", "TELEPORT_UNSTABLE_SCOPES=yes",
                       "tbot", "start", "--oneshot", "-c", f"/tmp/tbot-{case}.yaml"],
                  timeout=TBOT_TIMEOUT)
    # Two independent signals: tbot's exit status and whether an identity actually landed.
    # A join that "succeeded" without writing an identity is not a join.
    ident_rc, _ = _vm(vm, ["sudo", "test", "-s", f"/out/{case}/identity"])
    joined = rc == 0 and ident_rc == 0
    return _record(case, before, {
        "joined": "true" if joined else "false",
        "tbot_exit": str(rc),
        "token_created": "true",
        "message": "" if joined else _denial(out),
    }, note)


def act(cluster, nodes):
    """Run the matrix once and return one observation record per case."""
    cached = _cached(cluster)
    if cached:
        return cached

    raw = cluster.state_file("tpm/facts.json")
    if not raw:
        return [_record("setup", {"expected": "facts.json written by prebuild.sh"},
                        {"joined": "false", "message":
                         "no state/<id>/tpm/facts.json — prebuild.sh did not run or failed"})]
    facts = json.loads(raw)
    vm = facts["vm"]

    if not _wait_auth(cluster):
        return [_record("setup", {"expected": "auth reachable"},
                        {"joined": "false", "message":
                         "auth never became ready, so no token could be created"})]

    proxy = cluster.proxy_addr()
    records: list[dict] = []
    for scoped in (False, True):
        for shape in CREATE_SHAPES:
            records.append(_run_create_case(cluster, scoped, shape, facts))
        for shape, valid in JOIN_SHAPES:
            records.append(_run_join_case(cluster, vm, proxy, scoped, shape, valid, facts))

    _store(cluster, records)
    return records
