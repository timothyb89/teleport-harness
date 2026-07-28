#!/usr/bin/env bash
# Pre-render hook (run by harness/render.py with the render context as UPPER_CASE env).
# Builds the Teleport Kubernetes operator from the clone's WORKING TREE, packages it into
# a container image, and stages that image plus the checked-in CRDs where the cluster can
# pick them up.
#
# Deliberately NOT cached by commit SHA (unlike lib/build.sh): like terraform-runner, the
# point of this component is the edit -> rebuild -> retest loop on an UNCOMMITTED fix, so
# it always rebuilds. Go's build cache keeps a no-op rebuild to a couple of seconds.
set -euo pipefail
: "${REPO:?}" "${OUT:?}" "${CLUSTER_ID:?}"

opdir="$REPO/integrations/operator"
[ -d "$opdir" ] || { echo "[render] $opdir not found — does this clone have the operator?" >&2; exit 1; }

# --- arch --------------------------------------------------------------------
# The rest of the harness is pinned to linux/amd64, but the k8s side is built for the
# docker daemon's NATIVE architecture on purpose: emulating a whole control plane is slow
# and flaky, and a cross-arch image would not even resolve on the node. The operator only
# speaks gRPC to auth, so its architecture is irrelevant to what is under test.
daemon_arch="$(docker info --format '{{.Architecture}}' 2>/dev/null || echo x86_64)"
case "$daemon_arch" in
  aarch64|arm64) goarch=arm64 ;;
  x86_64|amd64)  goarch=amd64 ;;
  *) echo "[render] unsupported docker architecture '$daemon_arch'" >&2; exit 1 ;;
esac

stage="$OUT/k8s"
rm -rf "$stage"
mkdir -p "$stage/build" "$stage/images" "$stage/crds"

# --- build -------------------------------------------------------------------
# CGO_ENABLED=0 despite the upstream Dockerfile using CGO: nothing the operator actually
# exercises needs it, and a static binary means no cross toolchain and no libc matching
# against the runtime image.
echo "[render] building teleport-operator (linux/$goarch, CGO-free) from $(basename "$REPO")" >&2
( cd "$REPO" && GOWORK=off CGO_ENABLED=0 GOOS=linux GOARCH="$goarch" \
    go build -trimpath -tags "kustomize_disable_go_plugin_support" \
      -o "$stage/build/teleport-operator" ./integrations/operator ) >&2

cat > "$stage/build/Dockerfile" <<'EOF'
FROM alpine:3.21
# ca-certificates so the operator's embedded tbot can validate the cluster's real
# Let's Encrypt proxy certificate; a shell so the pod stays debuggable with kubectl exec.
RUN apk add --no-cache ca-certificates
COPY teleport-operator /usr/local/bin/teleport-operator
ENTRYPOINT ["/usr/local/bin/teleport-operator"]
EOF

image="teleport-harness-operator:${CLUSTER_ID}"
echo "[render] packaging $image" >&2
docker build --platform "linux/$goarch" -t "$image" "$stage/build" >&2

# k3s imports any image tarball it finds under /var/lib/rancher/k3s/agent/images at agent
# startup (the documented air-gap path), which is why the deployment can use
# imagePullPolicy: Never and never touches a registry.
docker save "$image" -o "$stage/images/operator.tar"
echo "[render] image -> $stage/images/operator.tar" >&2

# --- CRDs --------------------------------------------------------------------
# The CHECKED-IN generated CRDs are what ships, so they are what we test.
# If you change integrations/operator/crdgen/*, regenerate before re-running:
#     make -C integrations/operator crd-manifests
crdsrc="$opdir/config/crd/bases"
[ -d "$crdsrc" ] || { echo "[render] no CRDs at $crdsrc" >&2; exit 1; }
cp "$crdsrc"/*.yaml "$stage/crds/"
echo "[render] $(find "$stage/crds" -name '*.yaml' | wc -l | tr -d ' ') CRDs -> $stage/crds" >&2
echo "[render] NOTE: CRDs come from the checked-in $crdsrc — after editing crdgen, run" >&2
echo "[render]       'make -C integrations/operator crd-manifests' or this will test the old schema." >&2
