"""Behavioral coverage for setup preview and filesystem safety."""

import json
import os
from pathlib import Path

from conftest import run_script

CONFIG_PATH = ".sdlc/config.json"
STATE_PATH = ".sdlc/state.json"


def write_detection(path: Path, repo: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "root": repo.name,
                "languages": [{"name": "python", "confidence": "high"}],
                "git": {"default_branch": "main", "owner_repo": ""},
                "test_command_guess": "python3 -m pytest -q",
            }
        ),
        encoding="utf-8",
    )
    return path


def init_config(repo: Path, detect_path: Path) -> dict:
    code, result = run_script("render.py", "init-config", repo, "--detect", detect_path)
    assert code == 0, result
    return result


def snapshot_tree(repo: Path) -> dict[str, tuple[str, bytes | str]]:
    snapshot = {}
    for root, directories, files in os.walk(repo, followlinks=False):
        root_path = Path(root)
        for name in directories + files:
            path = root_path / name
            relative = path.relative_to(repo).as_posix()
            if path.is_symlink():
                snapshot[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                snapshot[relative] = ("file", path.read_bytes())
            else:
                snapshot[relative] = ("dir", b"")
    return snapshot


def configured_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    detect_path = write_detection(tmp_path / "detect.json", repo)
    init_config(repo, detect_path)
    return repo, detect_path


def test_init_config_rerun_preserves_existing_config_bytes(tmp_path):
    repo, detect_path = configured_repo(tmp_path)
    config_path = repo / CONFIG_PATH
    original = config_path.read_bytes() + b"\n"
    config_path.write_bytes(original)

    code, result = run_script("render.py", "init-config", repo, "--detect", detect_path)

    assert code == 1
    assert result["ok"] is False
    assert "already exists" in result["error"]
    assert config_path.read_bytes() == original


def test_proposed_config_and_plan_do_not_modify_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "user.txt").write_text("keep me\n", encoding="utf-8")
    detect_path = write_detection(tmp_path / "detect.json", repo)
    candidate = tmp_path / "candidate.json"
    before = snapshot_tree(repo)

    code, proposed = run_script(
        "render.py",
        "init-config",
        repo,
        "--detect",
        detect_path,
        "--output",
        candidate,
    )
    assert code == 0, proposed
    assert proposed["action"] == "proposed"
    assert snapshot_tree(repo) == before

    code, plan = run_script("render.py", "plan", repo, "--config", candidate)
    assert code == 0, plan
    assert plan["config"]["action"] == "write"
    assert snapshot_tree(repo) == before

    candidate_bytes = candidate.read_bytes()
    code, applied = run_script(
        "render.py",
        "apply",
        repo,
        "--config",
        candidate,
        "--expected-plan-digest",
        plan["plan_digest"],
    )
    assert code == 0, applied
    assert applied["status"] == "complete"
    assert applied["binding"]["status"] == "matched"
    assert applied["config"]["action"] == "written"
    assert (repo / CONFIG_PATH).read_bytes() == candidate_bytes


def test_config_update_candidate_preserves_unknown_keys_and_requires_force(tmp_path):
    repo, _ = configured_repo(tmp_path)
    config_path = repo / CONFIG_PATH
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["private_extension"] = {"keep": [1, 2, 3]}
    config_path.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
    candidate = tmp_path / "customized.json"

    code, proposed = run_script(
        "render.py",
        "set-config",
        repo,
        "--set",
        "features.codeql=false",
        "--output",
        candidate,
    )
    assert code == 0, proposed
    candidate_config = json.loads(candidate.read_text(encoding="utf-8"))
    assert candidate_config["private_extension"] == {"keep": [1, 2, 3]}
    original = config_path.read_bytes()

    code, plan = run_script("render.py", "plan", repo, "--config", candidate)
    assert code == 0, plan
    assert any(item["path"] == CONFIG_PATH for item in plan["conflicts"])
    assert config_path.read_bytes() == original

    code, blocked = run_script(
        "render.py",
        "apply",
        repo,
        "--config",
        candidate,
        "--expected-plan-digest",
        plan["plan_digest"],
    )
    assert code == 0, blocked
    assert blocked["status"] == "blocked"
    assert blocked["written"] == []
    assert config_path.read_bytes() == original

    code, applied = run_script(
        "render.py",
        "apply",
        repo,
        "--config",
        candidate,
        "--expected-plan-digest",
        plan["plan_digest"],
        "--force",
        CONFIG_PATH,
    )
    assert code == 0, applied
    assert applied["config"]["action"] == "written"
    assert config_path.read_bytes() == candidate.read_bytes()


def test_reviewed_candidate_change_is_rejected_before_repository_mutation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "owner.txt").write_text("preserve\n", encoding="utf-8")
    detect_path = write_detection(tmp_path / "detect.json", repo)
    candidate = tmp_path / "candidate.json"
    code, proposed = run_script(
        "render.py", "init-config", repo, "--detect", detect_path, "--output", candidate
    )
    assert code == 0, proposed
    code, plan = run_script("render.py", "plan", repo, "--config", candidate)
    assert code == 0, plan
    before = snapshot_tree(repo)

    changed = json.loads(candidate.read_text(encoding="utf-8"))
    changed["commands"]["test"] = "echo UNREVIEWED-COMMAND"
    candidate.write_text(json.dumps(changed, indent=2) + "\n", encoding="utf-8")
    code, result = run_script(
        "render.py",
        "apply",
        repo,
        "--config",
        candidate,
        "--expected-plan-digest",
        plan["plan_digest"],
    )

    assert code == 1
    assert result["ok"] is False
    assert result["status"] == "stale-plan"
    assert result["complete"] is False
    assert result["binding"]["status"] == "mismatch"
    assert result["written"] == []
    assert result["removed"] == []
    assert result["state_written"] is False
    assert snapshot_tree(repo) == before


def test_candidate_apply_requires_the_reviewed_plan_digest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    detect_path = write_detection(tmp_path / "detect.json", repo)
    candidate = tmp_path / "candidate.json"
    code, proposed = run_script(
        "render.py", "init-config", repo, "--detect", detect_path, "--output", candidate
    )
    assert code == 0, proposed
    before = snapshot_tree(repo)

    code, result = run_script("render.py", "apply", repo, "--config", candidate)

    assert code == 1
    assert result["status"] == "missing-plan-digest"
    assert result["binding"]["status"] == "required"
    assert snapshot_tree(repo) == before


def test_reviewed_candidate_byte_change_is_rejected_even_when_json_is_equal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    detect_path = write_detection(tmp_path / "detect.json", repo)
    candidate = tmp_path / "candidate.json"
    code, proposed = run_script(
        "render.py", "init-config", repo, "--detect", detect_path, "--output", candidate
    )
    assert code == 0, proposed
    code, plan = run_script("render.py", "plan", repo, "--config", candidate)
    assert code == 0, plan
    before = snapshot_tree(repo)
    candidate.write_bytes(candidate.read_bytes().replace(b"\n", b"\r\n"))

    code, result = run_script(
        "render.py",
        "apply",
        repo,
        "--config",
        candidate,
        "--expected-plan-digest",
        plan["plan_digest"],
    )

    assert code == 1
    assert result["status"] == "stale-plan"
    assert result["binding"]["status"] == "mismatch"
    assert snapshot_tree(repo) == before


def test_invalid_config_candidate_is_rejected_before_any_write(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = tmp_path / "candidate.json"
    candidate.write_text("[]\n", encoding="utf-8")
    before = snapshot_tree(repo)

    code, result = run_script("render.py", "apply", repo, "--config", candidate)

    assert code == 1
    assert result["ok"] is False
    assert "JSON object" in result["error"]
    assert snapshot_tree(repo) == before


def test_symlinked_config_is_refused_without_changing_target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sdlc = repo / ".sdlc"
    sdlc.mkdir()
    target = tmp_path / "outside-config.json"
    target.write_text('{"outside": true}\n', encoding="utf-8")
    (sdlc / "config.json").symlink_to(target)
    before = target.read_bytes()

    code, result = run_script("render.py", "plan", repo)

    assert code == 1
    assert result["ok"] is False
    assert "symlink" in result["error"]
    assert target.read_bytes() == before


def test_dangling_config_symlink_is_refused_by_init(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sdlc = repo / ".sdlc"
    sdlc.mkdir()
    missing = tmp_path / "missing-config.json"
    (sdlc / "config.json").symlink_to(missing)
    detect_path = write_detection(tmp_path / "detect.json", repo)

    code, result = run_script("render.py", "init-config", repo, "--detect", detect_path)

    assert code == 1
    assert result["ok"] is False
    assert "symlink" in result["error"]
    assert not missing.exists()


def test_symlinked_destination_is_refused_even_when_forced(tmp_path):
    repo, _ = configured_repo(tmp_path)
    target = tmp_path / "outside-claude.md"
    target.write_text("outside\n", encoding="utf-8")
    (repo / "CLAUDE.md").symlink_to(target)
    before = target.read_bytes()

    code, result = run_script("render.py", "apply", repo, "--force", "CLAUDE.md")

    assert code == 1
    assert result["ok"] is False
    assert any(error["path"] == "CLAUDE.md" for error in result["errors"])
    assert target.read_bytes() == before


def test_dangling_destination_symlink_is_refused(tmp_path):
    repo, _ = configured_repo(tmp_path)
    target = tmp_path / "missing-claude.md"
    (repo / "CLAUDE.md").symlink_to(target)

    code, result = run_script("render.py", "plan", repo)

    assert code == 1
    assert result["ok"] is False
    assert any(error["path"] == "CLAUDE.md" for error in result["errors"])
    assert not target.exists()


def test_symlinked_destination_parent_is_refused_without_escape(tmp_path):
    repo, _ = configured_repo(tmp_path)
    outside = tmp_path / "outside-github"
    outside.mkdir()
    (repo / ".github").symlink_to(outside, target_is_directory=True)

    code, result = run_script("render.py", "apply", repo)

    assert code == 1
    assert result["ok"] is False
    assert any(".github" in error["path"] for error in result["errors"])
    assert list(outside.iterdir()) == []


def test_symlinked_state_file_is_refused_without_changing_target(tmp_path):
    repo, _ = configured_repo(tmp_path)
    state_target = tmp_path / "outside-state.json"
    state_target.write_text('{"kit_version": "outside", "files": {}}\n', encoding="utf-8")
    (repo / STATE_PATH).symlink_to(state_target)
    before = state_target.read_bytes()

    code, result = run_script("render.py", "apply", repo)

    assert code == 1
    assert result["ok"] is False
    assert "symlink" in json.dumps(result)
    assert state_target.read_bytes() == before


def test_dangling_state_symlink_is_refused(tmp_path):
    repo, _ = configured_repo(tmp_path)
    state_path = repo / STATE_PATH
    missing = tmp_path / "missing-state.json"
    state_path.symlink_to(missing)

    code, result = run_script("render.py", "plan", repo)

    assert code == 1
    assert result["ok"] is False
    assert "symlink" in result["error"]
    assert not missing.exists()


def test_user_edited_design_and_config_survive_repair(tmp_path):
    repo, _ = configured_repo(tmp_path)
    code, first = run_script("render.py", "apply", repo)
    assert code == 0, first
    design_path = repo / "docs/sdlc/design.md"
    config_path = repo / CONFIG_PATH
    design = design_path.read_bytes() + b"\nUser-owned design notes.\n"
    config = config_path.read_bytes() + b"\n"
    design_path.write_bytes(design)
    config_path.write_bytes(config)
    old_record = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))["files"][
        "docs/sdlc/design.md"
    ]

    code, plan = run_script("render.py", "plan", repo)
    assert code == 0, plan
    assert any(item["path"] == "docs/sdlc/design.md" for item in plan["skips"])
    code, repaired = run_script("render.py", "apply", repo)

    assert code == 0, repaired
    assert design_path.read_bytes() == design
    assert config_path.read_bytes() == config
    new_record = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))["files"][
        "docs/sdlc/design.md"
    ]
    assert new_record == old_record


def test_crlf_only_drift_conflicts_before_regeneration(tmp_path):
    repo, _ = configured_repo(tmp_path)
    code, first = run_script("render.py", "apply", repo)
    assert code == 0, first
    workflow = repo / ".github/workflows/ci.yml"
    original = workflow.read_bytes()
    assert b"\r\n" not in original
    changed = original.replace(b"\n", b"\r\n")
    workflow.write_bytes(changed)
    code, updated = run_script(
        "render.py", "set-config", repo, "--set", "commands.test=python3 -m pytest tests -q"
    )
    assert code == 0, updated

    code, result = run_script("render.py", "apply", repo)

    assert code == 0, result
    assert result["status"] == "partial"
    assert any(item["path"] == ".github/workflows/ci.yml" for item in result["conflicts_untouched"])
    assert workflow.read_bytes() == changed


def test_mode_only_drift_conflicts_before_removal(tmp_path):
    repo, _ = configured_repo(tmp_path)
    code, configured = run_script(
        "render.py",
        "set-config",
        repo,
        "--set",
        "workflow_style=pr",
        "--set",
        "features.tiered_review=true",
    )
    assert code == 0, configured
    code, first = run_script("render.py", "apply", repo)
    assert code == 0, first
    workflow = repo / ".github/workflows/low-risk-automerge.yml"
    workflow.chmod(0o755)
    code, disabled = run_script(
        "render.py", "set-config", repo, "--set", "features.tiered_review=false"
    )
    assert code == 0, disabled

    code, result = run_script("render.py", "apply", repo)

    assert code == 0, result
    assert result["status"] == "partial"
    assert any(
        item["path"] == ".github/workflows/low-risk-automerge.yml"
        for item in result["conflicts_untouched"]
    )
    assert workflow.is_file()
    assert workflow.stat().st_mode & 0o777 == 0o755


def test_legacy_state_does_not_adopt_an_unknown_mode(tmp_path):
    repo, _ = configured_repo(tmp_path)
    code, first = run_script("render.py", "apply", repo)
    assert code == 0, first
    workflow = repo / ".github/workflows/ci.yml"
    workflow.chmod(0o755)
    state_path = repo / STATE_PATH
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for record in state["files"].values():
        record.pop("mode", None)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    code, plan = run_script("render.py", "plan", repo)

    assert code == 0, plan
    assert any(item["path"] == ".github/workflows/ci.yml" for item in plan["conflicts"])


def test_legacy_state_migrates_known_generated_modes(tmp_path):
    repo, _ = configured_repo(tmp_path)
    code, first = run_script("render.py", "apply", repo)
    assert code == 0, first
    state_path = repo / STATE_PATH
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for record in state["files"].values():
        record.pop("mode", None)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    code, result = run_script("render.py", "apply", repo)

    assert code == 0, result
    migrated = json.loads(state_path.read_text(encoding="utf-8"))
    assert migrated["files"]
    assert all(record["mode"] in {"0644", "0755"} for record in migrated["files"].values())


def test_partial_apply_preserves_completed_version_and_conflict_history(tmp_path):
    repo, _ = configured_repo(tmp_path)
    code, first = run_script("render.py", "apply", repo)
    assert code == 0, first
    claude_path = repo / "CLAUDE.md"
    claude_path.write_text(claude_path.read_text(encoding="utf-8") + "\nUser edit.\n")
    (repo / "AGENTS.md").unlink()
    state_path = repo / STATE_PATH
    state = json.loads(state_path.read_text(encoding="utf-8"))
    old_record = state["files"]["CLAUDE.md"]
    state["kit_version"] = "0.1.0"
    state["completed_kit_version"] = "0.1.0"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    code, result = run_script("render.py", "apply", repo)

    assert code == 0, result
    assert result["ok"] is True
    assert result["status"] == "partial"
    assert "AGENTS.md" in result["written"]
    assert any(item["path"] == "CLAUDE.md" for item in result["conflicts_untouched"])
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["status"] == "partial"
    assert final_state["kit_version"] == "0.1.0"
    assert final_state["completed_kit_version"] == "0.1.0"
    assert final_state["last_run_kit_version"] == "0.2.1"
    assert final_state["pending_conflicts"] == ["CLAUDE.md"]
    assert final_state["files"]["CLAUDE.md"] == old_record


def test_old_init_and_apply_cli_remain_compatible(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    detect_path = write_detection(tmp_path / "detect.json", repo)

    code, initialized = run_script("render.py", "init-config", repo, "--detect", detect_path)
    assert code == 0, initialized
    assert initialized["action"] == "created"
    code, applied = run_script("render.py", "apply", repo)
    assert code == 0, applied
    assert applied["ok"] is True
    assert applied["status"] == "complete"
    assert applied["binding"]["status"] == "unbound"
    assert applied["written"]
    state = json.loads((repo / STATE_PATH).read_text(encoding="utf-8"))
    assert state["status"] == "complete"
    assert state["kit_version"] == "0.2.1"
    assert state["completed_kit_version"] == "0.2.1"
    assert state["last_run_kit_version"] == "0.2.1"
