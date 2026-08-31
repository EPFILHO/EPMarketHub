from __future__ import annotations

from datetime import UTC, date

import pytest

from market_analytics.history_discovery import (
    DiscoveryAsset,
    HistoryDiscoveryEngine,
    ProbeObservation,
    business_days_in_month,
    classify_observations,
    coarse_anchor_months,
    latest_completed_weekday,
    month_probe_dates,
    probe_window,
)


def _observation(*, status: str, session_date: date = date(2026, 8, 28)) -> ProbeObservation:
    start, end = probe_window(session_date)
    return ProbeObservation(
        logical_id="win",
        symbol="WIN$",
        session_date=session_date,
        start_utc=start,
        end_utc=end,
        phase="test",
        status=status,
        tick_count=10 if status == "available" else 0,
        elapsed_seconds=0.1,
        error_message="erro" if status == "inconclusive" else None,
    )


def test_latest_completed_weekday_skips_weekend() -> None:
    assert latest_completed_weekday(date(2026, 8, 30)) == date(2026, 8, 28)
    assert latest_completed_weekday(date(2026, 8, 31)) == date(2026, 8, 28)


def test_coarse_anchors_are_six_months_apart_and_include_floor() -> None:
    anchors = coarse_anchor_months(date(2026, 8, 28), date(2024, 1, 1))

    assert anchors == [
        date(2026, 8, 1),
        date(2026, 2, 1),
        date(2025, 8, 1),
        date(2025, 2, 1),
        date(2024, 8, 1),
        date(2024, 2, 1),
        date(2024, 1, 1),
    ]


def test_month_probe_dates_are_three_spread_wednesdays() -> None:
    candidates = month_probe_dates(date(2026, 8, 1))

    assert len(candidates) == 3
    assert all(candidate.weekday() == 2 for candidate in candidates)
    assert candidates == sorted(candidates)


def test_probe_window_is_five_minutes_and_utc() -> None:
    start, end = probe_window(date(2026, 8, 28))

    assert start.tzinfo == UTC
    assert end.tzinfo == UTC
    assert (end - start).total_seconds() == 300
    assert start.hour == 16  # 13:00 em São Paulo em agosto de 2026


def test_inconclusive_never_counts_as_proof_of_absence() -> None:
    assert classify_observations([_observation(status="empty")]) == "empty"
    assert classify_observations([_observation(status="empty"), _observation(status="inconclusive")]) == (
        "inconclusive"
    )
    assert classify_observations([_observation(status="inconclusive"), _observation(status="available")]) == (
        "available"
    )


def test_business_day_scan_excludes_weekends_and_future_days() -> None:
    days = business_days_in_month(date(2026, 8, 1), not_after=date(2026, 8, 10))

    assert days[0] == date(2026, 8, 3)
    assert days[-1] == date(2026, 8, 10)
    assert all(day.weekday() < 5 for day in days)


def test_engine_finds_earliest_observed_day_and_projects_without_persisting_ticks() -> None:
    cutoff = date(2022, 3, 10)
    asset = DiscoveryAsset(
        logical_id="win",
        symbol="WIN$",
        benchmark_bytes_per_session=1000,
        benchmark_seconds_per_session=2.0,
    )

    def fake_probe(asset, session_date, start_utc, end_utc, phase):
        available = session_date >= cutoff
        return ProbeObservation(
            logical_id=asset.logical_id,
            symbol=asset.symbol,
            session_date=session_date,
            start_utc=start_utc,
            end_utc=end_utc,
            phase=phase,
            status="available" if available else "empty",
            tick_count=25 if available else 0,
            elapsed_seconds=0.01,
        )

    result = HistoryDiscoveryEngine(
        assets=(asset,),
        reference_date=date(2026, 8, 28),
        floor_date=date(2020, 1, 1),
        probe=fake_probe,
    ).run()

    discovered = result["assets"][0]
    assert discovered["latest_observed_session"] == "2026-08-28"
    assert discovered["earliest_observed_session"] == "2022-03-10"
    assert discovered["confidence"] in {"medium", "high"}
    assert discovered["projection"]["estimated_sessions"] > 1000
    assert discovered["projection"]["estimated_bytes"] == (
        discovered["projection"]["estimated_sessions"] * 1000
    )
    assert all("ticks" not in observation for observation in result["observations"])


@pytest.mark.parametrize("step", [0, -1])
def test_coarse_anchor_step_must_advance(step: int) -> None:
    with pytest.raises(ValueError):
        coarse_anchor_months(date(2026, 8, 28), date(2020, 1, 1), step_months=step)
