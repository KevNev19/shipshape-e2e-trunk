#!/usr/bin/env python3
"""Collect bounded weekly security and quality evidence as one JSON object."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from datetime import date
from pathlib import Path

PAGE_SIZE = 100
MAX_ALERTS = 3000
PULL_PAGE_SIZE = 50
MAX_PULL_REQUESTS = 1000
ACTION_RUN_LIMIT = 20
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PERMISSIONS_RE = re.compile(r"(?m)^\s*permissions\s*:")


class CollectionError(RuntimeError):
    """A bounded evidence source was unavailable or malformed."""


class GitHubClient:
    """Small JSON adapter for read-only GitHub API calls."""

    def get(self, path: str):
        try:
            process = subprocess.run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github+json",
                    "-H",
                    "X-GitHub-Api-Version: 2022-11-28",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CollectionError("GitHub API command was unavailable or timed out") from error
        if process.returncode != 0:
            raise CollectionError("GitHub API request failed")
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise CollectionError("GitHub API returned malformed JSON") from error


def _safe_text(value, fallback="unknown") -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return " ".join(value.split())[:300]


def _nested(record: dict, *keys):
    value = record
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _complete(items: list[dict]) -> dict:
    return {"status": "complete", "count": len(items), "items": items, "error": None}


def _failed(items: list[dict], error: str) -> dict:
    return {
        "status": "incomplete" if items else "unavailable",
        "count": len(items),
        "items": items,
        "error": error,
    }


def _project_text(value):
    return value if isinstance(value, str) else None


def _project_codeql(record: dict) -> dict:
    line = _nested(record, "most_recent_instance", "location", "start_line")
    return {
        "severity": _project_text(
            _nested(record, "rule", "security_severity_level")
            or _nested(record, "rule", "severity")
        ),
        "description": _project_text(_nested(record, "rule", "description")),
        "path": _project_text(_nested(record, "most_recent_instance", "location", "path")),
        "start_line": line if isinstance(line, int) and not isinstance(line, bool) else None,
        "html_url": _project_text(record.get("html_url")),
    }


def _project_dependabot(record: dict) -> dict:
    return {
        "severity": _project_text(_nested(record, "security_advisory", "severity")),
        "package": _project_text(_nested(record, "dependency", "package", "name")),
        "summary": _project_text(_nested(record, "security_advisory", "summary")),
        "html_url": _project_text(record.get("html_url")),
    }


def _project_secret_scanning(record: dict) -> dict:
    return {
        "secret_type_display_name": _project_text(record.get("secret_type_display_name")),
        "html_url": _project_text(record.get("html_url")),
    }


def _project_action_run(record: dict) -> dict:
    return {
        "name": _project_text(record.get("name")),
        "status": _project_text(record.get("status")),
        "conclusion": _project_text(record.get("conclusion")),
        "created_at": _project_text(record.get("created_at")),
    }


def _project_pull_request(record: dict) -> dict:
    number = record.get("number")
    draft = record.get("draft")
    return {
        "number": number if isinstance(number, int) and not isinstance(number, bool) else None,
        "title": _project_text(record.get("title")),
        "created_at": _project_text(record.get("created_at")),
        "draft": draft if isinstance(draft, bool) else None,
    }


def _collect_pages(
    api,
    base_path: str,
    *,
    page_size: int,
    max_items: int,
    record_name: str,
    projector,
) -> dict:
    items = []
    for page in range(1, max_items // page_size + 1):
        path = f"{base_path}&per_page={page_size}&page={page}"
        try:
            batch = api.get(path)
        except (CollectionError, KeyError):
            return _failed(items, "GitHub API request failed")
        if not isinstance(batch, list) or len(batch) > page_size:
            return _failed(items, "GitHub API pagination was malformed")
        if not all(isinstance(item, dict) for item in batch):
            return _failed(items, f"GitHub API returned a malformed {record_name} record")
        items.extend(projector(item) for item in batch)
        if len(batch) < page_size:
            return _complete(items)
    return {
        "status": "truncated",
        "count": len(items),
        "items": items,
        "error": f"{record_name} collection reached the {max_items}-record safety limit",
    }


def _collect_alert_pages(api, base_path: str, projector) -> dict:
    return _collect_pages(
        api,
        base_path,
        page_size=PAGE_SIZE,
        max_items=MAX_ALERTS,
        record_name="alert",
        projector=projector,
    )


def _collect_pull_pages(api, base_path: str) -> dict:
    return _collect_pages(
        api,
        base_path,
        page_size=PULL_PAGE_SIZE,
        max_items=MAX_PULL_REQUESTS,
        record_name="pull request",
        projector=_project_pull_request,
    )


def _action_result(
    items: list[dict],
    *,
    status: str,
    error: str | None,
    total_count: int | None,
    window_complete: bool | None,
    history_complete: bool | None,
) -> dict:
    return {
        "status": status,
        "count": len(items),
        "items": items,
        "error": error,
        "window": {
            "limit": ACTION_RUN_LIMIT,
            "returned_count": len(items),
            "complete": window_complete,
        },
        "history": {"total_count": total_count, "complete": history_complete},
    }


def _collect_action_runs(api, path: str) -> dict:
    try:
        payload = api.get(path)
    except (CollectionError, KeyError):
        return _action_result(
            [],
            status="unavailable",
            error="GitHub API request failed",
            total_count=None,
            window_complete=None,
            history_complete=None,
        )
    if not isinstance(payload, dict):
        return _action_result(
            [],
            status="unavailable",
            error="GitHub API response was malformed",
            total_count=None,
            window_complete=None,
            history_complete=None,
        )

    runs = payload.get("workflow_runs")
    if (
        not isinstance(runs, list)
        or len(runs) > ACTION_RUN_LIMIT
        or not all(isinstance(item, dict) for item in runs)
    ):
        return _action_result(
            [],
            status="unavailable",
            error="GitHub API action run window was malformed",
            total_count=None,
            window_complete=None,
            history_complete=None,
        )
    items = [_project_action_run(item) for item in runs]
    total_count = payload.get("total_count")
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count < 0:
        return _action_result(
            items,
            status="incomplete" if items else "unavailable",
            error="GitHub API action run count was malformed",
            total_count=None,
            window_complete=None,
            history_complete=None,
        )

    expected_count = min(total_count, ACTION_RUN_LIMIT)
    if len(items) != expected_count:
        return _action_result(
            items,
            status="incomplete" if items else "unavailable",
            error="GitHub API action run count was inconsistent",
            total_count=total_count,
            window_complete=None,
            history_complete=None,
        )
    if total_count > ACTION_RUN_LIMIT:
        return _action_result(
            items,
            status="truncated",
            error=(
                f"action run collection retained the latest {ACTION_RUN_LIMIT} "
                f"of {total_count} available runs"
            ),
            total_count=total_count,
            window_complete=True,
            history_complete=False,
        )
    return _action_result(
        items,
        status="complete",
        error=None,
        total_count=total_count,
        window_complete=True,
        history_complete=True,
    )


def _inspect_permission_declarations(root: Path) -> dict:
    workflow_dir = root / ".github" / "workflows"
    if not workflow_dir.exists():
        return {
            "status": "complete",
            "count": 0,
            "inspected_count": 0,
            "items": [],
            "error": None,
            "missing_declaration": [],
        }
    if not workflow_dir.is_dir():
        return {
            "status": "unavailable",
            "count": 0,
            "inspected_count": 0,
            "items": [],
            "error": ".github/workflows is not a directory",
            "missing_declaration": [],
        }
    files = sorted({*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")})
    missing = []
    inspected_count = 0
    try:
        for path in files:
            if not path.is_file() or not PERMISSIONS_RE.search(path.read_text(encoding="utf-8")):
                missing.append(str(path.relative_to(root)))
            inspected_count += 1
    except (OSError, UnicodeError):
        return {
            "status": "incomplete",
            "count": len(files),
            "inspected_count": inspected_count,
            "items": [],
            "error": "workflow files could not be inspected",
            "missing_declaration": missing,
        }
    return {
        "status": "complete",
        "count": len(files),
        "inspected_count": inspected_count,
        "items": [],
        "error": None,
        "missing_declaration": missing,
    }


def _render_items(section: dict, formatter) -> list[str]:
    lines = [formatter(item) for item in section["items"]]
    if section["status"] == "complete" and not lines:
        return ["- None reported by the API."]
    if section["status"] != "complete":
        prefix = "Collection incomplete" if section["items"] else "Collection unavailable"
        lines.append(
            f"- {prefix}: {_safe_text(section['error'])}. No absence inference can be made."
        )
    return lines


def _codeql_line(item: dict) -> str:
    severity = item.get("severity")
    description = item.get("description")
    path = item.get("path")
    line = item.get("start_line")
    location = f"{_safe_text(path)}:{line}" if isinstance(line, int) else _safe_text(path)
    return (
        f"- [{_safe_text(severity)}] {_safe_text(description)} — {location} "
        f"({_safe_text(item.get('html_url'))})"
    )


def _dependabot_line(item: dict) -> str:
    return (
        f"- [{_safe_text(item.get('severity'))}] "
        f"{_safe_text(item.get('package'))}: "
        f"{_safe_text(item.get('summary'))} "
        f"({_safe_text(item.get('html_url'))})"
    )


def _secret_line(item: dict) -> str:
    return (
        f"- {_safe_text(item.get('secret_type_display_name'))} ({_safe_text(item.get('html_url'))})"
    )


def _run_line(item: dict) -> str:
    state = item.get("conclusion") or item.get("status")
    return (
        f"- {_safe_text(item.get('name'))}: {_safe_text(state)} "
        f"({_safe_text(item.get('created_at'))})"
    )


def _pull_line(item: dict) -> str:
    number = item.get("number") if isinstance(item.get("number"), int) else "unknown"
    draft = item.get("draft") if isinstance(item.get("draft"), bool) else "unknown"
    return (
        f"- #{number} {_safe_text(item.get('title'))} "
        f"(opened {_safe_text(item.get('created_at'))}, draft: {draft})"
    )


def _assignment(assignment_status: str) -> dict:
    if assignment_status == "requested":
        detail = "agent assignment will be attempted; assignment is not a completed review"
    elif assignment_status == "manual_handoff":
        detail = "no assignable agent is proven; manual handoff is required"
    else:
        detail = "agent assignment was not requested"
    return {"status": assignment_status, "detail": detail}


def _render_body(repo: str, gathered_on: date, default_branch: str, report: dict) -> str:
    sections = report["raw_collection"]["sections"]
    assignment = report["assignment"]
    lines = [
        f"Weekly security and quality data for {repo}, gathered {gathered_on.isoformat()}.",
        "",
        "Agent: read `.github/agents/security-quality.md` for the review briefing.",
        "",
        "## Collection status",
        "",
        f"- Raw collection: {report['raw_collection']['status']}",
        "- Inference: not_run",
        "- Review: not_run",
        f"- Agent assignment: {assignment['status']} — {assignment['detail']}.",
        "",
        "## CodeQL alerts (open)",
        "",
        *_render_items(sections["codeql"], _codeql_line),
        "",
        "## Dependabot alerts (open)",
        "",
        *_render_items(sections["dependabot"], _dependabot_line),
        "",
        "## Secret scanning alerts (open)",
        "",
        *_render_items(sections["secret_scanning"], _secret_line),
        "",
        f"## CI health (last {ACTION_RUN_LIMIT} runs on `{default_branch}`)",
        "",
        *_render_items(sections["actions"], _run_line),
    ]
    actions = sections["actions"]
    action_history = actions["history"]
    if action_history["complete"] is True:
        lines.append(
            f"- API total_count confirms all {action_history['total_count']} available runs "
            f"fit within the {ACTION_RUN_LIMIT}-run window."
        )
    elif action_history["complete"] is False:
        lines.append(
            f"- The bounded window contains {actions['count']} of "
            f"{action_history['total_count']} available runs; earlier history was omitted."
        )
    else:
        lines.append(
            "- History completeness is unknown because API total_count could not be validated."
        )
    lines.extend(
        [
            "",
            "## Workflow permission declarations (bounded inspection)",
            "",
        ]
    )
    permissions = sections["permissions"]
    missing = permissions["missing_declaration"]
    if permissions["status"] != "complete":
        lines.append(
            "- Inspection incomplete after "
            f"{permissions['inspected_count']} of {permissions['count']} workflow files: "
            f"{_safe_text(permissions['error'])}."
        )
        if missing:
            lines.append("- Missing declarations found before inspection stopped:")
            lines.extend(f"  - `{path}`" for path in missing)
    elif permissions["count"] == 0:
        lines.append("- No workflow files were found.")
    elif missing:
        lines.append("- Workflows without a detected permissions declaration:")
        lines.extend(f"  - `{path}`" for path in missing)
    else:
        lines.append("- Every inspected workflow declares a permissions block.")
    lines.extend(
        [
            "- This textual inspection does not determine effective least privilege.",
            "",
            "## Open pull requests",
            "",
            *_render_items(sections["pull_requests"], _pull_line),
            "",
        ]
    )
    return "\n".join(lines)


def collect_report(
    api,
    repo: str,
    root: Path,
    *,
    gathered_on: date | None = None,
    assignment_status: str = "not_requested",
) -> dict:
    """Collect the fixed weekly sections without performing inference or review."""
    if REPO_RE.fullmatch(repo) is None:
        raise ValueError("repository must be an exact owner/name slug")
    if assignment_status not in {"not_requested", "requested", "manual_handoff"}:
        raise ValueError("assignment status was unsupported")
    gathered_on = date.today() if gathered_on is None else gathered_on
    repository_error = None
    try:
        repository = api.get(f"repos/{repo}")
        if (
            not isinstance(repository, dict)
            or repository.get("full_name") != repo
            or not isinstance(repository.get("default_branch"), str)
            or not repository["default_branch"]
        ):
            raise CollectionError("repository identity or default branch was malformed")
        default_branch = repository["default_branch"]
    except (CollectionError, KeyError):
        default_branch = "unknown"
        repository_error = "repository metadata was unavailable"

    encoded_branch = urllib.parse.quote(default_branch, safe="")
    sections = {
        "codeql": _collect_alert_pages(
            api, f"repos/{repo}/code-scanning/alerts?state=open", _project_codeql
        ),
        "dependabot": _collect_alert_pages(
            api, f"repos/{repo}/dependabot/alerts?state=open", _project_dependabot
        ),
        "secret_scanning": _collect_alert_pages(
            api,
            f"repos/{repo}/secret-scanning/alerts?state=open",
            _project_secret_scanning,
        ),
        "actions": (
            _action_result(
                [],
                status="unavailable",
                error=f"default branch unavailable: {repository_error}",
                total_count=None,
                window_complete=None,
                history_complete=None,
            )
            if repository_error
            else _collect_action_runs(
                api,
                f"repos/{repo}/actions/runs?branch={encoded_branch}&per_page={ACTION_RUN_LIMIT}",
            )
        ),
        "pull_requests": _collect_pull_pages(api, f"repos/{repo}/pulls?state=open"),
        "permissions": _inspect_permission_declarations(Path(root)),
    }
    collection_complete = repository_error is None and all(
        section["status"] == "complete" for section in sections.values()
    )
    report = {
        "ok": collection_complete,
        "status": "complete" if collection_complete else "incomplete",
        "repository": repo,
        "default_branch": default_branch,
        "gathered_on": gathered_on.isoformat(),
        "raw_collection": {
            "status": "complete" if collection_complete else "incomplete",
            "repository_error": repository_error,
            "sections": sections,
        },
        "inference": {"status": "not_run"},
        "review": {"status": "not_run"},
        "assignment": _assignment(assignment_status),
    }
    report["body"] = _render_body(repo, gathered_on, default_branch, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--assignment-status",
        choices=("not_requested", "requested", "manual_handoff"),
        default="not_requested",
    )
    args = parser.parse_args(argv)
    try:
        report = collect_report(
            GitHubClient(),
            args.repo,
            Path(args.root),
            assignment_status=args.assignment_status,
        )
    except ValueError as error:
        report = {
            "ok": False,
            "status": "invalid",
            "body": "",
            "error": str(error),
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"complete", "incomplete"} else 2


if __name__ == "__main__":
    sys.exit(main())
