#!/bin/sh
# bound_keypair_status mutator.
#
# Proves the rule: for an EXISTING bound_keypair token, `.status` is always preserved and any
# `.status` on an incoming copy is always discarded; delete + recreate is the only way to set
# it. Everything here runs the way a real admin tool does — `tctl create -f` over a bot
# identity — so it exercises the same UpsertToken RPC that tctl, the Terraform provider and
# the Kubernetes operator all reach.
#
# READ-AFTER-WRITE IS NOT COHERENT HERE. `tctl get` resolves through the auth server's CACHE
# (Server embeds both *Services and authclient.Cache, and Cache wins for GetToken), so a read
# taken straight after an apply can still return the PRE-write token. That makes "status was
# preserved" indistinguishable from "I read a stale copy of the old status" — a pre-fix run
# passed upsert-discards-status for exactly that reason, a false green on a build with the
# bug. So every apply carries a DISTINCT spec sentinel (recovery.limit) and every assertion
# waits for that sentinel to become visible first. Once the new limit is readable the write
# has landed, and the status read alongside it is the real post-write status.
#
# Emits one `RESULT <case>: PASS|FAIL` line per case; module.yaml gates on those with
# log_count (NOT log_contains, which SKIPs on no match and would hide a regression).
#
# Deliberately does NOT `set -e`: a failing case is the finding, and aborting would leave the
# later cases unreported — which reads identically to "the mutator crashed".

set -u

TCTL="tctl --identity ${IDENTITY} --auth-server ${AUTH_ADDR}"
BARRIER_TRIES=40        # × 1s; cache lag is sub-second, this is pure headroom

log()    { echo "[mutate] $*"; }
result() { echo "RESULT $1: $2"; }

# `tctl get token/<name> --format json` emits a JSON ARRAY even for one resource, hence .[0].
# Tokens are always fetched with secrets (tctl forces it: "tokens cannot be retrieved without
# secrets"), and a BOT identity is exempt from admin-action MFA, so this needs no login.
field() { # field <token-name> <jq-path-after-.[0]>
    $TCTL get "token/$1" --format json 2>/dev/null | jq -r ".[0].$2 // empty" 2>/dev/null
}

# Barrier: block until <path> reads <expected>. Returns 1 on timeout, and the caller reports
# the case FAILed — a write that never becomes visible is a real defect, not a flake to skip.
wait_field() { # wait_field <token> <jq-path> <expected>
    _i=0
    while [ "$_i" -lt "$BARRIER_TRIES" ]; do
        [ "$(field "$1" "$2")" = "$3" ] && return 0
        _i=$((_i + 1)); sleep 1
    done
    log "  TIMEOUT waiting for $1.$2 to read '$3' (last: '$(field "$1" "$2")')"
    return 1
}

wait_present() { # wait_present <token>
    _i=0
    while [ "$_i" -lt "$BARRIER_TRIES" ]; do
        [ -n "$(field "$1" 'metadata.name')" ] && return 0
        _i=$((_i + 1)); sleep 1
    done
    log "  TIMEOUT waiting for $1 to exist"; return 1
}

wait_absent() { # wait_absent <token>
    _i=0
    while [ "$_i" -lt "$BARRIER_TRIES" ]; do
        [ -z "$(field "$1" 'metadata.name')" ] && return 0
        _i=$((_i + 1)); sleep 1
    done
    log "  TIMEOUT waiting for $1 to disappear"; return 1
}

expect_eq() { # expect_eq <case> <what> <expected> <actual>
    if [ "$3" = "$4" ]; then
        result "$1" "PASS"; log "  ok   $2: $4"
    else
        result "$1" "FAIL"; log "  BAD  $2: expected '$3', got '$4'"
    fi
}

# Compare the whole server-owned triple against the baseline captured before any apply.
compare_status() { # compare_status <case>
    _key=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_public_key')
    _inst=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_bot_instance_id')
    _cnt=$(field "$TOKEN_NAME" 'status.bound_keypair.recovery_count')
    _sec=$(field "$TOKEN_NAME" 'status.bound_keypair.registration_secret')
    if [ "$_key" = "$KEY0" ] && [ "$_inst" = "$INST0" ] && [ "$_cnt" = "$CNT0" ] && [ "$_sec" = "$SEC0" ]; then
        result "$1" "PASS"
        log "  ok   bound key, bot instance, recovery counter and secret all unchanged"
    else
        result "$1" "FAIL"
        log "  BAD  bound_public_key      '${KEY0}' -> '${_key}'"
        log "  BAD  bound_bot_instance_id '${INST0}' -> '${_inst}'"
        log "  BAD  recovery_count        '${CNT0}' -> '${_cnt}'"
        log "  BAD  registration_secret   '${SEC0}' -> '${_sec}'"
    fi
}

###############################################################################
# 0. Precondition — wait for the bot to actually bind a keypair.
#    Until the join ceremony completes there is no server-owned status to preserve, and
#    every case below would trivially "pass" against an empty status.
###############################################################################
log "waiting for ${TOKEN_NAME} to have a bound public key (bot join in progress)"
i=0
KEY0=""
while [ "$i" -lt 120 ]; do
    KEY0=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_public_key')
    [ -n "$KEY0" ] && break
    i=$((i + 1)); sleep 2
done

if [ -z "$KEY0" ]; then
    log "FATAL: ${TOKEN_NAME} never got a bound_public_key; the bot did not join."
    result "mutator" "ABORTED-NO-BOUND-KEY"
    touch /tmp/mutate-done
    exec sleep infinity
fi

INST0=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_bot_instance_id')
CNT0=$(field "$TOKEN_NAME" 'status.bound_keypair.recovery_count')
SEC0=$(field "$TOKEN_NAME" 'status.bound_keypair.registration_secret')
log "baseline status.bound_keypair:"
log "  bound_public_key      = ${KEY0}"
log "  bound_bot_instance_id = ${INST0}"
log "  recovery_count        = ${CNT0}"
log "  registration_secret   = ${SEC0}"

###############################################################################
# 1. Spec-only re-apply — the tctl/terraform/operator roundtrip.
#    The incoming copy has NO status at all. This is the case that knocks bots offline:
#    without the fix the server builds a fresh status, copies the spec's registration
#    secret into it, and the bound key is simply gone.
###############################################################################
log "case 1: re-applying ${TOKEN_NAME} with NO status (recovery.limit -> ${NEW_RECOVERY_LIMIT})"
if ! $TCTL create -f /work/token-spec-only.yaml; then
    log "  apply FAILED (an update should be accepted; only the status must be ignored)"
    result "spec-only-reapply-preserves-status" "FAIL"
    result "spec-edit-applied" "FAIL"
elif ! wait_field "$TOKEN_NAME" 'spec.bound_keypair.recovery.limit' "$NEW_RECOVERY_LIMIT"; then
    result "spec-only-reapply-preserves-status" "FAIL"
    result "spec-edit-applied" "FAIL"
else
    # The barrier IS the spec-edit assertion: the new limit is readable, so the update
    # landed. Guards the over-correction where "preserve status" degrades into "ignore
    # the whole update".
    result "spec-edit-applied" "PASS"
    log "  ok   spec.bound_keypair.recovery.limit: ${NEW_RECOVERY_LIMIT}"
    compare_status "spec-only-reapply-preserves-status"
fi

###############################################################################
# 2. Tampered-status re-apply — explicit attempt to write server-owned state.
#    Every status value in this copy is wrong (a key the bot does not hold, a nil-UUID
#    instance, a reset counter, an unissued secret). None may reach storage.
###############################################################################
log "case 2: re-applying ${TOKEN_NAME} WITH a tampered status (recovery.limit -> ${TAMPERED_RECOVERY_LIMIT})"
if ! $TCTL create -f /work/token-tampered.yaml; then
    # A hard rejection is a defensible design too, but it is not the rule this module
    # encodes (silent discard, so terraform/operator do not reconcile-loop forever).
    log "  apply was REJECTED outright; the rule under test is silent discard"
    result "tampered-status-discarded" "FAIL"
elif ! wait_field "$TOKEN_NAME" 'spec.bound_keypair.recovery.limit' "$TAMPERED_RECOVERY_LIMIT"; then
    result "tampered-status-discarded" "FAIL"
else
    compare_status "tampered-status-discarded"
fi

###############################################################################
# 3. The escape hatch, on a spare token nothing joins with.
#    create accepts status -> upsert discards it -> delete+recreate sets it. Same marker-B
#    file in steps b and c; the ONLY difference is whether the token already existed.
###############################################################################
log "case 3a: creating ${SPARE_TOKEN} with status marker A"
$TCTL rm "token/${SPARE_TOKEN}" >/dev/null 2>&1   # idempotent: tolerate a re-run
wait_absent "$SPARE_TOKEN" >/dev/null 2>&1
if ! $TCTL create -f /work/spare-marker-a.yaml; then
    log "  create FAILED"; result "create-accepts-status" "FAIL"
elif ! wait_present "$SPARE_TOKEN"; then
    result "create-accepts-status" "FAIL"
else
    expect_eq "create-accepts-status" "status.bound_keypair.registration_secret" \
        "$MARKER_A" "$(field "$SPARE_TOKEN" 'status.bound_keypair.registration_secret')"
fi

log "case 3b: upserting ${SPARE_TOKEN} with status marker B (must be discarded)"
if ! $TCTL create -f /work/spare-marker-b.yaml; then
    log "  upsert FAILED"; result "upsert-discards-status" "FAIL"
elif ! wait_field "$SPARE_TOKEN" 'spec.bound_keypair.recovery.limit' "$MARKER_B_RECOVERY_LIMIT"; then
    # Without this barrier the next read can return the pre-upsert token and marker A
    # "survives" on a build that in fact overwrote it. This is the exact false green.
    result "upsert-discards-status" "FAIL"
else
    expect_eq "upsert-discards-status" "status.bound_keypair.registration_secret (still A)" \
        "$MARKER_A" "$(field "$SPARE_TOKEN" 'status.bound_keypair.registration_secret')"
fi

log "case 3c: deleting ${SPARE_TOKEN}, then recreating with marker B (must be accepted)"
if ! $TCTL rm "token/${SPARE_TOKEN}"; then
    log "  delete FAILED"; result "recreate-sets-status" "FAIL"
elif ! wait_absent "$SPARE_TOKEN"; then
    result "recreate-sets-status" "FAIL"
elif ! $TCTL create -f /work/spare-marker-b.yaml; then
    log "  recreate FAILED"; result "recreate-sets-status" "FAIL"
elif ! wait_present "$SPARE_TOKEN"; then
    result "recreate-sets-status" "FAIL"
else
    expect_eq "recreate-sets-status" "status.bound_keypair.registration_secret" \
        "$MARKER_B" "$(field "$SPARE_TOKEN" 'status.bound_keypair.registration_secret')"
fi

###############################################################################
# Done. Stay alive so the container is inspectable; the checks read these logs, which
# survive exit either way, but a healthy long-lived service keeps `compose ps` honest.
###############################################################################
log "final ${TOKEN_NAME} status.bound_keypair:"
$TCTL get "token/${TOKEN_NAME}" --format json 2>/dev/null | jq '.[0].status.bound_keypair'
result "mutator" "DONE"
touch /tmp/mutate-done
exec sleep infinity
