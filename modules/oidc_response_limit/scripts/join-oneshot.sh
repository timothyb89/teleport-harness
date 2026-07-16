#!/usr/bin/env bash
# Entrypoint for a kube `oidc`-join bot in the oidc_response_limit module.
#
# Mints a k8s service-account JWT from its IdP's /k8s/token (that endpoint stays
# well-behaved even on the hostile IdPs — only discovery/JWKS are oversized), points
# tbot at it via KUBERNETES_TOKEN_PATH, and performs a SINGLE join attempt.
#
# A negative bot's join is EXPECTED to fail: the auth server rejects the oversized
# discovery/JWKS while validating the token. So a failed `tbot start` is NOT a fatal
# script error here — we record the exit code and then sleep so the container stays up
# for the file/identity checks (docker exec needs it running) and its logs persist.
# The positive bot's join succeeds and writes /out/id/identity before the same sleep.
set -uo pipefail
: "${OIDC_URL:?}" "${SERVICE_ACCOUNT:?}"
NAMESPACE="${NAMESPACE:-default}"
TOKEN_FILE=/sa/token
export KUBERNETES_TOKEN_PATH="$TOKEN_FILE"   # tbot's documented override of the SA-token path
mkdir -p /sa

echo "[limit-bot] minting SA token for ${NAMESPACE}:${SERVICE_ACCOUNT} from ${OIDC_URL}"
for _ in $(seq 1 60); do
  if curl -fsSk "${OIDC_URL}/k8s/token?namespace=${NAMESPACE}&serviceaccount=${SERVICE_ACCOUNT}&pod=${SERVICE_ACCOUNT}-pod" -o "$TOKEN_FILE" && [ -s "$TOKEN_FILE" ]; then
    break
  fi
  sleep 2
done
[ -s "$TOKEN_FILE" ] || { echo "[limit-bot] failed to mint SA token" >&2; exit 1; }

echo "[limit-bot] attempting kube oidc join (single shot) against ${OIDC_URL}"
tbot start --oneshot -c /etc/tbot.yaml
rc=$?
echo "[limit-bot] tbot exited with code ${rc}"

echo "[limit-bot] staying alive for inspection"
exec sleep infinity
