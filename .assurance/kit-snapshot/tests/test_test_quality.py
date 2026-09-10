"""Public-interface tests for the assertion-free test sensor."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "test_quality.py"


def run_scan(repo: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "scan", str(repo), *extra],
        capture_output=True,
        check=False,
        text=True,
    )
    return completed, json.loads(completed.stdout)


def test_healthy_asserting_test_is_not_flagged(tmp_path):
    test_file = tmp_path / "tests" / "test_healthy.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_healthy():\n    assert 2 + 2 == 4\n", encoding="utf-8")

    completed, result = run_scan(tmp_path)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert result == {
        "ok": True,
        "mode": "assertion-scan",
        "language": "python",
        "tests_scanned": 1,
        "assertion_free": [],
        "unsupported": [],
        "warnings": [],
        "next_action": "No assertion-free tests are flagged.",
    }


def test_assertion_free_test_is_flagged(tmp_path):
    (tmp_path / "example_test.py").write_text(
        "def test_empty():\n    value = 42\n", encoding="utf-8"
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 1
    assert [item["test"] for item in result["assertion_free"]] == ["test_empty"]
    assert result["assertion_free"][0]["path"] == "example_test.py"
    assert result["assertion_free"][0]["line"] == 1
    assert "test_empty" in result["next_action"]


def test_fixture_decorated_test_functions_are_not_scanned(tmp_path):
    (tmp_path / "test_fixtures.py").write_text(
        """@fixture
def test_bare_fixture():
    pass

@fixture()
def test_called_fixture():
    pass

@pytest.fixture
async def test_qualified_fixture():
    pass

@pytest.fixture(scope="session")
async def test_qualified_called_fixture():
    pass
""",
        encoding="utf-8",
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 0
    assert result["assertion_free"] == []


def test_unittest_assert_is_recognized(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "unittest_example.py").write_text(
        """import unittest

class ExampleTests(unittest.TestCase):
    def test_unittest(self):
        self.assertEqual(1, 1)

""",
        encoding="utf-8",
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 1
    assert result["assertion_free"] == []


def test_pytest_raises_is_recognized(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "raises_example.py").write_text(
        """import pytest

def test_raises():
    with pytest.raises(ValueError):
        raise ValueError()
""",
        encoding="utf-8",
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 1
    assert result["assertion_free"] == []


def test_assertions_in_nested_functions_are_recognized(tmp_path):
    (tmp_path / "test_nested_assertions.py").write_text(
        """def test_nested_sync():
    def run_test():
        assert True

def test_nested_async():
    async def run_test():
        assert True
""",
        encoding="utf-8",
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 2
    assert result["assertion_free"] == []


def test_nested_function_without_assertions_does_not_satisfy_test(tmp_path):
    (tmp_path / "test_nested_without_assertions.py").write_text(
        """def test_nested_without_assertions():
    def run_test():
        return 42
""",
        encoding="utf-8",
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 1
    assert [item["test"] for item in result["assertion_free"]] == ["test_nested_without_assertions"]


def test_nested_tests_are_not_scanned_and_class_asserts_are_ignored(tmp_path):
    (tmp_path / "test_nested.py").write_text(
        """def test_outer():
    def test_nested():
        return 42
    class Local:
        assert True
    callback = lambda: 1
""",
        encoding="utf-8",
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 1
    assert [item["test"] for item in result["assertion_free"]] == ["test_outer"]


def test_mock_assertion_requires_a_traceable_mock_receiver(tmp_path):
    (tmp_path / "test_mocks.py").write_text(
        """from unittest.mock import Mock

def test_mock_call():
    mock = Mock()
    mock.method.assert_called_once()

def test_domain_call():
    domain.assert_called()
""",
        encoding="utf-8",
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 2
    assert [item["test"] for item in result["assertion_free"]] == ["test_domain_call"]


def test_unparseable_file_becomes_warning(tmp_path):
    (tmp_path / "test_broken.py").write_text("def test_broken(:\n", encoding="utf-8")

    completed, result = run_scan(tmp_path)

    assert completed.returncode == 0
    assert result["tests_scanned"] == 0
    assert result["warnings"][0]["path"] == "test_broken.py"


def test_skipped_test_is_scanned_but_not_flagged(tmp_path):
    (tmp_path / "test_skipped.py").write_text(
        "@pytest.mark.skip(reason='later')\ndef test_skipped():\n    pass\n", encoding="utf-8"
    )

    _, result = run_scan(tmp_path)

    assert result["tests_scanned"] == 1
    assert result["assertion_free"] == []
    assert result["warnings"][0]["test"] == "test_skipped"
    assert "Skipped test" in result["warnings"][0]["warning"]


def test_unsupported_language_is_reported_without_scanning(tmp_path):
    completed, result = run_scan(tmp_path, "--language", "go")

    assert completed.returncode == 0
    assert result["language"] == "go"
    assert result["unsupported"] == ["go"]
    assert result["tests_scanned"] == 0
