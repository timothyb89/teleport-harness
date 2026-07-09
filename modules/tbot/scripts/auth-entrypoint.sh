#!/usr/bin/env bash
# auth+proxy entrypoint for the tbot module: start teleport, then (local-admin,
# MFA-exempt) create the tester role, a token-method join token, and the test bot.
set -euo pipefail
CONFIG=/etc/teleport/auth.yaml
: "${BOT_TOKEN:?}"

echo "[auth] starting teleport..."
teleport start --config "$CONFIG" &
TPID=$!
for _ in $(seq 1 120); do
  curl -fsS http://localhost:3000/healthz >/dev/null 2>&1 && break
  kill -0 "$TPID" 2>/dev/null || { echo "[auth] teleport exited"; wait "$TPID"; exit 1; }
  sleep 1
done

echo "[auth][bootstrap] role + bot join token + bot..."
tctl --config "$CONFIG" create -f /bootstrap/role-tbot-tester.yaml 2>&1 | sed 's/^/[auth] /' || true
tctl --config "$CONFIG" create -f - 2>&1 <<TOK | sed 's/^/[auth] /' || true
kind: token
version: v2
metadata: {name: ${BOT_TOKEN}}
spec: {roles: [Bot], bot_name: test-bot, join_method: token}
TOK
if ! tctl --config "$CONFIG" bots ls 2>/dev/null | grep -qw test-bot; then
  tctl --config "$CONFIG" bots add test-bot --roles=tbot-tester --token="$BOT_TOKEN" 2>&1 | sed 's/^/[auth] /'
fi

echo "[auth] ready"
wait "$TPID"
