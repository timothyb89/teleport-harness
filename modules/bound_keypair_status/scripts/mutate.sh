#!/bin/sh
# bound_keypair_status actor.
#
# Drives the scenario and RECORDS what it observed. It does not judge: no PASS/FAIL is
# emitted here. Each case appends a record to /out/observations.json —
#
#   {case, at, token, applied, before: {field: value}, after: {field: value}}
#
# — and the module's `observation_unchanged` / `observation_equals` checks do the asserting.
#
# Why the split: a script that mutates cluster state is often the only thing that can see a
# before/after across its own mutation, so it has to run. But if it also decides the verdict,
# the module can only grep its log for `RESULT foo: PASS`, and the report ends up citing a
# magic string whose meaning lives in a file the reader may never open. Recording instead
# means the proof IS the observed values, the detail line is generated from them, and editing
# the comparison logic can't silently invalidate a hand-written explanation.
#
# READ-AFTER-WRITE IS NOT COHERENT HERE. `tctl get` resolves through the auth server's CACHE
# (Server embeds both *Services and authclient.Cache, and Cache wins for GetToken), so a read
# taken straight after an apply can still return the PRE-write token — which would make
# "unchanged" indistinguishable from "I read a stale copy". Every apply therefore carries a
# distinct spec sentinel (recovery.limit) and the `after` snapshot is taken only once that
# sentinel is visible. The sentinel is recorded too, so `observation_equals` can assert the
# write actually landed rather than taking the barrier on trust.
#
# Deliberately no `set -e`: a case that goes wrong must still be RECORDED (a timed-out
# barrier records what it last saw), because a missing record is indistinguishable from a
# crashed actor, while a recorded bad value is a finding.

set -u

TCTL="tctl --identity ${IDENTITY} --auth-server ${AUTH_ADDR}"
OBS=/out/observations.json
BARRIER_TRIES=40        # × 1s; cache lag is sub-second, this is pure headroom

# The server-owned fields under test, plus the spec sentinel that proves an apply landed.
FIELDS="metadata.revision
status.bound_keypair.bound_public_key
status.bound_keypair.bound_bot_instance_id
status.bound_keypair.recovery_count
status.bound_keypair.registration_secret
spec.bound_keypair.recovery.limit"

log() { echo "[mutate] $*"; }

# A FAILED read and an EMPTY value are different facts, and conflating them is dangerous:
# when auth is briefly unavailable `tctl get` errors, and returning "" for that makes a token
# look absent (so wait_absent returns immediately) or a field look cleared (so a comparison
# "passes"). But `tctl get` ALSO exits non-zero for a token that simply does not exist, which
# is a normal, frequent observation here — so the two cases must be told apart without
# retrying the common one (blind retries made an absent-token read cost 10s, and a snapshot
# 60s, which starved the later cases entirely).
#
# `tctl get tokens` is the cheap discriminator: if it succeeds, auth is up and the token is
# genuinely absent; only if it also fails do we retry.
UNREADABLE="<unreadable>"

read_token() { # read_token <name> -> token JSON | "" if absent | $UNREADABLE if auth is down
    if _j=$($TCTL get "token/$1" --format json 2>/dev/null); then printf '%s' "$_j"; return 0; fi
    $TCTL get tokens >/dev/null 2>&1 && { printf ''; return 0; }
    _try=0
    while [ "$_try" -lt 5 ]; do
        sleep 2; _try=$((_try + 1))
        if _j=$($TCTL get "token/$1" --format json 2>/dev/null); then printf '%s' "$_j"; return 0; fi
        $TCTL get tokens >/dev/null 2>&1 && { printf ''; return 0; }
    done
    log "  WARNING: auth unreachable while reading $1"
    printf '%s' "$UNREADABLE"
}

_extract() { # _extract <token-json> <jq-path>
    case "$1" in
        "$UNREADABLE") printf '%s' "$UNREADABLE" ;;
        "")            printf '' ;;
        *)             printf '%s' "$1" | jq -r ".[0].$2 // empty" 2>/dev/null ;;
    esac
}

field() { # field <token-name> <jq-path-after-.[0]>
    _extract "$(read_token "$1")" "$2"
}

# Snapshot every field under test as a JSON object.
# ONE read for the whole snapshot: 6x fewer round trips, and — more importantly — every
# field comes from the same read, so a snapshot can't be torn across a concurrent write.
snapshot() { # snapshot <token-name>
    _json=$(read_token "$1")
    _out="{}"
    for f in $FIELDS; do
        _v=$(_extract "$_json" "$f")
        _out=$(printf '%s' "$_out" | jq --arg k "$f" --arg v "$_v" '. + {($k): $v}')
    done
    printf '%s' "$_out"
}

# Existence is NOT a sufficient barrier for a delete+recreate: `tctl get` reads through the
# auth cache, so a token that existed before the delete can still read as present afterwards
# — the pre-delete copy. Key off the REVISION instead, which differs for every write, so the
# SAME file can be applied twice and the barrier still distinguishes the two applies. (Found
# by composing with a module that restarts auth: 9 restarts churned the cache and a stale
# read made the recreate look like it kept the old status.)
wait_revision() { # wait_revision <token> <previous-revision>
    _i=0
    while [ "$_i" -lt "$BARRIER_TRIES" ]; do
        _r=$(field "$1" 'metadata.revision')
        [ -n "$_r" ] && [ "$_r" != "$2" ] && return 0
        _i=$((_i + 1)); sleep 1
    done
    log "  TIMEOUT waiting for $1 to get a revision different from '$2' (last: '$_r')"
    return 1
}

rev_of() { field "$1" 'metadata.revision'; }
wait_absent()  { _i=0; while [ "$_i" -lt "$BARRIER_TRIES" ]; do
    [ -z "$(field "$1" 'metadata.name')" ] && return 0; _i=$((_i + 1)); sleep 1; done; return 1; }

mkdir -p /out
echo '[]' > "$OBS"
record() { # record <case> <token> <applied> <before-json> <after-json>
    jq --arg case "$1" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg token "$2" \
       --arg applied "$3" --argjson before "$4" --argjson after "$5" \
       --arg actor scripts/mutate.sh \
       '. + [{case: $case, at: $at, token: $token, applied: $applied, actor: $actor,
              before: $before, after: $after}]' "$OBS" > "$OBS.tmp" && mv "$OBS.tmp" "$OBS"
    log "recorded '$1'"
}

###############################################################################
# 0. Precondition — the bot must actually bind a keypair first, or there is no
#    server-owned status to preserve and every comparison below is vacuous.
###############################################################################
log "waiting for ${TOKEN_NAME} to have a bound public key (bot join in progress)"
i=0; KEY0=""
while [ "$i" -lt 120 ]; do
    KEY0=$(field "$TOKEN_NAME" 'status.bound_keypair.bound_public_key')
    [ -n "$KEY0" ] && break
    i=$((i + 1)); sleep 2
done
if [ -z "$KEY0" ]; then
    log "FATAL: ${TOKEN_NAME} never got a bound_public_key; the bot did not join."
    touch /tmp/mutate-done
    exec sleep infinity
fi
log "bot bound key: ${KEY0}"

###############################################################################
# 1. Spec-only re-apply — the `tctl get | apply`, terraform and operator roundtrip.
###############################################################################
log "case spec-only-reapply: applying ${TOKEN_NAME} with NO status (recovery.limit -> ${NEW_RECOVERY_LIMIT})"
REV=$(rev_of "$TOKEN_NAME"); BEFORE=$(snapshot "$TOKEN_NAME")
$TCTL create -f /work/token-spec-only.yaml
wait_revision "$TOKEN_NAME" "$REV"
record spec-only-reapply "$TOKEN_NAME" config/token-spec-only.yaml "$BEFORE" "$(snapshot "$TOKEN_NAME")"

###############################################################################
# 2. Tampered-status re-apply — an explicit attempt to write server-owned state.
###############################################################################
log "case tampered-reapply: applying ${TOKEN_NAME} WITH a forged status (recovery.limit -> ${TAMPERED_RECOVERY_LIMIT})"
REV=$(rev_of "$TOKEN_NAME"); BEFORE=$(snapshot "$TOKEN_NAME")
$TCTL create -f /work/token-tampered.yaml
wait_revision "$TOKEN_NAME" "$REV"
record tampered-reapply "$TOKEN_NAME" config/token-tampered.yaml "$BEFORE" "$(snapshot "$TOKEN_NAME")"

###############################################################################
# 3. The escape hatch, on a spare token nothing joins with. The SAME marker-B file is
#    applied as an update (must be discarded) and after a delete (must be accepted).
###############################################################################
log "case create-with-status: creating ${SPARE_TOKEN} with status marker A"
$TCTL rm "token/${SPARE_TOKEN}" >/dev/null 2>&1; wait_absent "$SPARE_TOKEN"
REV=$(rev_of "$SPARE_TOKEN"); BEFORE=$(snapshot "$SPARE_TOKEN")
$TCTL create -f /work/spare-marker-a.yaml
wait_revision "$SPARE_TOKEN" "$REV"
record create-with-status "$SPARE_TOKEN" config/spare-marker-a.yaml "$BEFORE" "$(snapshot "$SPARE_TOKEN")"

log "case update-with-status: applying marker B OVER the existing ${SPARE_TOKEN}"
REV=$(rev_of "$SPARE_TOKEN"); BEFORE=$(snapshot "$SPARE_TOKEN")
$TCTL create -f /work/spare-marker-b.yaml
wait_revision "$SPARE_TOKEN" "$REV"
record update-with-status "$SPARE_TOKEN" config/spare-marker-b.yaml "$BEFORE" "$(snapshot "$SPARE_TOKEN")"

log "case recreate-with-status: deleting ${SPARE_TOKEN}, then applying marker B again"
REV=$(rev_of "$SPARE_TOKEN"); BEFORE=$(snapshot "$SPARE_TOKEN")
$TCTL rm "token/${SPARE_TOKEN}"; wait_absent "$SPARE_TOKEN"
$TCTL create -f /work/spare-marker-b.yaml
wait_revision "$SPARE_TOKEN" "$REV"
record recreate-with-status "$SPARE_TOKEN" config/spare-marker-b.yaml "$BEFORE" "$(snapshot "$SPARE_TOKEN")"

###############################################################################
# Done. Stay alive so the container stays inspectable; the checks read $OBS from it.
###############################################################################
log "observations:"
jq . "$OBS"
touch /tmp/mutate-done
exec sleep infinity
