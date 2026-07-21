#!/usr/bin/env bash
# agent-runner prebuild: the workbench writes /out/agent-result.json to a bind mount onto
# $OUT/agent/out. Create it host-side first so it's owned by the invoking user (otherwise
# docker creates the bind-source as root at `compose up`, and the host `agent_result` verb
# may not be able to read it back). Not SHA-cached — cheap, idempotent.
set -euo pipefail
: "${OUT:?agent-runner prebuild needs \$OUT (the state dir)}"
mkdir -p "$OUT/agent/out"
