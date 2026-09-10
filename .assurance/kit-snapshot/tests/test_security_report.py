"""Tests for the bounded weekly security and quality collector."""

import json
import subprocess
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

from scripts import security_report


class FakeGitHub:
    def __init__(self, responses=None, errors=None):
        self.responses = responses or {}
        self.errors = errors or {}
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path in self.errors:
            raise security_report.CollectionError(self.errors[path])
        return self.responses[path]


def base_responses():
    return {
        "repos/example/project": {
            "full_name": "example/project",
            "default_branch": "develop",
        },
        "repos/example/project/code-scanning/alerts?state=open&per_page=100&page=1": [],
        "repos/example/project/dependabot/alerts?state=open&per_page=100&page=1": [],
        "repos/example/project/secret-scanning/alerts?state=open&per_page=100&page=1": [],
        "repos/example/project/actions/runs?branch=develop&per_page=20": {
            "total_count": 0,
            "workflow_runs": [],
        },
        "repos/example/project/pulls?state=open&per_page=50": [],
        "repos/example/project/pulls?state=open&per_page=50&page=1": [],
    }


def test_successful_empty_sections_are_explicitly_none(tmp_path):
    api = FakeGitHub(base_responses())

    report = security_report.collect_report(
        api,
        "example/project",
        tmp_path,
        gathered_on=date(2026, 9, 9),
        assignment_status="manual_handoff",
    )

    assert report["status"] == "complete"
    assert report["raw_collection"]["status"] == "complete"
    assert report["inference"]["status"] == "not_run"
    assert report["review"]["status"] == "not_run"
    assert report["assignment"]["status"] == "manual_handoff"
    assert report["body"].count("- None reported by the API.") == 5
    actions = report["raw_collection"]["sections"]["actions"]
    assert actions["window"] == {"limit": 20, "returned_count": 0, "complete": True}
    assert actions["history"] == {"total_count": 0, "complete": True}
    assert "all 0 available runs fit within the 20-run window" in report["body"]
    assert "branch=develop" in api.calls[-2]


def test_api_error_is_incomplete_and_never_rendered_as_none(tmp_path):
    responses = base_responses()
    path = "repos/example/project/dependabot/alerts?state=open&per_page=100&page=1"
    api = FakeGitHub(responses, {path: "HTTP 403"})

    report = security_report.collect_report(
        api,
        "example/project",
        tmp_path,
        gathered_on=date(2026, 9, 9),
        assignment_status="requested",
    )

    section = report["raw_collection"]["sections"]["dependabot"]
    assert report["status"] == "incomplete"
    assert section["status"] == "unavailable"
    assert "absence" in report["body"].lower()
    assert report["assignment"]["status"] == "requested"
    assert report["review"]["status"] == "not_run"


def test_missing_repository_metadata_keeps_action_history_unknown(tmp_path):
    responses = base_responses()
    api = FakeGitHub(responses, {"repos/example/project": "HTTP 404"})

    report = security_report.collect_report(api, "example/project", tmp_path)
    actions = report["raw_collection"]["sections"]["actions"]

    assert report["status"] == "incomplete"
    assert actions["status"] == "unavailable"
    assert actions["window"] == {"limit": 20, "returned_count": 0, "complete": None}
    assert actions["history"] == {"total_count": None, "complete": None}
    assert "History completeness is unknown" in report["body"]


def test_alert_sections_paginate_until_a_short_page(tmp_path):
    responses = base_responses()
    first = "repos/example/project/code-scanning/alerts?state=open&per_page=100&page=1"
    second = "repos/example/project/code-scanning/alerts?state=open&per_page=100&page=2"
    responses[first] = [
        {
            "rule": {"severity": "warning", "description": f"Finding {index}"},
            "most_recent_instance": {"location": {"path": "src/app.py", "start_line": 1}},
            "html_url": f"https://example.invalid/alerts/{index}",
        }
        for index in range(100)
    ]
    responses[second] = []
    api = FakeGitHub(responses)

    report = security_report.collect_report(
        api,
        "example/project",
        tmp_path,
        gathered_on=date(2026, 9, 9),
        assignment_status="manual_handoff",
    )

    section = report["raw_collection"]["sections"]["codeql"]
    assert section["status"] == "complete"
    assert section["count"] == 100
    assert second in api.calls


def test_alert_pagination_failure_marks_partial_data_incomplete(tmp_path):
    responses = base_responses()
    first = "repos/example/project/secret-scanning/alerts?state=open&per_page=100&page=1"
    second = "repos/example/project/secret-scanning/alerts?state=open&per_page=100&page=2"
    responses[first] = [
        {"secret_type_display_name": "Synthetic token", "html_url": "https://example.invalid"}
        for _ in range(100)
    ]
    api = FakeGitHub(responses, {second: "HTTP 502"})

    report = security_report.collect_report(
        api,
        "example/project",
        tmp_path,
        gathered_on=date(2026, 9, 9),
        assignment_status="manual_handoff",
    )

    section = report["raw_collection"]["sections"]["secret_scanning"]
    assert report["status"] == "incomplete"
    assert section["status"] == "incomplete"
    assert section["count"] == 100
    assert "Collection incomplete" in report["body"]


def test_nonempty_success_formats_realistic_alert_run_and_pull_shapes(tmp_path):
    responses = base_responses()
    responses["repos/example/project/code-scanning/alerts?state=open&per_page=100&page=1"] = [
        {
            "rule": {"security_severity_level": "high", "description": "Unsafe input"},
            "most_recent_instance": {"location": {"path": "src/app.py", "start_line": 17}},
            "html_url": "https://example.invalid/codeql/1",
        }
    ]
    responses["repos/example/project/dependabot/alerts?state=open&per_page=100&page=1"] = [
        {
            "security_advisory": {"severity": "moderate", "summary": "Example advisory"},
            "dependency": {"package": {"name": "example-package"}},
            "html_url": "https://example.invalid/dependabot/1",
        }
    ]
    responses["repos/example/project/secret-scanning/alerts?state=open&per_page=100&page=1"] = [
        {
            "secret_type_display_name": "Synthetic token",
            "html_url": "https://example.invalid/secrets/1",
        }
    ]
    responses["repos/example/project/actions/runs?branch=develop&per_page=20"] = {
        "total_count": 1,
        "workflow_runs": [
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-09T08:00:00Z",
            }
        ],
    }
    responses["repos/example/project/pulls?state=open&per_page=50"] = [
        {
            "number": 12,
            "title": "Document setup",
            "created_at": "2026-09-08T08:00:00Z",
            "draft": False,
        }
    ]
    responses["repos/example/project/pulls?state=open&per_page=50&page=1"] = responses[
        "repos/example/project/pulls?state=open&per_page=50"
    ]

    report = security_report.collect_report(
        FakeGitHub(responses),
        "example/project",
        tmp_path,
        gathered_on=date(2026, 9, 9),
        assignment_status="requested",
    )

    assert report["status"] == "complete"
    assert "[high] Unsafe input — src/app.py:17" in report["body"]
    assert "example-package: Example advisory" in report["body"]
    assert "CI: success" in report["body"]
    assert "#12 Document setup" in report["body"]
    assert "assignment is not a completed review" in report["body"]


def test_actions_window_reports_available_history_truncation(tmp_path):
    responses = base_responses()
    actions_path = "repos/example/project/actions/runs?branch=develop&per_page=20"
    responses[actions_path] = {
        "total_count": 21,
        "workflow_runs": [
            {
                "name": f"CI {index}",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-09T08:00:00Z",
                "unretained": "fixture-private-material",
            }
            for index in range(20)
        ],
    }

    report = security_report.collect_report(FakeGitHub(responses), "example/project", tmp_path)
    actions = report["raw_collection"]["sections"]["actions"]

    assert report["status"] == "incomplete"
    assert actions["status"] == "truncated"
    assert actions["count"] == 20
    assert actions["window"] == {"limit": 20, "returned_count": 20, "complete": True}
    assert actions["history"] == {"total_count": 21, "complete": False}
    assert "fixture-private-material" not in json.dumps(report)
    assert "20 of 21" in report["body"]


@pytest.mark.parametrize("invalid_count", [None, True, -1, 1.5, "20"])
def test_actions_window_rejects_invalid_total_count(tmp_path, invalid_count):
    responses = base_responses()
    actions_path = "repos/example/project/actions/runs?branch=develop&per_page=20"
    responses[actions_path] = {"total_count": invalid_count, "workflow_runs": []}

    report = security_report.collect_report(FakeGitHub(responses), "example/project", tmp_path)
    actions = report["raw_collection"]["sections"]["actions"]

    assert report["status"] == "incomplete"
    assert actions["status"] == "unavailable"
    assert actions["window"] == {"limit": 20, "returned_count": 0, "complete": None}
    assert actions["history"] == {"total_count": None, "complete": None}
    assert actions["error"] == "GitHub API action run count was malformed"
    assert "History completeness is unknown" in report["body"]


def test_actions_window_rejects_count_smaller_than_returned_records(tmp_path):
    responses = base_responses()
    actions_path = "repos/example/project/actions/runs?branch=develop&per_page=20"
    responses[actions_path] = {
        "total_count": 0,
        "workflow_runs": [
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-09-09T08:00:00Z",
            }
        ],
    }

    report = security_report.collect_report(FakeGitHub(responses), "example/project", tmp_path)
    actions = report["raw_collection"]["sections"]["actions"]

    assert report["status"] == "incomplete"
    assert actions["status"] == "incomplete"
    assert actions["window"]["complete"] is None
    assert actions["history"]["complete"] is None
    assert actions["error"] == "GitHub API action run count was inconsistent"


def test_alert_records_are_projected_through_section_allowlists(tmp_path):
    responses = base_responses()
    raw_secret = "fixture-" + "private-material"
    unknown_value = "future-" + "unretained-value"
    responses["repos/example/project/code-scanning/alerts?state=open&per_page=100&page=1"] = [
        {
            "rule": {"security_severity_level": "high", "description": "Unsafe input"},
            "most_recent_instance": {"location": {"path": "src/app.py", "start_line": 17}},
            "html_url": "https://example.invalid/codeql/1",
            "future_api_field": {"opaque": unknown_value},
        }
    ]
    responses["repos/example/project/dependabot/alerts?state=open&per_page=100&page=1"] = [
        {
            "security_advisory": {"severity": "high", "summary": "Example advisory"},
            "dependency": {"package": {"name": "example-package"}},
            "html_url": "https://example.invalid/dependabot/1",
            "future_api_field": unknown_value,
        }
    ]
    responses["repos/example/project/secret-scanning/alerts?state=open&per_page=100&page=1"] = [
        {
            "secret_type_display_name": "Synthetic token",
            "html_url": "https://example.invalid/secrets/1",
            "secret": raw_secret,
            "future_api_field": {"opaque": unknown_value},
        }
    ]

    report = security_report.collect_report(
        FakeGitHub(responses),
        "example/project",
        tmp_path,
        gathered_on=date(2026, 9, 9),
    )
    sections = report["raw_collection"]["sections"]

    assert sections["codeql"]["items"] == [
        {
            "severity": "high",
            "description": "Unsafe input",
            "path": "src/app.py",
            "start_line": 17,
            "html_url": "https://example.invalid/codeql/1",
        }
    ]
    assert sections["dependabot"]["items"] == [
        {
            "severity": "high",
            "package": "example-package",
            "summary": "Example advisory",
            "html_url": "https://example.invalid/dependabot/1",
        }
    ]
    assert set(sections["secret_scanning"]["items"][0]) == {
        "secret_type_display_name",
        "html_url",
    }
    serialized = json.dumps(report)
    assert raw_secret not in serialized
    assert unknown_value not in serialized


def test_api_error_details_are_not_retained_in_report_output(tmp_path):
    responses = base_responses()
    path = "repos/example/project/secret-scanning/alerts?state=open&per_page=100&page=1"
    api = FakeGitHub(responses, {path: "upstream response included private fields"})

    report = security_report.collect_report(api, "example/project", tmp_path)

    section = report["raw_collection"]["sections"]["secret_scanning"]
    assert section["error"] == "GitHub API request failed"
    assert "private fields" not in json.dumps(report)


def test_open_pull_requests_paginate_after_a_full_page(tmp_path):
    responses = base_responses()
    first = "repos/example/project/pulls?state=open&per_page=50&page=1"
    second = "repos/example/project/pulls?state=open&per_page=50&page=2"
    responses[first] = [
        {
            "number": number,
            "title": f"Pull {number}",
            "created_at": "2026-09-09T08:00:00Z",
            "draft": False,
        }
        for number in range(1, 51)
    ]
    responses[second] = [
        {
            "number": 51,
            "title": "Pull 51",
            "created_at": "2026-09-09T08:00:00Z",
            "draft": False,
        }
    ]
    api = FakeGitHub(responses)

    report = security_report.collect_report(api, "example/project", tmp_path)

    section = report["raw_collection"]["sections"]["pull_requests"]
    assert section["status"] == "complete"
    assert section["count"] == 51
    assert second in api.calls


def test_open_pull_request_second_page_error_preserves_partial_status(tmp_path):
    responses = base_responses()
    first = "repos/example/project/pulls?state=open&per_page=50&page=1"
    second = "repos/example/project/pulls?state=open&per_page=50&page=2"
    responses[first] = [
        {
            "number": number,
            "title": f"Pull {number}",
            "created_at": "2026-09-09T08:00:00Z",
            "draft": False,
        }
        for number in range(1, 51)
    ]
    api = FakeGitHub(responses, {second: "HTTP 502"})

    report = security_report.collect_report(api, "example/project", tmp_path)

    section = report["raw_collection"]["sections"]["pull_requests"]
    assert section["status"] == "incomplete"
    assert section["count"] == 50
    assert "No absence inference can be made" in report["body"]


def test_open_pull_request_safety_limit_is_explicitly_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(security_report, "MAX_PULL_REQUESTS", 50)
    responses = base_responses()
    first = "repos/example/project/pulls?state=open&per_page=50&page=1"
    responses[first] = [
        {
            "number": number,
            "title": f"Pull {number}",
            "created_at": "2026-09-09T08:00:00Z",
            "draft": False,
        }
        for number in range(1, 51)
    ]

    report = security_report.collect_report(FakeGitHub(responses), "example/project", tmp_path)

    section = report["raw_collection"]["sections"]["pull_requests"]
    assert section["status"] == "truncated"
    assert section["count"] == 50
    assert "No absence inference can be made" in report["body"]


def test_partial_permission_inspection_never_claims_every_workflow_was_inspected(
    tmp_path, monkeypatch
):
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "a.yml").write_text("permissions:\n  contents: read\n")
    (workflow_dir / "b.yml").write_text("permissions:\n  contents: read\n")
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "b.yml":
            raise OSError("synthetic inspection failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    report = security_report.collect_report(
        FakeGitHub(base_responses()), "example/project", tmp_path
    )

    permissions = report["raw_collection"]["sections"]["permissions"]
    assert permissions["status"] == "incomplete"
    assert permissions["inspected_count"] == 1
    assert "Every inspected workflow" not in report["body"]
    assert "Inspection incomplete after 1 of 2 workflow files" in report["body"]


def test_safety_limit_reports_truncation_instead_of_no_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(security_report, "MAX_ALERTS", 100)
    responses = base_responses()
    path = "repos/example/project/code-scanning/alerts?state=open&per_page=100&page=1"
    responses[path] = [
        {
            "rule": {"severity": "warning", "description": f"Finding {index}"},
            "most_recent_instance": {"location": {"path": "src/app.py"}},
            "html_url": f"https://example.invalid/alerts/{index}",
        }
        for index in range(100)
    ]

    report = security_report.collect_report(
        FakeGitHub(responses),
        "example/project",
        tmp_path,
        gathered_on=date(2026, 9, 9),
    )

    assert report["status"] == "incomplete"
    assert report["raw_collection"]["sections"]["codeql"]["status"] == "truncated"
    assert "No absence inference can be made" in report["body"]


def test_subprocess_client_uses_one_read_only_api_call():
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"full_name": "example/project", "default_branch": "main"}),
        stderr="",
    )
    with mock.patch("scripts.security_report.subprocess.run", return_value=completed) as run:
        payload = security_report.GitHubClient().get("repos/example/project")

    assert payload["default_branch"] == "main"
    arguments = run.call_args.args[0]
    assert arguments[-1] == "repos/example/project"
    assert "--method" not in arguments


def test_subprocess_client_does_not_expose_failed_response_details():
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="upstream response included private fields",
    )
    with mock.patch("scripts.security_report.subprocess.run", return_value=completed):
        with pytest.raises(security_report.CollectionError) as raised:
            security_report.GitHubClient().get("repos/example/project/secret-scanning/alerts")

    assert str(raised.value) == "GitHub API request failed"


def test_workflow_keeps_manual_handoff_distinct_from_review_completion():
    workflow = (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "workflows"
        / "security-quality-report.yml"
    ).read_text(encoding="utf-8")

    assert "assignment_status=manual_handoff" in workflow
    assert "status=manual_handoff" in workflow
    assert "Inference and review have not run" in workflow
    assert "replaceActorsForAssignable" in workflow
    assert "grant" not in workflow.casefold()
