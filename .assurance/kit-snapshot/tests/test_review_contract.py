"""Review surfaces keep ADR 0007's verdict and escalation invariants aligned."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW_SURFACES = (
    ROOT / "skills" / "shipshape-review" / "references" / "finding-contract.md",
    ROOT / "templates" / "github" / "agents" / "reviewer.md.tmpl",
    ROOT / "templates" / "github" / "copilot-instructions.md.tmpl",
)
REQUIRED_RULES = (
    '"Looks safe to merge"',
    '"Needs attention first: <the one thing>"',
    '"Do not merge: <reason>"',
    'Any secret in the diff is an automatic "Do not merge".',
    'Red checks can never be called "looks safe" — ever.',
)


def test_review_surfaces_share_verdict_and_escalation_rules():
    for surface in REVIEW_SURFACES:
        content = surface.read_text(encoding="utf-8")
        for rule in REQUIRED_RULES:
            assert rule in content, f"{surface.relative_to(ROOT)} is missing: {rule}"
