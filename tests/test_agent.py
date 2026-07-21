"""Unit tests for harness/agent.py — the agent-driven test driver. These assert the CONTAINMENT
contract (the agent gets exactly one tool; every host-touching built-in is denied; the target
container is bound by env, not a model-influenceable argument) and config/schema handling, all
without invoking `claude` or docker."""

from __future__ import annotations

from pathlib import Path

from harness.agent import (
    ALLOWED_TOOL,
    DISALLOWED_TOOLS,
    AgentConfig,
    AgentResult,
    build_claude_argv,
    load_agent_config,
    mcp_config,
    settings_json,
)

ROOT = Path(__file__).resolve().parent.parent


def test_lockdown_flags_allow_exactly_one_tool_and_deny_host_builtins():
    cfg = AgentConfig(model="claude-opus-4-8")
    argv = build_claude_argv(cfg, Path("/m.json"), Path("/s.json"), Path("/sp.txt"))
    assert argv[0] == "claude" and "-p" in argv
    # the prompt is fed via stdin, NOT argv — a trailing positional would be swallowed by the
    # variadic --disallowed-tools; so the last argv token is the final denied tool.
    assert argv[-1] == DISALLOWED_TOOLS[-1]
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    # exactly ONE allowed tool, and it's the workbench exec tool
    ai = argv.index("--allowed-tools")
    assert argv[ai + 1] == ALLOWED_TOOL == "mcp__workbench__run"
    assert argv[ai + 2] == "--disallowed-tools"  # nothing else allow-listed after it
    # every host-touching built-in is denied (so the host-run brain can't read local files)
    di = argv.index("--disallowed-tools")
    denied = set(argv[di + 1: di + 1 + len(DISALLOWED_TOOLS)])
    for t in ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"):
        assert t in denied, t
    # deny-unlisted + only-our-mcp-server
    assert "dontAsk" in argv and "--strict-mcp-config" in argv


def test_mcp_config_binds_container_via_env_not_model_argument():
    srv = mcp_config("c1-workbench")["mcpServers"]["workbench"]
    assert srv["env"]["WORKBENCH_CONTAINER"] == "c1-workbench"
    # the container name is delivered via env — NOT anywhere the model could set it
    assert "c1-workbench" not in srv["args"]
    assert srv["command"] == "uv"  # launches our stdio server in the harness project


def test_settings_hook_denies_any_non_workbench_tool():
    hook = settings_json()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert ALLOWED_TOOL in hook and "sys.exit" in hook  # fail-closed backstop


def test_load_agent_config_reads_and_defaults():
    tmp = ROOT / "modules" / "docs_bound_keypair"
    cfg = load_agent_config(tmp)
    assert cfg and cfg.provider == "claude" and cfg.prompt == "prompt.md"
    assert cfg.timeout_seconds == 900


def test_load_agent_config_none_when_no_agent_block(tmp_path):
    (tmp_path / "render.yaml").write_text("components: [x]\n")
    assert load_agent_config(tmp_path) is None
    # and None for a module with no render.yaml at all
    assert load_agent_config(tmp_path / "nope") is None


def test_agent_result_ignores_extra_keys_and_defaults():
    r = AgentResult.model_validate_json(
        '{"status":"pass","chatty":"ignored","steps":[{"n":1,"action":"a","junk":1}]}'
    )
    assert r.status == "pass" and r.steps[0].action == "a" and r.steps[0].ok is True
    assert r.issues == []
