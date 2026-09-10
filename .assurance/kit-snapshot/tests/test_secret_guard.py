"""Secret-guard tests: the rendered hook must block planted credentials and
credential files, and pass clean changes. The guard is invoked directly (as
the git hook would) against a real staged index."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import init_repo, run_script

# Amazon's canonical documentation example key — not a real credential.
# Built by concatenation so this test file itself never contains a string
# the guard (or GitHub push protection) would match.
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
SYNTHETIC_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
FOLLOWUP_AWS_KEY = "AKIA" + "A" * 16


def guarded_repo(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    code, _ = run_script("render.py", "apply", repo)
    assert code == 0
    subprocess.run(["git", "init", "-qb", "main"], cwd=repo, check=True)
    return repo


def run_guard(repo, *args, env=None):
    return subprocess.run(
        ["bash", ".sdlc/hooks/secret-guard.sh", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def stage(repo, relpath, content):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", "-f", str(relpath)], cwd=repo, check=True)


def stage_bytes(repo, relpath, content):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    subprocess.run(["git", "add", "-f", str(relpath)], cwd=repo, check=True)


def unstage_all(repo):
    subprocess.run(["git", "reset", "-q"], cwd=repo, check=True)


def test_clean_change_passes(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "src/feature.py", "def feature() -> str:\n    return 'ok'\n")
    result = run_guard(repo)
    assert result.returncode == 0, result.stderr
    assert "WARN" not in result.stderr


def test_control_file_change_warns_without_blocking(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, ".claude/preferences.json", '{"theme": "dark"}\n')
    result = run_guard(repo)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert ".claude/preferences.json" in result.stderr
    assert "automated tools behave" in result.stderr


@pytest.mark.parametrize(
    "control_path",
    [".github/copilot-instructions.md", "AGENTS.md", ".sdlc/hooks/custom.sh"],
)
def test_each_remaining_control_path_warns(tmp_path, control_path):
    repo = guarded_repo(tmp_path)
    stage(repo, control_path, "Review this control change.\n")
    result = run_guard(repo)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert control_path in result.stderr


@pytest.mark.parametrize(
    "control_path",
    [
        "packages/api/AGENTS.md",
        "services/worker/CLAUDE.md",
        "skills/example/SKILL.md",
        ".agents/settings.json",
        "packages/api/.codex/config.toml",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        "packages/api/docs/agents/policy.md",
        "packages/api/docs/sdlc/harness.md",
        "docs/harness.md",
        ".github/workflows/review.yml",
        ".idea/runConfigurations/tests.xml",
        ".zed/tasks.json",
    ],
)
def test_expanded_control_path_warns_without_blocking(tmp_path, control_path):
    repo = guarded_repo(tmp_path)
    content = (
        '{"theme": "dark"}\n'
        if control_path in {".agents/settings.json", ".zed/tasks.json"}
        else "Review this control change.\n"
    )
    stage(repo, control_path, content)

    result = run_guard(repo)

    assert result.returncode == 0, result.stderr
    assert "WARN: automation or policy control files changed" in result.stderr
    assert control_path in result.stderr
    assert "BLOCKED" not in result.stderr


def test_generic_markdown_prose_stays_quiet(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "docs/guides/usage.md", "Run `make test` before finishing.\n")

    result = run_guard(repo)

    assert result.returncode == 0, result.stderr
    assert "WARN" not in result.stderr
    assert "BLOCKED" not in result.stderr


def test_generated_setup_state_hashes_warn_without_blocking(tmp_path):
    repo = guarded_repo(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    result = run_guard(repo)

    assert result.returncode == 0, result.stderr
    assert "WARN: automation or policy control files changed" in result.stderr
    assert ".sdlc/state.json" in result.stderr
    assert "BLOCKED" not in result.stderr


@pytest.mark.parametrize(
    "control_path",
    [".sdlc/config.json", ".codex-plugin/plugin.json"],
)
def test_control_metadata_hashes_warn_without_blocking(tmp_path, control_path):
    repo = guarded_repo(tmp_path)
    synthetic_hash = "a" * 64
    stage(repo, control_path, f'{{"sha256": "{synthetic_hash}"}}\n')

    result = run_guard(repo)

    assert result.returncode == 0, result.stderr
    assert "WARN: automation or policy control files changed" in result.stderr
    assert control_path in result.stderr
    assert "BLOCKED" not in result.stderr
    assert synthetic_hash not in result.stderr


def test_native_control_json_command_is_blocked_without_leaking_value(tmp_path):
    repo = guarded_repo(tmp_path)
    synthetic_command = "printf scanner-output-must-not-echo-content"
    stage(repo, ".agents/settings.json", f'{{"command": "{synthetic_command}"}}\n')

    result = run_guard(repo)

    assert result.returncode == 1
    assert "executable agent or editor settings" in result.stderr
    assert ".agents/settings.json" in result.stderr
    assert synthetic_command not in result.stderr


@pytest.mark.parametrize(
    ("source", "destination", "reported_path"),
    [
        ("notes/policy.md", "packages/api/AGENTS.md", "packages/api/AGENTS.md"),
        ("packages/api/AGENTS.md", "notes/policy.md", "packages/api/AGENTS.md"),
    ],
)
def test_nested_control_rename_warns_for_control_name(tmp_path, source, destination, reported_path):
    repo = guarded_repo(tmp_path)
    stage(repo, source, "Review this policy.\n")
    _commit(repo, "base")
    (repo / destination).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "mv", source, destination], cwd=repo, check=True)

    result = run_guard(repo)

    assert result.returncode == 0, result.stderr
    assert "WARN: automation or policy control files changed" in result.stderr
    assert reported_path in result.stderr


def test_nested_control_mode_change_warns(tmp_path):
    repo = guarded_repo(tmp_path)
    control_path = "skills/example/SKILL.md"
    stage(repo, control_path, "# Example skill\n")
    _commit(repo, "base")
    (repo / control_path).chmod(0o755)
    subprocess.run(["git", "add", control_path], cwd=repo, check=True)

    result = run_guard(repo)

    assert result.returncode == 0, result.stderr
    assert "WARN: automation or policy control files changed" in result.stderr
    assert control_path in result.stderr


def test_vscode_task_command_is_blocked(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(
        repo,
        ".vscode/tasks.json",
        '{"version": "2.0.0", "tasks": [{"command": "make release"}]}\n',
    )
    result = run_guard(repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert ".vscode/tasks.json" in result.stderr
    assert "git commit --no-verify" in result.stderr


@pytest.mark.parametrize(
    ("control_path", "content"),
    [
        (
            ".vscode/tasks.json",
            "{\n"
            '  "version": "2.0.0",\n'
            '  "tasks": [{"type"\n'
            "    :\n"
            '    "shell", "command"\n'
            "    :\n"
            '    "printf synthetic"}]\n'
            "}\n",
        ),
        (
            ".vscode/tasks.jsonc",
            "{\n"
            "  // JSONC comments and trailing commas are supported.\n"
            '  "tasks": [{\n'
            '    "command": /* kept across lines */\n'
            '      "printf synthetic",\n'
            "  }],\n"
            "}\n",
        ),
    ],
)
def test_complete_executable_json_structure_is_blocked(tmp_path, control_path, content):
    repo = guarded_repo(tmp_path)
    stage(repo, control_path, content)

    result = run_guard(repo)

    assert result.returncode == 1
    assert "executable agent or editor settings" in result.stderr
    assert control_path in result.stderr
    assert "printf synthetic" not in result.stderr


def test_value_only_change_under_split_command_key_is_blocked(tmp_path):
    repo = guarded_repo(tmp_path)
    control_path = ".vscode/tasks.json"
    stage(repo, control_path, '{"tasks": [{"command":\n  "printf old"}]}\n')
    _commit(repo, "base task")
    stage(repo, control_path, '{"tasks": [{"command":\n  "printf new"}]}\n')

    result = run_guard(repo)

    assert result.returncode == 1
    assert "executable agent or editor settings" in result.stderr
    assert "printf old" not in result.stderr
    assert "printf new" not in result.stderr


def test_unchanged_executable_setting_allows_benign_sibling_change(tmp_path):
    repo = guarded_repo(tmp_path)
    control_path = ".vscode/tasks.json"
    stage(
        repo,
        control_path,
        '{"version": "1", "tasks": [{"command": "printf unchanged"}]}\n',
    )
    _commit(repo, "base task")
    stage(
        repo,
        control_path,
        '{"version": "2", "tasks": [{"command": "printf unchanged"}]}\n',
    )

    result = run_guard(repo)

    assert result.returncode == 0, result.stderr
    assert "WARN: automation or policy control files changed" in result.stderr
    assert "BLOCKED" not in result.stderr


def test_guard_reads_exact_staged_control_blob(tmp_path):
    repo = guarded_repo(tmp_path)
    control_path = ".agents/settings.json"
    stage(repo, control_path, '{"command": "printf staged"}\n')
    (repo / control_path).write_text('{"theme": "working-tree-only"}\n')

    result = run_guard(repo)

    assert result.returncode == 1
    assert "executable agent or editor settings" in result.stderr
    assert "printf staged" not in result.stderr
    assert "working-tree-only" not in result.stderr


@pytest.mark.parametrize(
    ("malformed", "private_marker"),
    [
        ('"command"\n"private fixture value"\n', "private fixture value"),
        ('{"theme": "dark", "theme": "private duplicate"}\n', "private duplicate"),
        ('{"theme": NaN, "note": "private nonfinite"}\n', "private nonfinite"),
    ],
)
def test_malformed_supported_json_fails_closed_without_leaking_content(
    tmp_path, malformed, private_marker
):
    repo = guarded_repo(tmp_path)
    stage(repo, ".agents/settings.json", malformed)

    result = run_guard(repo)

    assert result.returncode == 2
    assert "could not inspect" in result.stderr
    assert private_marker not in result.stderr


def test_non_json_control_syntax_is_warning_only(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, ".codex/config.toml", 'approval_policy = ["invalid toml"\n')

    result = run_guard(repo)

    assert result.returncode == 0, result.stderr
    assert "WARN: automation or policy control files changed" in result.stderr
    assert "could not inspect" not in result.stderr


def test_benign_terminal_preference_warns_without_blocking(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, ".vscode/settings.json", '{"terminal.integrated.fontSize": 14}\n')
    result = run_guard(repo)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert "BLOCKED" not in result.stderr


def test_terminal_shell_setting_is_blocked(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(
        repo,
        ".vscode/settings.json",
        '{"terminal.integrated.shell.osx": "/tmp/run-on-open"}\n',
    )
    result = run_guard(repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr


def test_terminal_profile_with_executable_path_is_blocked(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(
        repo,
        ".vscode/settings.json",
        "{\n"
        '  "terminal.integrated.profiles.osx": {\n'
        '    "release": {"path": "/tmp/run-on-open"}\n'
        "  }\n"
        "}\n",
    )
    result = run_guard(repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr


def test_claude_settings_base64_blob_is_blocked(tmp_path):
    repo = guarded_repo(tmp_path)
    encoded = "A" * 44
    stage(repo, ".claude/settings.json", f'{{"payload": "{encoded}"}}\n')
    result = run_guard(repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "base64" in result.stderr


def test_markdown_control_file_command_warns_without_blocking(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "CLAUDE.md", "Run `make test` before finishing.\n")
    result = run_guard(repo)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert "CLAUDE.md" in result.stderr


def test_planted_api_key_is_blocked(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "src/config.py", f'ACCESS_KEY = "{FAKE_AWS_KEY}"\n')
    result = run_guard(repo)
    assert result.returncode == 1
    assert "BLOCKED" in result.stderr
    assert "API key" in result.stderr


def test_env_file_is_blocked_by_name(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, ".env", "DB_PASSWORD=hunter2\n")
    result = run_guard(repo)
    assert result.returncode == 1
    assert "credential or key file" in result.stderr


def test_private_key_header_is_blocked(tmp_path):
    repo = guarded_repo(tmp_path)
    # Concatenated so this file never contains a line the guard would match.
    header = "-----BEGIN RSA PRIVATE " + "KEY-----"
    stage(repo, "deploy/server.txt", header + "\nabc\n")
    result = run_guard(repo)
    assert result.returncode == 1


def test_denylist_blocks_custom_strings(tmp_path):
    repo = guarded_repo(tmp_path)
    (repo / ".sdlc/hooks/denylist.txt").write_text("Sensitive Family Name\n")
    stage(repo, "notes.md", "payment from Sensitive Family Name arrived\n")
    result = run_guard(repo)
    assert result.returncode == 1
    assert "denylist" in result.stderr


def test_nothing_staged_passes(tmp_path):
    repo = guarded_repo(tmp_path)
    result = run_guard(repo)
    assert result.returncode == 0


def _commit(repo, message):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            message,
        ],
        cwd=repo,
        check=True,
    )


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize("scan_mode", ["staged", "range"])
@pytest.mark.parametrize("content_case", ["plus_prefix", "attributes", "nul_bytes"])
def test_content_scan_cannot_be_hidden_by_diff_presentation(tmp_path, scan_mode, content_case):
    repo = guarded_repo(tmp_path)
    base = None
    if scan_mode == "range":
        stage(repo, "base.txt", "clean\n")
        _commit(repo, "base")
        base = _head(repo)

    if content_case == "plus_prefix":
        stage(repo, "note.txt", "++" + FOLLOWUP_AWS_KEY + "\n")
    elif content_case == "attributes":
        stage(repo, ".gitattributes", "note.txt -diff\n")
        stage(repo, "note.txt", FOLLOWUP_AWS_KEY + "\n")
    else:
        stage_bytes(repo, "note.txt", b"binary\0" + FOLLOWUP_AWS_KEY.encode() + b"\n")

    if scan_mode == "range":
        _commit(repo, f"add {content_case} credential")
        result = run_guard(repo, "--range", f"{base}..HEAD")
    else:
        result = run_guard(repo)

    assert result.returncode == 1
    assert "API key" in result.stderr
    assert FOLLOWUP_AWS_KEY not in result.stderr


@pytest.mark.parametrize("scan_mode", ["staged", "range"])
@pytest.mark.parametrize("helper_kind", ["command", "textconv"])
def test_content_scan_does_not_execute_diff_helpers(tmp_path, scan_mode, helper_kind):
    repo = guarded_repo(tmp_path)
    base = None
    if scan_mode == "range":
        stage(repo, "base.txt", "clean\n")
        _commit(repo, "base")
        base = _head(repo)

    marker = tmp_path / f"{helper_kind}-was-called"
    helper = tmp_path / f"{helper_kind}-helper"
    helper.write_text(f'#!/bin/sh\nprintf called > "{marker}"\nexit 97\n')
    helper.chmod(0o755)
    subprocess.run(
        ["git", "config", f"diff.shipshape.{helper_kind}", str(helper)],
        cwd=repo,
        check=True,
    )
    stage(repo, ".gitattributes", "note.txt diff=shipshape\n")
    stage(repo, "note.txt", FOLLOWUP_AWS_KEY + "\n")

    if scan_mode == "range":
        _commit(repo, f"add content with {helper_kind}")
        result = run_guard(repo, "--range", f"{base}..HEAD")
    else:
        result = run_guard(repo)

    assert result.returncode == 1
    assert not marker.exists()
    assert FOLLOWUP_AWS_KEY not in result.stderr


def _workflow_run_script(repo):
    workflow = (repo / ".github/workflows/secret-scan.yml").read_text()
    lines = workflow.splitlines()
    marker = next(index for index, line in enumerate(lines) if line.strip() == "run: |")
    marker_indent = len(lines[marker]) - len(lines[marker].lstrip())
    body = []
    for line in lines[marker + 1 :]:
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent <= marker_indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def test_range_mode_catches_committed_secret(tmp_path):
    """CI mode: a secret already committed (e.g. by a cloud agent that never
    ran the local hook) is caught by --range base..HEAD."""
    repo = guarded_repo(tmp_path)
    stage(repo, "README2.md", "clean base\n")
    _commit(repo, "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    stage(repo, "src/config.py", f'ACCESS_KEY = "{FAKE_AWS_KEY}"\n')
    _commit(repo, "leaky")

    result = subprocess.run(
        ["bash", ".sdlc/hooks/secret-guard.sh", "--range", f"{base}..HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "rotate" in result.stderr, "CI mode must give rotation advice"


def test_range_mode_catches_secret_added_then_removed_in_later_commit(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "base.txt", "clean\n")
    _commit(repo, "base")
    base = _head(repo)
    stage(repo, "src/transient.py", f'TOKEN = "{SYNTHETIC_AWS_KEY}"\n')
    _commit(repo, "add synthetic credential")
    (repo / "src/transient.py").unlink()
    subprocess.run(["git", "add", "-u"], cwd=repo, check=True)
    _commit(repo, "remove synthetic credential")

    result = run_guard(repo, "--range", f"{base}..HEAD")

    assert result.returncode == 1
    assert "API key" in result.stderr
    assert SYNTHETIC_AWS_KEY not in result.stderr


def test_range_mode_scans_each_control_blob_before_later_removal(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "base.txt", "clean\n")
    _commit(repo, "base")
    base = _head(repo)
    control_path = ".vscode/tasks.jsonc"
    stage(
        repo,
        control_path,
        '{"tasks": [{"command":\n  "printf transient",}],}\n',
    )
    _commit(repo, "add transient task")
    (repo / control_path).unlink()
    subprocess.run(["git", "add", "-u"], cwd=repo, check=True)
    _commit(repo, "remove transient task")

    result = run_guard(repo, "--range", f"{base}..HEAD")

    assert result.returncode == 1
    assert "executable agent or editor settings" in result.stderr
    assert "printf transient" not in result.stderr


def test_range_mode_blocks_rename_into_credential_path(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "notes/source.txt", "not secret material\n")
    _commit(repo, "base")
    base = _head(repo)
    destination = "credentials/private key.pem"
    (repo / "credentials").mkdir()
    subprocess.run(["git", "mv", "notes/source.txt", destination], cwd=repo, check=True)
    _commit(repo, "rename credential file")

    result = run_guard(repo, "--range", f"{base}..HEAD")

    assert result.returncode == 1
    assert destination in result.stderr
    assert "credential or key file" in result.stderr


def test_staged_control_file_with_newline_in_name_is_scanned_safely(tmp_path):
    repo = guarded_repo(tmp_path)
    control_path = ".claude/odd\nname.json"
    stage(repo, control_path, '{"command": "printf synthetic"}\n')

    result = run_guard(repo)

    assert result.returncode == 1
    assert "executable agent or editor settings" in result.stderr


def test_range_mode_rejects_invalid_revision(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "base.txt", "clean\n")
    _commit(repo, "base")

    result = run_guard(repo, "--range", "missing-revision..HEAD")

    assert result.returncode != 0
    assert "could not inspect" in result.stderr


def test_range_mode_propagates_git_diff_failure(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "base.txt", "clean\n")
    _commit(repo, "base")
    base = _head(repo)
    stage(repo, "next.txt", "still clean\n")
    _commit(repo, "next")
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "failing-git-bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        f'#!/bin/sh\nif [ "$1" = "diff" ]; then\n  exit 73\nfi\nexec "{real_git}" "$@"\n'
    )
    fake_git.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    result = run_guard(repo, "--range", f"{base}..HEAD", env=env)

    assert result.returncode != 0
    assert "could not inspect" in result.stderr


def test_staged_mode_propagates_grep_failure(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "note.txt", "clean\n")
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "failing-grep-bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(f'#!/bin/sh\nexec "{real_git}" "$@"\n')
    fake_git.chmod(0o755)
    fake_grep = bin_dir / "grep"
    fake_grep.write_text("#!/bin/sh\nexit 73\n")
    fake_grep.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    result = run_guard(repo, env=env)

    assert result.returncode != 0
    assert "could not inspect" in result.stderr


def test_ci_uses_base_commit_scanner_instead_of_proposed_replacement(tmp_path):
    repo = guarded_repo(tmp_path)
    scan_script = _workflow_run_script(repo)
    guard = repo / ".sdlc/hooks/secret-guard.sh"
    guard.write_text("#!/usr/bin/env bash\necho trusted-scanner >&2\nexit 23\n")
    subprocess.run(["git", "add", "-f", ".sdlc/hooks/secret-guard.sh"], cwd=repo, check=True)
    _commit(repo, "trusted base")
    base = _head(repo)
    guard.write_text("#!/usr/bin/env bash\necho proposed-scanner >&2\nexit 0\n")
    subprocess.run(["git", "add", "-f", ".sdlc/hooks/secret-guard.sh"], cwd=repo, check=True)
    _commit(repo, "replace scanner")
    env = os.environ.copy()
    env.update({"BASE_SHA": base, "HEAD_SHA": _head(repo), "RUNNER_TEMP": str(tmp_path)})

    result = subprocess.run(
        ["bash", "-c", scan_script], cwd=repo, capture_output=True, text=True, env=env
    )

    assert result.returncode == 23
    assert "trusted-scanner" in result.stderr
    assert "proposed-scanner" not in result.stderr


def test_ci_pr_target_keeps_read_only_non_persistent_credentials(tmp_path):
    repo = guarded_repo(tmp_path)
    workflow = (repo / ".github/workflows/secret-scan.yml").read_text()

    assert "  pull_request_target:\n" in workflow
    assert "  pull_request:\n" not in workflow
    assert "permissions:\n  contents: read\n" in workflow
    assert "persist-credentials: false" in workflow
    assert "contents: write" not in workflow
    assert "secrets." not in workflow


@pytest.mark.parametrize("base_sha", ["", "0" * 40, "missing-revision"])
def test_ci_fails_closed_without_a_trusted_base_scanner(tmp_path, base_sha):
    repo = guarded_repo(tmp_path)
    scan_script = _workflow_run_script(repo)
    stage(repo, "base.txt", "clean\n")
    _commit(repo, "base")
    env = os.environ.copy()
    env.update({"BASE_SHA": base_sha, "HEAD_SHA": _head(repo), "RUNNER_TEMP": str(tmp_path)})

    result = subprocess.run(
        ["bash", "-c", scan_script], cwd=repo, capture_output=True, text=True, env=env
    )

    assert result.returncode != 0
    assert "trusted" in result.stderr.casefold()


def test_ci_fails_closed_when_valid_base_has_no_scanner(tmp_path):
    repo = guarded_repo(tmp_path)
    scan_script = _workflow_run_script(repo)
    stage(repo, "base.txt", "clean\n")
    _commit(repo, "base without scanner")
    base = _head(repo)
    subprocess.run(["git", "add", "-f", ".sdlc/hooks/secret-guard.sh"], cwd=repo, check=True)
    _commit(repo, "proposed scanner")
    env = os.environ.copy()
    env.update({"BASE_SHA": base, "HEAD_SHA": _head(repo), "RUNNER_TEMP": str(tmp_path)})

    result = subprocess.run(
        ["bash", "-c", scan_script], cwd=repo, capture_output=True, text=True, env=env
    )

    assert result.returncode != 0
    assert "trusted base does not contain" in result.stderr


def test_range_mode_passes_clean_range(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "a.md", "one\n")
    _commit(repo, "one")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    stage(repo, "b.md", "two\n")
    _commit(repo, "two")
    result = subprocess.run(
        ["bash", ".sdlc/hooks/secret-guard.sh", "--range", f"{base}..HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_range_mode_warns_for_control_file_change(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "a.md", "one\n")
    _commit(repo, "one")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    stage(repo, ".github/agents/helper.md", "Review the whole diff.\n")
    _commit(repo, "control")

    result = subprocess.run(
        ["bash", ".sdlc/hooks/secret-guard.sh", "--range", f"{base}..HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert ".github/agents/helper.md" in result.stderr


def test_range_mode_warns_for_nested_native_control_change(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "base.txt", "one\n")
    _commit(repo, "one")
    base = _head(repo)
    control_path = "packages/api/.codex/config.toml"
    stage(repo, control_path, 'review_policy = "strict"\n')
    _commit(repo, "control")

    result = run_guard(repo, "--range", f"{base}..HEAD")

    assert result.returncode == 0, result.stderr
    assert "WARN: automation or policy control files changed" in result.stderr
    assert control_path in result.stderr
    assert "BLOCKED" not in result.stderr


def test_range_mode_control_failure_does_not_give_secret_rotation_advice(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, "a.md", "one\n")
    _commit(repo, "one")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    stage(repo, ".claude/settings.json", '{"hooks": {"command": "curl example.test"}}\n')
    _commit(repo, "control")

    result = subprocess.run(
        ["bash", ".sdlc/hooks/secret-guard.sh", "--range", f"{base}..HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "automated tools" in result.stderr
    assert "rotate" not in result.stderr
    assert "looks like a secret" not in result.stderr


def test_supported_json_changed_to_symlink_fails_closed(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, ".claude/settings.json", '{"theme": "dark"}\n')
    _commit(repo, "settings")
    settings = repo / ".claude" / "settings.json"
    settings.unlink()
    settings.symlink_to(Path("..") / "shared-settings.json")
    subprocess.run(["git", "add", ".claude/settings.json"], cwd=repo, check=True)

    result = run_guard(repo)
    assert result.returncode == 2
    assert "could not inspect" in result.stderr
    assert ".claude/settings.json" in result.stderr


def test_control_file_renamed_out_of_control_path_warns(tmp_path):
    repo = guarded_repo(tmp_path)
    stage(repo, ".claude/preferences.json", '{"theme": "dark"}\n')
    _commit(repo, "preferences")
    (repo / "config").mkdir()
    subprocess.run(
        ["git", "mv", ".claude/preferences.json", "config/preferences.json"],
        cwd=repo,
        check=True,
    )

    result = run_guard(repo)
    assert result.returncode == 0, result.stderr
    assert "WARN" in result.stderr
    assert ".claude/preferences.json" in result.stderr


@pytest.mark.parametrize("case", ["clean_after_block"])
def test_block_then_fix_passes(tmp_path, case):
    repo = guarded_repo(tmp_path)
    stage(repo, "src/config.py", f'ACCESS_KEY = "{FAKE_AWS_KEY}"\n')
    assert run_guard(repo).returncode == 1
    unstage_all(repo)
    stage(repo, "src/config.py", 'ACCESS_KEY = os.environ["ACCESS_KEY"]\n')
    assert run_guard(repo).returncode == 0
