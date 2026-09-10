"""Doctor tests for bounded local evidence and fail-closed remote gates."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import init_repo, prepend_to_path, run_script


@pytest.fixture(autouse=True)
def pin_doctor_subprocess_path(hermetic_doctor_path):
    return hermetic_doctor_path


def healthy_repo(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    code, _ = run_script("render.py", "apply", repo)
    assert code == 0
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["features"]["branch_protection"] = False
    config_path.write_text(json.dumps(config))
    subprocess.run(["git", "init", "-qb", "main"], cwd=repo, check=True)
    # Activate the guard the way init's step 7 does.
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-commit").symlink_to("../../.sdlc/hooks/secret-guard.sh")
    return repo


def checks_by_name(payload, section):
    (match,) = [s for s in payload["sections"] if s["name"] == section]
    return {c["name"]: c for c in match["checks"]}


def install_fake_gh(tmp_path, response: dict | None, exit_code: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    output = json.dumps(response) if response is not None else ""
    gh.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {exit_code}\n")
    gh.chmod(0o755)
    return bin_dir


def install_routed_fake_gh(tmp_path):
    bin_dir = tmp_path / "routed-bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_GH_CALLS"\n'
        'case "$*" in\n'
        '  *"/rulesets/"*)\n'
        "    printf '%s\\n' \"${FAKE_GH_RULESET_BODY:-}\"\n"
        '    exit "${FAKE_GH_RULESET_CODE:-1}" ;;\n'
        '  *"/rules/branches/"*)\n'
        "    printf '%s\\n' \"${FAKE_GH_RULES_BODY:-}\"\n"
        '    exit "${FAKE_GH_RULES_CODE:-1}" ;;\n'
        "esac\n"
        "exit 1\n"
    )
    gh.chmod(0o755)
    calls = tmp_path / "doctor-gh-calls"
    calls.touch()
    return bin_dir, calls


def set_fake_gh_response(monkeypatch, name, payload, exit_code=0):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setenv(f"FAKE_GH_{name}_BODY", body)
    monkeypatch.setenv(f"FAKE_GH_{name}_CODE", str(exit_code))


def set_fake_gh_error(monkeypatch, name, message):
    monkeypatch.setenv(f"FAKE_GH_{name}_ERROR", message)


def configure_remote_protection(
    repo, *, workflow_style="pr", branch_protection=True, tiered_review=False
):
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow_style"] = workflow_style
    config["features"]["branch_protection"] = branch_protection
    config["features"]["tiered_review"] = tiered_review
    config["repo"]["owner_repo"] = "example/project"
    config_path.write_text(json.dumps(config))
    if tiered_review:
        path = repo / ".github" / "workflows" / "low-risk-automerge.yml"
        path.write_text("permissions:\n  contents: read\n  pull-requests: write\n")


def enable_tiered_review(repo, *, workflow=True):
    configure_remote_protection(repo, tiered_review=True)
    if not workflow:
        (repo / ".github" / "workflows" / "low-risk-automerge.yml").unlink()


def valid_ruleset(*, enforcement="active", strict=True, test_app_id=15368, tiered=True):
    checks = [{"context": "test", "integration_id": test_app_id}]
    if tiered:
        checks.append({"context": "secret-scan", "integration_id": 15368})
    checks.append({"context": "CodeQL", "integration_id": 57789})
    return {
        "id": 41,
        "name": ("Shipshape automatic merge safety" if tiered else "Shipshape branch protection"),
        "target": "branch",
        "source_type": "Repository",
        "source": "example/project",
        "enforcement": enforcement,
        "current_user_can_bypass": "never",
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": strict,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": checks,
                },
            }
        ],
    }


def valid_trunk_ruleset():
    return {
        "id": 41,
        "name": "Shipshape branch protection",
        "target": "branch",
        "source_type": "Repository",
        "source": "example/project",
        "enforcement": "active",
        "current_user_can_bypass": "never",
        "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
    }


def configure_protection_api(monkeypatch, tmp_path, *, rules=None, ruleset=None):
    bin_dir, calls = install_routed_fake_gh(tmp_path)
    prepend_to_path(monkeypatch, bin_dir)
    monkeypatch.setenv("FAKE_GH_CALLS", str(calls))
    set_fake_gh_response(
        monkeypatch,
        "RULES",
        rules if rules is not None else [{"type": "required_status_checks", "ruleset_id": 41}],
    )
    if ruleset is not None:
        set_fake_gh_response(monkeypatch, "RULESET", ruleset)
    return calls


def test_unset_repo_points_to_init(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    code, payload = run_script("doctor.py", bare)
    assert code == 1
    assert payload["set_up"] is False
    assert "shipshape-init" in payload["next_action"]


def test_healthy_repo_all_green(tmp_path):
    repo = healthy_repo(tmp_path)
    code, payload = run_script("doctor.py", repo)
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["counts"]["FAIL"] == 0
    assert payload["sections"][0]["name"] == "security", "security must lead"
    assert "shipshape" in payload["next_action"]


def test_remote_protection_passes_only_when_configuration_explicitly_does_not_request_it(
    tmp_path,
):
    repo = healthy_repo(tmp_path)

    code, payload = run_script("doctor.py", repo)

    protection = checks_by_name(payload, "security")["remote protection"]
    assert code == 0, payload
    assert protection == {
        "name": "remote protection",
        "status": "PASS",
        "detail": "remote branch protection is not requested by configuration",
        "next_action": "",
    }


def test_tiered_review_gates_fail_when_feature_is_off_but_workflow_remains(tmp_path):
    repo = healthy_repo(tmp_path)
    workflow = repo / ".github" / "workflows" / "low-risk-automerge.yml"
    workflow.write_text("name: unexpectedly active\n")

    code, payload = run_script("doctor.py", repo)

    tiered_review = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert tiered_review["next_action"] == (
        "Remove .github/workflows/low-risk-automerge.yml before relying on tiered review being off."
    )


def test_tiered_review_gates_fail_when_repository_identity_is_unavailable(tmp_path):
    repo = healthy_repo(tmp_path)
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow_style"] = "pr"
    config["features"]["branch_protection"] = True
    config["features"]["tiered_review"] = True
    config_path.write_text(json.dumps(config))
    workflow = repo / ".github" / "workflows" / "low-risk-automerge.yml"
    workflow.write_text("permissions:\n  contents: read\n  pull-requests: write\n")
    code, payload = run_script("doctor.py", repo)

    tiered_review = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert "repository must be an exact owner/name slug" in tiered_review["detail"]


def test_tiered_review_gates_fail_when_protection_api_is_inaccessible(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    enable_tiered_review(repo)
    bin_dir, calls = install_routed_fake_gh(tmp_path)
    prepend_to_path(monkeypatch, bin_dir)
    monkeypatch.setenv("FAKE_GH_CALLS", str(calls))
    set_fake_gh_response(monkeypatch, "RULES", "", exit_code=1)

    code, payload = run_script("doctor.py", repo)

    tiered_review = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert "unverified" in tiered_review["detail"]


def test_tiered_review_gates_pass_only_for_canonical_source_bound_ruleset(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    enable_tiered_review(repo)
    calls = configure_protection_api(monkeypatch, tmp_path, ruleset=valid_ruleset())

    code, payload = run_script("doctor.py", repo)

    tiered_review = checks_by_name(payload, "security")["remote protection"]
    assert code == 0, payload
    assert tiered_review["status"] == "PASS"
    assert "strict source-bound" in tiered_review["detail"]
    api_calls = calls.read_text()
    assert "rules/branches/main" in api_calls
    assert "rulesets/41" in api_calls
    assert "--method" not in api_calls
    assert "graphql" not in api_calls


def test_tiered_review_gates_fail_when_no_active_ruleset_exists(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    enable_tiered_review(repo)
    configure_protection_api(monkeypatch, tmp_path, rules=[])

    code, payload = run_script("doctor.py", repo)

    tiered_review = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert "active branch rules was empty" in tiered_review["detail"]


@pytest.mark.parametrize(
    ("ruleset", "reason"),
    [
        (valid_ruleset(enforcement="evaluate"), "disabled"),
        (valid_ruleset(strict=False), "not strict"),
        (valid_ruleset(test_app_id=999), "every expected source"),
    ],
)
def test_tiered_review_gates_fail_for_unsafe_ruleset(tmp_path, monkeypatch, ruleset, reason):
    repo = healthy_repo(tmp_path)
    enable_tiered_review(repo)
    configure_protection_api(monkeypatch, tmp_path, ruleset=ruleset)

    code, payload = run_script("doctor.py", repo)

    tiered_review = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert reason in tiered_review["detail"]


def test_tiered_review_gates_fail_when_workflow_is_missing(tmp_path):
    repo = healthy_repo(tmp_path)
    enable_tiered_review(repo, workflow=False)
    code, payload = run_script("doctor.py", repo)

    tiered_review = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert tiered_review["next_action"] == (
        "Turn off tiered review until every required check is enforced."
    )


def test_tiered_review_gates_fail_when_api_output_is_malformed(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    enable_tiered_review(repo)
    configure_protection_api(monkeypatch, tmp_path, rules="not-json")

    code, payload = run_script("doctor.py", repo)

    tiered_review = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert "malformed JSON" in tiered_review["detail"]


def test_non_tiered_pr_protection_is_verified_read_only(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    configure_remote_protection(repo)
    calls = configure_protection_api(monkeypatch, tmp_path, ruleset=valid_ruleset(tiered=False))

    code, payload = run_script("doctor.py", repo)

    protection = checks_by_name(payload, "security")["remote protection"]
    assert code == 0, payload
    assert protection["status"] == "PASS"
    assert "configured PR protection verified" in protection["detail"]
    api_calls = calls.read_text()
    assert "rules/branches/main" in api_calls
    assert "rulesets/41" in api_calls
    assert "--method" not in api_calls
    assert "graphql" not in api_calls


def test_trunk_protection_is_verified_without_inventing_status_checks(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    configure_remote_protection(repo, workflow_style="trunk")
    rules = [
        {"type": "deletion", "ruleset_id": 41},
        {"type": "non_fast_forward", "ruleset_id": 41},
    ]
    configure_protection_api(monkeypatch, tmp_path, rules=rules, ruleset=valid_trunk_ruleset())

    code, payload = run_script("doctor.py", repo)

    protection = checks_by_name(payload, "security")["remote protection"]
    assert code == 0, payload
    assert protection["status"] == "PASS"
    assert "configured trunk protection verified" in protection["detail"]
    assert "deletion and non-fast-forward safeguards" in protection["detail"]


def test_configured_applicable_codeql_missing_is_not_reported_as_protected(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    configure_remote_protection(repo)
    (repo / ".github/workflows/codeql.yml").unlink()
    bin_dir, calls = install_routed_fake_gh(tmp_path)
    prepend_to_path(monkeypatch, bin_dir)
    monkeypatch.setenv("FAKE_GH_CALLS", str(calls))

    code, payload = run_script("doctor.py", repo)

    protection = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert protection["status"] == "FAIL"
    assert "configured applicable CodeQL workflow was missing" in protection["detail"]
    assert calls.read_text() == ""


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("branch_protection", "false", "feature flags were malformed"),
        ("codeql", "false", "feature flags were malformed"),
        ("workflow_style", False, "workflow style"),
        ("owner_repo", False, "repository must be an exact owner/name slug"),
    ],
)
def test_malformed_protection_metadata_never_passes(tmp_path, field, value, reason):
    repo = healthy_repo(tmp_path)
    configure_remote_protection(repo)
    config_path = repo / ".sdlc/config.json"
    config = json.loads(config_path.read_text())
    if field in config["features"]:
        config["features"][field] = value
    elif field == "owner_repo":
        config["repo"][field] = value
    else:
        config[field] = value
    config_path.write_text(json.dumps(config))

    code, payload = run_script("doctor.py", repo)

    protection = checks_by_name(payload, "security")["remote protection"]
    assert code == 1
    assert protection["status"] == "FAIL"
    assert reason in protection["detail"]


def test_agent_control_file_coverage_passes_with_guard_and_reviewer(tmp_path):
    repo = healthy_repo(tmp_path)
    code, payload = run_script("doctor.py", repo)
    coverage = checks_by_name(payload, "security")["agent control-file coverage"]
    assert code == 0, payload
    assert coverage["status"] == "PASS"


def test_agent_control_file_coverage_warns_without_github_agents(tmp_path):
    repo = healthy_repo(tmp_path)
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["features"]["github_agents"] = False
    config_path.write_text(json.dumps(config))

    code, payload = run_script("doctor.py", repo)
    coverage = checks_by_name(payload, "security")["agent control-file coverage"]
    assert code == 0, payload
    assert coverage["status"] == "WARN"
    assert "deterministic" in coverage["detail"]


def test_agent_control_file_coverage_fails_for_stale_guard(tmp_path):
    repo = healthy_repo(tmp_path)
    guard = repo / ".sdlc" / "hooks" / "secret-guard.sh"
    guard.write_text(
        guard.read_text().replace("# Layer 3: automation and policy control files.", "")
    )

    code, payload = run_script("doctor.py", repo)
    coverage = checks_by_name(payload, "security")["agent control-file coverage"]
    assert code == 1
    assert coverage["status"] == "FAIL"
    assert coverage["next_action"] == (
        "Re-run /shipshape-init to restore checks for agent and editor control files."
    )


@pytest.mark.parametrize(
    "removed_rule",
    [
        "are security findings: report purpose and consequence.",
        'Unexplained control-file changes require at least "Needs attention first".',
    ],
)
def test_agent_control_file_coverage_fails_for_stale_reviewer(tmp_path, removed_rule):
    repo = healthy_repo(tmp_path)
    reviewer = repo / ".github" / "agents" / "reviewer.md"
    reviewer.write_text(reviewer.read_text().replace(removed_rule, ""))

    code, payload = run_script("doctor.py", repo)
    coverage = checks_by_name(payload, "security")["agent control-file coverage"]
    assert code == 1
    assert coverage["status"] == "FAIL"
    assert "reviewer" in coverage["detail"]


def test_uninstalled_guard_is_fail_with_fix(tmp_path):
    repo = healthy_repo(tmp_path)
    (repo / ".git" / "hooks" / "pre-commit").unlink()
    code, payload = run_script("doctor.py", repo)
    assert code == 1
    assert payload["ok"] is False
    check = checks_by_name(payload, "security")["secret guard installed"]
    assert check["status"] == "FAIL"
    assert "install.sh" in payload["next_action"]


def test_unrelated_pre_commit_hook_is_not_reported_as_guard(tmp_path):
    repo = healthy_repo(tmp_path)
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 1
    assert installed["status"] == "FAIL"
    assert "does not invoke" in installed["detail"]


def test_non_executable_guard_hook_is_not_reported_as_active(tmp_path):
    repo = healthy_repo(tmp_path)
    (repo / ".sdlc/hooks/secret-guard.sh").chmod(0o644)

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 1
    assert installed["status"] == "FAIL"
    assert "not executable" in installed["detail"]


def test_core_hooks_path_override_is_the_only_path_doctor_checks(tmp_path):
    repo = healthy_repo(tmp_path)
    subprocess.run(["git", "config", "core.hooksPath", ".active-hooks"], cwd=repo, check=True)

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 1
    assert installed["status"] == "FAIL"
    assert "active Git hook path" in installed["detail"]


def test_direct_guard_in_configured_hooks_path_is_verified(tmp_path):
    repo = healthy_repo(tmp_path)
    subprocess.run(["git", "config", "core.hooksPath", ".active hooks"], cwd=repo, check=True)
    hooks = repo / ".active hooks"
    hooks.mkdir()
    (hooks / "pre-commit").symlink_to(repo / ".sdlc/hooks/secret-guard.sh")

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 0, payload
    assert installed["status"] == "PASS"
    assert "contains the expected guard" in installed["detail"]


def test_verified_pre_commit_wiring_is_reported_as_scheduled(tmp_path):
    repo = healthy_repo(tmp_path)
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "# File generated by pre-commit\n"
        "ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)\n"
        'exec pre-commit "${ARGS[@]}"\n'
    )
    hook.chmod(0o755)

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 0, payload
    assert installed["status"] == "PASS"
    assert "supported local configuration" in installed["detail"]


def test_pre_commit_marker_text_without_dispatch_is_not_guard_wiring(tmp_path):
    repo = healthy_repo(tmp_path)
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "# File generated by pre-commit\n"
        "ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)\n"
        "exit 0\n"
    )
    hook.chmod(0o755)

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 1
    assert installed["status"] == "FAIL"


def test_incomplete_pre_commit_guard_entry_is_not_verified(tmp_path):
    repo = healthy_repo(tmp_path)
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "# File generated by pre-commit\n"
        "ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)\n"
        'exec pre-commit "${ARGS[@]}"\n'
    )
    hook.chmod(0o755)
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: secret-guard\n"
        "        entry: .sdlc/hooks/secret-guard.sh\n"
    )

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 1
    assert installed["status"] == "FAIL"


def test_pre_commit_dispatcher_with_unrelated_config_is_not_guard_wiring(tmp_path):
    repo = healthy_repo(tmp_path)
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "# File generated by pre-commit\n"
        "ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)\n"
        'exec pre-commit "${ARGS[@]}"\n'
    )
    hook.chmod(0o755)
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: unrelated\n"
        "        entry: scripts/unrelated.sh\n"
    )

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 1
    assert installed["status"] == "FAIL"


def test_disabled_guard_is_not_reported_as_running(tmp_path):
    repo = healthy_repo(tmp_path)
    config_path = repo / ".sdlc/config.json"
    config = json.loads(config_path.read_text())
    config["features"]["secret_guard"] = False
    config_path.write_text(json.dumps(config))

    code, payload = run_script("doctor.py", repo)

    security = checks_by_name(payload, "security")
    assert code == 0, payload
    assert security["secret guard"]["status"] == "WARN"
    assert security["secret guard installed"]["status"] == "WARN"
    assert "disabled" in security["secret guard installed"]["detail"]


def test_github_actions_reports_configuration_without_claiming_local_hook(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 0, payload
    assert installed["status"] == "WARN"
    assert "configured" in installed["detail"]
    assert "local hook was not verified" in installed["detail"]


def test_unknown_active_hook_path_is_warn(tmp_path, monkeypatch):
    repo = healthy_repo(tmp_path)
    bin_dir = tmp_path / "failing-git"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text("#!/bin/sh\nexit 71\n")
    git.chmod(0o755)
    prepend_to_path(monkeypatch, bin_dir)

    code, payload = run_script("doctor.py", repo)

    installed = checks_by_name(payload, "security")["secret guard installed"]
    assert code == 0, payload
    assert installed["status"] == "WARN"
    assert "unknown" in installed["detail"]


def test_presence_checks_state_their_inspection_limits(tmp_path):
    repo = healthy_repo(tmp_path)

    code, payload = run_script("doctor.py", repo)

    security = checks_by_name(payload, "security")
    setup = checks_by_name(payload, "setup")
    assert code == 0, payload
    assert "does not execute it or prove behavioural security" in security["secret guard"]["detail"]
    assert (
        "does not query vulnerability alerts" in security["dependency watch (Dependabot)"]["detail"]
    )
    assert "no run was observed" in setup["automated tests (CI)"]["detail"]
    assert "no pre-landing enforcement is proved" in setup["automated tests (CI)"]["detail"]


def test_missing_dependabot_is_warn_not_fail(tmp_path):
    repo = healthy_repo(tmp_path)
    (repo / ".github" / "dependabot.yml").unlink()
    code, payload = run_script("doctor.py", repo)
    assert code == 0, "availability gaps must not hard-fail"
    check = checks_by_name(payload, "security")["dependency watch (Dependabot)"]
    assert check["status"] == "WARN"


def test_workflow_without_permissions_is_flagged(tmp_path):
    repo = healthy_repo(tmp_path)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "name: Deploy\non: push\njobs:\n  d:\n    runs-on: ubuntu-latest\n    steps: []\n"
    )
    code, payload = run_script("doctor.py", repo)
    check = checks_by_name(payload, "security")["workflow permissions"]
    assert check["status"] == "WARN"
    assert "deploy.yml" in check["detail"]


def test_unused_project_board_setting_is_warn_not_fail(tmp_path):
    repo = healthy_repo(tmp_path)
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["features"]["project_board"] = False
    config_path.write_text(json.dumps(config))

    code, payload = run_script("doctor.py", repo)
    check = checks_by_name(payload, "setup")["unused settings"]
    message = (
        "Remove features.project_board; this setting has never changed what shipshape installs."
    )
    assert code == 0
    assert check == {
        "name": "unused settings",
        "status": "WARN",
        "detail": message,
        "next_action": message,
    }


def test_no_unused_settings_is_pass(tmp_path):
    repo = healthy_repo(tmp_path)
    code, payload = run_script("doctor.py", repo)
    check = checks_by_name(payload, "setup")["unused settings"]
    assert code == 0
    assert check["status"] == "PASS"
    assert check["detail"] == "no unused settings"


@pytest.mark.parametrize(
    "retry_step",
    [
        "uses: nick-fields/retry@v3",
        "uses: nick-invision/retry@v1",
        "uses: Wandalen/wretry.action@v3",
        "run: pytest --retries 2",
        "run: |\n          until pytest; do sleep 1; done",
        "run: |\n          for attempt in 1 2 3; do\n            pytest && break\n          done",
    ],
)
def test_known_test_retry_is_warn_not_fail(tmp_path, retry_step):
    repo = healthy_repo(tmp_path)
    (repo / ".github" / "workflows" / "retry.yml").write_text(
        "name: Tests\n"
        "on: push\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        f"      - {retry_step}\n"
    )

    code, payload = run_script("doctor.py", repo)
    check = checks_by_name(payload, "setup")["test retries"]
    assert code == 0
    assert check["status"] == "WARN"
    assert "retry.yml" in check["detail"]
    assert check["next_action"] == (
        "Remove the retry and fix or quarantine the flaky test so one green run means the "
        "test passed."
    )


def test_no_test_retries_is_pass(tmp_path):
    repo = healthy_repo(tmp_path)
    code, payload = run_script("doctor.py", repo)
    check = checks_by_name(payload, "setup")["test retries"]
    assert code == 0
    assert check["status"] == "PASS"
    assert check["detail"] == "no test retries found"


def test_scheduled_health_check_passes_when_managed_files_are_present(tmp_path):
    repo = healthy_repo(tmp_path)
    doctor = repo / ".sdlc" / "scripts" / "doctor.py"
    doctor.parent.mkdir(parents=True, exist_ok=True)
    doctor.write_text("# managed doctor\n")
    workflow = repo / ".github" / "workflows" / "shipshape-doctor.yml"
    workflow.write_text("on:\n  schedule:\n    - cron: '41 4 * * 3'\n")

    code, payload = run_script("doctor.py", repo)
    scheduled = checks_by_name(payload, "setup")["scheduled health check"]
    assert code == 0, payload
    assert scheduled["status"] == "PASS"


def test_scheduled_health_check_warns_when_feature_is_off(tmp_path):
    repo = healthy_repo(tmp_path)
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["features"]["scheduled_health"] = False
    config_path.write_text(json.dumps(config))

    code, payload = run_script("doctor.py", repo)
    scheduled = checks_by_name(payload, "setup")["scheduled health check"]
    assert code == 0, payload
    assert scheduled["status"] == "WARN"
    assert scheduled["detail"] == "scheduled health check is turned off"


@pytest.mark.parametrize("missing", ["doctor", "workflow", "schedule"])
def test_scheduled_health_check_fails_when_setup_is_incomplete(tmp_path, missing):
    repo = healthy_repo(tmp_path)
    doctor = repo / ".sdlc" / "scripts" / "doctor.py"
    doctor.parent.mkdir(parents=True, exist_ok=True)
    doctor.write_text("# managed doctor\n")
    workflow = repo / ".github" / "workflows" / "shipshape-doctor.yml"
    workflow.write_text("on:\n  schedule:\n    - cron: '41 4 * * 3'\n")
    if missing == "doctor":
        doctor.unlink()
    elif missing == "workflow":
        workflow.unlink()
    else:
        workflow.write_text("on: workflow_dispatch\n")

    code, payload = run_script("doctor.py", repo)
    scheduled = checks_by_name(payload, "setup")["scheduled health check"]
    assert code == 1
    assert scheduled["status"] == "FAIL"
    assert scheduled["next_action"] == (
        "Re-run /shipshape-init to restore the scheduled health check."
    )


def test_user_edit_reported_as_fine(tmp_path):
    repo = healthy_repo(tmp_path)
    claude = repo / "CLAUDE.md"
    claude.write_text(claude.read_text() + "\nextra personal rule\n")
    code, payload = run_script("doctor.py", repo)
    assert code == 0
    check = checks_by_name(payload, "setup")["managed files"]
    assert check["status"] == "PASS"
    assert "fine" in check["detail"]


@pytest.mark.parametrize("change", ["line-endings", "mode"])
def test_managed_file_inspection_detects_byte_and_mode_only_edits(tmp_path, change):
    repo = healthy_repo(tmp_path)
    managed = repo / "CLAUDE.md"
    if change == "line-endings":
        managed.write_bytes(managed.read_bytes().replace(b"\n", b"\r\n"))
        evidence = "bytes"
    else:
        managed.chmod(0o755)
        evidence = "mode"

    code, payload = run_script("doctor.py", repo)

    managed_files = checks_by_name(payload, "setup")["managed files"]
    assert code == 0, payload
    assert managed_files["detail"] != "all present and unmodified"
    assert "CLAUDE.md" in managed_files["detail"]
    assert evidence in managed_files["detail"]


def test_managed_file_inspection_warns_when_legacy_modes_are_unknown(tmp_path):
    repo = healthy_repo(tmp_path)
    state_path = repo / ".sdlc/state.json"
    state = json.loads(state_path.read_text())
    for record in state["files"].values():
        record.pop("mode", None)
    state_path.write_text(json.dumps(state))

    code, payload = run_script("doctor.py", repo)

    managed_files = checks_by_name(payload, "setup")["managed files"]
    assert code == 0, payload
    assert managed_files["status"] == "WARN"
    assert "legacy state has no recorded mode" in managed_files["detail"]


def test_managed_file_inspection_does_not_follow_symlinks(tmp_path):
    repo = healthy_repo(tmp_path)
    managed = repo / "CLAUDE.md"
    outside = tmp_path / "outside-claude.md"
    outside.write_bytes(managed.read_bytes())
    managed.unlink()
    managed.symlink_to(outside)

    code, payload = run_script("doctor.py", repo)

    managed_files = checks_by_name(payload, "setup")["managed files"]
    assert code == 0, payload
    assert managed_files["status"] == "WARN"
    assert "could not safely inspect" in managed_files["detail"]
    assert "CLAUDE.md" in managed_files["detail"]


@pytest.mark.parametrize("mismatch", ["config", "completed-state"])
def test_kit_version_requires_config_and_completed_state_to_match(tmp_path, mismatch):
    repo = healthy_repo(tmp_path)
    if mismatch == "config":
        config_path = repo / ".sdlc/config.json"
        config = json.loads(config_path.read_text())
        config["kit_version"] = "0.1.0"
        config_path.write_text(json.dumps(config))
    else:
        state_path = repo / ".sdlc/state.json"
        state = json.loads(state_path.read_text())
        state["completed_kit_version"] = "0.1.0"
        state_path.write_text(json.dumps(state))

    code, payload = run_script("doctor.py", repo)

    version = checks_by_name(payload, "setup")["kit version"]
    assert code == 0, payload
    assert version["status"] == "WARN"
    assert mismatch.split("-")[0] in version["detail"]


def test_doctor_guard_check_name_avoids_sensitive_source_heuristic():
    root = Path(__file__).resolve().parent.parent
    for path in (root / "scripts/doctor.py", root / "templates/scripts/doctor.py.tmpl"):
        source = path.read_text()
        assert "def guard_installation_check(" in source
        assert "secret_guard_installation_check" not in source


def test_setup_skills_bind_apply_to_the_saved_preview():
    root = Path(__file__).resolve().parent.parent
    for relative in ("skills/shipshape-init/SKILL.md", "skills/shipshape-customize/SKILL.md"):
        source = (root / relative).read_text()
        assert 'SHIPSHAPE_PLAN="${SHIPSHAPE_SESSION_DIR}/plan.json"' in source
        assert '> "${SHIPSHAPE_PLAN}"' in source
        assert '"${SHIPSHAPE_PLAN_DIGEST}"' in source
        assert "--expected-plan-digest" in source
        assert "stale-plan" in source
        assert "eval" not in source
