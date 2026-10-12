"""Frozen-fixture detection tests.

The JSON files in tests/expected/ are the ground truth for what detect.py
must report for each fixture repo. Regenerate one only when the CORRECT
answer genuinely changes — never to make a real regression pass:

    python3 scripts/detect.py tests/fixtures/<name> --no-network \
        > tests/expected/<name>.json
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parent.parent
DETECT = KIT_ROOT / "scripts" / "detect.py"
FIXTURES = KIT_ROOT / "tests" / "fixtures"
EXPECTED = KIT_ROOT / "tests" / "expected"

FIXTURE_NAMES = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())


def run_detect(path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(DETECT), str(path), "--no-network"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_matches_frozen_output(name):
    actual = run_detect(FIXTURES / name)
    expected = json.loads((EXPECTED / f"{name}.json").read_text())
    assert actual == expected, (
        f"detect.py output for {name} drifted from tests/expected/{name}.json"
    )


def test_every_fixture_has_frozen_output():
    missing = [n for n in FIXTURE_NAMES if not (EXPECTED / f"{n}.json").is_file()]
    assert not missing, f"fixtures without frozen expected output: {missing}"


def test_errors_are_json_not_tracebacks():
    proc = subprocess.run(
        [sys.executable, str(DETECT), str(FIXTURES / "does-not-exist")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "error" in json.loads(proc.stdout)
