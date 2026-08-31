from datetime import date

import pytest

from market_analytics.pilot_backfill import (
    PilotAsset,
    build_pilot_queue,
    retryable_failure,
    summarize_results,
    weekday_sessions,
)

ASSETS = (PilotAsset("win", "WIN$"), PilotAsset("wdo", "WDO$"))


def test_august_2026_pilot_has_twenty_weekdays_and_forty_sessions() -> None:
    days = weekday_sessions(date(2026, 8, 1), date(2026, 8, 28))
    queue = build_pilot_queue(ASSETS, date(2026, 8, 1), date(2026, 8, 28))

    assert len(days) == 20
    assert len(queue) == 40
    assert queue[0].session_date == date(2026, 8, 28)
    assert queue[0].asset.symbol == "WIN$"
    assert queue[1].asset.symbol == "WDO$"
    assert queue[-1].session_date == date(2026, 8, 3)


def test_weekday_range_rejects_reversed_dates() -> None:
    with pytest.raises(ValueError):
        weekday_sessions(date(2026, 8, 28), date(2026, 8, 1))


def test_only_mt5_source_failure_is_retried_automatically() -> None:
    assert retryable_failure("mt5_error") is True
    assert retryable_failure("disk_error") is False
    assert retryable_failure("integrity_error") is False
    assert retryable_failure(None) is False


def test_summary_separates_completed_empty_reused_and_failed() -> None:
    results = [
        {
            "logical_id": "win",
            "symbol": "WIN$",
            "state": "completed",
            "reused": True,
            "tick_count": 100,
            "file_size_bytes": 50,
        },
        {
            "logical_id": "win",
            "symbol": "WIN$",
            "state": "empty",
            "reused": False,
            "tick_count": 0,
            "file_size_bytes": 10,
        },
        {
            "logical_id": "wdo",
            "symbol": "WDO$",
            "state": "failed",
            "reused": False,
            "tick_count": 0,
            "file_size_bytes": 0,
        },
    ]

    summary = summarize_results(results)

    assert summary["totals"] == {
        "sessions": 3,
        "completed": 1,
        "empty": 1,
        "reused": 1,
        "failed": 1,
        "tick_count": 100,
        "file_size_bytes": 60,
    }
