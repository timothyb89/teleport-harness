# shellcheck shell=bash
# Share a report bundle as a GitHub gist.
#
# The bundle's results.md is already self-contained (proofs inlined). `share` flattens
# the bundle (gists are flat, no dirs), rewrites results.md's relative links to gist
# anchors, and pushes everything with `gh gist create`. Secret by default; --public opts in.

# cluster_share <run-bundle-dir | cluster-id> [--public]
cluster_share() {
  load_target
  local arg="${1:-}"; [ -n "$arg" ] || die "usage: share <run-bundle-dir | cluster-id> [--public]"
  shift || true
  local public=""
  for a in "$@"; do [ "$a" = "--public" ] && public="--public"; done

  require_cmd gh
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run: gh auth login"

  # Resolve the bundle: an explicit bundle dir, or a cluster id → make a fresh report.
  local bundle
  if [ -d "$arg" ]; then
    bundle="$arg"
  elif [ -d "$(state_dir_for "$arg")" ]; then
    hlog "no bundle path given — generating a fresh report for cluster '$arg'"
    bundle="$(cluster_report "$arg")"
  else
    die "no such bundle dir or cluster: $arg"
  fi
  [ -f "$bundle/results.md" ] || die "no results.md in $bundle"

  local stage; stage="$(mktemp -d)"
  local files; files="$(pybrain gist-stage --bundle "$bundle" --out "$stage")" \
    || die "failed to stage bundle for gist"

  local file_args=()
  while IFS= read -r f; do [ -n "$f" ] && file_args+=("$stage/$f"); done <<<"$files"
  [ "${#file_args[@]}" -gt 0 ] || die "nothing staged to share"

  local desc; desc="teleport-harness report — $(basename "$bundle")"
  [ -n "$public" ] && hwarn "public gist: rendered configs/logs may contain (disposable) join secrets — review first"
  hlog "pushing $(( ${#file_args[@]} )) file(s) to a ${public:+public }gist"
  local url
  url="$(gh gist create ${public:+"$public"} --desc "$desc" "${file_args[@]}")" \
    || die "gh gist create failed"
  rm -rf "$stage"
  hok "shared: $url"
  echo "$url"
}
