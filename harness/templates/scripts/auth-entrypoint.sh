#!/usr/bin/env bash
# Shared auth+proxy entrypoint (mounted into every cluster's auth container).
#
# Starts teleport, waits for it, then applies every bootstrap resource the
# cluster's components/modules contributed (roles + provision tokens, rendered
# into /bootstrap) and adds every declared bot — all via LOCAL-ADMIN tctl, which
# only works because this runs in the same container as the auth server. Finally
# touches /tmp/bootstrap-done so the healthcheck lets dependent bots/agents join
# ONLY after bootstrap has completed (closes the join-before-user-exists race).
#
# This is generic + declarative on purpose: multiple modules compose into one
# cluster by each dropping their role/token YAML + bots.manifest lines here.
set -euo pipefail
CONFIG=/etc/teleport/auth.yaml

echo "[auth] starting teleport auth+proxy..."
teleport start --config "$CONFIG" &
TPID=$!

echo "[auth] waiting for healthz on :3000..."
for _ in $(seq 1 120); do
  curl -fsS http://localhost:3000/healthz >/dev/null 2>&1 && break
  kill -0 "$TPID" 2>/dev/null || { echo "[auth] teleport exited during startup" >&2; wait "$TPID"; exit 1; }
  sleep 1
done

echo "[auth][bootstrap] applying resources from /bootstrap ..."
shopt -s nullglob
for f in /bootstrap/*.yaml; do
  echo "[auth][bootstrap] apply $(basename "$f")"
  tctl --config "$CONFIG" create -f "$f" 2>&1 | sed 's/^/[auth][bootstrap] /' || true
done

# Bootstrap hooks: local-admin scripts for resources that must be built at runtime
# (e.g. a kube static_jwks token whose JWKS is fetched from the oidc-server). Run
# after static resources + before bots add, with CONFIG exported for tctl.
export CONFIG
for h in /bootstrap/hooks/*.sh; do
  echo "[auth][bootstrap] hook $(basename "$h")"
  bash "$h" 2>&1 | sed 's/^/[auth][bootstrap] /' || true
done

# bots.manifest: TAB-separated  name<TAB>roles<TAB>token  (one bot per line).
# Tokens are applied above, so `bots add --token` references an existing token.
if [ -f /bootstrap/bots.manifest ]; then
  while IFS=$'\t' read -r name roles token || [ -n "$name" ]; do
    [ -z "$name" ] && continue
    case "$name" in \#*) continue ;; esac
    if tctl --config "$CONFIG" bots ls 2>/dev/null | grep -qw "$name"; then
      echo "[auth][bootstrap] bot $name already exists"
    else
      echo "[auth][bootstrap] adding bot $name (roles=$roles)"
      # empty token => create the bot; a separately-created join token (matching bot_name)
      # authorizes the join (e.g. a kubernetes-method token).
      if [ -n "$token" ]; then
        tctl --config "$CONFIG" bots add "$name" --roles="$roles" --token="$token" 2>&1 | sed 's/^/[auth][bootstrap] /' || true
      else
        tctl --config "$CONFIG" bots add "$name" --roles="$roles" 2>&1 | sed 's/^/[auth][bootstrap] /' || true
      fi
    fi
  done < /bootstrap/bots.manifest
fi

touch /tmp/bootstrap-done   # signal the healthcheck: dependent bots/agents may now join
echo "[auth] bootstrap complete; teleport running (pid $TPID)"
wait "$TPID"
