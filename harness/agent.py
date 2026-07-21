"""Agent-driven tests: run a locked-down AI agent (today `claude -p`, on the host, using
the user's subscription login — no API key) that drives a disposable cluster to accomplish a
real task (e.g. follow a doc), then report what it found.

Containment is the whole game. The agent's brain runs on the host, but its ONLY capability is a
single MCP tool `run(cmd)` (harness/agent_mcp.py) that execs inside one named workbench
container — never the host. This module assembles the lockdown invocation (the guarantee is the
*combination* of layers, not any single flag):

  * --strict-mcp-config + --mcp-config <ours>  → the only MCP server is our one-tool workbench
  * --allowed-tools mcp__workbench__run        → that tool is the only one auto-approved
  * --disallowed-tools <all host built-ins>    → Bash/Read/Write/... removed from context, so the
                                                 host-run brain can't read ~/.ssh etc.
  * --permission-mode dontAsk                  → anything not allow-listed is denied, not prompted
  * a PreToolUse hook (settings.json)          → belt-and-suspenders deny of any other tool
  * cwd = a scratch dir, NOT the harness repo  → non-bare `claude -p` loads cwd CLAUDE.md/skills

The driver lives behind a tiny seam so a future API-key `SdkApiDriver` / `CodexDriver` is a
drop-in (selected by `agent.provider`) with the module contract unchanged.

The agent writes its verdict to /out/agent-result.json (a state-dir bind mount); the
`agent_result` verb (harness/verify.py) reads it back and surfaces it in the report. Objective
declarative checks (bot_joined/resource_present) are what actually gate PASS/FAIL — the agent's
self-verdict is advisory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field

# --- where the workbench drops its verdict / where we stash the transcript (state-dir-relative)
RESULT_RELPATH = "agent/out/agent-result.json"
TRANSCRIPT_RELPATH = "agent/transcript.json"

# The agent's single tool + every built-in that touches the HOST (removed from context).
ALLOWED_TOOL = "mcp__workbench__run"
DISALLOWED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite",
]

SYSTEM_PROMPT = (
    "You are an automated test agent validating a real task end-to-end. Your ONLY capability is "
    "the tool `run(cmd)`, which executes a shell command INSIDE a disposable container — you "
    "cannot touch the host, read local files, or browse the web. Do the work by issuing shell "
    "commands through `run`. Background long-running processes and poll them. Never fabricate "
    "success: verify each step from real command output. Finish by writing your verdict to "
    "/out/agent-result.json as JSON with keys: task, status (pass|partial|fail), summary, "
    "steps[{n,action,expected,observed,ok,doc_ref}], issues[{severity,area,description,evidence,"
    "suggested_fix}]. Report every snag as an issue."
)


# --- structured verdict the agent must produce (extra keys ignored — robust to a chatty agent)
class AgentStep(BaseModel):
    model_config = ConfigDict(extra="ignore")
    n: int = 0
    action: str = ""
    expected: str = ""
    observed: str = ""
    ok: bool = True
    doc_ref: str = ""


class AgentIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    severity: str = "minor"   # blocker | major | minor | nit
    area: str = "docs"        # docs | product | env
    description: str = ""
    evidence: str = ""
    suggested_fix: str = ""


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task: str = ""
    status: str = "partial"   # pass | partial | fail
    summary: str = ""
    steps: list[AgentStep] = Field(default_factory=list)
    issues: list[AgentIssue] = Field(default_factory=list)


# --- per-module agent config, read from the module's render.yaml `agent:` block
class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: str = "claude"          # future: codex / sdk-api
    model: str = "claude-opus-4-8"
    prompt: str = "prompt.md"         # module-relative; rendered as jinja with connection facts
    timeout_seconds: int = 900        # wall-clock cap (no --max-turns flag in claude today)


def load_agent_config(module_dir: Path) -> AgentConfig | None:
    """Return the module's AgentConfig, or None if it declares no `agent:` block (=> not an
    agent module; agent-run is a no-op for it)."""
    f = Path(module_dir) / "render.yaml"
    if not f.is_file():
        return None
    rv = yaml.safe_load(f.read_text()) or {}
    block = rv.get("agent") if isinstance(rv, dict) else None
    if not block:
        return None
    return AgentConfig(**block)


def _harness_root() -> Path:
    return Path(__file__).resolve().parent.parent


def mcp_config(workbench_container: str, timeout_s: int = 120) -> dict:
    """The --mcp-config payload: launch our stdio workbench server, binding the target
    container via env (NOT via any model-controlled argument)."""
    return {
        "mcpServers": {
            "workbench": {
                "command": "uv",
                "args": ["run", "--quiet", "--project", str(_harness_root()),
                         "python", "-m", "harness.agent_mcp"],
                "env": {
                    "WORKBENCH_CONTAINER": workbench_container,
                    "WORKBENCH_TIMEOUT": str(timeout_s),
                },
            }
        }
    }


def settings_json() -> dict:
    """Minimal settings + a PreToolUse hook backstop that denies any tool other than our one
    workbench tool (hooks run first and are enforced even under bypass). Fail-closed: a
    non-matching tool_name exits 2 (block)."""
    deny = (
        "python3 -c \"import json,sys; d=json.load(sys.stdin); "
        "sys.exit(0 if d.get('tool_name')=='%s' else 2)\"" % ALLOWED_TOOL
    )
    return {
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": deny}]}
            ]
        }
    }


def build_claude_argv(cfg: AgentConfig, mcp_path: Path, settings_path: Path,
                      sysprompt_path: Path) -> list[str]:
    """Assemble the locked-down `claude -p` invocation (pure — unit-tested). The task prompt is
    NOT an argv element: it's fed via stdin, because `--disallowed-tools <tools...>` is variadic
    and would otherwise swallow a trailing positional prompt."""
    return [
        "claude", "-p",
        "--output-format", "json",
        "--model", cfg.model,
        "--strict-mcp-config",
        "--mcp-config", str(mcp_path),
        "--permission-mode", "dontAsk",
        "--settings", str(settings_path),
        "--append-system-prompt-file", str(sysprompt_path),
        "--allowed-tools", ALLOWED_TOOL,
        "--disallowed-tools", *DISALLOWED_TOOLS,
    ]


def _read_meta(state_dir: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    f = state_dir / "meta.env"
    if f.is_file():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                meta[k.strip()] = v.strip()
    return meta


def _render_prompt(module_dir: Path, cfg: AgentConfig, ctx: dict) -> str:
    src = Path(module_dir) / cfg.prompt
    tmpl = src.read_text() if src.is_file() else ""
    env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
    return env.from_string(tmpl).render(**ctx)


class SubscriptionCLIDriver:
    """Runs the agent via the host `claude` CLI (subscription auth). The only driver today."""

    name = "claude"

    def run(self, argv: list[str], cwd: Path, timeout_s: int, stdin: str = "") -> tuple[int, str, str]:
        cp = subprocess.run(argv, cwd=str(cwd), input=stdin, capture_output=True,
                            text=True, timeout=timeout_s)
        return cp.returncode, cp.stdout or "", cp.stderr or ""


def run_agent(module_dir: Path, cluster_id: str, state_dir: Path) -> tuple[bool, str]:
    """Render the prompt, write the lockdown config, and drive the workbench with the agent.

    Returns (ok, message). `ok` is False only on INFRASTRUCTURAL failure (claude missing /
    timeout / crash) — never on the agent's own verdict (that's advisory, surfaced later by the
    agent_result verb). A no-`agent:` module is a no-op (ok=True). The agent writes its result to
    /out/agent-result.json inside the workbench, which is a bind mount onto state/<id>/agent/out.
    """
    module_dir, state_dir = Path(module_dir), Path(state_dir)
    cfg = load_agent_config(module_dir)
    if cfg is None:
        return True, f"{module_dir.name}: no agent task (no `agent:` in render.yaml), skipping"
    if cfg.provider != "claude":
        return False, f"agent provider '{cfg.provider}' not supported yet (only 'claude')"
    if shutil.which("claude") is None:
        return False, "agent-run: `claude` CLI not found on PATH (needed for subscription auth)"

    meta = _read_meta(state_dir)
    fqdn, port = meta.get("FQDN", ""), meta.get("PORT", "")
    workbench = f"{cluster_id}-workbench"

    agent_dir = state_dir / "agent"
    run_cwd, out_dir = agent_dir / "run", agent_dir / "out"
    run_cwd.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    # stale result from a previous run would mask a silent failure — clear it.
    (out_dir / "agent-result.json").unlink(missing_ok=True)

    prompt_text = _render_prompt(module_dir, cfg, {
        "cluster_id": cluster_id, "fqdn": fqdn, "port": port,
        "auth_addr": f"{cluster_id}-auth:3025",
    })

    mcp_path = agent_dir / "mcp.json"
    settings_path = agent_dir / "settings.json"
    sysprompt_path = agent_dir / "system-prompt.txt"
    mcp_path.write_text(json.dumps(mcp_config(workbench), indent=2) + "\n")
    settings_path.write_text(json.dumps(settings_json(), indent=2) + "\n")
    sysprompt_path.write_text(SYSTEM_PROMPT + "\n")

    argv = build_claude_argv(cfg, mcp_path, settings_path, sysprompt_path)
    driver = SubscriptionCLIDriver()
    try:
        rc, stdout, stderr = driver.run(argv, cwd=run_cwd, timeout_s=cfg.timeout_seconds,
                                        stdin=prompt_text)
    except subprocess.TimeoutExpired:
        (agent_dir / "transcript.json").write_text('{"error":"timeout"}\n')
        return False, f"agent-run: timed out after {cfg.timeout_seconds}s"
    (agent_dir / "transcript.json").write_text(stdout or stderr or "")
    if rc != 0:
        return False, f"agent-run: claude exited {rc} (see {TRANSCRIPT_RELPATH}); stderr: {stderr[:200]}"
    produced = (out_dir / "agent-result.json").is_file()
    return True, (f"agent-run: {module_dir.name} completed"
                  + ("" if produced else " (WARNING: no agent-result.json written)"))
