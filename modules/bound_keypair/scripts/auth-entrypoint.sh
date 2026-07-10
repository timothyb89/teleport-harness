#!/usr/bin/env bash
# auth+proxy entrypoint for the bound_keypair module: start teleport, then create a
# bound_keypair token with a preset registration secret and the test bot (local-admin,
# MFA-exempt).
set -euo pipefail
CONFIG=/etc/teleport/auth.yaml
: "${REG_SECRET:?}"

echo "[auth] starting teleport..."
teleport start --config "$CONFIG" &
TPID=$!
for _ in $(seq 1 120); do
  curl -fsS http://localhost:3000/healthz >/dev/null 2>&1 && break
  kill -0 "$TPID" 2>/dev/null || { echo "[auth] teleport exited"; wait "$TPID"; exit 1; }
  sleep 1
done

echo "[auth][bootstrap] role + bound_keypair token + bot..."
tctl --config "$CONFIG" create -f /bootstrap/role-tbot-tester.yaml 2>&1 | sed 's/^/[auth] /' || true
tctl --config "$CONFIG" create -f - 2>&1 <<TOK | sed 's/^/[auth] /' || true
kind: token
version: v2
metadata: {name: bk-token}
spec:
  roles: [Bot]
  bot_name: bk-bot
  join_method: bound_keypair
  bound_keypair:
    onboarding:
      registration_secret: ${REG_SECRET}
TOK
if ! tctl --config "$CONFIG" bots ls 2>/dev/null | grep -qw bk-bot; then
  tctl --config "$CONFIG" bots add bk-bot --roles=tbot-tester --token=bk-token 2>&1 | sed 's/^/[auth] /'
fi

touch /tmp/bootstrap-done   # signal the healthcheck: dependent bots may now join
echo "[auth] ready"
wait "$TPID"
