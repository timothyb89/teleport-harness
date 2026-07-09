#!/usr/bin/env bash
#
# Render the generic_oidc AGENT tokens from the running OIDC server and create
# them in the cluster using the token-manager bot's identity file (NOT cluster
# admin) — exercising the "manage tokens via a tbot identity" path.
set -euo pipefail


IDENT=/idents/identity
AUTH=auth:3025

echo "[token-manager] waiting for bot identity at $IDENT ..."
for _ in $(seq 1 150); do
  [[ -s "$IDENT" ]] && break
  sleep 2
done
[[ -s "$IDENT" ]] || { echo "[token-manager] identity never appeared" >&2; exit 1; }

echo "[token-manager] waiting for OIDC server at ${FETCH_URL:-https://oidc:8443} ..."
for _ in $(seq 1 60); do
  curl -fsSk "${FETCH_URL:-https://oidc:8443}/healthz" >/dev/null 2>&1 && break
  sleep 2
done

echo "[token-manager] rendering agent tokens from ${ISSUER:-https://oidc:8443} ..."
ISSUER="${ISSUER:-https://oidc:8443}" \
FETCH_URL="${FETCH_URL:-https://oidc:8443}" \
AUDIENCE="${AUDIENCE:-teleport.test/agents}" \
AGENT_SUB="${AGENT_SUB:-test-agent}" \
OUT_DIR=/out \
  /render-resources.sh

echo "[token-manager] creating unscoped agent tokens via the bot identity..."
for f in token-agent-discovery token-agent-static-jwks; do
  tctl --identity "$IDENT" --auth-server "$AUTH" create -f "/out/$f.yaml"
  echo "[token-manager] created $f"
done

# Scoped agent tokens (only when the scopes feature is enabled). The bot can
# create these thanks to its scoped token-admin assignment in /genericoidc-test.
if [[ "${TELEPORT_UNSTABLE_SCOPES:-}" == "yes" ]]; then
  echo "[token-manager] creating scoped agent tokens via the bot identity..."
  for f in scoped-token-agent-discovery scoped-token-agent-static-jwks; do
    tctl --identity "$IDENT" --auth-server "$AUTH" create -f "/out/$f.yaml"
    echo "[token-manager] created $f"
  done
fi

echo "[token-manager] tokens now in the cluster:"
tctl --identity "$IDENT" --auth-server "$AUTH" tokens ls || true
echo "[token-manager] done"
