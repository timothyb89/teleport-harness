# Plan runner: gate a module's test cases on the target's features/version, bring
# the cluster up (or reuse one), verify, and produce a report. shellcheck shell=bash
#
# Iteration 1: a "plan" is a single module (the unit of test cases). Multi-module
# plan files are a thin future wrapper over this.

module_meta() {
  local m="$1" field="$2" f="$MODULES_DIR/$1/module.yaml"
  case "$field" in
    requires_features) grep -E '^requires_features:' "$f" | sed 's/.*\[//; s/\].*//; s/,/ /g' ;;
    provides_feature)  grep -E '^provides_feature:' "$f" | awk '{print $2}' ;;
    min_version)       grep -E '^min_version:' "$f" | awk '{print $2}' ;;
  esac
}
_vnum() { local v="${1#v}"; echo "${v%%.*}"; }   # v18 / v18.2.1 -> 18

run_plan() {
  load_target
  local module="${1:?usage: run-plan <module> --repo <clone> [--features a,b] [--version vNN] [--id <id>]}"
  [ -d "$MODULES_DIR/$module" ] || die "unknown module '$module'"

  # ---- feature/version gating (no silent skips) ----
  local reqf minv skip="" feat
  reqf="$(module_meta "$module" requires_features)"
  minv="$(module_meta "$module" min_version)"
  if [ -n "${FEATURES:-}" ]; then
    local have=",${FEATURES},"
    for feat in $reqf; do case "$have" in *",$feat,"*) : ;; *) skip="target lacks feature '$feat'";; esac; done
  else
    hwarn "no --features given; assuming target provides required features: ${reqf:-none}"
  fi
  if [ -z "$skip" ] && [ -n "${VERSION:-}" ] && [ -n "$minv" ]; then
    if [ "$(_vnum "$VERSION")" -lt "$(_vnum "$minv")" ] 2>/dev/null; then
      skip="target version $VERSION < module min_version $minv"
    fi
  fi
  if [ -n "$skip" ]; then
    hwarn "SKIP '$module' — $skip"
    echo "SKIP $module: $skip"
    return 0
  fi

  # ---- bring up (or reuse an existing cluster with the same --id) ----
  : "${REPO:?--repo <teleport-clone> required}"
  export ID="${ID:-$(gen_id)}"
  if [ -d "$(state_dir_for "$ID")" ]; then hlog "reusing existing cluster '$ID'"; else cluster_up "$module"; fi

  # ---- settle, then verify (module-agnostic: retry until checks pass or timeout) ----
  # Agents/bots take time to join and negatives take a beat to log their denial, and
  # each module expects different things — so instead of guessing counts, just re-run
  # the module's own checks a few times until they pass.
  hlog "waiting for '$module' checks to pass on cluster '$ID'"
  local res rc=1 attempt; res="$(mktemp)"
  sleep 8
  for attempt in $(seq 1 8); do
    if run_verification "$ID" "$module" > "$res" 2>&1; then rc=0; break; fi
    rc=1; sleep 8
  done
  cat "$res"

  local bundle; bundle="$(cluster_report "$ID" "$res")"
  rm -f "$res"
  echo
  if [ "$rc" = 0 ]; then hok "PLAN PASSED — report: $bundle (cluster '$ID' left up)"
  else herr "PLAN FAILED — report: $bundle (cluster '$ID' left up for inspection)"; fi
  return "$rc"
}
