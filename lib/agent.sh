# shellcheck shell=bash
# Agent-driven test runner (host step). For a module that declares an `agent:` block in its
# render.yaml, drive its `workbench` container with a locked-down `claude -p` (subscription auth,
# on the host — no API key) via the Python brain (harness agent-run -> harness/agent.py). The
# agent's ONLY capability is a single MCP tool that execs inside that one container.
#
# Called by lib/plan.sh AFTER the cluster is up + healthy and BEFORE verification, so the agent's
# /out/agent-result.json exists when the `agent_result` verb reads it. Deliberately tolerant: any
# failure here is logged, never fatal — the module's `agent_result` check surfaces "no result" in
# the report, and the OBJECTIVE checks (bot_joined/resource_present) still gate the run.
#
# Convention: an agent module names its runner service `workbench` (container `<id>-workbench`).

# run_agents <cluster-id> <module>
run_agents() {
  local id="$1" module="$2" sdir wb up=""
  local rv="$MODULES_DIR/$module/render.yaml"
  [ -f "$rv" ] || return 0
  grep -qE '^[[:space:]]*agent:' "$rv" || return 0   # not an agent module — no-op
  if ! command -v claude >/dev/null 2>&1; then
    hwarn "agent module '$module' but 'claude' is not on PATH — skipping (agent_result will FAIL)"
    return 0
  fi

  wb="${id}-workbench"
  hlog "agent '$module': waiting for workbench '$wb' to start"
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Running}}' "$wb" 2>/dev/null)" = "true" ] && { up=1; break; }
    sleep 2
  done
  [ -n "$up" ] || { hwarn "workbench '$wb' never started — skipping agent run for '$module'"; return 0; }

  sdir="$(state_dir_for "$id")"
  hlog "agent '$module': driving claude against the workbench (subscription login)"
  pybrain agent-run "$module" --cluster-id "$id" --state-dir "$sdir" --repo "${REPO:-}" \
    || hwarn "agent-run for '$module' reported a problem (see the report's agent_result check)"
}
