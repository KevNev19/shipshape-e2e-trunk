"""Executed decision tests for the canonical automatic-merge helper."""

import base64
import copy
import hashlib
import json
import urllib.parse

import pytest

from scripts import review_gates

REPO = "example/project"
REPO_ID = 101
REPO_NAME = "project"
REPO_URL = f"https://api.github.com/repos/{REPO}"
HEAD_REPO = "contributor/project"
HEAD_REPO_ID = 202
HEAD_REPO_NAME = "project"
HEAD_REPO_URL = f"https://api.github.com/repos/{HEAD_REPO}"
PR_NUMBER = 17
PR_ID = 1700
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
MERGE_SHA = "c" * 40
BASE_TREE_SHA = "d" * 40
HEAD_TREE_SHA = "e" * 40
BLOB_SHA = "f" * 40
RULESET_ID = 71
CONFIG_PATH = ".sdlc/config.json"
RUN_STARTED_AT = "2026-09-09T23:30:47Z"


class ResponseSequence:
    def __init__(self, *values):
        self.values = list(values)

    def next(self):
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class FakeGitHub:
    """Route exact gh API paths and record every mutation."""

    def __init__(self):
        self.responses = {}
        self.errors = {}
        self.rest_writes = []
        self.graphql_writes = []
        self.auto_merge_actor = None
        self.graphql_response = None
        self.pull_node_id = "PR_node_17"

    def set(self, path, value):
        self.responses[path] = value

    def sequence(self, path, *values):
        self.responses[path] = ResponseSequence(*values)

    def get(self, path):
        if path in self.errors:
            raise review_gates.GateError(self.errors[path])
        if path not in self.responses:
            raise AssertionError(f"unexpected GET {path}")
        response = self.responses[path]
        if isinstance(response, ResponseSequence):
            response = response.next()
        return copy.deepcopy(response)

    def graphql(self, query, variables, *, mutation=False):
        if mutation:
            self.graphql_writes.append((query, copy.deepcopy(variables)))
            return {
                "data": {"disablePullRequestAutoMerge": {"pullRequest": {"id": self.pull_node_id}}}
            }
        if self.graphql_response is not None:
            return copy.deepcopy(self.graphql_response)
        request = None
        if self.auto_merge_actor is not None:
            request = {"enabledBy": {"login": self.auto_merge_actor}}
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "id": self.pull_node_id,
                        "autoMergeRequest": request,
                    }
                }
            }
        }

    def put(self, path, payload):
        self.rest_writes.append((path, copy.deepcopy(payload)))
        return {"merged": True, "sha": "9" * 40, "message": "Pull Request merged"}


def paged(path, page=1):
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}per_page=100&page={page}"


def blob(path, mode="100644", sha=BLOB_SHA):
    return {"path": path, "mode": mode, "type": "blob", "sha": sha}


def git_blob_sha(content):
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def trusted_config(
    *, workflow_style="pr", tiered_review=True, secret_guard=True, codeql=True, languages=None
):
    return {
        "workflow_style": workflow_style,
        "languages": ["python"] if languages is None else languages,
        "features": {
            "tiered_review": tiered_review,
            "secret_guard": secret_guard,
            "codeql": codeql,
        },
    }


def install_trusted_config(api, base_entries, config=None, *, mode="100644", raw=None):
    if raw is None:
        raw = json.dumps(config or trusted_config()).encode()
    config_sha = git_blob_sha(raw)
    base_entries.append(blob(CONFIG_PATH, mode, config_sha))
    api.set(
        f"repos/{REPO}/git/blobs/{config_sha}",
        {
            "sha": config_sha,
            "encoding": "base64",
            "size": len(raw),
            "content": base64.encodebytes(raw).decode(),
        },
    )


def pull_snapshot(*, head_sha=HEAD_SHA, base_sha=BASE_SHA, path_labels=None):
    return {
        "id": PR_ID,
        "number": PR_NUMBER,
        "url": f"{REPO_URL}/pulls/{PR_NUMBER}",
        "state": "open",
        "draft": False,
        "changed_files": 1,
        "merge_commit_sha": MERGE_SHA,
        "mergeable": True,
        "mergeable_state": "clean",
        "labels": path_labels or [],
        "head": {
            "sha": head_sha,
            "ref": "docs-update",
            "repo": {
                "id": HEAD_REPO_ID,
                "name": HEAD_REPO_NAME,
                "full_name": HEAD_REPO,
                "url": HEAD_REPO_URL,
            },
        },
        "base": {
            "sha": base_sha,
            "ref": "main",
            "repo": {
                "id": REPO_ID,
                "name": REPO_NAME,
                "full_name": REPO,
                "url": REPO_URL,
            },
        },
    }


def pull_link():
    return [
        {
            "id": PR_ID,
            "number": PR_NUMBER,
            "url": f"{REPO_URL}/pulls/{PR_NUMBER}",
            "head": {
                "sha": HEAD_SHA,
                "ref": "docs-update",
                "repo": {
                    "id": HEAD_REPO_ID,
                    "name": HEAD_REPO_NAME,
                    "url": HEAD_REPO_URL,
                },
            },
            "base": {
                "sha": BASE_SHA,
                "ref": "main",
                "repo": {"id": REPO_ID, "name": REPO_NAME, "url": REPO_URL},
            },
        }
    ]


def run_record(spec, run_id, workflow_id, suite_id, **overrides):
    run = {
        "id": run_id,
        "workflow_id": workflow_id,
        "check_suite_id": suite_id,
        "head_sha": HEAD_SHA,
        "event": spec.event,
        "path": spec.workflow_path,
        "repository": {
            "id": REPO_ID,
            "name": REPO_NAME,
            "full_name": REPO,
            "url": REPO_URL,
        },
        "pull_requests": pull_link(),
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "run_started_at": RUN_STARTED_AT,
    }
    run.update(overrides)
    return run


def run_list_path(spec):
    workflow_file = urllib.parse.quote(spec.workflow_path.rsplit("/", 1)[-1], safe="")
    query = urllib.parse.urlencode({"event": spec.event, "head_sha": HEAD_SHA})
    return paged(f"repos/{REPO}/actions/workflows/{workflow_file}/runs?{query}")


def install_workflow_evidence(api, spec, run_id):
    workflow_id = run_id + 100
    suite_id = run_id + 200
    check_id = run_id + 300
    run = run_record(spec, run_id, workflow_id, suite_id)
    api.set(run_list_path(spec), {"total_count": 1, "workflow_runs": [run]})
    api.set(
        f"repos/{REPO}/actions/workflows/{workflow_id}",
        {
            "id": workflow_id,
            "name": spec.workflow_name,
            "path": spec.workflow_path,
            "state": "active",
        },
    )
    jobs_path = f"repos/{REPO}/actions/runs/{run_id}/attempts/1/jobs?filter=latest"
    api.set(
        paged(jobs_path),
        {
            "total_count": 1,
            "jobs": [
                {
                    "id": run_id + 400,
                    "name": spec.job_name,
                    "run_id": run_id,
                    "head_sha": HEAD_SHA,
                    "status": "completed",
                    "conclusion": "success",
                    "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{check_id}",
                }
            ],
        },
    )
    api.set(
        f"repos/{REPO}/check-runs/{check_id}",
        {
            "id": check_id,
            "name": spec.job_name,
            "head_sha": HEAD_SHA,
            "status": "completed",
            "conclusion": "success",
            "app": {"id": review_gates.GITHUB_ACTIONS_APP_ID},
            "check_suite": {"id": suite_id},
        },
    )
    return run


def sanitized_captured_evidence(spec, run_id, *, started_at, completed_at):
    """Representative REST run/job/check fields captured on 2026-09-09."""
    workflow_id = run_id + 100
    suite_id = run_id + 200
    check_id = run_id + 300
    run = run_record(
        spec,
        run_id,
        workflow_id,
        suite_id,
        name=spec.workflow_name,
        head_branch="docs-update",
        created_at=started_at,
        run_started_at=started_at,
        updated_at=completed_at,
    )
    job = {
        "id": run_id + 400,
        "run_id": run_id,
        "workflow_name": spec.workflow_name,
        "head_branch": "docs-update",
        "run_attempt": 1,
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "created_at": started_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "name": spec.job_name,
        "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{check_id}",
    }
    check = {
        "id": check_id,
        "name": spec.job_name,
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "started_at": started_at,
        "completed_at": completed_at,
        "app": {"id": review_gates.GITHUB_ACTIONS_APP_ID, "slug": "github-actions"},
        "check_suite": {"id": suite_id},
        "pull_requests": pull_link(),
    }
    return run, job, check


def valid_api(*, codeql=True, config=None, config_mode="100644"):
    api = FakeGitHub()
    api.set(
        f"repos/{REPO}",
        {
            "id": REPO_ID,
            "name": REPO_NAME,
            "full_name": REPO,
            "url": REPO_URL,
            "default_branch": "main",
        },
    )
    api.set(f"repos/{REPO}/pulls/{PR_NUMBER}", pull_snapshot())
    api.set(
        paged(f"repos/{REPO}/pulls/{PR_NUMBER}/files"),
        [{"filename": "README.md", "status": "modified"}],
    )
    base_entries = [
        blob("README.md"),
        blob(".github/workflows/ci.yml"),
        blob(".github/workflows/secret-scan.yml"),
    ]
    if codeql:
        base_entries.append(blob(".github/workflows/codeql.yml"))
    install_trusted_config(
        api,
        base_entries,
        config or trusted_config(codeql=codeql),
        mode=config_mode,
    )
    api.set(
        f"repos/{REPO}/git/commits/{BASE_SHA}",
        {"sha": BASE_SHA, "tree": {"sha": BASE_TREE_SHA}},
    )
    api.set(
        f"repos/{REPO}/git/trees/{BASE_TREE_SHA}?recursive=1",
        {"truncated": False, "tree": base_entries},
    )
    api.set(
        f"repos/{REPO}/git/commits/{HEAD_SHA}",
        {"sha": HEAD_SHA, "tree": {"sha": HEAD_TREE_SHA}},
    )
    api.set(
        f"repos/{REPO}/git/trees/{HEAD_TREE_SHA}?recursive=1",
        {"truncated": False, "tree": [blob("README.md")]},
    )

    gates = review_gates.BASE_GATES + ((review_gates.CODEQL_GATE,) if codeql else ())
    active_path = paged(f"repos/{REPO}/rules/branches/main")
    api.set(
        active_path,
        [{"type": "required_status_checks", "ruleset_id": RULESET_ID}],
    )
    api.set(
        f"repos/{REPO}/rulesets/{RULESET_ID}",
        {
            "id": RULESET_ID,
            "name": review_gates.RULESET_NAME,
            "target": "branch",
            "source_type": "Repository",
            "source": REPO,
            "enforcement": "active",
            "current_user_can_bypass": "never",
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "do_not_enforce_on_create": False,
                        "required_status_checks": [
                            {"context": gate.context, "integration_id": gate.app_id}
                            for gate in gates
                        ],
                    },
                }
            ],
        },
    )
    for index, spec in enumerate(gates, start=1):
        install_workflow_evidence(api, spec, 1000 + index)
    if codeql:
        codeql_query = urllib.parse.urlencode(
            {"check_name": review_gates.CODEQL_GATE.context, "filter": "latest"}
        )
        api.set(
            paged(f"repos/{REPO}/commits/{HEAD_SHA}/check-runs?{codeql_query}"),
            {
                "total_count": 1,
                "check_runs": [
                    {
                        "id": 5001,
                        "name": "CodeQL",
                        "head_sha": HEAD_SHA,
                        "status": "completed",
                        "conclusion": "success",
                        "app": {"id": review_gates.CODEQL_APP_ID},
                        "check_suite": {"id": 6001},
                    }
                ],
            },
        )
        api.set(
            f"repos/{REPO}/check-suites/6001",
            {
                "id": 6001,
                "head_sha": HEAD_SHA,
                "app": {"id": review_gates.CODEQL_APP_ID},
            },
        )
    return api


def assert_no_merge(api):
    assert api.rest_writes == []


@pytest.mark.parametrize(
    "config",
    [
        trusted_config(tiered_review=False),
        trusted_config(workflow_style="trunk"),
        trusted_config(secret_guard=False),
    ],
)
def test_trusted_base_config_must_explicitly_enable_automatic_authority(config):
    api = valid_api(config=config)

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert result["merged"] is False
    assert "configuration" in result["reason"]
    assert_no_merge(api)


def test_missing_or_symlinked_trusted_base_config_never_merges():
    for mode in (None, "120000"):
        api = valid_api()
        tree_path = f"repos/{REPO}/git/trees/{BASE_TREE_SHA}?recursive=1"
        entries = api.responses[tree_path]["tree"]
        if mode is None:
            entries[:] = [entry for entry in entries if entry["path"] != CONFIG_PATH]
        else:
            next(entry for entry in entries if entry["path"] == CONFIG_PATH)["mode"] = mode

        result = review_gates.evaluate(api, REPO, PR_NUMBER)

        assert result["eligible"] is False
        assert_no_merge(api)


def test_malformed_trusted_base_config_never_merges():
    api = valid_api()
    tree_path = f"repos/{REPO}/git/trees/{BASE_TREE_SHA}?recursive=1"
    entries = api.responses[tree_path]["tree"]
    entries[:] = [entry for entry in entries if entry["path"] != CONFIG_PATH]
    install_trusted_config(api, entries, raw=b"not json")

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert "configuration" in result["reason"]
    assert_no_merge(api)


@pytest.mark.parametrize("field", ["encoding", "size", "sha", "content"])
def test_trusted_config_blob_requires_exact_encoding_size_and_identity(field):
    api = valid_api()
    tree_path = f"repos/{REPO}/git/trees/{BASE_TREE_SHA}?recursive=1"
    entry = next(item for item in api.responses[tree_path]["tree"] if item["path"] == CONFIG_PATH)
    payload = api.responses[f"repos/{REPO}/git/blobs/{entry['sha']}"]
    mutations = {
        "encoding": "utf-8",
        "size": payload["size"] + 1,
        "sha": "1" * 40,
        "content": payload["content"] + " ",
    }
    payload[field] = mutations[field]

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert "configuration" in result["reason"]
    assert_no_merge(api)


def test_configured_applicable_codeql_missing_workflow_never_merges():
    api = valid_api(codeql=False, config=trusted_config(codeql=True, languages=["python"]))

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert "CodeQL" in result["reason"]
    assert_no_merge(api)


def test_enabled_codeql_is_inapplicable_for_unsupported_primary_language():
    api = valid_api(codeql=False, config=trusted_config(codeql=True, languages=["rust"]))

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["merged"] is True


def test_disabled_codeql_ignores_a_retained_workflow_and_extra_strict_gate():
    api = valid_api(codeql=True, config=trusted_config(codeql=False))

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["merged"] is True


def test_positive_ordinary_document_merges_only_at_expected_head():
    api = valid_api()

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["classification"] == "docs-only"
    assert result["eligible"] is True
    assert result["merged"] is True
    assert api.rest_writes == [
        (
            f"repos/{REPO}/pulls/{PR_NUMBER}/merge",
            {"sha": HEAD_SHA, "merge_method": "squash"},
        )
    ]
    assert "content semantics were not assessed" in result["reason"]


def test_live_token_ruleset_shape_does_not_require_admin_only_bypass_actors():
    api = valid_api()
    ruleset = api.responses[f"repos/{REPO}/rulesets/{RULESET_ID}"]

    assert "bypass_actors" not in ruleset

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["merged"] is True


@pytest.mark.parametrize(
    ("files", "base_paths", "head_paths"),
    [
        (
            [{"filename": "docs/guides/AGENTS.md", "status": "modified"}],
            [blob("docs/guides/AGENTS.md")],
            [blob("docs/guides/AGENTS.md")],
        ),
        (
            [
                {
                    "filename": "docs/guide.md",
                    "previous_filename": ".github/agents/reviewer.md",
                    "status": "renamed",
                }
            ],
            [blob(".github/agents/reviewer.md")],
            [blob("docs/guide.md")],
        ),
        (
            [{"filename": "package.json", "status": "modified"}],
            [blob("package.json")],
            [blob("package.json")],
        ),
        (
            [{"filename": "docs/security/model.md", "status": "modified"}],
            [blob("docs/security/model.md")],
            [blob("docs/security/model.md")],
        ),
        (
            [{"filename": "docs/.private/guide.md", "status": "modified"}],
            [blob("docs/.private/guide.md")],
            [blob("docs/.private/guide.md")],
        ),
        (
            [{"filename": "notes.md", "status": "modified"}],
            [blob("notes.md")],
            [blob("notes.md")],
        ),
        (
            [{"filename": "LICENSE.md", "status": "modified"}],
            [blob("LICENSE.md")],
            [blob("LICENSE.md")],
        ),
        (
            [{"filename": "CODE_OF_CONDUCT.md", "status": "modified"}],
            [blob("CODE_OF_CONDUCT.md")],
            [blob("CODE_OF_CONDUCT.md")],
        ),
        (
            [{"filename": "CONTRIBUTING.md", "status": "modified"}],
            [blob("CONTRIBUTING.md")],
            [blob("CONTRIBUTING.md")],
        ),
        (
            [{"filename": "docs/legal/terms.md", "status": "modified"}],
            [blob("docs/legal/terms.md")],
            [blob("docs/legal/terms.md")],
        ),
        (
            [{"filename": "docs/governance/voting.md", "status": "modified"}],
            [blob("docs/governance/voting.md")],
            [blob("docs/governance/voting.md")],
        ),
        (
            [{"filename": "docs/privacy.md", "status": "modified"}],
            [blob("docs/privacy.md")],
            [blob("docs/privacy.md")],
        ),
        (
            [{"filename": "docs/NOTICE.md", "status": "modified"}],
            [blob("docs/NOTICE.md")],
            [blob("docs/NOTICE.md")],
        ),
        (
            [{"filename": "docs/process/release.md", "status": "modified"}],
            [blob("docs/process/release.md")],
            [blob("docs/process/release.md")],
        ),
        (
            [{"filename": "docs/runbooks/deploy.md", "status": "modified"}],
            [blob("docs/runbooks/deploy.md")],
            [blob("docs/runbooks/deploy.md")],
        ),
        (
            [{"filename": "docs/adrs/0010-architecture.md", "status": "modified"}],
            [blob("docs/adrs/0010-architecture.md")],
            [blob("docs/adrs/0010-architecture.md")],
        ),
        (
            [{"filename": "docs/decisions/0010-platform.md", "status": "modified"}],
            [blob("docs/decisions/0010-platform.md")],
            [blob("docs/decisions/0010-platform.md")],
        ),
        (
            [
                {
                    "filename": "docs/architecture-decisions/0010-platform.md",
                    "status": "modified",
                }
            ],
            [blob("docs/architecture-decisions/0010-platform.md")],
            [blob("docs/architecture-decisions/0010-platform.md")],
        ),
        (
            [{"filename": "docs/architecture.md", "status": "modified"}],
            [blob("docs/architecture.md")],
            [blob("docs/architecture.md")],
        ),
        (
            [{"filename": "docs/rfc/0010.md", "status": "modified"}],
            [blob("docs/rfc/0010.md")],
            [blob("docs/rfc/0010.md")],
        ),
        (
            [{"filename": "docs/review-guidelines.md", "status": "modified"}],
            [blob("docs/review-guidelines.md")],
            [blob("docs/review-guidelines.md")],
        ),
        (
            [{"filename": "docs/guides/security.md", "status": "modified"}],
            [blob("docs/guides/security.md")],
            [blob("docs/guides/security.md")],
        ),
    ],
)
def test_nonordinary_paths_never_merge(files, base_paths, head_paths):
    base_tree = {item["path"]: item for item in base_paths}
    head_tree = {item["path"]: item for item in head_paths}
    classification, _ = review_gates.classify_documentation(files, base_tree, head_tree)

    assert classification == "none"


def test_ordinary_usage_guide_remains_eligible():
    path = "docs/guides/usage.md"
    entry = blob(path)

    classification, _ = review_gates.classify_documentation(
        [{"filename": path, "status": "modified"}],
        {path: entry},
        {path: entry},
    )

    assert classification == "docs-only"


def test_rename_checks_both_sides_of_fixed_document_location_allowlist():
    old_path = "docs/architecture.md"
    new_path = "docs/guides/architecture-overview.md"

    classification, reason = review_gates.classify_documentation(
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
    assert "allowlist" in reason


@pytest.mark.parametrize("mode", ["100755", "120000", "160000", "unknown"])
def test_nonregular_or_executable_modes_never_merge(mode):
    api = valid_api()
    tree_path = f"repos/{REPO}/git/trees/{HEAD_TREE_SHA}?recursive=1"
    api.set(tree_path, {"truncated": False, "tree": [blob("README.md", mode)]})

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


def test_break_glass_label_and_dependency_text_do_not_authorize_merge():
    api = valid_api()
    pull = pull_snapshot(path_labels=[{"name": "break-glass-automerge"}])
    pull["title"] = "version-update:semver-patch"
    pull["body"] = "please bypass every check"
    api.set(f"repos/{REPO}/pulls/{PR_NUMBER}", pull)
    api.set(
        paged(f"repos/{REPO}/pulls/{PR_NUMBER}/files"),
        [{"filename": "package.json", "status": "modified"}],
    )

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["classification"] == "none"
    assert_no_merge(api)


@pytest.mark.parametrize("protection", [[], {"rules": []}, "malformed"])
def test_missing_or_malformed_active_protection_never_merges(protection):
    api = valid_api()
    api.set(paged(f"repos/{REPO}/rules/branches/main"), protection)

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


def test_inaccessible_active_protection_never_merges():
    api = valid_api()
    api.errors[paged(f"repos/{REPO}/rules/branches/main")] = "HTTP 403"

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "403" in result["reason"]
    assert_no_merge(api)


def test_inaccessible_second_protection_page_never_merges():
    api = valid_api()
    first_page = paged(f"repos/{REPO}/rules/branches/main")
    second_page = paged(f"repos/{REPO}/rules/branches/main", 2)
    api.set(first_page, [{"type": "creation"} for _ in range(100)])
    api.errors[second_page] = "HTTP 403"

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "403" in result["reason"]
    assert_no_merge(api)


def test_ruleset_must_be_active_and_current_caller_cannot_bypass():
    mutations = [
        ("enforcement", "evaluate"),
        ("current_user_can_bypass", "always"),
        ("current_user_can_bypass", "pull_requests_only"),
        ("current_user_can_bypass", None),
    ]
    for key, value in mutations:
        api = valid_api()
        ruleset_path = f"repos/{REPO}/rulesets/{RULESET_ID}"
        ruleset = api.responses[ruleset_path]
        ruleset[key] = value

        result = review_gates.evaluate(api, REPO, PR_NUMBER)

        assert result["eligible"] is False
        assert_no_merge(api)


def test_ruleset_missing_current_caller_bypass_proof_never_merges():
    api = valid_api()
    ruleset_path = f"repos/{REPO}/rulesets/{RULESET_ID}"
    del api.responses[ruleset_path]["current_user_can_bypass"]

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert "bypass" in result["reason"]
    assert_no_merge(api)


def test_stricter_extra_source_bound_checks_and_rules_are_preserved():
    api = valid_api()
    ruleset = api.responses[f"repos/{REPO}/rulesets/{RULESET_ID}"]
    required = ruleset["rules"][0]["parameters"]["required_status_checks"]
    required.append({"context": "lint", "integration_id": 4242})
    ruleset["rules"].append(
        {
            "type": "pull_request",
            "parameters": {"required_approving_review_count": 2},
        }
    )

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["merged"] is True


@pytest.mark.parametrize("conclusion", [None, "failure", "neutral", "skipped", "unknown"])
def test_latest_incomplete_or_unsuccessful_workflow_never_merges(conclusion):
    api = valid_api()
    spec = review_gates.BASE_GATES[0]
    payload = api.responses[run_list_path(spec)]
    run = payload["workflow_runs"][0]
    run["status"] = "in_progress" if conclusion is None else "completed"
    run["conclusion"] = conclusion

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_sha", "8" * 40),
        ("path", ".github/workflows/not-ci.yml"),
        ("event", "push"),
    ],
)
def test_wrong_head_path_or_event_workflow_never_merges(field, value):
    api = valid_api()
    payload = api.responses[run_list_path(review_gates.BASE_GATES[0])]
    payload["workflow_runs"][0][field] = value

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


def test_workflow_path_with_ref_suffix_remains_unsupported_and_fails_closed():
    api = valid_api()
    run = api.responses[run_list_path(review_gates.BASE_GATES[0])]["workflow_runs"][0]
    run["path"] = f"{review_gates.BASE_GATES[0].workflow_path}@refs/heads/main"

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "not bound to this pull request" in result["reason"]
    assert_no_merge(api)


@pytest.mark.parametrize(
    ("spec", "run_id", "started_at", "completed_at"),
    [
        (
            review_gates.BASE_GATES[0],
            3101,
            "2026-09-09T23:30:47Z",
            "2026-09-09T23:30:59Z",
        ),
        (
            review_gates.CODEQL_GATE,
            3201,
            "2026-09-09T23:30:47Z",
            "2026-09-09T23:31:56Z",
        ),
    ],
)
def test_sanitized_captured_run_job_check_shapes_validate_exact_pr_head(
    spec, run_id, started_at, completed_at
):
    api = FakeGitHub()
    run, job, check = sanitized_captured_evidence(
        spec,
        run_id,
        started_at=started_at,
        completed_at=completed_at,
    )
    api.set(f"repos/{REPO}/pulls/{PR_NUMBER}", pull_snapshot())
    api.set(
        f"repos/{REPO}/actions/workflows/{run['workflow_id']}",
        {
            "id": run["workflow_id"],
            "name": spec.workflow_name,
            "path": spec.workflow_path,
            "state": "active",
        },
    )
    api.set(
        paged(f"repos/{REPO}/actions/runs/{run_id}/attempts/1/jobs?filter=latest"),
        {"total_count": 1, "jobs": [job]},
    )
    api.set(f"repos/{REPO}/check-runs/{check['id']}", check)
    snapshot = review_gates._pull_snapshot(api, REPO, PR_NUMBER)

    review_gates._validate_run_identity(api, run, REPO, PR_NUMBER, snapshot, spec)
    review_gates._verify_actions_job(api, REPO, snapshot, spec, run)


def test_sanitized_captured_pull_request_target_shape_binds_pr_head_not_base_sha():
    api = FakeGitHub()
    spec = review_gates.BASE_GATES[1]
    run, _, _ = sanitized_captured_evidence(
        spec,
        3301,
        started_at="2026-09-09T22:39:28Z",
        completed_at="2026-09-09T22:39:36Z",
    )
    run["status"] = "completed"
    run["conclusion"] = "failure"
    api.set(f"repos/{REPO}/pulls/{PR_NUMBER}", pull_snapshot())
    api.set(
        f"repos/{REPO}/actions/workflows/{run['workflow_id']}",
        {
            "id": run["workflow_id"],
            "name": spec.workflow_name,
            "path": spec.workflow_path,
            "state": "active",
        },
    )
    snapshot = review_gates._pull_snapshot(api, REPO, PR_NUMBER)

    review_gates._validate_run_identity(api, run, REPO, PR_NUMBER, snapshot, spec)

    assert run["head_sha"] == snapshot["head_sha"]
    assert run["head_sha"] != snapshot["base_sha"]


def test_wrong_check_app_never_merges():
    api = valid_api()
    check_path = f"repos/{REPO}/check-runs/{1301}"
    api.responses[check_path]["app"]["id"] = 999

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


@pytest.mark.parametrize("field", ["repository", "base", "check_suite"])
def test_wrong_repository_base_or_check_suite_binding_never_merges(field):
    api = valid_api()
    spec = review_gates.BASE_GATES[0]
    run = api.responses[run_list_path(spec)]["workflow_runs"][0]
    if field == "repository":
        run["repository"]["full_name"] = "other/project"
    elif field == "base":
        run["pull_requests"][0]["base"]["sha"] = "8" * 40
    else:
        api.responses[f"repos/{REPO}/check-runs/1301"]["check_suite"]["id"] = 999

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("pull", "id", 999),
        ("head_repo", "id", 999),
        ("head_repo", "url", "https://evil.test/head"),
        ("base_repo", "id", 999),
        ("base_repo", "url", "https://evil.test/base"),
    ],
)
def test_actual_shaped_workflow_pull_link_rejects_spoofed_repo_identity(section, field, value):
    api = valid_api()
    run = api.responses[run_list_path(review_gates.BASE_GATES[0])]["workflow_runs"][0]
    link = run["pull_requests"][0]
    if section == "pull":
        target = link
    elif section == "head_repo":
        target = link["head"]["repo"]
    else:
        target = link["base"]["repo"]
    target[field] = value

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert "bound to this pull request" in result["reason"]
    assert_no_merge(api)


def test_missing_check_results_never_merge():
    api = valid_api()
    spec = review_gates.BASE_GATES[0]
    run = api.responses[run_list_path(spec)]["workflow_runs"][0]
    jobs_path = paged(f"repos/{REPO}/actions/runs/{run['id']}/attempts/1/jobs?filter=latest")
    api.set(jobs_path, {"total_count": 0, "jobs": []})

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


def test_failed_latest_rerun_overrides_earlier_success():
    api = valid_api()
    spec = review_gates.BASE_GATES[0]
    runs_path = run_list_path(spec)
    older = api.responses[runs_path]["workflow_runs"][0]
    newer = copy.deepcopy(older)
    newer["id"] = older["id"] + 50
    newer["run_started_at"] = "2026-09-09T23:40:47Z"
    newer["conclusion"] = "failure"
    api.set(runs_path, {"total_count": 2, "workflow_runs": [older, newer]})

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "latest test workflow run was not successful" in result["reason"]
    assert_no_merge(api)


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("completed", "failure"), ("in_progress", None)],
)
def test_chronologically_later_lower_id_rerun_overrides_older_green(status, conclusion):
    api = valid_api()
    spec = review_gates.BASE_GATES[0]
    runs_path = run_list_path(spec)
    older = api.responses[runs_path]["workflow_runs"][0]
    later = copy.deepcopy(older)
    later["id"] = older["id"] - 50
    later["run_started_at"] = "2026-09-09T23:40:47Z"
    later["status"] = status
    later["conclusion"] = conclusion
    api.set(runs_path, {"total_count": 2, "workflow_runs": [older, later]})

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "latest test workflow run was not successful" in result["reason"]
    assert_no_merge(api)


@pytest.mark.parametrize("timestamp", [None, "", "2026-09-09", "not-a-timestamp"])
def test_malformed_workflow_run_chronology_never_merges(timestamp):
    api = valid_api()
    spec = review_gates.BASE_GATES[0]
    run = api.responses[run_list_path(spec)]["workflow_runs"][0]
    run["run_started_at"] = timestamp

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "chronology" in result["reason"]
    assert_no_merge(api)


def test_ambiguous_workflow_run_chronology_never_cherry_picks_green():
    api = valid_api()
    spec = review_gates.BASE_GATES[0]
    runs_path = run_list_path(spec)
    first = api.responses[runs_path]["workflow_runs"][0]
    second = copy.deepcopy(first)
    second["id"] = first["id"] + 1
    second["conclusion"] = "failure"
    api.set(runs_path, {"total_count": 2, "workflow_runs": [first, second]})

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "chronology was ambiguous" in result["reason"]
    assert_no_merge(api)


def test_wrong_codeql_app_never_merges():
    api = valid_api()
    query = urllib.parse.urlencode({"check_name": "CodeQL", "filter": "latest"})
    path = paged(f"repos/{REPO}/commits/{HEAD_SHA}/check-runs?{query}")
    api.responses[path]["check_runs"][0]["app"]["id"] = 999

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


@pytest.mark.parametrize("changed_field", ["head", "base"])
def test_changed_head_or_base_before_merge_stops(changed_field):
    api = valid_api()
    changed = pull_snapshot()
    changed[changed_field]["sha"] = "7" * 40
    api.sequence(f"repos/{REPO}/pulls/{PR_NUMBER}", pull_snapshot(), changed)

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "changed before merge" in result["reason"]
    assert_no_merge(api)


def test_policy_loaded_from_stale_base_never_authorizes_current_pull_request():
    api = valid_api()

    result = review_gates.evaluate(
        api,
        REPO,
        PR_NUMBER,
        trusted_base_sha="7" * 40,
    )

    assert "policy commit no longer matches" in result["reason"]
    assert_no_merge(api)


def test_policy_from_non_default_target_branch_never_authorizes_merge():
    api = valid_api()
    pull = pull_snapshot()
    pull["base"]["ref"] = "release"
    api.set(f"repos/{REPO}/pulls/{PR_NUMBER}", pull)
    for spec in review_gates.required_gates(True):
        run = api.responses[run_list_path(spec)]["workflow_runs"][0]
        run["pull_requests"][0]["base"]["ref"] = "release"

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert "default branch" in result["reason"]
    assert_no_merge(api)


def test_truncated_tree_never_merges():
    api = valid_api()
    api.set(
        f"repos/{REPO}/git/trees/{HEAD_TREE_SHA}?recursive=1",
        {"truncated": True, "tree": [blob("README.md")]},
    )

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


@pytest.mark.parametrize("files", [[], {"files": []}, "malformed"])
def test_empty_or_malformed_file_pages_never_merge(files):
    api = valid_api()
    api.set(paged(f"repos/{REPO}/pulls/{PR_NUMBER}/files"), files)

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert_no_merge(api)


@pytest.mark.parametrize("actor", ["github-actions[bot]", "maintainer", "merge-app[bot]"])
def test_existing_persistent_auto_merge_blocks_with_manual_migration(actor):
    api = valid_api()
    api.auto_merge_actor = actor
    api.set(
        paged(f"repos/{REPO}/pulls/{PR_NUMBER}/files"),
        [{"filename": "src/app.py", "status": "modified"}],
    )

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["eligible"] is False
    assert result["persistent_auto_merge_present"] is True
    assert "cancel auto-merge manually" in result["reason"].lower()
    assert api.graphql_writes == []
    assert_no_merge(api)


def test_persistent_auto_merge_blocks_even_when_pr_becomes_draft():
    api = valid_api()
    api.auto_merge_actor = "github-actions[bot]"
    draft = pull_snapshot()
    draft["draft"] = True
    api.set(f"repos/{REPO}/pulls/{PR_NUMBER}", draft)

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["persistent_auto_merge_present"] is True
    assert "cancel auto-merge manually" in result["reason"].lower()
    assert api.graphql_writes == []
    assert_no_merge(api)


def test_absent_persistent_auto_merge_does_not_block_evaluation():
    api = valid_api()
    api.set(
        paged(f"repos/{REPO}/pulls/{PR_NUMBER}/files"),
        [{"filename": "src/app.py", "status": "modified"}],
    )

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["persistent_auto_merge_present"] is False
    assert api.graphql_writes == []
    assert_no_merge(api)


def test_partial_graphql_errors_never_prove_persistent_auto_merge_absent():
    api = valid_api()
    api.graphql_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "id": api.pull_node_id,
                    "autoMergeRequest": None,
                }
            }
        },
        "errors": [{"message": "field could not be resolved"}],
    }

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["persistent_auto_merge_present"] is False
    assert "GraphQL errors" in result["reason"]
    assert_no_merge(api)


@pytest.mark.parametrize("errors", [None, {}, "error"])
def test_malformed_graphql_errors_state_remains_unknown(errors):
    api = valid_api()
    api.graphql_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "id": api.pull_node_id,
                    "autoMergeRequest": None,
                }
            }
        },
        "errors": errors,
    }

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["ok"] is False
    assert "GraphQL errors" in result["reason"]
    assert_no_merge(api)


def test_error_free_but_malformed_graphql_data_remains_unknown():
    api = valid_api()
    api.graphql_response = {"data": {"repository": None}, "errors": []}

    result = review_gates.evaluate(api, REPO, PR_NUMBER)

    assert result["ok"] is False
    assert "auto-merge repository was malformed" in result["reason"]
    assert_no_merge(api)


def test_unrelated_workflow_run_wakeup_never_merges():
    api = valid_api()
    run_id = 9001
    api.set(
        f"repos/{REPO}/actions/runs/{run_id}",
        {
            "id": run_id,
            "path": ".github/workflows/unrelated.yml",
            "repository": {"full_name": REPO},
            "pull_requests": pull_link(),
        },
    )

    result = review_gates.evaluate(api, REPO, PR_NUMBER, run_id)

    assert "trusted workflow" in result["reason"]
    assert_no_merge(api)


def test_doctor_protection_interface_reuses_exact_contract():
    api = valid_api()

    result = review_gates.inspect_protection(api, REPO, "main", True)

    assert result == {
        "ok": True,
        "ruleset_name": review_gates.RULESET_NAME,
        "ruleset_id": RULESET_ID,
        "required_checks": [
            {"context": gate.context, "integration_id": gate.app_id}
            for gate in review_gates.required_gates(True)
        ],
        "reason": "strict source-bound required checks are active",
    }
