# Build linux/amd64 teleport/tctl/tbot from a teleport clone's working tree and bake
# a base image, keyed by commit SHA so repeat builds are instant.
# shellcheck shell=bash

: "${HARNESS_CC:=x86_64-unknown-linux-gnu-gcc}"   # messense glibc cross toolchain

# build_image <repo-path> [ent]  -> prints the resulting image tag on stdout.
# Builds from whatever is currently checked out in <repo-path> (no branch switching,
# so it never disturbs the clone). Reuses the clone's prebuilt webassets.
build_image() {
  local repo ent variant sha bincache image target tags assetdir
  repo="$(cd "$1" && pwd)"; ent="${2:-0}"
  [ -d "$repo/.git" ] || die "not a git repo: $repo"
  command -v "$HARNESS_CC" >/dev/null 2>&1 || die "cross compiler '$HARNESS_CC' not found (brew install messense/macos-cross-toolchains/x86_64-unknown-linux-gnu)"

  sha="$(git -C "$repo" rev-parse --short=12 HEAD)"
  [ "$ent" = 1 ] && variant=ent || variant=oss
  bincache="$CACHE_DIR/bin/${sha}-${variant}"
  image="teleport-harness:${sha}-${variant}"

  if [ "${REBUILD_IMAGE:-0}" != 1 ] && docker image inspect "$image" >/dev/null 2>&1; then
    hlog "image cached: $image (repo $(basename "$repo") @ $sha)"
    echo "$image"; return 0
  fi

  mkdir -p "$bincache"
  local need=0 b
  for b in teleport tctl tbot tsh; do [ -x "$bincache/$b" ] || need=1; done
  if [ "$need" = 1 ]; then
    if [ "$ent" = 1 ]; then target=./e/tool/teleport; tags="grpcnotrace webassets_embed webassets_ent"; assetdir=webassets/e/teleport/app
    else target=./tool/teleport; tags="grpcnotrace webassets_embed"; assetdir=webassets/teleport/app; fi
    [ -n "$(ls -A "$repo/$assetdir" 2>/dev/null)" ] || die "prebuilt web assets missing at $repo/$assetdir — run 'make ensure-webassets' in the clone first"

    hlog "cross-building teleport/tctl/tbot/tsh (${variant}) from $(basename "$repo") @ $sha (first time; cached after)"
    ( cd "$repo" || exit 1
      [ -x "$bincache/teleport" ] || GOOS=linux GOARCH=amd64 CGO_ENABLED=1 CC="$HARNESS_CC" \
        go build -buildvcs=false -tags "$tags" -ldflags "-s -w" -o "$bincache/teleport" "$target"
      for tool in tctl tbot tsh; do
        [ -x "$bincache/$tool" ] || GOOS=linux GOARCH=amd64 CGO_ENABLED=1 CC="$HARNESS_CC" \
          go build -buildvcs=false -tags grpcnotrace -ldflags "-s -w" -o "$bincache/$tool" "./tool/$tool"
      done )
    hok "binaries -> $bincache"
  else
    hlog "binaries cached: $bincache"
  fi

  # Base image: tiny glibc runtime + the three binaries + CLI tools the scripts need.
  cat > "$bincache/Dockerfile" <<'EOF'
FROM --platform=linux/amd64 debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl openssl bash \
 && rm -rf /var/lib/apt/lists/*
COPY teleport /usr/local/bin/teleport
COPY tctl /usr/local/bin/tctl
COPY tbot /usr/local/bin/tbot
COPY tsh /usr/local/bin/tsh
RUN teleport version && tctl version && tbot version && tsh version --client
EOF
  hlog "building base image $image"
  DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t "$image" "$bincache" >/dev/null
  hok "image $image"
  echo "$image"
}
