# shellcheck shell=bash
# Pinned client tools the k8s backend needs (crane, buildctl). A Beams microVM has no
# package manager we can count on, so rather than make these a manual prerequisite, the
# harness downloads a pinned release into .cache/tools/ and checks it against a sha256
# recorded HERE (not one fetched from the same release page, which would prove nothing).
# A copy already on PATH wins only when it is the pinned version, so a stray newer or older
# install can't quietly change behavior.
#
# To bump: change the version, then replace all four hashes (crane: the release's
# checksums.txt; buildkit: `shasum -a 256` of each tarball). buildctl should track the
# buildkitd image in infra/k8s/manifests/harness-30-buildkitd.yaml.

CRANE_VERSION=v0.22.1
BUILDCTL_VERSION=v0.34.0

# _tool_platform -> darwin-arm64 | darwin-amd64 | linux-amd64 | linux-arm64
_tool_platform() {
  local os arch
  case "$(uname -s)" in Darwin) os=darwin ;; Linux) os=linux ;; *) die "unsupported OS $(uname -s) for pinned tools" ;; esac
  case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; x86_64|amd64) arch=amd64 ;; *) die "unsupported arch $(uname -m) for pinned tools" ;; esac
  echo "$os-$arch"
}

# _tool_release <name> <platform> -> "<url> <sha256> <member path in the tarball>"
_tool_release() {
  local name="$1" plat="$2" gcr
  case "$name" in
    crane)
      case "$plat" in
        darwin-arm64) gcr=Darwin_arm64;  set -- 2231fc8df8806d20d680ff1225db44e095a55dd6ac1ae8eced4faf4b278b78fb ;;
        darwin-amd64) gcr=Darwin_x86_64; set -- 6fedd06a648c11335f0e8b9547e4783002c30e9336047fec292942cd518bb799 ;;
        linux-amd64)  gcr=Linux_x86_64;  set -- 0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0 ;;
        linux-arm64)  gcr=Linux_arm64;   set -- 898c0cff975f898a33e8c4580bdafb0e7c02c7faa33374e946762f97c4ab7110 ;;
      esac
      echo "https://github.com/google/go-containerregistry/releases/download/$CRANE_VERSION/go-containerregistry_$gcr.tar.gz $1 crane" ;;
    buildctl)
      case "$plat" in
        darwin-arm64) set -- 3eee1b54b362af2e1e4e9ad8314bf2751e9447ecbd1455eb9207af0c3a40baf2 ;;
        darwin-amd64) set -- 0a11beb091549a04aa34066800e05fa44d5e4c95a24ff5a066fbfc639585a91d ;;
        linux-amd64)  set -- 14813b1fb2a40082114e6d386c6fc5e7dd5298176ce7ee1a18bc33e73e109ac3 ;;
        linux-arm64)  set -- 0619ca3e3205a32ea2beb115e0242136e4a0716cc1917ba0ceaa4ef4d52382c9 ;;
      esac
      echo "https://github.com/moby/buildkit/releases/download/$BUILDCTL_VERSION/buildkit-$BUILDCTL_VERSION.$plat.tar.gz $1 bin/buildctl" ;;
    *) die "no pinned release for tool '$name'" ;;
  esac
}

_tool_version_ok() {
  local name="$1" bin="$2"
  case "$name" in
    crane)    [ "$("$bin" version 2>/dev/null)" = "${CRANE_VERSION#v}" ] ;;
    buildctl) "$bin" --version 2>/dev/null | grep -q " $BUILDCTL_VERSION " ;;
  esac
}

# tool_path <crane|buildctl> -> prints an executable path, downloading the pinned release
# on first use.
tool_path() {
  local name="$1" plat url sha member dir tmp got onpath
  onpath="$(command -v "$name" 2>/dev/null || true)"
  if [ -n "$onpath" ] && _tool_version_ok "$name" "$onpath"; then echo "$onpath"; return 0; fi

  plat="$(_tool_platform)"
  local ver="$BUILDCTL_VERSION"; [ "$name" = crane ] && ver="$CRANE_VERSION"
  dir="$CACHE_DIR/tools/$name-$ver-$plat"
  if [ -x "$dir/$name" ]; then echo "$dir/$name"; return 0; fi

  read -r url sha member <<<"$(_tool_release "$name" "$plat")"
  hlog "fetching $name ($plat) -> $dir"
  require_cmd curl tar shasum
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/release.tgz" "$url" || { rm -rf "$tmp"; die "download failed: $url"; }
  got="$(shasum -a 256 "$tmp/release.tgz" | cut -d' ' -f1)"
  if [ "$got" != "$sha" ]; then
    rm -rf "$tmp"
    die "$name release checksum mismatch: got $got, pinned $sha ($url)"
  fi
  tar xzf "$tmp/release.tgz" -C "$tmp" "$member" || { rm -rf "$tmp"; die "$url has no $member"; }
  mkdir -p "$dir"
  install -m 0755 "$tmp/$member" "$dir/$name"
  rm -rf "$tmp"
  echo "$dir/$name"
}
