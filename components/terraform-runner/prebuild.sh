#!/usr/bin/env bash
# Pre-render hook (run by harness/render.py with the render context as UPPER_CASE env).
# Build a DEV terraform-provider-teleport (linux/amd64) from the clone's WORKING TREE
# into $OUT/tf-plugins/, so a Terraform runner can `dev_overrides` straight to it.
#
# Deliberately NOT cached by commit SHA (unlike lib/build.sh): the whole point of this
# component is the edit -> rebuild -> retest loop on an UNCOMMITTED provider fix, so we
# always rebuild. Go's build cache keeps a no-op rebuild to seconds.
set -euo pipefail
: "${REPO:?}" "${OUT:?}"

tfdir="$REPO/integrations/terraform"
[ -d "$tfdir" ] || { echo "[render] $tfdir not found — does this clone have the terraform provider?" >&2; exit 1; }

echo "[render] building terraform-provider-teleport (linux/amd64) from $(basename "$REPO")" >&2
# The Makefile recipe hardcodes GOOS=$(OS)/GOARCH=$(ARCH), so cross-building means passing
# OS=/ARCH= as MAKE VARS (not GOOS=/GOARCH= env, which the recipe overrides). CGO is off in
# the recipe, so this pure-Go cross-compile needs no C toolchain. Do NOT set GOWORK here —
# the recipe sets GOWORK=off itself so the provider's replace directives resolve to $REPO.
make -C "$tfdir" build OS=linux ARCH=amd64 >&2

mkdir -p "$OUT/tf-plugins"
install -m 0755 "$tfdir/build/terraform-provider-teleport" "$OUT/tf-plugins/terraform-provider-teleport"
echo "[render] provider -> $OUT/tf-plugins/terraform-provider-teleport" >&2
