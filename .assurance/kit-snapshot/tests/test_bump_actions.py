"""Offline tests for the action-version bumper's rewrite logic (the network
lookup is exercised only by the real template-deps workflow)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from bump_actions import rewrite_text  # noqa: E402

MAJORS = {"actions/checkout": 7, "github/codeql-action": 4}


def test_bumps_older_major_and_reports():
    text, changes = rewrite_text("      - uses: actions/checkout@v4\n", MAJORS)
    assert "actions/checkout@v7" in text
    assert changes == [{"action": "actions/checkout", "from": "v4", "to": "v7"}]


def test_subpath_actions_keep_their_path():
    text, changes = rewrite_text("      - uses: github/codeql-action/init@v3\n", MAJORS)
    assert "github/codeql-action/init@v4" in text
    assert changes[0]["action"] == "github/codeql-action/init"


def test_current_and_unknown_pins_untouched():
    original = "      - uses: actions/checkout@v7\n      - uses: someone/unknown@v1\n"
    text, changes = rewrite_text(original, MAJORS)
    assert text == original
    assert changes == []


def test_never_downgrades():
    text, changes = rewrite_text("uses: actions/checkout@v9\n", MAJORS)
    assert "@v9" in text and not changes
