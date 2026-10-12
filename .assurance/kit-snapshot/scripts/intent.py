#!/usr/bin/env python3
"""Create a lightweight, user-owned change intent file."""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

FIELDS = ("problem", "constraints", "non_goals", "acceptance_checks")
FIELD_NAMES = {
    "problem": "problem",
    "constraints": "constraints",
    "non_goals": "non-goals",
    "acceptance_checks": "acceptance checks",
}
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class ArgumentError(Exception):
    """Raised when the command line does not match the JSON-only contract."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ArgumentError(message)


def fail(next_action: str, **details: str) -> int:
    payload = {"ok": False, **details, "next_action": next_action}
    print(json.dumps(payload))
    return 1


def load_input(path: Path) -> tuple[dict | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"Create the input JSON file at {path} and try again."
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, f"Fix the JSON in {path} and try again."
    if not isinstance(payload, dict):
        return None, "Add a non-empty problem field to the input JSON and try again."
    return payload, None


def validate(payload: dict) -> tuple[dict | None, str | None]:
    cleaned = {}
    for field in FIELDS:
        name = FIELD_NAMES[field]
        if field not in payload:
            return None, f"Add the missing {name} field and try again."
        value = payload[field]
        if field == "acceptance_checks":
            if not isinstance(value, list) or not value:
                return None, (
                    "Add at least one non-empty string to the acceptance checks field "
                    "and try again."
                )
            if any(not isinstance(item, str) or not item.strip() for item in value):
                return None, (
                    "Make every item in the acceptance checks field a non-empty string "
                    "and try again."
                )
            cleaned[field] = [item.strip() for item in value]
        elif not isinstance(value, str) or not value.strip():
            return None, f"Make the {name} field a non-empty string and try again."
        else:
            cleaned[field] = value.strip()

    unexpected = sorted(set(payload) - set(FIELDS))
    if unexpected:
        return None, f"Remove the unexpected {unexpected[0]} field and try again."
    return cleaned, None


def render(slug: str, intent: dict) -> str:
    checks = "\n".join(f"- {check}" for check in intent["acceptance_checks"])
    return (
        f"# Change intent: {slug}\n\n"
        f"## Problem\n\n{intent['problem']}\n\n"
        f"## Constraints\n\n{intent['constraints']}\n\n"
        f"## Non-goals\n\n{intent['non_goals']}\n\n"
        f"## Acceptance checks\n\n{checks}\n"
    )


def create(repo: Path, slug: str, input_path: Path) -> int:
    if not repo.is_dir():
        return fail(f"Choose an existing repository directory; {repo} was not found.")
    if not SLUG_RE.fullmatch(slug):
        return fail("Use a lowercase kebab-case slug such as add-health-check and try again.")

    payload, error = load_input(input_path)
    if error:
        return fail(error)
    intent, error = validate(payload)
    if error:
        return fail(error)

    relative_path = Path("docs") / "changes" / f"{date.today().isoformat()}-{slug}.md"
    destination = repo / relative_path
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as output:
            output.write(render(slug, intent))
    except FileExistsError:
        return fail(
            f"A change intent already exists at {relative_path.as_posix()}; choose another slug.",
            action="exists",
            path=relative_path.as_posix(),
        )
    except OSError:
        return fail(f"Check write access to {destination.parent} and try again.")

    print(
        json.dumps(
            {
                "ok": True,
                "action": "created",
                "path": relative_path.as_posix(),
                "next_action": "Begin implementation against this intent.",
            }
        )
    )
    return 0


def main() -> int:
    parser = JsonArgumentParser(prog="intent.py", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", add_help=False)
    create_parser.add_argument("repo")
    create_parser.add_argument("--slug", required=True)
    create_parser.add_argument("--input", required=True)
    try:
        args = parser.parse_args()
    except ArgumentError:
        return fail("Use: intent.py create <repo> --slug <lowercase-kebab> --input <json-file>.")

    return create(Path(args.repo).resolve(), args.slug, Path(args.input).resolve())


if __name__ == "__main__":
    sys.exit(main())
