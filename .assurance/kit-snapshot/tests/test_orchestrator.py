"""Host-to-worker orchestration routing through the public JSON CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "orchestrator.py"


def run_route(*args: str, env: dict[str, str] | None = None) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "route", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


def test_codex_host_routes_to_pinned_claude_orchestrator():
    code, payload = run_route("--host", "codex")

    assert code == 0
    assert payload == {
        "coordinator_host": "codex",
        "worker_provider": "claude",
        "plugin": "codex-claude-orchestrator",
        "skill": "claude-pty-agents",
        "entrypoint": "$codex-claude-orchestrator:claude-pty-agents",
        "entrypoint_path": "skills/claude-pty-agents/SKILL.md",
        "install": {
            "repository": "coredo-eu/codex-claude-orchestrator",
            "ref": "519ccacda39aafe3ea703cc871d93b141eb8089b",
            "version": "0.4.0+codex.20260818204818",
            "commands": [
                "codex plugin marketplace add coredo-eu/codex-claude-orchestrator "
                "--ref 519ccacda39aafe3ea703cc871d93b141eb8089b",
                "codex plugin add codex-claude-orchestrator@codex-claude-orchestrator",
            ],
        },
    }


def test_claude_host_routes_to_codex_orchestrator_workflow():
    code, payload = run_route("--host", "claude")

    assert code == 0
    assert payload == {
        "coordinator_host": "claude",
        "worker_provider": "codex",
        "plugin": "codex-orchestrator",
        "skill": "workflow",
        "entrypoint": "/codex-orchestrator:workflow",
        "entrypoint_path": "skills/workflow/SKILL.md",
        "metadata": {
            "repository": "alexzh3/codex-orchestrator",
            "version": "0.5.0",
            "contract_path": "docs/orchestration-contract.md",
        },
    }


def test_invalid_host_returns_one_json_error():
    code, payload = run_route("--host", "other")

    assert code == 2
    assert payload["ok"] is False
    assert "invalid choice" in payload["error"]


def test_omitted_host_returns_one_json_error():
    code, payload = run_route()

    assert code == 2
    assert payload == {
        "ok": False,
        "error": "the following arguments are required: --host",
    }


def test_explicit_host_ignores_inherited_provider_markers():
    inherited = os.environ | {
        "CODEX_THREAD_ID": "inherited-from-parent",
        "CLAUDECODE": "inherited-from-parent",
        "CLAUDE_CODE_ENTRYPOINT": "inherited-from-parent",
    }

    for host, expected_provider in (("codex", "claude"), ("claude", "codex")):
        code, payload = run_route("--host", host, env=inherited)

        assert code == 0
        assert payload["coordinator_host"] == host
        assert payload["worker_provider"] == expected_provider
