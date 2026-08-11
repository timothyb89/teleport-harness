"""Host-side ACTOR for the apply-on-startup re-apply path.

This module's interesting properties only appear when `teleport start --apply-on-startup`
RE-RUNS, which needs an auth restart — something only the host can do, so the actor lives
here rather than in a container. It does not judge: `act()` drives the scenario and returns
observation records, and module.yaml's `observation_*` checks do the asserting.

The split matters for the same reason it does for a shell actor: when the actor decides the
verdict, the report can only show a sentence the module wrote about itself. Recording makes
the proof the observed VALUES and the detail text generated from them, so it cannot drift
away from what the code actually compared.

Flow (idempotent, so the plan's verify-retry loop can re-run it safely):
  1. wait for the positive bot to finish its bound_keypair registration — the real,
     server-owned join state (`bound_public_key` becomes non-empty).
  2. rewrite the applied token YAML IN PLACE with (a) a CHANGED spec (recovery.limit 1 -> 5)
     and (b) a BOGUS status, then restart auth so teleport re-applies it.
  3. re-read and record before/after, so the checks can assert that status survived, the
     config-supplied status did not land, and the spec change did.

Mirrors lib/auth's TestInit_ApplyOnStartup_BoundKeypair, but end-to-end: a real tbot join
populates the status and a real `teleport` process restart runs the re-apply path.
"""

from __future__ import annotations

import base64
import time

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

ACTOR = "checks.py"  # recorded on each observation so its proof links back here

# The fields the checks assert over. Kept flat and dotted so a record reads the same way as
# the shell actor's in bound_keypair_status — one contract, two actor kinds.
FIELDS = (
    "status.bound_keypair.bound_public_key",
    "status.bound_keypair.recovery_count",
    "status.bound_keypair.registration_secret",
    "spec.bound_keypair.recovery.limit",
)

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
    cur = doc
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _snapshot(cluster) -> dict:
    """Every field under test, as strings — the same shape a shell actor records."""
    doc = cluster.get_resource("token", TOKEN) or {}
    out = {}
    for f in FIELDS:
        found, v = _dig(doc, f)
        out[f] = "" if not found or v is None else str(v)
    return out


def _wait_bound(cluster, timeout=None, interval=None) -> dict:
    timeout = BOUND_WAIT_TIMEOUT if timeout is None else timeout
    interval = BOUND_WAIT_INTERVAL if interval is None else interval
    deadline = time.monotonic() + timeout
    while True:
        snap = _snapshot(cluster)
        if snap.get("status.bound_keypair.bound_public_key"):
            return snap
        if time.monotonic() >= deadline:
            return snap
        time.sleep(interval)


def _record(case, before, after, note=""):
    rec = {"case": case, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "token": TOKEN, "applied": "apply_on_startup/token.yaml", "actor": ACTOR,
           "before": before, "after": after}
    if note:
        rec["note"] = note
    return rec


def act(cluster, nodes):
    """Drive the re-apply and return observation records (see module docstring)."""
    before = _wait_bound(cluster)
    if not before.get("status.bound_keypair.bound_public_key"):
        # Record the failure rather than raising: a missing record is indistinguishable from
        # a crashed actor, while a recorded empty key is a finding the checks can report.
        return [_record("reapply-on-restart", before, before,
                        note="the positive bot never bound a key; apply-on-startup produced "
                             "an unusable status.bound_keypair, so nothing was re-applied")]

    # Overwrite the applied token file IN PLACE (preserving its rendered name, so the
    # report's setup.json source link still resolves) rather than adding a second doc.
    # base64 avoids any quoting hazards through two layers of shell.
    b64 = base64.b64encode(MODIFIED_TOKEN_YAML.encode()).decode()
    rc, out = cluster.exec_out("auth", ["sh", "-c",
        'f="$(ls /apply-on-startup/*.yaml 2>/dev/null | head -1)"; '
        '[ -n "$f" ] || f=/apply-on-startup/token.yaml; '
        f"printf %s '{b64}' | base64 -d > \"$f\""])
    if rc != 0:
        return [_record("reapply-on-restart", before, before,
                        note=f"could not rewrite the apply-on-startup token (exit {rc}): {out}")]

    if not cluster.restart_auth():
        return [_record("reapply-on-restart", before, before,
                        note="auth did not come back healthy after restart, so the re-apply "
                             "path was never exercised")]

    return [_record("reapply-on-restart", before, _snapshot(cluster))]
