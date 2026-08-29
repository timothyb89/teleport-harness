#!/usr/bin/env bash
# A real SPIFFE Workload API client against tbot's socket: retry `tbot spiffe-inspect` until it
# returns an SVID or the deadline passes, reporting the current state on a parseable line after
# EVERY attempt.
#
# This ACTS and REPORTS; it does not judge (checks.py's act() reads the latest line into a
# recorded observation, and the module's verbs do the asserting).
#
# WHY IT REPORTS EVERY TIME RATHER THAN ONCE AT THE END. run-plan re-runs a module's checks on a
# retry loop with a bounded budget (~90s; lib/plan.sh), which is how it absorbs services that
# take a while to settle. A probe that only speaks when it finishes is silent for that whole
# window on a build where the SVID never arrives — so the observation reads "the probe did not
# run", the precondition fails, and the claims are marked UNTESTED. That is precisely backwards:
# not getting an SVID is the FINDING on such a build, not missing scaffolding. Reporting every
# attempt makes "still trying" and "gave up" both observable at any instant, and lets the outer
# retry loop do the waiting — the first attempts report served=no and the checks simply retry.
set -uo pipefail

CASE="${CASE:?CASE is required}"
SOCKET="${SOCKET:?SOCKET is required}"
DEADLINE="${DEADLINE:-240}"

start="$(date +%s)"
served=no
spiffe_id=""
attempts=0
last_err=""

echo "[probe] case=$CASE socket=$SOCKET deadline=${DEADLINE}s — waiting for an SVID"

# checks.py parses the LAST of these, so it always sees the probe's current state. While the
# probe is still trying, `elapsed` is how long it has been trying; once served=yes it is the
# time to first SVID. Keep the key=value shape stable.
report() {
  echo "[probe] RESULT case=$CASE served=$served elapsed=$(( $(date +%s) - start )) attempts=$attempts spiffe_id=${spiffe_id:-none}"
}

while :; do
  elapsed=$(( $(date +%s) - start ))
  [ "$elapsed" -ge "$DEADLINE" ] && break
  attempts=$(( attempts + 1 ))

  # Capture first, then grep: the harness runs with pipefail, and `cmd | grep -q` reports the
  # producer's SIGPIPE on an early match, which reads as "no match".
  if out="$(tbot spiffe-inspect --path "$SOCKET" 2>&1)"; then
    spiffe_id="$(grep -oE 'spiffe://[^ ]+' <<<"$out" | head -1)"
    if [ -n "$spiffe_id" ]; then
      served=yes
      break
    fi
    last_err="inspect succeeded but returned no SPIFFE ID"
  else
    last_err="$(tail -1 <<<"$out")"
  fi
  report
  sleep 2
done

if [ "$served" = yes ]; then
  echo "[probe] got SVID: $spiffe_id"
else
  echo "[probe] no SVID within ${DEADLINE}s; last error: ${last_err:-<none>}"
fi
report

# Stay up so the container is still there to read logs from at verify time (and so a plan that
# reuses the cluster can re-verify without re-running the probe).
exec sleep infinity
