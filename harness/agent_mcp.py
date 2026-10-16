"""The workbench MCP server: the agent's ONE and ONLY tool.

Launched as a stdio subprocess by `claude -p --mcp-config …` (see harness/agent.py). It exposes
a single tool, `run(cmd)`, which executes a shell command INSIDE one specific, disposable
container — the workbench — and nothing else. This is the containment boundary: the agent's
brain runs on the host, but its entire action space is "strings executed inside the workbench".

Where commands run is bound from the environment, NEVER from a tool argument: `WORKBENCH_EXEC`
is a JSON argv prefix (the cluster backend's exec-into-the-workbench command, from
Cluster.exec_argv — e.g. `["docker","exec","-w","/work","<id>-workbench"]`), and the model's
string only ever becomes the trailing `sh -lc <cmd>`. So the model cannot redirect the exec at
another container or the host. The workbench mounts no docker socket, so in-container root
cannot escape. Output is size-capped; each call has a wall-clock timeout.

Run standalone for a smoke test:
  WORKBENCH_EXEC='["docker","exec","-w","/work","<id>-workbench"]' python -m harness.agent_mcp
"""

from __future__ import annotations

import json
import os
import subprocess

from mcp.server.fastmcp import FastMCP



def _exec_prefix(raw: str) -> list[str]:
    """Parse WORKBENCH_EXEC; [] if unset or not a non-empty JSON list of strings."""
    try:
        prefix = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    if not (isinstance(prefix, list) and prefix and all(isinstance(a, str) for a in prefix)):
        return []
    return prefix


EXEC_PREFIX = _exec_prefix(os.environ.get("WORKBENCH_EXEC", ""))
TIMEOUT = int(os.environ.get("WORKBENCH_TIMEOUT", "120"))
MAX_OUTPUT = 60_000  # chars per stream, to keep the model's context bounded

mcp = FastMCP("workbench")


@mcp.tool()
def run(cmd: str) -> str:
    """Run a shell command inside the workbench container and return its output.

    The command runs via `sh -lc` with the working directory set to /work. Use this for
    everything: reading the mounted docs under /docs, running tctl/tbot, writing files under
    /work, and writing your final /out/agent-result.json. Long-running processes must be
    backgrounded (e.g. `some-daemon >/work/log 2>&1 &`) or they will hit the per-call timeout.
    Returns the exit code plus captured stdout and stderr.
    """
    if not EXEC_PREFIX:
        return "error: no workbench configured (WORKBENCH_EXEC unset or not a JSON argv list)"
    try:
        cp = subprocess.run(
            [*EXEC_PREFIX, "sh", "-lc", cmd],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {TIMEOUT}s (background long-running processes)"
    except FileNotFoundError:
        return f"error: {EXEC_PREFIX[0]} not found on PATH"
    out = (cp.stdout or "")[:MAX_OUTPUT]
    err = (cp.stderr or "")[:MAX_OUTPUT]
    return f"exit={cp.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
