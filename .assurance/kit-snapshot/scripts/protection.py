#!/usr/bin/env python3
"""Plan or add Shipshape-owned GitHub repository rulesets.

Planning inspects existing classic protection and rulesets without mutation.
Applying may create one separate named ruleset. It never updates or deletes
classic protection, an existing ruleset, or any user-owned setting.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import urllib.parse

try:
    from scripts import review_gates
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    import review_gates  # type: ignore[no-redef]

API_PAGE_SIZE = 100
MAX_RULESETS = 3000
BASELINE_RULESET_NAME = review_gates.BASELINE_RULESET_NAME


class GitHubAdminClient:
    """JSON adapter for the small set of ruleset administration calls used here."""

    def _call(self, arguments: list[str], payload: dict | None = None, *, optional=False):
        try:
            process = subprocess.run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github+json",
                    "-H",
                    "X-GitHub-Api-Version: 2022-11-28",
                    *arguments,
                ],
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise review_gates.GateError(
                "GitHub API command was unavailable or timed out"
            ) from error
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or "GitHub API request failed"
            if optional and "HTTP 404" in detail:
                return None
            raise review_gates.GateError(detail)
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise review_gates.GateError("GitHub API returned malformed JSON") from error

    def get(self, path: str):
        return self._call([path])

    def get_optional(self, path: str):
        return self._call([path], optional=True)

    def post(self, path: str, payload: dict):
        return self._call(["--method", "POST", path, "--input", "-"], payload)


def _validate_branch(branch: str) -> str:
    if (
        not isinstance(branch, str)
        or not branch
        or branch.startswith("refs/")
        or any(character in branch for character in "\x00\r\n")
    ):
        raise review_gates.GateError("branch must be a non-empty branch name")
    return branch


def _list_rulesets(api, repo: str) -> list[dict]:
    rulesets = []
    base = f"repos/{repo}/rulesets?includes_parents=true"
    for page in range(1, MAX_RULESETS // API_PAGE_SIZE + 2):
        payload = api.get(f"{base}&per_page={API_PAGE_SIZE}&page={page}")
        if not isinstance(payload, list) or len(payload) > API_PAGE_SIZE:
            raise review_gates.GateError("repository ruleset pagination was malformed")
        if not all(isinstance(item, dict) for item in payload):
            raise review_gates.GateError("repository ruleset list contained malformed records")
        rulesets.extend(payload)
        if len(rulesets) > MAX_RULESETS:
            raise review_gates.GateError("repository rulesets exceeded the inspection limit")
        if len(payload) < API_PAGE_SIZE:
            return rulesets
    raise review_gates.GateError("repository ruleset pagination did not terminate")


def ruleset_proposal(
    branch: str,
    gates: tuple[review_gates.GateSpec, ...],
    name: str,
    *,
    require_review: bool,
) -> dict:
    """Build the complete payload for one narrowly targeted additive ruleset."""
    _validate_branch(branch)
    if not gates or not all(isinstance(gate, review_gates.GateSpec) for gate in gates):
        raise review_gates.GateError("at least one known GateSpec is required")
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "do_not_enforce_on_create": False,
                "required_status_checks": [
                    {"context": gate.context, "integration_id": gate.app_id} for gate in gates
                ],
            },
        }
    ]
    if require_review:
        rules.append(
            {
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
        )
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": [f"refs/heads/{branch}"], "exclude": []}},
        "rules": rules,
    }


def trunk_ruleset_proposal(branch: str) -> dict:
    """Build safeguards that do not depend on post-push CI completing first."""
    _validate_branch(branch)
    return {
        "name": BASELINE_RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": [f"refs/heads/{branch}"], "exclude": []}},
        "rules": [{"type": rule_type} for rule_type in review_gates.TRUNK_RULE_TYPES],
    }


def _desired_policy(
    branch: str,
    workflow_style: str,
    tiered_review: bool,
    codeql_enabled: bool,
    require_review: bool,
) -> dict:
    if workflow_style not in {"trunk", "pr"}:
        raise review_gates.GateError("workflow style must be exactly 'trunk' or 'pr'")
    if any(type(value) is not bool for value in (tiered_review, codeql_enabled, require_review)):
        raise review_gates.GateError("protection options must be exact booleans")
    if workflow_style == "trunk":
        if tiered_review or require_review:
            raise review_gates.GateError(
                "trunk protection cannot enable PR-only tiered review or approval rules"
            )
        return trunk_ruleset_proposal(branch)
    if tiered_review:
        gates = review_gates.required_gates(codeql_enabled)
        name = review_gates.RULESET_NAME
    else:
        gates = (review_gates.BASE_GATES[0],)
        if codeql_enabled:
            gates += (review_gates.CODEQL_GATE,)
        name = BASELINE_RULESET_NAME
    return ruleset_proposal(branch, gates, name, require_review=require_review)


def _owned_ruleset_matches(detail: dict, proposal: dict, repo: str) -> bool:
    return (
        isinstance(detail, dict)
        and type(detail.get("id")) is int
        and detail["id"] > 0
        and detail.get("source_type") == "Repository"
        and detail.get("source") == repo
        and detail.get("current_user_can_bypass") == "never"
        and all(detail.get(key) == value for key, value in proposal.items())
    )


def _empty_result(proposal: dict | None = None, workflow_style: str | None = None) -> dict:
    return {
        "ok": False,
        "dry_run": True,
        "action": "refuse",
        "reason": "protection planning did not complete",
        "workflow_style": workflow_style,
        "pre_landing_checks": "unknown",
        "ruleset_id": None,
        "proposal": proposal,
        "observed": {"classic_protection": "unknown", "ruleset_ids": []},
        "preservation": {
            "classic_protection": "unchanged",
            "reviews": "unchanged",
            "restrictions": "unchanged",
            "enforcement": "unchanged",
            "checks": "unchanged",
            "bypasses": "unchanged",
            "unrelated_rulesets": "unchanged",
        },
    }


def _validate_repository_and_branch(api, repo: str, branch: str) -> None:
    repository = review_gates._require_dict(api.get(f"repos/{repo}"), "repository")
    permissions = repository.get("permissions")
    if repository.get("full_name") != repo:
        raise review_gates.GateError("repository identity did not match the requested slug")
    if not isinstance(permissions, dict) or permissions.get("admin") is not True:
        raise review_gates.GateError(
            "repository administration permission was absent or could not be proven"
        )

    encoded_branch = urllib.parse.quote(branch, safe="")
    branch_payload = review_gates._require_dict(
        api.get(f"repos/{repo}/branches/{encoded_branch}"), "branch"
    )
    commit = review_gates._require_dict(branch_payload.get("commit"), "branch commit")
    if branch_payload.get("name") != branch:
        raise review_gates.GateError("branch identity did not match the requested branch")
    review_gates._require_sha(commit.get("sha"), "branch commit")


def _inspect_controls(api, repo: str, branch: str) -> tuple[str, list[dict]]:
    encoded_branch = urllib.parse.quote(branch, safe="")
    classic = api.get_optional(f"repos/{repo}/branches/{encoded_branch}/protection")
    if classic is not None and not isinstance(classic, dict):
        raise review_gates.GateError("classic branch protection was malformed")

    details = []
    seen_ids = set()
    for summary in _list_rulesets(api, repo):
        ruleset_id = summary.get("id")
        if (
            type(ruleset_id) is not int
            or ruleset_id <= 0
            or ruleset_id in seen_ids
            or not isinstance(summary.get("name"), str)
            or not isinstance(summary.get("source_type"), str)
            or not isinstance(summary.get("source"), str)
            or not isinstance(summary.get("enforcement"), str)
        ):
            raise review_gates.GateError("repository ruleset list contained incomplete records")
        seen_ids.add(ruleset_id)
        detail = review_gates._require_dict(
            api.get(f"repos/{repo}/rulesets/{ruleset_id}"), "repository ruleset"
        )
        if detail.get("id") != ruleset_id or detail.get("name") != summary["name"]:
            raise review_gates.GateError("repository ruleset identity changed during planning")
        details.append(detail)
    return "present" if classic is not None else "absent", details


def plan_protection(
    api,
    repo: str,
    branch: str,
    *,
    workflow_style: str,
    tiered_review: bool = False,
    codeql_enabled: bool = False,
    require_review: bool = False,
) -> dict:
    """Inspect all protection and return a JSON-safe additive plan."""
    result = _empty_result(workflow_style=workflow_style)
    try:
        review_gates._require_repo(repo)
        branch = _validate_branch(branch)
        proposal = _desired_policy(
            branch,
            workflow_style,
            tiered_review,
            codeql_enabled,
            require_review,
        )
        result["proposal"] = proposal
        result["pre_landing_checks"] = (
            "not configured; CI runs after direct pushes"
            if workflow_style == "trunk"
            else "strict source-bound required checks"
        )
        _validate_repository_and_branch(api, repo, branch)
        classic_state, details = _inspect_controls(api, repo, branch)
        result["observed"]["classic_protection"] = classic_state
        result["observed"]["ruleset_ids"] = [item["id"] for item in details]

        same_name = [item for item in details if item.get("name") == proposal["name"]]
        if not same_name:
            result.update(
                ok=True,
                action="create",
                reason="create one separate Shipshape-owned additive ruleset",
            )
            return result
        if len(same_name) == 1 and _owned_ruleset_matches(same_name[0], proposal, repo):
            result.update(
                ok=True,
                action="unchanged",
                ruleset_id=same_name[0]["id"],
                reason="the exact Shipshape-owned ruleset is already active",
            )
            return result
        raise review_gates.GateError(
            "a same-name ruleset is unfamiliar or modified; review it manually before retrying"
        )
    except review_gates.GateError as error:
        result["reason"] = str(error)
        return result


def apply_protection(
    api,
    repo: str,
    branch: str,
    *,
    workflow_style: str,
    tiered_review: bool = False,
    codeql_enabled: bool = False,
    require_review: bool = False,
) -> dict:
    """Create only a missing exact ruleset and verify it without cleanup mutations."""
    result = plan_protection(
        api,
        repo,
        branch,
        workflow_style=workflow_style,
        tiered_review=tiered_review,
        codeql_enabled=codeql_enabled,
        require_review=require_review,
    )
    result = copy.deepcopy(result)
    result["dry_run"] = False
    if not result["ok"] or result["action"] == "unchanged":
        return result
    try:
        created = review_gates._require_dict(
            api.post(f"repos/{repo}/rulesets", result["proposal"]),
            "created ruleset",
        )
        ruleset_id = created.get("id")
        if type(ruleset_id) is not int or ruleset_id <= 0:
            raise review_gates.GateError("created ruleset identity was unavailable")
        result["ruleset_id"] = ruleset_id
        detail = review_gates._require_dict(
            api.get(f"repos/{repo}/rulesets/{ruleset_id}"), "created ruleset"
        )
        if not _owned_ruleset_matches(detail, result["proposal"], repo):
            result.update(
                ok=False,
                action="created_unverified",
                reason=(
                    "the additive ruleset was created but its exact active no-bypass state "
                    "could not be verified; manual inspection is required"
                ),
            )
            return result
        result.update(
            action="created",
            reason="created and verified one separate Shipshape-owned additive ruleset",
        )
        return result
    except review_gates.GateError as error:
        result.update(
            ok=False,
            action="created_unverified" if result["ruleset_id"] else "create_failed",
            reason=f"{error}; manual inspection is required before retrying",
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--repo", required=True)
        command_parser.add_argument("--branch", required=True)
        command_parser.add_argument("--workflow-style", required=True)
        command_parser.add_argument("--tiered-review", action="store_true")
        command_parser.add_argument("--codeql-enabled", action="store_true")
        command_parser.add_argument("--require-review", action="store_true")
    args = parser.parse_args(argv)
    arguments = {
        "workflow_style": args.workflow_style,
        "tiered_review": args.tiered_review,
        "codeql_enabled": args.codeql_enabled,
        "require_review": args.require_review,
    }
    if args.command == "plan":
        result = plan_protection(GitHubAdminClient(), args.repo, args.branch, **arguments)
    else:
        result = apply_protection(GitHubAdminClient(), args.repo, args.branch, **arguments)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
