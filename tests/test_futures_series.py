from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from market_analytics.daily_capture import (
    build_current_contract_plan,
    latest_closed_b3_session,
    latest_closed_weekday,
)
from market_analytics.futures_series import (
    B3_CONTINUOUS_SERIES,
    get_continuous_spec,
    parse_b3_contract_symbol,
    select_current_b3_contract,
)

NOW = datetime(2026, 8, 31, 15, tzinfo=UTC)


def _state(
    *,
    volume=0,
    deals=0,
    quote=True,
    tradable=True,
    expiration=datetime(2026, 10, 15, tzinfo=UTC),
):
    return {
        "tradable": tradable,
        "has_quote": quote,
        "session_volume": volume,
        "session_deals": deals,
        "expiration_time": int(expiration.timestamp()),
    }


def test_registry_declares_all_four_adjustment_combinations_for_win_and_wdo() -> None:
    assert len(B3_CONTINUOUS_SERIES) == 8
    combinations = {
        (spec.instrument, spec.roll_rule, spec.adjustment_method)
        for spec in B3_CONTINUOUS_SERIES
    }
    assert combinations == {
        (root, roll, adjustment)
        for root in ("win", "wdo")
        for roll in ("liquidity", "expiration")
        for adjustment in ("proportional", "difference")
    }
    assert get_continuous_spec("WDO@D").logical_id == "wdo_cont_exp_diff"


@pytest.mark.parametrize(
    "symbol,root,month,year",
    [
        ("WINV26", "WIN", 10, 2026),
        ("WDOU26", "WDO", 9, 2026),
        ("winz29", "WIN", 12, 2029),
    ],
)
def test_parse_b3_contract_symbol(symbol, root, month, year) -> None:
    parsed = parse_b3_contract_symbol(symbol)
    assert parsed is not None
    assert (parsed.root, parsed.month, parsed.year) == (root, month, year)


def test_parser_rejects_continuous_and_unrelated_symbols() -> None:
    assert parse_b3_contract_symbol("WIN$") is None
    assert parse_b3_contract_symbol("WDO@D") is None
    assert parse_b3_contract_symbol("EURUSD") is None


def test_selection_prefers_observed_liquidity_instead_of_nearest_month() -> None:
    states = {
        "WINV26": _state(volume=1_000, deals=100),
        "WINZ26": _state(volume=8_000, deals=700, expiration=datetime(2026, 12, 16, tzinfo=UTC)),
    }

    selected = select_current_b3_contract("WIN", states, now_utc=NOW)

    assert selected is not None
    assert selected.spec.symbol == "WINZ26"
    assert selected.selection_reason == "highest_session_liquidity"
    assert selected.spec.adjustment_method == "none"
    assert selected.provenance()["contract_month"] == "2026-12-01"


def test_selection_ignores_expired_disabled_and_other_root_contracts() -> None:
    states = {
        "WDOQ26": _state(volume=99_999, expiration=datetime(2026, 8, 1, tzinfo=UTC)),
        "WDOU26": _state(volume=4_000, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
        "WDOV26": _state(volume=30_000, tradable=False),
        "WINV26": _state(volume=50_000),
    }

    selected = select_current_b3_contract("WDO", states, now_utc=NOW)

    assert selected is not None
    assert selected.spec.symbol == "WDOU26"
    assert selected.spec.logical_id == "wdo_contract_wdou26"


def test_selection_falls_back_to_nearest_quoted_contract_without_liquidity_metrics() -> None:
    states = {
        "WINV26": _state(quote=True),
        "WINZ26": _state(quote=True, expiration=datetime(2026, 12, 16, tzinfo=UTC)),
    }

    selected = select_current_b3_contract("WIN", states, now_utc=NOW)

    assert selected is not None
    assert selected.spec.symbol == "WINV26"
    assert selected.selection_reason == "nearest_tradable_with_quote"


def test_daily_plan_captures_only_individual_contracts_for_last_closed_weekday() -> None:
    states = {
        "WINV26": _state(volume=10_000),
        "WDOU26": _state(volume=20_000, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
        "WIN$": {"tradable": False},
        "WDO$D": {"tradable": False},
    }

    plan = build_current_contract_plan(
        states,
        now_utc=NOW,
        session_date=date(2026, 8, 28),
    )

    assert latest_closed_weekday(date(2026, 8, 31)) == date(2026, 8, 28)
    assert [item.selection.spec.symbol for item in plan] == ["WINV26", "WDOU26"]
    assert all(item.session_date == date(2026, 8, 28) for item in plan)
    assert all(item.selection.spec.series_kind == "individual_contract" for item in plan)


def test_daily_cutoff_captures_same_day_after_close_and_previous_day_before_close() -> None:
    zone = ZoneInfo("America/Sao_Paulo")
    assert latest_closed_b3_session(datetime(2026, 8, 31, 18, 59, tzinfo=zone)) == date(2026, 8, 28)
    assert latest_closed_b3_session(datetime(2026, 8, 31, 19, 0, tzinfo=zone)) == date(2026, 8, 31)
    assert latest_closed_b3_session(datetime(2026, 9, 5, 12, 0, tzinfo=zone)) == date(2026, 9, 4)
