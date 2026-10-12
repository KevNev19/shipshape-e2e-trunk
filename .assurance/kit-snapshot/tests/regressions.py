#!/usr/bin/env python3
"""Regression guard: one dated test per historically fixed bug, so none can
silently return. Every test's docstring opens with the date and symptom of
the bug it guards. Run directly: python3 tests/regressions.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TEMPLATES = ROOT / "templates"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))


def render_fixture(fixture: str, root: Path, overrides: list[str] | None = None) -> Path:
    """Render a detected fixture through the consumer installation path."""
    repo = root / fixture
    shutil.copytree(FIXTURES / fixture, repo)
    detect = subprocess.run(
        [sys.executable, str(SCRIPTS / "detect.py"), str(repo), "--no-network"],
        capture_output=True,
        text=True,
        check=True,
    )
    detect_file = root / f"{fixture}-detect.json"
    detect_file.write_text(detect.stdout)
    init_args = [
        sys.executable,
        str(SCRIPTS / "render.py"),
        "init-config",
        str(repo),
        "--detect",
        str(detect_file),
    ]
    for override in overrides or []:
        init_args.extend(["--set", override])
    subprocess.run(init_args, capture_output=True, text=True, check=True)
    subprocess.run(
        [sys.executable, str(SCRIPTS / "render.py"), "apply", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    return repo


def run_focused_pytest(*nodeids: str, environment: dict[str, str] | None = None) -> None:
    """Run existing behavioral tests as dated regression evidence."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *nodeids],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_foreign_files_cannot_claim_test_command() -> None:
    """2026-08-10: dogfooding on shipshape itself guessed 'go test ./...' as
    the test command for a pure-Python project. A single main.go deep in
    tests/fixtures/ produced a low-confidence 'go' language, the implicit
    'go test' runner was added regardless of confidence, and the guess picked
    runners alphabetically ('go test' < 'pytest'). Guard: implicit runners
    require high confidence and the guess follows language-confidence order."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['tests']\n")
        (root / "tests").mkdir()
        nested = root / "examples" / "toy-go"
        nested.mkdir(parents=True)
        (nested / "main.go").write_text("package main\n")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "detect.py"), str(root), "--no-network"],
            capture_output=True,
            text=True,
            check=True,
        )
        result = json.loads(proc.stdout)
        assert result["test_command_guess"] == "pytest", result["test_command_guess"]


def test_lookalike_remote_host_is_not_github() -> None:
    """2026-08-10: CodeQL flagged detect_git's `"github.com" in url` substring
    check (high severity, incomplete URL sanitization) — a remote like
    https://evilgithub.com.attacker.example/o/r would have been classified as
    GitHub and its path offered as owner_repo. Guard: remotes are parsed into
    an exact host, and only github.com (or *.github.com) counts."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["git", "init", "-qb", "main", str(root)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "remote",
                "add",
                "origin",
                "https://evilgithub.com.attacker.example/owner/repo.git",
            ],
            check=True,
        )
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "detect.py"), str(root), "--no-network"],
            capture_output=True,
            text=True,
            check=True,
        )
        git_info = json.loads(proc.stdout)["git"]
        assert git_info["remote"] == "other", git_info
        assert git_info["owner_repo"] == "", git_info


def test_ci_doctor_uses_secret_scan_workflow() -> None:
    """2026-08-14: the weekly rendered doctor checked a per-clone pre-commit
    hook that cannot exist in GitHub Actions, so healthy repositories received
    false-positive health issues. Guard: CI checks the secret-scan workflow
    that protects pushed changes instead of the runner clone's local hook."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        doctor = root / ".sdlc" / "scripts" / "doctor.py"
        doctor.parent.mkdir(parents=True)
        template = TEMPLATES / "scripts" / "doctor.py.tmpl"
        doctor.write_text(template.read_text().replace("{{KIT_VERSION}}", "test"))
        review_gates = doctor.with_name("review-gates.py")
        review_gates.write_text(
            (TEMPLATES / "scripts" / "review-gates.py.tmpl")
            .read_text()
            .replace("{{KIT_VERSION}}", "test")
        )
        config = {
            "workflow_style": "trunk",
            "default_branch": "main",
            "languages": ["python"],
            "repo": {"owner_repo": ""},
            "features": {
                "branch_protection": False,
                "codeql": False,
                "github_agents": False,
                "scheduled_health": False,
                "secret_guard": True,
                "tiered_review": False,
            },
            "commands": {"test": ""},
        }
        (root / ".sdlc" / "config.json").write_text(json.dumps(config))
        guard = root / ".sdlc" / "hooks" / "secret-guard.sh"
        guard.parent.mkdir()
        guard.write_text("# layer 3: agent control files\n")
        workflow = root / ".github" / "workflows" / "secret-scan.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("permissions:\n  contents: read\n")
        env = os.environ.copy()
        env["GITHUB_ACTIONS"] = "true"

        proc = subprocess.run(
            [sys.executable, str(doctor), str(root)],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        checks = {
            item["name"]: item
            for section in json.loads(proc.stdout)["sections"]
            for item in section["checks"]
        }
        installed = checks["secret guard installed"]
        assert installed["status"] == "WARN", installed
        assert installed["detail"] == (
            "secret-scan workflow is configured; local hook was not verified in GitHub Actions"
        ), installed


def test_python_ci_installs_pytest_when_project_does_not() -> None:
    """2026-08-24: a fresh Python repo's first CI run failed because the
    rendered workflow invoked pytest without ensuring the runner was installed."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = render_fixture("repo-python", Path(tmp))
        workflow = (repo / ".github" / "workflows" / "ci.yml").read_text()
        assert 'python -c "import pytest" >/dev/null 2>&1 || pip install pytest' in workflow


def test_low_risk_automerge_has_merge_permission() -> None:
    """2026-08-24: eligible pull requests could not enable auto-merge because
    the rendered PR-style workflow had read-only contents permission."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = render_fixture(
            "repo-python",
            Path(tmp),
            overrides=["workflow_style=pr", "features.tiered_review=true"],
        )
        workflow = (repo / ".github" / "workflows" / "low-risk-automerge.yml").read_text()
        assert "  contents: write\n" in workflow
        assert "  pull-requests: write\n" in workflow
        review_helper = repo / ".sdlc" / "scripts" / "review-gates.py"
        assert review_helper.is_file()
        assert review_helper.stat().st_mode & 0o111 == 0
        release_helper = repo / ".sdlc" / "scripts" / "release-preflight.py"
        assert release_helper.is_file()
        assert release_helper.stat().st_mode & 0o111


def test_verification_window_uses_the_exact_clock_boundary() -> None:
    """2026-09-10: verification-cost aggregation mixed a date boundary with
    an exact timestamp boundary, so a run just outside the window could count."""
    from scripts import verification_cost

    original_fetch = verification_cost.fetch_json
    original_token = os.environ.get("GITHUB_TOKEN")
    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    search_queries = []

    def fake_fetch(path: str):
        if path.startswith("repos/"):
            return {
                "workflow_runs": [
                    {
                        "run_attempt": 1,
                        "run_started_at": "2026-08-10T12:00:00Z",
                        "updated_at": "2026-08-10T12:01:00Z",
                    },
                    {
                        "run_attempt": 1,
                        "run_started_at": "2026-08-10T11:59:59.999999Z",
                        "updated_at": "2026-08-10T12:01:00Z",
                    },
                ]
            }
        search_queries.append(urllib.parse.unquote(path.split("q=", 1)[1].split("&", 1)[0]))
        return {"total_count": 1}

    try:
        os.environ["GITHUB_TOKEN"] = "regression-token"
        verification_cost.fetch_json = fake_fetch
        result = verification_cost.summarize("example/project", 30, now=now)
    finally:
        verification_cost.fetch_json = original_fetch
        if original_token is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = original_token

    assert result["runs"] == 1, result
    assert result["minutes"] == 1, result
    assert search_queries == [
        "repo:example/project is:pr is:merged merged:>=2026-08-10T12:00:00+00:00"
    ], search_queries


def test_automatic_merge_rejects_nested_and_renamed_controls() -> None:
    """2026-09-09: nested controls and a rename away from a control path were
    classified from only the destination basename and became auto-mergeable;
    plural ADR and decision directories were also treated as ordinary docs."""
    from scripts import review_gates

    def blob(path: str) -> dict:
        return {"path": path, "mode": "100644", "type": "blob", "sha": "f" * 40}

    nested = "docs/guides/AGENTS.md"
    classification, _ = review_gates.classify_documentation(
        [{"filename": nested, "status": "modified"}],
        {nested: blob(nested)},
        {nested: blob(nested)},
    )
    assert classification == "none"

    old = ".github/agents/reviewer.md"
    new = "docs/guides/reviewer.md"
    classification, _ = review_gates.classify_documentation(
        [{"filename": new, "previous_filename": old, "status": "renamed"}],
        {old: blob(old)},
        {new: blob(new)},
    )
    assert classification == "none"

    for path in (
        "docs/adrs/0010-architecture.md",
        "docs/decisions/0010-platform.md",
        "docs/architecture-decisions/0010-platform.md",
    ):
        classification, _ = review_gates.classify_documentation(
            [{"filename": path, "status": "modified"}],
            {path: blob(path)},
            {path: blob(path)},
        )
        assert classification == "none", path

    guide = "docs/guides/usage.md"
    classification, _ = review_gates.classify_documentation(
        [{"filename": guide, "status": "modified"}],
        {guide: blob(guide)},
        {guide: blob(guide)},
    )
    assert classification == "docs-only"


def test_weekly_report_projects_alerts_and_paginates_pulls() -> None:
    """2026-09-09: weekly collection retained unbounded API alert fields and
    treated a full first page of open pull requests as a complete result."""
    from scripts import security_report

    class API:
        def __init__(self):
            self.calls = []
            self.unretained_fixture_value = "fixture-" + "private-material"
            self.responses = {
                "repos/example/project": {
                    "full_name": "example/project",
                    "default_branch": "main",
                },
                "repos/example/project/code-scanning/alerts?state=open&per_page=100&page=1": [],
                "repos/example/project/dependabot/alerts?state=open&per_page=100&page=1": [],
                "repos/example/project/secret-scanning/alerts?state=open&per_page=100&page=1": [
                    {
                        "secret_type_display_name": "Synthetic token",
                        "html_url": "https://example.invalid/secret/1",
                        "secret": self.unretained_fixture_value,
                        "future_api_field": "unretained",
                    }
                ],
                "repos/example/project/actions/runs?branch=main&per_page=20": {
                    "workflow_runs": [],
                    "total_count": 0,
                },
                "repos/example/project/pulls?state=open&per_page=50&page=1": [
                    {
                        "number": number,
                        "title": f"Pull {number}",
                        "created_at": "2026-09-09T08:00:00Z",
                        "draft": False,
                    }
                    for number in range(1, 51)
                ],
                "repos/example/project/pulls?state=open&per_page=50&page=2": [],
            }

        def get(self, path: str):
            self.calls.append(path)
            return self.responses[path]

    with tempfile.TemporaryDirectory() as tmp:
        api = API()
        report = security_report.collect_report(
            api,
            "example/project",
            Path(tmp),
        )

    sections = report["raw_collection"]["sections"]
    assert set(sections["secret_scanning"]["items"][0]) == {
        "secret_type_display_name",
        "html_url",
    }
    assert sections["pull_requests"]["count"] == 50
    assert "repos/example/project/pulls?state=open&per_page=50&page=2" in api.calls
    assert api.unretained_fixture_value not in json.dumps(report)


def test_automatic_authority_uses_exact_gates_and_refuses_persistent_state() -> None:
    """2026-09-09: check names without exact sources and prior persistent
    auto-merge state could be treated as current automatic authority."""
    from scripts import review_gates

    gates = review_gates.required_gates(True)
    assert [(gate.context, gate.app_id) for gate in gates] == [
        ("test", review_gates.GITHUB_ACTIONS_APP_ID),
        ("secret-scan", review_gates.GITHUB_ACTIONS_APP_ID),
        ("CodeQL", review_gates.CODEQL_APP_ID),
    ]

    class PersistentAPI:
        def get(self, path: str):
            if path == "repos/example/project":
                return {
                    "id": 1,
                    "name": "project",
                    "full_name": "example/project",
                    "url": "https://api.github.com/repos/example/project",
                    "default_branch": "main",
                }
            return {
                "id": 2,
                "number": 7,
                "url": "https://api.github.com/repos/example/project/pulls/7",
                "state": "open",
                "draft": False,
                "changed_files": 1,
                "merge_commit_sha": "c" * 40,
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {
                    "sha": "a" * 40,
                    "ref": "docs",
                    "repo": {
                        "id": 3,
                        "name": "project",
                        "full_name": "contributor/project",
                        "url": "https://api.github.com/repos/contributor/project",
                    },
                },
                "base": {
                    "sha": "b" * 40,
                    "ref": "main",
                    "repo": {
                        "id": 1,
                        "name": "project",
                        "full_name": "example/project",
                        "url": "https://api.github.com/repos/example/project",
                    },
                },
            }

        def graphql(self, _query, _variables):
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "id": "PR_7",
                            "autoMergeRequest": {"enabledBy": {"login": "maintainer"}},
                        }
                    }
                }
            }

        def put(self, _path, _payload):
            raise AssertionError("persistent authorization must refuse before mutation")

    result = review_gates.evaluate(PersistentAPI(), "example/project", 7)
    assert result["merged"] is False, result
    assert result["persistent_auto_merge_present"] is True, result
    assert "cancel auto-merge manually" in result["reason"].lower(), result


def test_secret_guard_scans_history_and_propagates_git_errors() -> None:
    """2026-09-09: history scans could miss add-then-remove secrets, text-diff
    presentation could hide content, and Git inspection errors could pass."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = render_fixture("repo-python", Path(tmp))
        subprocess.run(["git", "init", "-qb", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Regression"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "regression@example.invalid"], cwd=repo, check=True
        )
        (repo / "base.txt").write_text("clean\n")
        subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "base"],
            cwd=repo,
            check=True,
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        synthetic_key = "AKIA" + "A" * 16
        (repo / "note.txt").write_text(f"++{synthetic_key}\n")
        subprocess.run(["git", "add", "note.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "add then remove"],
            cwd=repo,
            check=True,
        )
        (repo / "note.txt").unlink()
        subprocess.run(["git", "add", "-u"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "remove"],
            cwd=repo,
            check=True,
        )
        guard = ["bash", ".sdlc/hooks/secret-guard.sh", "--range"]
        history = subprocess.run(
            [*guard, f"{base}..HEAD"], cwd=repo, capture_output=True, text=True
        )
        invalid = subprocess.run(
            [*guard, "missing-revision..HEAD"], cwd=repo, capture_output=True, text=True
        )
        assert history.returncode == 1, history.stderr
        assert "API key" in history.stderr and synthetic_key not in history.stderr
        assert invalid.returncode != 0 and "could not inspect" in invalid.stderr


def test_hook_installation_preserves_user_hook_and_pins_commit_stage() -> None:
    """2026-09-09: installation could overwrite custom hooks, while a global
    manual-only pre-commit default silently kept the guard out of commits."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = render_fixture("repo-python", Path(tmp))
        subprocess.run(["git", "init", "-qb", "main"], cwd=repo, check=True)
        hook = repo / ".git" / "hooks" / "pre-commit"
        original = b"#!/bin/sh\nprintf user-hook\n"
        hook.write_bytes(original)
        hook.chmod(0o755)
        result = subprocess.run(
            ["bash", ".sdlc/hooks/install.sh"], cwd=repo, capture_output=True, text=True
        )
        assert result.returncode != 0, result.stdout
        assert hook.read_bytes() == original
        pre_commit = (repo / ".pre-commit-config.yaml").read_text()
        assert "stages: [pre-commit]" in pre_commit


def test_node_package_manager_selection_is_explicit() -> None:
    """2026-09-09: Node setup defaulted to npm despite lockfiles or unsafe,
    conflicting package-manager declarations."""
    from scripts import detect

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "package.json").write_text('{"packageManager":"pnpm@10.15.1"}\n')
        (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        selected = detect.detect_node_package_manager(repo)
        assert selected["name"] == "pnpm", selected
        assert selected["variant"] == "pnpm", selected
        (repo / "package-lock.json").write_text("{}\n")
        conflict = detect.detect_node_package_manager(repo)
        assert "conflicting Node package-manager signals" in conflict["error"], conflict


def test_setup_refuses_symlinks_and_records_partial_completion() -> None:
    """2026-09-09: setup could follow repository symlinks, overwrite config,
    or claim the newest kit version after a partial apply."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        escaped = root / "outside.json"
        escaped.write_text('{"outside":true}\n')
        unsafe = root / "unsafe"
        (unsafe / ".sdlc").mkdir(parents=True)
        (unsafe / ".sdlc" / "config.json").symlink_to(escaped)
        refused = subprocess.run(
            [sys.executable, str(SCRIPTS / "render.py"), "plan", str(unsafe)],
            capture_output=True,
            text=True,
        )
        assert refused.returncode == 1 and "symlink" in refused.stdout
        assert escaped.read_text() == '{"outside":true}\n'

        repo = render_fixture("repo-python", root)
        claude = repo / "CLAUDE.md"
        claude.write_text(claude.read_text() + "\nUser edit.\n")
        (repo / "AGENTS.md").unlink()
        state_path = repo / ".sdlc" / "state.json"
        state = json.loads(state_path.read_text())
        state["kit_version"] = state["completed_kit_version"] = "0.1.0"
        state_path.write_text(json.dumps(state))
        applied = subprocess.run(
            [sys.executable, str(SCRIPTS / "render.py"), "apply", str(repo)],
            capture_output=True,
            text=True,
            check=True,
        )
        result = json.loads(applied.stdout)
        final_state = json.loads(state_path.read_text())
        assert result["status"] == "partial", result
        assert final_state["completed_kit_version"] == "0.1.0", final_state
        assert final_state["pending_conflicts"] == ["CLAUDE.md"], final_state


def test_release_publisher_uses_exact_context_from_a_bare_directory() -> None:
    """2026-09-09: the release publisher relied on checkout Git context and
    could publish from an implicit repository or a non-exact candidate."""
    workflow = (TEMPLATES / "github" / "workflows" / "release.yml.tmpl").read_text()
    section = workflow.split("      - name: Verify the exact tag and publish\n", 1)[1]
    block = section.split("        run: |\n", 1)[1]
    script_lines = []
    for line in block.splitlines():
        if line and not line.startswith("          "):
            break
        script_lines.append(line[10:] if line.startswith("          ") else "")
    script = textwrap.dedent("\n".join(script_lines))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runner = root / "runner"
        runner.mkdir()
        helper = runner / "release-preflight.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "expected=['publish','--repo',os.environ['GITHUB_REPOSITORY'],'--tag',"
            "os.environ['GITHUB_REF_NAME'],'--candidate',os.environ['GITHUB_SHA']]\n"
            "if sys.argv[1:] != expected:\n"
            "    raise SystemExit(2)\n"
            "print(json.dumps({'ok': True, 'candidate_sha': os.environ['GITHUB_SHA']}))\n"
        )
        bindir = root / "bin"
        bindir.mkdir()
        calls = root / "calls.json"
        fake_gh = bindir / "gh"
        fake_gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args=sys.argv[1:]\n"
            "json.dump(args, open(os.environ['CALLS'], 'w'))\n"
            "raise SystemExit(0 if '--repo' in args and '--target' in args else 2)\n"
        )
        fake_gh.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "CALLS": str(calls),
                "GH_TOKEN": "regression-token",
                "GITHUB_REF_NAME": "v1.2.3",
                "GITHUB_REPOSITORY": "example/project",
                "GITHUB_SHA": "a" * 40,
                "PATH": f"{bindir}{os.pathsep}{environment['PATH']}",
                "RUNNER_TEMP": str(runner),
            }
        )
        result = subprocess.run(
            ["bash", "-c", script], cwd=root, capture_output=True, text=True, env=environment
        )
        assert result.returncode == 0, result.stderr
        call = json.loads(calls.read_text())
        assert call[call.index("--repo") + 1] == "example/project", call
        assert call[call.index("--target") + 1] == "a" * 40, call


def test_reviewed_plan_binds_candidate_and_rendered_outputs() -> None:
    """2026-09-10: apply could accept changed candidate bytes or changed
    rendered output after a human reviewed an earlier plan."""
    run_focused_pytest(
        "tests/test_setup_safety.py::test_reviewed_candidate_change_is_rejected_before_repository_mutation",
        "tests/test_setup_safety.py::test_reviewed_candidate_byte_change_is_rejected_even_when_json_is_equal",
        "tests/test_codex_plugin.py::test_reviewed_plan_rejects_a_changed_rendered_template_before_writes",
    )


def test_managed_drift_uses_raw_bytes_and_recorded_modes() -> None:
    """2026-09-10: CRLF-only and chmod-only edits could look unchanged, and
    legacy records could adopt a mode without evidence."""
    run_focused_pytest(
        "tests/test_setup_safety.py::test_crlf_only_drift_conflicts_before_regeneration",
        "tests/test_setup_safety.py::test_mode_only_drift_conflicts_before_removal",
        "tests/test_setup_safety.py::test_legacy_state_does_not_adopt_an_unknown_mode",
        "tests/test_setup_safety.py::test_legacy_state_migrates_known_generated_modes",
    )


def test_copied_bundle_upgrade_preserves_completion_truth() -> None:
    """2026-09-10: a newer copied bundle could leave config and generated
    files stale while recording the new version as completely installed."""
    run_focused_pytest(
        "tests/test_codex_plugin.py::test_copied_bundle_upgrade_updates_config_outputs_and_completed_state",
        "tests/test_codex_plugin.py::test_copied_bundle_upgrade_keeps_completion_at_old_version_on_conflict",
    )


def test_fresh_node_setup_requires_reproducible_inputs() -> None:
    """2026-09-10: fresh Node setup accepted missing lockfiles and missing or
    contradictory package-manager declarations."""
    run_focused_pytest(
        "tests/test_package_managers.py::test_package_json_without_a_lockfile_is_rejected_for_fresh_setup",
        "tests/test_package_managers.py::test_non_npm_lock_requires_an_exact_package_manager_declaration",
        "tests/test_package_managers.py::test_contradictory_manager_signals_fail_instead_of_defaulting_to_npm",
    )


def test_node_25_bootstrap_is_pinned_and_ordered() -> None:
    """2026-09-10: accepted Node 25 workflows invoked absent bundled
    Corepack instead of installing a pinned compatible manager first."""
    run_focused_pytest(
        "tests/test_package_managers.py::test_node_25_installs_pinned_corepack_before_enabling_manager",
        "tests/test_package_managers.py::test_node_25_declared_npm_installs_the_exact_manager_without_corepack",
    )


def test_managed_helpers_install_remove_and_preserve_drift() -> None:
    """2026-09-10: release-preflight and review-gates generated copies were
    absent from installation or removal expectations and drift guarantees."""
    run_focused_pytest(
        "tests/test_render.py::test_full_apply",
        "tests/test_render.py::test_tiered_review_requires_pr_style_and_feature_flag",
        "tests/test_render.py::test_disabling_tiered_review_removes_the_managed_workflow",
        "tests/test_render.py::test_disabling_tiered_review_never_deletes_a_drifted_workflow",
    )


def test_setup_callers_apply_only_the_saved_preview() -> None:
    """2026-09-10: init and customize could apply a regenerated proposal
    without binding it to the JSON preview the person approved."""
    run_focused_pytest(
        "tests/test_doctor.py::test_setup_skills_bind_apply_to_the_saved_preview",
        "tests/test_setup_safety.py::test_proposed_config_and_plan_do_not_modify_repository",
        "tests/test_setup_safety.py::test_candidate_apply_requires_the_reviewed_plan_digest",
    )


def test_doctor_health_uses_byte_mode_and_version_evidence() -> None:
    """2026-09-10: both doctors could call CRLF or mode drift unmodified and
    pass installed-version health while config still named another version."""
    run_focused_pytest(
        "tests/test_doctor.py::test_managed_file_inspection_detects_byte_and_mode_only_edits",
        "tests/test_doctor.py::test_managed_file_inspection_warns_when_legacy_modes_are_unknown",
        "tests/test_doctor.py::test_managed_file_inspection_does_not_follow_symlinks",
        "tests/test_doctor.py::test_kit_version_requires_config_and_completed_state_to_match",
        "tests/test_rendered_doctor.py::test_rendered_doctor_detects_byte_and_mode_only_edits",
        "tests/test_rendered_doctor.py::test_rendered_doctor_exposes_unknown_legacy_modes",
    )


def test_release_latest_current_attempt_uses_run_started_at() -> None:
    """2026-09-10: release evidence ordered by run number and ignored a
    chronologically later failed attempt of an older workflow run."""
    run_focused_pytest(
        "tests/test_release_preflight.py::test_chronologically_later_older_run_rerun_takes_precedence"
    )


def test_release_readiness_requires_registered_active_publisher() -> None:
    """2026-09-10: local release readiness could pass without the exact active
    release workflow and publication helper on the candidate commit."""
    run_focused_pytest(
        "tests/test_release_preflight.py::test_local_preflight_requires_exact_active_release_workflow",
        "tests/test_release_preflight.py::test_local_preflight_requires_exact_publication_helper",
    )


def test_release_requires_successful_codeql_findings() -> None:
    """2026-09-10: release readiness accepted a successful CodeQL Actions
    run while its separate exact-candidate CodeQL findings check was failing."""
    run_focused_pytest(
        "tests/test_release_preflight.py::test_successful_codeql_analysis_does_not_override_failed_findings_check",
        "tests/test_release_preflight.py::test_older_green_codeql_findings_do_not_satisfy_a_newer_analysis_attempt",
        "tests/test_release_preflight.py::test_publish_and_postflight_require_successful_codeql_findings",
    )


def test_review03_automation_boundaries() -> None:
    """2026-09-10: broad documentation paths, partial GraphQL errors, and
    run-number ordering could incorrectly authorize automatic merging."""
    run_focused_pytest(
        "tests/test_render.py::test_tiered_review_docs_class_rejects_executable_paths",
        "tests/test_review_gates.py::test_rename_checks_both_sides_of_fixed_document_location_allowlist",
        "tests/test_review_gates.py::test_partial_graphql_errors_never_prove_persistent_auto_merge_absent",
        "tests/test_review_gates.py::test_malformed_graphql_errors_state_remains_unknown",
        "tests/test_review_gates.py::test_chronologically_later_lower_id_rerun_overrides_older_green",
        "tests/test_review_gates.py::test_malformed_workflow_run_chronology_never_merges",
    )


def test_review03_configured_protection_modes() -> None:
    """2026-09-10: health inspection could omit ordinary branch protection,
    infer CodeQL from presence, or accept malformed feature metadata."""
    run_focused_pytest(
        "tests/test_protection.py::test_config_derives_exact_ordinary_pr_protection_request",
        "tests/test_protection.py::test_config_derives_exact_trunk_safeguard_request_without_status_checks",
        "tests/test_protection.py::test_config_derives_exact_tiered_pr_request",
        "tests/test_protection.py::test_config_derivation_rejects_nonboolean_feature_flags",
        "tests/test_protection.py::test_configured_protection_reports_missing_codeql_as_unknown_without_api_reads",
        "tests/test_protection.py::test_non_tiered_requested_protection_is_inspected_and_missing_state_refuses",
        "tests/test_doctor.py::test_non_tiered_pr_protection_is_verified_read_only",
        "tests/test_doctor.py::test_trunk_protection_is_verified_without_inventing_status_checks",
        "tests/test_doctor.py::test_configured_applicable_codeql_missing_is_not_reported_as_protected",
        "tests/test_rendered_doctor.py::test_rendered_doctor_verifies_non_tiered_pr_protection_read_only",
        "tests/test_rendered_doctor.py::test_rendered_doctor_verifies_trunk_safeguards_without_status_checks",
        "tests/test_rendered_doctor.py::test_rendered_doctor_rejects_malformed_protection_metadata",
    )


def test_review03_guard_report_access_boundaries() -> None:
    """2026-09-10: structured control files could evade the guard, and
    truncated Actions history or obsolete access syntax could overclaim proof."""
    run_focused_pytest(
        "tests/test_secret_guard.py::test_complete_executable_json_structure_is_blocked",
        "tests/test_secret_guard.py::test_value_only_change_under_split_command_key_is_blocked",
        "tests/test_secret_guard.py::test_guard_reads_exact_staged_control_blob",
        "tests/test_secret_guard.py::test_malformed_supported_json_fails_closed_without_leaking_content",
        "tests/test_secret_guard.py::test_range_mode_scans_each_control_blob_before_later_removal",
        "tests/test_security_report.py::test_actions_window_reports_available_history_truncation",
        "tests/test_security_report.py::test_actions_window_rejects_invalid_total_count",
    )
    access_skill = ROOT / "skills" / "shipshape-access" / "SKILL.md"
    assert "protect <branch> <trunk|pr>" in access_skill.read_text()


def test_doctor_tests_isolate_ambient_github_actions_mode() -> None:
    """2026-09-10: hosted pytest inherited GITHUB_ACTIONS=true, so local-hook
    tests accidentally exercised the doctor's explicit CI evidence mode."""
    environment = os.environ.copy()
    environment["GITHUB_ACTIONS"] = "true"
    run_focused_pytest(
        "tests/test_doctor.py::test_uninstalled_guard_is_fail_with_fix",
        "tests/test_doctor.py::test_github_actions_reports_configuration_without_claiming_local_hook",
        "tests/test_rendered_doctor.py::test_rendered_doctor_still_requires_local_hook_outside_ci",
        "tests/test_rendered_doctor.py::test_rendered_doctor_accepts_secret_scan_in_ci_without_local_hook",
        environment=environment,
    )


def test_weekly_report_rejects_untrusted_repository_metadata() -> None:
    """2026-09-10: a path-insensitive synthetic API fixture made a raw alert
    field appear able to become repository metadata and reach JSON stdout."""
    run_focused_pytest(
        "tests/test_security_report.py::test_alert_records_are_projected_through_section_allowlists",
        "tests/test_security_report.py::test_malformed_repository_metadata_is_not_echoed",
    )


def main() -> int:
    checks = [
        (
            "nested foreign files cannot claim test command",
            test_nested_foreign_files_cannot_claim_test_command,
        ),
        (
            "lookalike remote host is not github",
            test_lookalike_remote_host_is_not_github,
        ),
        (
            "CI doctor uses secret-scan workflow",
            test_ci_doctor_uses_secret_scan_workflow,
        ),
        (
            "Python CI installs pytest when project does not",
            test_python_ci_installs_pytest_when_project_does_not,
        ),
        (
            "low-risk auto-merge has merge permission",
            test_low_risk_automerge_has_merge_permission,
        ),
        (
            "verification window uses exact clock boundary",
            test_verification_window_uses_the_exact_clock_boundary,
        ),
        (
            "automatic merge rejects nested and renamed controls",
            test_automatic_merge_rejects_nested_and_renamed_controls,
        ),
        (
            "weekly report projects alerts and paginates pulls",
            test_weekly_report_projects_alerts_and_paginates_pulls,
        ),
        (
            "automatic authority uses exact gates and refuses persistent state",
            test_automatic_authority_uses_exact_gates_and_refuses_persistent_state,
        ),
        (
            "secret guard scans history and propagates git errors",
            test_secret_guard_scans_history_and_propagates_git_errors,
        ),
        (
            "hook installation preserves user hook and pins commit stage",
            test_hook_installation_preserves_user_hook_and_pins_commit_stage,
        ),
        (
            "node package manager selection is explicit",
            test_node_package_manager_selection_is_explicit,
        ),
        (
            "setup refuses symlinks and records partial completion",
            test_setup_refuses_symlinks_and_records_partial_completion,
        ),
        (
            "release publisher uses exact bare context",
            test_release_publisher_uses_exact_context_from_a_bare_directory,
        ),
        (
            "reviewed plan binds candidate and rendered outputs",
            test_reviewed_plan_binds_candidate_and_rendered_outputs,
        ),
        (
            "managed drift uses raw bytes and recorded modes",
            test_managed_drift_uses_raw_bytes_and_recorded_modes,
        ),
        (
            "copied bundle upgrade preserves completion truth",
            test_copied_bundle_upgrade_preserves_completion_truth,
        ),
        (
            "fresh Node setup requires reproducible inputs",
            test_fresh_node_setup_requires_reproducible_inputs,
        ),
        (
            "Node 25 bootstrap is pinned and ordered",
            test_node_25_bootstrap_is_pinned_and_ordered,
        ),
        (
            "managed helpers install, remove, and preserve drift",
            test_managed_helpers_install_remove_and_preserve_drift,
        ),
        (
            "setup callers apply only the saved preview",
            test_setup_callers_apply_only_the_saved_preview,
        ),
        (
            "doctor health uses byte, mode, and version evidence",
            test_doctor_health_uses_byte_mode_and_version_evidence,
        ),
        (
            "release latest attempt uses run start time",
            test_release_latest_current_attempt_uses_run_started_at,
        ),
        (
            "release readiness requires active publisher",
            test_release_readiness_requires_registered_active_publisher,
        ),
        (
            "release requires successful CodeQL findings",
            test_release_requires_successful_codeql_findings,
        ),
        (
            "review03 automation boundaries",
            test_review03_automation_boundaries,
        ),
        (
            "review03 configured protection modes",
            test_review03_configured_protection_modes,
        ),
        (
            "review03 guard/report/access boundaries",
            test_review03_guard_report_access_boundaries,
        ),
        (
            "doctor tests isolate ambient GitHub Actions mode",
            test_doctor_tests_isolate_ambient_github_actions_mode,
        ),
        (
            "weekly report rejects untrusted repository metadata",
            test_weekly_report_rejects_untrusted_repository_metadata,
        ),
    ]
    failures = 0
    for name, check in checks:
        try:
            check()
            print(f"[OK  ] {name}")
        except AssertionError as exc:
            print(f"[FAIL] {name}: {exc}")
            failures += 1
    print(f"{len(checks) - failures}/{len(checks)} regression checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
