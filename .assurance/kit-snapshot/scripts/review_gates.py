#!/usr/bin/env python3
"""Load the canonical rendered review-gates implementation for kit tests."""

from pathlib import Path

_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "templates" / "scripts" / "review-gates.py.tmpl"
)
_SOURCE = _TEMPLATE.read_text(encoding="utf-8").replace("{{KIT_VERSION}}", "development")
exec(compile(_SOURCE, str(_TEMPLATE), "exec"), globals())
