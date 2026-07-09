# Cluster lifecycle: up / teardown / ls / logs / web.
# shellcheck shell=bash
#
# Module contract: modules/<name>/render.sh is invoked with these env vars exported —
#   CLUSTER_ID FQDN PORT IMAGE HARNESS_DOMAIN LAB_DOMAIN OUT
# and must write a self-contained OUT/docker-compose.yml (+ any configs under OUT/)
# describing a cluster whose auth+proxy container is named "${CLUSTER_ID}-auth",
# listens on ${PORT}, mounts the shared "harness-certs" volume, joins the external
# "teleport-harness" network with alias ${FQDN}, and sets public_addr ${FQDN}:${PORT}.

cluster_up() {
  local module="${1:?usage: cluster up <module> --repo <path> [--id <id>]}"
  load_target
  : "${REPO:?--repo <teleport-clone-path> required}"
  [ -d "$MODULES_DIR/$module" ] || die "unknown module '$module' (see: ls $MODULES_DIR)"
  require_cmd docker git openssl

  local id fqdn out image
  id="${ID:-$(gen_id)}"; fqdn="$(fqdn "$id")"; out="$(state_dir_for "$id")"
  [ -e "$out" ] && die "cluster id '$id' already exists ($out)"

  ingress_up
  image="$(build_image "$REPO" "${ENT:-0}")"

  mkdir -p "$out"
  cat > "$out/meta.env" <<EOF
CLUSTER_ID=$id
FQDN=$fqdn
PORT=$INGRESS_PORT
IMAGE=$image
MODULE=$module
REPO=$REPO
SHA=$(git -C "$REPO" rev-parse --short=12 HEAD)
DOMAIN=$HARNESS_DOMAIN
CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

  hlog "rendering cluster '$id' [$module] at $fqdn"
  CLUSTER_ID="$id" FQDN="$fqdn" PORT="$INGRESS_PORT" IMAGE="$image" \
    HARNESS_DOMAIN="$HARNESS_DOMAIN" LAB_DOMAIN="$LAB_DOMAIN" OUT="$out" \
    bash "$MODULES_DIR/$module/render.sh" || die "module render failed"
  [ -f "$out/docker-compose.yml" ] || die "module did not produce $out/docker-compose.yml"

  hlog "starting containers"
  compose "teleport-harness-$id" "$out/docker-compose.yml" up -d
  register_route "$fqdn" "${id}-auth:${INGRESS_PORT}"
  cluster_wait_healthy "$id"
  hok "cluster '$id' up  ->  https://$fqdn:$INGRESS_PORT"
  echo "  logs:     $(basename "$0") logs $id"
  echo "  web:      $(basename "$0") web $id"
  echo "  teardown: $(basename "$0") teardown $id"
}

cluster_wait_healthy() {
  local id="$1" i
  hlog "waiting for auth to become healthy"
  for i in $(seq 1 60); do
    case "$(docker inspect -f '{{.State.Health.Status}}' "${id}-auth" 2>/dev/null)" in
      healthy) hok "auth healthy"; return 0 ;;
      *) sleep 2 ;;
    esac
  done
  hwarn "auth not healthy after 120s (check: docker logs ${id}-auth)"
}

cluster_teardown() {
  load_target
  local id="${1:?usage: cluster teardown <id|--all>}"
  if [ "$id" = "--all" ]; then
    local c; for c in $(list_cluster_ids); do cluster_teardown "$c"; done; return 0
  fi
  local out fqdn; out="$(state_dir_for "$id")"
  [ -d "$out" ] || die "no such cluster: $id"
  fqdn="$(cluster_meta "$id" FQDN)"
  hlog "tearing down $id"
  [ -n "$fqdn" ] && unregister_route "$fqdn" || true
  compose "teleport-harness-$id" "$out/docker-compose.yml" down -v >/dev/null 2>&1 || true
  rm -rf "$out"
  hok "torn down $id"
}

cluster_ls() {
  local id
  printf '%-10s %-34s %-14s %s\n' ID FQDN MODULE STATUS
  for id in $(list_cluster_ids); do
    local st; st="$(docker inspect -f '{{.State.Status}}' "${id}-auth" 2>/dev/null || echo "-")"
    printf '%-10s %-34s %-14s %s\n' "$id" "$(cluster_meta "$id" FQDN)" "$(cluster_meta "$id" MODULE)" "$st"
  done
}

cluster_logs() {
  local id="${1:?usage: cluster logs <id> [service]}"; shift || true
  local out; out="$(state_dir_for "$id")"; [ -d "$out" ] || die "no such cluster: $id"
  compose "teleport-harness-$id" "$out/docker-compose.yml" logs "$@"
}

# Print the web URL and mint an admin signup link.
cluster_web() {
  load_target
  local id="${1:?usage: cluster web <id>}"
  local fqdn port; fqdn="$(cluster_meta "$id" FQDN)"; port="$(cluster_meta "$id" PORT)"
  [ -n "$fqdn" ] || die "no such cluster: $id"
  echo "Web UI: https://$fqdn:$port"
  local invite
  invite="$(docker exec "${id}-auth" tctl users add admin --roles=editor,access,auditor 2>/dev/null \
            | grep -oE 'https://[^ ]+/web/invite/[a-z0-9]+' | head -1 || true)"
  if [ -n "$invite" ]; then echo "Admin signup (expires ~1h): $invite"
  else echo "Admin 'admin' already exists. Reset: docker exec ${id}-auth tctl users rm admin && $(basename "$0") web $id"; fi
}
