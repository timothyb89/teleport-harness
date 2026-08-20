# Cluster lifecycle: up / teardown / ls / logs / web.
# shellcheck shell=bash
#
# Cluster rendering is done by the Python brain (`pybrain render --modules …`): it composes
# a base auth+proxy scaffold + shared components + module `services.yml.j2` fragments into
# OUT/docker-compose.yml (see harness/render.py). The auth+proxy container is "${CLUSTER_ID}-auth",
# listens on ${PORT}, mounts the shared "harness-certs" volume, joins the external
# "teleport-harness" network with alias ${FQDN}, and sets public_addr ${FQDN}:${PORT}.

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
  require_cmd docker openssl

  local id fqdn out image
  id="${ID:-$(gen_id)}"; fqdn="$(fqdn "$id")"; out="$(state_dir_for "$id")"
  [ -e "$out" ] && die "cluster id '$id' already exists ($out)"

  ingress_up
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
  local id="$1"
  hlog "waiting for auth to become healthy"
  for _ in $(seq 1 60); do
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
  # Per-cluster images built by a component prebuild (today: the k8s-runner operator image,
  # tagged with the cluster id so concurrent clusters never collide). `compose down` does
  # not know about these — without this they'd accumulate one per run. No-op if absent.
  docker image rm -f "teleport-harness-operator:$id" >/dev/null 2>&1 || true
  # The tpm module's joining client is a lima VM, not a container, so it is outside
  # everything `compose down` collects. Each VM carries a disk image and its own emulated
  # TPM state, so leaking one per run is expensive as well as untidy. No-op if absent.
  if command -v limactl >/dev/null 2>&1 && \
     limactl list --format '{{.Name}}' 2>/dev/null | grep -qx "tpm-$id"; then
    hlog "deleting TPM VM tpm-$id"
    limactl delete -f "tpm-$id" >/dev/null 2>&1 || true
  fi
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
