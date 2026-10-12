"""Codex-native plugin packaging regression tests."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from conftest import KIT_ROOT

SCRIPT_SKILLS = (
    "shipshape-init",
    "shipshape-doctor",
    "shipshape-customize",
    "shipshape-intent",
    "shipshape-access",
)
EXPLICIT_ONLY_SKILLS = (
    "shipshape-init",
    "shipshape-customize",
    "shipshape-access",
    "shipshape-intent",
    "shipshape-release",
    "shipshape-review",
)


def copy_render_bundle(
    destination: Path,
    *,
    native: bool = True,
    legacy: bool = False,
) -> Path:
    bundle = destination / "Shipshape bundle with spaces"
    (bundle / "scripts").mkdir(parents=True)
    shutil.copy2(KIT_ROOT / "scripts" / "render.py", bundle / "scripts" / "render.py")
    shutil.copytree(KIT_ROOT / "templates", bundle / "templates")
    if native:
        shutil.copytree(KIT_ROOT / ".codex-plugin", bundle / ".codex-plugin")
    if legacy:
        shutil.copytree(KIT_ROOT / ".claude-plugin", bundle / ".claude-plugin")
    return bundle


def run_init_config(bundle: Path, tmp_path: Path) -> tuple[subprocess.CompletedProcess, dict]:
    consumer = tmp_path / "consumer repo"
    consumer.mkdir()
    detect_path = tmp_path / "detect.json"
    detect_path.write_text(json.dumps({"root": "consumer", "languages": [], "git": {}}))
    proc = subprocess.run(
        [
            sys.executable,
            str(bundle / "scripts" / "render.py"),
            "init-config",
            str(consumer),
            "--detect",
            str(detect_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    return proc, payload


def run_bundle_render(bundle: Path, *args: str | Path) -> tuple[subprocess.CompletedProcess, dict]:
    proc = subprocess.run(
        [sys.executable, str(bundle / "scripts" / "render.py"), *map(str, args)],
        capture_output=True,
        text=True,
    )
    return proc, json.loads(proc.stdout)


def set_bundle_versions(bundle: Path, version: str) -> None:
    for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        path = bundle / relative
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["version"] = version
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def test_native_manifest_declares_codex_plugin_contract():
    manifest_path = KIT_ROOT / ".codex-plugin" / "plugin.json"

    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "shipshape"
    assert manifest["version"] == "0.2.1"
    legacy_manifest = json.loads(
        (KIT_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert legacy_manifest["version"] == manifest["version"]
    assert 'version = "0.2.1"' in (KIT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "Shipshape"
    assert manifest["interface"]["shortDescription"]
    assert manifest["interface"]["longDescription"]
    assert manifest["interface"]["developerName"]
    assert manifest["interface"]["category"]
    assert manifest["interface"]["capabilities"]
    assert manifest["interface"]["defaultPrompt"]


def test_explicit_only_skills_use_native_invocation_policy():
    for skill_name in EXPLICIT_ONLY_SKILLS:
        skill_root = KIT_ROOT / "skills" / skill_name
        frontmatter = skill_root.joinpath("SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
        metadata = skill_root.joinpath("agents", "openai.yaml").read_text(encoding="utf-8")

        assert "disable-model-invocation" not in frontmatter
        assert metadata.count("allow_implicit_invocation: false") == 1
        default_prompt = next(
            line for line in metadata.splitlines() if line.strip().startswith("default_prompt:")
        )
        assert f"${skill_name}" in default_prompt


def test_native_only_copied_bundle_reads_version_and_runs_render(tmp_path):
    bundle = copy_render_bundle(tmp_path)

    proc, payload = run_init_config(bundle, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert payload["ok"] is True
    assert payload["config"]["kit_version"] == "0.2.1"
    assert not (bundle / ".claude-plugin").exists()


def test_render_falls_back_to_legacy_manifest_when_native_is_absent(tmp_path):
    bundle = copy_render_bundle(tmp_path, native=False, legacy=True)

    proc, payload = run_init_config(bundle, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert payload["config"]["kit_version"] == "0.2.1"


def test_render_prefers_native_manifest_when_both_are_present(tmp_path):
    bundle = copy_render_bundle(tmp_path, native=True, legacy=True)
    native_manifest_path = bundle / ".codex-plugin" / "plugin.json"
    native_manifest = json.loads(native_manifest_path.read_text(encoding="utf-8"))
    native_manifest["version"] = "0.2.0"
    native_manifest_path.write_text(json.dumps(native_manifest), encoding="utf-8")

    proc, payload = run_init_config(bundle, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert payload["config"]["kit_version"] == "0.2.0"


def test_malformed_native_manifest_does_not_fall_back_to_legacy(tmp_path):
    cases = (
        ("{malformed", "valid JSON"),
        ("[]", "JSON object"),
        ("{}", "non-empty version"),
        ('{"version": 201}', "non-empty version"),
    )
    for index, (contents, expected_error) in enumerate(cases):
        case_path = tmp_path / f"case-{index}"
        bundle = copy_render_bundle(case_path, native=False, legacy=True)
        native_manifest = bundle / ".codex-plugin" / "plugin.json"
        native_manifest.parent.mkdir()
        native_manifest.write_text(contents, encoding="utf-8")

        proc, payload = run_init_config(bundle, case_path)

        assert proc.returncode == 1
        assert payload["ok"] is False
        assert ".codex-plugin/plugin.json" in payload["error"]
        assert expected_error in payload["error"]


def test_copied_bundle_upgrade_updates_config_outputs_and_completed_state(tmp_path):
    bundle = copy_render_bundle(tmp_path, native=True, legacy=True)
    set_bundle_versions(bundle, "0.1.0")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    detect_path = tmp_path / "detect.json"
    detect_path.write_text(
        json.dumps({"root": consumer.name, "languages": [], "git": {}}),
        encoding="utf-8",
    )
    proc, initialized = run_bundle_render(bundle, "init-config", consumer, "--detect", detect_path)
    assert proc.returncode == 0, initialized
    config_path = consumer / ".sdlc/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["private_extension"] = {"keep": True}
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    proc, initial_apply = run_bundle_render(bundle, "apply", consumer)
    assert proc.returncode == 0, initial_apply
    assert "managed-by: shipshape v0.1.0" in (consumer / "AGENTS.md").read_text()

    set_bundle_versions(bundle, "0.2.1")
    proc, plan = run_bundle_render(bundle, "plan", consumer)
    assert proc.returncode == 0, plan
    assert plan["config"]["action"] == "update-version"
    assert "AGENTS.md" in {item["path"] for item in plan["regenerates"]}
    proc, applied = run_bundle_render(
        bundle,
        "apply",
        consumer,
        "--expected-plan-digest",
        plan["plan_digest"],
    )

    assert proc.returncode == 0, applied
    assert applied["status"] == "complete"
    assert applied["binding"]["status"] == "matched"
    upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    assert upgraded["kit_version"] == "0.2.1"
    assert upgraded["private_extension"] == {"keep": True}
    assert "managed-by: shipshape v0.2.1" in (consumer / "AGENTS.md").read_text()
    state = json.loads((consumer / ".sdlc/state.json").read_text(encoding="utf-8"))
    assert state["status"] == "complete"
    assert state["completed_kit_version"] == "0.2.1"


def test_copied_bundle_upgrade_keeps_completion_at_old_version_on_conflict(tmp_path):
    bundle = copy_render_bundle(tmp_path, native=True, legacy=True)
    set_bundle_versions(bundle, "0.1.0")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    detect_path = tmp_path / "detect.json"
    detect_path.write_text(
        json.dumps({"root": consumer.name, "languages": [], "git": {}}),
        encoding="utf-8",
    )
    proc, initialized = run_bundle_render(bundle, "init-config", consumer, "--detect", detect_path)
    assert proc.returncode == 0, initialized
    proc, initial_apply = run_bundle_render(bundle, "apply", consumer)
    assert proc.returncode == 0, initial_apply
    agents = consumer / "AGENTS.md"
    edited = agents.read_bytes() + b"\nOwner edit.\n"
    agents.write_bytes(edited)

    set_bundle_versions(bundle, "0.2.1")
    proc, plan = run_bundle_render(bundle, "plan", consumer)
    assert proc.returncode == 0, plan
    proc, applied = run_bundle_render(
        bundle,
        "apply",
        consumer,
        "--expected-plan-digest",
        plan["plan_digest"],
    )

    assert proc.returncode == 0, applied
    assert applied["status"] == "partial"
    assert agents.read_bytes() == edited
    state = json.loads((consumer / ".sdlc/state.json").read_text(encoding="utf-8"))
    assert state["completed_kit_version"] == "0.1.0"
    assert state["last_run_kit_version"] == "0.2.1"


def test_reviewed_plan_rejects_a_changed_rendered_template_before_writes(tmp_path):
    bundle = copy_render_bundle(tmp_path)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    detect_path = tmp_path / "detect.json"
    detect_path.write_text(
        json.dumps({"root": consumer.name, "languages": [], "git": {}}),
        encoding="utf-8",
    )
    proc, initialized = run_bundle_render(bundle, "init-config", consumer, "--detect", detect_path)
    assert proc.returncode == 0, initialized
    proc, plan = run_bundle_render(bundle, "plan", consumer)
    assert proc.returncode == 0, plan
    config_bytes = (consumer / ".sdlc/config.json").read_bytes()
    template = bundle / "templates/adapters/AGENTS.md.tmpl"
    template.write_text(template.read_text(encoding="utf-8") + "\nRendered change.\n")

    proc, result = run_bundle_render(
        bundle,
        "apply",
        consumer,
        "--expected-plan-digest",
        plan["plan_digest"],
    )

    assert proc.returncode == 1
    assert result["status"] == "stale-plan"
    assert result["binding"]["status"] == "mismatch"
    assert (consumer / ".sdlc/config.json").read_bytes() == config_bytes
    assert not (consumer / "AGENTS.md").exists()
    assert not (consumer / ".sdlc/state.json").exists()


def test_documented_bundle_root_runs_from_other_cwd_with_spaces(tmp_path):
    for skill_name in SCRIPT_SKILLS:
        contents = (KIT_ROOT / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
        normalized = " ".join(contents.split())
        assert "CLAUDE_PLUGIN_ROOT" not in contents
        assert "SHIPSHAPE_ROOT" in contents
        assert "directory containing this loaded `SKILL.md`" in normalized
        assert '"${SHIPSHAPE_ROOT}/scripts/' in contents

    recipes = (KIT_ROOT / "skills" / "shipshape-access" / "references" / "gh-recipes.md").read_text(
        encoding="utf-8"
    )
    assert "CLAUDE_PLUGIN_ROOT" not in recipes
    assert '"${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh"' in recipes

    bundle = tmp_path / "Shipshape bundle with spaces"
    shutil.copytree(KIT_ROOT / "skills", bundle / "skills")
    (bundle / "scripts").mkdir()
    shutil.copy2(KIT_ROOT / "scripts" / "detect.py", bundle / "scripts" / "detect.py")
    skill_file = bundle / "skills" / "shipshape-init" / "SKILL.md"
    bundle_root = skill_file.parent.parent.parent.resolve()
    consumer = tmp_path / "consumer cwd"
    consumer.mkdir()
    (consumer / "pyproject.toml").write_text('[project]\nname = "consumer"\n')
    env = os.environ.copy()
    env["SHIPSHAPE_ROOT"] = str(bundle_root)

    proc = subprocess.run(
        ["/bin/sh", "-c", 'python3 "${SHIPSHAPE_ROOT}/scripts/detect.py" . --no-network'],
        cwd=consumer,
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["root"] == "consumer cwd"
