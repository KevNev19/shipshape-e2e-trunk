"""Package-manager detection and rendered Node workflow tests."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import KIT_ROOT, SCRIPTS, run_script

WORKFLOW_PATHS = (
    ".github/workflows/ci.yml",
    ".github/workflows/copilot-setup-steps.yml",
)
PINNED_COREPACK_INSTALL = "npm install --global corepack@0.34.7"


def node_package(package_manager: str | None = None) -> dict:
    package = {
        "name": "node-app",
        "private": True,
        "scripts": {"test": "vitest run"},
        "devDependencies": {"vitest": "^4.0.0"},
    }
    if package_manager:
        package["packageManager"] = package_manager
    return package


def detect_node_repo(
    tmp_path: Path,
    package_json: dict,
    files: dict[str, str | bytes],
) -> tuple[Path, dict, Path]:
    repo = tmp_path / "node-repo"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps(package_json), encoding="utf-8")
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "detect.py"), str(repo), "--no-network"],
        capture_output=True,
        text=True,
        check=True,
    )
    detect_file = tmp_path / "detect.json"
    detect_file.write_text(proc.stdout, encoding="utf-8")
    return repo, json.loads(proc.stdout), detect_file


def init_node_repo(
    tmp_path: Path,
    package_json: dict,
    files: dict[str, str | bytes],
) -> tuple[Path, dict, dict]:
    repo, detected, detect_file = detect_node_repo(tmp_path, package_json, files)
    code, result = run_script(
        "render.py",
        "init-config",
        repo,
        "--detect",
        detect_file,
        "--set",
        "features.secret_guard=false",
    )
    assert code == 0, result
    return repo, detected, result["config"]


def render_node_repo(
    tmp_path: Path,
    package_json: dict,
    files: dict[str, str | bytes],
) -> tuple[Path, dict, dict]:
    repo, detected, config = init_node_repo(tmp_path, package_json, files)
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result
    return repo, detected, config


def rendered_workflows(repo: Path) -> list[str]:
    return [(repo / relative).read_text(encoding="utf-8") for relative in WORKFLOW_PATHS]


def test_pnpm_workspace_renders_pnpm_in_ci_and_copilot_setup(tmp_path):
    package = node_package("pnpm@10.15.1")
    package["dependencies"] = {"workspace-package": "workspace:*"}
    repo, detected, config = render_node_repo(
        tmp_path,
        package,
        {"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"},
    )

    assert detected["node_package_manager"]["name"] == "pnpm"
    assert config["node_package_manager"]["version"] == "10.15.1"
    assert config["toolchain"]["node"] == "24"
    for workflow in rendered_workflows(repo):
        assert PINNED_COREPACK_INSTALL in workflow
        assert "corepack enable pnpm" in workflow
        assert "pnpm install --frozen-lockfile" in workflow
        assert "run: pnpm test" in workflow
        assert "if [ -f package-lock.json ]; then npm ci; else npm install; fi" not in workflow
        assert "run: npm test" not in workflow


def test_npm_lock_renders_npm_ci_and_test_in_both_workflows(tmp_path):
    repo, detected, config = render_node_repo(
        tmp_path,
        node_package(),
        {"package-lock.json": '{"lockfileVersion": 3}\n'},
    )

    assert detected["node_package_manager"]["name"] == "npm"
    assert config["node_package_manager"]["lockfiles"] == ["package-lock.json"]
    for workflow in rendered_workflows(repo):
        assert "run: npm ci" in workflow
        assert "run: npm test" in workflow
        assert "npm install" not in workflow


def test_declared_npm_version_is_honored_by_workflow(tmp_path):
    repo, detected, config = render_node_repo(
        tmp_path,
        node_package("npm@10.9.3"),
        {"package-lock.json": '{"lockfileVersion": 3}\n'},
    )

    assert detected["node_package_manager"]["variant"] == "npm_corepack"
    assert config["node_package_manager"]["version"] == "10.9.3"
    for workflow in rendered_workflows(repo):
        assert "npm install --global npm@10.9.3" in workflow
        assert "corepack" not in workflow
        assert "run: npm ci" in workflow
        assert "run: npm test" in workflow


def test_declared_npm_integrity_hash_is_not_used_as_the_registry_version(tmp_path):
    repo, detected, config = render_node_repo(
        tmp_path,
        node_package("npm@10.9.3+sha224.abcdef"),
        {"package-lock.json": '{"lockfileVersion": 3}\n'},
    )

    assert detected["node_package_manager"]["version"] == "10.9.3+sha224.abcdef"
    assert config["node_package_manager"]["version"] == "10.9.3+sha224.abcdef"
    for workflow in rendered_workflows(repo):
        assert "npm install --global npm@10.9.3" in workflow
        assert "sha224" not in workflow


@pytest.mark.parametrize(
    ("manager", "lock", "install"),
    [
        (
            "yarn@1.22.22",
            "# THIS IS AN AUTOGENERATED FILE.\n# yarn lockfile v1\n",
            "yarn install --frozen-lockfile",
        ),
        (
            "yarn@4.9.2",
            "__metadata:\n  version: 8\n  cacheKey: 10c0\n",
            "yarn install --immutable",
        ),
    ],
)
def test_yarn_lock_format_selects_classic_or_modern_install(tmp_path, manager, lock, install):
    repo, detected, config = render_node_repo(
        tmp_path,
        node_package(manager),
        {"yarn.lock": lock},
    )

    expected_variant = "yarn_classic" if manager.startswith("yarn@1.") else "yarn_modern"
    assert detected["node_package_manager"]["variant"] == expected_variant
    assert config["node_package_manager"]["variant"] == expected_variant
    for workflow in rendered_workflows(repo):
        assert PINNED_COREPACK_INSTALL in workflow
        assert "corepack enable yarn" in workflow
        assert install in workflow
        assert "run: yarn test" in workflow


@pytest.mark.parametrize(
    ("lockfile", "content"),
    [
        ("bun.lock", '{\n  "lockfileVersion": 1\n}\n'),
        ("bun.lockb", b"bun-lockfile"),
    ],
)
def test_bun_text_and_binary_locks_render_bun_ci(lockfile, content, tmp_path):
    repo, detected, config = render_node_repo(
        tmp_path,
        node_package("bun@1.2.20"),
        {lockfile: content},
    )

    assert detected["node_package_manager"]["name"] == "bun"
    assert config["node_package_manager"]["lockfiles"] == [lockfile]
    for workflow in rendered_workflows(repo):
        assert "uses: oven-sh/setup-bun@v2" in workflow
        assert 'bun-version: "1.2.20"' in workflow
        assert "run: bun ci" in workflow
        assert "run: bun run test" in workflow


def test_declared_manager_without_lock_reports_usable_configuration_error(tmp_path):
    repo, detected, detect_file = detect_node_repo(
        tmp_path,
        node_package("pnpm@10.15.1"),
        {},
    )

    assert detected["node_package_manager"]["name"] == "pnpm"
    code, result = run_script("render.py", "init-config", repo, "--detect", detect_file)
    assert code == 1
    assert "pnpm-lock.yaml" in result["error"]
    assert "commit" in result["error"].lower()
    assert not (repo / ".sdlc/config.json").exists()


def test_package_json_without_a_lockfile_is_rejected_for_fresh_setup(tmp_path):
    repo, detected, detect_file = detect_node_repo(tmp_path, node_package(), {})

    assert detected["node_package_manager"]["name"] == "npm"
    assert "lockfile" in detected["node_package_manager"]["error"].lower()
    code, result = run_script("render.py", "init-config", repo, "--detect", detect_file)

    assert code == 1
    assert "package-lock.json" in result["error"]
    assert not (repo / ".sdlc/config.json").exists()


@pytest.mark.parametrize(
    ("lockfile", "contents", "manager"),
    [
        ("pnpm-lock.yaml", "lockfileVersion: 9\n", "pnpm"),
        ("yarn.lock", "# yarn lockfile v1\n", "yarn"),
        ("bun.lock", '{"lockfileVersion": 1}\n', "bun"),
    ],
)
def test_non_npm_lock_requires_an_exact_package_manager_declaration(
    tmp_path, lockfile, contents, manager
):
    repo, detected, detect_file = detect_node_repo(tmp_path, node_package(), {lockfile: contents})

    assert detected["node_package_manager"]["name"] == manager
    assert "exact package.json packageManager" in detected["node_package_manager"]["error"]
    code, result = run_script("render.py", "init-config", repo, "--detect", detect_file)

    assert code == 1
    assert f'"packageManager": "{manager}@' in result["error"]
    assert not (repo / ".sdlc/config.json").exists()


@pytest.mark.parametrize(
    ("manager", "files"),
    [
        ("pnpm@10.15.1", {"package-lock.json": "{}\n"}),
        (None, {"package-lock.json": "{}\n", "pnpm-lock.yaml": "lockfileVersion: 9\n"}),
    ],
)
def test_contradictory_manager_signals_fail_instead_of_defaulting_to_npm(tmp_path, manager, files):
    repo, detected, detect_file = detect_node_repo(tmp_path, node_package(manager), files)

    assert detected["package_managers"] == ["npm", "pnpm"]
    assert "conflicting Node package-manager signals" in detected["node_package_manager"]["error"]
    code, result = run_script("render.py", "init-config", repo, "--detect", detect_file)
    assert code == 1
    assert "conflicting Node package-manager signals" in result["error"]
    assert not (repo / ".sdlc/config.json").exists()


def test_unsafe_declared_manager_version_is_rejected_before_render(tmp_path):
    repo, detected, detect_file = detect_node_repo(
        tmp_path,
        node_package("pnpm@10.15.1; echo injected"),
        {"pnpm-lock.yaml": "lockfileVersion: 9\n"},
    )

    assert "exact safe version" in detected["node_package_manager"]["error"]
    code, result = run_script("render.py", "init-config", repo, "--detect", detect_file)
    assert code == 1
    assert "exact safe version" in result["error"]
    assert not (repo / ".github/workflows/ci.yml").exists()


def test_unsafe_manager_version_override_does_not_modify_config(tmp_path):
    repo, _, config = render_node_repo(
        tmp_path,
        node_package("pnpm@10.15.1"),
        {"pnpm-lock.yaml": "lockfileVersion: 9\n"},
    )

    code, result = run_script(
        "render.py",
        "set-config",
        repo,
        "--set",
        "node_package_manager.version=10.15.1; echo injected",
    )
    assert code == 1
    assert "exact safe" in result["error"]
    persisted = json.loads((repo / ".sdlc/config.json").read_text(encoding="utf-8"))
    assert persisted == config


def test_old_node_config_keeps_compatible_npm_fallback(tmp_path):
    repo = tmp_path / "old-config"
    config_path = repo / ".sdlc/config.json"
    config_path.parent.mkdir(parents=True)
    config = json.loads((KIT_ROOT / "templates/sdlc/config.json").read_text(encoding="utf-8"))
    config.pop("node_package_manager", None)
    config.update(
        {
            "kit_version": "0.2.1",
            "project_name": "old-config",
            "languages": ["node"],
            "commands": {"test": "npm test", "lint": ""},
        }
    )
    config["features"]["secret_guard"] = False
    config_path.write_text(json.dumps(config), encoding="utf-8")

    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result
    for workflow in rendered_workflows(repo):
        assert "if [ -f package-lock.json ]; then npm ci; else npm install; fi" in workflow
        assert "run: npm test" in workflow


def test_legacy_detection_without_reproducible_node_details_is_rejected(tmp_path):
    repo = tmp_path / "legacy-detection"
    repo.mkdir()
    detect_file = tmp_path / "detect.json"
    detect_file.write_text(
        json.dumps(
            {
                "root": repo.name,
                "languages": [{"name": "node", "confidence": "high"}],
                "package_managers": ["npm"],
                "git": {},
            }
        ),
        encoding="utf-8",
    )

    code, result = run_script("render.py", "init-config", repo, "--detect", detect_file)

    assert code == 1
    assert "rerun scripts/detect.py" in result["error"]
    assert not (repo / ".sdlc/config.json").exists()


@pytest.mark.parametrize(
    ("declaration", "files", "manager_step", "install_step"),
    [
        (
            "pnpm@10.15.1",
            {"pnpm-lock.yaml": "lockfileVersion: 9\n"},
            "corepack enable pnpm",
            "pnpm install --frozen-lockfile",
        ),
        (
            "yarn@4.9.2",
            {"yarn.lock": "__metadata:\n  version: 8\n"},
            "corepack enable yarn",
            "yarn install --immutable",
        ),
    ],
)
def test_node_25_installs_pinned_corepack_before_enabling_manager(
    tmp_path, declaration, files, manager_step, install_step
):
    repo, _, _ = init_node_repo(tmp_path, node_package(declaration), files)
    code, updated = run_script("render.py", "set-config", repo, "--set", "toolchain.node=25")
    assert code == 0, updated
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result

    for workflow in rendered_workflows(repo):
        assert 'node-version: "25"' in workflow
        assert PINNED_COREPACK_INSTALL in workflow
        assert manager_step in workflow
        assert install_step in workflow
        assert workflow.index(PINNED_COREPACK_INSTALL) < workflow.index(manager_step)
        assert workflow.index(manager_step) < workflow.index(install_step)


def test_node_25_declared_npm_installs_the_exact_manager_without_corepack(tmp_path):
    repo, _, _ = init_node_repo(
        tmp_path,
        node_package("npm@10.9.3"),
        {"package-lock.json": '{"lockfileVersion": 3}\n'},
    )
    code, updated = run_script("render.py", "set-config", repo, "--set", "toolchain.node=25")
    assert code == 0, updated
    code, result = run_script("render.py", "apply", repo)
    assert code == 0, result

    for workflow in rendered_workflows(repo):
        assert 'node-version: "25"' in workflow
        assert "npm install --global npm@10.9.3" in workflow
        assert "corepack" not in workflow
        assert "run: npm ci" in workflow
