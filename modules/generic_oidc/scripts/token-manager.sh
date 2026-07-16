#!/usr/bin/env bash
#
# Render the generic_oidc AGENT and BOT tokens from the running OIDC servers and
# create them in the cluster using the token-manager bot's identity file (NOT
# cluster admin) — exercising the "manage tokens via a tbot identity" path.
#
# Two token sets are rendered:
#   main (/out/main)  from the system-trusted "oidc" issuer: agent tokens (discovery
#                     over system trust + static_jwks) and the static_jwks BOT tokens.
#   ca   (/out/ca)    from the self-signed "oidc-ca" issuer: the DISCOVERY BOT tokens,
#                     which embed oidc-ca's CA as tls_ca (custom-CA discovery path).
set -euo pipefail

IDENT=/idents/identity
AUTH=auth:3025

# --- token-manager bot identity ---------------------------------------------
echo "[token-manager] waiting for bot identity at $IDENT ..."
for _ in $(seq 1 150); do
  [[ -s "$IDENT" ]] && break
  sleep 2
done
[[ -s "$IDENT" ]] || { echo "[token-manager] identity never appeared" >&2; exit 1; }

# --- wait for both OIDC servers ---------------------------------------------
for url in "${FETCH_URL:-https://oidc:8443}" "${CA_FETCH_URL:-https://oidc-ca:8443}"; do
  echo "[token-manager] waiting for OIDC server at ${url} ..."
  for _ in $(seq 1 60); do
    curl -fsSk "${url}/healthz" >/dev/null 2>&1 && break
    sleep 2
  done
done

create() { tctl --identity "$IDENT" --auth-server "$AUTH" create -f "$1" && echo "[token-manager] created $1"; }

# --- MAIN set (system-trust issuer): agents + static_jwks bots --------------
echo "[token-manager] rendering MAIN token set from ${ISSUER:-https://oidc:8443} ..."
ISSUER="${ISSUER:-https://oidc:8443}" \
FETCH_URL="${FETCH_URL:-https://oidc:8443}" \
AUDIENCE="${AUDIENCE:-teleport.test/agents}" \
AGENT_SUB="${AGENT_SUB:-test-agent}" \
OMIT_TLS_CA=1 \
BOT_NAME=gobot-static \
SCOPED_BOT_NAME=gobot-scoped-static \
OUT_DIR=/out/main \
  /render-resources.sh

echo "[token-manager] creating unscoped agent + static_jwks bot tokens..."
create /out/main/token-agent-discovery.yaml     # agent, discovery (system trust)
create /out/main/token-agent-static-jwks.yaml   # agent, static_jwks
create /out/main/token-agent-expr.yaml          # agent, static_jwks, EXPRESSION rule: contains(set(claims.groups), "dev")
create /out/main/token-static-jwks.yaml         # BOT, static_jwks (gobot-static-static)

# --- CA set (self-signed issuer): custom-CA discovery bots ------------------
echo "[token-manager] rendering CA token set from ${CA_ISSUER:-https://oidc-ca:8443} ..."
ISSUER="${CA_ISSUER:-https://oidc-ca:8443}" \
FETCH_URL="${CA_FETCH_URL:-https://oidc-ca:8443}" \
AUDIENCE="${AUDIENCE:-teleport.test/agents}" \
AGENT_SUB="${AGENT_SUB:-test-agent}" \
OMIT_TLS_CA=0 \
BOT_NAME=gobot-disc \
SCOPED_BOT_NAME=gobot-scoped-disc \
OUT_DIR=/out/ca \
  /render-resources.sh

echo "[token-manager] creating custom-CA discovery bot token..."
create /out/ca/token-discovery.yaml             # BOT, discovery + tls_ca (gobot-disc-discovery)

# --- Scoped tokens (only when the scopes feature is enabled) ----------------
# The bot can create these thanks to its scoped token-admin assignment; the scoped
# bots themselves are created as bootstrap scoped_bot resources.
if [[ "${TELEPORT_UNSTABLE_SCOPES:-}" == "yes" ]]; then
  echo "[token-manager] creating scoped agent + bot tokens..."
  create /out/main/scoped-token-agent-discovery.yaml    # scoped agent, discovery
  create /out/main/scoped-token-agent-static-jwks.yaml  # scoped agent, static_jwks
  create /out/main/scoped-token-static-jwks.yaml         # scoped BOT, static_jwks (gobot-scoped-static-token)
  create /out/ca/scoped-token-discovery.yaml             # scoped BOT, discovery + tls_ca (gobot-scoped-disc-token)
fi

echo "[token-manager] tokens now in the cluster:"
tctl --identity "$IDENT" --auth-server "$AUTH" tokens ls || true
echo "[token-manager] done"
