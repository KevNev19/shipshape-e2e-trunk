"""Behavioral tests for additive GitHub protection provisioning."""

import copy
import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import review_gates

REPO = "example/project"
BRANCH = "main"
BRANCH_SHA = "a" * 40
UNRELATED_RULESET_ID = 41
OWNED_RULESET_ID = 71
ACTIVE_RULES_PATH = f"repos/{REPO}/rules/branches/{BRANCH}?per_page=100&page=1"
CI_PATH = review_gates.BASE_GATES[0].workflow_path
SECRET_SCAN_PATH = review_gates.BASE_GATES[1].workflow_path
CODEQL_PATH = review_gates.CODEQL_GATE.workflow_path


@pytest.fixture
def protection():
    return importlib.import_module("scripts.protection")


class FakeAdminGitHub:
    def __init__(self):
        self.responses = {}
        self.optional = {}
        self.errors = {}
        self.reads = []
        self.writes = []
        self.created_user_can_bypass = "never"

    def set(self, path, value):
        self.responses[path] = value

    def get(self, path):
        self.reads.append(("get", path))
        if path in self.errors:
            raise review_gates.GateError(self.errors[path])
        if path not in self.responses:
            raise AssertionError(f"unexpected GET {path}")
        return copy.deepcopy(self.responses[path])

    def get_optional(self, path):
        self.reads.append(("get_optional", path))
        if path in self.errors:
            raise review_gates.GateError(self.errors[path])
        return copy.deepcopy(self.optional.get(path))

    def post(self, path, payload):
        self.writes.append(("POST", path, copy.deepcopy(payload)))
        ruleset_id = OWNED_RULESET_ID
        detail = {
            **copy.deepcopy(payload),
            "id": ruleset_id,
            "source_type": "Repository",
            "source": REPO,
            "current_user_can_bypass": self.created_user_can_bypass,
        }
        self.responses[f"repos/{REPO}/rulesets/{ruleset_id}"] = detail
        return {"id": ruleset_id}


def paged_rulesets(page=1):
    return f"repos/{REPO}/rulesets?includes_parents=true&per_page=100&page={page}"


def protection_config(
    *, workflow_style="pr", branch_protection=True, tiered_review=False, codeql=True, languages=None
):
    return {
        "workflow_style": workflow_style,
        "default_branch": BRANCH,
        "languages": ["python"] if languages is None else languages,
        "repo": {"owner_repo": REPO},
        "features": {
            "branch_protection": branch_protection,
            "tiered_review": tiered_review,
            "codeql": codeql,
            "secret_guard": True,
        },
    }


def classic_protection():
    return {
        "required_status_checks": {
            "strict": True,
            "checks": [
                {"context": "test", "app_id": review_gates.GITHUB_ACTIONS_APP_ID},
                {"context": "security-review", "app_id": 4242},
            ],
        },
        "enforce_admins": {"enabled": True},
        "required_pull_request_reviews": {
            "required_approving_review_count": 2,
            "dismiss_stale_reviews": True,
        },
        "restrictions": {"users": [{"login": "release-manager"}]},
    }


def unrelated_ruleset():
    return {
        "id": UNRELATED_RULESET_ID,
        "name": "Owner controls",
        "target": "branch",
        "source_type": "Repository",
        "source": REPO,
        "enforcement": "active",
        "bypass_actors": [{"actor_id": 5, "actor_type": "Team", "bypass_mode": "pull_request"}],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {
                "type": "pull_request",
                "parameters": {"required_approving_review_count": 2},
            }
        ],
        "current_user_can_bypass": "never",
    }


def valid_api():
    api = FakeAdminGitHub()
    api.set(
        f"repos/{REPO}",
        {
            "id": 101,
            "full_name": REPO,
            "default_branch": BRANCH,
            "permissions": {"admin": True, "maintain": True, "push": True},
        },
    )
    api.set(
        f"repos/{REPO}/branches/{BRANCH}",
        {"name": BRANCH, "commit": {"sha": BRANCH_SHA}},
    )
    api.optional[f"repos/{REPO}/branches/{BRANCH}/protection"] = classic_protection()
    api.set(
        paged_rulesets(),
        [
            {
                "id": UNRELATED_RULESET_ID,
                "name": "Owner controls",
                "source_type": "Repository",
                "source": REPO,
                "enforcement": "active",
            }
        ],
    )
    api.set(f"repos/{REPO}/rulesets/{UNRELATED_RULESET_ID}", unrelated_ruleset())
    api.set(ACTIVE_RULES_PATH, [])
    return api


def install_active_ruleset(api, proposal, *, ruleset_id=OWNED_RULESET_ID):
    api.set(
        ACTIVE_RULES_PATH,
        [{"type": rule["type"], "ruleset_id": ruleset_id} for rule in proposal["rules"]],
    )
    api.set(
        f"repos/{REPO}/rulesets/{ruleset_id}",
        {
            **copy.deepcopy(proposal),
            "id": ruleset_id,
            "source_type": "Repository",
            "source": REPO,
            "current_user_can_bypass": "never",
        },
    )


def install_owned_ruleset(api, protection, *, mutate=None):
    proposal = protection.ruleset_proposal(
        BRANCH,
        review_gates.required_gates(True),
        review_gates.RULESET_NAME,
        require_review=False,
    )
    detail = {
        **copy.deepcopy(proposal),
        "id": OWNED_RULESET_ID,
        "source_type": "Repository",
        "source": REPO,
        "current_user_can_bypass": "never",
    }
    if mutate is not None:
        mutate(detail)
    api.responses[paged_rulesets()].append(
        {
            "id": OWNED_RULESET_ID,
            "name": review_gates.RULESET_NAME,
            "source_type": "Repository",
            "source": REPO,
            "enforcement": "active",
        }
    )
    api.set(f"repos/{REPO}/rulesets/{OWNED_RULESET_ID}", detail)


def test_config_derives_exact_ordinary_pr_protection_request():
    request = review_gates.derive_protection_request(
        protection_config(),
        {CI_PATH, CODEQL_PATH},
    )

    assert request == {
        "requested": True,
        "repo": REPO,
        "branch": BRANCH,
        "workflow_style": "pr",
        "tiered_review": False,
        "codeql_enabled": True,
        "ruleset_name": review_gates.BASELINE_RULESET_NAME,
        "required_checks": [
            {
                "context": review_gates.BASE_GATES[0].context,
                "integration_id": review_gates.BASE_GATES[0].app_id,
            },
            {
                "context": review_gates.CODEQL_GATE.context,
                "integration_id": review_gates.CODEQL_GATE.app_id,
            },
        ],
        "required_rule_types": ["required_status_checks"],
    }


def test_config_derives_exact_trunk_safeguard_request_without_status_checks():
    request = review_gates.derive_protection_request(
        protection_config(workflow_style="trunk", codeql=True),
        {CODEQL_PATH},
    )

    assert request["requested"] is True
    assert request["workflow_style"] == "trunk"
    assert request["codeql_enabled"] is True
    assert request["required_checks"] == []
    assert request["required_rule_types"] == ["deletion", "non_fast_forward"]
    assert request["ruleset_name"] == review_gates.BASELINE_RULESET_NAME


def test_config_derives_exact_tiered_pr_request():
    request = review_gates.derive_protection_request(
        protection_config(tiered_review=True),
        {CI_PATH, SECRET_SCAN_PATH, CODEQL_PATH},
    )

    assert request["tiered_review"] is True
    assert request["ruleset_name"] == review_gates.RULESET_NAME
    assert request["required_checks"] == [
        {"context": gate.context, "integration_id": gate.app_id}
        for gate in review_gates.required_gates(True)
    ]


def test_disabled_branch_protection_derives_no_remote_inspection_request():
    request = review_gates.derive_protection_request(
        protection_config(branch_protection=False, codeql=False),
        set(),
    )

    assert request["requested"] is False
    assert request["ruleset_name"] is None
    assert request["required_checks"] == []
    assert request["required_rule_types"] == []


def test_tiered_review_without_branch_protection_is_malformed():
    with pytest.raises(review_gates.GateError, match="requires PR-style branch protection"):
        review_gates.derive_protection_request(
            protection_config(branch_protection=False, tiered_review=True),
            {CI_PATH, SECRET_SCAN_PATH, CODEQL_PATH},
        )


@pytest.mark.parametrize(
    ("feature", "value"),
    [
        ("branch_protection", "true"),
        ("tiered_review", "false"),
        ("codeql", 1),
        ("secret_guard", None),
    ],
)
def test_config_derivation_rejects_nonboolean_feature_flags(feature, value):
    config = protection_config()
    config["features"][feature] = value

    with pytest.raises(review_gates.GateError, match="feature flags"):
        review_gates.derive_protection_request(config, {CI_PATH, CODEQL_PATH})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workflow_style", True),
        ("workflow_style", "unknown"),
        ("default_branch", ["main"]),
        ("default_branch", "refs/heads/main"),
        ("languages", "python"),
        ("languages", ["python", 7]),
        ("languages", ["python", "python"]),
    ],
)
def test_config_derivation_rejects_malformed_workflow_branch_or_languages(field, value):
    config = protection_config()
    config[field] = value

    with pytest.raises(review_gates.GateError):
        review_gates.derive_protection_request(config, {CI_PATH, CODEQL_PATH})


def test_configured_applicable_codeql_requires_the_workflow():
    with pytest.raises(review_gates.GateError, match="CodeQL workflow was missing"):
        review_gates.derive_protection_request(protection_config(), {CI_PATH})


def test_configured_protection_reports_missing_codeql_as_unknown_without_api_reads():
    api = valid_api()

    result = review_gates.inspect_configured_protection(
        api,
        protection_config(),
        {CI_PATH},
    )

    assert result == {
        "ok": False,
        "requested": None,
        "request": None,
        "ruleset_id": None,
        "reason": "configured applicable CodeQL workflow was missing",
    }
    assert api.reads == []
    assert api.writes == []


def test_unsupported_primary_language_does_not_infer_codeql_from_file_presence():
    request = review_gates.derive_protection_request(
        protection_config(languages=["rust"]),
        {CI_PATH, CODEQL_PATH},
    )

    assert request["codeql_enabled"] is False
    assert request["required_checks"] == [
        {
            "context": review_gates.BASE_GATES[0].context,
            "integration_id": review_gates.BASE_GATES[0].app_id,
        }
    ]


def test_non_tiered_requested_protection_is_inspected_and_missing_state_refuses():
    api = valid_api()

    result = review_gates.inspect_configured_protection(
        api,
        protection_config(workflow_style="trunk"),
        {CODEQL_PATH},
    )

    assert result["ok"] is False
    assert result["requested"] is True
    assert "active branch rules was empty" in result["reason"]
    assert ("get", ACTIVE_RULES_PATH) in api.reads
    assert api.writes == []


def test_disabled_branch_protection_returns_known_unrequested_without_api_reads():
    api = valid_api()

    result = review_gates.inspect_configured_protection(
        api,
        protection_config(branch_protection=False, codeql=False),
        set(),
    )

    assert result["ok"] is True
    assert result["requested"] is False
    assert result["ruleset_id"] is None
    assert api.reads == []
    assert api.writes == []


def test_trunk_inspection_accepts_additive_safeguards_and_preserves_stricter_controls(
    protection,
):
    api = valid_api()
    proposal = protection.trunk_ruleset_proposal(BRANCH)
    proposal["rules"].append(
        {
            "type": "pull_request",
            "parameters": {"required_approving_review_count": 2},
        }
    )
    install_active_ruleset(api, proposal)

    result = review_gates.inspect_configured_protection(
        api,
        protection_config(workflow_style="trunk"),
        {CODEQL_PATH},
    )

    assert result["ok"] is True
    assert result["ruleset_id"] == OWNED_RULESET_ID
    assert result["request"]["required_rule_types"] == ["deletion", "non_fast_forward"]
    assert api.writes == []


def test_ordinary_pr_inspection_uses_configured_codeql_and_exact_source_checks(
    protection,
):
    api = valid_api()
    proposal = protection.ruleset_proposal(
        BRANCH,
        (review_gates.BASE_GATES[0], review_gates.CODEQL_GATE),
        review_gates.BASELINE_RULESET_NAME,
        require_review=False,
    )
    install_active_ruleset(api, proposal)

    result = review_gates.inspect_configured_protection(
        api,
        protection_config(),
        {CI_PATH, CODEQL_PATH},
    )

    assert result["ok"] is True
    assert result["ruleset_id"] == OWNED_RULESET_ID
    assert result["request"]["codeql_enabled"] is True
    assert api.writes == []


def test_tiered_pr_inspection_reuses_automatic_merge_ruleset_contract(protection):
    api = valid_api()
    proposal = protection.ruleset_proposal(
        BRANCH,
        review_gates.required_gates(True),
        review_gates.RULESET_NAME,
        require_review=False,
    )
    install_active_ruleset(api, proposal)

    result = review_gates.inspect_configured_protection(
        api,
        protection_config(tiered_review=True),
        {CI_PATH, SECRET_SCAN_PATH, CODEQL_PATH},
    )

    assert result["ok"] is True
    assert result["ruleset_id"] == OWNED_RULESET_ID
    assert result["request"]["tiered_review"] is True
    assert api.writes == []


def test_plan_reads_and_preserves_every_existing_control(protection):
    api = valid_api()
    original_classic = copy.deepcopy(api.optional)
    original_rulesets = copy.deepcopy(api.responses)

    result = protection.plan_protection(
        api,
        REPO,
        BRANCH,
        workflow_style="pr",
        tiered_review=True,
        codeql_enabled=True,
    )

    assert result["ok"] is True
    assert result["action"] == "create"
    assert result["dry_run"] is True
    assert result["observed"] == {
        "classic_protection": "present",
        "ruleset_ids": [UNRELATED_RULESET_ID],
    }
    assert result["preservation"] == {
        "classic_protection": "unchanged",
        "reviews": "unchanged",
        "restrictions": "unchanged",
        "enforcement": "unchanged",
        "checks": "unchanged",
        "bypasses": "unchanged",
        "unrelated_rulesets": "unchanged",
    }
    assert result["proposal"]["name"] == review_gates.RULESET_NAME
    assert result["proposal"]["bypass_actors"] == []
    checks = result["proposal"]["rules"][0]["parameters"]["required_status_checks"]
    assert checks == [
        {"context": gate.context, "integration_id": gate.app_id}
        for gate in review_gates.required_gates(True)
    ]
    assert api.writes == []
    assert api.optional == original_classic
    assert api.responses == original_rulesets
    assert ("get_optional", f"repos/{REPO}/branches/{BRANCH}/protection") in api.reads
    assert ("get", f"repos/{REPO}/rulesets/{UNRELATED_RULESET_ID}") in api.reads


def test_apply_creates_only_a_separate_owned_ruleset(protection):
    api = valid_api()
    original_classic = copy.deepcopy(api.optional)
    original_unrelated = copy.deepcopy(api.responses)

    result = protection.apply_protection(
        api,
        REPO,
        BRANCH,
        workflow_style="pr",
        tiered_review=True,
        codeql_enabled=True,
    )

    assert result["ok"] is True
    assert result["action"] == "created"
    assert result["ruleset_id"] == OWNED_RULESET_ID
    assert len(api.writes) == 1
    method, path, payload = api.writes[0]
    assert method == "POST"
    assert path == f"repos/{REPO}/rulesets"
    assert payload["name"] == review_gates.RULESET_NAME
    assert payload["conditions"] == {
        "ref_name": {"include": [f"refs/heads/{BRANCH}"], "exclude": []}
    }
    assert api.optional == original_classic
    for path, value in original_unrelated.items():
        assert api.responses[path] == value


def test_exact_existing_owned_ruleset_is_an_idempotent_noop(protection):
    api = valid_api()
    install_owned_ruleset(api, protection)

    result = protection.apply_protection(
        api,
        REPO,
        BRANCH,
        workflow_style="pr",
        tiered_review=True,
        codeql_enabled=True,
    )

    assert result["ok"] is True
    assert result["action"] == "unchanged"
    assert result["ruleset_id"] == OWNED_RULESET_ID
    assert api.writes == []


def test_modified_same_name_ruleset_refuses_without_replacing_it(protection):
    api = valid_api()

    def add_user_rule(detail):
        detail["rules"].append(
            {
                "type": "pull_request",
                "parameters": {"required_approving_review_count": 2},
            }
        )

    install_owned_ruleset(api, protection, mutate=add_user_rule)
    before = copy.deepcopy(api.responses)

    result = protection.apply_protection(
        api,
        REPO,
        BRANCH,
        workflow_style="pr",
        tiered_review=True,
        codeql_enabled=True,
    )

    assert result["ok"] is False
    assert result["action"] == "refuse"
    assert "same-name" in result["reason"]
    assert "review" in result["reason"]
    assert api.writes == []
    assert api.responses == before


@pytest.mark.parametrize("permissions", [{}, {"admin": False}, None])
def test_unknown_or_insufficient_admin_permission_fails_closed(protection, permissions):
    api = valid_api()
    api.responses[f"repos/{REPO}"]["permissions"] = permissions

    result = protection.plan_protection(api, REPO, BRANCH, workflow_style="trunk")

    assert result["ok"] is False
    assert result["action"] == "refuse"
    assert api.writes == []


def test_inaccessible_classic_or_ruleset_state_fails_closed(protection):
    for path in (
        f"repos/{REPO}/branches/{BRANCH}/protection",
        paged_rulesets(),
    ):
        api = valid_api()
        api.errors[path] = "HTTP 403"

        result = protection.plan_protection(api, REPO, BRANCH, workflow_style="trunk")

        assert result["ok"] is False
        assert "403" in result["reason"]
        assert api.writes == []


def test_failed_post_verification_never_deletes_or_replaces_controls(protection):
    api = valid_api()
    api.created_user_can_bypass = "always"

    result = protection.apply_protection(api, REPO, BRANCH, workflow_style="trunk")

    assert result["ok"] is False
    assert result["action"] == "created_unverified"
    assert "manual inspection" in result["reason"]
    assert [write[0] for write in api.writes] == ["POST"]


def test_baseline_protection_keeps_prior_test_and_review_purpose(protection):
    api = valid_api()

    result = protection.plan_protection(
        api,
        REPO,
        BRANCH,
        workflow_style="pr",
        require_review=True,
    )

    rules = result["proposal"]["rules"]
    assert rules[0]["parameters"]["required_status_checks"] == [
        {
            "context": review_gates.BASE_GATES[0].context,
            "integration_id": review_gates.BASE_GATES[0].app_id,
        }
    ]
    assert rules[1] == {
        "type": "pull_request",
        "parameters": {
            "allowed_merge_methods": ["merge", "squash", "rebase"],
            "dismiss_stale_reviews_on_push": False,
            "require_code_owner_review": False,
            "require_last_push_approval": False,
            "required_approving_review_count": 1,
            "required_review_thread_resolution": False,
        },
    }


def test_plan_cli_emits_one_json_object_without_mutation(protection, monkeypatch, capsys):
    api = valid_api()
    monkeypatch.setattr(protection, "GitHubAdminClient", lambda: api)

    code = protection.main(
        [
            "plan",
            "--repo",
            REPO,
            "--branch",
            BRANCH,
            "--workflow-style",
            "pr",
            "--tiered-review",
            "--codeql-enabled",
        ]
    )

    output = capsys.readouterr()
    assert code == 0
    assert output.err == ""
    assert json.loads(output.out) == protection.plan_protection(
        valid_api(),
        REPO,
        BRANCH,
        workflow_style="pr",
        tiered_review=True,
        codeql_enabled=True,
    )
    assert api.writes == []


def test_shell_protect_routes_to_additive_helper_without_classic_put(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.txt"
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/bin/sh
printf 'gh:%s\n' "$*" >> "$CALL_LOG"
if [ "$1 $2" = "repo view" ]; then
  printf '%s\n' 'example/project'
fi
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/bin/sh
printf 'python3:%s\n' "$*" >> "$CALL_LOG"
printf '%s\n' '{"ok": true, "action": "created"}'
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = os.environ.copy()
    env.update({"CALL_LOG": str(call_log), "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}"})

    result = subprocess.run(
        ["bash", "scripts/gh_helpers.sh", "protect", BRANCH, "pr", "--require-review"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "--method PUT" not in calls
    assert "branches/main/protection" not in calls
    assert "python3:" in calls
    assert (
        "protection.py apply --repo example/project --branch main "
        "--workflow-style pr --require-review"
    ) in calls


def test_trunk_plan_adds_only_push_compatible_safeguards(protection):
    api = valid_api()

    result = protection.plan_protection(api, REPO, BRANCH, workflow_style="trunk")

    assert result["ok"] is True
    assert result["workflow_style"] == "trunk"
    assert {rule["type"] for rule in result["proposal"]["rules"]} == {
        "deletion",
        "non_fast_forward",
    }
    assert "required_status_checks" not in json.dumps(result["proposal"])
    assert result["preservation"]["checks"] == "unchanged"
    assert api.writes == []


def test_unknown_or_ambiguous_workflow_style_refuses_without_mutation(protection):
    api = valid_api()

    result = protection.plan_protection(api, REPO, BRANCH, workflow_style="unknown")

    assert result["ok"] is False
    assert result["action"] == "refuse"
    assert result["proposal"] is None
    assert api.writes == []


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("tiered_review", "false"),
        ("codeql_enabled", 1),
        ("require_review", None),
    ],
)
def test_nonboolean_direct_protection_options_refuse_without_mutation(protection, option, value):
    api = valid_api()
    options = {
        "tiered_review": False,
        "codeql_enabled": False,
        "require_review": False,
    }
    options[option] = value

    result = protection.plan_protection(
        api,
        REPO,
        BRANCH,
        workflow_style="pr",
        **options,
    )

    assert result["ok"] is False
    assert "exact booleans" in result["reason"]
    assert api.writes == []
