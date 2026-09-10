"""Workflow-level regressions for automatic-merge authorization safety."""

import base64
import hashlib
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from scripts import review_gates

KIT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_TEMPLATE = KIT_ROOT / "templates/github/workflows/low-risk-automerge.yml.tmpl"
HELPER_TEMPLATE = KIT_ROOT / "templates/scripts/review-gates.py.tmpl"
BLOB_SHA = "f" * 40
REPO = "example/project"
REPO_ID = 101
HEAD_REPO = "contributor/project"
HEAD_REPO_ID = 202
PR_NUMBER = 17
PR_ID = 1700
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


def workflow_step(name):
    workflow = WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
    section = workflow.split(f"      - name: {name}\n", 1)[1]
    script = section.split("        run: |\n", 1)[1]
    script = script.split("\n      - name:", 1)[0]
    return textwrap.dedent(script)


def fake_gh(tmp_path, responses):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    response_dir = tmp_path / "responses"
    response_dir.mkdir()
    manifest = {}
    for index, (path, payload) in enumerate(responses.items()):
        response_path = response_dir / f"{index}.json"
        response_path.write_text(json.dumps(payload), encoding="utf-8")
        manifest[path] = str(response_path)
    manifest_path = tmp_path / "gh-responses.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    executable = fake_bin / "gh"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

responses = json.loads(Path(os.environ["FAKE_GH_RESPONSES"]).read_text())
path = sys.argv[-1]
if path not in responses:
    print(f"unexpected gh API path: {path}", file=sys.stderr)
    raise SystemExit(1)
print(Path(responses[path]).read_text())
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return fake_bin, manifest_path


def run_workflow_step(tmp_path, name, responses, **environment):
    fake_bin, manifest = fake_gh(tmp_path, responses)
    env = os.environ.copy()
    env.update(
        {
            "FAKE_GH_RESPONSES": str(manifest),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            **environment,
        }
    )
    return subprocess.run(
        ["bash", "-c", workflow_step(name)],
        capture_output=True,
        text=True,
        env=env,
    )


def repo_ref(repo, repo_id):
    return {
        "id": repo_id,
        "name": repo.rsplit("/", 1)[1],
        "url": f"https://api.github.com/repos/{repo}",
    }


def resolved_pull():
    return {
        "id": PR_ID,
        "number": PR_NUMBER,
        "url": f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
        "head": {
            "ref": "docs-update",
            "sha": HEAD_SHA,
            "repo": {**repo_ref(HEAD_REPO, HEAD_REPO_ID), "full_name": HEAD_REPO},
        },
        "base": {
            "ref": "main",
            "sha": BASE_SHA,
            "repo": {**repo_ref(REPO, REPO_ID), "full_name": REPO},
        },
    }


def workflow_run_event():
    pull = resolved_pull()
    return {
        "action": "completed",
        "repository": {
            **repo_ref(REPO, REPO_ID),
            "full_name": REPO,
            "default_branch": "main",
        },
        "workflow_run": {
            "id": 9001,
            "repository": {**repo_ref(REPO, REPO_ID), "full_name": REPO},
            "pull_requests": [
                {
                    "id": PR_ID,
                    "number": PR_NUMBER,
                    "url": f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
                    "head": {
                        "ref": pull["head"]["ref"],
                        "sha": pull["head"]["sha"],
                        "repo": repo_ref(HEAD_REPO, HEAD_REPO_ID),
                    },
                    "base": {
                        "ref": pull["base"]["ref"],
                        "sha": pull["base"]["sha"],
                        "repo": repo_ref(REPO, REPO_ID),
                    },
                }
            ],
        },
    }


def git_blob_sha(content):
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()


def loader_responses(content, *, claimed_sha=None, encoded=None):
    blob_sha = claimed_sha or git_blob_sha(content)
    tree_sha = "d" * 40
    return {
        f"repos/{REPO}/git/commits/{BASE_SHA}": {
            "sha": BASE_SHA,
            "tree": {"sha": tree_sha},
        },
        f"repos/{REPO}/git/trees/{tree_sha}?recursive=1": {
            "truncated": False,
            "tree": [
                {
                    "path": ".sdlc/scripts/review-gates.py",
                    "type": "blob",
                    "mode": "100644",
                    "sha": blob_sha,
                }
            ],
        },
        f"repos/{REPO}/git/blobs/{blob_sha}": {
            "sha": blob_sha,
            "encoding": "base64",
            "size": len(content),
            "content": encoded or base64.b64encode(content).decode(),
        },
    }


def blob(path):
    return {"path": path, "mode": "100644", "type": "blob", "sha": BLOB_SHA}


def test_nested_agent_control_is_never_documentation():
    path = "docs/guides/AGENTS.md"
    classification, _ = review_gates.classify_documentation(
        [{"filename": path, "status": "modified"}],
        {path: blob(path)},
        {path: blob(path)},
    )

    assert classification == "none"


def test_rename_from_control_file_is_never_documentation():
    old_path = ".github/agents/reviewer.md"
    new_path = "docs/guide.md"
    classification, _ = review_gates.classify_documentation(
        [
            {
                "filename": new_path,
                "previous_filename": old_path,
                "status": "renamed",
            }
        ],
        {old_path: blob(old_path)},
        {new_path: blob(new_path)},
    )

    assert classification == "none"


def test_workflow_requires_live_source_bound_enforcement():
    helper = HELPER_TEMPLATE.read_text(encoding="utf-8")

    assert "/rules/branches/" in helper
    assert str(review_gates.GITHUB_ACTIONS_APP_ID) in helper
    assert str(review_gates.CODEQL_APP_ID) in helper
    assert "strict_required_status_checks_policy" in helper
    assert "current_user_can_bypass" in helper


def test_workflow_has_no_persistent_merge_authorization():
    workflow = WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
    helper = HELPER_TEMPLATE.read_text(encoding="utf-8")

    assert "--auto" not in workflow
    assert "--auto" not in helper
    assert '"merge_method": "squash"' in helper
    assert '"sha": final_snapshot["head_sha"]' in helper
    assert "workflow_run:" in workflow
    assert "disablePullRequestAutoMerge" not in helper
    assert "enablePullRequestAutoMerge" not in helper


def test_ordinary_push_workflow_completion_skips_evaluation_job():
    workflow = WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
    condition = workflow.split("  evaluate:\n    if: >-\n", 1)[1].split("\n    runs-on:", 1)[0]

    assert "github.event.workflow_run.event == 'pull_request'" in condition
    assert "github.event.workflow_run.event == 'pull_request_target'" in condition
    assert "github.event.workflow_run.pull_requests[0].number" in condition


def test_privileged_workflow_never_executes_candidate_content():
    workflow = WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
    permissions = workflow.split("\npermissions:\n", 1)[1].split("\nconcurrency:\n", 1)[0]

    assert permissions == (
        "  actions: read\n  checks: read\n  contents: write\n  pull-requests: write\n"
    )
    assert not re.search(r"(?m)^\s*uses\s*:", workflow)
    assert "git/commits/$TRUSTED_SHA" in workflow
    assert "git/trees/$tree_sha?recursive=1" in workflow
    assert "git/blobs/$blob_sha" in workflow
    assert 'item.get("mode") == "100644"' in workflow
    assert "trusted_sha={base['sha']}" in workflow
    for forbidden in (
        "actions/checkout",
        "github.event.pull_request.title",
        "github.event.pull_request.body",
        "github.event.pull_request.head.ref",
        "gh pr checkout",
        "gh pr diff",
        "git fetch",
        "git clone",
        "git show",
        "curl ",
        "wget ",
        "eval ",
        "source ",
        "/archive/",
        "actions/upload-artifact",
        "actions/cache",
    ):
        assert forbidden not in workflow


def test_dependency_and_break_glass_automation_are_absent():
    workflow = WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
    helper = HELPER_TEMPLATE.read_text(encoding="utf-8")
    policy = workflow + helper

    for forbidden in (
        "dependabot[bot]",
        "dependency-bot-patch",
        "semver-patch",
        "break-glass-automerge",
        "gh pr comment",
    ):
        assert forbidden not in policy


def test_embedded_workflow_run_selector_resolves_actual_repo_ref_shape(tmp_path):
    event_path = tmp_path / "event.json"
    output_path = tmp_path / "github-output"
    event_path.write_text(json.dumps(workflow_run_event()), encoding="utf-8")
    output_path.write_text("", encoding="utf-8")
    result = run_workflow_step(
        tmp_path,
        "Select one pull request and trusted policy commit",
        {f"repos/{REPO}/pulls/{PR_NUMBER}": resolved_pull()},
        GITHUB_EVENT_PATH=str(event_path),
        GITHUB_OUTPUT=str(output_path),
        GH_REPO=REPO,
        GH_TOKEN="fixture-token",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        f"pr={PR_NUMBER}",
        f"trusted_sha={BASE_SHA}",
        "workflow_run=9001",
    ]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("workflow_run", "repository", "id"), 999),
        (("workflow_run", "pull_requests", 0, "head", "repo", "id"), 999),
        (("workflow_run", "pull_requests", 0, "base", "repo", "url"), "https://evil.test"),
        (("workflow_run", "pull_requests", 0, "head", "sha"), "8" * 40),
        (("workflow_run", "pull_requests", 0, "base", "sha"), "7" * 40),
    ],
)
def test_embedded_workflow_run_selector_rejects_spoofed_or_stale_links(tmp_path, path, value):
    event = workflow_run_event()
    target = event
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value
    event_path = tmp_path / "event.json"
    output_path = tmp_path / "github-output"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    output_path.write_text("", encoding="utf-8")
    result = run_workflow_step(
        tmp_path,
        "Select one pull request and trusted policy commit",
        {f"repos/{REPO}/pulls/{PR_NUMBER}": resolved_pull()},
        GITHUB_EVENT_PATH=str(event_path),
        GITHUB_OUTPUT=str(output_path),
        GH_REPO=REPO,
        GH_TOKEN="fixture-token",
    )

    assert result.returncode != 0
    assert output_path.read_text(encoding="utf-8") == ""


def test_embedded_selector_rejects_consistent_non_default_policy_base(tmp_path):
    event = workflow_run_event()
    event["workflow_run"]["pull_requests"][0]["base"]["ref"] = "release"
    pull = resolved_pull()
    pull["base"]["ref"] = "release"
    event_path = tmp_path / "event.json"
    output_path = tmp_path / "github-output"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    output_path.write_text("", encoding="utf-8")
    result = run_workflow_step(
        tmp_path,
        "Select one pull request and trusted policy commit",
        {f"repos/{REPO}/pulls/{PR_NUMBER}": pull},
        GITHUB_EVENT_PATH=str(event_path),
        GITHUB_OUTPUT=str(output_path),
        GH_REPO=REPO,
        GH_TOKEN="fixture-token",
    )

    assert result.returncode != 0
    assert "default branch" in result.stdout.lower()
    assert output_path.read_text(encoding="utf-8") == ""


def test_embedded_loader_accepts_newline_wrapped_exact_git_blob(tmp_path):
    content = HELPER_TEMPLATE.read_bytes()
    result = run_workflow_step(
        tmp_path,
        "Load review policy from the fixed trusted base",
        loader_responses(content, encoded=base64.encodebytes(content).decode()),
        GH_REPO=REPO,
        GH_TOKEN="fixture-token",
        TRUSTED_SHA=BASE_SHA,
        RUNNER_TEMP=str(tmp_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "shipshape-review-gates.py").read_bytes() == content


def test_embedded_loader_rejects_false_claimed_git_blob_identity(tmp_path):
    trusted = HELPER_TEMPLATE.read_bytes()
    changed = trusted + b"\n# different content\n"
    claimed_sha = git_blob_sha(trusted)
    result = run_workflow_step(
        tmp_path,
        "Load review policy from the fixed trusted base",
        loader_responses(changed, claimed_sha=claimed_sha),
        GH_REPO=REPO,
        GH_TOKEN="fixture-token",
        TRUSTED_SHA=BASE_SHA,
        RUNNER_TEMP=str(tmp_path),
    )

    assert result.returncode != 0
    assert not (tmp_path / "shipshape-review-gates.py").exists()


def test_embedded_loader_rejects_base64_whitespace_other_than_line_wrapping(tmp_path):
    content = HELPER_TEMPLATE.read_bytes()
    encoded = base64.b64encode(content).decode()
    encoded = f"{encoded[:40]} {encoded[40:]}"
    result = run_workflow_step(
        tmp_path,
        "Load review policy from the fixed trusted base",
        loader_responses(content, encoded=encoded),
        GH_REPO=REPO,
        GH_TOKEN="fixture-token",
        TRUSTED_SHA=BASE_SHA,
        RUNNER_TEMP=str(tmp_path),
    )

    assert result.returncode != 0
    assert not (tmp_path / "shipshape-review-gates.py").exists()
