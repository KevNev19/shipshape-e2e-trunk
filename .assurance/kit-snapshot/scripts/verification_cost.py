#!/usr/bin/env python3
"""Summarize GitHub Actions verification volume for a recent window.

Durations are a billable-free approximation: each workflow run contributes
``updated_at - run_started_at``. GitHub's billing APIs require different
scopes, so this spike never guesses monetary cost and always reports it as
unknown. Uses only the standard library and emits one JSON object on stdout.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


def fetch_json(path: str):
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "shipshape"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _failure(days: int, action: str) -> dict:
    return {
        "ok": False,
        "window_days": days,
        "runs": 0,
        "attempts": 0,
        "retried_runs": 0,
        "minutes": 0,
        "merged_changes": 0,
        "minutes_per_merge": None,
        "estimated_cost": {"status": "unknown", "currency": None, "amount": None},
        "warnings": [],
        "next_action": action,
    }


def summarize(repo: str, days: int, now: datetime | None = None) -> dict:
    if not os.environ.get("GITHUB_TOKEN"):
        return _failure(days, "Set GITHUB_TOKEN with repository read access and try again.")

    current = datetime.now(timezone.utc) if now is None else now
    since = current.astimezone(timezone.utc).replace(microsecond=0) - timedelta(days=days)
    since_text = since.isoformat(timespec="seconds")
    runs = []
    warnings = []
    page = 1
    while True:
        data = fetch_json(f"repos/{repo}/actions/runs?per_page=100&page={page}")
        if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
            return _failure(
                days, "Check GITHUB_TOKEN access and the owner/repo name, then try again."
            )
        batch = data["workflow_runs"]
        for run in batch:
            try:
                if _timestamp(run["run_started_at"]) >= since:
                    runs.append(run)
            except (KeyError, TypeError, ValueError):
                warnings.append("A workflow run with an invalid start timestamp was excluded.")
                continue
        if len(batch) < 100:
            break
        oldest = batch[-1].get("run_started_at")
        if oldest:
            try:
                if _timestamp(oldest) < since:
                    break
            except (TypeError, ValueError):
                warnings.append("A malformed pagination timestamp was ignored.")
        page += 1

    query = urllib.parse.quote(f"repo:{repo} is:pr is:merged merged:>={since_text}")
    search = fetch_json(f"search/issues?q={query}&per_page=1")
    if not isinstance(search, dict) or not isinstance(search.get("total_count"), int):
        return _failure(
            days, "Check GITHUB_TOKEN search access and the owner/repo name, then try again."
        )

    attempt_values = []
    for run in runs:
        try:
            attempt_values.append(max(1, int(run.get("run_attempt", 1))))
        except (TypeError, ValueError):
            attempt_values.append(1)
            warnings.append("A malformed run_attempt was counted conservatively as one attempt.")
    attempts = sum(attempt_values)
    retried = sum(attempt > 1 for attempt in attempt_values)
    seconds = 0.0
    for run in runs:
        try:
            seconds += max(
                0.0,
                (_timestamp(run["updated_at"]) - _timestamp(run["run_started_at"])).total_seconds(),
            )
        except (KeyError, TypeError, ValueError):
            warnings.append("A workflow run with incomplete timestamps was excluded from minutes.")
    minutes = round(seconds / 60)
    merged = search["total_count"]
    return {
        "ok": True,
        "window_days": days,
        "runs": len(runs),
        "attempts": attempts,
        "retried_runs": retried,
        "minutes": minutes,
        "merged_changes": merged,
        "minutes_per_merge": round(minutes / merged, 2) if merged else None,
        "estimated_cost": {"status": "unknown", "currency": None, "amount": None},
        "warnings": warnings,
        "next_action": "Review verification minutes and retries for avoidable CI work.",
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) < 2 or args[0] != "summarize":
            raise ValueError
        repo = args[1]
        days = int(args[args.index("--days") + 1]) if "--days" in args else 30
        if days < 1:
            raise ValueError
        result = summarize(repo, days)
    except (IndexError, ValueError):
        result = _failure(30, "Use: verification_cost.py summarize <owner/repo> [--days N].")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
