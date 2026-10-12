"""Offline tests for verification-cost aggregation."""

import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verification_cost  # noqa: E402

FIXED_NOW = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)


def test_happy_path_aggregates_runs_and_merged_changes(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def fake_fetch(path):
        if path.startswith("repos/"):
            return {
                "workflow_runs": [
                    {
                        "run_attempt": 1,
                        "run_started_at": "2026-08-10T10:00:00Z",
                        "updated_at": "2026-08-10T10:12:00Z",
                    },
                    {
                        "run_attempt": 1,
                        "run_started_at": "2026-08-11T10:00:00Z",
                        "updated_at": "2026-08-11T10:08:00Z",
                    },
                ]
            }
        return {"total_count": 2}

    monkeypatch.setattr(verification_cost, "fetch_json", fake_fetch)

    result = verification_cost.summarize("owner/repo", 30, now=FIXED_NOW)

    assert result == {
        "ok": True,
        "window_days": 30,
        "runs": 2,
        "attempts": 2,
        "retried_runs": 0,
        "minutes": 20,
        "merged_changes": 2,
        "minutes_per_merge": 10,
        "estimated_cost": {"status": "unknown", "currency": None, "amount": None},
        "warnings": [],
        "next_action": "Review verification minutes and retries for avoidable CI work.",
    }


def test_retried_run_counts_all_attempts(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def fake_fetch(path):
        if path.startswith("repos/"):
            return {
                "workflow_runs": [
                    {
                        "run_attempt": 3,
                        "run_started_at": "2026-08-12T10:00:00Z",
                        "updated_at": "2026-08-12T10:15:00Z",
                    }
                ]
            }
        return {"total_count": 0}

    monkeypatch.setattr(verification_cost, "fetch_json", fake_fetch)

    result = verification_cost.summarize("owner/repo", 30, now=FIXED_NOW)

    assert result["runs"] == 1
    assert result["attempts"] == 3
    assert result["retried_runs"] == 1
    assert result["minutes_per_merge"] is None


def test_missing_token_fails_without_network(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def unexpected_fetch(_path):
        raise AssertionError("network fetch should not run without a token")

    monkeypatch.setattr(verification_cost, "fetch_json", unexpected_fetch)

    result = verification_cost.summarize("owner/repo", 30, now=FIXED_NOW)

    assert result["ok"] is False
    assert result["estimated_cost"]["status"] == "unknown"
    assert "Set GITHUB_TOKEN" in result["next_action"]


def test_malformed_run_attempt_is_graceful(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def fake_fetch(path):
        if path.startswith("repos/"):
            return {
                "workflow_runs": [
                    {
                        "run_attempt": "not-a-number",
                        "run_started_at": "2026-08-12T10:00:00Z",
                        "updated_at": "2026-08-12T10:05:00Z",
                    }
                ]
            }
        return {"total_count": 1}

    monkeypatch.setattr(verification_cost, "fetch_json", fake_fetch)

    result = verification_cost.summarize("owner/repo", 30, now=FIXED_NOW)

    assert result["ok"] is True
    assert result["runs"] == 1
    assert result["attempts"] == 1
    assert result["retried_runs"] == 0
    assert result["warnings"]


def test_malformed_pagination_timestamp_is_graceful(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    malformed = {
        "run_attempt": 1,
        "run_started_at": "not-a-timestamp",
        "updated_at": "2026-08-12T10:05:00Z",
    }
    batch = [
        {
            "run_attempt": 1,
            "run_started_at": "2026-08-12T10:00:00Z",
            "updated_at": "2026-08-12T10:05:00Z",
        }
        for _ in range(99)
    ] + [malformed]

    def fake_fetch(path):
        if path.endswith("&page=1"):
            return {"workflow_runs": batch}
        if path.startswith("repos/"):
            return {"workflow_runs": []}
        return {"total_count": 1}

    monkeypatch.setattr(verification_cost, "fetch_json", fake_fetch)

    result = verification_cost.summarize("owner/repo", 30, now=FIXED_NOW)

    assert result["ok"] is True
    assert result["runs"] == 99
    assert result["warnings"]


def test_run_at_exact_cutoff_is_included(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    def fake_fetch(path):
        if path.startswith("repos/"):
            return {
                "workflow_runs": [
                    {
                        "run_attempt": 1,
                        "run_started_at": "2026-08-10T12:00:00Z",
                        "updated_at": "2026-08-10T12:01:00Z",
                    }
                ]
            }
        query = urllib.parse.unquote(path.split("q=", 1)[1].split("&", 1)[0])
        assert query == ("repo:owner/repo is:pr is:merged merged:>=2026-08-10T12:00:00+00:00")
        return {"total_count": 1}

    monkeypatch.setattr(verification_cost, "fetch_json", fake_fetch)

    result = verification_cost.summarize("owner/repo", 30, now=now)

    assert result["runs"] == 1
    assert result["minutes"] == 1


def test_run_just_outside_cutoff_is_excluded(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    def fake_fetch(path):
        if path.startswith("repos/"):
            return {
                "workflow_runs": [
                    {
                        "run_attempt": 1,
                        "run_started_at": "2026-08-10T11:59:59.999999Z",
                        "updated_at": "2026-08-10T12:01:00Z",
                    }
                ]
            }
        return {"total_count": 0}

    monkeypatch.setattr(verification_cost, "fetch_json", fake_fetch)

    result = verification_cost.summarize("owner/repo", 30, now=now)

    assert result["runs"] == 0
    assert result["minutes"] == 0
