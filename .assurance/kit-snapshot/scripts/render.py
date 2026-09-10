#!/usr/bin/env python3
"""Template renderer for shipshape (ADR 0003: {{TOKEN}} substitution, no engine).

Four subcommands, all emitting one JSON object on stdout (skills branch on
the JSON and do the talking):

    render.py init-config <repo> --detect FILE [--output FILE] [--set key=value]...
        Build config defaults merged with detection output and overrides.
        --output creates a proposal outside the repo; without it, a missing
        repo config is created for backward compatibility.

    render.py set-config <repo> --set key=value [--output FILE]...
        Update selected keys while preserving the rest. --output creates a
        proposal instead of modifying the repository.

    render.py plan <repo> [--config FILE]
        Dry run. Classify every applicable template as write / regenerate /
        skip / conflict using the exact candidate config when supplied.

    render.py apply <repo> [--config FILE] [--expected-plan-digest SHA256]
                    [--force <dest>]...
        Perform the writes and regenerates from the plan. Conflicted files
        are NEVER touched unless their dest path is named with --force.
        A reviewed --config flow requires the digest returned by plan.

Drift contract: .sdlc/state.json records the raw-byte sha256 and file mode of
every file as shipshape wrote it. A file whose current bytes or mode no longer
match becomes a conflict — shipshape never clobbers it silently.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = KIT_ROOT / "templates"
TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")

# Fallback when a repo has no detectable test command: CI still runs and
# tells the user what to do instead of failing mysteriously.
NO_TESTS_CMD = 'echo "No tests configured yet - see docs/sdlc/harness.md"'
COREPACK_VERSION = "0.34.7"
EXACT_MANAGER_VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
NODE_MANAGER_VARIANTS = {
    "npm": {"npm", "npm_corepack", "npm_legacy"},
    "pnpm": {"pnpm"},
    "yarn": {"yarn_classic", "yarn_modern"},
    "bun": {"bun"},
}
NODE_MANAGER_LOCKFILES = {
    "npm": {"package-lock.json", "npm-shrinkwrap.json"},
    "pnpm": {"pnpm-lock.yaml"},
    "yarn": {"yarn.lock"},
    "bun": {"bun.lock", "bun.lockb"},
}
CONFIG_DEST = ".sdlc/config.json"
STATE_DEST = ".sdlc/state.json"
PLAN_DIGEST_SCHEMA = 1


class PluginManifestError(ValueError):
    """A bundled plugin manifest cannot provide a usable kit version."""


class RenderError(ValueError):
    """A render input or filesystem path is unsafe or invalid."""


def fail(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}))
    return 1


def kit_version() -> str:
    native_manifest = KIT_ROOT / ".codex-plugin" / "plugin.json"
    legacy_manifest = KIT_ROOT / ".claude-plugin" / "plugin.json"
    if native_manifest.exists():
        manifest_path = native_manifest
    elif legacy_manifest.exists():
        manifest_path = legacy_manifest
    else:
        raise PluginManifestError(
            "plugin manifest not found; expected .codex-plugin/plugin.json "
            "or .claude-plugin/plugin.json"
        )

    manifest_name = manifest_path.relative_to(KIT_ROOT).as_posix()
    if not manifest_path.is_file():
        raise PluginManifestError(f"{manifest_name} must be a file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PluginManifestError(f"{manifest_name} must contain valid JSON") from error
    except OSError as error:
        raise PluginManifestError(f"unable to read {manifest_name}: {error}") from error
    if not isinstance(manifest, dict):
        raise PluginManifestError(f"{manifest_name} must contain a JSON object")
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PluginManifestError(f"{manifest_name} must contain a non-empty version")
    return version


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def generated_mode(raw_mode: str | None) -> int:
    return int(raw_mode, 8) if raw_mode else 0o644


def serialized_mode(mode: int) -> str:
    return f"{stat.S_IMODE(mode):04o}"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def relative_parts(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise RenderError(f"unsafe repository-relative path: {relative}")
    return path.parts


def repo_path_status(repo: Path, relative: str) -> tuple[Path, str]:
    """Inspect a repo path without following an existing symlink component."""
    current = repo
    parts = relative_parts(relative)
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return repo.joinpath(*parts), "missing"
        visible = Path(*parts[: index + 1]).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise RenderError(f"{relative}: symlink path component is not allowed: {visible}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise RenderError(f"{relative}: parent path is not a directory: {visible}")
    if stat.S_ISREG(info.st_mode):
        return current, "file"
    if stat.S_ISDIR(info.st_mode):
        return current, "directory"
    return current, "other"


def ensure_repo_parent(repo: Path, relative: str) -> Path:
    parts = relative_parts(relative)
    current = repo
    for index, part in enumerate(parts[:-1]):
        current = current / part
        visible = Path(*parts[: index + 1]).as_posix()
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RenderError(f"{relative}: symlink path component is not allowed: {visible}")
        if not stat.S_ISDIR(info.st_mode):
            raise RenderError(f"{relative}: parent path is not a directory: {visible}")
    return repo.joinpath(*parts).parent


def atomic_replace(path: Path, content: bytes, mode: int | None = None) -> None:
    """Replace one file atomically within its existing filesystem."""
    if mode is None:
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
        except FileNotFoundError:
            mode = 0o644
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=".shipshape-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_repo(
    repo: Path,
    relative: str,
    content: str | bytes,
    mode: int | None = None,
) -> None:
    path, status = repo_path_status(repo, relative)
    if status in ("directory", "other"):
        raise RenderError(f"{relative}: destination must be a regular file")
    ensure_repo_parent(repo, relative)
    # Recheck after creating parents. This narrows, but does not eliminate, the
    # interval in which another process could change the filesystem.
    path, status = repo_path_status(repo, relative)
    if status in ("directory", "other"):
        raise RenderError(f"{relative}: destination must be a regular file")
    data = content.encode("utf-8") if isinstance(content, str) else content
    atomic_replace(path, data, mode)


def atomic_write_external(path: Path, content: str) -> None:
    if path.is_symlink():
        raise RenderError(f"output path is a symlink: {path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise RenderError(f"output parent must be a real directory: {path.parent}")
    if path.exists():
        raise RenderError(f"output path already exists: {path}")
    atomic_replace(path, content.encode("utf-8"))


def is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def read_config_file(path: Path, label: str) -> tuple[dict, bytes]:
    if path.is_symlink():
        raise RenderError(f"{label} is a symlink: {path}")
    if not path.is_file():
        raise RenderError(f"{label} not found: {path}")
    content = path.read_bytes()
    text = content.decode("utf-8")
    config = json.loads(text)
    problems = validate_config(config)
    if problems:
        raise RenderError(f"{label} is invalid: {'; '.join(problems)}")
    return config, content


def set_dotted(config: dict, dotted_key: str, raw_value: str) -> None:
    """Apply one --set override like features.codeql=false or commands.test=pytest."""
    if not dotted_key or any(not key for key in dotted_key.split(".")):
        raise RenderError(f"invalid config key: {dotted_key or '(empty)'}")
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        value = raw_value
    node = config
    keys = dotted_key.split(".")
    for key in keys[:-1]:
        child = node.setdefault(key, {})
        if not isinstance(child, dict):
            raise RenderError(f"cannot set {dotted_key}: {key} is not an object")
        node = child
    node[keys[-1]] = value


def node_manager_variant(config: dict) -> str:
    """Return the Node template variant, retaining old config behavior."""
    if "node_package_manager" not in config:
        return "npm_legacy"
    manager = config.get("node_package_manager")
    return manager.get("variant", "") if isinstance(manager, dict) else ""


def validate_node_manager_config(config: dict) -> list[str]:
    languages = config.get("languages", [])
    if not languages or languages[0] != "node":
        return []
    if "node_package_manager" not in config:
        return []
    manager = config.get("node_package_manager")
    if not isinstance(manager, dict):
        return ["node_package_manager must be an object"]
    name = manager.get("name")
    variant = manager.get("variant")
    version = manager.get("version", "")
    lockfiles = manager.get("lockfiles", [])
    if name not in NODE_MANAGER_VARIANTS:
        return [f"unsupported Node package manager in config: {name or '(empty)'}"]
    if variant not in NODE_MANAGER_VARIANTS[name]:
        return [f"Node package manager {name} cannot use template variant {variant or '(empty)'}"]
    if not isinstance(version, str) or (
        version and not EXACT_MANAGER_VERSION_RE.fullmatch(version)
    ):
        return ["node_package_manager.version must be an exact safe x.y.z version"]
    if not isinstance(lockfiles, list) or not all(isinstance(item, str) for item in lockfiles):
        return ["node_package_manager.lockfiles must be a list of paths"]
    if variant == "npm_legacy":
        return [
            "node_package_manager.variant npm_legacy is reserved for existing configs "
            "without node_package_manager"
        ]
    if not lockfiles or not set(lockfiles).issubset(NODE_MANAGER_LOCKFILES[name]):
        expected = " or ".join(sorted(NODE_MANAGER_LOCKFILES[name]))
        return [f"node_package_manager for {name} requires a matching {expected}"]
    if name != "npm" and not version:
        return [f"node_package_manager.version must pin an exact {name} version"]
    if variant == "npm_corepack" and not version:
        return ["node_package_manager.version must pin an exact npm version"]
    return []


def validate_config(config) -> list[str]:
    if not isinstance(config, dict):
        return ["config must contain a JSON object"]
    problems = []
    languages = config.get("languages", [])
    if not isinstance(languages, list) or not all(isinstance(item, str) for item in languages):
        problems.append("languages must be a list of strings")
    for key in ("features", "commands", "toolchain", "owners", "repo"):
        if not isinstance(config.get(key, {}), dict):
            problems.append(f"{key} must be an object")
    if problems:
        return problems
    for key in ("kit_version", "project_name", "default_branch", "workflow_style", "profile"):
        if key in config and not isinstance(config[key], str):
            problems.append(f"{key} must be a string")
    commands = config.get("commands", {})
    features = config.get("features", {})
    owners = config.get("owners", {})
    repository = config.get("repo", {})
    for key in ("test", "lint"):
        if key in commands and not isinstance(commands[key], str):
            problems.append(f"commands.{key} must be a string")
    for key, value in features.items():
        if not isinstance(key, str) or not isinstance(value, bool):
            problems.append("features must map string keys to booleans")
            break
    if "default" in owners and not isinstance(owners["default"], str):
        problems.append("owners.default must be a string")
    if "owner_repo" in repository and not isinstance(repository["owner_repo"], str):
        problems.append("repo.owner_repo must be a string")
    if problems:
        return problems
    return validate_node_manager_config(config)


def validate_detection(detected) -> list[str]:
    if not isinstance(detected, dict):
        return ["detection output must contain a JSON object"]
    languages = detected.get("languages", [])
    if not isinstance(languages, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in languages
    ):
        return ["detection languages must be a list of named objects"]
    if not isinstance(detected.get("git", {}), dict):
        return ["detection git field must be an object"]
    git_info = detected.get("git", {})
    for key in ("default_branch", "owner_repo"):
        if key in git_info and git_info[key] is not None and not isinstance(git_info[key], str):
            return [f"detection git.{key} must be a string"]
    test_command = detected.get("test_command_guess")
    if test_command is not None and not isinstance(test_command, str):
        return ["detection test_command_guess must be a string"]
    managers = detected.get("package_managers", [])
    if not isinstance(managers, list) or not all(isinstance(item, str) for item in managers):
        return ["detection package_managers must be a list of strings"]
    return []


def manager_test_command(command: str, manager: str) -> str:
    if command != "npm test":
        return command
    if manager == "bun":
        return "bun run test"
    return f"{manager} test"


def build_initial_config(repo: Path, detect_file: Path, overrides: list[str]) -> dict:
    config = load_json(TEMPLATES_DIR / "sdlc" / "config.json")
    detected = load_json(detect_file)
    if not isinstance(config, dict):
        raise RenderError("bundled config template must contain a JSON object")
    detection_problems = validate_detection(detected)
    if detection_problems:
        raise RenderError("; ".join(detection_problems))

    config["kit_version"] = kit_version()
    config["project_name"] = detected.get("root", repo.name) or repo.name
    config["languages"] = [
        lang["name"]
        for lang in detected.get("languages", [])
        if lang.get("confidence") in ("high", "medium")
    ]
    git_info = detected.get("git", {})
    config["default_branch"] = git_info.get("default_branch") or "main"
    config["repo"]["owner_repo"] = git_info.get("owner_repo", "")
    owner_repo = config["repo"]["owner_repo"]
    if owner_repo and "/" in owner_repo:
        config["owners"]["default"] = "@" + owner_repo.split("/")[0]
    test_command = detected.get("test_command_guess") or NO_TESTS_CMD
    if "node" in config["languages"]:
        node_manager = detected.get("node_package_manager")
        if node_manager is None:
            raise RenderError(
                "legacy detection output lacks reproducible Node setup details; "
                "rerun scripts/detect.py and init-config"
            )
        if not isinstance(node_manager, dict):
            raise RenderError("node_package_manager detection output must be an object")
        if node_manager.get("error"):
            raise RenderError(str(node_manager["error"]))
        config["node_package_manager"] = {
            key: node_manager.get(key, default)
            for key, default in (
                ("name", ""),
                ("version", ""),
                ("variant", ""),
                ("lockfiles", []),
            )
        }
        test_command = manager_test_command(test_command, config["node_package_manager"]["name"])
    config["commands"]["test"] = test_command

    for override in overrides:
        if "=" not in override:
            raise RenderError(f"--set expects key=value, got: {override}")
        key, _, value = override.partition("=")
        set_dotted(config, key, value)

    problems = validate_config(config)
    if problems:
        raise RenderError("; ".join(problems))
    return config


def serialized_config(config: dict) -> str:
    return json.dumps(config, indent=2) + "\n"


def cmd_init_config(
    repo: Path,
    detect_file: Path,
    overrides: list[str],
    output: Path | None,
) -> int:
    config = build_initial_config(repo, detect_file, overrides)
    content = serialized_config(config)

    if output is not None:
        output = Path(os.path.abspath(output))
        if is_within(output, repo):
            raise RenderError("proposed config output must be outside the target repository")
        atomic_write_external(output, content)
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "proposed",
                    "config_path": str(output),
                    "sha256": sha256_text(content),
                    "config": config,
                },
                indent=2,
            )
        )
        return 0

    _, status = repo_path_status(repo, CONFIG_DEST)
    if status == "file":
        return fail(
            f"{CONFIG_DEST} already exists; preserve it or use a reviewed candidate "
            f"with apply --force {CONFIG_DEST}"
        )
    if status != "missing":
        raise RenderError(f"{CONFIG_DEST}: destination must be a regular file")
    atomic_write_repo(repo, CONFIG_DEST, content)
    print(
        json.dumps(
            {
                "ok": True,
                "action": "created",
                "config_path": str(repo / CONFIG_DEST),
                "config": config,
            },
            indent=2,
        )
    )
    return 0


# CodeQL analysis language per detected primary language (unsupported => "").
CODEQL_LANGUAGES = {
    "python": "python",
    "node": "javascript-typescript",
    "go": "go",
    "java": "java-kotlin",
    "ruby": "ruby",
    "csharp": "csharp",
}

DEPENDABOT_ECOSYSTEMS = {
    "python": "pip",
    "node": "npm",
    "go": "gomod",
    "rust": "cargo",
    "ruby": "bundler",
    "java": "gradle",
}


def dependabot_updates(languages: list[str]) -> str:
    """Build the dependabot `updates:` list body: GitHub Actions always,
    plus one ecosystem per configured language that has one."""
    ecosystems = ["github-actions"] + [
        DEPENDABOT_ECOSYSTEMS[lang] for lang in languages if lang in DEPENDABOT_ECOSYSTEMS
    ]
    blocks = [
        f'  - package-ecosystem: "{eco}"\n'
        '    directory: "/"\n'
        "    schedule:\n"
        '      interval: "weekly"'
        for eco in ecosystems
    ]
    return "\n".join(blocks)


def build_tokens(config: dict) -> dict[str, str]:
    languages = config.get("languages", [])
    toolchain = config.get("toolchain", {})
    manager = config.get("node_package_manager", {})
    manager_version = str(manager.get("version", "")) if isinstance(manager, dict) else ""
    primary = languages[0] if languages else "none"
    return {
        "CODEQL_LANGUAGE": CODEQL_LANGUAGES.get(primary, ""),
        "DEPENDABOT_UPDATES": dependabot_updates(languages),
        "PROJECT_NAME": config.get("project_name", ""),
        "KIT_VERSION": kit_version(),
        "DEFAULT_BRANCH": config.get("default_branch", "main"),
        "WORKFLOW_STYLE": config.get("workflow_style", "trunk"),
        "PROFILE": config.get("profile", "solo"),
        "TEST_CMD": config.get("commands", {}).get("test", ""),
        "LINT_CMD": config.get("commands", {}).get("lint", ""),
        "PRIMARY_LANGUAGE": languages[0] if languages else "none",
        "LANGUAGES_SUMMARY": ", ".join(languages) if languages else "none detected yet",
        "PYTHON_VERSION": str(toolchain.get("python", "3.12")),
        "NODE_VERSION": str(toolchain.get("node", "20")),
        "NODE_PACKAGE_MANAGER_VERSION": manager_version.split("+", 1)[0],
        "COREPACK_VERSION": COREPACK_VERSION,
        "GO_VERSION": str(toolchain.get("go", "1.22")),
        "OWNER_REPO": config.get("repo", {}).get("owner_repo", ""),
        "CODEOWNERS_DEFAULT": config.get("owners", {}).get("default", ""),
    }


def entry_applies(entry: dict, config: dict) -> bool:
    flag = entry.get("feature_flag")
    if flag and not config.get("features", {}).get(flag, False):
        return False
    languages = config.get("languages", [])
    primary = languages[0] if languages else "none"
    # Derived conditions: collaboration = anyone besides the owner is involved.
    collaboration = config.get("profile") == "team" or config.get("workflow_style") == "pr"
    has_owner = bool(config.get("owners", {}).get("default"))
    for key, expected in entry.get("when", {}).items():
        if key == "primary_language" and primary != expected:
            return False
        if key == "primary_language_in" and primary not in expected:
            return False
        if key == "primary_language_not_in" and primary in expected:
            return False
        if key == "node_package_manager" and node_manager_variant(config) != expected:
            return False
        if key == "workflow_style" and config.get("workflow_style") != expected:
            return False
        if key == "profile" and config.get("profile") != expected:
            return False
        if key == "collaboration" and collaboration != expected:
            return False
        if key == "has_owner" and has_owner != expected:
            return False
    return True


def render_template(src: Path, tokens: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute tokens; return (text, problems). Empty values count as missing
    because a blank in a command or branch name produces broken output."""
    text = src.read_text(encoding="utf-8")
    needed = set(TOKEN_RE.findall(text))
    problems = [f"token {name} has no value" for name in sorted(needed) if not tokens.get(name)]
    for name in needed:
        value = tokens.get(name)
        if value:
            text = text.replace("{{" + name + "}}", value)
    if not problems and TOKEN_RE.search(text):
        problems.append(f"unsubstituted tokens remain in {src.name}")
    return text, problems


def load_state(repo: Path) -> dict:
    state_path, status = repo_path_status(repo, STATE_DEST)
    if status == "missing":
        return {
            "kit_version": "",
            "completed_kit_version": "",
            "last_run_kit_version": "",
            "status": "not-installed",
            "pending_conflicts": [],
            "last_run_errors": [],
            "files": {},
        }
    if status != "file":
        raise RenderError(f"{STATE_DEST}: expected a regular file")
    state = load_json(state_path)
    if not isinstance(state, dict) or not isinstance(state.get("files"), dict):
        raise RenderError(f"{STATE_DEST} must contain an object with a files object")
    for path, record in state["files"].items():
        if (
            not isinstance(path, str)
            or not isinstance(record, dict)
            or not isinstance(record.get("sha256"), str)
        ):
            raise RenderError(f"{STATE_DEST} contains an invalid file record")
        mode = record.get("mode")
        if mode is not None and (
            not isinstance(mode, str) or re.fullmatch(r"0[0-7]{3}", mode) is None
        ):
            raise RenderError(f"{STATE_DEST} contains an invalid file mode record")
    return state


def observe_file(path: Path, status: str) -> dict:
    observation = {"status": status}
    if status == "file":
        observation["sha256"] = sha256_bytes(path.read_bytes())
        observation["mode"] = serialized_mode(path.lstat().st_mode)
    return observation


def drift_reason(path: Path, record: dict, expected_mode: int) -> str:
    if sha256_bytes(path.read_bytes()) != record["sha256"]:
        return "bytes were edited since shipshape last wrote the file"
    current_mode = serialized_mode(path.lstat().st_mode)
    recorded_mode = record.get("mode", serialized_mode(expected_mode))
    if current_mode != recorded_mode:
        return "mode was edited since shipshape last wrote the file"
    return ""


def config_plan(
    repo: Path,
    *,
    candidate_supplied: bool,
    target_bytes: bytes,
    version_changed: bool,
) -> tuple[dict, list[dict], dict]:
    config_path, status = repo_path_status(repo, CONFIG_DEST)
    observation = observe_file(config_path, status)
    if not candidate_supplied:
        if version_changed:
            return (
                {"path": CONFIG_DEST, "action": "update-version"},
                [],
                observation,
            )
        return {"path": CONFIG_DEST, "action": "unchanged"}, [], observation
    if status == "missing":
        return {"path": CONFIG_DEST, "action": "write"}, [], observation
    if status != "file":
        raise RenderError(f"{CONFIG_DEST}: expected a regular file")
    if config_path.read_bytes() == target_bytes:
        return (
            {"path": CONFIG_DEST, "action": "skip", "reason": "matches candidate"},
            [],
            observation,
        )
    conflict = {
        "path": CONFIG_DEST,
        "reason": "existing config differs from the reviewed candidate",
        "action": "write-config",
    }
    return {"path": CONFIG_DEST, "action": "conflict"}, [conflict], observation


def add_plan_error(plan: dict, path: str, problem: str) -> None:
    if any(item["path"] == path and problem in item["problems"] for item in plan["errors"]):
        return
    plan["errors"].append({"path": path, "problems": [problem]})
    plan["ok"] = False


def attach_plan_digest(plan: dict) -> None:
    actions = {}
    for group, action in (
        ("writes", "write"),
        ("regenerates", "regenerate"),
        ("removals", "remove"),
        ("skips", "skip"),
    ):
        for item in plan[group]:
            actions[item["path"]] = action
    for item in plan["conflicts"]:
        if item["path"] != CONFIG_DEST:
            actions[item["path"]] = "conflict"
    for item in plan["errors"]:
        actions[item["path"]] = "error"

    artifact_paths = set(actions) | set(plan["rendered"])
    artifacts = []
    for path in sorted(artifact_paths):
        metadata = plan["meta"].get(path, {})
        rendered = plan["rendered"].get(path)
        artifacts.append(
            {
                "path": path,
                "action": actions.get(path, "rendered"),
                "rendered_sha256": (
                    sha256_bytes(rendered.encode("utf-8")) if rendered is not None else None
                ),
                "target_mode": metadata.get("mode"),
                "observation": plan["observed"].get(path, {"status": "unobserved"}),
            }
        )

    payload = {
        "schema": PLAN_DIGEST_SCHEMA,
        "kit_version": plan["kit_version"],
        "config": {
            "action": plan["config"]["action"],
            "source_sha256": plan["config_source_sha256"],
            "target_sha256": sha256_bytes(plan["config_bytes"]),
            "observation": plan["config_observation"],
        },
        "artifacts": artifacts,
        "conflicts": plan["conflicts"],
        "errors": plan["errors"],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    plan["plan_digest"] = sha256_bytes(encoded)


def compute_plan(
    repo: Path,
    config: dict,
    source_bytes: bytes,
    candidate_supplied: bool = False,
) -> dict:
    current_version = kit_version()
    effective_config = copy.deepcopy(config)
    version_changed = effective_config.get("kit_version") != current_version
    effective_config["kit_version"] = current_version
    config_bytes = (
        serialized_config(effective_config).encode("utf-8") if version_changed else source_bytes
    )
    manifest = load_json(TEMPLATES_DIR / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("templates"), list):
        raise RenderError("bundled template manifest must contain a templates list")
    config_problems = validate_config(effective_config)
    if config_problems:
        return {
            "ok": False,
            "status": "invalid",
            "config": {"path": CONFIG_DEST, "action": "invalid"},
            "writes": [],
            "regenerates": [],
            "removals": [],
            "skips": [],
            "conflicts": [],
            "errors": [{"path": CONFIG_DEST, "problems": config_problems}],
            "rendered": {},
            "meta": {},
        }
    tokens = build_tokens(effective_config)
    state = load_state(repo)
    planned_config, config_conflicts, config_observation = config_plan(
        repo,
        candidate_supplied=candidate_supplied,
        target_bytes=config_bytes,
        version_changed=version_changed,
    )
    if version_changed:
        planned_config["from_version"] = config.get("kit_version", "")
        planned_config["to_version"] = current_version
    plan = {
        "ok": True,
        "status": "ready",
        "config": planned_config,
        "writes": [],
        "regenerates": [],
        "removals": [],
        "skips": [],
        "conflicts": config_conflicts,
        "errors": [],
        "rendered": {},
        "meta": {},
        "observed": {},
        "config_bytes": config_bytes,
        "config_source_sha256": sha256_bytes(source_bytes),
        "config_observation": config_observation,
        "kit_version": current_version,
    }
    for entry in manifest["templates"]:
        if not entry_applies(entry, effective_config):
            if not entry.get("remove_when_inapplicable"):
                continue
            dest = entry["dest"]
            try:
                dest_path, status = repo_path_status(repo, dest)
            except RenderError as error:
                add_plan_error(plan, dest, str(error))
                continue
            if status == "missing":
                continue
            plan["observed"][dest] = observe_file(dest_path, status)
            if status != "file":
                add_plan_error(plan, dest, f"{dest}: expected a regular file")
                continue
            recorded = state["files"].get(dest)
            if recorded is None:
                plan["conflicts"].append(
                    {
                        "path": dest,
                        "reason": (
                            "inactive managed path already exists but was not written by shipshape"
                        ),
                        "action": "remove",
                    }
                )
                continue
            expected_mode = generated_mode(entry.get("mode"))
            drift = drift_reason(dest_path, recorded, expected_mode)
            if drift:
                plan["conflicts"].append(
                    {
                        "path": dest,
                        "reason": f"inactive managed file {drift}",
                        "action": "remove",
                    }
                )
                continue
            plan["removals"].append({"path": dest, "pillar": entry["pillar"]})
            continue
        dest = entry["dest"]
        rendered, problems = render_template(TEMPLATES_DIR / entry["src"], tokens)
        if problems:
            plan["errors"].append({"path": dest, "problems": problems})
            plan["ok"] = False
            continue
        plan["rendered"][dest] = rendered
        # Applicable-entry metadata; keyed by dest so apply never picks up a
        # sibling variant's template name for the same destination.
        expected_mode = generated_mode(entry.get("mode"))
        plan["meta"][dest] = {
            "template": entry["src"],
            "mode": serialized_mode(expected_mode),
        }
        try:
            dest_path, status = repo_path_status(repo, dest)
        except RenderError as error:
            add_plan_error(plan, dest, str(error))
            continue
        plan["observed"][dest] = observe_file(dest_path, status)
        recorded = state["files"].get(dest)
        write_once = entry.get("overwrite_policy") == "write-once"

        if status == "missing":
            plan["writes"].append({"path": dest, "pillar": entry["pillar"]})
            continue
        if status != "file":
            add_plan_error(plan, dest, f"{dest}: expected a regular file")
            continue
        if write_once:
            plan["skips"].append({"path": dest, "reason": "write-once, already present"})
            continue
        if recorded is None:
            plan["conflicts"].append(
                {"path": dest, "reason": "file already exists and was not written by shipshape"}
            )
            continue
        drift = drift_reason(dest_path, recorded, expected_mode)
        if drift:
            plan["conflicts"].append({"path": dest, "reason": drift})
            continue
        target_mode_changed = recorded.get("mode") not in (None, serialized_mode(expected_mode))
        if rendered.encode("utf-8") == dest_path.read_bytes() and not target_mode_changed:
            plan["skips"].append({"path": dest, "reason": "up to date"})
        else:
            plan["regenerates"].append({"path": dest, "pillar": entry["pillar"]})
    if not plan["ok"]:
        plan["status"] = "invalid"
    elif plan["conflicts"]:
        plan["status"] = "conflicts"
    attach_plan_digest(plan)
    return plan


def public_plan(plan: dict) -> dict:
    private = {
        "config_bytes",
        "config_observation",
        "config_source_sha256",
        "meta",
        "observed",
        "rendered",
    }
    return {key: value for key, value in plan.items() if key not in private}


def cmd_plan(
    repo: Path,
    config: dict,
    source_bytes: bytes,
    candidate_supplied: bool,
) -> int:
    plan = compute_plan(repo, config, source_bytes, candidate_supplied)
    print(json.dumps(public_plan(plan), indent=2))
    return 0 if plan["ok"] else 1


def apply_overrides(config: dict, overrides: list[str]) -> dict:
    for override in overrides:
        if "=" not in override:
            raise RenderError(f"--set expects key=value, got: {override}")
        key, _, value = override.partition("=")
        set_dotted(config, key, value)
    problems = validate_config(config)
    if problems:
        raise RenderError("; ".join(problems))
    return config


def cmd_set_config(
    repo: Path,
    config: dict,
    overrides: list[str],
    output: Path | None,
) -> int:
    apply_overrides(config, overrides)
    content = serialized_config(config)
    if output is not None:
        output = Path(os.path.abspath(output))
        if is_within(output, repo):
            raise RenderError("proposed config output must be outside the target repository")
        atomic_write_external(output, content)
        print(
            json.dumps(
                {
                    "ok": True,
                    "action": "proposed",
                    "config_path": str(output),
                    "sha256": sha256_text(content),
                    "config": config,
                },
                indent=2,
            )
        )
        return 0
    atomic_write_repo(repo, CONFIG_DEST, content)
    print(json.dumps({"ok": True, "action": "updated", "config": config}, indent=2))
    return 0


def apply_result(
    *,
    ok: bool,
    status: str,
    config: dict,
    written: list[str],
    removed: list[str],
    forced: list[str],
    skipped: list[dict],
    conflicts: list[dict],
    errors: list[dict],
    versions: dict,
    state_written: bool,
    binding: dict,
) -> dict:
    return {
        "ok": ok,
        "status": status,
        "complete": status == "complete",
        "config": config,
        "written": written,
        "removed": removed,
        "forced": forced,
        "skipped": skipped,
        "conflicts_untouched": conflicts,
        "errors": errors,
        "versions": versions,
        "state_written": state_written,
        "binding": binding,
    }


def cmd_apply(
    repo: Path,
    config: dict,
    force: list[str],
    source_bytes: bytes,
    candidate_supplied: bool,
    expected_plan_digest: str | None,
) -> int:
    if candidate_supplied and expected_plan_digest is None:
        result = apply_result(
            ok=False,
            status="missing-plan-digest",
            config={"path": CONFIG_DEST, "action": "not-applied"},
            written=[],
            removed=[],
            forced=[],
            skipped=[],
            conflicts=[],
            errors=[
                {
                    "path": CONFIG_DEST,
                    "operation": "bind",
                    "error": "--config apply requires --expected-plan-digest from plan",
                }
            ],
            versions={},
            state_written=False,
            binding={"status": "required"},
        )
        print(json.dumps(result, indent=2))
        return 1

    plan = compute_plan(repo, config, source_bytes, candidate_supplied)
    if not plan["ok"]:
        print(json.dumps(public_plan(plan), indent=2))
        return 1
    actual_plan_digest = plan["plan_digest"]
    if expected_plan_digest is not None and expected_plan_digest != actual_plan_digest:
        result = apply_result(
            ok=False,
            status="stale-plan",
            config=plan["config"],
            written=[],
            removed=[],
            forced=[],
            skipped=[],
            conflicts=[],
            errors=[
                {
                    "path": "plan",
                    "operation": "bind",
                    "error": "reviewed plan digest does not match the current plan",
                }
            ],
            versions={"last_run_kit_version": plan["kit_version"]},
            state_written=False,
            binding={
                "status": "mismatch",
                "expected_plan_digest": expected_plan_digest,
                "actual_plan_digest": actual_plan_digest,
            },
        )
        print(json.dumps(result, indent=2))
        return 1
    binding = {
        "status": "matched" if expected_plan_digest is not None else "unbound",
        "actual_plan_digest": actual_plan_digest,
    }
    if expected_plan_digest is not None:
        binding["expected_plan_digest"] = expected_plan_digest
    state = load_state(repo)
    current_version = kit_version()
    completed_version = state.get("completed_kit_version", state.get("kit_version", ""))
    versions = {
        "completed_kit_version": completed_version,
        "last_run_kit_version": current_version,
    }

    to_write = [item["path"] for item in plan["writes"] + plan["regenerates"]]
    forced_writes, forced_removals, refused = [], [], []
    config_forced = False
    for conflict in plan["conflicts"]:
        if conflict["path"] in force:
            if conflict.get("action") == "write-config":
                config_forced = True
            elif conflict.get("action") == "remove":
                forced_removals.append(conflict["path"])
            else:
                forced_writes.append(conflict["path"])
        else:
            refused.append(conflict)
    recognized_force = set(forced_writes) | set(forced_removals)
    if config_forced:
        recognized_force.add(CONFIG_DEST)
    unknown_force = sorted(set(force) - recognized_force)
    if unknown_force:
        return fail(f"--force paths not in conflict list: {unknown_force}")

    config_conflict = next((item for item in refused if item.get("action") == "write-config"), None)
    if config_conflict is not None:
        result = apply_result(
            ok=True,
            status="blocked",
            config={"path": CONFIG_DEST, "action": "conflict"},
            written=[],
            removed=[],
            forced=[],
            skipped=plan["skips"],
            conflicts=plan["conflicts"],
            errors=[],
            versions=versions,
            state_written=False,
            binding=binding,
        )
        print(json.dumps(result, indent=2))
        return 0

    config_result = {"path": CONFIG_DEST, "action": "unchanged"}
    successful_forces = []
    if plan["config"]["action"] in ("write", "conflict", "update-version"):
        try:
            atomic_write_repo(repo, CONFIG_DEST, plan["config_bytes"])
        except (OSError, RenderError) as error:
            result = apply_result(
                ok=False,
                status="partial",
                config={"path": CONFIG_DEST, "action": "error"},
                written=[],
                removed=[],
                forced=[],
                skipped=plan["skips"],
                conflicts=refused,
                errors=[{"path": CONFIG_DEST, "operation": "write", "error": str(error)}],
                versions=versions,
                state_written=False,
                binding=binding,
            )
            print(json.dumps(result, indent=2))
            return 1
        config_result = {"path": CONFIG_DEST, "action": "written"}
        if config_forced:
            successful_forces.append(CONFIG_DEST)
    elif candidate_supplied:
        config_result = {"path": CONFIG_DEST, "action": "unchanged"}

    removed = []
    operation_errors = []
    for dest in [item["path"] for item in plan["removals"]] + forced_removals:
        try:
            dest_path, status = repo_path_status(repo, dest)
            if status == "file":
                dest_path.unlink()
            elif status != "missing":
                raise RenderError(f"{dest}: removal target must be a regular file")
        except (OSError, RenderError) as error:
            operation_errors.append({"path": dest, "operation": "remove", "error": str(error)})
            continue
        state["files"].pop(dest, None)
        removed.append(dest)
        if dest in forced_removals:
            successful_forces.append(dest)

    written = []
    for dest in to_write + forced_writes:
        content = plan["rendered"][dest]
        mode = plan["meta"][dest]["mode"]
        try:
            atomic_write_repo(repo, dest, content, int(mode, 8))
        except (OSError, RenderError) as error:
            operation_errors.append({"path": dest, "operation": "write", "error": str(error)})
            continue
        state["files"][dest] = {
            "template": plan["meta"][dest]["template"],
            "sha256": sha256_bytes(content.encode("utf-8")),
            "mode": mode,
            "kit_version": current_version,
        }
        written.append(dest)
        if dest in forced_writes:
            successful_forces.append(dest)

    conflict_paths = {item["path"] for item in plan["conflicts"]}
    for dest, metadata in plan["meta"].items():
        record = state["files"].get(dest)
        observation = plan["observed"].get(dest, {})
        if (
            record is not None
            and "mode" not in record
            and dest not in conflict_paths
            and observation.get("status") == "file"
            and observation.get("sha256") == record["sha256"]
            and observation.get("mode") == metadata["mode"]
        ):
            record["mode"] = metadata["mode"]

    remaining_conflicts = [item for item in refused if item.get("action") != "write-config"]
    status = "partial" if remaining_conflicts or operation_errors else "complete"
    state["last_run_kit_version"] = current_version
    state["status"] = status
    state["pending_conflicts"] = sorted(item["path"] for item in remaining_conflicts)
    state["last_run_errors"] = operation_errors
    if status == "complete":
        state["kit_version"] = current_version
        state["completed_kit_version"] = current_version
    else:
        state.setdefault("kit_version", completed_version)
        state["completed_kit_version"] = completed_version
    versions["completed_kit_version"] = state.get("completed_kit_version", "")

    state_written = False
    try:
        atomic_write_repo(repo, STATE_DEST, json.dumps(state, indent=2, sort_keys=True) + "\n")
        state_written = True
    except (OSError, RenderError) as error:
        operation_errors.append({"path": STATE_DEST, "operation": "write", "error": str(error)})
        status = "partial"
        versions["completed_kit_version"] = completed_version

    result = apply_result(
        ok=not operation_errors,
        status=status,
        config=config_result,
        written=written,
        removed=removed,
        forced=successful_forces,
        skipped=plan["skips"],
        conflicts=remaining_conflicts,
        errors=operation_errors,
        versions=versions,
        state_written=state_written,
        binding=binding,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="render.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-config")
    p_init.add_argument("repo")
    p_init.add_argument("--detect", required=True)
    p_init.add_argument("--set", action="append", default=[], dest="overrides")
    p_init.add_argument("--output")

    p_set = sub.add_parser("set-config")
    p_set.add_argument("repo")
    p_set.add_argument("--set", action="append", default=[], dest="overrides", required=True)
    p_set.add_argument("--output")

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("repo")
    p_plan.add_argument("--config")

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("repo")
    p_apply.add_argument("--force", action="append", default=[])
    p_apply.add_argument("--config")
    p_apply.add_argument("--expected-plan-digest")

    args = parser.parse_args()
    repo = Path(os.path.abspath(args.repo))
    if repo.is_symlink():
        return fail(f"repository path is a symlink: {repo}")
    if not repo.is_dir():
        return fail(f"not a directory: {repo}")

    try:
        if args.command == "init-config":
            detect_file = Path(args.detect)
            if detect_file.is_symlink() or not detect_file.is_file():
                raise RenderError(f"detect file not found or is a symlink: {detect_file}")
            output = Path(args.output) if args.output else None
            return cmd_init_config(repo, detect_file, args.overrides, output)

        candidate_supplied = bool(getattr(args, "config", None))
        if args.command in ("plan", "apply") and args.config:
            config, source_bytes = read_config_file(Path(args.config), "candidate config")
        else:
            config_path, config_status = repo_path_status(repo, CONFIG_DEST)
            if config_status != "file":
                raise RenderError(
                    f"no {CONFIG_DEST} in this repo - run init-config first "
                    "(or use shipshape-init in Codex or Claude Code)"
                )
            config, source_bytes = read_config_file(config_path, CONFIG_DEST)

        if args.command == "set-config":
            output = Path(args.output) if args.output else None
            return cmd_set_config(repo, config, args.overrides, output)

        if args.command == "plan":
            return cmd_plan(repo, config, source_bytes, candidate_supplied)
        return cmd_apply(
            repo,
            config,
            args.force,
            source_bytes,
            candidate_supplied,
            args.expected_plan_digest,
        )
    except (json.JSONDecodeError, OSError, PluginManifestError, RenderError, UnicodeError) as error:
        return fail(str(error))


if __name__ == "__main__":
    sys.exit(main())
