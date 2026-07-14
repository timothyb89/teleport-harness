#!/usr/bin/env bash
# Repeated-join probe for the OIDC caching validator. Joins via the kube `oidc` type
# JOINS times against the dedicated cache-idp, each time from a COLD tbot store so it is a
# fresh JOIN (not a renewal) → each join re-validates the SA JWT via the auth server's
# oidc.CachingTokenValidator. With caching working, cache-idp serves discovery + JWKS only
# once across all JOINS. Each attempt mints its own SA token from cache-idp (/k8s/token),
# so the IdP log's /k8s/token tally is a lower bound on join attempts.
set -euo pipefail
: "${OIDC_URL:?}" "${SERVICE_ACCOUNT:?}"
NAMESPACE="${NAMESPACE:-default}"
JOINS="${JOINS:-5}"
TOKEN_FILE=/sa/token
export KUBERNETES_TOKEN_PATH="$TOKEN_FILE"   # tbot's documented override of the SA-token path
mkdir -p /sa

mint() {
  for _ in $(seq 1 60); do
    if curl -fsSk "${OIDC_URL}/k8s/token?namespace=${NAMESPACE}&serviceaccount=${SERVICE_ACCOUNT}&pod=${SERVICE_ACCOUNT}-pod" -o "$TOKEN_FILE" && [ -s "$TOKEN_FILE" ]; then
      return 0
    fi
    sleep 2
  done
  echo "[cache-probe] failed to mint SA token from ${OIDC_URL}" >&2
  return 1
}

for i in $(seq 1 "$JOINS"); do
  echo "[cache-probe] fresh join attempt ${i}/${JOINS}"
  rm -rf /var/lib/tbot/* 2>/dev/null || true   # cold store => fresh JOIN, not a renewal
  mint
  tbot start --oneshot -c /etc/tbot.yaml || { echo "[cache-probe] join ${i} failed" >&2; exit 1; }
  sleep 1
done

echo "[cache-probe] completed ${JOINS} fresh joins"
exec sleep infinity   # keep alive so the produced identity can be inspected
