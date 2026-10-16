# Admin access via a privileged BOT identity (bots are exempt from admin-action MFA;
# user-minted identity files are not). A long-running tbot writes a renewable identity
# that only the cluster can see (the backend's admin_* ops); `cluster tctl`/`cluster tsh`
# run the cluster's own image so the client version always matches. The web UI is
# break-glass (`cluster web`).
# shellcheck shell=bash

ADMIN_BOT_SECRET="harness-admin-secret"

# cluster_admin <id> — create/refresh the privileged admin bot + identity.
cluster_admin() {
  load_target
  local id="${1:?usage: admin <id>}"
  local out fqdn port img
  out="$(state_dir_for "$id")"; [ -d "$out" ] || die "no such cluster: $id"
  backend_for_cluster "$id"
  fqdn="$(cluster_meta "$id" FQDN)"; port="$(cluster_meta "$id" PORT)"; img="$(cluster_meta "$id" IMAGE)"

  # 1) privileged bot + a token-method join token. Uses local-admin tctl inside the
  #    auth container (the built-in admin is MFA-exempt, as the bootstrap already relies on).
  if ! be exec "$id" auth tctl bots ls 2>/dev/null | grep -qw harness-admin; then
    hlog "creating privileged admin bot (roles: editor,access,auditor)"
    be exec_i "$id" auth tctl create -f - >/dev/null 2>&1 <<EOF || true
kind: token
version: v2
metadata: {name: ${ADMIN_BOT_SECRET}}
spec: {roles: [Bot], bot_name: harness-admin, join_method: token}
EOF
    be exec "$id" auth tctl bots add harness-admin \
      --roles=editor,access,auditor --token="${ADMIN_BOT_SECRET}" >/dev/null 2>&1 || true
  fi

  # 2) long-running tbot that outputs a renewable identity.
  cat > "$out/admin-tbot.yaml" <<EOF
version: v2
proxy_server: ${fqdn}:${port}
storage: {type: directory, path: /var/lib/tbot}
outputs:
  - type: identity
    destination: {type: directory, path: /idents}
EOF
  be admin_up "$id" "$out" "$img" "$out/admin-tbot.yaml" "$ADMIN_BOT_SECRET"

  # 3) wait for the identity to land.
  for _ in $(seq 1 30); do
    be admin_identity_present "$id" && break
    sleep 2
  done
  be admin_identity_present "$id" || die "admin identity never appeared (check the logs of ${id}-admin-tbot)"

  # Also drop a copy on the host for native tsh/tctl (tbot keeps the cluster's copy renewed;
  # re-run `admin` to refresh this one). chmod 600 — it's a credential.
  be admin_copy_identity "$id" "$out/identity" && chmod 600 "$out/identity" || true

  hok "admin identity ready for '$id'"
  echo "  containerized (version-matched, auto-renewed):"
  echo "    $(basename "$0") tctl $id get nodes"
  echo "    $(basename "$0") tsh  $id ls"
  echo "  native host binaries (uses $out/identity):"
  echo "    tsh --proxy $fqdn:$port -i $out/identity ls"
}

_admin_ready() {
  local id="$1"
  backend_for_cluster "$id"
  be admin_ready "$id" || die "no admin identity for '$id' — run: $(basename "$0") admin $id"
}

# cluster_tctl <id> [tctl args...] — admin tctl via the bot identity (MFA-free).
cluster_tctl() {
  load_target
  local id="${1:?usage: tctl <id> [args...]}"; shift || true
  _admin_ready "$id"
  be admin_run "$id" 0 tctl --identity /id/identity --auth-server "${id}-auth:3025" "$@"
}

# cluster_tsh <id> [tsh args...] — tsh via the bot identity + proxy (interactive-capable).
cluster_tsh() {
  load_target
  local id="${1:?usage: tsh <id> [args...]}"; shift || true
  _admin_ready "$id"
  local tty=0; [ -t 0 ] && tty=1
  be admin_run "$id" "$tty" \
    tsh --proxy "$(cluster_meta "$id" FQDN):$(cluster_meta "$id" PORT)" --identity /id/identity "$@"
}
