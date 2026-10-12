"""Drift and idempotency tests — the never-clobber contract.

Shipshape must: apply twice with no changes (idempotent), refuse to touch a
file the user edited, refuse to adopt a pre-existing file it never wrote,
overwrite only with an explicit --force, and never rewrite write-once files.
"""

from conftest import init_repo, run_script


def test_apply_twice_is_idempotent(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    code, first = run_script("render.py", "apply", repo)
    assert code == 0 and first["written"]

    state_before = (repo / ".sdlc/state.json").read_bytes()
    code, second = run_script("render.py", "apply", repo)
    assert code == 0
    assert second["written"] == []
    assert not second["conflicts_untouched"]
    assert (repo / ".sdlc/state.json").read_bytes() == state_before


def test_user_edit_becomes_conflict_and_is_never_touched(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    run_script("render.py", "apply", repo)

    claude_md = repo / "CLAUDE.md"
    edited = claude_md.read_text() + "\nMy own extra rule: always be kind.\n"
    claude_md.write_text(edited)

    code, plan = run_script("render.py", "plan", repo)
    assert code == 0
    assert [c["path"] for c in plan["conflicts"]] == ["CLAUDE.md"]

    code, result = run_script("render.py", "apply", repo)
    assert code == 0
    assert claude_md.read_text() == edited, "apply touched a user-edited file"
    assert [c["path"] for c in result["conflicts_untouched"]] == ["CLAUDE.md"]


def test_force_overwrites_only_named_conflict(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    run_script("render.py", "apply", repo)

    (repo / "CLAUDE.md").write_text("totally rewritten\n")
    harness = repo / "docs/sdlc/harness.md"
    harness.write_text(harness.read_text() + "\nedited too\n")

    code, result = run_script("render.py", "apply", repo, "--force", "CLAUDE.md")
    assert code == 0
    assert result["forced"] == ["CLAUDE.md"]
    assert "managed-by: shipshape" in (repo / "CLAUDE.md").read_text()
    assert "edited too" in harness.read_text(), "--force bled onto another file"


def test_preexisting_file_is_conflict_not_adoption(tmp_path):
    repo = init_repo("repo-node", tmp_path)
    ci = repo / ".github/workflows/ci.yml"
    ci.parent.mkdir(parents=True)
    ci.write_text("name: my own pipeline\n")

    code, result = run_script("render.py", "apply", repo)
    assert code == 0
    assert ci.read_text() == "name: my own pipeline\n"
    assert [c["path"] for c in result["conflicts_untouched"]] == [".github/workflows/ci.yml"]
    assert "not written by shipshape" in result["conflicts_untouched"][0]["reason"]


def test_write_once_design_doc_survives_config_change(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    run_script("render.py", "apply", repo)
    design = repo / "docs/sdlc/design.md"
    original = design.read_text()

    import json

    config_path = repo / ".sdlc/config.json"
    config = json.loads(config_path.read_text())
    config["project_name"] = "renamed-project"
    config_path.write_text(json.dumps(config, indent=2))

    code, result = run_script("render.py", "apply", repo)
    assert code == 0
    assert design.read_text() == original, "write-once file was regenerated"
    regenerated = set(result["written"])
    assert "CLAUDE.md" in regenerated, "managed unedited files should regenerate"


def test_force_unknown_path_is_error(tmp_path):
    repo = init_repo("repo-python", tmp_path)
    run_script("render.py", "apply", repo)
    code, payload = run_script("render.py", "apply", repo, "--force", "nope.md")
    assert code == 1
    assert "force" in payload["error"]
