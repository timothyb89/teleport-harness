#!/usr/bin/env bash
# Render a generic_oidc cluster into $OUT: builds the OIDC server image, renders
# teleport configs from templates, and emits a self-contained docker-compose.yml.
#
# Invoked by lib/cluster.sh with: CLUSTER_ID FQDN PORT IMAGE HARNESS_DOMAIN LAB_DOMAIN OUT
set -euo pipefail

MODULE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${OUT:?}" "${CLUSTER_ID:?}" "${FQDN:?}" "${PORT:?}" "${IMAGE:?}"
export CLUSTER_ID FQDN PORT

AUDIENCE="${FQDN}/generic-oidc"     # token audience == OIDC server -audience
BOT_TOKEN="harness-tokmgr-secret"   # token-manager bot join secret (per-cluster, isolated)
AGENT_SUB="test-agent"              # subject the positive agents request / rules match
OIDC_IMAGE="teleport-harness-oidc:latest"

# 1) OIDC server image (built once, reused).
if ! docker image inspect "$OIDC_IMAGE" >/dev/null 2>&1; then
  echo "[render] building $OIDC_IMAGE" >&2
  DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t "$OIDC_IMAGE" "$MODULE/oidc-server" >/dev/null
fi

# 2) Render teleport configs (only these three vars are substituted).
mkdir -p "$OUT/config"
for t in "$MODULE"/config/*.tmpl; do
  envsubst '${CLUSTER_ID} ${FQDN} ${PORT}' < "$t" > "$OUT/config/$(basename "$t" .tmpl)"
done

# 3) Emit compose. Static services first, then agents in a loop, then nets/volumes.
cf="$OUT/docker-compose.yml"
cat > "$cf" <<EOF
name: teleport-harness-${CLUSTER_ID}

services:
  auth:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-auth
    entrypoint: ["/scripts/auth-entrypoint.sh"]
    environment:
      BOT_TOKEN: ${BOT_TOKEN}
      TELEPORT_UNSTABLE_SCOPES: "yes"
    volumes:
      - harness-certs:/certs:ro
      - ${OUT}/config/auth.yaml:/etc/teleport/auth.yaml:ro
      - ${MODULE}/bootstrap:/bootstrap:ro
      - ${MODULE}/scripts:/scripts:ro
      - auth-data:/var/lib/teleport
    networks:
      internal:
        aliases: ["${FQDN}"]
      teleport-harness:
        aliases: ["${FQDN}"]
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:3000/healthz"]
      interval: 5s
      timeout: 3s
      retries: 40
      start_period: 10s

  oidc:
    image: ${OIDC_IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-oidc
    command:
      - -issuer=https://oidc:8443
      - -addr=:8443
      - -audience=${AUDIENCE}
      - -cluster-name=${FQDN}
      - -extra-sans=oidc,localhost,127.0.0.1
    volumes:
      - oidc-data:/data
    networks: [internal]

  tbot:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-tbot
    command: [tbot, start, -c, /etc/teleport/tbot.yaml, "--token=${BOT_TOKEN}", "--join-method=token"]
    volumes:
      - ${OUT}/config/tbot.yaml:/etc/teleport/tbot.yaml:ro
      - bot-data:/var/lib/tbot
      - idents:/idents
    networks: [internal]
    depends_on:
      auth: {condition: service_healthy}

  token-manager:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-token-manager
    command: ["/scripts/token-manager.sh"]
    restart: "no"
    environment:
      ISSUER: https://oidc:8443
      FETCH_URL: https://oidc:8443
      AUDIENCE: ${AUDIENCE}
      AGENT_SUB: ${AGENT_SUB}
      TELEPORT_UNSTABLE_SCOPES: "yes"
    volumes:
      - idents:/idents:ro
      - ${MODULE}/scripts:/scripts:ro
      - ${MODULE}/render-resources.sh:/render-resources.sh:ro
    networks: [internal]
    depends_on:
      auth: {condition: service_healthy}
      oidc: {condition: service_started}
      tbot: {condition: service_started}
EOF

# Agents: positive (discovery/static/scoped-*) + negative (deny/scoped-deny).
for name in discovery static scoped-discovery scoped-static deny scoped-deny; do
cat >> "$cf" <<EOF

  agent-${name}:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-agent-${name}
    command: ["teleport", "start", "--config", "/etc/teleport/teleport.yaml"]
    environment:
      TELEPORT_UNSTABLE_SCOPES: "yes"
    volumes:
      - ${OUT}/config/agent-${name}.yaml:/etc/teleport/teleport.yaml:ro
    networks: [internal]
    depends_on:
      token-manager: {condition: service_completed_successfully}
      oidc: {condition: service_started}
EOF
done

cat >> "$cf" <<EOF

networks:
  internal:
  teleport-harness:
    external: true
    name: teleport-harness

volumes:
  auth-data:
  bot-data:
  idents:
  oidc-data:
  harness-certs:
    external: true
    name: harness-certs
EOF

echo "[render] wrote $cf" >&2
