# shellcheck shell=bash
# Plan runner: gate a module's test cases on the target's features/version, bring
# the cluster up (or reuse one), verify, and produce a report.
#
# Iteration 1: a "plan" is a single module (the unit of test cases). Multi-module
# plan files are a thin future wrapper over this.

run_plan() {
  load_target
  local module="${1:?usage: run-plan <module> --repo <clone> [--features a,b] [--version vNN] [--id <id>]}"
  [ -d "$MODULES_DIR/$module" ] || die "unknown module '$module'"

  # ---- feature/version gating (no silent skips) — decided by the Python brain ----
  local gate_args=() skip="" rc=0
  if [ -n "${FEATURES:-}" ]; then gate_args+=(--features "$FEATURES")
  else hwarn "no --features given; assuming target provides required features: $(pybrain meta "$module" requires_features)"; fi
  [ -n "${VERSION:-}" ] && gate_args+=(--version "$VERSION")
  # (guard the empty-array expansion — env bash here is 3.2, where "${a[@]}" under set -u throws)
  if [ "${#gate_args[@]}" -gt 0 ]; then skip="$(pybrain gate "$module" "${gate_args[@]}")" || rc=$?
  else skip="$(pybrain gate "$module")" || rc=$?; fi
  if [ "$rc" = 3 ]; then
    hwarn "SKIP '$module' — $skip"
    echo "SKIP $module: $skip"
    return 0
  elif [ "$rc" != 0 ]; then
    die "gating '$module' failed: $skip"
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
