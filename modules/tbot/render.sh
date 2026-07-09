#!/usr/bin/env bash
# Render a tbot (Machine ID) test cluster into $OUT: auth+proxy that bootstraps a
# test bot, a tbot that joins (token method) and writes an identity output, and a
# negative tbot that must be denied (bad token).
set -euo pipefail

MODULE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${OUT:?}" "${CLUSTER_ID:?}" "${FQDN:?}" "${PORT:?}" "${IMAGE:?}"
export CLUSTER_ID FQDN PORT
BOT_TOKEN="harness-tbot-secret"

mkdir -p "$OUT/config"
for t in "$MODULE"/config/*.tmpl; do
  envsubst '${CLUSTER_ID} ${FQDN} ${PORT}' < "$t" > "$OUT/config/$(basename "$t" .tmpl)"
done

cat > "$OUT/docker-compose.yml" <<EOF
name: teleport-harness-${CLUSTER_ID}

services:
  auth:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-auth
    entrypoint: ["/scripts/auth-entrypoint.sh"]
    environment:
      BOT_TOKEN: ${BOT_TOKEN}
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

  # positive: joins with the real token, writes an identity output to /out/id.
  tbot:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-tbot
    command: [tbot, start, -c, /etc/tbot.yaml, "--token=${BOT_TOKEN}", "--join-method=token"]
    volumes:
      - ${OUT}/config/tbot.yaml:/etc/tbot.yaml:ro
    networks: [internal]
    depends_on:
      auth: {condition: service_healthy}

  # negative: wrong token -> must be denied, produces no identity.
  tbot-deny:
    image: ${IMAGE}
    platform: linux/amd64
    container_name: ${CLUSTER_ID}-tbot-deny
    command: [tbot, start, -c, /etc/tbot.yaml, "--token=wrong-secret-nope", "--join-method=token"]
    volumes:
      - ${OUT}/config/tbot.yaml:/etc/tbot.yaml:ro
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
