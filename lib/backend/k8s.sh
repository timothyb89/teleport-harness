# shellcheck shell=bash
# The k8s backend: each cluster is a namespace th-<id> on a long-lived k3s host reached with
# kubectl (from Beams: through `tsh kube`). Design: docs/remote-kubernetes.md; the host's
# shared infra (registry, Traefik, buildkitd, janitor) is infra/k8s/, set up once.
#
# Images (Phase 3). No docker daemon is involved anywhere:
#   - the teleport image is `crane append` of one deterministic layer per binary
#     (harness layer-tar) onto a base that buildkitd builds from lib/backend/k8s/base.Dockerfile;
#   - side images (the oidc-server) are Dockerfile builds on the in-cluster buildkitd;
#   - artifact images carry large read-only mounts (k8s_publish_artifacts).
# Every image is content-keyed and the in-cluster registry is the cache, so whichever
# client (laptop or Beams) publishes a key first, the others find it there.
# Clusters (Phase 4): `up` translates the render (harness/kube.py), publishes artifacts,
# then applies wave by wave (harness/kube_apply.py); the verifier execs in through
# KubeCluster (harness/cluster.py). Admin access is Phase 5 and refuses until then.
# Every kubectl call is pinned to the cluster's recorded kubeconfig + context (kc).
#
# Target env (targets/<t>.env):
#   BACKEND=k8s
#   KUBE_CONTEXT       kubectl context of the harness host (REQUIRED: never the ambient one)
#   KUBECONFIG         optional; e.g. lima's own file, to keep the harness off your others
#   INGRESS_PORT       the harness Traefik port, 9443 (infra/k8s/manifests/harness-20-traefik.yaml)
#   REGISTRY_PUSH      where THIS client pushes (default registry.lab.<domain>:<INGRESS_PORT>,
#                      the TLS route through Traefik; *.lab resolves to 127.0.0.1, so it works
#                      wherever the forwarder or a lima port-forward listens on that port)
#   REGISTRY_INSECURE  1 to push over plain http (e.g. REGISTRY_PUSH=localhost:30500, the
#                      NodePort, which lima forwards to the mac)

K8S_SYSTEM_NS=harness-system
# What pods pull from. Must match harness/kube.py REGISTRY: the node's registries.yaml and
# buildkitd's config both map this name to the in-cluster registry.
K8S_REGISTRY_PULL=registry.harness:5000
K8S_ARTIFACT_REPO=harness-artifact
K8S_HELPER_UPSTREAM=busybox:1.37
K8S_DOC="docs/remote-kubernetes.md"

# Every kubectl call names its kubeconfig and context outright. Ambient defaults are how a
# command meant for the harness host ends up on whatever cluster the user's own kubeconfig
# currently points at (a work cluster, say) — so for an EXISTING cluster they come from its
# meta.env (be_k8s_use_cluster), and a new one records what preflight resolved.
kc() {
  [ -n "${HARNESS_KUBE_CONTEXT:-}" ] || _k8s_resolve_connection
  kubectl ${HARNESS_KUBECONFIG:+--kubeconfig "$HARNESS_KUBECONFIG"} --context "$HARNESS_KUBE_CONTEXT" "$@"
}

_k8s_registry_push() { echo "${REGISTRY_PUSH:-registry.lab.${HARNESS_DOMAIN}:${INGRESS_PORT}}"; }
_k8s_push_ref() { echo "$(_k8s_registry_push)/$1"; }
_k8s_pull_ref() { echo "$K8S_REGISTRY_PULL/$1"; }

_k8s_crane() {
  [ -n "${_K8S_CRANE:-}" ] || _K8S_CRANE="$(tool_path crane)"
  if [ "${REGISTRY_INSECURE:-0}" = 1 ]; then "$_K8S_CRANE" --insecure "$@"; else "$_K8S_CRANE" "$@"; fi
}

_k8s_todo() { die "k8s backend: '$1' is not built yet (Phase ${2:-4}, $K8S_DOC §8)"; }

# Memo of what an image (or binary) reported when probed: a pod start costs seconds, and
# every key here names immutable content.
_k8s_memo_file() { echo "$CACHE_DIR/k8s/probe/$(printf '%s' "$1" | tr '/:@' '___')"; }

# ---- running one-off pods -----------------------------------------------------
# _k8s_run_once <pull-ref> <cmd…>  — run cmd in a throwaway pod in harness-system, print
# its output, and exit with its success. The pod is always deleted.
_k8s_run_once() {
  local image="$1"; shift
  local name phase="" i out rc=1
  name="probe-$(openssl rand -hex 4)"
  kc -n "$K8S_SYSTEM_NS" run "$name" --image "$image" --restart=Never \
    --labels app.kubernetes.io/name=harness-probe --command -- "$@" >/dev/null
  # 5 minutes: the first pull of a fresh teleport image is a few hundred MB.
  for i in $(seq 1 300); do
    phase="$(kc -n "$K8S_SYSTEM_NS" get pod "$name" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    case "$phase" in Succeeded|Failed) break ;; esac
    sleep 1
  done
  out="$(kc -n "$K8S_SYSTEM_NS" logs "$name" 2>&1 || true)"
  if [ "$phase" = Succeeded ]; then rc=0
  elif [ "$phase" != Failed ]; then
    out="$out
(pod $name never finished: phase '${phase:-unknown}'; $(kc -n "$K8S_SYSTEM_NS" get pod "$name" \
      -o jsonpath='{.status.containerStatuses[0].state.waiting.reason} {.status.containerStatuses[0].state.waiting.message}' 2>/dev/null || true))"
  fi
  kc -n "$K8S_SYSTEM_NS" delete pod "$name" --wait=false >/dev/null 2>&1 || true
  printf '%s\n' "$out"
  return "$rc"
}

# ---- buildkitd ------------------------------------------------------------------
# _k8s_buildkit <context-dir> <tag>  — build the Dockerfile in <context-dir> for linux/amd64
# on the in-cluster buildkitd and push it as <tag>. buildctl reaches the daemon with
# `kubectl exec` (kube-pod://), so this works over `tsh kube` like everything else.
# buildctl adds no attestations unless asked, so the output is a single-platform MANIFEST,
# which is what lets an amd64 image run on an arm64 node ($K8S_DOC §4).
_k8s_buildkit() {
  local ctx="$1" tag="$2" pod addr
  pod="$(kc -n "$K8S_SYSTEM_NS" get pod -l app=buildkitd --field-selector=status.phase=Running \
           -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -n "$pod" ] || die "no running buildkitd pod in $K8S_SYSTEM_NS (infra/k8s/manifests/harness-30-buildkitd.yaml)"
  addr="kube-pod://$pod?namespace=$K8S_SYSTEM_NS${HARNESS_KUBE_CONTEXT:+&context=$HARNESS_KUBE_CONTEXT}"
  hlog "buildkitd: building $tag from $ctx"
  # buildctl's kube-pod helper runs plain kubectl, so it gets the kubeconfig via the env.
  KUBECONFIG="${HARNESS_KUBECONFIG:-${KUBECONFIG:-}}" "$(tool_path buildctl)" --addr "$addr" build --progress plain \
    --frontend dockerfile.v0 --local context="$ctx" --local dockerfile="$ctx" \
    --opt platform=linux/amd64 \
    --output "type=image,name=$(_k8s_pull_ref "$tag"),push=true" >&2 \
    || die "buildkitd build of $tag failed (output above)"
}

# _k8s_ensure_base -> the base image's push ref, built on first use. Tagged by the
# Dockerfile's hash, so it is shared by every client and rebuilt only when edited.
_k8s_ensure_base() {
  local df="$HARNESS_ROOT/lib/backend/k8s/base.Dockerfile" key tag ctx
  key="$(shasum -a 256 "$df" | cut -c1-12)"
  tag="teleport-harness-base:$key"
  if ! _k8s_crane digest "$(_k8s_push_ref "$tag")" >/dev/null 2>&1; then
    ctx="$(mktemp -d)"
    cp "$df" "$ctx/Dockerfile"
    _k8s_buildkit "$ctx" "$tag"
    rm -rf "$ctx"
  fi
  _k8s_push_ref "$tag"
}

# _k8s_ensure_helper -> push ref of the amd64 busybox that artifact images are built on.
# Mirrored as a single-platform manifest (a copied index would not run cross-arch).
_k8s_ensure_helper() {
  local ref; ref="$(_k8s_push_ref "$K8S_ARTIFACT_REPO-base:${K8S_HELPER_UPSTREAM//:/-}")"
  if ! _k8s_crane digest "$ref" >/dev/null 2>&1; then
    hlog "mirroring $K8S_HELPER_UPSTREAM (linux/amd64) -> $ref"
    _k8s_crane copy --platform linux/amd64 "$K8S_HELPER_UPSTREAM" "$ref" >&2
  fi
  echo "$ref"
}

# _k8s_append <base-ref> <final-ref> <layer.tar…>  — append the layers and push. Blobs the
# registry already has (the same binary from another client) are not uploaded again.
_k8s_append() {
  local base="$1" final="$2"; shift 2
  local args=() l
  for l in "$@"; do args+=(-f "$l"); done
  _k8s_crane append -b "$base" "${args[@]}" -t "$final" >/dev/null
}

# ---- ops: images --------------------------------------------------------------------
be_k8s_image_exists() { _k8s_crane digest "$(_k8s_push_ref "$1")" >/dev/null 2>&1; }

be_k8s_publish_image() {
  local bincache="$1" image="$2" base layers=() b staging probe smoke
  base="$(_k8s_ensure_base)"
  staging="$bincache/.layers"; rm -rf "$staging"; mkdir -p "$staging"
  for b in $HARNESS_BINS fdpass-teleport; do
    [ -f "$bincache/$b" ] || continue          # fdpass-teleport: releases only (see docker.sh)
    pybrain layer-tar --out "$staging/$b.tar" --file "$bincache/$b=usr/local/bin/$b" >/dev/null
    layers+=("$staging/$b.tar")
  done
  # The docker backend's `RUN teleport version && …` smoke test, kept: push under a
  # scratch tag, run the four binaries in a pod, and only then give it the real tag — so a
  # broken image can never become a cache hit.
  probe="$(_k8s_push_ref "$image")-unverified"
  hlog "pushing $image ($(du -sh "$staging" | cut -f1) of layers; blobs the registry has are skipped)"
  _k8s_append "$base" "$probe" "${layers[@]}"
  smoke="$(_k8s_run_once "$(_k8s_pull_ref "$image")-unverified" \
             sh -c 'teleport version && tctl version && tbot version && tsh version --client')" \
    || die "the published image fails its smoke test:
$smoke"
  _k8s_crane tag "$probe" "${image##*:}" >/dev/null
  mkdir -p "$CACHE_DIR/k8s/probe"
  printf '%s\n' "$smoke" | head -1 > "$(_k8s_memo_file "$image")"
  rm -rf "$staging"
}

be_k8s_image_version() {
  local memo; memo="$(_k8s_memo_file "$1")"
  if [ ! -s "$memo" ]; then
    mkdir -p "$(dirname "$memo")"
    { _k8s_run_once "$(_k8s_pull_ref "$1")" teleport version || true; } | head -1 > "$memo"
  fi
  cat "$memo"
}

# A linux/amd64 client (Beams) just runs the binary. Anywhere else it has to run on the
# node, so publish a probe image of base + the teleport layer: the layer is byte-identical
# to the one the real image gets, so the registry already has it when that is pushed.
be_k8s_binary_version() {
  local dir="$1" sha memo tmp tag
  if [ "$(uname -sm)" = "Linux x86_64" ]; then
    { "$dir/teleport" version 2>/dev/null || true; } | head -1; return 0
  fi
  sha="$(shasum -a 256 "$dir/teleport" | cut -c1-12)"
  memo="$(_k8s_memo_file "bin-$sha")"
  if [ ! -s "$memo" ]; then
    tmp="$(mktemp -d)"; tag="teleport-harness-probe:$sha"
    pybrain layer-tar --out "$tmp/teleport.tar" --file "$dir/teleport=usr/local/bin/teleport" >/dev/null
    hlog "probing $dir/teleport on the node (not a linux/amd64 client)"
    _k8s_append "$(_k8s_ensure_base)" "$(_k8s_push_ref "$tag")" "$tmp/teleport.tar"
    rm -rf "$tmp"
    mkdir -p "$(dirname "$memo")"
    { _k8s_run_once "$(_k8s_pull_ref "$tag")" teleport version || true; } | head -1 > "$memo"
  fi
  cat "$memo"
}

be_k8s_build_side_image() {
  local ctx="$1" tag="$2"
  be_k8s_image_exists "$tag" && return 0
  _k8s_buildkit "$ctx" "$tag"
}

# k8s_publish_artifacts <state-dir>  — publish every artifact image in kube/plan.json (the
# large read-only mounts, harness/kube.py). Each is pushed once under a CONTENT tag
# (c<sha of its layer>) and then tagged with the name the manifests use, so an unchanged
# tf-plugins build or docs tree is a tag operation, not an upload.
k8s_publish_artifacts() {
  local out="$1" plan n i image src kind tmp digest content helper
  plan="$out/kube/plan.json"
  [ -f "$plan" ] || die "no $plan (run kube-render first)"
  n="$(jq '.artifacts | length' "$plan")"
  [ "$n" -gt 0 ] || return 0
  helper="$(_k8s_ensure_helper)"
  tmp="$(mktemp -d)"
  for i in $(seq 0 $((n - 1))); do
    image="$(jq -r ".artifacts[$i].image" "$plan")"
    src="$(jq -r ".artifacts[$i].source" "$plan")"
    kind="$(jq -r ".artifacts[$i].kind" "$plan")"
    [ -e "$src" ] || die "artifact source $src does not exist (needed by $image)"
    # The translator recorded the kind at render time; a prebuild runs before that, so
    # a source that appeared since must still match it (the mount was built for it).
    if [ -d "$src" ]; then
      [ "$kind" = dir ] || die "artifact $src is a directory but was translated as a $kind"
      digest="$(pybrain layer-tar --out "$tmp/a.tar" --dir "$src=artifact")"
    else
      [ "$kind" = file ] || die "artifact $src is a file but was translated as a $kind"
      digest="$(pybrain layer-tar --out "$tmp/a.tar" --file "$src=artifact/$(basename "$src")")"
    fi
    content="$(_k8s_push_ref "$K8S_ARTIFACT_REPO:c${digest:0:12}")"
    if _k8s_crane digest "$content" >/dev/null 2>&1; then
      hlog "artifact $(basename "$src"): cached (c${digest:0:12})"
    else
      hlog "artifact $(basename "$src"): pushing $(du -h "$tmp/a.tar" | cut -f1)"
      _k8s_append "$helper" "$content" "$tmp/a.tar"
    fi
    _k8s_crane tag "$content" "${image##*:}" >/dev/null
  done
  rm -rf "$tmp"
}

# ---- ops: host + infra ------------------------------------------------------------
# _k8s_resolve_connection — pin a NEW cluster's connection from the target: KUBE_CONTEXT is
# required (never "whatever the kubeconfig's current context is", which `tsh kube login`
# or `kubectl config use-context` can move at any time), KUBECONFIG optional.
_k8s_resolve_connection() {
  [ -n "${KUBE_CONTEXT:-}" ] || die "the k8s backend needs KUBE_CONTEXT in targets/${TARGET:-default}.env (it never falls back to your kubeconfig's current context)"
  export HARNESS_KUBECONFIG="${KUBECONFIG:-}" HARNESS_KUBE_CONTEXT="$KUBE_CONTEXT"
}

be_k8s_use_cluster() {
  local id="$1"
  HARNESS_KUBECONFIG="$(cluster_meta "$id" KUBECONFIG)"
  HARNESS_KUBE_CONTEXT="$(cluster_meta "$id" KUBE_CONTEXT)"
  [ -n "$HARNESS_KUBE_CONTEXT" ] || die "cluster $id records no KUBE_CONTEXT in its meta.env; refusing to guess which Kubernetes cluster it is on"
  export HARNESS_KUBECONFIG HARNESS_KUBE_CONTEXT
}

be_k8s_preflight() {
  require_cmd kubectl jq openssl
  _k8s_resolve_connection      # what a new cluster records in meta.env
  tool_path crane >/dev/null
  kc get namespace "$K8S_SYSTEM_NS" >/dev/null 2>&1 \
    || die "cannot reach namespace $K8S_SYSTEM_NS with kubectl (context $HARNESS_KUBE_CONTEXT) — is the host up and set up per infra/README.md?"
}

# _k8s_infra_problems — one line per missing piece of the shared infra (none = healthy).
_k8s_infra_problems() {
  kc -n "$K8S_SYSTEM_NS" get deployment registry >/dev/null 2>&1 || echo "registry deployment missing"
  kc -n "$K8S_SYSTEM_NS" get deployment buildkitd >/dev/null 2>&1 || echo "buildkitd deployment missing"
  kc -n "$K8S_SYSTEM_NS" get secret wildcard-cert >/dev/null 2>&1 \
    || echo "secret wildcard-cert missing (infra/k8s/import-cert.sh)"
  _k8s_crane catalog "$(_k8s_registry_push)" >/dev/null 2>&1 \
    || echo "registry not reachable from here at $(_k8s_registry_push) (REGISTRY_PUSH)"
}

be_k8s_infra_up() {
  # The shared infra is installed once on the host (k3s applies infra/k8s/manifests/ by
  # itself), so there is nothing to start here — only to check.
  local problems; problems="$(_k8s_infra_problems)"
  [ -z "$problems" ] || die "harness host infra is not ready (see infra/README.md):
$problems"
}

be_k8s_doctor() {
  local t0 t1 problems line
  [ -n "${KUBE_CONTEXT:-}" ] || { chk_fail "KUBE_CONTEXT is not set in targets/${TARGET:-default}.env"; return 0; }
  _k8s_resolve_connection
  t0="$(date +%s)"
  if kc get namespace "$K8S_SYSTEM_NS" >/dev/null 2>&1; then
    t1="$(date +%s)"
    pass "kubectl reaches $K8S_SYSTEM_NS(context $HARNESS_KUBE_CONTEXT) ($((t1 - t0))s round trip)"
  else
    chk_fail "kubectl cannot reach namespace $K8S_SYSTEM_NS (context $HARNESS_KUBE_CONTEXT; infra/README.md)"
    return 0
  fi
  if tool_path crane >/dev/null && tool_path buildctl >/dev/null; then
    pass "crane $CRANE_VERSION + buildctl $BUILDCTL_VERSION available"
  else chk_fail "cannot obtain pinned crane/buildctl"; fi
  problems="$(_k8s_infra_problems)"
  if [ -z "$problems" ]; then pass "host infra ready (registry at $(_k8s_registry_push), buildkitd, wildcard cert)"
  else while IFS= read -r line; do chk_fail "$line"; done <<<"$problems"; fi
}

# ---- ops: clusters --------------------------------------------------------------------
# A cluster is namespace th-<id>. Which pod/container runs each compose service comes from
# the plan kube-render wrote (state/<id>/kube/plan.json): a service that joined another's
# netns runs in THAT pod, as its own container.
_k8s_ns() { echo "th-$1"; }
_k8s_plan() { echo "$(state_dir_for "$1")/kube/plan.json"; }

# _k8s_where <id> <svc> -> "<pod> <container>"
_k8s_where() {
  jq -er --arg s "$2" '.services[$s] | select(.) | "\(.pod) \(.container)"' "$(_k8s_plan "$1")" 2>/dev/null \
    || die "cluster $1 has no service '$2' (have: $(jq -r '.services | keys | join(" ")' "$(_k8s_plan "$1")" 2>/dev/null))"
}

_k8s_services() { jq -r '.services | keys[]' "$(_k8s_plan "$1")"; }

be_k8s_up() {
  local id="$1" out="$2" expires img missing=""
  # The janitor (infra/k8s/manifests/harness-40-janitor.yaml) deletes the namespace after
  # this, so a cluster abandoned by a Beams session doesn't run forever.
  expires=$(( $(date +%s) + ${CLUSTER_TTL_HOURS:-24} * 3600 ))
  pybrain kube-render --state-dir "$out" --expires-at "$expires" >&2 || die "kube-render failed"
  # Every harness image the pods reference must already be in the registry (the teleport
  # image from build_image, side images from component prebuilds); a miss here is a
  # clearer error than an ImagePullBackOff later.
  for img in $(jq -r '.local_images[]' "$out/kube/plan.json"); do
    be_k8s_image_exists "$img" || missing="$missing $img"
  done
  [ -z "$missing" ] || die "not in the registry:$missing (a prebuild that runs 'docker build' instead of 'cluster side-image'?)"
  k8s_publish_artifacts "$out"
  pybrain kube-up --state-dir "$out" || die "bringing '$id' up failed — inspect: kubectl -n $(_k8s_ns "$id") get pods"
}

be_k8s_auth_healthy() {
  [ "$(kc -n "$(_k8s_ns "$1")" get pod "$1-auth" \
        -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]
}

be_k8s_status() {
  local st; st="$(kc -n "$(_k8s_ns "$1")" get pod "$1-auth" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  if [ -z "$st" ]; then echo "-"; elif be_k8s_auth_healthy "$1"; then echo "$st (ready)"; else echo "$st"; fi
}

# be_k8s_logs <id> [-f] [service…] — the subset of `compose logs` the CLI uses.
be_k8s_logs() {
  local id="$1"; shift
  local follow="" a pod ctr svc svcs=""
  for a in "$@"; do
    case "$a" in
      -f|--follow) follow=-f ;;
      --no-color) ;;
      -*) die "logs: '$a' is not supported on the k8s backend (have: -f, service names)" ;;
      *) svcs="$svcs $a" ;;
    esac
  done
  [ -n "$svcs" ] || svcs="$(_k8s_services "$id")"
  # shellcheck disable=SC2086  # word-split list of service names
  set -- $svcs
  if [ $# = 1 ]; then
    read -r pod ctr <<<"$(_k8s_where "$id" "$1")"
    kc -n "$(_k8s_ns "$id")" logs ${follow:+"$follow"} "$pod" -c "$ctr"; return
  fi
  [ -z "$follow" ] || die "logs -f follows one service at a time on the k8s backend"
  for svc in "$@"; do
    read -r pod ctr <<<"$(_k8s_where "$id" "$svc")"
    kc -n "$(_k8s_ns "$id")" logs "$pod" -c "$ctr" 2>&1 | sed "s/^/$svc | /" || true
  done
}

be_k8s_collect_logs() {
  local id="$1" dir="$2" svc pod ctr ns
  ns="$(_k8s_ns "$id")"
  for svc in $(_k8s_services "$id"); do
    read -r pod ctr <<<"$(_k8s_where "$id" "$svc")"
    # a restarted container's earlier run first, as `docker logs` would show it
    { kc -n "$ns" logs "$pod" -c "$ctr" --previous 2>/dev/null || true
      kc -n "$ns" logs "$pod" -c "$ctr" 2>&1 || true; } > "$dir/$svc.log"
  done
}

be_k8s_exec() {
  local id="$1" pod ctr; read -r pod ctr <<<"$(_k8s_where "$1" "$2")"; shift 2
  kc -n "$(_k8s_ns "$id")" exec "$pod" -c "$ctr" -- "$@"
}

be_k8s_exec_i() {
  local id="$1" pod ctr; read -r pod ctr <<<"$(_k8s_where "$1" "$2")"; shift 2
  kc -n "$(_k8s_ns "$id")" exec -i "$pod" -c "$ctr" -- "$@"
}

be_k8s_running() {
  local pod ctr; read -r pod ctr <<<"$(_k8s_where "$1" "$2")"
  kc -n "$(_k8s_ns "$1")" get pod "$pod" -o json 2>/dev/null | jq -e --arg c "$ctr" \
    '[(.status.containerStatuses // []) + (.status.initContainerStatuses // []) | .[]
      | select(.name == $c and .state.running)] | length > 0' >/dev/null
}

be_k8s_teardown() {
  local id="$1" ns; ns="$(_k8s_ns "$id")"
  # Everything the cluster made lives in its namespace (the route, the secrets, the PVCs).
  # Not waited on: deletion finishes in the background, and the janitor ignores it.
  kc delete namespace "$ns" --wait=false --ignore-not-found >/dev/null 2>&1 \
    || hwarn "could not delete namespace $ns (kubectl -n $ns get all)"
}

# ---- ops: admin access (Phase 5) ----------------------------------------------------------
be_k8s_admin_up()               { _k8s_todo admin_up 5; }
be_k8s_admin_ready()            { _k8s_todo admin_ready 5; }
be_k8s_admin_identity_present() { _k8s_todo admin_identity_present 5; }
be_k8s_admin_copy_identity()    { _k8s_todo admin_copy_identity 5; }
be_k8s_admin_run()              { _k8s_todo admin_run 5; }
