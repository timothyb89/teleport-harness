#!/usr/bin/env bash
# Render a bound_keypair test cluster into $OUT: auth+proxy bootstrapping a
# bound_keypair token (preset registration secret) + the bot, a tbot that joins via
# bound_keypair and writes an identity, and a negative tbot with a wrong reg secret.
set -euo pipefail

MODULE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${OUT:?}" "${CLUSTER_ID:?}" "${FQDN:?}" "${PORT:?}" "${IMAGE:?}"
export CLUSTER_ID FQDN PORT
REG_SECRET="harness-bk-regsecret"

mkdir -p "$OUT/config"
envsubst '${CLUSTER_ID} ${FQDN} ${PORT}' < "$MODULE/config/auth.yaml.tmpl" > "$OUT/config/auth.yaml"
REGSECRET="$REG_SECRET"      envsubst '${FQDN} ${PORT} ${REGSECRET}' < "$MODULE/config/tbot.yaml.tmpl" > "$OUT/config/tbot.yaml"
REGSECRET="wrong-bk-secret"  envsubst '${FQDN} ${PORT} ${REGSECRET}' < "$MODULE/config/tbot.yaml.tmpl" > "$OUT/config/tbot-deny.yaml"

cat > "$OUT/docker-compose.yml" <<EOF
name: teleport-harness-${CLUSTER_ID}

services:
  auth:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-auth
    entrypoint: ["/scripts/auth-entrypoint.sh"]
    environment:
      REG_SECRET: ${REG_SECRET}
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

  # positive: joins via bound_keypair (correct registration secret), writes identity.
  bkbot:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-bkbot
    command: [tbot, start, -c, /etc/tbot.yaml]
    volumes:
      - ${OUT}/config/tbot.yaml:/etc/tbot.yaml:ro
    networks: [internal]
    depends_on:
      auth: {condition: service_healthy}

  # negative: wrong registration secret -> registration denied, no identity.
  bkbot-deny:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-bkbot-deny
    command: [tbot, start, -c, /etc/tbot.yaml]
    volumes:
      - ${OUT}/config/tbot-deny.yaml:/etc/tbot.yaml:ro
    networks: [internal]
    depends_on:
      auth: {condition: service_healthy}

networks:
  internal:
  teleport-harness:
    external: true
    name: teleport-harness

volumes:
  auth-data:
  harness-certs:
    external: true
    name: harness-certs
EOF

echo "[render] wrote $OUT/docker-compose.yml" >&2
