import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = KIT_ROOT / "scripts"
FIXTURES = KIT_ROOT / "tests" / "fixtures"


@pytest.fixture
def hermetic_doctor_path(tmp_path: Path, monkeypatch) -> Path:
    """Keep doctor subprocesses away from host-installed CLI tools."""
    bin_dir = tmp_path / "doctor-bin"
    bin_dir.mkdir(exist_ok=True)
    for command in ("git", "python3"):
        executable = shutil.which(command)
        assert executable is not None
        (bin_dir / command).symlink_to(executable)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("gh") is None
    return bin_dir


def prepend_to_path(monkeypatch, directory: Path) -> None:
    """Put a test shim ahead of the curated subprocess PATH."""
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def run_script(script: str, *args) -> tuple[int, dict]:
    """Run a shipshape script and parse its JSON stdout."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *[str(a) for a in args]],
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc.returncode, payload


def init_repo(fixture: str, tmp_path: Path, overrides: list[str] | None = None) -> Path:
    """Copy a fixture to a temp dir and run detect + init-config on it."""
    repo = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, repo)
    detect_proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "detect.py"), str(repo), "--no-network"],
        capture_output=True,
        text=True,
        check=True,
    )
    detect_file = tmp_path / f"{fixture}-detect.json"
    detect_file.write_text(detect_proc.stdout)
    args = ["init-config", repo, "--detect", detect_file]
    for override in overrides or []:
        args += ["--set", override]
    code, payload = run_script("render.py", *args)
    assert code == 0, payload
    return repo
