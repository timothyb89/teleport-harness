"""The workbench MCP server: the agent's ONE and ONLY tool.

Launched as a stdio subprocess by `claude -p --mcp-config …` (see harness/agent.py). It exposes
a single tool, `run(cmd)`, which executes a shell command INSIDE one specific, disposable
container — the workbench — and nothing else. This is the containment boundary: the agent's
brain runs on the host, but its entire action space is "strings executed inside $WORKBENCH_CONTAINER".

The target container is bound from the environment (`WORKBENCH_CONTAINER`), NEVER from a tool
argument, so the model cannot redirect the exec at another container or the host. The workbench
mounts no docker socket, so in-container root cannot escape. Output is size-capped; each call has
a wall-clock timeout.

Run standalone for a smoke test:  WORKBENCH_CONTAINER=<id>-workbench python -m harness.agent_mcp
"""

from __future__ import annotations

import os
import subprocess

from mcp.server.fastmcp import FastMCP

CONTAINER = os.environ.get("WORKBENCH_CONTAINER", "")
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
    if not CONTAINER:
        return "error: no workbench container configured (WORKBENCH_CONTAINER unset)"
    try:
        cp = subprocess.run(
            ["docker", "exec", "-w", "/work", CONTAINER, "sh", "-lc", cmd],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {TIMEOUT}s (background long-running processes)"
    except FileNotFoundError:
        return "error: docker not found on PATH"
    out = (cp.stdout or "")[:MAX_OUTPUT]
    err = (cp.stderr or "")[:MAX_OUTPUT]
    return f"exit={cp.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
