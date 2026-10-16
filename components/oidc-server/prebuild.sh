#!/usr/bin/env bash
# Pre-render hook (run by harness/render.py with the render context as UPPER_CASE env).
# Build the in-cluster OIDC server image once; reused across clusters. MODULE_DIR is
# this component's dir. The backend does the building (docker locally, buildkitd on the
# k8s host) and skips it when the tag already exists.
set -euo pipefail
: "${OIDC_IMAGE:?}" "${MODULE_DIR:?}" "${HARNESS_ROOT:?}"
"$HARNESS_ROOT/bin/cluster" side-image "$MODULE_DIR/build" "$OIDC_IMAGE"
