#!/usr/bin/env bash
# Pre-render hook (run by harness/render.py with the render context as UPPER_CASE env).
# Build the in-cluster OIDC server image once; reused across clusters.
set -euo pipefail
: "${OIDC_IMAGE:?}" "${MODULE_DIR:?}"
if ! docker image inspect "$OIDC_IMAGE" >/dev/null 2>&1; then
  echo "[render] building $OIDC_IMAGE" >&2
  DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t "$OIDC_IMAGE" "$MODULE_DIR/oidc-server" >/dev/null
fi
