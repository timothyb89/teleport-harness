#!/usr/bin/env bash
# Mint one Kubernetes service-account JWT per native-joining runner from the shared
# oidc-server, into the volume the runners mount at /sa. Same trick as
# modules/kubernetes' bot-entrypoint (an out-of-cluster simulation of a projected SA
# token) — hoisted into its own service here because the runners are Terraform
# containers, not the teleport image, and only this image is guaranteed to have curl.
#
# Writes each token to /sa/<service-account>.token and then idles; the container's
# healthcheck gates the runners on every file existing, so no runner can start before
# it can join.
set -euo pipefail
: "${OIDC_URL:?}" "${SERVICE_ACCOUNTS:?}"
NAMESPACE="${NAMESPACE:-default}"
mkdir -p /sa

for sa in $SERVICE_ACCOUNTS; do
  echo "[sa-minter] minting SA token for ${NAMESPACE}:${sa} from ${OIDC_URL}"
  for _ in $(seq 1 60); do
    if curl -fsSk "${OIDC_URL}/k8s/token?namespace=${NAMESPACE}&serviceaccount=${sa}&pod=${sa}-pod" \
         -o "/sa/${sa}.token" && [ -s "/sa/${sa}.token" ]; then
      break
    fi
    sleep 2
  done
  [ -s "/sa/${sa}.token" ] || { echo "[sa-minter] failed to mint SA token for ${sa}" >&2; exit 1; }
done

echo "[sa-minter] all SA tokens minted: $SERVICE_ACCOUNTS"
touch /sa/.ready
# Stay up so the tokens' provenance is inspectable (docker exec) after the run.
tail -f /dev/null
