"""Hook installation preserves user hooks and follows Git's active hook path."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import init_repo, run_script


def rendered_repo(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    code, payload = run_script("render.py", "apply", repo)
    assert code == 0, payload
    subprocess.run(["git", "init", "-qb", "main"], cwd=repo, check=True)
    return repo


def run_installer(repo, *, env=None):
    clean_env = os.environ.copy()
    clean_env["PATH"] = os.defpath
    if env:
        clean_env.update(env)
    return subprocess.run(
        ["bash", ".sdlc/hooks/install.sh"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=clean_env,
    )


def configure_pre_commit_case(repo, case):
    config = repo / ".pre-commit-config.yaml"
    text = config.read_text()
    if case == "global-manual-implicit":
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
    else:
        raise AssertionError(f"unknown pre-commit test case: {case}")
    config.write_text(text)


def test_installer_uses_configured_hooks_path_and_is_idempotent(tmp_path):
    repo = rendered_repo(tmp_path)
    subprocess.run(["git", "config", "core.hooksPath", ".custom hooks"], cwd=repo, check=True)

    first = run_installer(repo)
    hook = repo / ".custom hooks" / "pre-commit"
    first_target = hook.readlink()
    first_bytes = hook.resolve().read_bytes()
    second = run_installer(repo)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert hook.is_symlink()
    assert hook.readlink() == first_target
    assert hook.resolve().read_bytes() == first_bytes
    assert not (repo / ".git/hooks/pre-commit").exists()


def test_installer_uses_direct_guard_for_custom_path_when_pre_commit_exists(tmp_path):
    repo = rendered_repo(tmp_path)
    subprocess.run(["git", "config", "core.hooksPath", ".custom-hooks"], cwd=repo, check=True)
    bin_dir = tmp_path / "unexpected-pre-commit-bin"
    bin_dir.mkdir()
    marker = tmp_path / "pre-commit-was-called"
    pre_commit = bin_dir / "pre-commit"
    pre_commit.write_text(f"#!/bin/sh\nprintf called > '{marker}'\nexit 79\n")
    pre_commit.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}

    result = run_installer(repo, env=env)

    hook = repo / ".custom-hooks/pre-commit"
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert hook.is_symlink()
    assert hook.samefile(repo / ".sdlc/hooks/secret-guard.sh")


def test_installer_fails_closed_when_git_config_errors(tmp_path):
    repo = rendered_repo(tmp_path)
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "failing-git-bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(
        f'#!/bin/sh\nif [ "$1" = "config" ]; then\n  exit 73\nfi\nexec "{real_git}" "$@"\n'
    )
    fake_git.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}

    result = run_installer(repo, env=env)

    assert result.returncode != 0
    assert not (repo / ".git/hooks/pre-commit").exists()
    assert "could not inspect" in result.stderr


def test_installer_preserves_unrelated_existing_hook_byte_for_byte(tmp_path):
    repo = rendered_repo(tmp_path)
    hook = repo / ".git/hooks/pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    original = b"#!/bin/sh\nprintf unrelated-hook"
    hook.write_bytes(original)
    hook.chmod(0o755)

    result = run_installer(repo)

    assert result.returncode != 0
    assert hook.read_bytes() == original
    assert "existing pre-commit hook" in result.stderr
    assert "not changed" in result.stderr


def test_installer_rejects_unrelated_pre_commit_config_without_claiming_success(tmp_path):
    repo = rendered_repo(tmp_path)
    config = repo / ".pre-commit-config.yaml"
    config.write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: unrelated\n"
        "        entry: scripts/unrelated.sh\n"
    )
    original_config = config.read_bytes()
    bin_dir = tmp_path / "fake-pre-commit-bin"
    bin_dir.mkdir()
    marker = tmp_path / "pre-commit-was-called"
    pre_commit = bin_dir / "pre-commit"
    pre_commit.write_text(f"#!/bin/sh\nprintf called > '{marker}'\n")
    pre_commit.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}

    result = run_installer(repo, env=env)

    assert result.returncode != 0
    assert not marker.exists()
    assert config.read_bytes() == original_config
    assert not (repo / ".git/hooks/pre-commit").exists()
    assert "does not wire" in result.stderr
    assert "Installed" not in result.stdout


def test_installer_rejects_incomplete_guard_pre_commit_entry(tmp_path):
    repo = rendered_repo(tmp_path)
    config = repo / ".pre-commit-config.yaml"
    config.write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: secret-guard\n"
        "        entry: .sdlc/hooks/secret-guard.sh\n"
    )
    bin_dir = tmp_path / "incomplete-config-bin"
    bin_dir.mkdir()
    marker = tmp_path / "pre-commit-was-called"
    pre_commit = bin_dir / "pre-commit"
    pre_commit.write_text(f"#!/bin/sh\nprintf called > '{marker}'\n")
    pre_commit.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}

    result = run_installer(repo, env=env)

    assert result.returncode != 0
    assert not marker.exists()
    assert "does not wire" in result.stderr


@pytest.mark.parametrize(
    "case",
    [
        "global-manual-implicit",
        "remote-repo",
        "duplicate-entry",
        "args",
        "hook-manual",
        "duplicate-default",
        "duplicate-repo",
        "guard-not-first",
    ],
)
def test_installer_rejects_unsupported_or_ineffective_guard_config(tmp_path, case):
    repo = rendered_repo(tmp_path)
    configure_pre_commit_case(repo, case)
    bin_dir = tmp_path / "rejected-config-bin"
    bin_dir.mkdir()
    marker = tmp_path / "pre-commit-was-called"
    pre_commit = bin_dir / "pre-commit"
    pre_commit.write_text(f"#!/bin/sh\nprintf called > '{marker}'\nexit 79\n")
    pre_commit.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}

    result = run_installer(repo, env=env)

    assert result.returncode != 0
    assert not marker.exists()
    assert not (repo / ".git/hooks/pre-commit").exists()
    assert "does not wire" in result.stderr


def test_installer_rejects_current_skip_without_installing_dispatcher(tmp_path):
    repo = rendered_repo(tmp_path)
    bin_dir = tmp_path / "skipped-guard-bin"
    bin_dir.mkdir()
    marker = tmp_path / "pre-commit-was-called"
    pre_commit = bin_dir / "pre-commit"
    pre_commit.write_text(f"#!/bin/sh\nprintf called > '{marker}'\nexit 79\n")
    pre_commit.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
        "SKIP": "other-hook,secret-guard",
    }

    result = run_installer(repo, env=env)

    assert result.returncode != 0
    assert not marker.exists()
    assert not (repo / ".git/hooks/pre-commit").exists()


def test_installer_accepts_verified_pre_commit_dispatcher(tmp_path):
    repo = rendered_repo(tmp_path)
    bin_dir = tmp_path / "working-pre-commit-bin"
    bin_dir.mkdir()
    pre_commit = bin_dir / "pre-commit"
    pre_commit.write_text(
        "#!/bin/sh\n"
        'hooks="$(git rev-parse --git-path hooks)"\n'
        'mkdir -p "$hooks"\n'
        "printf '%s\\n' '#!/usr/bin/env bash' '# File generated by pre-commit' "
        "'ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)' "
        "'exec pre-commit \"${ARGS[@]}\"' "
        '> "$hooks/pre-commit"\n'
        'chmod +x "$hooks/pre-commit"\n'
    )
    pre_commit.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}

    result = run_installer(repo, env=env)
    second = run_installer(repo, env=env)

    assert result.returncode == 0, result.stderr
    assert second.returncode == 0, second.stderr
    assert "Installed hooks via the pre-commit framework." in result.stdout


def test_installer_preserves_edited_verified_pre_commit_dispatcher(tmp_path):
    repo = rendered_repo(tmp_path)
    pre_commit = shutil.which("pre-commit")
    if pre_commit is None:
        pytest.skip("genuine pre-commit executable is unavailable")
    env = {"PATH": f"{Path(pre_commit).parent}{os.pathsep}{os.defpath}"}

    first = run_installer(repo, env=env)
    hook = repo / ".git/hooks/pre-commit"
    hook.write_bytes(hook.read_bytes() + b"\n# user-owned custom marker\n")
    edited = hook.read_bytes()
    second = run_installer(repo, env=env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert hook.read_bytes() == edited
    assert "existing wiring was checked" in second.stdout.casefold()


def test_installer_preserves_dispatcher_when_config_becomes_ineffective(tmp_path):
    repo = rendered_repo(tmp_path)
    pre_commit = shutil.which("pre-commit")
    if pre_commit is None:
        pytest.skip("genuine pre-commit executable is unavailable")
    env = {"PATH": f"{Path(pre_commit).parent}{os.pathsep}{os.defpath}"}
    first = run_installer(repo, env=env)
    assert first.returncode == 0, first.stderr
    hook = repo / ".git/hooks/pre-commit"
    hook.write_bytes(hook.read_bytes() + b"\n# user-owned custom marker\n")
    edited = hook.read_bytes()
    configure_pre_commit_case(repo, "global-manual-implicit")

    second = run_installer(repo, env=env)

    assert second.returncode != 0
    assert hook.read_bytes() == edited
    assert "not changed" in second.stderr


def test_generated_guard_runs_at_commit_with_manual_global_default_stage(tmp_path):
    pre_commit = shutil.which("pre-commit")
    if pre_commit is None:
        pytest.skip("genuine pre-commit executable is unavailable")
    repo = rendered_repo(tmp_path)
    config = repo / ".pre-commit-config.yaml"
    config.write_text("default_stages: [manual]\n" + config.read_text())
    env = {
        "PATH": f"{Path(pre_commit).parent}{os.pathsep}{os.defpath}",
        "PRE_COMMIT_HOME": str(tmp_path / "pre-commit-home"),
    }

    installation = run_installer(repo, env=env)
    synthetic_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    (repo / "note.txt").write_text(f"{synthetic_key}\n")
    subprocess.run(["git", "add", "note.txt"], cwd=repo, check=True)
    commit_env = os.environ.copy()
    commit_env.update(env)
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": "Shipshape Test",
            "GIT_AUTHOR_EMAIL": "shipshape@example.invalid",
            "GIT_COMMITTER_NAME": "Shipshape Test",
            "GIT_COMMITTER_EMAIL": "shipshape@example.invalid",
        }
    )
    commit = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "test: probe secret guard"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=commit_env,
    )

    assert installation.returncode == 0, installation.stderr
    assert commit.returncode != 0, "a real commit bypassed the configured secret guard"
    assert "look like an API key" in commit.stderr
