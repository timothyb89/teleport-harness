# shellcheck shell=bash
# Share a report bundle as a GitHub gist.
#
# The bundle's results.md is already self-contained (proofs inlined). `share` flattens
# the bundle (gists are flat, no dirs), rewrites results.md's relative links to gist
# anchors, and pushes everything with `gh gist create`. Secret by default; --public opts in.
#
# Ephemeral environments (Beams) typically have no authenticated `gh` at all, so `share`
# has two indirect modes built on the SAME staging step, for a client that does have `gh`
# (a laptop) to finish the upload:
#   --export [path]   stage + tar.gz the gist files to `path` (default: beside the bundle)
#                      and print the scp + `gh gist create` one-liners to run on that client.
#   share <tar.gz>     the other end of --export: given a staged export (not a bundle dir or
#                      cluster id), skip staging and upload it directly — this is the command
#                      the printed one-liner invokes, so there's nothing new to remember.

# cluster_share <run-bundle-dir | cluster-id | exported-tar.gz> [--public] [--export [path]]
cluster_share() {
  load_target
  local arg="${1:-}"
  [ -n "$arg" ] || die "usage: share <run-bundle-dir | cluster-id | exported-tar.gz> [--public] [--export [path]]"
  shift || true
  local public="" export_path="" export_mode=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --public) public="--public" ;;
      --export)
        export_mode=1
        # optional path: only consume the next arg if it isn't itself a flag
        if [ $# -gt 1 ] && [ "${2#--}" = "$2" ]; then export_path="$2"; shift; fi
        ;;
      *) die "unknown share option: $1" ;;
    esac
    shift
  done

  # Mode 1: the argument IS a previously-exported tar.gz (produced by --export elsewhere,
  # usually scp'd back here) — unpack it and push straight to gh, no bundle/staging involved.
  case "$arg" in
    *.tar.gz|*.tgz)
      [ -f "$arg" ] || die "no such export file: $arg"
      require_cmd gh
      gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run: gh auth login"
      local tmp; tmp="$(mktemp -d)"
      tar -xzf "$arg" -C "$tmp"
      [ -f "$tmp/meta.txt" ] || die "not a share export (missing meta.txt): $arg"
      local desc; desc="$(cat "$tmp/meta.txt")"
      local file_args=()
      while IFS= read -r -d '' f; do file_args+=("$f"); done < <(find "$tmp/files" -type f -print0)
      [ "${#file_args[@]}" -gt 0 ] || die "nothing to share in $arg"
      [ -n "$public" ] && hwarn "public gist: rendered configs/logs may contain (disposable) join secrets — review first"
      hlog "pushing $(( ${#file_args[@]} )) file(s) from $arg to a ${public:+public }gist"
      local url
      url="$(gh gist create ${public:+"$public"} --desc "$desc" "${file_args[@]}")" \
        || die "gh gist create failed"
      rm -rf "$tmp"
      hok "shared: $url"
      echo "$url"
      return 0
      ;;
  esac

  # Resolve the bundle: an explicit bundle dir, or a cluster id → make a fresh report.
  local bundle
  if [ -d "$arg" ]; then
    bundle="$arg"
  elif [ -d "$(state_dir_for "$arg")" ]; then
    hlog "no bundle path given — generating a fresh report for cluster '$arg'"
    bundle="$(cluster_report "$arg")"
  else
    die "no such bundle dir, cluster, or export file: $arg"
  fi
  [ -f "$bundle/results.md" ] || die "no results.md in $bundle"

  local stage; stage="$(mktemp -d)"
  local files; files="$(pybrain gist-stage --bundle "$bundle" --out "$stage")" \
    || die "failed to stage bundle for gist"

  local file_args=()
  while IFS= read -r f; do [ -n "$f" ] && file_args+=("$stage/$f"); done <<<"$files"
  [ "${#file_args[@]}" -gt 0 ] || die "nothing staged to share"

  local desc; desc="teleport-harness report — $(basename "$bundle")"

  # Mode 2: --export — package the staged files for a client that has `gh` authenticated,
  # instead of pushing from here.
  if [ -n "$export_mode" ]; then
    [ -n "$export_path" ] || export_path="$(dirname "$bundle")/$(basename "$bundle").share.tar.gz"
    local pkg; pkg="$(mktemp -d)"
    mkdir -p "$pkg/files"
    for f in "${file_args[@]}"; do cp "$f" "$pkg/files/"; done
    echo "$desc" >"$pkg/meta.txt"
    mkdir -p "$(dirname "$export_path")"
    tar -czf "$export_path" -C "$pkg" meta.txt files
    rm -rf "$pkg" "$stage"
    hok "exported: $export_path"
    echo
    echo "Copy it to a client with gh authenticated, then upload it from there:"
    echo "  scp \"$(hostname -f 2>/dev/null || hostname)\":\"$(readlink -f "$export_path")\" ."
    echo "  cluster share \"$(basename "$export_path")\"$( [ -n "$public" ] && echo " --public" )"
    echo "$export_path"
    return 0
  fi

  require_cmd gh
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run: gh auth login (or use --export)"
  [ -n "$public" ] && hwarn "public gist: rendered configs/logs may contain (disposable) join secrets — review first"
  hlog "pushing $(( ${#file_args[@]} )) file(s) to a ${public:+public }gist"
  local url
  url="$(gh gist create ${public:+"$public"} --desc "$desc" "${file_args[@]}")" \
    || die "gh gist create failed"
  rm -rf "$stage"
  hok "shared: $url"
  echo "$url"
}
