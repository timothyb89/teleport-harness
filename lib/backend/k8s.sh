# shellcheck shell=bash
# The k8s backend: each cluster is a namespace th-<id> on a long-lived k3s host reached with
# kubectl (from Beams: through `tsh kube`). Design: docs/remote-kubernetes.md; the host's
# shared infra (registry, Traefik, buildkitd, janitor) is infra/k8s/, set up once.
#
# Built so far (Phase 3): everything about IMAGES. No docker daemon is involved anywhere:
#   - the teleport image is `crane append` of one deterministic layer per binary
#     (harness layer-tar) onto a base that buildkitd builds from lib/backend/k8s/base.Dockerfile;
#   - side images (the oidc-server) are Dockerfile builds on the in-cluster buildkitd;
#   - artifact images carry large read-only mounts (k8s_publish_artifacts).
# Every image is content-keyed and the in-cluster registry is the cache, so whichever
# client (laptop or Beams) publishes a key first, the others find it there.
# Running clusters is Phase 4; those ops refuse with a pointer until then.
#
# Target env (targets/<t>.env):
#   BACKEND=k8s
#   KUBE_CONTEXT       kubectl context of the harness host (default: the current context)
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

kc() { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} "$@"; }

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
  addr="kube-pod://$pod?namespace=$K8S_SYSTEM_NS${KUBE_CONTEXT:+&context=$KUBE_CONTEXT}"
  hlog "buildkitd: building $tag from $ctx"
  "$(tool_path buildctl)" --addr "$addr" build --progress plain \
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
be_k8s_preflight() {
  require_cmd kubectl jq openssl
  tool_path crane >/dev/null
  kc get namespace "$K8S_SYSTEM_NS" >/dev/null 2>&1 \
    || die "cannot reach namespace $K8S_SYSTEM_NS with kubectl${KUBE_CONTEXT:+ (context $KUBE_CONTEXT)} — is the host up and set up per infra/README.md?"
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
  t0="$(date +%s)"
  if kc get namespace "$K8S_SYSTEM_NS" >/dev/null 2>&1; then
    t1="$(date +%s)"
    pass "kubectl reaches $K8S_SYSTEM_NS${KUBE_CONTEXT:+ (context $KUBE_CONTEXT)} ($((t1 - t0))s round trip)"
  else
    chk_fail "kubectl cannot reach namespace $K8S_SYSTEM_NS${KUBE_CONTEXT:+ (context $KUBE_CONTEXT)} (infra/README.md)"
    return 0
  fi
  if tool_path crane >/dev/null && tool_path buildctl >/dev/null; then
    pass "crane $CRANE_VERSION + buildctl $BUILDCTL_VERSION available"
  else chk_fail "cannot obtain pinned crane/buildctl"; fi
  problems="$(_k8s_infra_problems)"
  if [ -z "$problems" ]; then pass "host infra ready (registry at $(_k8s_registry_push), buildkitd, wildcard cert)"
  else while IFS= read -r line; do chk_fail "$line"; done <<<"$problems"; fi
}

# ---- ops: clusters (Phase 4) and admin access (Phase 5) ------------------------------
be_k8s_up()           { _k8s_todo up; }
be_k8s_auth_healthy() { _k8s_todo auth_healthy; }
be_k8s_status()       { _k8s_todo status; }
be_k8s_logs()         { _k8s_todo logs; }
be_k8s_collect_logs() { _k8s_todo collect_logs; }
be_k8s_exec()         { _k8s_todo exec; }
be_k8s_exec_i()       { _k8s_todo exec_i; }
be_k8s_running()      { _k8s_todo running; }
be_k8s_teardown()     { _k8s_todo teardown; }
be_k8s_admin_up()               { _k8s_todo admin_up 5; }
be_k8s_admin_ready()            { _k8s_todo admin_ready 5; }
be_k8s_admin_identity_present() { _k8s_todo admin_identity_present 5; }
be_k8s_admin_copy_identity()    { _k8s_todo admin_copy_identity 5; }
be_k8s_admin_run()              { _k8s_todo admin_run 5; }
