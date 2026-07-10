# Admin access via a privileged BOT identity (bots are exempt from admin-action MFA;
# user-minted identity files are not). A long-running tbot writes a renewable identity
# to a per-cluster volume; `cluster tctl`/`cluster tsh` run the cluster's own image so
# the client version always matches. The web UI is break-glass (`cluster web`).
# shellcheck shell=bash

_admin_vol()  { echo "harness-admin-$1"; }
_cluster_net() { echo "teleport-harness-$1_internal"; }
ADMIN_BOT_SECRET="harness-admin-secret"

# cluster_admin <id> — create/refresh the privileged admin bot + identity.
cluster_admin() {
  load_target
  local id="${1:?usage: admin <id>}"
  local out fqdn port img vol net
  out="$(state_dir_for "$id")"; [ -d "$out" ] || die "no such cluster: $id"
  fqdn="$(cluster_meta "$id" FQDN)"; port="$(cluster_meta "$id" PORT)"; img="$(cluster_meta "$id" IMAGE)"
  vol="$(_admin_vol "$id")"; net="$(_cluster_net "$id")"

  # 1) privileged bot + a token-method join token. Uses local-admin tctl inside the
  #    auth container (the built-in admin is MFA-exempt, as the bootstrap already relies on).
  if ! docker exec "${id}-auth" tctl bots ls 2>/dev/null | grep -qw harness-admin; then
    hlog "creating privileged admin bot (roles: editor,access,auditor)"
    docker exec -i "${id}-auth" tctl create -f - >/dev/null 2>&1 <<EOF || true
kind: token
version: v2
metadata: {name: ${ADMIN_BOT_SECRET}}
spec: {roles: [Bot], bot_name: harness-admin, join_method: token}
EOF
    docker exec "${id}-auth" tctl bots add harness-admin \
      --roles=editor,access,auditor --token="${ADMIN_BOT_SECRET}" >/dev/null 2>&1 || true
  fi

  # 2) long-running tbot that outputs a renewable identity to the shared volume.
  docker volume create "$vol" >/dev/null
  cat > "$out/admin-tbot.yaml" <<EOF
version: v2
proxy_server: ${fqdn}:${port}
storage: {type: directory, path: /var/lib/tbot}
outputs:
  - type: identity
    destination: {type: directory, path: /idents}
EOF
  if ! docker ps --format '{{.Names}}' | grep -qx "${id}-admin-tbot"; then
    hlog "starting admin tbot (renewable identity output)"
    docker rm -f "${id}-admin-tbot" >/dev/null 2>&1 || true
    docker run -d --name "${id}-admin-tbot" --platform linux/amd64 --network "$net" \
      -v "$out/admin-tbot.yaml:/etc/tbot.yaml:ro" -v "${vol}:/idents" \
      "$img" tbot start -c /etc/tbot.yaml --token="${ADMIN_BOT_SECRET}" --join-method=token >/dev/null
  fi

  # 3) wait for the identity to land.
  for _ in $(seq 1 30); do
    docker run --rm --network "$net" -v "${vol}:/id" alpine:3 sh -c '[ -s /id/identity ]' 2>/dev/null && break
    sleep 2
  done
  docker run --rm --network "$net" -v "${vol}:/id" alpine:3 sh -c '[ -s /id/identity ]' 2>/dev/null \
    || die "admin identity never appeared (docker logs ${id}-admin-tbot)"

  # Also drop a copy on the host for native tsh/tctl (tbot keeps the volume renewed;
  # re-run `admin` to refresh this copy). chmod 600 — it's a credential.
  docker cp "${id}-admin-tbot:/idents/identity" "$out/identity" >/dev/null 2>&1 && chmod 600 "$out/identity" || true

  hok "admin identity ready for '$id'"
  echo "  containerized (version-matched, auto-renewed):"
  echo "    $(basename "$0") tctl $id get nodes"
  echo "    $(basename "$0") tsh  $id ls"
  echo "  native host binaries (uses $out/identity):"
  echo "    tsh --proxy $fqdn:$port -i $out/identity ls"
}

_admin_ready() {
  local id="$1"
  docker volume inspect "$(_admin_vol "$id")" >/dev/null 2>&1 \
    || die "no admin identity for '$id' — run: $(basename "$0") admin $id"
}

# cluster_tctl <id> [tctl args...] — admin tctl via the bot identity (MFA-free).
cluster_tctl() {
  load_target
  local id="${1:?usage: tctl <id> [args...]}"; shift || true
  _admin_ready "$id"
  docker run --rm -i --network "$(_cluster_net "$id")" -v "$(_admin_vol "$id"):/id:ro" \
    "$(cluster_meta "$id" IMAGE)" \
    tctl --identity /id/identity --auth-server "${id}-auth:3025" "$@"
}

# cluster_tsh <id> [tsh args...] — tsh via the bot identity + proxy (interactive-capable).
cluster_tsh() {
  load_target
  local id="${1:?usage: tsh <id> [args...]}"; shift || true
  _admin_ready "$id"
  local flags=(--rm -i); [ -t 0 ] && flags=(--rm -it)
  docker run "${flags[@]}" --network "$(_cluster_net "$id")" -v "$(_admin_vol "$id"):/id:ro" \
    "$(cluster_meta "$id" IMAGE)" \
    tsh --proxy "$(cluster_meta "$id" FQDN):$(cluster_meta "$id" PORT)" --identity /id/identity "$@"
}
