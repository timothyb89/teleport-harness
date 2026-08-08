#!/bin/sh
# bound_keypair_status mutator.
#
# Proves the rule: for an EXISTING bound_keypair token, `.status` is always preserved and any
# `.status` on an incoming copy is always discarded; delete + recreate is the only way to set
# it. Everything here runs the way a real admin tool does — `tctl create -f` over a bot
# identity — so it exercises the same UpsertToken RPC that tctl, the Terraform provider and
# the Kubernetes operator all reach.
#
# Emits one `RESULT <case>: PASS|FAIL` line per case; module.yaml gates on those with
# log_count (NOT log_contains, which SKIPs on no match and would hide a regression).
#
# Deliberately does NOT `set -e`: a failing case is the finding, and aborting would leave the
# later cases unreported — which reads identically to "the mutator crashed". Cases are
# ordered so each one's precondition is established by the previous.

set -u

TCTL="tctl --identity ${IDENTITY} --auth-server ${AUTH_ADDR}"

log()    { echo "[mutate] $*"; }
result() { echo "RESULT $1: $2"; }

# `tctl get token/<name> --format json` emits a JSON ARRAY even for one resource, hence .[0].
# Tokens are always fetched with secrets (tctl forces it: "tokens cannot be retrieved without
# secrets"), and a BOT identity is exempt from admin-action MFA, so this needs no login.
field() { # field <token-name> <jq-path-after-.[0]>
    $TCTL get "token/$1" --format json 2>/dev/null | jq -r ".[0].$2 // empty" 2>/dev/null
}

# Compare and report in one step so every case logs its actual values, not just a verdict.
expect_eq() { # expect_eq <case> <what> <expected> <actual>
    if [ "$3" = "$4" ]; then
        result "$1" "PASS"
        log "  ok   $2: $4"
    else
        result "$1" "FAIL"
        log "  BAD  $2: expected '$3', got '$4'"
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
    i=$((i + 1))
    sleep 2
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
log "case 1: re-applying ${TOKEN_NAME} with NO status (and recovery.limit -> ${NEW_RECOVERY_LIMIT})"
if ! $TCTL create -f /work/token-spec-only.yaml; then
    log "  apply FAILED (an update should be accepted; only the status must be ignored)"
    result "spec-only-reapply-preserves-status" "FAIL"
    result "spec-edit-applied" "FAIL"
else
    KEY1=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_public_key')
    INST1=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_bot_instance_id')
    CNT1=$(field "$TOKEN_NAME" 'status.bound_keypair.recovery_count')

    if [ "$KEY1" = "$KEY0" ] && [ "$INST1" = "$INST0" ] && [ "$CNT1" = "$CNT0" ]; then
        result "spec-only-reapply-preserves-status" "PASS"
        log "  ok   bound key, bot instance and recovery counter all unchanged"
    else
        result "spec-only-reapply-preserves-status" "FAIL"
        log "  BAD  bound_public_key      '${KEY0}' -> '${KEY1}'"
        log "  BAD  bound_bot_instance_id '${INST0}' -> '${INST1}'"
        log "  BAD  recovery_count        '${CNT0}' -> '${CNT1}'"
    fi

    # ...and the legitimate spec edit carried by the SAME apply must still have landed.
    # Guards the over-correction where "preserve status" degrades into "ignore the update".
    LIMIT1=$(field "$TOKEN_NAME" 'spec.bound_keypair.recovery.limit')
    expect_eq "spec-edit-applied" "spec.bound_keypair.recovery.limit" "$NEW_RECOVERY_LIMIT" "$LIMIT1"
fi

###############################################################################
# 2. Tampered-status re-apply — explicit attempt to write server-owned state.
#    Every status value in this copy is wrong (a key the bot does not hold, a nil-UUID
#    instance, a reset counter, an unissued secret). None may reach storage.
###############################################################################
log "case 2: re-applying ${TOKEN_NAME} WITH a tampered status"
if ! $TCTL create -f /work/token-tampered.yaml; then
    # A hard rejection is a defensible design too, but it is not the rule this module
    # encodes (silent discard, so terraform/operator do not reconcile-loop forever).
    log "  apply was REJECTED outright; the rule under test is silent discard"
    result "tampered-status-discarded" "FAIL"
else
    KEY2=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_public_key')
    INST2=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_bot_instance_id')
    CNT2=$(field "$TOKEN_NAME" 'status.bound_keypair.recovery_count')
    SEC2=$(field "$TOKEN_NAME" 'status.bound_keypair.registration_secret')

    if [ "$KEY2" = "$KEY0" ] && [ "$INST2" = "$INST0" ] && [ "$CNT2" = "$CNT0" ] && [ "$SEC2" = "$SEC0" ]; then
        result "tampered-status-discarded" "PASS"
        log "  ok   every tampered field was discarded; server-side status intact"
    else
        result "tampered-status-discarded" "FAIL"
        log "  BAD  bound_public_key      '${KEY0}' -> '${KEY2}'"
        log "  BAD  bound_bot_instance_id '${INST0}' -> '${INST2}'"
        log "  BAD  recovery_count        '${CNT0}' -> '${CNT2}'"
        log "  BAD  registration_secret   '${SEC0}' -> '${SEC2}'"
    fi
fi

###############################################################################
# 3. The escape hatch, on a spare token nothing joins with.
#    create accepts status -> upsert discards it -> delete+recreate sets it. Same file
#    applied in steps b and c; the ONLY difference is whether the token already existed.
###############################################################################
log "case 3a: creating ${SPARE_TOKEN} with status marker A"
$TCTL rm "token/${SPARE_TOKEN}" >/dev/null 2>&1   # idempotent: tolerate a re-run
if ! $TCTL create -f /work/spare-marker-a.yaml; then
    log "  create FAILED"
    result "create-accepts-status" "FAIL"
else
    expect_eq "create-accepts-status" "status.bound_keypair.registration_secret" \
        "$MARKER_A" "$(field "$SPARE_TOKEN" 'status.bound_keypair.registration_secret')"
fi

log "case 3b: upserting ${SPARE_TOKEN} with status marker B (must be discarded)"
if ! $TCTL create -f /work/spare-marker-b.yaml; then
    log "  upsert FAILED"
    result "upsert-discards-status" "FAIL"
else
    expect_eq "upsert-discards-status" "status.bound_keypair.registration_secret (still A)" \
        "$MARKER_A" "$(field "$SPARE_TOKEN" 'status.bound_keypair.registration_secret')"
fi

log "case 3c: deleting ${SPARE_TOKEN}, then recreating with marker B (must be accepted)"
if ! $TCTL rm "token/${SPARE_TOKEN}"; then
    log "  delete FAILED"
    result "recreate-sets-status" "FAIL"
elif ! $TCTL create -f /work/spare-marker-b.yaml; then
    log "  recreate FAILED"
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
