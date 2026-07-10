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

  # metadata + rendered artifacts
  cp "$out/meta.env" "$bundle/meta.env" 2>/dev/null || true
  cp "$out/docker-compose.yml" "$bundle/rendered/" 2>/dev/null || true
  cp -R "$out/config" "$bundle/rendered/" 2>/dev/null || true

  # per-service logs
  local svc
  for svc in $(compose "teleport-harness-$id" "$out/docker-compose.yml" ps -a --format '{{.Service}}' 2>/dev/null); do
    compose "teleport-harness-$id" "$out/docker-compose.yml" logs --no-color "$svc" > "$bundle/logs/${svc}.log" 2>&1 || true
  done

  # results.md
  {
    echo "# Test run: ${id}"
    echo
    echo "- module:  $(cluster_meta "$id" MODULE)"
    echo "- repo:    $(cluster_meta "$id" REPO) @ $(cluster_meta "$id" SHA)"
    echo "- created: $(cluster_meta "$id" CREATED)"
    echo "- web UI:  https://${fqdn}:${port}  (publicly-trusted LE cert; \`cluster web ${id}\` for admin login)"
    echo
    echo '## Results'
    echo '```'
    if [ -n "$results" ] && [ -f "$results" ]; then cat "$results"; else echo "(no results file provided)"; fi
    echo '```'
    echo
    echo "## Inspect"
    echo "- live cluster: \`cluster logs ${id} [service]\`, or open the web UI above"
    echo "- rendered compose/config: rendered/"
    echo "- per-service logs: logs/"
    echo "- teardown when done: \`cluster teardown ${id}\`"
  } > "$bundle/results.md"

  hok "report bundle: $bundle"
  echo "$bundle"
}
