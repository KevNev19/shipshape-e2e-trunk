#!/usr/bin/env python3
"""Bump pinned GitHub Action versions in template/workflow files (ADR 0002:
stdlib only). Dependabot cannot parse `.tmpl` files, so the kit keeps its own
templates current with this script — run weekly by the template-deps workflow
and available locally:

    python3 scripts/bump_actions.py <path>... [--apply]

Scans the given files/directories (*.tmpl, *.yml, *.yaml) for
`uses: owner/repo[/subpath]@vN`, queries the GitHub API for each repo's
latest release, and reports (or with --apply, rewrites) any pinned major
that is behind. Emits one JSON object on stdout. Uses GITHUB_TOKEN/GH_TOKEN
from the environment when present for rate limits.
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

USES_RE = re.compile(r"(uses:\s*)([\w.-]+/[\w.-]+)((?:/[\w./-]+)?)@v(\d+)")
SCAN_SUFFIXES = {".tmpl", ".yml", ".yaml"}


def _api(path: str):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "shipshape"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except OSError:
        return None


def latest_major(repo: str) -> int | None:
    """Latest published major for owner/repo, or None when unreachable.

    Tries the latest release first; falls back to scanning vN* git tags for
    repos (like github/codeql-action) whose release names aren't plain vN."""
    release = _api(f"repos/{repo}/releases/latest")
    if release:
        match = re.match(r"v(\d+)", release.get("tag_name", ""))
        if match:
            return int(match.group(1))
    refs = _api(f"repos/{repo}/git/matching-refs/tags/v")
    if refs:
        majors = [
            int(m.group(1))
            for ref in refs
            if (m := re.match(r"refs/tags/v(\d+)([.]|$)", ref.get("ref", "")))
        ]
        if majors:
            return max(majors)
    return None


def rewrite_text(text: str, majors: dict[str, int]) -> tuple[str, list[dict]]:
    """Rewrite pinned majors that are behind; return (new_text, changes)."""
    changes: list[dict] = []

    def substitute(match: re.Match) -> str:
        repo, current = match.group(2), int(match.group(4))
        newest = majors.get(repo)
        if newest and newest > current:
            changes.append(
                {"action": repo + match.group(3), "from": f"v{current}", "to": f"v{newest}"}
            )
            return f"{match.group(1)}{repo}{match.group(3)}@v{newest}"
        return match.group(0)

    return USES_RE.sub(substitute, text), changes


def collect_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(
                p for p in sorted(path.rglob("*")) if p.suffix in SCAN_SUFFIXES and p.is_file()
            )
        elif path.is_file():
            files.append(path)
    return files


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not paths:
        print(json.dumps({"ok": False, "error": "usage: bump_actions.py <path>... [--apply]"}))
        return 2

    files = collect_files(paths)
    repos = {
        match.group(2)
        for file in files
        for match in USES_RE.finditer(file.read_text(encoding="utf-8"))
    }
    majors: dict[str, int] = {}
    unreachable = []
    for repo in sorted(repos):
        newest = latest_major(repo)
        if newest is None:
            unreachable.append(repo)
        else:
            majors[repo] = newest

    bumps = []
    for file in files:
        text = file.read_text(encoding="utf-8")
        new_text, changes = rewrite_text(text, majors)
        for change in changes:
            bumps.append({"file": str(file), **change})
        if apply and changes:
            file.write_text(new_text, encoding="utf-8")

    print(
        json.dumps(
            {"ok": True, "applied": apply, "bumps": bumps, "unreachable": unreachable},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
