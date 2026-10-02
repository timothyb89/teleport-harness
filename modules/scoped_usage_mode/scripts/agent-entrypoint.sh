#!/bin/sh
# Entrypoint for every scoped_usage_mode Node agent.
#
# Two jobs, both optional per container:
#
#   WAIT_FOR=<service>   Hold off joining until the FIRST agent of this pair reports ready on
#                        its diag endpoint. This is what makes "first" and "second" mean
#                        something: a single_use token is consumed by whichever host redeems
#                        it first, so without the ordering the two agents race and the checks
#                        could not say which one should have been denied.
#
#                        Deliberately NOT compose `depends_on: service_healthy`: if the first
#                        agent never became healthy (a real regression), compose would fail
#                        the whole `up` instead of letting the checks report it. So we wait a
#                        bounded time and then join anyway, logging that we did — a second
#                        agent that joins without its first having joined shows up in the
#                        checks as exactly that.
#
#   SERVICE_ACCOUNT=<sa> Mint a Kubernetes SA JWT from the oidc-server and point the join
#                        client at it via KUBERNETES_TOKEN_PATH (the documented override of the
#                        projected-token path). Minted per agent, so the pair never shares a JWT.
set -eu

log() { echo "[su-agent] $*"; }

if [ -n "${WAIT_FOR:-}" ]; then
    log "waiting for first agent ${WAIT_FOR} to be ready before joining"
    i=0
    until curl -fs "http://${WAIT_FOR}:3000/readyz" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 90 ]; then
            log "first agent ${WAIT_FOR} never became ready after 180s; joining anyway"
            break
        fi
        sleep 2
    done
    [ "$i" -lt 90 ] && log "first agent ${WAIT_FOR} is ready; joining now"
fi

if [ -n "${SERVICE_ACCOUNT:-}" ]; then
    : "${OIDC_URL:?}"
    TOKEN_FILE=/sa/token
    mkdir -p /sa
    log "minting SA token for default:${SERVICE_ACCOUNT} from ${OIDC_URL}"
    i=0
    until curl -fsSk "${OIDC_URL}/k8s/token?namespace=default&serviceaccount=${SERVICE_ACCOUNT}&pod=$(hostname)" -o "$TOKEN_FILE" \
            && [ -s "$TOKEN_FILE" ]; do
        i=$((i + 1))
        [ "$i" -ge 60 ] && { log "failed to mint SA token" >&2; exit 1; }
        sleep 2
    done
    export KUBERNETES_TOKEN_PATH="$TOKEN_FILE"
fi

log "joining as $(hostname)"
exec teleport start --config /etc/teleport/teleport.yaml
