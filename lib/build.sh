# Resolve where the teleport binaries come from, and bake a base image out of them.
#
# THREE mutually-exclusive sources, all converging on the same two artifacts — a cache dir
# of linux/amd64 binaries (teleport/tctl/tbot/tsh) and the image
# `teleport-harness:<key>-<variant>` every cluster runs:
#
#   --repo <clone>      cross-build from the clone's CURRENTLY CHECKED OUT tree (git SHA key)
#   --package <tar.gz>  a released teleport-*-bin.tar.gz, unpacked (tarball sha256 key)
#   --binary <path>     prebuilt binaries already on disk (their combined sha256 key)
#
# Everything downstream (render, compose, verify, report) only ever sees the image, so a
# package/binary run is identical to a source run — except that nothing can be BUILT from it,
# which is why modules/components that need the clone declare `requires_repo: true` in their
# render.yaml and gate out (see harness/models.py repo_requirement).
# shellcheck shell=bash

: "${HARNESS_CC:=x86_64-unknown-linux-gnu-gcc}"   # messense glibc cross toolchain

HARNESS_BINS="teleport tctl tbot tsh"   # every source must supply all four

# ---- source resolution -------------------------------------------------------
# resolve_source — validate the source flags and export how the rest of the harness
# refers to the source:
#   SOURCE_KIND     repo | package | binary
#   SOURCE_REF      absolute path (clone dir | tarball | dir holding the binaries)
#   SOURCE_KEY      cache key: <git-sha12> | pkg<sha12> | bin<sha12>
#   SOURCE_LABEL    one-line human description (logs, meta.env, the report)
#   SOURCE_VERSION  vX.Y.Z when the source knows it — defaults --version for gating
#   SOURCE_VARIANT  oss | ent   ·   BIN_CACHE   ·   IMAGE_TAG
# Idempotent: resolving twice in one process (run_plan then cluster_up) is a no-op.
resolve_source() {
  [ -n "${SOURCE_KIND:-}" ] && return 0

  local n=0
  [ -n "${REPO:-}" ]    && n=$((n+1))
  [ -n "${PACKAGE:-}" ] && n=$((n+1))
  [ -n "${BINARY:-}" ]  && n=$((n+1))
  [ "$n" = 1 ] || die "need exactly one source: --repo <clone> | --package <tar.gz> | --binary <path> (got $n)"

  export SOURCE_VERSION=""
  if   [ -n "${REPO:-}" ];    then _resolve_source_repo
  elif [ -n "${PACKAGE:-}" ]; then _resolve_source_package
  else                             _resolve_source_binary
  fi

  # Enterprise-ness may have been discovered by the resolver above (an -ent package /
  # an "Enterprise" binary), so the variant is decided only once that has happened.
  if [ "${ENT:-0}" = 1 ]; then SOURCE_VARIANT=ent; else SOURCE_VARIANT=oss; fi
  export SOURCE_VARIANT
  export BIN_CACHE="$CACHE_DIR/bin/${SOURCE_KEY}-${SOURCE_VARIANT}"
  export IMAGE_TAG="teleport-harness:${SOURCE_KEY}-${SOURCE_VARIANT}"

  # A previous build of this exact artifact recorded what `teleport version` says. Read it
  # back for a package/binary only: for a clone this would make gating flip between the
  # first run of a command (nothing cached, no auto --version) and the second, which is a
  # far worse surprise than just passing --version as clone runs always have.
  if [ -z "$SOURCE_VERSION" ] && [ "$SOURCE_KIND" != repo ] && [ -s "$BIN_CACHE/VERSION" ]; then
    SOURCE_VERSION="$(cat "$BIN_CACHE/VERSION")"
  fi
  return 0
}

_resolve_source_repo() {
  local repo; repo="$(cd "$REPO" && pwd)"
  # Ask git rather than testing for a .git DIRECTORY: in a `git worktree` .git is a
  # FILE pointing at the main clone, and a worktree is exactly how you build an
  # arbitrary commit without disturbing the clone's checkout (everything else here
  # already works in one — rev-parse resolves, and go build passes -buildvcs=false).
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || die "not a git repo: $repo"
  export REPO="$repo" SOURCE_KIND=repo SOURCE_REF="$repo"
  export SOURCE_KEY; SOURCE_KEY="$(git -C "$repo" rev-parse --short=12 HEAD)"
  SOURCE_LABEL="repo $(basename "$repo") @ $SOURCE_KEY"; export SOURCE_LABEL
  # No version probe: knowing a clone's version means building it first, and --version
  # has always been explicit for clones. Packages/binaries can answer for free.
  return 0
}

_resolve_source_package() {
  local pkg root sha
  [ -f "$PACKAGE" ] || die "no such package: $PACKAGE"
  pkg="$(cd "$(dirname "$PACKAGE")" && pwd)/$(basename "$PACKAGE")"
  case "$pkg" in
    *.tar.gz|*.tgz) ;;
    *) die "--package expects a teleport *-bin.tar.gz release tarball (got $(basename "$pkg")) — unpack a .deb/.rpm/.pkg yourself and pass --binary <dir>" ;;
  esac

  # Archive root: 'teleport/' (community) or 'teleport-ent/' (enterprise). `|| true`
  # because head closes the pipe early and pipefail reads the SIGPIPEd tar as a failure.
  root="$( { tar tzf "$pkg" || true; } | head -1 | cut -d/ -f1 )"
  [ -n "$root" ] || die "cannot read $pkg as a gzipped tar archive"
  case "$root" in
    *-ent)
      if [ "${ENT:-0}" != 1 ]; then
        hwarn "package is an ENTERPRISE build (${root}/) — enabling ent; auth needs a license (set HARNESS_LICENSE_FILE)"
        export ENT=1
      fi ;;
    *)
      [ "${ENT:-0}" = 1 ] && hwarn "--ent given but $(basename "$pkg") unpacks to ${root}/ (a community build)" || true ;;
  esac

  sha="$(shasum -a 256 "$pkg" | cut -c1-12)"
  export SOURCE_KIND=package SOURCE_REF="$pkg" PACKAGE_ROOT="$root"
  export SOURCE_KEY="pkg$sha"
  SOURCE_VERSION="$(_pkg_version "$pkg" "$root")"
  SOURCE_LABEL="package $(basename "$pkg")${SOURCE_VERSION:+ ($SOURCE_VERSION)}"; export SOURCE_LABEL
  return 0
}

_resolve_source_binary() {
  local dir b sha ver
  [ -e "$BINARY" ] || die "no such path: $BINARY"
  if [ -d "$BINARY" ]; then dir="$(cd "$BINARY" && pwd)"
  else dir="$(cd "$(dirname "$BINARY")" && pwd)"; fi
  # The harness drives tctl/tbot/tsh as much as teleport itself (bootstrap, bots, admin
  # access), and a mismatched set would fail much later and much less clearly.
  for b in $HARNESS_BINS; do
    [ -f "$dir/$b" ] || die "--binary needs teleport, tctl, tbot and tsh in one directory ('$b' missing from $dir) — a release *-bin.tar.gz has all four (use --package)"
  done

  sha="$( cat "$dir/teleport" "$dir/tctl" "$dir/tbot" "$dir/tsh" | shasum -a 256 | cut -c1-12 )"
  export SOURCE_KIND=binary SOURCE_REF="$dir"
  export SOURCE_KEY="bin$sha"
  # A loose binary carries no manifest, so ask it. Cheap (the runtime base image is
  # local by then) and it makes --version gating work the same as for a package.
  ver="$(_probe_version "$dir")"
  case "$ver" in
    *Enterprise*)
      if [ "${ENT:-0}" != 1 ]; then
        hwarn "binary is an ENTERPRISE build — enabling ent; auth needs a license (set HARNESS_LICENSE_FILE)"
        export ENT=1
      fi ;;
  esac
  SOURCE_VERSION="$(_version_from "$ver")"
  export SOURCE_LABEL="binary $dir/teleport${SOURCE_VERSION:+ ($SOURCE_VERSION)}"
  return 0
}

# _pkg_version <tarball> <root>  — the VERSION file every release tarball ships.
_pkg_version() {
  local v
  v="$( { tar xzOf "$1" "$2/VERSION" 2>/dev/null || true; } | tr -d '[:space:]' )"
  [ -n "$v" ] || return 0
  case "$v" in v*) echo "$v" ;; *) echo "v$v" ;; esac
}

# _probe_version <dir>  — first line of `teleport version`, run under linux/amd64.
_probe_version() {
  { docker run --rm --platform linux/amd64 -v "$1:/src:ro" --entrypoint /src/teleport \
      debian:bookworm-slim version 2>/dev/null || true; } | head -1
}

# _version_from <version-line>  ->  vX.Y.Z[-suffix]
_version_from() {
  { echo "$1" | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*' || true; } | head -1
}

# ---- image build -------------------------------------------------------------
# build_image  -> prints the resulting image tag on stdout. Reads the resolved source
# (call resolve_source first, or let this do it). Repeat builds are a docker inspect.
build_image() {
  resolve_source
  local image="$IMAGE_TAG" bincache="$BIN_CACHE" b need=0

  if [ "${REBUILD_IMAGE:-0}" != 1 ] && docker image inspect "$image" >/dev/null 2>&1; then
    hlog "image cached: $image ($SOURCE_LABEL)"
    echo "$image"; return 0
  fi

  mkdir -p "$bincache"
  for b in $HARNESS_BINS; do [ -x "$bincache/$b" ] || need=1; done
  if [ "$need" = 1 ]; then
    case "$SOURCE_KIND" in
      repo)    _stage_bins_repo "$bincache" ;;
      package) _stage_bins_package "$bincache" ;;
      binary)  _stage_bins_binary "$bincache" ;;
    esac
    for b in $HARNESS_BINS; do chmod +x "$bincache/$b"; done
    hok "binaries -> $bincache"
  else
    hlog "binaries cached: $bincache"
  fi
  # Checked on every build, not just the staging one: a cache dir left behind by a bad
  # source would otherwise look "already staged" on the retry and fail as an inscrutable
  # exec format error three layers into docker build.
  for b in $HARNESS_BINS; do _assert_amd64 "$bincache/$b"; done
  if [ -n "${SOURCE_VERSION:-}" ]; then printf '%s\n' "$SOURCE_VERSION" > "$bincache/VERSION"; fi

  # Base image: tiny glibc runtime + the binaries + the CLI tools the scripts need.
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

  hlog "building base image $image ($SOURCE_LABEL)"
  DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t "$image" "$bincache" >/dev/null
  hok "image $image"
  _record_version "$bincache" "$image"
  echo "$image"
}

# Cross-build from the clone's working tree, reusing its prebuilt webassets.
_stage_bins_repo() {
  local bincache="$1" repo="$SOURCE_REF" target tags assetdir tool
  command -v "$HARNESS_CC" >/dev/null 2>&1 || die "cross compiler '$HARNESS_CC' not found (brew install messense/macos-cross-toolchains/x86_64-unknown-linux-gnu)"
  if [ "${ENT:-0}" = 1 ]; then target=./e/tool/teleport; tags="grpcnotrace webassets_embed webassets_ent"; assetdir=webassets/e/teleport/app
  else target=./tool/teleport; tags="grpcnotrace webassets_embed"; assetdir=webassets/teleport/app; fi
  [ -n "$(ls -A "$repo/$assetdir" 2>/dev/null)" ] || die "prebuilt web assets missing at $repo/$assetdir — run 'make ensure-webassets' in the clone first"

  hlog "cross-building teleport/tctl/tbot/tsh (${SOURCE_VARIANT}) from $(basename "$repo") @ $SOURCE_KEY (first time; cached after)"
  ( cd "$repo" || exit 1
    [ -x "$bincache/teleport" ] || GOOS=linux GOARCH=amd64 CGO_ENABLED=1 CC="$HARNESS_CC" \
      go build -buildvcs=false -tags "$tags" -ldflags "-s -w" -o "$bincache/teleport" "$target"
    for tool in tctl tbot tsh; do
      [ -x "$bincache/$tool" ] || GOOS=linux GOARCH=amd64 CGO_ENABLED=1 CC="$HARNESS_CC" \
        go build -buildvcs=false -tags grpcnotrace -ldflags "-s -w" -o "$bincache/$tool" "./tool/$tool"
    done )
}

# Unpack the binaries out of a release tarball (members are named explicitly, so the
# 200-odd MB of examples/docs in the archive never land on disk).
_stage_bins_package() {
  local bincache="$1" tmp b members=""
  hlog "unpacking $(basename "$SOURCE_REF") (${PACKAGE_ROOT}/) -> $bincache"
  tmp="$(mktemp -d)"
  for b in $HARNESS_BINS; do members="$members ${PACKAGE_ROOT}/$b"; done
  # shellcheck disable=SC2086  # $members is a deliberately word-split member list
  tar xzf "$SOURCE_REF" -C "$tmp" $members "${PACKAGE_ROOT}/fdpass-teleport" 2>/dev/null \
    || tar xzf "$SOURCE_REF" -C "$tmp" $members \
    || die "$(basename "$SOURCE_REF") does not contain ${PACKAGE_ROOT}/{teleport,tctl,tbot,tsh} — is this a *-bin.tar.gz release tarball?"
  for b in $HARNESS_BINS fdpass-teleport; do
    if [ -f "$tmp/${PACKAGE_ROOT}/$b" ]; then cp "$tmp/${PACKAGE_ROOT}/$b" "$bincache/$b"; fi
  done
  rm -rf "$tmp"
}

_stage_bins_binary() {
  local bincache="$1" b
  hlog "staging binaries from $SOURCE_REF -> $bincache"
  for b in $HARNESS_BINS; do cp "$SOURCE_REF/$b" "$bincache/$b"; done
  if [ -f "$SOURCE_REF/fdpass-teleport" ]; then cp "$SOURCE_REF/fdpass-teleport" "$bincache/fdpass-teleport"; fi
}

# Everything the harness runs is linux/amd64; catching a darwin/arm64 binary here beats
# a bare "exec format error" out of docker build three layers later.
_assert_amd64() {
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -c 'import sys
d = open(sys.argv[1], "rb").read(20)
sys.exit(0 if d[:4] == b"\x7fELF" and d[4] == 2 and d[18:20] == b"\x3e\x00" else 1)' "$1" \
    || die "$(basename "$1") is not a linux/amd64 ELF binary ($( { file -b "$1" 2>/dev/null || true; } | head -1)) — the harness runs linux/amd64 images"
}

# Record what the image's own `teleport version` reports, next to the binaries: it is
# what the report shows and what defaults --version on the next run of this source.
_record_version() {
  local bincache="$1" image="$2" ver
  [ -s "$bincache/VERSION" ] && return 0
  ver="$(_version_from "$( { docker run --rm --platform linux/amd64 "$image" teleport version 2>/dev/null || true; } | head -1 )")"
  if [ -n "$ver" ]; then
    printf '%s\n' "$ver" > "$bincache/VERSION"
    export SOURCE_VERSION="$ver"
  fi
  return 0
}
