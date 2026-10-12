"""Render tests: every fixture gets a full plan + apply into a temp copy.

Asserts the manifest's destinations are honored, no {{TOKEN}} survives into
written output, every managed file self-identifies, and the right CI variant
is chosen per detected stack.
"""

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest
from conftest import init_repo, run_script

BASE_FILES = {
    "docs/sdlc/harness.md",
    "CLAUDE.md",
    "docs/sdlc/design.md",
    ".github/workflows/ci.yml",
    "SECURITY.md",
    "docs/sdlc/security.md",
    ".sdlc/hooks/secret-guard.sh",
    ".sdlc/hooks/install.sh",
    ".pre-commit-config.yaml",
    ".github/dependabot.yml",
    ".github/workflows/release.yml",
    "docs/sdlc/glossary.md",
    "AGENTS.md",
    ".github/workflows/secret-scan.yml",
    ".github/workflows/copilot-setup-steps.yml",
    ".github/agents/reviewer.md",
    ".github/copilot-instructions.md",
    ".sdlc/scripts/doctor.py",
    ".sdlc/scripts/review-gates.py",
    ".sdlc/scripts/release-preflight.py",
    ".github/workflows/shipshape-doctor.yml",
    ".sdlc/templates/change-intent.md",
}
CODEQL = ".github/workflows/codeql.yml"
LOW_RISK_AUTOMERGE = ".github/workflows/low-risk-automerge.yml"
REVIEW_GATES = ".sdlc/scripts/review-gates.py"

# fixture name -> (has codeql, marker expected inside the rendered CI workflow)
FIXTURE_EXPECT = {
    "repo-python": (True, "actions/setup-python"),
    "repo-node": (True, "actions/setup-node"),
    "repo-go": (True, "actions/setup-go"),
    "repo-empty": (False, "No tests configured yet"),
}


def expected_files(fixture: str) -> set[str]:
    has_codeql, _ = FIXTURE_EXPECT[fixture]
    return BASE_FILES | ({CODEQL} if has_codeql else set())


@pytest.mark.parametrize("fixture", sorted(FIXTURE_EXPECT))
def test_full_apply(fixture, tmp_path):
    repo = init_repo(fixture, tmp_path)

    code, plan = run_script("render.py", "plan", repo)
    assert code == 0
    assert plan["ok"], plan.get("errors")
    assert not plan["conflicts"]
    assert {w["path"] for w in plan["writes"]} == expected_files(fixture)

    code, result = run_script("render.py", "apply", repo)
    assert code == 0 and result["ok"]
    assert set(result["written"]) == expected_files(fixture)

    # GitHub Actions' own ${{ ... }} expressions are legitimate output; only
    # shipshape's uppercase {{TOKEN}} placeholders must not survive.
    token_re = re.compile(r"\{\{[A-Z0-9_]+\}\}")
    for rel in expected_files(fixture):
        content = (repo / rel).read_text()
        assert not token_re.search(content), f"unsubstituted token left in {rel}"
        assert "managed-by: shipshape" in content, f"missing managed header in {rel}"

    ci = (repo / ".github/workflows/ci.yml").read_text()
    assert FIXTURE_EXPECT[fixture][1] in ci

    state = (repo / ".sdlc/state.json").read_text()
    for rel in expected_files(fixture):
        assert rel in state


def test_hook_scripts_are_executable(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    run_script("render.py", "apply", repo)
    for rel in (".sdlc/hooks/secret-guard.sh", ".sdlc/hooks/install.sh"):
        assert os.access(repo / rel, os.X_OK), f"{rel} is not executable"


def test_rendered_doctor_is_executable(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    run_script("render.py", "apply", repo)
    doctor = repo / ".sdlc" / "scripts" / "doctor.py"
    assert os.access(doctor, os.X_OK), f"{doctor} is not executable"


def test_codeql_language_and_dependabot_ecosystems(tmp_path):
    repo = init_repo("repo-node", tmp_path)
    run_script("render.py", "apply", repo)
    codeql = (repo / CODEQL).read_text()
    assert 'languages: "javascript-typescript"' in codeql
    dependabot = (repo / ".github/dependabot.yml").read_text()
    for ecosystem in ("github-actions", "npm"):
        assert f'package-ecosystem: "{ecosystem}"' in dependabot


def test_feature_flags_gate_security_files(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["features.secret_guard=false", "features.dependabot=false"],
    )
    code, result = run_script("render.py", "apply", repo)
    assert code == 0
    written = set(result["written"])
    assert ".sdlc/hooks/secret-guard.sh" not in written
    assert ".pre-commit-config.yaml" not in written
    assert ".github/workflows/secret-scan.yml" not in written
    assert ".github/dependabot.yml" not in written
    assert "SECURITY.md" in written  # policy doc is not feature-gated


def test_github_agents_flag_gates_agent_surface(tmp_path):
    repo = init_repo("repo-python", tmp_path, overrides=["features.github_agents=false"])
    code, result = run_script("render.py", "apply", repo)
    assert code == 0
    written = set(result["written"])
    assert ".github/workflows/copilot-setup-steps.yml" not in written
    assert ".github/agents/reviewer.md" not in written
    assert ".github/copilot-instructions.md" not in written
    assert "AGENTS.md" in written  # the adapter is always written


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (["workflow_style=pr", "features.tiered_review=true"], True),
        (["workflow_style=pr", "features.tiered_review=false"], False),
        (
            [
                "workflow_style=trunk",
                "profile=solo",
                "features.tiered_review=true",
            ],
            False,
        ),
    ],
)
def test_tiered_review_requires_pr_style_and_feature_flag(tmp_path, overrides, expected):
    repo = init_repo("repo-python", tmp_path, overrides=overrides)
    code, result = run_script("render.py", "apply", repo)

    assert code == 0, result
    assert (LOW_RISK_AUTOMERGE in result["written"]) is expected
    assert (repo / LOW_RISK_AUTOMERGE).is_file() is expected
    assert REVIEW_GATES in result["written"]
    assert (repo / REVIEW_GATES).is_file()


def test_disabling_tiered_review_removes_the_managed_workflow(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.tiered_review=true"],
    )
    code, enabled = run_script("render.py", "apply", repo)
    assert code == 0, enabled
    assert (repo / LOW_RISK_AUTOMERGE).is_file()

    code, _ = run_script("render.py", "set-config", repo, "--set", "features.tiered_review=false")
    assert code == 0
    code, plan = run_script("render.py", "plan", repo)
    assert code == 0, plan
    assert [item["path"] for item in plan["removals"]] == [LOW_RISK_AUTOMERGE]

    code, disabled = run_script("render.py", "apply", repo)
    assert code == 0, disabled
    assert disabled["removed"] == [LOW_RISK_AUTOMERGE]
    assert (repo / REVIEW_GATES).is_file()
    assert not (repo / LOW_RISK_AUTOMERGE).exists()


def test_disabling_tiered_review_never_deletes_a_drifted_workflow(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.tiered_review=true"],
    )
    code, enabled = run_script("render.py", "apply", repo)
    assert code == 0, enabled
    workflow = repo / LOW_RISK_AUTOMERGE
    workflow.write_text(workflow.read_text() + "\n# owner edit\n")
    code, _ = run_script("render.py", "set-config", repo, "--set", "features.tiered_review=false")
    assert code == 0

    code, disabled = run_script("render.py", "apply", repo)

    assert code == 0, disabled
    assert disabled["removed"] == []
    assert (repo / REVIEW_GATES).is_file()
    assert workflow.is_file()
    assert [item["path"] for item in disabled["conflicts_untouched"]] == [LOW_RISK_AUTOMERGE]

    code, confirmed = run_script("render.py", "apply", repo, "--force", LOW_RISK_AUTOMERGE)
    assert code == 0, confirmed
    assert confirmed["removed"] == [LOW_RISK_AUTOMERGE]
    assert confirmed["forced"] == [LOW_RISK_AUTOMERGE]
    assert not workflow.exists()


def test_always_available_review_helper_preserves_owner_drift(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    code, applied = run_script("render.py", "apply", repo)
    assert code == 0, applied
    helper = repo / REVIEW_GATES
    owner_edit = helper.read_text() + "\n# owner edit\n"
    helper.write_text(owner_edit)

    code, reapplied = run_script("render.py", "apply", repo)

    assert code == 0, reapplied
    assert helper.read_text() == owner_edit
    assert [item["path"] for item in reapplied["conflicts_untouched"]] == [REVIEW_GATES]


def test_default_trunk_renders_passive_protection_inspector(tmp_path):
    repo = init_repo("repo-python", tmp_path, overrides=["features.branch_protection=false"])

    code, applied = run_script("render.py", "apply", repo)

    assert code == 0, applied
    helper = repo / REVIEW_GATES
    assert helper.is_file()
    module_name = "_rendered_default_trunk_review_gates"
    spec = importlib.util.spec_from_file_location(module_name, helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    config = json.loads((repo / ".sdlc/config.json").read_text())
    result = module.inspect_configured_protection(
        module.GitHubClient(),
        config,
        {
            ".github/workflows/ci.yml",
            ".github/workflows/secret-scan.yml",
            ".github/workflows/codeql.yml",
        },
    )
    assert result["ok"] is True
    assert result["requested"] is False


def test_tiered_review_workflow_never_executes_proposed_content(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.tiered_review=true"],
    )
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result

    workflow = (repo / LOW_RISK_AUTOMERGE).read_text()
    trigger = workflow.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
    permissions = workflow.split("\npermissions:\n", 1)[1].split("\nconcurrency:\n", 1)[0]

    assert "# managed-by: shipshape" in "\n".join(workflow.splitlines()[:5])
    assert "pull_request_target:" in trigger
    assert "workflow_run:" in trigger
    assert "edited" in trigger and "unlabeled" in trigger
    assert permissions == (
        "  actions: read\n  checks: read\n  contents: write\n  pull-requests: write\n"
    )
    assert not re.search(r"(?m)^\s*uses\s*:", workflow)
    for forbidden in (
        "actions/checkout",
        "github.event.pull_request.head",
        "github.head_ref",
        "head_ref",
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
    for untrusted_context in (
        "github.event.pull_request.title",
        "github.event.pull_request.body",
        "github.event.pull_request.head.ref",
        "github.event.pull_request.labels",
    ):
        assert untrusted_context not in workflow


def test_tiered_review_workflow_fails_closed_on_incomplete_metadata(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.tiered_review=true"],
    )
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result

    workflow = (repo / LOW_RISK_AUTOMERGE).read_text()
    assert "GITHUB_EVENT_PATH" in workflow
    assert "len(pulls) != 1" in workflow
    assert "json.loads" in workflow
    assert "git/commits/$TRUSTED_SHA" in workflow
    assert "git/trees/$tree_sha?recursive=1" in workflow
    assert "git/blobs/$blob_sha" in workflow
    assert 'item.get("mode") == "100644"' in workflow
    assert "base64.b64decode" in workflow


def test_tiered_review_workflow_pins_ordinary_documentation_only(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.tiered_review=true"],
    )
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result

    helper = Path(__file__).resolve().parent.parent / "templates/scripts/review-gates.py.tmpl"
    policy = helper.read_text()
    assert '"README.md"' in policy
    assert '"LICENSE.md"' not in policy
    assert '"CODE_OF_CONDUCT.md"' not in policy
    assert '"CONTRIBUTING.md"' not in policy
    assert "SENSITIVE_WORDS" in policy
    assert "previous_filename" in policy
    assert 'item.get("mode") == "100644"' in policy
    assert "dependabot[bot]" not in policy
    assert "semver-patch" not in policy


def test_tiered_review_workflow_has_no_automatic_break_glass(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.tiered_review=true"],
    )
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result

    workflow = (repo / LOW_RISK_AUTOMERGE).read_text()
    helper = Path(__file__).resolve().parent.parent / "templates/scripts/review-gates.py.tmpl"
    policy = workflow + helper.read_text()
    assert "break-glass-automerge" not in policy
    assert "gh pr comment" not in policy


def test_tiered_review_workflow_blocks_existing_persistent_auto_merge(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.tiered_review=true"],
    )
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result

    helper = Path(__file__).resolve().parent.parent / "templates/scripts/review-gates.py.tmpl"
    policy = helper.read_text()
    assert "autoMergeRequest" in policy
    assert "cancel auto-merge manually" in policy.lower()
    assert "disablePullRequestAutoMerge" not in policy
    assert "enablePullRequestAutoMerge" not in policy


def test_tiered_review_workflow_binds_merge_to_the_classified_head(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.tiered_review=true"],
    )
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result

    workflow = (repo / LOW_RISK_AUTOMERGE).read_text()
    helper = Path(__file__).resolve().parent.parent / "templates/scripts/review-gates.py.tmpl"
    policy = helper.read_text()
    assert "_same_candidate(snapshot, final_snapshot)" in policy
    assert '"sha": final_snapshot["head_sha"]' in policy
    assert '"merge_method": "squash"' in policy
    assert "--auto" not in workflow + policy


def run_tiered_review_classifier(tmp_path, *, author, body, files, commits):
    del tmp_path, author, body, commits
    from scripts import review_gates

    blob_sha = "f" * 40
    base_tree = {}
    head_tree = {}
    for item in files:
        path = item["filename"]
        entry = {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
        if item["status"] in {"modified", "removed"}:
            base_tree[path] = entry
        if item["status"] in {"added", "modified", "renamed"}:
            head_tree[path] = entry
        previous = item.get("previous_filename")
        if previous:
            base_tree[previous] = {
                "path": previous,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            }
    class_name, _ = review_gates.classify_documentation(files, base_tree, head_tree)
    return {"class_name": class_name}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/guides/guide.rst", "docs-only"),
        ("docs/guide.rst", "none"),
        ("docs/build.sh", "none"),
    ],
)
def test_tiered_review_docs_class_rejects_executable_paths(tmp_path, path, expected):
    result = run_tiered_review_classifier(
        tmp_path,
        author="contributor",
        body="",
        files=[{"filename": path, "status": "modified"}],
        commits=[],
    )

    assert result["class_name"] == expected


def test_tiered_review_dependency_class_rejects_human_appended_commit(tmp_path):
    result = run_tiered_review_classifier(
        tmp_path,
        author="dependabot[bot]",
        body="version-update:semver-patch",
        files=[{"filename": "package.json", "status": "modified"}],
        commits=[
            {
                "authors": [{"login": "dependabot[bot]"}],
                "messageBody": "",
                "messageHeadline": "Bump example from 1.0.0 to 1.0.1",
            },
            {
                "authors": [{"login": "maintainer"}],
                "messageBody": "",
                "messageHeadline": "Add install script",
            },
        ],
    )

    assert result["class_name"] == "none"


@pytest.mark.parametrize(
    "path",
    [
        ".sdlc/package.json",
        ".claude/package.json",
        ".vscode/package-lock.json",
        "docs/sdlc/requirements.txt",
        "docs/adr/build.gradle",
        "AGENTS.md",
        "CLAUDE.md",
        "SECURITY.md",
    ],
)
def test_tiered_review_dependency_class_rejects_protected_paths(tmp_path, path):
    result = run_tiered_review_classifier(
        tmp_path,
        author="dependabot[bot]",
        body="version-update:semver-patch",
        files=[{"filename": path, "status": "modified"}],
        commits=[
            {
                "authors": [{"login": "dependabot[bot]"}],
                "messageBody": "version-update:semver-patch",
                "messageHeadline": "Bump dependency",
            }
        ],
    )

    assert result["class_name"] == "none"


def test_copilot_setup_matches_language_and_reviewer_has_frontmatter(tmp_path):
    repo = init_repo("repo-go", tmp_path)
    run_script("render.py", "apply", repo)
    setup = (repo / ".github/workflows/copilot-setup-steps.yml").read_text()
    assert "actions/setup-go" in setup
    assert "copilot-setup-steps:" in setup, "job name is contractual"
    reviewer = (repo / ".github/agents/reviewer.md").read_text()
    assert reviewer.startswith("---\nname: reviewer\n")
    assert "description:" in reviewer


def test_init_config_captures_detection(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    config = json.loads((repo / ".sdlc/config.json").read_text())
    assert "python" in config["languages"]
    assert "pytest" in config["commands"]["test"]
    assert config["features"]["tiered_review"] is False


def test_init_config_set_overrides(tmp_path):
    repo = init_repo(
        "repo-python",
        tmp_path,
        overrides=["workflow_style=pr", "features.codeql=false", "commands.test=pytest -q"],
    )
    config = json.loads((repo / ".sdlc/config.json").read_text())
    assert config["workflow_style"] == "pr"
    assert config["features"]["codeql"] is False
    assert config["commands"]["test"] == "pytest -q"


def test_set_config_edits_in_place_and_replans(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    run_script("render.py", "apply", repo)

    code, result = run_script(
        "render.py", "set-config", repo, "--set", "features.release_automation=false"
    )
    assert code == 0
    assert result["config"]["features"]["release_automation"] is False
    # Detection-derived values survive a set-config edit.
    assert result["config"]["languages"] == ["python"]

    code, plan = run_script("render.py", "plan", repo)
    assert code == 0
    planned = {p["path"] for group in ("writes", "regenerates", "skips") for p in plan[group]}
    assert ".github/workflows/release.yml" not in planned


def test_empty_repo_gets_fallback_test_command(tmp_path):
    repo = init_repo("repo-empty", tmp_path)
    run_script("render.py", "apply", repo)
    ci = (repo / ".github/workflows/ci.yml").read_text()
    assert "No tests configured yet" in ci


def test_plan_without_config_is_friendly_error(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    code, payload = run_script("render.py", "plan", bare)
    assert code == 1
    assert "shipshape-init" in payload["error"]


def test_kit_templates_only_use_known_tokens():
    """Every {{TOKEN}} in every template must be one build_tokens can supply."""
    kit_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(kit_root / "scripts"))
    from render import build_tokens  # noqa: E402

    known = set(build_tokens({"languages": ["python"]}))
    token_re = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
    for tmpl in (kit_root / "templates").rglob("*.tmpl"):
        used = set(token_re.findall(tmpl.read_text()))
        unknown = used - known
        assert not unknown, f"{tmpl.name} uses unknown tokens: {unknown}"
