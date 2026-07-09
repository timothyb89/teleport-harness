#!/usr/bin/env bash
# Phase 0 spike runner. Renders configs for a cluster id, brings it up behind the
# shared ingress, and verifies: agent joins (east-west via docker alias on :443) and
# the web UI is reachable + publicly trusted via the ingress (north-south on :PORT).
#
# Usage: spike/run.sh [cluster-id]      (default: spike)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$HERE"

set -a; . "$ROOT/targets/default.env"; set +a
export CLUSTER_ID="${1:-spike}"
export CLUSTER_FQDN="${CLUSTER_ID}.lab.${HARNESS_DOMAIN}"
export INGRESS_PORT="${INGRESS_PORT:-8443}"

echo "== rendering configs for ${CLUSTER_FQDN} =="
mkdir -p "gen/${CLUSTER_ID}"

cat > "gen/${CLUSTER_ID}/auth.yaml" <<EOF
version: v3
teleport:
  nodename: auth
  data_dir: /var/lib/teleport
  diag_addr: 0.0.0.0:3000
  log: {output: stderr, severity: INFO}
auth_service:
  enabled: "yes"
  cluster_name: ${CLUSTER_FQDN}
  listen_addr: 0.0.0.0:3025
  proxy_listener_mode: multiplex
  tokens:
    - "node:spike-node-token"
proxy_service:
  enabled: "yes"
  # One port everywhere (${INGRESS_PORT}) so east-west (agents) and north-south (ingress)
  # agree — avoids the public_addr/dial port split that breaks reverse tunnels.
  web_listen_addr: 0.0.0.0:${INGRESS_PORT}
  public_addr: ["${CLUSTER_FQDN}:${INGRESS_PORT}"]
  https_keypairs:
    - key_file: /certs/wildcard.key
      cert_file: /certs/wildcard.crt
ssh_service: {enabled: "no"}
EOF

cat > "gen/${CLUSTER_ID}/agent.yaml" <<EOF
version: v3
teleport:
  nodename: ${CLUSTER_ID}-agent
  data_dir: /var/lib/teleport
  # East-west: dial the proxy by its FQDN (docker alias) on the shared port, NOT via ingress.
  proxy_server: ${CLUSTER_FQDN}:${INGRESS_PORT}
  auth_token: spike-node-token
  log: {output: stderr, severity: INFO}
ssh_service:
  enabled: "yes"
  labels: {spike: "true"}
auth_service: {enabled: "no"}
proxy_service: {enabled: "no"}
EOF

echo "== bringing up cluster ${CLUSTER_ID} =="
docker compose -f docker-compose.yml up -d

echo "== registering SNI route with ingress =="
mkdir -p "$ROOT/ingress/dynamic"
echo "${CLUSTER_FQDN} ${CLUSTER_ID}-auth:${INGRESS_PORT};" > "$ROOT/ingress/dynamic/${CLUSTER_ID}.map"
docker exec harness-ingress nginx -s reload 2>/dev/null && echo "  ingress reloaded" || echo "  WARN ingress reload failed"

echo "== waiting for agent to join =="
ok=0
for _ in $(seq 1 60); do
  if docker exec "${CLUSTER_ID}-auth" tctl get nodes --format text 2>/dev/null | grep -q "${CLUSTER_ID}-agent"; then
    ok=1; break
  fi
  sleep 2
done
[ "$ok" = 1 ] && echo "  PASS agent joined" || echo "  FAIL agent did not join"

echo "== web UI reachable + PUBLICLY TRUSTED via ingress (host -> :${INGRESS_PORT}) =="
# NB: macOS system curl (LibreSSL) mishandles this TLS path and reports 000 even when
# it works; python3 (OpenSSL, validates against system trust like a browser) is the
# authoritative check.
python3 - "$CLUSTER_FQDN" "$INGRESS_PORT" <<'PY'
import sys, urllib.request, urllib.error
fqdn, port = sys.argv[1], sys.argv[2]
url = f"https://{fqdn}:{port}/web/login"
try:
    r = urllib.request.urlopen(url, timeout=10)
    print(f"  {url} -> HTTP {r.status} (trusted, no CA import)")
except urllib.error.HTTPError as e:
    print(f"  {url} -> HTTP {e.code} (reached + trusted)")
except Exception as e:
    print(f"  {url} -> FAIL {type(e).__name__}: {e}")
PY

echo "== nodes =="
docker exec "${CLUSTER_ID}-auth" tctl get nodes --format text 2>/dev/null || true
