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
  # The default push address goes through the ingress, so it needs this client's forwarder.
  if [ -z "${REGISTRY_PUSH:-}" ] && [ -z "${_K8S_FWD_CHECKED:-}" ]; then
    _K8S_FWD_CHECKED=1; be_k8s_forward start
  fi
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
  # itself), so there is nothing to start there — only this client's forwarder, then check.
  be_k8s_forward start
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
  local rc=0
  pybrain kube-up --state-dir "$out" || rc=$?
  # Publish the state for other clients even when the apply failed part-way: a half-up
  # cluster from Beams must still be attachable from the laptop, if only to tear it down.
  _k8s_publish_state "$id" "$out"
  [ "$rc" = 0 ] || die "bringing '$id' up failed — inspect: kubectl -n $(_k8s_ns "$id") get pods"
}

# _k8s_publish_state <id> <state-dir> — ConfigMap harness-meta: what `attach` needs to manage
# this cluster from another client (harness/kube_state.py says what travels).
_k8s_publish_state() {
  local id="$1" out="$2" tmp ns; ns="$(_k8s_ns "$id")"
  kc get namespace "$ns" >/dev/null 2>&1 || return 0
  tmp="$(mktemp)"
  if pybrain state-pack --state-dir "$out" --out "$tmp"; then
    kc -n "$ns" create configmap harness-meta --from-file=state.tgz="$tmp" \
      --dry-run=client -o yaml | kc apply --server-side --field-manager=teleport-harness -f - >/dev/null \
      || hwarn "could not publish cluster state to $ns/harness-meta; other clients cannot attach"
  else
    hwarn "could not pack the state of '$id'; other clients cannot attach"
  fi
  rm -f "$tmp"
}

be_k8s_auth_healthy() {
  [ "$(kc -n "$(_k8s_ns "$1")" get pod "$1-auth" \
        -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ]
}

be_k8s_status() {
  if ! kc get namespace "$(_k8s_ns "$1")" >/dev/null 2>&1; then
    echo "gone (namespace deleted; 'teardown $1' clears the local state)"; return 0
  fi
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

# ---- ops: admin access ------------------------------------------------------------------
# The admin tbot is a pod in the namespace, so the identity it keeps renewed never leaves the
# cluster; `cluster tctl/tsh` exec into that pod, which runs the cluster's own image (so the
# client always matches the server version, and a mac needs no linux binaries).
_k8s_admin_pod() { echo "$1-admin-tbot"; }

be_k8s_admin_up() {
  local id="$1" out="$2" img="$3" cfg="$4" token="$5" ns pod ip
  ns="$(_k8s_ns "$id")"; pod="$(_k8s_admin_pod "$id")"
  kc -n "$ns" get pod "$pod" >/dev/null 2>&1 && return 0
  # tbot dials the proxy by its FQDN, which resolves publicly to 127.0.0.1; the namespace's
  # resolver answers it with the auth pod, as it does for every other pod (harness/kube.py).
  ip="$(kc -n "$ns" get service harness-dns -o jsonpath='{.spec.clusterIP}')"
  [ -n "$ip" ] || die "no resolver Service harness-dns in $ns"
  hlog "starting admin tbot pod (renewable identity output)"
  kc -n "$ns" create configmap admin-tbot --from-file=tbot.yaml="$cfg" --dry-run=client -o yaml \
    | kc apply --server-side --field-manager=teleport-harness -f - >/dev/null
  jq -n --arg ns "$ns" --arg pod "$pod" --arg ip "$ip" \
        --arg img "$K8S_REGISTRY_PULL/$img" --arg token "$token" '{
    apiVersion: "v1", kind: "Pod",
    metadata: {name: $pod, namespace: $ns,
               labels: {"app.kubernetes.io/part-of": "teleport-harness", "harness.teleport/pod": $pod}},
    spec: {
      restartPolicy: "Never", enableServiceLinks: false, automountServiceAccountToken: false,
      terminationGracePeriodSeconds: 5,
      dnsPolicy: "None",
      dnsConfig: {nameservers: [$ip], options: [{name: "ndots", value: "5"}],
                  searches: [($ns + ".svc.cluster.local"), "svc.cluster.local", "cluster.local"]},
      containers: [{name: "tbot", image: $img,
        command: ["tbot", "start", "-c", "/etc/tbot/tbot.yaml", ("--token=" + $token), "--join-method=token"],
        volumeMounts: [{name: "cfg", mountPath: "/etc/tbot"},
                       {name: "id", mountPath: "/idents"},
                       {name: "id", mountPath: "/id"}]}],
      volumes: [{name: "cfg", configMap: {name: "admin-tbot"}}, {name: "id", emptyDir: {}}]}}' \
    | kc apply --server-side --field-manager=teleport-harness -f - >/dev/null
}

be_k8s_admin_ready() { kc -n "$(_k8s_ns "$1")" get pod "$(_k8s_admin_pod "$1")" >/dev/null 2>&1; }

be_k8s_admin_identity_present() {
  kc -n "$(_k8s_ns "$1")" exec "$(_k8s_admin_pod "$1")" -c tbot -- test -s /idents/identity 2>/dev/null
}

be_k8s_admin_copy_identity() {
  local tmp; tmp="$(mktemp)"
  kc -n "$(_k8s_ns "$1")" exec "$(_k8s_admin_pod "$1")" -c tbot -- cat /idents/identity > "$tmp" 2>/dev/null \
    && [ -s "$tmp" ] && mv "$tmp" "$2" || { rm -f "$tmp"; return 1; }
}

be_k8s_admin_run() {
  local id="$1" tty="$2"; shift 2
  local flags=(-i); [ "$tty" = 1 ] && flags=(-i -t)
  kc -n "$(_k8s_ns "$id")" exec "${flags[@]}" "$(_k8s_admin_pod "$id")" -c tbot -- "$@"
}

# ---- ops: clusters other clients made ----------------------------------------------------
# be_k8s_remote_ls — "<id> <expires-at>" for every harness namespace on the host, whichever
# client made it.
be_k8s_remote_ls() {
  kc get namespaces -l app.kubernetes.io/part-of=teleport-harness -o json 2>/dev/null | jq -r '
    .items[] | .metadata.labels as $l | select($l["harness.teleport/cluster-id"])
    | "\($l["harness.teleport/cluster-id"]) \($l["harness.teleport/expires-at"] // "-")"'
}

be_k8s_attach() {
  local id="$1" out="$2" ns tmp; ns="$(_k8s_ns "$id")"
  # Resolved here, not lazily in kc: kc may run in a subshell, and this client's connection
  # is what the attached meta.env must record.
  _k8s_resolve_connection
  tmp="$(mktemp)"
  kc -n "$ns" get configmap harness-meta -o jsonpath='{.binaryData.state\.tgz}' 2>/dev/null \
    | base64 -d > "$tmp" 2>/dev/null || true
  [ -s "$tmp" ] || { rm -f "$tmp"; die "no attachable state in $ns (namespace missing, or made before attach existed)"; }
  pybrain state-unpack --in "$tmp" --state-dir "$out" \
    --set "KUBECONFIG=${HARNESS_KUBECONFIG:-}" --set "KUBE_CONTEXT=$HARNESS_KUBE_CONTEXT" \
    || { rm -f "$tmp"; die "could not unpack the state of '$id'"; }
  rm -f "$tmp"
}

be_k8s_extend() {
  local id="$1" hours="$2" at
  at=$(( $(date +%s) + hours * 3600 ))
  kc label namespace "$(_k8s_ns "$id")" --overwrite "harness.teleport/expires-at=$at" >/dev/null \
    || die "could not relabel namespace $(_k8s_ns "$id")"
  echo "$at"
}

# ---- ops: the client forwarder -------------------------------------------------------
# Makes 127.0.0.1:$INGRESS_PORT reach the host's Traefik, so <id>.lab.<domain>:<port> (which
# resolves to 127.0.0.1) works from this client: tsh, a browser, a local agent. FORWARD=
#   none (default)      something already listens (lima forwards the port to the mac)
#   tsh-app:<app>       `tsh proxy app <app>` — the outer Teleport's TCP app (Beams, roaming)
#   lan:<host[:port]>   a TCP relay to the host on the LAN (harness/relay.py; port 9443)
#   kubectl             `kubectl port-forward` to Traefik — a fallback: long-lived
#                       port-forwards drop idle and streaming connections
_k8s_fwd_pidfile() { echo "$STATE_DIR/.forward-${TARGET:-default}.pid"; }

_k8s_port_open() {
  python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(s.connect_ex(("127.0.0.1", int(sys.argv[1]))))' "$1" 2>/dev/null
}

be_k8s_forward() {
  local action="${1:-start}" mode="${FORWARD:-none}" pidfile pid log
  pidfile="$(_k8s_fwd_pidfile)"; log="${pidfile%.pid}.log"
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  case "$action" in
    status)
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "forwarder running (pid $pid, $mode) on 127.0.0.1:$INGRESS_PORT"
      elif _k8s_port_open "$INGRESS_PORT"; then echo "127.0.0.1:$INGRESS_PORT is listening (not ours: FORWARD=$mode)"
      else echo "nothing listening on 127.0.0.1:$INGRESS_PORT (FORWARD=$mode)"; return 1; fi
      return 0 ;;
    stop)
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then kill "$pid"; hok "forwarder stopped"; fi
      rm -f "$pidfile"; return 0 ;;
    start) ;;
    *) die "usage: forward [start|stop|status]" ;;
  esac
  if _k8s_port_open "$INGRESS_PORT"; then return 0; fi
  [ "$mode" != none ] || die "nothing listens on 127.0.0.1:$INGRESS_PORT; set FORWARD in targets/${TARGET:-default}.env (tsh-app:<app> | lan:<host> | kubectl)"
  mkdir -p "$STATE_DIR"
  case "$mode" in
    tsh-app:*) require_cmd tsh
               nohup tsh proxy app "${mode#tsh-app:}" --port "$INGRESS_PORT" >"$log" 2>&1 & ;;
    lan:*)     nohup uv run --quiet --project "$HARNESS_ROOT" harness relay --port "$INGRESS_PORT" "${mode#lan:}" >"$log" 2>&1 & ;;
    kubectl)   [ -n "${HARNESS_KUBE_CONTEXT:-}" ] || _k8s_resolve_connection
               nohup kubectl ${HARNESS_KUBECONFIG:+--kubeconfig "$HARNESS_KUBECONFIG"} --context "$HARNESS_KUBE_CONTEXT" \
                 -n "$K8S_SYSTEM_NS" port-forward svc/harness-traefik "$INGRESS_PORT:9443" >"$log" 2>&1 & ;;
    *) die "unknown FORWARD '$mode' (none | tsh-app:<app> | lan:<host[:port]> | kubectl)" ;;
  esac
  echo $! > "$pidfile"
  for _ in $(seq 1 30); do _k8s_port_open "$INGRESS_PORT" && { hok "forwarder up ($mode) on 127.0.0.1:$INGRESS_PORT"; return 0; }; sleep 0.5; done
  die "forwarder ($mode) did not start listening on $INGRESS_PORT; see $log"
}
