from datetime import UTC, date, datetime

from market_analytics.contract_tracking import empty_tracker, update_contract_tracker
from market_analytics.daily_capture import build_current_contract_plan

NOW = datetime(2026, 8, 31, 22, tzinfo=UTC)
SESSION = date(2026, 8, 31)


def _state(symbol: str, *, volume: float, expiration: datetime, tradable: bool = True) -> tuple[str, dict]:
    return symbol, {
        "tradable": tradable,
        "has_quote": True,
        "session_volume": volume,
        "session_deals": int(volume),
        "expiration_time": int(expiration.timestamp()),
    }


def test_tracker_adds_active_contracts_with_stable_identity() -> None:
    states = dict(
        [
            _state("WINV26", volume=10_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
            _state("WDOU26", volume=20_000, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)

    plan, tracker = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )

    assert [item.selection.spec.symbol for item in plan] == ["WDOU26", "WINV26"]
    assert {row["state"] for row in tracker["contracts"]} == {"active"}
    assert {row["logical_id"] for row in tracker["contracts"]} == {
        "win_contract_winv26",
        "wdo_contract_wdou26",
    }


def test_tracker_keeps_previous_contract_until_expiration_after_liquidity_roll() -> None:
    first_states = dict(
        [
            _state("WINV26", volume=50_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
            _state("WINZ26", volume=10_000, expiration=datetime(2026, 12, 16, tzinfo=UTC)),
            _state("WDOU26", volume=60_000, expiration=datetime(2026, 9, 1, 23, tzinfo=UTC)),
            _state("WDOV26", volume=5_000, expiration=datetime(2026, 10, 1, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(first_states, now_utc=NOW, session_date=SESSION)
    _plan, tracker = update_contract_tracker(
        empty_tracker(), active, first_states, now_utc=NOW, session_date=SESSION
    )

    later = datetime(2026, 9, 1, 12, tzinfo=UTC)
    rolled_states = dict(
        [
            _state("WINV26", volume=5_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
            _state("WINZ26", volume=80_000, expiration=datetime(2026, 12, 16, tzinfo=UTC)),
            _state("WDOU26", volume=2_000, expiration=datetime(2026, 9, 1, 23, tzinfo=UTC)),
            _state("WDOV26", volume=90_000, expiration=datetime(2026, 10, 1, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(rolled_states, now_utc=later, session_date=date(2026, 9, 1))

    plan, tracker = update_contract_tracker(
        tracker,
        active,
        rolled_states,
        now_utc=later,
        session_date=date(2026, 9, 1),
    )

    assert {item.selection.spec.symbol for item in plan} == {
        "WINV26",
        "WINZ26",
        "WDOU26",
        "WDOV26",
    }
    states_by_symbol = {row["symbol"]: row["state"] for row in tracker["contracts"]}
    assert states_by_symbol == {
        "WINV26": "tracking_until_expiration",
        "WINZ26": "active",
        "WDOU26": "tracking_until_expiration",
        "WDOV26": "active",
    }


def test_tracker_captures_expiring_contract_one_last_time_on_its_expiration_date() -> None:
    states = dict(
        [
            _state("WDOU26", volume=20_000, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)
    _plan, tracker = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )

    later = datetime(2026, 9, 1, 14, tzinfo=UTC)
    new_states = dict(
        [
            _state("WDOU26", volume=0, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
            _state("WDOV26", volume=50_000, expiration=datetime(2026, 10, 1, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(new_states, now_utc=later, session_date=date(2026, 9, 1))

    plan, tracker = update_contract_tracker(
        tracker,
        active,
        new_states,
        now_utc=later,
        session_date=date(2026, 9, 1),
    )

    assert "WDOU26" in {item.selection.spec.symbol for item in plan}
    assert (
        {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"]
        == "tracking_until_expiration"
    )


def test_tracker_stops_expired_contract_after_its_final_session() -> None:
    states = dict(
        [
            _state("WDOU26", volume=1, expiration=datetime(2026, 8, 31, 22, 15, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)
    _plan, tracker = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )
    later = datetime(2026, 9, 1, 22, tzinfo=UTC)
    new_states = dict(
        [
            _state("WDOU26", volume=0, expiration=datetime(2026, 8, 31, 22, 15, tzinfo=UTC)),
            _state("WDOV26", volume=50_000, expiration=datetime(2026, 10, 1, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(new_states, now_utc=later, session_date=date(2026, 9, 1))

    plan, tracker = update_contract_tracker(
        tracker,
        active,
        new_states,
        now_utc=later,
        session_date=date(2026, 9, 1),
    )

    assert "WDOU26" not in {item.selection.spec.symbol for item in plan}
    assert {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"] == "expired"
