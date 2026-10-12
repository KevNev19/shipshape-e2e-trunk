"""The consumer doctor is executable, JSON-only, and kept in sync."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import init_repo, prepend_to_path, run_script

KIT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def pin_doctor_subprocess_path(hermetic_doctor_path):
    return hermetic_doctor_path


def install_routed_fake_gh(tmp_path):
    bin_dir = tmp_path / "rendered-bin"
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
    calls = tmp_path / "rendered-doctor-gh-calls"
    calls.touch()
    return bin_dir, calls


def set_fake_gh_response(monkeypatch, name, payload, exit_code=0):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setenv(f"FAKE_GH_{name}_BODY", body)
    monkeypatch.setenv(f"FAKE_GH_{name}_CODE", str(exit_code))


def set_fake_gh_error(monkeypatch, name, message):
    monkeypatch.setenv(f"FAKE_GH_{name}_ERROR", message)


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


def enable_tiered_review(repo, *, helper=True, workflow=True):
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow_style"] = "pr"
    config["features"]["branch_protection"] = True
    config["features"]["tiered_review"] = True
    config["repo"]["owner_repo"] = "example/project"
    config_path.write_text(json.dumps(config))
    if workflow:
        (repo / ".github/workflows/low-risk-automerge.yml").write_text(
            "permissions:\n  contents: read\n  pull-requests: write\n"
        )
    destination = repo / ".sdlc/scripts/review-gates.py"
    if helper:
        source = (KIT_ROOT / "templates/scripts/review-gates.py.tmpl").read_text()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.replace("{{KIT_VERSION}}", "test"))
    else:
        destination.unlink(missing_ok=True)


def configure_remote_protection(repo, *, workflow_style="pr"):
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow_style"] = workflow_style
    config["features"]["branch_protection"] = True
    config["features"]["tiered_review"] = False
    config["repo"]["owner_repo"] = "example/project"
    config_path.write_text(json.dumps(config))


def rendered_repo(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    code, payload = run_script("render.py", "apply", repo)
    assert code == 0, payload
    config_path = repo / ".sdlc/config.json"
    config = json.loads(config_path.read_text())
    config["features"]["branch_protection"] = False
    config_path.write_text(json.dumps(config))
    subprocess.run(["git", "init", "-qb", "main"], cwd=repo, check=True)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-commit").symlink_to("../../.sdlc/hooks/secret-guard.sh")
    return repo


def run_rendered_doctor(repo, *, github_actions=False):
    env = os.environ.copy()
    if github_actions:
        env["GITHUB_ACTIONS"] = "true"
    else:
        env.pop("GITHUB_ACTIONS", None)
    proc = subprocess.run(
        [sys.executable, str(repo / ".sdlc" / "scripts" / "doctor.py"), str(repo)],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, json.loads(proc.stdout)


def check_map(payload):
    return {item["name"]: item for section in payload["sections"] for item in section["checks"]}


def activate_pre_commit_dispatcher(repo):
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "# File generated by pre-commit\n"
        "ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)\n"
        'exec pre-commit "${ARGS[@]}"\n'
    )
    hook.chmod(0o755)


def configure_pre_commit_case(repo, case):
    config = repo / ".pre-commit-config.yaml"
    text = config.read_text()
    if case == "global-manual-explicit":
        text = "default_stages: [manual]\n" + text
    elif case == "global-manual-implicit":
        text = "default_stages: [manual]\n" + text.replace("        stages: [pre-commit]\n", "")
    elif case == "remote-repo":
        text = text.replace("  - repo: local", "  - repo: https://example.invalid/hooks")
    elif case == "duplicate-entry":
        text = text.replace(
            "        entry: .sdlc/hooks/secret-guard.sh",
            "        entry: .sdlc/hooks/secret-guard.sh\n        entry: scripts/noop.sh",
        )
    elif case == "args":
        text = text.replace(
            "        entry: .sdlc/hooks/secret-guard.sh",
            "        entry: .sdlc/hooks/secret-guard.sh\n        args: [--quiet]",
        )
    elif case == "hook-manual":
        text = text.replace("stages: [pre-commit]", "stages: [manual]")
    elif case == "duplicate-default":
        text = "default_stages: [pre-commit]\ndefault_stages: [manual]\n" + text
    elif case == "duplicate-repo":
        text = text.replace(
            "  - repo: local", "  - repo: local\n    repo: https://example.invalid/hooks"
        )
    elif case == "guard-not-first":
        text = text.replace(
            "      - id: secret-guard",
            "      - id: earlier-hook\n"
            "        name: earlier hook\n"
            "        entry: /usr/bin/false\n"
            "        language: script\n"
            "      - id: secret-guard",
        )
    elif case != "managed":
        raise AssertionError(f"unknown pre-commit test case: {case}")
    config.write_text(text)


def test_rendered_doctor_reports_a_healthy_repo(tmp_path):
    repo = rendered_repo(tmp_path)
    code, payload = run_rendered_doctor(repo)

    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["counts"]["FAIL"] == 0
    assert set(payload) == {"ok", "set_up", "counts", "sections", "next_action"}
    assert check_map(payload)["remote protection"] == {
        "name": "remote protection",
        "status": "PASS",
        "detail": "remote branch protection is not requested by configuration",
        "next_action": "",
    }


def test_rendered_doctor_fails_when_tiered_review_has_no_repository_identity(tmp_path):
    repo = rendered_repo(tmp_path)
    config_path = repo / ".sdlc" / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow_style"] = "pr"
    config["features"]["branch_protection"] = True
    config["features"]["tiered_review"] = True
    config_path.write_text(json.dumps(config))
    workflow = repo / ".github" / "workflows" / "low-risk-automerge.yml"
    workflow.write_text("permissions:\n  contents: read\n  pull-requests: write\n")
    code, payload = run_rendered_doctor(repo)

    tiered_review = check_map(payload)["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert "repository must be an exact owner/name slug" in tiered_review["detail"]


def test_rendered_doctor_fails_when_tiered_review_workflow_is_missing(tmp_path):
    repo = rendered_repo(tmp_path)
    enable_tiered_review(repo, workflow=False)

    code, payload = run_rendered_doctor(repo)

    tiered_review = check_map(payload)["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert tiered_review["next_action"] == (
        "Turn off tiered review until every required check is enforced."
    )


def test_rendered_doctor_fails_when_review_gates_helper_is_missing(tmp_path):
    repo = rendered_repo(tmp_path)
    enable_tiered_review(repo, helper=False)

    code, payload = run_rendered_doctor(repo)

    tiered_review = check_map(payload)["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert "review-gates helper is missing" in tiered_review["detail"]


def test_rendered_doctor_fails_when_active_rules_are_missing(tmp_path, monkeypatch):
    repo = rendered_repo(tmp_path)
    enable_tiered_review(repo)
    configure_protection_api(monkeypatch, tmp_path, rules=[])

    code, payload = run_rendered_doctor(repo)

    tiered_review = check_map(payload)["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert "active branch rules was empty" in tiered_review["detail"]


def test_rendered_doctor_fails_when_protection_api_is_inaccessible(tmp_path, monkeypatch):
    repo = rendered_repo(tmp_path)
    enable_tiered_review(repo)
    bin_dir, calls = install_routed_fake_gh(tmp_path)
    prepend_to_path(monkeypatch, bin_dir)
    monkeypatch.setenv("FAKE_GH_CALLS", str(calls))
    set_fake_gh_response(monkeypatch, "RULES", "", exit_code=1)

    code, payload = run_rendered_doctor(repo)

    tiered_review = check_map(payload)["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"
    assert "unverified" in tiered_review["detail"]


@pytest.mark.parametrize(
    "ruleset",
    [
        valid_ruleset(enforcement="evaluate"),
        valid_ruleset(strict=False),
        valid_ruleset(test_app_id=999),
    ],
)
def test_rendered_doctor_fails_for_unsafe_ruleset(tmp_path, monkeypatch, ruleset):
    repo = rendered_repo(tmp_path)
    enable_tiered_review(repo)
    configure_protection_api(monkeypatch, tmp_path, ruleset=ruleset)

    code, payload = run_rendered_doctor(repo)

    tiered_review = check_map(payload)["remote protection"]
    assert code == 1
    assert tiered_review["status"] == "FAIL"


def test_rendered_doctor_uses_canonical_read_only_protection_inspection(tmp_path, monkeypatch):
    repo = rendered_repo(tmp_path)
    enable_tiered_review(repo)
    calls = configure_protection_api(monkeypatch, tmp_path, ruleset=valid_ruleset())

    code, payload = run_rendered_doctor(repo)

    tiered_review = check_map(payload)["remote protection"]
    assert code == 0, payload
    assert tiered_review["status"] == "PASS"
    api_calls = calls.read_text()
    assert "rules/branches/main" in api_calls
    assert "rulesets/41" in api_calls
    assert "--method" not in api_calls
    assert "graphql" not in api_calls


def test_rendered_doctor_verifies_non_tiered_pr_protection_read_only(tmp_path, monkeypatch):
    repo = rendered_repo(tmp_path)
    configure_remote_protection(repo)
    calls = configure_protection_api(monkeypatch, tmp_path, ruleset=valid_ruleset(tiered=False))

    code, payload = run_rendered_doctor(repo)

    protection = check_map(payload)["remote protection"]
    assert code == 0, payload
    assert protection["status"] == "PASS"
    assert "configured PR protection verified" in protection["detail"]
    api_calls = calls.read_text()
    assert "rules/branches/main" in api_calls
    assert "rulesets/41" in api_calls
    assert "--method" not in api_calls
    assert "graphql" not in api_calls


def test_rendered_doctor_verifies_trunk_safeguards_without_status_checks(tmp_path, monkeypatch):
    repo = rendered_repo(tmp_path)
    configure_remote_protection(repo, workflow_style="trunk")
    rules = [
        {"type": "deletion", "ruleset_id": 41},
        {"type": "non_fast_forward", "ruleset_id": 41},
    ]
    configure_protection_api(monkeypatch, tmp_path, rules=rules, ruleset=valid_trunk_ruleset())

    code, payload = run_rendered_doctor(repo)

    protection = check_map(payload)["remote protection"]
    assert code == 0, payload
    assert protection["status"] == "PASS"
    assert "configured trunk protection verified" in protection["detail"]
    assert "deletion and non-fast-forward safeguards" in protection["detail"]


def test_rendered_doctor_fails_when_configured_codeql_workflow_is_missing(tmp_path, monkeypatch):
    repo = rendered_repo(tmp_path)
    configure_remote_protection(repo)
    (repo / ".github/workflows/codeql.yml").unlink()
    bin_dir, calls = install_routed_fake_gh(tmp_path)
    prepend_to_path(monkeypatch, bin_dir)
    monkeypatch.setenv("FAKE_GH_CALLS", str(calls))

    code, payload = run_rendered_doctor(repo)

    protection = check_map(payload)["remote protection"]
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
def test_rendered_doctor_rejects_malformed_protection_metadata(tmp_path, field, value, reason):
    repo = rendered_repo(tmp_path)
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

    code, payload = run_rendered_doctor(repo)

    protection = check_map(payload)["remote protection"]
    assert code == 1
    assert protection["status"] == "FAIL"
    assert reason in protection["detail"]


def test_rendered_doctor_fails_when_the_secret_guard_is_missing(tmp_path):
    repo = rendered_repo(tmp_path)
    (repo / ".sdlc" / "hooks" / "secret-guard.sh").unlink()

    code, payload = run_rendered_doctor(repo)

    assert code == 1
    assert payload["ok"] is False
    assert check_map(payload)["secret guard"]["status"] == "FAIL"


def test_rendered_doctor_accepts_secret_scan_in_ci_without_local_hook(tmp_path):
    repo = rendered_repo(tmp_path)
    (repo / ".git" / "hooks" / "pre-commit").unlink()

    code, payload = run_rendered_doctor(repo, github_actions=True)

    installed = check_map(payload)["secret guard installed"]
    assert code == 0, payload
    assert installed["status"] == "WARN"
    assert installed["detail"] == (
        "secret-scan workflow is configured; local hook was not verified in GitHub Actions"
    )


def test_rendered_doctor_fails_when_secret_scan_is_missing_in_ci(tmp_path):
    repo = rendered_repo(tmp_path)
    (repo / ".github" / "workflows" / "secret-scan.yml").unlink()

    code, payload = run_rendered_doctor(repo, github_actions=True)

    installed = check_map(payload)["secret guard installed"]
    assert code == 1
    assert payload["ok"] is False
    assert installed["status"] == "FAIL"
    assert installed["next_action"] == (
        "Re-run /shipshape-init to restore the secret-scan workflow."
    )


def test_rendered_doctor_still_requires_local_hook_outside_ci(tmp_path):
    repo = rendered_repo(tmp_path)
    (repo / ".git" / "hooks" / "pre-commit").unlink()

    code, payload = run_rendered_doctor(repo)

    installed = check_map(payload)["secret guard installed"]
    assert code == 1
    assert payload["ok"] is False
    assert installed == {
        "name": "secret guard installed",
        "status": "FAIL",
        "detail": (
            "the scanner exists but no pre-commit hook is present in the active Git hook path"
        ),
        "next_action": "run: bash .sdlc/hooks/install.sh",
    }


def test_rendered_and_plugin_doctors_report_the_same_checks(tmp_path):
    repo = rendered_repo(tmp_path)

    plugin_code, plugin_payload = run_script("doctor.py", repo)
    rendered_code, rendered_payload = run_rendered_doctor(repo)
    plugin_checks = check_map(plugin_payload)
    rendered_checks = check_map(rendered_payload)

    assert plugin_code == rendered_code == 0
    assert set(rendered_checks) == set(plugin_checks)
    assert {name: item["status"] for name, item in rendered_checks.items()} == {
        name: item["status"] for name, item in plugin_checks.items()
    }


def test_rendered_doctor_rejects_unrelated_active_hook(tmp_path):
    repo = rendered_repo(tmp_path)
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)

    code, payload = run_rendered_doctor(repo)

    installed = check_map(payload)["secret guard installed"]
    assert code == 1
    assert installed["status"] == "FAIL"
    assert "does not invoke" in installed["detail"]


def test_rendered_doctor_honors_core_hooks_path(tmp_path):
    repo = rendered_repo(tmp_path)
    subprocess.run(["git", "config", "core.hooksPath", ".other-hooks"], cwd=repo, check=True)

    code, payload = run_rendered_doctor(repo)

    installed = check_map(payload)["secret guard installed"]
    assert code == 1
    assert installed["status"] == "FAIL"
    assert "active Git hook path" in installed["detail"]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("managed", "PASS"),
        ("global-manual-explicit", "PASS"),
        ("global-manual-implicit", "FAIL"),
        ("remote-repo", "FAIL"),
        ("duplicate-entry", "FAIL"),
        ("args", "FAIL"),
        ("hook-manual", "FAIL"),
        ("duplicate-default", "FAIL"),
        ("duplicate-repo", "FAIL"),
        ("guard-not-first", "FAIL"),
    ],
)
def test_both_doctors_agree_on_supported_pre_commit_configuration(tmp_path, case, expected):
    repo = rendered_repo(tmp_path)
    activate_pre_commit_dispatcher(repo)
    configure_pre_commit_case(repo, case)

    plugin_code, plugin_payload = run_script("doctor.py", repo)
    rendered_code, rendered_payload = run_rendered_doctor(repo)
    plugin_check = check_map(plugin_payload)["secret guard installed"]
    rendered_check = check_map(rendered_payload)["secret guard installed"]

    assert plugin_check == rendered_check
    assert plugin_check["status"] == expected
    assert plugin_code == rendered_code == (0 if expected == "PASS" else 1)


def test_both_doctors_report_current_pre_commit_skip_as_fail(tmp_path, monkeypatch):
    repo = rendered_repo(tmp_path)
    activate_pre_commit_dispatcher(repo)
    monkeypatch.setenv("SKIP", "other-hook,secret-guard")

    plugin_code, plugin_payload = run_script("doctor.py", repo)
    rendered_code, rendered_payload = run_rendered_doctor(repo)
    plugin_check = check_map(plugin_payload)["secret guard installed"]
    rendered_check = check_map(rendered_payload)["secret guard installed"]

    assert plugin_code == rendered_code == 1
    assert plugin_check == rendered_check
    assert plugin_check["status"] == "FAIL"
    assert "SKIP" in plugin_check["detail"]


def test_both_doctors_inspect_but_do_not_execute_arbitrary_dispatcher(tmp_path):
    repo = rendered_repo(tmp_path)
    marker = tmp_path / "dispatcher-executed"
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "# File generated by pre-commit\n"
        "ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)\n"
        f': > "{marker}"\n'
        'exec pre-commit "${ARGS[@]}"\n'
    )
    hook.chmod(0o755)

    plugin_code, plugin_payload = run_script("doctor.py", repo)
    rendered_code, rendered_payload = run_rendered_doctor(repo)
    plugin_check = check_map(plugin_payload)["secret guard installed"]
    rendered_check = check_map(rendered_payload)["secret guard installed"]

    assert plugin_code == rendered_code == 0
    assert plugin_check == rendered_check
    assert plugin_check["status"] == "PASS"
    assert "execution was not observed" in plugin_check["detail"]
    assert not marker.exists()


def test_both_doctors_report_partial_setup_without_calling_user_edits_safe(tmp_path):
    repo = rendered_repo(tmp_path)
    state_path = repo / ".sdlc/state.json"
    state = json.loads(state_path.read_text())
    state.update(
        {
            "status": "partial",
            "completed_kit_version": "0.1.0",
            "last_run_kit_version": "0.2.1",
            "pending_conflicts": ["CLAUDE.md"],
            "last_run_errors": [{"path": "AGENTS.md", "operation": "write", "error": "synthetic"}],
        }
    )
    state_path.write_text(json.dumps(state))

    plugin_code, plugin_payload = run_script("doctor.py", repo)
    rendered_code, rendered_payload = run_rendered_doctor(repo)
    plugin_checks = check_map(plugin_payload)
    rendered_checks = check_map(rendered_payload)

    assert plugin_code == rendered_code == 0
    assert plugin_checks["managed files"] == rendered_checks["managed files"]
    assert plugin_checks["managed files"]["status"] == "WARN"
    assert "last setup apply was partial" in plugin_checks["managed files"]["detail"]
    assert "which is fine" not in plugin_checks["managed files"]["detail"]
    assert plugin_checks["kit version"]["status"] == "WARN"
    assert rendered_checks["kit version"]["status"] == "WARN"
    assert "last complete setup" in plugin_checks["kit version"]["detail"]


def test_rendered_and_plugin_doctors_agree_in_github_actions(tmp_path, monkeypatch):
    repo = rendered_repo(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    plugin_code, plugin_payload = run_script("doctor.py", repo)
    rendered_code, rendered_payload = run_rendered_doctor(repo, github_actions=True)
    plugin_installed = check_map(plugin_payload)["secret guard installed"]
    rendered_installed = check_map(rendered_payload)["secret guard installed"]

    assert plugin_code == rendered_code == 0
    assert plugin_installed == rendered_installed
    assert rendered_installed["status"] == "WARN"


@pytest.mark.parametrize("change", ["line-endings", "mode"])
def test_rendered_doctor_detects_byte_and_mode_only_edits(tmp_path, change):
    repo = rendered_repo(tmp_path)
    managed = repo / "CLAUDE.md"
    if change == "line-endings":
        managed.write_bytes(managed.read_bytes().replace(b"\n", b"\r\n"))
        evidence = "bytes"
    else:
        managed.chmod(0o755)
        evidence = "mode"

    code, payload = run_rendered_doctor(repo)

    managed_files = check_map(payload)["managed files"]
    assert code == 0, payload
    assert managed_files["detail"] != "all present and unmodified"
    assert "CLAUDE.md" in managed_files["detail"]
    assert evidence in managed_files["detail"]


def test_rendered_doctor_exposes_unknown_legacy_modes(tmp_path):
    repo = rendered_repo(tmp_path)
    state_path = repo / ".sdlc/state.json"
    state = json.loads(state_path.read_text())
    for record in state["files"].values():
        record.pop("mode", None)
    state_path.write_text(json.dumps(state))

    code, payload = run_rendered_doctor(repo)

    managed_files = check_map(payload)["managed files"]
    assert code == 0, payload
    assert managed_files["status"] == "WARN"
    assert "legacy state has no recorded mode" in managed_files["detail"]


def test_both_doctors_do_not_echo_credential_like_config_or_hook_contents(tmp_path):
    repo = rendered_repo(tmp_path)
    marker = "synthetic-credential-material-that-must-not-be-echoed"
    config_path = repo / ".sdlc/config.json"
    config = json.loads(config_path.read_text())
    config["private_extension"] = {"api_token": marker}
    config_path.write_text(json.dumps(config))
    guard_path = repo / ".sdlc/hooks/secret-guard.sh"
    guard_path.write_text(guard_path.read_text() + f"\n# password={marker}\n")

    plugin_code, plugin_payload = run_script("doctor.py", repo)
    rendered_code, rendered_payload = run_rendered_doctor(repo)

    assert plugin_code == rendered_code == 0
    assert marker not in json.dumps(plugin_payload)
    assert marker not in json.dumps(rendered_payload)
