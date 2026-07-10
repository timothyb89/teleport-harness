#!/usr/bin/env bash
# tbot entrypoint for a kubernetes-join bot (out-of-cluster simulation): mint a k8s
# service-account JWT from the shared oidc-server, write it to a file, and point the
# kubernetes join method at it via KUBERNETES_TOKEN_PATH (teleport's documented override
# of the default /var/run/secrets/... projected-token path).
set -euo pipefail
: "${OIDC_URL:?}" "${SERVICE_ACCOUNT:?}"
NAMESPACE="${NAMESPACE:-default}"
TOKEN_FILE=/sa/token
mkdir -p /sa

echo "[kube-bot] minting SA token for ${NAMESPACE}:${SERVICE_ACCOUNT} from ${OIDC_URL}"
for _ in $(seq 1 60); do
  if curl -fsSk "${OIDC_URL}/k8s/token?namespace=${NAMESPACE}&serviceaccount=${SERVICE_ACCOUNT}&pod=${SERVICE_ACCOUNT}-pod" -o "$TOKEN_FILE" && [ -s "$TOKEN_FILE" ]; then
    break
  fi
  sleep 2
done
[ -s "$TOKEN_FILE" ] || { echo "[kube-bot] failed to mint SA token" >&2; exit 1; }

export KUBERNETES_TOKEN_PATH="$TOKEN_FILE"
echo "[kube-bot] joining via kubernetes (KUBERNETES_TOKEN_PATH=$TOKEN_FILE)"
exec tbot start -c /etc/tbot.yaml
