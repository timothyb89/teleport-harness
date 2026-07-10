# shellcheck shell=bash
# Report bundles.
#
# A report captures everything needed to understand a test run AND leaves the
# cluster running for hands-on inspection: results, per-service logs, the rendered
# compose+configs, cluster metadata, and the web URL + admin login.

# cluster_report <id> [results-file]
cluster_report() {
  load_target
  local id="${1:?usage: report <id>}"; local results="${2:-}"
  local out; out="$(state_dir_for "$id")"; [ -d "$out" ] || die "no such cluster: $id"
  local fqdn port; fqdn="$(cluster_meta "$id" FQDN)"; port="$(cluster_meta "$id" PORT)"

  local ts bundle; ts="$(date -u +%Y%m%d-%H%M%S)"; bundle="$RUNS_DIR/${ts}-${id}"
  mkdir -p "$bundle/logs" "$bundle/rendered"

  # metadata + rendered artifacts (compose, configs, and the composed bootstrap so the
  # report can summarize + link the exact resources that were created)
  cp "$out/meta.env" "$bundle/meta.env" 2>/dev/null || true
  cp "$out/docker-compose.yml" "$bundle/rendered/" 2>/dev/null || true
  cp -R "$out/config" "$bundle/rendered/" 2>/dev/null || true
  cp -R "$out/bootstrap" "$bundle/rendered/" 2>/dev/null || true
  # structured verification results (per-module, written by the Python verifier)
  cp "$out"/results-*.json "$bundle/" 2>/dev/null || true
  # raw console output (the run-plan verification log), kept for reference
  [ -n "$results" ] && [ -f "$results" ] && cp "$results" "$bundle/console.txt" 2>/dev/null || true

  # per-service logs
  local svc
  for svc in $(compose "teleport-harness-$id" "$out/docker-compose.yml" ps -a --format '{{.Service}}' 2>/dev/null); do
    compose "teleport-harness-$id" "$out/docker-compose.yml" logs --no-color "$svc" > "$bundle/logs/${svc}.log" 2>&1 || true
  done

  # results.md — rich markdown built by the brain from the structured run data
  # (cluster setup + node inventory + per-check evidence). Falls back to a stub.
  if ! pybrain report-md --state-dir "$out" > "$bundle/results.md" 2>/dev/null; then
    {
      echo "# Test run: ${id}"
      echo "- repo: $(cluster_meta "$id" REPO) @ $(cluster_meta "$id" SHA)"
      echo "- web UI: https://${fqdn}:${port}"
      echo '## Results'; echo '```'
      [ -n "$results" ] && [ -f "$results" ] && cat "$results" || echo "(no results)"
      echo '```'
    } > "$bundle/results.md"
  fi

  hok "report bundle: $bundle"
  echo "$bundle"
}
