# shellcheck shell=bash
# The docker backend: each cluster is a compose project `teleport-harness-<id>` on the local
# daemon (lima here), its services are containers named `<id>-<svc>`, and the shared
# SNI-passthrough ingress (lib/ingress.sh) routes to them. Op contract: lib/backend.sh.

# compose <project> <compose-file> [args...]  — docker compose wrapper
compose() {
  local project="$1" file="$2"; shift 2
  docker compose -p "$project" -f "$file" "$@"
}

_docker_project() { echo "teleport-harness-$1"; }
_docker_compose_file() { echo "$(state_dir_for "$1")/docker-compose.yml"; }
_docker_admin_vol() { echo "harness-admin-$1"; }
_docker_cluster_net() { echo "teleport-harness-$1_internal"; }

be_docker_preflight() { require_cmd docker; }
be_docker_use_cluster() { :; }   # one local daemon; nothing to select

be_docker_doctor() {
  # docker + lima
  if docker info >/dev/null 2>&1; then pass "docker reachable (context: $(docker context show 2>/dev/null))"
  else chk_fail "docker not reachable — is the lima VM running? (limactl start docker)"; fi

  # amd64 emulation (binaries are amd64)
  if [ "$(docker run --rm --platform linux/amd64 alpine:3 uname -m 2>/dev/null)" = "x86_64" ]; then
    pass "linux/amd64 emulation works"
  else chk_fail "cannot run linux/amd64 containers (need Rosetta/qemu emulation in the docker VM)"; fi

  # ingress + cert
  if docker ps --format '{{.Names}}' | grep -q '^harness-ingress$'; then
    pass "ingress running"
    if docker run --rm -v harness-certs:/certs alpine:3 sh -c '[ -s /certs/wildcard.crt ]' 2>/dev/null; then
      pass "wildcard cert issued"
    else chk_warn "ingress up but cert not issued yet (docker logs harness-certs)"; fi
  else chk_warn "ingress not running yet (will be started on first 'cluster up')"; fi

  # ingress host port
  if lsof -nP -iTCP:"${INGRESS_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    docker ps --format '{{.Ports}}' | grep -q ":${INGRESS_PORT}->" && pass "ingress port ${INGRESS_PORT} in use by ingress" || chk_warn "port ${INGRESS_PORT} in use by something else"
  else pass "ingress port ${INGRESS_PORT} free"; fi
}

be_docker_infra_up() { ingress_up; }

be_docker_image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

# Base image: tiny glibc runtime + the binaries + the CLI tools the scripts need.
be_docker_publish_image() {
  local bincache="$1" image="$2" b
  cat > "$bincache/Dockerfile" <<'EOF'
FROM --platform=linux/amd64 debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl openssl bash jq \
 && rm -rf /var/lib/apt/lists/*
EOF
  for b in $HARNESS_BINS; do echo "COPY $b /usr/local/bin/$b" >> "$bincache/Dockerfile"; done
  # fdpass-teleport ships in releases (teleport execs it for the SSH multiplexer) and is
  # absent from our source builds; take it when the source has it, so a package run is as
  # close to a real install as the harness can make it.
  if [ -f "$bincache/fdpass-teleport" ]; then
    echo "COPY fdpass-teleport /usr/local/bin/fdpass-teleport" >> "$bincache/Dockerfile"
  fi
  echo 'RUN teleport version && tctl version && tbot version && tsh version --client' >> "$bincache/Dockerfile"
  DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t "$image" "$bincache" >/dev/null
}

be_docker_image_version() {
  { docker run --rm --platform linux/amd64 "$1" teleport version 2>/dev/null || true; } | head -1
}

be_docker_binary_version() {
  { docker run --rm --platform linux/amd64 -v "$1:/src:ro" --entrypoint /src/teleport \
      debian:bookworm-slim version 2>/dev/null || true; } | head -1
}

be_docker_build_side_image() {
  local ctx="$1" tag="$2"
  be_docker_image_exists "$tag" && return 0
  hlog "building $tag"
  # The legacy builder never sets BuildKit's automatic platform args, so a Dockerfile that
  # cross-compiles (FROM --platform=$BUILDPLATFORM, GOARCH=$TARGETARCH) gets them here.
  DOCKER_BUILDKIT=0 docker build --platform linux/amd64 \
    --build-arg BUILDPLATFORM=linux/amd64 --build-arg TARGETOS=linux --build-arg TARGETARCH=amd64 \
    -t "$tag" "$ctx" >/dev/null
}

be_docker_up() {
  local id="$1" out="$2"
  compose "$(_docker_project "$id")" "$out/docker-compose.yml" up -d
  register_route "$(cluster_meta "$id" FQDN)" "${id}-auth:$(cluster_meta "$id" PORT)"
}

be_docker_auth_healthy() {
  [ "$(docker inspect -f '{{.State.Health.Status}}' "${1}-auth" 2>/dev/null)" = healthy ]
}

be_docker_status() {
  docker inspect -f '{{.State.Status}}' "${1}-auth" 2>/dev/null || echo "-"
}

be_docker_logs() {
  local id="$1"; shift
  compose "$(_docker_project "$id")" "$(_docker_compose_file "$id")" logs "$@"
}

be_docker_collect_logs() {
  local id="$1" dir="$2" svc project file
  project="$(_docker_project "$id")"; file="$(_docker_compose_file "$id")"
  for svc in $(compose "$project" "$file" ps -a --format '{{.Service}}' 2>/dev/null); do
    compose "$project" "$file" logs --no-color "$svc" > "$dir/${svc}.log" 2>&1 || true
  done
}

be_docker_exec()   { local id="$1" svc="$2"; shift 2; docker exec "${id}-${svc}" "$@"; }
be_docker_exec_i() { local id="$1" svc="$2"; shift 2; docker exec -i "${id}-${svc}" "$@"; }

be_docker_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "${1}-${2}" 2>/dev/null)" = "true" ]
}

be_docker_teardown() {
  local id="$1" out="$2" fqdn
  fqdn="$(cluster_meta "$id" FQDN)"
  [ -n "$fqdn" ] && unregister_route "$fqdn" || true
  # The admin tbot (admin_up) is a plain `docker run`, outside the compose project, so
  # `compose down` neither removes it nor its volume — and while it runs it holds the
  # project's network, which then leaks too. Remove it first.
  docker rm -f "${id}-admin-tbot" >/dev/null 2>&1 || true
  docker volume rm "$(_docker_admin_vol "$id")" >/dev/null 2>&1 || true
  compose "$(_docker_project "$id")" "$out/docker-compose.yml" down -v >/dev/null 2>&1 || true
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
}

be_docker_admin_up() {
  local id="$1" out="$2" img="$3" cfg="$4" token="$5" vol net
  vol="$(_docker_admin_vol "$id")"; net="$(_docker_cluster_net "$id")"
  docker volume create "$vol" >/dev/null
  if ! docker ps --format '{{.Names}}' | grep -qx "${id}-admin-tbot"; then
    hlog "starting admin tbot (renewable identity output)"
    docker rm -f "${id}-admin-tbot" >/dev/null 2>&1 || true
    docker run -d --name "${id}-admin-tbot" --platform linux/amd64 --network "$net" \
      -v "$cfg:/etc/tbot.yaml:ro" -v "${vol}:/idents" \
      "$img" tbot start -c /etc/tbot.yaml --token="$token" --join-method=token >/dev/null
  fi
}

be_docker_admin_ready() { docker volume inspect "$(_docker_admin_vol "$1")" >/dev/null 2>&1; }

be_docker_admin_identity_present() {
  docker run --rm --network "$(_docker_cluster_net "$1")" -v "$(_docker_admin_vol "$1"):/id" \
    alpine:3 sh -c '[ -s /id/identity ]' 2>/dev/null
}

be_docker_admin_copy_identity() {
  docker cp "${1}-admin-tbot:/idents/identity" "$2" >/dev/null 2>&1
}

be_docker_admin_run() {
  local id="$1" tty="$2"; shift 2
  local flags=(--rm -i); [ "$tty" = 1 ] && flags=(--rm -it)
  docker run "${flags[@]}" --platform linux/amd64 --network "$(_docker_cluster_net "$id")" -v "$(_docker_admin_vol "$id"):/id:ro" \
    "$(cluster_meta "$id" IMAGE)" "$@"
}

# Clusters live in this client's own docker daemon: nothing remote to list or attach, no
# expiry, and the ingress already listens locally.
be_docker_remote_ls() { :; }
be_docker_attach() { die "docker clusters live on this client's own daemon; there is nothing to attach"; }
be_docker_extend() { die "docker clusters do not expire"; }
be_docker_forward() { [ "${1:-start}" = status ] && echo "docker backend: the ingress listens on 127.0.0.1:${INGRESS_PORT} itself"; return 0; }
