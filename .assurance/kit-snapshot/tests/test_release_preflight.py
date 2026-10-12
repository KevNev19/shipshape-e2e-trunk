import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_preflight.py"
TEMPLATE = ROOT / "templates" / "scripts" / "release-preflight.py.tmpl"
WORKFLOW_TEMPLATE = ROOT / "templates" / "github" / "workflows" / "release.yml.tmpl"
REPOSITORY = "acme/widgets"
WORKFLOWS = {
    ".github/workflows/ci.yml": 101,
    ".github/workflows/secret-scan.yml": 102,
    ".github/workflows/codeql.yml": 103,
    ".github/workflows/release.yml": 104,
}
ASSURANCE_WORKFLOWS = set(WORKFLOWS) - {".github/workflows/release.yml"}
PUBLICATION_HELPER = ".sdlc/scripts/release-preflight.py"
CODEQL_APP_ID = 57789
CODEQL_CHECK_NAME = "CodeQL"


def workflow_run_block(step_name: str) -> str:
    workflow = WORKFLOW_TEMPLATE.read_text()
    step = workflow.split(f"      - name: {step_name}\n", 1)[1]
    indented_block = step.split("        run: |\n", 1)[1]
    lines = []
    for line in indented_block.splitlines():
        if not line.startswith("          "):
            break
        lines.append(line[10:])
    return "\n".join(lines)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "release-tests@example.invalid")
    git(repo, "config", "user.name", "Release Tests")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / ".sdlc").mkdir()
    (repo / ".sdlc" / "config.json").write_text(
        json.dumps(
            {
                "features": {
                    "release_automation": True,
                    "secret_guard": True,
                    "codeql": True,
                }
            }
        )
        + "\n"
    )
    (repo / "app.py").write_text("VALUE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    return repo, remote, git(repo, "rev-parse", "HEAD")


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n")
    git(repo, "add", name)
    git(repo, "commit", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


def run_record(
    path: str,
    sha: str,
    *,
    run_id: int | None = None,
    status: str = "completed",
    conclusion: str | None = "success",
    branch: str = "main",
    event: str = "push",
    attempt: int = 1,
    number: int = 20,
    started_at: str = "2026-09-09T10:00:00Z",
) -> dict:
    workflow_id = WORKFLOWS[path]
    return {
        "id": run_id or workflow_id * 10,
        "workflow_id": workflow_id,
        "run_number": number,
        "run_attempt": attempt,
        "run_started_at": started_at,
        "check_suite_id": workflow_id * 100,
        "status": status,
        "conclusion": conclusion,
        "head_sha": sha,
        "head_branch": branch,
        "event": event,
        "path": path,
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id or workflow_id * 10}",
    }


def job_record(
    path: str, sha: str, *, run_id: int | None = None, conclusion: str = "success"
) -> dict:
    workflow_id = WORKFLOWS[path]
    actual_run_id = run_id or workflow_id * 10
    return {
        "id": workflow_id * 1000,
        "run_id": actual_run_id,
        "head_sha": sha,
        "name": f"{Path(path).stem} checks",
        "status": "completed",
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{workflow_id * 10}/job/1",
    }


def codeql_check_record(
    sha: str,
    *,
    check_id: int = 9001,
    suite_id: int = 9002,
    app_id: int = CODEQL_APP_ID,
    status: str = "completed",
    conclusion: str | None = "success",
    started_at: str = "2026-09-09T10:00:30Z",
    completed_at: str | None = "2026-09-09T10:01:00Z",
) -> dict:
    return {
        "id": check_id,
        "name": CODEQL_CHECK_NAME,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
        "completed_at": completed_at,
        "html_url": f"https://github.com/{REPOSITORY}/runs/{check_id}",
        "app": {"id": app_id},
        "check_suite": {"id": suite_id},
    }


def base_responses(
    sha: str,
    *,
    secret_guard: bool = True,
    codeql: bool = True,
    languages: list[str] | None = None,
) -> dict:
    languages = ["python"] if languages is None else languages
    enabled_paths = [".github/workflows/ci.yml"]
    if secret_guard:
        enabled_paths.append(".github/workflows/secret-scan.yml")
    if codeql and languages and languages[0] in {"python", "node", "go", "java", "ruby", "csharp"}:
        enabled_paths.append(".github/workflows/codeql.yml")
    catalog_paths = [*enabled_paths, ".github/workflows/release.yml"]
    config = {
        "languages": languages,
        "features": {
            "release_automation": True,
            "secret_guard": secret_guard,
            "codeql": codeql,
        },
    }
    responses = {
        f"repos/{REPOSITORY}": {
            "full_name": REPOSITORY,
            "default_branch": "main",
        },
        f"repos/{REPOSITORY}/commits/main": {
            "sha": sha,
            "html_url": f"https://github.com/{REPOSITORY}/commit/{sha}",
        },
        f"repos/{REPOSITORY}/contents/.sdlc/config.json": {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(json.dumps(config).encode()).decode(),
        },
        f"repos/{REPOSITORY}/contents/{PUBLICATION_HELPER}": {
            "type": "file",
            "path": PUBLICATION_HELPER,
            "sha": "d" * 40,
        },
        f"repos/{REPOSITORY}/actions/workflows": [
            {
                "total_count": len(catalog_paths),
                "workflows": [
                    {
                        "id": WORKFLOWS[path],
                        "name": Path(path).stem,
                        "path": path,
                        "state": "active",
                        "html_url": f"https://github.com/{REPOSITORY}/actions/workflows/{Path(path).name}",
                    }
                    for path in catalog_paths
                ],
            }
        ],
    }
    for path in enabled_paths:
        workflow_id = WORKFLOWS[path]
        responses[f"repos/{REPOSITORY}/actions/workflows/{workflow_id}/runs"] = [
            {
                "total_count": 1,
                "workflow_runs": [run_record(path, sha)],
            }
        ]
        responses[f"repos/{REPOSITORY}/actions/runs/{workflow_id * 10}/attempts/1/jobs"] = [
            {"total_count": 1, "jobs": [job_record(path, sha)]}
        ]
    if ".github/workflows/codeql.yml" in enabled_paths:
        check = codeql_check_record(sha)
        responses[f"repos/{REPOSITORY}/commits/{sha}/check-runs"] = [
            {"total_count": 1, "check_runs": [check]}
        ]
        responses[f"repos/{REPOSITORY}/check-suites/{check['check_suite']['id']}"] = {
            "id": check["check_suite"]["id"],
            "head_sha": sha,
            "app": {"id": CODEQL_APP_ID},
        }
    return responses


def install_fake_gh(tmp_path: Path, responses: dict) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    response_file = tmp_path / "gh-responses.json"
    response_file.write_text(json.dumps(responses))
    log_file = tmp_path / "gh-calls.jsonl"
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
endpoint = next((arg for arg in args if arg == "graphql" or arg.startswith("repos/")), "")
with open(os.environ["FAKE_GH_RESPONSES"], encoding="utf-8") as source:
    responses = json.load(source)
response = responses.get(endpoint, {"__exit__": 1})
if isinstance(response, dict) and "__raw__" in response:
    sys.stdout.write(response["__raw__"])
    raise SystemExit(response.get("__exit__", 0))
if isinstance(response, dict) and response.get("__exit__"):
    raise SystemExit(response["__exit__"])
json.dump(response, sys.stdout)
sys.stdout.write("\\n")
"""
    )
    fake_gh.chmod(0o755)
    return bin_dir, log_file


def run_preflight(
    tmp_path: Path,
    responses: dict,
    *args: str | Path,
    script: Path = SCRIPT,
) -> tuple[subprocess.CompletedProcess[str], dict, list[list[str]]]:
    bin_dir, log_file = install_fake_gh(tmp_path, responses)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_GH_RESPONSES"] = str(tmp_path / "gh-responses.json")
    env["FAKE_GH_LOG"] = str(log_file)
    result = subprocess.run(
        [sys.executable, str(script), *[str(arg) for arg in args]],
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    calls = (
        [json.loads(line) for line in log_file.read_text().splitlines()]
        if log_file.exists()
        else []
    )
    return result, payload, calls


def error_codes(payload: dict) -> set[str]:
    return {error["code"] for error in payload.get("errors", [])}


def test_local_preflight_accepts_only_exact_remote_head_with_exact_green_runs(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    result, payload, calls = run_preflight(
        tmp_path, base_responses(sha), "local", repo, "--repo", REPOSITORY
    )

    assert result.returncode == 0, payload
    assert result.stderr == ""
    assert payload["ok"] is True
    assert payload["candidate_sha"] == sha
    assert payload["default_branch"] == "main"
    assert {item["path"] for item in payload["workflow_runs"]} == ASSURANCE_WORKFLOWS
    assert "release_workflow_active" in payload["checks"]
    assert "publication_helper_present" in payload["checks"]
    assert all(item["head_sha"] == sha for item in payload["workflow_runs"])
    assert all(item["url"].startswith("https://github.com/") for item in payload["workflow_runs"])
    paginated = [call for call in calls if "--paginate" in call]
    assert paginated
    assert all("--slurp" in call and "per_page=100" in call for call in paginated)
    assert not any("actions/workflows/104/runs" in field for call in calls for field in call)
    helper_calls = [
        call for call in calls if any(f"contents/{PUBLICATION_HELPER}" in field for field in call)
    ]
    assert len(helper_calls) == 1
    assert f"ref={sha}" in helper_calls[0]


def test_rendered_helper_is_standalone_and_matches_kit_entrypoint(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    rendered = tmp_path / "release-preflight.py"
    rendered.write_text(TEMPLATE.read_text().replace("{{KIT_VERSION}}", "9.9.9"))
    rendered.chmod(0o755)

    result, payload, _ = run_preflight(
        tmp_path, base_responses(sha), "local", repo, "--repo", REPOSITORY, script=rendered
    )

    assert result.returncode == 0, payload
    assert payload["ok"] is True
    assert payload["candidate_sha"] == sha
    assert {item["workflow"] for item in payload["workflow_runs"]} == {
        "ci",
        "secret_scan",
        "codeql",
    }
    assert {item["path"] for item in payload["workflow_runs"]} == ASSURANCE_WORKFLOWS
    assert re.search(r"\{\{[A-Z0-9_]+\}\}", rendered.read_text()) is None


def test_green_unrelated_workflow_and_stale_ci_sha_are_not_evidence(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    stale = "1" * 40
    responses = base_responses(sha, secret_guard=False, codeql=False)
    workflows = responses[f"repos/{REPOSITORY}/actions/workflows"][0]["workflows"]
    workflows.append(
        {
            "id": 999,
            "name": "CI",
            "path": ".github/workflows/health.yml",
            "state": "active",
            "html_url": f"https://github.com/{REPOSITORY}/actions/workflows/health.yml",
        }
    )
    responses[f"repos/{REPOSITORY}/actions/workflows"][0]["total_count"] += 1
    responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"][0]["workflow_runs"] = [
        run_record(".github/workflows/ci.yml", stale)
    ]
    responses[f"repos/{REPOSITORY}/actions/workflows/999/runs"] = [
        {
            "total_count": 1,
            "workflow_runs": [
                {
                    **run_record(".github/workflows/ci.yml", sha, run_id=9990),
                    "workflow_id": 999,
                    "path": ".github/workflows/health.yml",
                }
            ],
        }
    ]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "workflow_run_absent" in error_codes(payload)
    assert payload["ok"] is False


def test_codeql_is_not_required_when_enabled_but_inapplicable_to_the_primary_language(tmp_path):
    repo, _, sha = make_repo(tmp_path)

    result, payload, calls = run_preflight(
        tmp_path,
        base_responses(sha, languages=["rust"]),
        "local",
        repo,
        "--repo",
        REPOSITORY,
    )

    assert result.returncode == 0, payload
    assert {item["path"] for item in payload["workflow_runs"]} == {
        ".github/workflows/ci.yml",
        ".github/workflows/secret-scan.yml",
    }
    assert not any("/check-runs" in field for call in calls for field in call)


def test_successful_codeql_analysis_does_not_override_failed_findings_check(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    endpoint = f"repos/{REPOSITORY}/commits/{sha}/check-runs"
    responses[endpoint][0]["check_runs"][0]["conclusion"] = "failure"

    result, payload, calls = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "codeql_findings_unsuccessful" in error_codes(payload)
    check_calls = [call for call in calls if endpoint in call]
    assert len(check_calls) == 1
    assert "check_name=CodeQL" in check_calls[0]
    assert f"app_id={CODEQL_APP_ID}" in check_calls[0]
    assert "filter=all" in check_calls[0]


def test_successful_codeql_findings_are_projected_as_release_evidence(tmp_path):
    repo, _, sha = make_repo(tmp_path)

    result, payload, calls = run_preflight(
        tmp_path, base_responses(sha), "local", repo, "--repo", REPOSITORY
    )

    assert result.returncode == 0, payload
    codeql = next(run for run in payload["workflow_runs"] if run["workflow"] == "codeql")
    findings = codeql["findings_check"]
    assert findings == {
        "name": CODEQL_CHECK_NAME,
        "app_id": CODEQL_APP_ID,
        "check_run_id": 9001,
        "check_suite_id": 9002,
        "head_sha": sha,
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-09-09T10:00:30Z",
        "completed_at": "2026-09-09T10:01:00Z",
        "url": f"https://github.com/{REPOSITORY}/runs/9001",
    }
    assert "successful_codeql_findings" in payload["checks"]
    assert any(f"repos/{REPOSITORY}/check-suites/9002" in call for call in calls)


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("missing", "codeql_findings_absent"),
        ("malformed", "incomplete_response"),
        ("pending", "codeql_findings_pending"),
        ("failed", "codeql_findings_unsuccessful"),
        ("neutral", "codeql_findings_unsuccessful"),
        ("skipped", "codeql_findings_unsuccessful"),
    ],
)
def test_codeql_findings_must_be_present_well_formed_and_successful(tmp_path, state, expected_code):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    endpoint = f"repos/{REPOSITORY}/commits/{sha}/check-runs"
    check = responses[endpoint][0]["check_runs"][0]
    if state == "missing":
        responses[endpoint] = [{"total_count": 0, "check_runs": []}]
    elif state == "malformed":
        del check["started_at"]
    elif state == "pending":
        check.update(status="in_progress", conclusion=None, completed_at=None)
    else:
        check["conclusion"] = {
            "failed": "failure",
            "neutral": "neutral",
            "skipped": "skipped",
        }[state]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1, state
    assert expected_code in error_codes(payload)
    assert payload["next_action"]


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("check", "head_sha", "f" * 40),
        ("check", "name", "CodeQL / Analyze"),
        ("check_app", "id", 15368),
        ("suite", "head_sha", "f" * 40),
        ("suite_app", "id", 15368),
    ],
)
def test_codeql_findings_require_exact_head_and_advanced_security_app(
    tmp_path, location, field, value
):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    endpoint = f"repos/{REPOSITORY}/commits/{sha}/check-runs"
    check = responses[endpoint][0]["check_runs"][0]
    suite = responses[f"repos/{REPOSITORY}/check-suites/9002"]
    target = {
        "check": check,
        "check_app": check["app"],
        "suite": suite,
        "suite_app": suite["app"],
    }[location]
    target[field] = value

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1, location
    assert "codeql_findings_identity_mismatch" in error_codes(payload)


def test_tied_latest_codeql_findings_chronology_fails_closed(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    endpoint = f"repos/{REPOSITORY}/commits/{sha}/check-runs"
    first = responses[endpoint][0]["check_runs"][0]
    second = codeql_check_record(
        sha,
        check_id=9003,
        suite_id=9004,
        conclusion="failure",
        started_at=first["started_at"],
    )
    responses[endpoint] = [{"total_count": 2, "check_runs": [first, second]}]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "codeql_findings_chronology_ambiguous" in error_codes(payload)
    assert set(payload["errors"][0]["check_run_ids"]) == {9001, 9003}


@pytest.mark.parametrize("failure", ["api", "malformed_json", "pagination", "suite_api"])
def test_codeql_findings_api_and_pagination_fail_closed(tmp_path, failure):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    endpoint = f"repos/{REPOSITORY}/commits/{sha}/check-runs"
    if failure == "api":
        responses[endpoint] = {"__exit__": 1}
        expected = "github_api_error"
    elif failure == "malformed_json":
        responses[endpoint] = {"__raw__": "not-json"}
        expected = "github_api_malformed"
    elif failure == "suite_api":
        responses[f"repos/{REPOSITORY}/check-suites/9002"] = {"__exit__": 1}
        expected = "github_api_error"
    else:
        responses[endpoint][0]["total_count"] = 2
        expected = "incomplete_response"

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1, failure
    assert expected in error_codes(payload)


def test_older_green_codeql_findings_do_not_satisfy_a_newer_analysis_attempt(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    codeql_runs = responses[f"repos/{REPOSITORY}/actions/workflows/103/runs"]
    codeql_runs[0]["workflow_runs"][0]["run_started_at"] = "2026-09-09T11:00:00Z"

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "codeql_findings_absent" in error_codes(payload)
    assert payload["errors"][0]["analysis_run_id"] == 1030


def test_later_failed_codeql_findings_override_older_green_evidence(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    endpoint = f"repos/{REPOSITORY}/commits/{sha}/check-runs"
    older_green = responses[endpoint][0]["check_runs"][0]
    later_failed = codeql_check_record(
        sha,
        check_id=9003,
        suite_id=9004,
        conclusion="failure",
        started_at="2026-09-09T10:02:00Z",
        completed_at="2026-09-09T10:03:00Z",
    )
    responses[endpoint] = [{"total_count": 2, "check_runs": [older_green, later_failed]}]
    responses[f"repos/{REPOSITORY}/check-suites/9004"] = {
        "id": 9004,
        "head_sha": sha,
        "app": {"id": CODEQL_APP_ID},
    }

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "codeql_findings_unsuccessful" in error_codes(payload)
    assert payload["errors"][0]["check_run_id"] == 9003


def test_later_green_codeql_findings_override_older_failed_evidence(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    endpoint = f"repos/{REPOSITORY}/commits/{sha}/check-runs"
    older_failed = responses[endpoint][0]["check_runs"][0]
    older_failed["conclusion"] = "failure"
    later_green = codeql_check_record(
        sha,
        check_id=9003,
        suite_id=9004,
        started_at="2026-09-09T10:02:00Z",
        completed_at="2026-09-09T10:03:00Z",
    )
    responses[endpoint] = [{"total_count": 2, "check_runs": [older_failed, later_green]}]
    responses[f"repos/{REPOSITORY}/check-suites/9004"] = {
        "id": 9004,
        "head_sha": sha,
        "app": {"id": CODEQL_APP_ID},
    }

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 0, payload
    codeql = next(run for run in payload["workflow_runs"] if run["workflow"] == "codeql")
    assert codeql["findings_check"]["check_run_id"] == 9003


def test_disabled_codeql_does_not_require_a_findings_check(tmp_path):
    repo, _, sha = make_repo(tmp_path)

    result, payload, calls = run_preflight(
        tmp_path,
        base_responses(sha, codeql=False),
        "local",
        repo,
        "--repo",
        REPOSITORY,
    )

    assert result.returncode == 0, payload
    assert "successful_codeql_findings" not in payload["checks"]
    assert not any("/check-runs" in field for call in calls for field in call)


@pytest.mark.parametrize("mode", ["publish", "verify-release"])
def test_publish_and_postflight_require_successful_codeql_findings(tmp_path, mode):
    _, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    add_remote_tag(responses, "v1.0.0", sha)
    endpoint = f"repos/{REPOSITORY}/commits/{sha}/check-runs"
    responses[endpoint][0]["check_runs"][0]["conclusion"] = "failure"

    result, payload, _ = run_preflight(
        tmp_path,
        responses,
        mode,
        "--repo",
        REPOSITORY,
        "--tag",
        "v1.0.0",
        "--candidate",
        sha,
    )

    assert result.returncode == 1, mode
    assert "codeql_findings_unsuccessful" in error_codes(payload)


@pytest.mark.parametrize(
    ("case", "runs", "expected_code"),
    [
        ("absent", [], "workflow_run_absent"),
        (
            "red",
            [run_record(".github/workflows/ci.yml", "{sha}", conclusion="failure")],
            "workflow_run_unsuccessful",
        ),
        (
            "cancelled",
            [run_record(".github/workflows/ci.yml", "{sha}", conclusion="cancelled")],
            "workflow_run_unsuccessful",
        ),
        (
            "skipped",
            [run_record(".github/workflows/ci.yml", "{sha}", conclusion="skipped")],
            "workflow_run_unsuccessful",
        ),
        (
            "pending",
            [
                run_record(
                    ".github/workflows/ci.yml",
                    "{sha}",
                    status="in_progress",
                    conclusion=None,
                )
            ],
            "workflow_run_pending",
        ),
    ],
)
def test_relevant_workflow_must_be_present_completed_and_successful(
    tmp_path, case, runs, expected_code
):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    prepared = [
        {key: (sha if value == "{sha}" else value) for key, value in run.items()} for run in runs
    ]
    responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"] = [
        {"total_count": len(prepared), "workflow_runs": prepared}
    ]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1, case
    assert expected_code in error_codes(payload)


def test_skipped_job_is_not_success_evidence(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    responses[f"repos/{REPOSITORY}/actions/runs/1010/attempts/1/jobs"] = [
        {
            "total_count": 1,
            "jobs": [job_record(".github/workflows/ci.yml", sha, conclusion="skipped")],
        }
    ]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "workflow_jobs_unsuccessful" in error_codes(payload)


def test_chronologically_later_older_run_rerun_takes_precedence(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    failed_rerun = run_record(
        ".github/workflows/ci.yml",
        sha,
        run_id=200,
        attempt=2,
        number=20,
        conclusion="failure",
        started_at="2026-09-09T12:00:00Z",
    )
    newer_success = run_record(
        ".github/workflows/ci.yml",
        sha,
        run_id=210,
        number=21,
        started_at="2026-09-09T11:00:00Z",
    )
    responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"] = [
        {"total_count": 2, "workflow_runs": [newer_success, failed_rerun]}
    ]
    responses[f"repos/{REPOSITORY}/actions/runs/210/attempts/1/jobs"] = [
        {
            "total_count": 1,
            "jobs": [job_record(".github/workflows/ci.yml", sha, run_id=210)],
        }
    ]

    result, payload, calls = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "workflow_run_unsuccessful" in error_codes(payload)
    assert payload["errors"][0]["run_id"] == 200
    assert payload["errors"][0]["run_attempt"] == 2
    run_calls = [call for call in calls if f"repos/{REPOSITORY}/actions/workflows/101/runs" in call]
    assert len(run_calls) == 1
    assert f"head_sha={sha}" in run_calls[0]
    assert "event=push" in run_calls[0]
    assert "branch=main" in run_calls[0]
    assert all(not field.startswith("conclusion=") for field in run_calls[0])
    assert all(not field.startswith("status=") for field in run_calls[0])


def test_higher_run_started_after_older_rerun_is_selected(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    older_failed_rerun = run_record(
        ".github/workflows/ci.yml",
        sha,
        run_id=200,
        attempt=2,
        number=20,
        conclusion="failure",
        started_at="2026-09-09T11:00:00Z",
    )
    later_success = run_record(
        ".github/workflows/ci.yml",
        sha,
        run_id=210,
        number=21,
        started_at="2026-09-09T12:00:00Z",
    )
    responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"] = [
        {"total_count": 2, "workflow_runs": [older_failed_rerun, later_success]}
    ]
    responses[f"repos/{REPOSITORY}/actions/runs/210/attempts/1/jobs"] = [
        {
            "total_count": 1,
            "jobs": [job_record(".github/workflows/ci.yml", sha, run_id=210)],
        }
    ]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 0, payload
    ci_evidence = next(item for item in payload["workflow_runs"] if item["workflow"] == "ci")
    assert ci_evidence["run_id"] == 210
    assert ci_evidence["run_started_at"] == "2026-09-09T12:00:00Z"


def test_chronologically_later_pending_older_run_rerun_blocks(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    later_pending_rerun = run_record(
        ".github/workflows/ci.yml",
        sha,
        run_id=200,
        status="in_progress",
        conclusion=None,
        attempt=2,
        number=20,
        started_at="2026-09-09T12:00:00Z",
    )
    earlier_success = run_record(
        ".github/workflows/ci.yml",
        sha,
        run_id=210,
        number=21,
        started_at="2026-09-09T11:00:00Z",
    )
    responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"] = [
        {"total_count": 2, "workflow_runs": [earlier_success, later_pending_rerun]}
    ]
    responses[f"repos/{REPOSITORY}/actions/runs/210/attempts/1/jobs"] = [
        {
            "total_count": 1,
            "jobs": [job_record(".github/workflows/ci.yml", sha, run_id=210)],
        }
    ]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "workflow_run_pending" in error_codes(payload)
    assert payload["errors"][0]["run_id"] == 200


@pytest.mark.parametrize(
    "chronology",
    [
        None,
        "not-a-timestamp",
        "2026-09-09T12:00:00",
        "2026-09-09T12:00:00+01:00",
    ],
)
def test_missing_or_malformed_run_chronology_fails_closed(tmp_path, chronology):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    run = responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"][0]["workflow_runs"][0]
    if chronology is None:
        del run["run_started_at"]
    else:
        run["run_started_at"] = chronology

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "incomplete_response" in error_codes(payload)
    assert "run_started_at" in payload["errors"][0]["message"]


def test_tied_latest_run_chronology_fails_closed(tmp_path):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    first = run_record(
        ".github/workflows/ci.yml",
        sha,
        run_id=200,
        number=20,
        started_at="2026-09-09T12:00:00Z",
    )
    second = run_record(
        ".github/workflows/ci.yml",
        sha,
        run_id=210,
        number=21,
        started_at="2026-09-09T12:00:00Z",
    )
    responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"] = [
        {"total_count": 2, "workflow_runs": [first, second]}
    ]
    responses[f"repos/{REPOSITORY}/actions/runs/210/attempts/1/jobs"] = [
        {
            "total_count": 1,
            "jobs": [job_record(".github/workflows/ci.yml", sha, run_id=210)],
        }
    ]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert "workflow_run_chronology_ambiguous" in error_codes(payload)
    assert {item["run_id"] for item in payload["errors"][0]["runs"]} == {200, 210}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_branch", "release-health"),
        ("event", "workflow_dispatch"),
        ("path", ".github/workflows/health.yml"),
    ],
)
def test_wrong_branch_event_or_path_is_not_applicable_evidence(tmp_path, field, value):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    wrong = run_record(".github/workflows/ci.yml", sha)
    wrong[field] = value
    responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"] = [
        {"total_count": 1, "workflow_runs": [wrong]}
    ]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert error_codes(payload) & {"workflow_run_absent", "workflow_run_identity_mismatch"}


@pytest.mark.parametrize("failure", ["remote", "malformed", "incomplete"])
def test_remote_errors_and_malformed_or_incomplete_responses_fail_closed(tmp_path, failure):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    if failure == "remote":
        responses[f"repos/{REPOSITORY}"] = {"__exit__": 1}
    elif failure == "malformed":
        responses[f"repos/{REPOSITORY}/actions/workflows"] = {"__raw__": "not-json"}
    else:
        del responses[f"repos/{REPOSITORY}/actions/workflows/101/runs"][0]["workflow_runs"][0][
            "check_suite_id"
        ]

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert payload["ok"] is False
    assert error_codes(payload) & {
        "github_api_error",
        "github_api_malformed",
        "incomplete_response",
    }
    assert payload["next_action"]


@pytest.mark.parametrize("local_state", ["dirty", "wrong-branch"])
def test_local_preflight_requires_clean_actual_default_branch(tmp_path, local_state):
    repo, _, sha = make_repo(tmp_path)
    if local_state == "dirty":
        (repo / "app.py").write_text("VALUE = 2\n")
    else:
        git(repo, "checkout", "-b", "feature")

    result, payload, _ = run_preflight(
        tmp_path, base_responses(sha), "local", repo, "--repo", REPOSITORY
    )

    assert result.returncode == 1
    expected = "working_tree_dirty" if local_state == "dirty" else "not_default_branch"
    assert expected in error_codes(payload)


def test_local_preflight_requires_candidate_to_be_exact_remote_head(tmp_path):
    repo, _, remote_sha = make_repo(tmp_path)
    local_sha = commit(repo, "unpushed.py")

    result, payload, _ = run_preflight(
        tmp_path, base_responses(remote_sha), "local", repo, "--repo", REPOSITORY
    )

    assert result.returncode == 1
    assert payload["candidate_sha"] == local_sha
    assert "candidate_not_remote_head" in error_codes(payload)


@pytest.mark.parametrize(
    ("catalog_state", "expected_code"),
    [
        ("absent", "publication_workflow_missing"),
        ("disabled", "publication_workflow_disabled"),
        ("wrong-path", "publication_workflow_missing"),
    ],
)
def test_local_preflight_requires_exact_active_release_workflow(
    tmp_path, catalog_state, expected_code
):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    catalog = responses[f"repos/{REPOSITORY}/actions/workflows"][0]
    release = next(
        item
        for item in catalog["workflows"]
        if item["id"] == WORKFLOWS[".github/workflows/release.yml"]
    )
    if catalog_state == "absent":
        catalog["workflows"].remove(release)
        catalog["total_count"] -= 1
    elif catalog_state == "disabled":
        release["state"] = "disabled_manually"
    else:
        release["path"] = ".github/workflows/publish.yml"

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert expected_code in error_codes(payload)
    assert ".github/workflows/release.yml" in payload["next_action"]
    assert "default branch" in payload["next_action"].lower()


@pytest.mark.parametrize(
    ("helper_state", "expected_code"),
    [
        ("absent", "publication_helper_unavailable"),
        ("wrong-path", "publication_helper_invalid"),
        ("wrong-sha", "publication_helper_invalid"),
    ],
)
def test_local_preflight_requires_exact_publication_helper(tmp_path, helper_state, expected_code):
    repo, _, sha = make_repo(tmp_path)
    responses = base_responses(sha, secret_guard=False, codeql=False)
    endpoint = f"repos/{REPOSITORY}/contents/{PUBLICATION_HELPER}"
    if helper_state == "absent":
        responses[endpoint] = {"__exit__": 1}
    elif helper_state == "wrong-path":
        responses[endpoint]["path"] = ".sdlc/scripts/not-the-publisher.py"
    else:
        responses[endpoint]["sha"] = "not-a-blob-sha"

    result, payload, _ = run_preflight(tmp_path, responses, "local", repo, "--repo", REPOSITORY)

    assert result.returncode == 1
    assert expected_code in error_codes(payload)
    assert PUBLICATION_HELPER in payload["next_action"]
    assert "default branch" in payload["next_action"].lower()


def test_local_only_repository_cannot_claim_github_release_readiness(tmp_path):
    repo = tmp_path / "local-only"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "release-tests@example.invalid")
    git(repo, "config", "user.name", "Release Tests")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "file.txt").write_text("content\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")

    result, payload, calls = run_preflight(tmp_path, {}, "local", repo)

    assert result.returncode == 1
    assert "github_remote_missing" in error_codes(payload)
    assert "GitHub" in payload["next_action"]
    assert calls == []


def test_remote_tag_must_match_local_candidate_and_must_not_already_exist(tmp_path):
    repo, _, old_sha = make_repo(tmp_path)
    candidate = commit(repo, "candidate.py")
    git(repo, "push", "origin", "main")
    git(repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    responses = base_responses(candidate)
    responses[f"repos/{REPOSITORY}/git/matching-refs/tags/v1.0.0"] = [
        [
            {
                "ref": "refs/tags/v1.0.0",
                "object": {"type": "commit", "sha": old_sha},
            }
        ]
    ]

    result, payload, _ = run_preflight(
        tmp_path,
        responses,
        "local",
        repo,
        "--repo",
        REPOSITORY,
        "--tag",
        "v1.0.0",
    )

    assert result.returncode == 1
    assert "remote_tag_mismatch" in error_codes(payload)
    assert payload["candidate_sha"] == candidate


def add_remote_tag(responses: dict, tag: str, sha: str) -> None:
    responses[f"repos/{REPOSITORY}/git/matching-refs/tags/{tag}"] = [
        [{"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": sha}}]
    ]


def test_publish_rejects_tag_outside_default_branch_ancestry(tmp_path):
    repo, _, default_sha = make_repo(tmp_path)
    git(repo, "checkout", "--orphan", "outside")
    git(repo, "rm", "-rf", ".")
    (repo / "outside.txt").write_text("outside\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "outside")
    outside_sha = git(repo, "rev-parse", "HEAD")
    responses = base_responses(default_sha)
    add_remote_tag(responses, "v1.0.0", outside_sha)
    responses[f"repos/{REPOSITORY}/compare/{outside_sha}...{default_sha}"] = {
        "status": "diverged",
        "base_commit": {"sha": outside_sha},
        "merge_base_commit": {"sha": "f" * 40},
    }

    result, payload, _ = run_preflight(
        tmp_path, responses, "publish", "--repo", REPOSITORY, "--tag", "v1.0.0"
    )

    assert result.returncode == 1
    assert payload["candidate_sha"] == outside_sha
    assert "candidate_not_default_ancestor" in error_codes(payload)


def test_publish_candidate_argument_detects_remote_tag_change(tmp_path):
    _, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    add_remote_tag(responses, "v1.0.0", sha)

    result, payload, _ = run_preflight(
        tmp_path,
        responses,
        "publish",
        "--repo",
        REPOSITORY,
        "--tag",
        "v1.0.0",
        "--candidate",
        "e" * 40,
    )

    assert result.returncode == 1
    assert "tag_candidate_changed" in error_codes(payload)


def test_publish_accepts_exact_remote_tag_head_and_reports_run_urls(tmp_path):
    _, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    tag_object_sha = "a" * 40
    responses[f"repos/{REPOSITORY}/git/matching-refs/tags/v1.0.0"] = [
        [
            {
                "ref": "refs/tags/v1.0.0",
                "object": {"type": "tag", "sha": tag_object_sha},
            }
        ]
    ]
    responses[f"repos/{REPOSITORY}/git/tags/{tag_object_sha}"] = {
        "object": {"type": "commit", "sha": sha}
    }

    result, payload, _ = run_preflight(
        tmp_path,
        responses,
        "publish",
        "--repo",
        REPOSITORY,
        "--tag",
        "v1.0.0",
        "--candidate",
        sha,
    )

    assert result.returncode == 0, payload
    assert payload["ok"] is True
    assert payload["candidate_sha"] == sha
    assert payload["tag"] == "v1.0.0"
    assert len(payload["workflow_runs"]) == 3
    assert all(run["url"] for run in payload["workflow_runs"])


def test_verify_release_binds_the_exact_tag_run_and_release(tmp_path):
    _, _, sha = make_repo(tmp_path)
    responses = base_responses(sha)
    add_remote_tag(responses, "v1.0.0", sha)
    release_path = ".github/workflows/release.yml"
    release_run = run_record(release_path, sha, branch="v1.0.0")
    responses[f"repos/{REPOSITORY}/actions/workflows/104/runs"] = [
        {"total_count": 1, "workflow_runs": [release_run]}
    ]
    responses[f"repos/{REPOSITORY}/actions/runs/1040/attempts/1/jobs"] = [
        {"total_count": 1, "jobs": [job_record(release_path, sha)]}
    ]
    responses[f"repos/{REPOSITORY}/releases/tags/v1.0.0"] = {
        "tag_name": "v1.0.0",
        "draft": False,
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/v1.0.0",
    }

    result, payload, _ = run_preflight(
        tmp_path,
        responses,
        "verify-release",
        "--repo",
        REPOSITORY,
        "--tag",
        "v1.0.0",
        "--candidate",
        sha,
    )

    assert result.returncode == 0, payload
    assert payload["release"]["head_sha"] == sha
    assert payload["release"]["url"].endswith("/releases/tag/v1.0.0")
    release_evidence = [run for run in payload["workflow_runs"] if run["path"] == release_path]
    assert len(release_evidence) == 1
    assert release_evidence[0]["head_branch"] == "v1.0.0"
    assert release_evidence[0]["head_sha"] == sha


def test_release_workflow_executes_trusted_preflight_before_publishing():
    workflow = WORKFLOW_TEMPLATE.read_text()
    permissions = workflow.split("\npermissions:\n", 1)[1].split("\njobs:\n", 1)[0]

    assert "actions/checkout" not in workflow
    assert permissions == "  actions: read\n  checks: read\n  contents: write\n"
    assert "checks: write" not in workflow
    assert ".sdlc/scripts/release-preflight.py" in workflow
    assert "--paginate" not in workflow
    assert '--candidate "$GITHUB_SHA"' in workflow
    assert "gh release create" in workflow
    assert '--repo "$GITHUB_REPOSITORY"' in workflow
    assert "--verify-tag" in workflow
    assert workflow.index("release-preflight.py") < workflow.index("gh release create")


def test_release_workflow_publish_block_passes_explicit_repository_from_bare_cwd(tmp_path):
    scratch = tmp_path / "bare"
    scratch.mkdir()
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    helper = runner_temp / "release-preflight.py"
    helper.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

expected = [
    "publish",
    "--repo",
    os.environ["GITHUB_REPOSITORY"],
    "--tag",
    os.environ["GITHUB_REF_NAME"],
    "--candidate",
    os.environ["GITHUB_SHA"],
]
if sys.argv[1:] != expected:
    raise SystemExit(2)
print(json.dumps({"ok": True, "candidate_sha": os.environ["GITHUB_SHA"]}))
"""
    )
    bin_dir = tmp_path / "workflow-bin"
    bin_dir.mkdir()
    gh_log = tmp_path / "release-gh-calls.jsonl"
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] != ["release", "create"]:
    raise SystemExit(2)
if "--repo" not in args:
    print(
        "failed to run git: fatal: not a git repository "
        "(or any of the parent directories): .git",
        file=sys.stderr,
    )
    raise SystemExit(1)
repository = args[args.index("--repo") + 1]
if repository != os.environ["GITHUB_REPOSITORY"]:
    raise SystemExit(2)
"""
    )
    fake_gh.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "FAKE_GH_LOG": str(gh_log),
            "GH_TOKEN": "test-token",
            "GITHUB_REF_NAME": "v1.2.3",
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_SHA": "a" * 40,
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
        }
    )

    result = subprocess.run(
        ["bash", "-c", workflow_run_block("Verify the exact tag and publish")],
        cwd=scratch,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in gh_log.read_text().splitlines()]
    assert calls == [
        [
            "release",
            "create",
            "v1.2.3",
            "--repo",
            REPOSITORY,
            "--target",
            "a" * 40,
            "--generate-notes",
            "--verify-tag",
        ]
    ]


def test_release_skill_uses_portable_deterministic_preflight_and_keeps_approval_boundary():
    skill = (ROOT / "skills" / "shipshape-release" / "SKILL.md").read_text()
    checklist = (
        ROOT / "skills" / "shipshape-release" / "references" / "release-checklist.md"
    ).read_text()

    assert "SHIPSHAPE_ROOT" in skill
    assert '"${SHIPSHAPE_ROOT}/scripts/release_preflight.py"' in skill
    assert skill.count("release_preflight.py") >= 2
    assert "/tmp/shipshape-release-preflight.json" not in skill
    assert "mktemp" in skill
    assert 'rm -f -- "$preflight_file"' in skill
    assert "gh run list --branch" not in skill
    assert "gh run list --workflow" not in checklist
    assert "approval" in skill.lower() or "yes" in skill.lower()
    assert "exact" in checklist.lower()
    assert "run_started_at" in checklist
    assert ".sdlc/scripts/release-preflight.py" in checklist
    assert "immutable default-branch snapshot" in checklist
    assert "prove a new candidate" in checklist
