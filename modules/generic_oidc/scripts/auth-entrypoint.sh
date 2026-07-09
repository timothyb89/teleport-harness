#!/usr/bin/env bash
#
# Entrypoint for the auth+proxy service. Starts teleport, waits for it to come
# up, then bootstraps the token-admin role and the token-manager bot (with a
# fixed join token) using local-admin tctl — which only works because this runs
# in the SAME container as the auth server (localhost:3025 + shared data_dir).
# Idempotent across restarts.
set -euo pipefail

CONFIG=/etc/teleport/auth.yaml
: "${BOT_TOKEN:?BOT_TOKEN must be set}"

echo "[auth] starting teleport auth+proxy..."
teleport start --config "$CONFIG" &
TPID=$!

echo "[auth] waiting for healthz on :3000..."
for _ in $(seq 1 120); do
  if curl -fsS http://localhost:3000/healthz >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$TPID" 2>/dev/null; then
    echo "[auth] teleport exited during startup" >&2
    wait "$TPID"; exit 1
  fi
  sleep 1
done

echo "[auth] creating token-admin role (idempotent)..."
tctl --config "$CONFIG" create -f /bootstrap/role-token-admin.yaml 2>&1 \
  | sed 's/^/[auth][bootstrap] /' || true

# Pre-create the bot join token ourselves (token method, fixed secret = name) so
# tbot can join with a known secret from .env. `bots add --token` references an
# EXISTING token; without it, bots add mints a bound-keypair token instead.
echo "[auth][bootstrap] creating bot join token (token method, fixed secret)..."
tctl --config "$CONFIG" create -f - 2>&1 <<EOF | sed 's/^/[auth][bootstrap] /' || true
kind: token
version: v2
metadata:
  name: ${BOT_TOKEN}
spec:
  roles: [Bot]
  bot_name: token-manager
  join_method: token
EOF

if tctl --config "$CONFIG" bots ls 2>/dev/null | grep -qw token-manager; then
  echo "[auth][bootstrap] bot token-manager already exists"
else
  echo "[auth][bootstrap] adding bot token-manager..."
  tctl --config "$CONFIG" bots add token-manager \
    --roles=token-admin --token="$BOT_TOKEN" 2>&1 \
    | sed 's/^/[auth][bootstrap] /'
fi

echo "[auth] bootstrap complete; teleport running (pid $TPID)"
wait "$TPID"
