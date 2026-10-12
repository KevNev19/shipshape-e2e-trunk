#!/usr/bin/env python3
"""Select Shipshape's worker orchestrator for an explicit tool host."""

import argparse
import json
import sys

CODEX_CLAUDE_REF = "519ccacda39aafe3ea703cc871d93b141eb8089b"

ROUTES = {
    "codex": {
        "coordinator_host": "codex",
        "worker_provider": "claude",
        "plugin": "codex-claude-orchestrator",
        "skill": "claude-pty-agents",
        "entrypoint": "$codex-claude-orchestrator:claude-pty-agents",
        "entrypoint_path": "skills/claude-pty-agents/SKILL.md",
        "install": {
            "repository": "coredo-eu/codex-claude-orchestrator",
            "ref": CODEX_CLAUDE_REF,
            "version": "0.4.0+codex.20260818204818",
            "commands": [
                "codex plugin marketplace add "
                "coredo-eu/codex-claude-orchestrator "
                f"--ref {CODEX_CLAUDE_REF}",
                "codex plugin add codex-claude-orchestrator@codex-claude-orchestrator",
            ],
        },
    },
    "claude": {
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
    },
}


class ArgumentError(Exception):
    """Raised when argparse encounters invalid CLI input."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ArgumentError(message)


def main(argv: list[str] | None = None) -> int:
    parser = JsonArgumentParser(prog="orchestrator.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    route_parser = subparsers.add_parser("route")
    route_parser.add_argument("--host", choices=sorted(ROUTES), required=True)
    try:
        args = parser.parse_args(argv)
    except ArgumentError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2

    print(json.dumps(ROUTES[args.host]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
