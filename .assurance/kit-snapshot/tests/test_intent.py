"""Tests for creating lightweight, user-owned change intent files."""

import json
from datetime import date

import pytest
from conftest import run_script

VALID_INTENT = {
    "problem": "Reviewers cannot tell what outcome this change should produce.",
    "constraints": "Keep the workflow optional and use only the standard library.",
    "non_goals": "Do not introduce a full specification system.",
    "acceptance_checks": [
        "The intent file is stored under docs/changes/.",
        "A second create attempt leaves the file untouched.",
    ],
}


def write_input(tmp_path, payload=VALID_INTENT):
    input_path = tmp_path / "intent.json"
    input_path.write_text(json.dumps(payload))
    return input_path


def expected_path(slug="clarify-review-target"):
    return f"docs/changes/{date.today().isoformat()}-{slug}.md"


def test_create_writes_all_four_sections_and_reports_success(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    code, result = run_script(
        "intent.py",
        "create",
        repo,
        "--slug",
        "clarify-review-target",
        "--input",
        write_input(tmp_path),
    )

    relative_path = expected_path()
    assert code == 0
    assert result == {
        "ok": True,
        "action": "created",
        "path": relative_path,
        "next_action": "Begin implementation against this intent.",
    }
    content = (repo / relative_path).read_text()
    assert "## Problem\n\n" + VALID_INTENT["problem"] in content
    assert "## Constraints\n\n" + VALID_INTENT["constraints"] in content
    assert "## Non-goals\n\n" + VALID_INTENT["non_goals"] in content
    assert "## Acceptance checks\n\n- " + VALID_INTENT["acceptance_checks"][0] in content
    assert "- " + VALID_INTENT["acceptance_checks"][1] in content
    assert "managed-by: shipshape" not in content


def test_create_makes_docs_changes_directory_when_absent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert not (repo / "docs").exists()

    code, result = run_script(
        "intent.py",
        "create",
        repo,
        "--slug",
        "add-health-check",
        "--input",
        write_input(tmp_path),
    )

    assert code == 0, result
    assert (repo / "docs" / "changes").is_dir()
    assert (repo / expected_path("add-health-check")).is_file()


def test_create_refuses_an_existing_destination(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    input_path = write_input(tmp_path)
    args = (
        "create",
        repo,
        "--slug",
        "clarify-review-target",
        "--input",
        input_path,
    )
    first_code, first = run_script("intent.py", *args)
    destination = repo / first["path"]
    original = destination.read_text()

    second_code, second = run_script("intent.py", *args)

    assert first_code == 0
    assert second_code == 1
    assert second["ok"] is False
    assert second["action"] == "exists"
    assert second["path"] == expected_path()
    assert "exists" in second["next_action"].lower()
    assert destination.read_text() == original


@pytest.mark.parametrize(
    ("missing", "field_name"),
    [
        ("problem", "problem"),
        ("constraints", "constraints"),
        ("non_goals", "non-goals"),
        ("acceptance_checks", "acceptance checks"),
    ],
)
def test_create_reports_each_missing_field(tmp_path, missing, field_name):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = {key: value for key, value in VALID_INTENT.items() if key != missing}

    code, result = run_script(
        "intent.py",
        "create",
        repo,
        "--slug",
        "clarify-review-target",
        "--input",
        write_input(tmp_path, payload),
    )

    assert code == 1
    assert result["ok"] is False
    assert field_name in result["next_action"].lower()


@pytest.mark.parametrize(
    ("field", "value", "field_name"),
    [
        ("problem", "", "problem"),
        ("constraints", "   ", "constraints"),
        ("non_goals", 42, "non-goals"),
        ("acceptance_checks", [], "acceptance checks"),
        ("acceptance_checks", ["passes", ""], "acceptance checks"),
        ("acceptance_checks", "passes", "acceptance checks"),
    ],
)
def test_create_reports_each_empty_or_invalid_field(tmp_path, field, value, field_name):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = {**VALID_INTENT, field: value}

    code, result = run_script(
        "intent.py",
        "create",
        repo,
        "--slug",
        "clarify-review-target",
        "--input",
        write_input(tmp_path, payload),
    )

    assert code == 1
    assert result["ok"] is False
    assert field_name in result["next_action"].lower()


def test_create_rejects_an_unexpected_field(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = {**VALID_INTENT, "solution": "Add more process."}

    code, result = run_script(
        "intent.py",
        "create",
        repo,
        "--slug",
        "clarify-review-target",
        "--input",
        write_input(tmp_path, payload),
    )

    assert code == 1
    assert result["ok"] is False
    assert "solution" in result["next_action"].lower()


def test_create_reports_a_directory_creation_failure_as_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").write_text("this path blocks the docs directory")

    code, result = run_script(
        "intent.py",
        "create",
        repo,
        "--slug",
        "clarify-review-target",
        "--input",
        write_input(tmp_path),
    )

    assert code == 1
    assert result["ok"] is False
    assert "write access" in result["next_action"].lower()


def test_create_reports_non_utf8_input_as_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    input_path = tmp_path / "intent.json"
    input_path.write_bytes(b"\xff")

    code, result = run_script(
        "intent.py",
        "create",
        repo,
        "--slug",
        "clarify-review-target",
        "--input",
        input_path,
    )

    assert code == 1
    assert result["ok"] is False
    assert "json" in result["next_action"].lower()


def test_help_preserves_the_json_only_stdout_contract():
    code, result = run_script("intent.py", "--help")

    assert code == 1
    assert result["ok"] is False
    assert "use:" in result["next_action"].lower()


@pytest.mark.parametrize(
    "slug",
    ["", "Uppercase", "two_words", "-leading", "trailing-", "two--hyphens"],
)
def test_create_rejects_a_non_kebab_slug(tmp_path, slug):
    repo = tmp_path / "repo"
    repo.mkdir()

    code, result = run_script(
        "intent.py",
        "create",
        repo,
        "--slug",
        slug,
        "--input",
        write_input(tmp_path),
    )

    assert code == 1
    assert result["ok"] is False
    assert "slug" in result["next_action"].lower()
