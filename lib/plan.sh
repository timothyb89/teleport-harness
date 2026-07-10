# shellcheck shell=bash
# Plan runner. `run-plan <name>` resolves <name> to either a multi-module plan file
# (plans/<name>.yaml — several modules composed into ONE cluster) or a single module.
# Both gate on the target's features/version, bring the cluster up (or reuse one),
# verify each module, and produce a report.

run_plan() {
  load_target
  local name="${1:?usage: run-plan <plan|module> --repo <clone> [--features a,b] [--version vNN] [--id <id>]}"
  if [ -f "$HARNESS_ROOT/plans/${name}.yaml" ]; then run_plan_multi "$name"
  elif [ -d "$MODULES_DIR/$name" ]; then run_plan_single "$name"
  else die "unknown plan or module '$name' (plans/ or modules/)"; fi
}

# run_plan_single <module> — one module == its own cluster (the original path).
run_plan_single() {
  local module="${1:?}"

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
  local res rc=1; res="$(mktemp)"
  sleep 8
  for _ in $(seq 1 8); do
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

# run_plan_multi <plan> — several modules composed into ONE cluster (shared auth +
# shared components). Each module is gated independently: gated-out modules are
# reported SKIP and left out of the compose; the rest are verified together.
run_plan_multi() {
  local plan="${1:?}"
  : "${REPO:?--repo <teleport-clone> required}"

  # ---- gate each module (the brain decides run vs skip) ----
  local gate_args=() resolved rc=0
  if [ -n "${FEATURES:-}" ]; then gate_args+=(--features "$FEATURES")
  else hwarn "no --features given; assuming target provides required features"; fi
  [ -n "${VERSION:-}" ] && gate_args+=(--version "$VERSION")
  if [ "${#gate_args[@]}" -gt 0 ]; then resolved="$(pybrain plan-resolve "$plan" "${gate_args[@]}")" || rc=$?
  else resolved="$(pybrain plan-resolve "$plan")" || rc=$?; fi
  [ "$rc" = 0 ] || die "plan-resolve '$plan' failed: $resolved"

  local l
  while IFS= read -r l; do [ -n "$l" ] && hwarn "$l"; done \
    < <(echo "$resolved" | jq -r '.skip[]? | "SKIP \(.module): \(.reason)"')

  local run_csv run_mods
  run_csv="$(echo "$resolved" | jq -r '.run | join(",")')"
  if [ -z "$run_csv" ]; then
    hwarn "SKIP plan '$plan' — all modules gated out"
    echo "SKIP $plan: all modules gated out"; return 0
  fi
  run_mods="${run_csv//,/ }"
  hlog "plan '$plan' running modules: $run_csv"

  # ---- bring up ONE composed cluster (or reuse an existing --id) ----
  export ID="${ID:-$(gen_id)}"
  if [ -d "$(state_dir_for "$ID")" ]; then hlog "reusing existing cluster '$ID'"
  else cluster_up_modules "$plan" "$run_csv"; fi

  # ---- verify every module against the shared cluster; retry until all pass ----
  hlog "waiting for plan '$plan' checks to pass on cluster '$ID'"
  local res m; res="$(mktemp)"; rc=1
  sleep 8
  for _ in $(seq 1 8); do
    : > "$res"; rc=0
    for m in $run_mods; do
      echo "### module: $m" >> "$res"
      run_verification "$ID" "$m" >> "$res" 2>&1 || rc=1
    done
    [ "$rc" = 0 ] && break
    sleep 8
  done
  cat "$res"

  local bundle; bundle="$(cluster_report "$ID" "$res")"
  rm -f "$res"
  echo
  if [ "$rc" = 0 ]; then hok "PLAN '$plan' PASSED — report: $bundle (cluster '$ID' left up)"
  else herr "PLAN '$plan' FAILED — report: $bundle (cluster '$ID' left up for inspection)"; fi
  return "$rc"
}
