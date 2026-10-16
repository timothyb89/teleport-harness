# Cluster lifecycle: up / teardown / ls / logs / web.
# shellcheck shell=bash
#
# Cluster rendering is done by the Python brain (`pybrain render --modules …`): it composes
# a base auth+proxy scaffold + shared components + module `services.yml.j2` fragments into
# OUT/docker-compose.yml (see harness/render.py). The auth+proxy service is "${CLUSTER_ID}-auth",
# listens on ${PORT}, mounts the shared wildcard cert, is reachable by alias ${FQDN}, and sets
# public_addr ${FQDN}:${PORT}. Running it is the backend's job (lib/backend.sh: `be …`).

# cluster_up <module>  — single-module convenience (cluster up <module>).
cluster_up() {
  local module="${1:?usage: cluster up <module> --repo <path> [--id <id>]}"
  cluster_up_modules "$module" "$module"
}

# cluster_up_modules <label> <module-csv>  — render+start a (possibly multi-module,
# component-composed) cluster. <label> is what shows in `ls`/reports (module or plan name).
cluster_up_modules() {
  local label="${1:?}" modules_csv="${2:?}"
  load_target
  resolve_source     # --repo | --package | --binary  (exports SOURCE_* / IMAGE_TAG)
  local m
  for m in ${modules_csv//,/ }; do
    [ -d "$MODULES_DIR/$m" ] || die "unknown module '$m' (see: ls $MODULES_DIR)"
  done
  require_cmd openssl
  be preflight

  local id fqdn out image
  id="${ID:-$(gen_id)}"; fqdn="$(fqdn "$id")"; out="$(state_dir_for "$id")"
  [ -e "$out" ] && die "cluster id '$id' already exists ($out)"

  be infra_up
  image="$(build_image)"
  # build_image runs in a subshell, so pick its recorded `teleport version` back up off
  # disk — for a clone this is the only place the version is ever known (the report wants
  # it; gating deliberately does not use it — see resolve_source).
  if [ -z "${SOURCE_VERSION:-}" ] && [ -s "$BIN_CACHE/VERSION" ]; then
    SOURCE_VERSION="$(cat "$BIN_CACHE/VERSION")"
  fi

  # Enterprise builds need a license file, or auth exits 1. A clone brings its own bundled
  # test license; a package/binary has nothing to take one from, so it must be supplied —
  # `--license-file <pem>`, HARNESS_LICENSE_FILE, or HARNESS_LICENSE_FILE in the target env
  # (all the same knob: the flag exports the var). Every error below names all three,
  # because the only thing worse than needing a license is guessing how to hand one over.
  local license_arg="" license_file="" how="--license-file <pem> (or HARNESS_LICENSE_FILE, or set it in targets/${TARGET:-default}.env)"
  if [ "${ENT:-0}" = 1 ]; then
    if [ -n "${HARNESS_LICENSE_FILE:-}" ]; then license_file="$HARNESS_LICENSE_FILE"
    elif [ "$SOURCE_KIND" = repo ]; then license_file="$REPO/e/fixtures/license-all-features.pem"
    else die "an enterprise $SOURCE_KIND needs a license (no clone to take e/fixtures/license-all-features.pem from): pass $how"; fi
    [ -f "$license_file" ] || die "ent build needs a license but '$license_file' is not a file: pass $how"
    # Absolute, because it becomes a docker bind mount — a relative path would resolve
    # against the compose file's dir (state/<id>/) and silently mount the wrong thing.
    license_file="$(cd "$(dirname "$license_file")" && pwd)/$(basename "$license_file")"
    license_arg="--license-file $license_file"
    hlog "ent build: mounting license $license_file"
  fi

  mkdir -p "$out"
  cat > "$out/meta.env" <<EOF
CLUSTER_ID=$id
BACKEND=$HARNESS_BACKEND
KUBECONFIG=${HARNESS_KUBECONFIG:-}
KUBE_CONTEXT=${HARNESS_KUBE_CONTEXT:-}
FQDN=$fqdn
PORT=$INGRESS_PORT
IMAGE=$image
MODULE=$label
MODULES=$modules_csv
REPO=$REPO
SHA=$SOURCE_KEY
SOURCE_KIND=$SOURCE_KIND
SOURCE_REF=$SOURCE_REF
SOURCE_LABEL=$SOURCE_LABEL
SOURCE_VERSION=${SOURCE_VERSION:-}
FEATURES=${FEATURES:-}
VERSION=${VERSION:-}
DOMAIN=$HARNESS_DOMAIN
CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

  hlog "rendering cluster '$id' [$label: $modules_csv] at $fqdn"
  # --repo may be empty (package/binary source): units that need the clone declare
  # `requires_repo: true` and the renderer refuses rather than mounting nothing.
  pybrain render --modules "$modules_csv" --cluster-id "$id" --fqdn "$fqdn" --port "$INGRESS_PORT" \
    --image "$image" --harness-domain "$HARNESS_DOMAIN" --lab-domain "$LAB_DOMAIN" \
    --repo "${REPO:-}" $license_arg --out "$out" || die "render failed"
  [ -f "$out/docker-compose.yml" ] || die "render did not produce $out/docker-compose.yml"

  hlog "starting services ($HARNESS_BACKEND)"
  be up "$id" "$out"
  cluster_wait_healthy "$id"
  hok "cluster '$id' up  ->  https://$fqdn:$INGRESS_PORT"
  echo "  logs:     $(basename "$0") logs $id"
  echo "  web:      $(basename "$0") web $id"
  echo "  teardown: $(basename "$0") teardown $id"
}

cluster_wait_healthy() {
  local id="$1"
  hlog "waiting for auth to become healthy"
  for _ in $(seq 1 60); do
    if be auth_healthy "$id"; then hok "auth healthy"; return 0; fi
    sleep 2
  done
  hwarn "auth not healthy after 120s (check: $(basename "$0") logs $id auth)"
}

cluster_teardown() {
  load_target
  local id="${1:?usage: cluster teardown <id|--all>}"
  if [ "$id" = "--all" ]; then
    local c; for c in $(list_cluster_ids); do cluster_teardown "$c"; done; return 0
  fi
  local out; out="$(state_dir_for "$id")"
  [ -d "$out" ] || die "no such cluster: $id"
  backend_for_cluster "$id"
  hlog "tearing down $id ($HARNESS_BACKEND)"
  be teardown "$id" "$out"
  rm -rf "$out"
  hok "torn down $id"
}

cluster_ls() {
  load_target
  local id local_ids remote
  printf '%-10s %-34s %-14s %s\n' ID FQDN MODULE STATUS
  local_ids="$(list_cluster_ids)"
  for id in $local_ids; do
    local st; st="$(backend_for_cluster "$id" && be status "$id")"
    printf '%-10s %-34s %-14s %s\n' "$id" "$(cluster_meta "$id" FQDN)" "$(cluster_meta "$id" MODULE)" "$st"
  done
  # Clusters on the target's backend that ANOTHER client made (a Beams session, the
  # laptop): listed so they can be attached, managed and torn down from here.
  backend_use "${BACKEND:-docker}"
  remote="$(be remote_ls 2>/dev/null || true)"
  local rid exp when
  while read -r rid exp; do
    [ -n "$rid" ] || continue
    printf '%s\n' "$local_ids" | grep -qx "$rid" && continue
    when="-"; [ "$exp" != "-" ] && when="expires $(date -u -r "$exp" +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -d "@$exp" +%Y-%m-%dT%H:%MZ)"
    printf '%-10s %-34s %-14s %s\n' "$rid" "$rid.$LAB_DOMAIN" "?" "not attached ($when): $(basename "$0") attach $rid"
  done <<<"$remote"
}

# cluster_attach <id> — manage a cluster another client created: hydrate state/<id>/ from
# what its `up` published, after which logs/admin/tctl/report/teardown work as usual.
cluster_attach() {
  load_target
  local id="${1:?usage: attach <id>}" out
  out="$(state_dir_for "$id")"
  [ -e "$out" ] && die "cluster '$id' already has local state ($out)"
  be attach "$id" "$out"
  hok "attached '$id' ($(cluster_meta "$id" MODULE): $(cluster_meta "$id" SOURCE_LABEL))"
  echo "  admin:    $(basename "$0") admin $id"
  echo "  teardown: $(basename "$0") teardown $id"
}

# cluster_extend <id> [hours] — push a cluster's expiry out (default 24h from now).
cluster_extend() {
  load_target
  local id="${1:?usage: extend <id> [hours]}" hours="${2:-24}" at
  [ -d "$(state_dir_for "$id")" ] || die "no such cluster: $id"
  case "$hours" in ''|*[!0-9]*) die "hours must be a whole number, got '$hours'" ;; esac
  backend_for_cluster "$id"
  at="$(be extend "$id" "$hours")"
  hok "'$id' now expires $(date -u -r "$at" +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -d "@$at" +%Y-%m-%dT%H:%MZ)"
}

cluster_forward() {
  load_target
  be forward "${1:-start}"
}

cluster_logs() {
  local id="${1:?usage: cluster logs <id> [service]}"; shift || true
  local out; out="$(state_dir_for "$id")"; [ -d "$out" ] || die "no such cluster: $id"
  backend_for_cluster "$id"
  be logs "$id" "$@"
}

# Print the web URL and mint an admin signup link.
cluster_web() {
  load_target
  local id="${1:?usage: cluster web <id>}"
  local fqdn port; fqdn="$(cluster_meta "$id" FQDN)"; port="$(cluster_meta "$id" PORT)"
  [ -n "$fqdn" ] || die "no such cluster: $id"
  backend_for_cluster "$id"
  echo "Web UI: https://$fqdn:$port"
  local invite
  invite="$(be exec "$id" auth tctl users add admin --roles=editor,access,auditor 2>/dev/null \
            | grep -oE 'https://[^ ]+/web/invite/[a-z0-9]+' | head -1 || true)"
  if [ -n "$invite" ]; then echo "Admin signup (expires ~1h): $invite"
  else echo "Admin 'admin' already exists. Reset: $(basename "$0") tctl $id users rm admin && $(basename "$0") web $id"; fi
}
