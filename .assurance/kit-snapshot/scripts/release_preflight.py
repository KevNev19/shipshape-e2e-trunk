#!/usr/bin/env python3
"""Run the canonical standalone release-preflight template from the kit."""

import json
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parent.parent / "templates" / "scripts" / "release-preflight.py.tmpl"
)


def main() -> None:
    try:
        source = TEMPLATE.read_text(encoding="utf-8").replace("{{KIT_VERSION}}", "development")
        namespace = {"__name__": "__main__", "__file__": str(TEMPLATE)}
        exec(compile(source, str(TEMPLATE), "exec"), namespace)
    except SystemExit:
        raise
    except Exception:
        print(
            json.dumps(
                {
                    "ok": False,
                    "errors": [
                        {
                            "code": "entrypoint_error",
                            "message": "The bundled release preflight could not be loaded.",
                        }
                    ],
                    "next_action": "Repair the shipshape installation before releasing.",
                },
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
