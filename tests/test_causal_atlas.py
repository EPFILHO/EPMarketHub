"""Testes do núcleo causal do Atlas (DEV-008A) com fixtures sintéticas.

Porta os invariantes já auditados no Fusion Quant
(`fusion_quant/tests/test_market_atlas.py`, DEV-011D.1/D.2) para o núcleo
local do EP Market Hub. Nenhum dado real, Parquet, MT5 ou caminho
`D:\\EPData` é usado aqui — tudo é gerado em memória.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from market_analytics.atlas_contract import (
    FUSION_QUANT_V1_CHECKPOINT_ROWS,
    FUSION_QUANT_V1_EVENT_ROWS,
    FUSION_QUANT_V1_OUTSIDE_CORE_ROWS,
    FUSION_QUANT_V1_SESSION_ROWS,
    to_fusion_quant_v1,
    to_hub_contract,
)
from market_analytics.bars import Bar
from market_analytics.causal_atlas import (
    AtlasBar,
    AtlasManifest,
    CausalAtlasError,
    build_causal_atlas,
)

SESSION_START = "12:00"
SESSION_END = "17:00"  # 300 minutos, uma barra por minuto


def _session_bars(
    session_date: str,
    start_hhmm: str,
    minutes: int,
    base_price: float,
    *,
    quality: str = "exchange",
    contract_id: str | None = None,
    roll_session: bool = False,
    source_id: str = "SYN",
) -> list[AtlasBar]:
    """Sessão sintética com um pequeno "V": sobe, desce, fecha perto da
    abertura, uma barra por minuto, sem furos."""

    hour, minute = int(start_hhmm[:2]), int(start_hhmm[3:5])
    start = datetime.fromisoformat(f"{session_date}T00:00:00+00:00") + timedelta(hours=hour, minutes=minute)
    rows: list[AtlasBar] = []
    price = base_price
    for index in range(minutes):
        drift = 0.5 if index < minutes / 2 else -0.5
        price = price + drift
        timestamp = start + timedelta(minutes=index)
        bar = Bar(
            source_id=source_id,
            symbol="SYN$",
            timeframe="M1",
            timestamp=timestamp,
            open=price - 0.25,
            high=price + 1.0,
            low=price - 1.0,
            close=price,
            volume=100.0 + index,
            volume_quality=quality,
        )
        rows.append(AtlasBar(bar=bar, contract_id=contract_id, roll_session=roll_session))
    return rows


def _standard_sessions() -> list[AtlasBar]:
    dates = ["2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06"]
    bars: list[AtlasBar] = []
    for index, session_date in enumerate(dates):
        bars.extend(_session_bars(session_date, "12:00", 300, 100_000.0 + index * 50))
    return bars


def _sessions(count: int, *, start_day: int = 2, minutes: int = 300, quality: str = "exchange") -> list[AtlasBar]:
    bars: list[AtlasBar] = []
    for index in range(count):
        bars.extend(
            _session_bars(
                f"2026-02-{start_day + index:02d}", "12:00", minutes, 100_000.0 + index * 50, quality=quality
            )
        )
    return bars


def _manifest(**overrides) -> AtlasManifest:
    defaults = dict(
        logical_id="syn",
        symbol="SYN$",
        series_kind="continuous_proportional",
        source_symbol="SYN$",
        adjustment_method="proportional",
        expected_session_start=SESSION_START,
        expected_session_end=SESSION_END,
    )
    defaults.update(overrides)
    return AtlasManifest(**defaults)


# --- AtlasManifest: validação de configuração ------------------------------


def test_manifest_rejects_empty_checkpoint_minutes():
    with pytest.raises(CausalAtlasError):
        _manifest(checkpoint_minutes=())


def test_manifest_rejects_duplicate_checkpoint_minutes():
    with pytest.raises(CausalAtlasError):
        _manifest(checkpoint_minutes=(15, 15, 30))


def test_manifest_rejects_unsorted_checkpoint_minutes():
    with pytest.raises(CausalAtlasError):
        _manifest(checkpoint_minutes=(30, 15))


def test_manifest_rejects_implausible_pct_threshold():
    with pytest.raises(CausalAtlasError):
        _manifest(pct_event_thresholds=(0.25,))  # 25%, não 0,25%


def test_manifest_rejects_min_coverage_ratio_out_of_range():
    with pytest.raises(CausalAtlasError):
        _manifest(min_coverage_ratio=0.0)
    with pytest.raises(CausalAtlasError):
        _manifest(min_coverage_ratio=1.5)


def test_manifest_rejects_bad_expected_session_window():
    with pytest.raises(CausalAtlasError):
        _manifest(expected_session_start="17:00", expected_session_end="12:00")


def test_manifest_rejects_unknown_series_kind():
    with pytest.raises(CausalAtlasError):
        _manifest(series_kind="bogus")


def test_manifest_rejects_empty_identity_fields():
    with pytest.raises(CausalAtlasError):
        _manifest(symbol="")


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("checkpoint_minutes", (15.5,)),
        ("opening_range_minutes", ("15",)),
        ("atr_percentile_windows", (20.0,)),
        ("event_outcome_horizons_minutes", (True,)),
        ("atr_lookback_sessions", 14.5),
        ("coverage_tolerance_minutes", 5.5),
        ("min_coverage_ratio", True),
        ("range_expansion_percentile", False),
    ],
)
def test_manifest_rejects_wrong_numeric_types(field_name, value):
    with pytest.raises(CausalAtlasError):
        _manifest(**{field_name: value})


# --- tabela de sessão --------------------------------------------------


def test_build_causal_atlas_produces_one_row_per_session():
    result = build_causal_atlas(_standard_sessions(), _manifest())
    assert len(result.session_rows) == 5
    assert result.bar_count == 5 * 300


def test_first_session_has_no_previous_context():
    result = build_causal_atlas(_standard_sessions(), _manifest())
    first = result.session_rows[0]
    assert first["is_first_session"] is True
    assert first["previous_close"] is None
    assert first["atr14_previous_session"] is None
    assert first["gap_points"] is None
    assert first["gap_quality"] == "unknown"
    assert first["previous_session_return_close_open_pct"] is None
    assert first["available_at_utc"] == first["expected_session_close_time_utc"]


def test_previous_session_features_are_causal_lags():
    bars = _standard_sessions()[: 2 * 300]
    rows = build_causal_atlas(bars, _manifest()).session_rows
    assert rows[1]["previous_session_return_close_open_pct"] == rows[0]["label_return_close_open_pct"]
    assert rows[1]["previous_session_close_position_in_range"] == rows[0]["label_close_position_in_range"]
    assert rows[1]["previous_session_efficiency_ratio"] == rows[0]["label_efficiency_ratio"]
    assert rows[1]["previous_session_day_type"] == rows[0]["label_day_type"]


def test_partial_session_flagged_but_kept():
    bars = _standard_sessions() + _session_bars("2026-02-09", "14:00", 40, 100_300.0)
    result = build_causal_atlas(bars, _manifest())
    partial_row = next(row for row in result.session_rows if row["session_date"] == "2026-02-09")
    assert partial_row["eligible"] is False
    assert "sessao_parcial" in partial_row["eligibility_reason"]
    assert any(row["session_date"] == "2026-02-09" for row in result.checkpoint_rows)
    assert any(row["session_date"] == "2026-02-09" for row in result.event_rows)


def test_missing_bars_inside_session_are_flagged():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0)
    bars = bars[:150] + bars[151:]  # remove um minuto no meio (furo)
    result = build_causal_atlas(bars, _manifest())
    row = result.session_rows[0]
    assert row["eligible"] is False
    assert "barras_m1_faltantes" in row["eligibility_reason"]


def test_median_of_whole_series_never_decides_eligibility():
    # Todas as sessões atípicas (começam às 13:00, fora do calendário
    # declarado 12:00-17:00): a mediana bateria com elas, mas o calendário
    # declarado precisa reprová-las de qualquer forma.
    bars: list[AtlasBar] = []
    for i in range(5):
        bars.extend(_session_bars(f"2026-02-{2 + i:02d}", "13:00", 240, 100_000.0))
    result = build_causal_atlas(bars, _manifest())
    assert all(not row["eligible"] for row in result.session_rows)


def test_default_coverage_tolerance_accepts_opening_auction_delay():
    delayed = _session_bars("2026-02-02", "12:04", 296, 100_000.0)
    row = build_causal_atlas(delayed, _manifest()).session_rows[0]
    assert row["eligible"] is True


def test_mixed_volume_quality_session_is_ineligible_and_never_blended():
    bars = _standard_sessions()
    mixed = _session_bars("2026-02-09", "12:00", 300, 100_300.0)
    for index in range(10):
        item = mixed[index]
        mixed[index] = AtlasBar(
            bar=Bar(
                source_id=item.bar.source_id, symbol=item.bar.symbol, timeframe=item.bar.timeframe,
                timestamp=item.bar.timestamp, open=item.bar.open, high=item.bar.high, low=item.bar.low,
                close=item.bar.close, volume=item.bar.volume, volume_quality="tick_proxy",
            )
        )
    result = build_causal_atlas(bars + mixed, _manifest())
    mixed_row = next(row for row in result.session_rows if row["session_date"] == "2026-02-09")
    assert mixed_row["eligible"] is False
    assert "volume_quality_mista" in mixed_row["eligibility_reason"]
    assert mixed_row["volume_quality"] == "mixed"
    checkpoints = [row for row in result.checkpoint_rows if row["session_date"] == "2026-02-09"]
    for row in checkpoints:
        if row["available"]:
            assert row["volume_relative_so_far"] is None


def test_roll_gap_is_not_clean():
    bars = _standard_sessions()[: 2 * 300]
    rolled = [
        AtlasBar(bar=item.bar, contract_id=item.contract_id, roll_session=True) for item in bars[300:]
    ]
    bars = bars[:300] + rolled
    result = build_causal_atlas(bars, _manifest())
    assert result.session_rows[1]["gap_quality"] == "roll_affected"
    assert result.session_rows[1]["eligible"] is True
    assert result.session_rows[1]["eligible_for_aggregates"] is False
    roll_events = [row for row in result.event_rows if row["session_date"] == "2026-02-03"]
    assert roll_events
    assert all(not row["evaluable"] for row in roll_events)
    assert {row["not_evaluable_reason"] for row in roll_events} == {"sessao_de_rolagem_excluida"}


def test_gap_with_partial_context_is_not_clean_or_continuous():
    sessions = [
        _session_bars("2026-02-02", "12:00", 40, 100_000.0),
        _session_bars("2026-02-03", "12:00", 300, 100_050.0),
    ]
    bars = sessions[0] + sessions[1]
    result = build_causal_atlas(bars, _manifest())
    assert result.session_rows[0]["eligible"] is False
    assert result.session_rows[1]["gap_quality"] == "partial_context"


def test_individual_contract_can_change_between_sessions_and_marks_roll():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0, contract_id="WINV26")
    bars += _session_bars("2026-02-03", "12:00", 300, 100_050.0, contract_id="WINZ26")
    manifest = _manifest(series_kind="individual_contract", source_symbol="WIN", adjustment_method="none")
    result = build_causal_atlas(bars, manifest)
    assert result.session_rows[0]["contract_id"] == "WINV26"
    assert result.session_rows[1]["contract_id"] == "WINZ26"
    assert result.session_rows[1]["gap_quality"] == "roll_affected"


def test_individual_contract_must_be_constant_within_a_session():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0, contract_id="WINV26")
    bars[150:] = [AtlasBar(bar=item.bar, contract_id="WINZ26") for item in bars[150:]]
    manifest = _manifest(series_kind="individual_contract", source_symbol="WIN", adjustment_method="none")
    with pytest.raises(CausalAtlasError):
        build_causal_atlas(bars, manifest)


def test_individual_contract_same_contract_is_clean():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0, contract_id="WINV26")
    bars += _session_bars("2026-02-03", "12:00", 300, 100_050.0, contract_id="WINV26")
    manifest = _manifest(series_kind="individual_contract", source_symbol="WIN", adjustment_method="none")
    result = build_causal_atlas(bars, manifest)
    assert result.session_rows[1]["gap_quality"] == "clean"


def test_individual_contract_requires_contract_id_somewhere():
    bars = _standard_sessions()[: 2 * 300]
    manifest = _manifest(series_kind="individual_contract", source_symbol="WIN", adjustment_method="none")
    with pytest.raises(CausalAtlasError, match="contract_id"):
        build_causal_atlas(bars, manifest)


def test_source_transition_is_flagged_and_never_silently_merged():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0, source_id="feed_a")
    bars += _session_bars("2026-02-03", "12:00", 300, 100_050.0, source_id="feed_b")
    result = build_causal_atlas(bars, _manifest())
    assert result.session_rows[0]["source_transition"] is False
    assert result.session_rows[1]["source_transition"] is True


def test_source_id_must_be_constant_within_a_session():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0, source_id="feed_a")
    bars[150:] = [
        AtlasBar(bar=Bar(
            source_id="feed_b", symbol=item.bar.symbol, timeframe=item.bar.timeframe,
            timestamp=item.bar.timestamp, open=item.bar.open, high=item.bar.high, low=item.bar.low,
            close=item.bar.close, volume=item.bar.volume, volume_quality=item.bar.volume_quality,
        ))
        for item in bars[150:]
    ]
    with pytest.raises(CausalAtlasError):
        build_causal_atlas(bars, _manifest())


def test_atr14_requires_14_full_eligible_prior_sessions():
    bars = _sessions(15)
    rows = build_causal_atlas(bars, _manifest()).session_rows
    assert rows[12]["atr14_previous_session"] is None
    assert rows[12]["atr14_previous_session_window_reason"] is not None
    assert rows[13]["atr14_previous_session"] is None
    assert rows[14]["atr14_previous_session"] is not None
    assert rows[14]["atr14_previous_session_window_reason"] is None


def test_ineligible_sessions_never_enter_atr_history():
    sessions_bars = _sessions(15)
    # substitui a 8a sessão (índice 7) por uma sessão atípica -> parcial
    replaced = _session_bars("2026-02-09", "14:00", 40, 100_000.0)
    bars = sessions_bars[: 7 * 300] + replaced + sessions_bars[8 * 300 :]
    rows = build_causal_atlas(bars, _manifest()).session_rows
    assert rows[7]["eligible"] is False
    assert rows[14]["atr14_previous_session"] is None


# --- invariância de prefixo (teste de aceite #1 do DEV-008) -----------------


def test_future_bars_never_change_earlier_checkpoints_or_events():
    sessions = _standard_sessions()
    result_a = build_causal_atlas(sessions, _manifest())

    mutated_tail = []
    for item in sessions[4 * 300 :]:
        b = item.bar
        mutated_tail.append(
            AtlasBar(bar=Bar(
                source_id=b.source_id, symbol=b.symbol, timeframe=b.timeframe, timestamp=b.timestamp,
                open=b.open + 500.0, high=b.high + 500.0, low=b.low + 500.0, close=b.close + 500.0,
                volume=b.volume, volume_quality=b.volume_quality,
            ))
        )
    mutated = sessions[: 4 * 300] + mutated_tail
    result_b = build_causal_atlas(mutated, _manifest())

    prior_dates = [row["session_date"] for row in result_a.session_rows[:-1]]
    rows_a = {row["session_date"]: row for row in result_a.session_rows if row["session_date"] in prior_dates}
    rows_b = {row["session_date"]: row for row in result_b.session_rows if row["session_date"] in prior_dates}
    assert rows_a == rows_b

    checkpoints_a = [row for row in result_a.checkpoint_rows if row["session_date"] in prior_dates]
    checkpoints_b = [row for row in result_b.checkpoint_rows if row["session_date"] in prior_dates]
    assert checkpoints_a == checkpoints_b

    events_a = [row for row in result_a.event_rows if row["session_date"] in prior_dates]
    events_b = [row for row in result_b.event_rows if row["session_date"] in prior_dates]
    assert events_a == events_b


def test_reopening_without_new_data_is_fully_deterministic():
    sessions = _standard_sessions()
    result_a = build_causal_atlas(sessions, _manifest())
    result_b = build_causal_atlas(sessions, _manifest())
    assert result_a.session_rows == result_b.session_rows
    assert result_a.checkpoint_rows == result_b.checkpoint_rows
    assert result_a.event_rows == result_b.event_rows
    assert result_a.params_sha256 == result_b.params_sha256
    assert result_a.bars_sha256 == result_b.bars_sha256


def test_percentiles_use_only_prior_eligible_sessions():
    result = build_causal_atlas(_standard_sessions(), _manifest())
    first_checkpoints = [row for row in result.checkpoint_rows if row["session_date"] == "2026-02-02"]
    for row in first_checkpoints:
        assert row["range_so_far_percentile_60"] is None
        assert row["realized_volatility_so_far_percentile_60"] is None


def test_realized_volatility_percentile_uses_only_prior_sessions_same_checkpoint():
    sessions = _standard_sessions()
    result = build_causal_atlas(sessions, _manifest())
    by_date: dict[str, dict[int, dict]] = {}
    for row in result.checkpoint_rows:
        by_date.setdefault(row["session_date"], {})[row["checkpoint_minutes"]] = row
    later_row = by_date["2026-02-06"][15]
    assert later_row["realized_volatility_so_far_percentile_60"] is not None

    mutated_tail = []
    for item in sessions[4 * 300 :]:
        b = item.bar
        mutated_tail.append(
            AtlasBar(bar=Bar(
                source_id=b.source_id, symbol=b.symbol, timeframe=b.timeframe, timestamp=b.timestamp,
                open=b.open, high=b.high + 5_000.0, low=b.low, close=b.close + 5_000.0,
                volume=b.volume, volume_quality=b.volume_quality,
            ))
        )
    mutated = sessions[: 4 * 300] + mutated_tail
    mutated_result = build_causal_atlas(mutated, _manifest())
    earlier_original = by_date["2026-02-05"][15]["realized_volatility_so_far_percentile_60"]
    earlier_mutated = {
        row["checkpoint_minutes"]: row for row in mutated_result.checkpoint_rows if row["session_date"] == "2026-02-05"
    }[15]["realized_volatility_so_far_percentile_60"]
    assert earlier_original == earlier_mutated


def test_missing_quality_volume_is_not_coerced_to_zero_at_checkpoint():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0)
    bars = [
        AtlasBar(bar=Bar(
            source_id=item.bar.source_id, symbol=item.bar.symbol, timeframe=item.bar.timeframe,
            timestamp=item.bar.timestamp, open=item.bar.open, high=item.bar.high, low=item.bar.low,
            close=item.bar.close, volume=None, volume_quality="missing",
        ))
        for item in bars
    ]
    result = build_causal_atlas(bars, _manifest())
    available = [row for row in result.checkpoint_rows if row["available"]]
    assert available
    assert all(row["volume_so_far"] is None for row in available)


def test_checkpoint_volume_relative_only_for_exchange_quality():
    bars = _sessions(3, quality="tick_proxy")
    result = build_causal_atlas(bars, _manifest())
    for row in result.checkpoint_rows:
        if row["available"]:
            assert row["volume_relative_so_far"] is None
            assert row["volume_relative_so_far_null_reason"] == "volume_quality_nao_exchange"
            assert row["volume_so_far"] is not None


def test_checkpoint_volume_relative_is_ratio_to_causal_median():
    bars = _sessions(4)
    result = build_causal_atlas(bars, _manifest())
    checkpoints_60 = sorted(
        (row for row in result.checkpoint_rows if row["checkpoint_minutes"] == 60),
        key=lambda row: row["session_date"],
    )
    assert checkpoints_60[0]["volume_relative_so_far"] is None
    assert checkpoints_60[0]["volume_relative_so_far_null_reason"] == "sem_historico_causal_de_volume_exchange"
    assert checkpoints_60[1]["volume_relative_so_far"] is not None
    expected_ratio = checkpoints_60[1]["volume_so_far"] / checkpoints_60[0]["volume_so_far"]
    assert checkpoints_60[1]["volume_relative_so_far"] == pytest.approx(expected_ratio, rel=1e-6)


def test_opening_range_whipsaw_is_never_hidden():
    start = datetime.fromisoformat("2026-02-02T12:00:00+00:00")
    bars: list[AtlasBar] = []
    for index in range(60):
        ts = start + timedelta(minutes=index)
        if index < 15:
            open_, high, low, close = 100_000.0, 100_002.0, 99_998.0, 100_000.0
        elif index == 20:
            open_, high, low, close = 100_000.0, 100_050.0, 100_000.0, 100_040.0
        elif index == 40:
            open_, high, low, close = 100_020.0, 100_020.0, 99_900.0, 99_910.0
        else:
            open_, high, low, close = 100_010.0, 100_015.0, 100_005.0, 100_010.0
        bar = Bar(
            source_id="SYN", symbol="SYN$", timeframe="M1", timestamp=ts,
            open=open_, high=high, low=low, close=close, volume=100.0, volume_quality="exchange",
        )
        bars.append(AtlasBar(bar=bar))
    manifest = _manifest(
        expected_session_start="12:00", expected_session_end="13:00",
        coverage_tolerance_minutes=5, min_coverage_ratio=0.1,
    )
    result = build_causal_atlas(bars, manifest)
    checkpoint_60 = next(row for row in result.checkpoint_rows if row["checkpoint_minutes"] == 60)
    assert checkpoint_60["opening_range_15_state"] in {"whipsaw_up", "whipsaw_down", "whipsaw_returned"}


# --- eventos: disponibilidade causal no fechamento da barra ----------------


def test_event_first_cross_time_equals_bar_close_availability():
    result = build_causal_atlas(_standard_sessions(), _manifest(pct_event_thresholds=(0.0005,)))
    triggered = [
        row for row in result.event_rows
        if row["triggered"] and row["event_type"] == "pct_threshold" and row["direction"] == "up"
    ]
    assert triggered
    for row in triggered:
        bar_time = datetime.fromisoformat(row["event_bar_timestamp_utc"])
        assert datetime.fromisoformat(row["first_cross_time_utc"]) == bar_time + timedelta(minutes=1)
        assert datetime.fromisoformat(row["available_at_utc"]) == bar_time + timedelta(minutes=1)


def test_event_outcomes_never_use_the_crossing_bar_itself():
    start = datetime.fromisoformat("2026-02-02T12:00:00+00:00")
    bars: list[AtlasBar] = []
    for index in range(30):
        ts = start + timedelta(minutes=index)
        if index == 5:
            open_, high, low, close = 100_000.0, 100_400.0, 99_500.0, 100_100.0
        else:
            open_, high, low, close = 100_100.0, 100_120.0, 100_080.0, 100_100.0
        bar = Bar(
            source_id="SYN", symbol="SYN$", timeframe="M1", timestamp=ts,
            open=open_, high=high, low=low, close=close, volume=100.0, volume_quality="exchange",
        )
        bars.append(AtlasBar(bar=bar))
    manifest = _manifest(
        expected_session_start="12:00", expected_session_end="12:30",
        coverage_tolerance_minutes=5, min_coverage_ratio=0.1,
    )
    result = build_causal_atlas(bars, manifest)
    event = next(
        row for row in result.event_rows
        if row["event_type"] == "pct_threshold" and row["event_id"] == "pct_0.25" and row["direction"] == "up"
    )
    assert event["triggered"] is True
    assert event["label_mae_pct"] < 1.0


def test_event_horizon_return_is_null_without_a_completed_bar():
    start = datetime.fromisoformat("2026-02-02T12:00:00+00:00")
    bars = []
    for index in range(10):
        ts = start + timedelta(minutes=index)
        if index == 8:
            open_, high, low, close = 100_000.0, 100_260.0, 100_000.0, 100_200.0
        else:
            open_, high, low, close = 100_000.0, 100_010.0, 99_990.0, 100_000.0
        bar = Bar(
            source_id="SYN", symbol="SYN$", timeframe="M1", timestamp=ts,
            open=open_, high=high, low=low, close=close, volume=100.0, volume_quality="exchange",
        )
        bars.append(AtlasBar(bar=bar))
    manifest = _manifest(
        expected_session_start="12:00", expected_session_end="12:10",
        coverage_tolerance_minutes=5, min_coverage_ratio=0.1,
    )
    result = build_causal_atlas(bars, manifest)
    event = next(
        row for row in result.event_rows
        if row["event_type"] == "pct_threshold" and row["event_id"] == "pct_0.25" and row["direction"] == "up"
    )
    assert event["triggered"] is True
    assert event["label_return_60m_pct"] is None
    assert event["label_return_60m_null_reason"] == "sessao_encerrou_antes_do_horizonte"


def test_ineligible_session_events_are_not_evaluable():
    bars = _standard_sessions() + _session_bars("2026-02-09", "14:00", 40, 100_300.0)
    result = build_causal_atlas(bars, _manifest())
    partial_events = [row for row in result.event_rows if row["session_date"] == "2026-02-09"]
    assert partial_events
    for row in partial_events:
        assert row["evaluable"] is False
        assert row["triggered"] is False
        assert row["not_evaluable_reason"] == "sessao_inelegivel"


def test_events_are_unique_first_crossings():
    result = build_causal_atlas(_standard_sessions(), _manifest())
    by_key: dict[tuple, list] = {}
    for row in result.event_rows:
        key = (row["session_date"], row["event_type"], row["event_id"], row["direction"])
        by_key.setdefault(key, []).append(row)
    for rows in by_key.values():
        assert len(rows) == 1


def test_range_expansion_has_two_directions_with_shared_denominator():
    sessions = _standard_sessions()
    result = build_causal_atlas(sessions, _manifest())
    range_rows = [row for row in result.event_rows if row["event_type"] == "range_expansion"]
    assert len(range_rows) == 5 * 2
    assert {row["direction"] for row in range_rows} == {"up", "down"}
    for session_date in {row["session_date"] for row in range_rows}:
        same_session = [row for row in range_rows if row["session_date"] == session_date]
        assert sum(bool(row["triggered"]) for row in same_session) <= 1


def test_label_columns_are_separated_by_prefix():
    result = build_causal_atlas(_standard_sessions(), _manifest())
    session_row = result.session_rows[2]
    label_keys = [key for key in session_row if key.startswith("label_")]
    assert "label_true_range" in label_keys
    assert "label_day_type" in label_keys
    assert "gap_points" not in label_keys


def test_partial_session_labels_are_explicitly_ineligible():
    # A fase 008A materializa sessões encerradas. Uma entrada truncada não
    # pode parecer completa: os rótulos descrevem apenas o trecho observado e
    # viajam com elegibilidade falsa. Snapshots intradiários sem label_* são
    # responsabilidade declarada da 008B.2/008C.
    partial_bars = _session_bars("2026-02-02", "12:00", 61, 100_000.0)
    manifest = _manifest(coverage_tolerance_minutes=0, min_coverage_ratio=0.01)
    result = build_causal_atlas(partial_bars, manifest)
    row = result.session_rows[0]
    assert row["eligible"] is False
    assert row["label_session_close"] is not None
    assert "sessao_parcial" in row["eligibility_reason"]


# --- barras fora do núcleo causal -------------------------------------------


def test_bars_outside_declared_core_are_audited_not_dropped_or_counted_as_gap():
    core = _session_bars("2026-02-02", "12:00", 300, 100_000.0)
    closing_auction_ts = datetime.fromisoformat("2026-02-02T17:06:00+00:00")
    outside_bar = AtlasBar(
        bar=Bar(
            source_id="SYN", symbol="SYN$", timeframe="M1", timestamp=closing_auction_ts,
            open=100_010.0, high=100_012.0, low=100_008.0, close=100_010.0,
            volume=50.0, volume_quality="exchange",
        )
    )
    result = build_causal_atlas(core + [outside_bar], _manifest())
    row = result.session_rows[0]
    assert row["eligible"] is True  # a barra fora do núcleo não vira lacuna interna
    assert len(result.outside_core_rows) == 1
    assert result.outside_core_rows[0]["outside_core_session_reason"] == "after_core_session_close"
    assert any(issue["issue_type"].startswith("outside_core_session_") for issue in result.quality_issue_rows)


# --- proveniência/determinismo ----------------------------------------------


def test_bars_sha256_changes_when_input_changes():
    sessions = _standard_sessions()
    result_a = build_causal_atlas(sessions, _manifest())
    mutated = list(sessions)
    b = mutated[0].bar
    mutated[0] = AtlasBar(bar=Bar(
        source_id=b.source_id, symbol=b.symbol, timeframe=b.timeframe, timestamp=b.timestamp,
        open=b.open + 1.0, high=b.high + 1.0, low=b.low, close=b.close, volume=b.volume,
        volume_quality=b.volume_quality,
    ))
    result_b = build_causal_atlas(mutated, _manifest())
    assert result_a.bars_sha256 != result_b.bars_sha256


def test_params_sha256_changes_when_manifest_changes():
    sessions = _standard_sessions()
    result_a = build_causal_atlas(sessions, _manifest())
    result_b = build_causal_atlas(sessions, _manifest(checkpoint_minutes=(10, 20, 30, 60, 120)))
    assert result_a.params_sha256 != result_b.params_sha256


def test_rejects_bars_out_of_order():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0)
    bars = list(reversed(bars))
    with pytest.raises(CausalAtlasError):
        build_causal_atlas(bars, _manifest())


def test_rejects_wrong_symbol():
    bars = _session_bars("2026-02-02", "12:00", 300, 100_000.0)
    with pytest.raises(CausalAtlasError):
        build_causal_atlas(bars, _manifest(symbol="OTHER$"))


def test_rejects_non_utc_timestamp():
    minus_three = timedelta(hours=-3)
    from datetime import timezone as tz_module

    bar = Bar(
        source_id="SYN", symbol="SYN$", timeframe="M1",
        timestamp=datetime(2026, 2, 2, 12, 0, tzinfo=tz_module(minus_three)),
        open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0, volume_quality="exchange",
    )
    with pytest.raises(CausalAtlasError):
        AtlasBar(bar=bar)


def test_rejects_non_m1_timeframe():
    bar = Bar(
        source_id="SYN", symbol="SYN$", timeframe="M5", timestamp=datetime(2026, 2, 2, 12, 0, tzinfo=UTC),
        open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0, volume_quality="exchange",
    )
    with pytest.raises(CausalAtlasError):
        AtlasBar(bar=bar)


# --- contrato: compatibilidade explícita com o v1 do Fusion Quant ----------


def test_fusion_quant_v1_adapter_matches_field_by_field_on_golden_fixture():
    result = build_causal_atlas(_standard_sessions(), _manifest())
    adapted = to_fusion_quant_v1(result)

    assert len(adapted["session_rows"]) == len(result.session_rows)
    assert len(adapted["checkpoint_rows"]) == len(result.checkpoint_rows)
    assert len(adapted["event_rows"]) == len(result.event_rows)

    for original, adapted_row in zip(result.session_rows, adapted["session_rows"], strict=True):
        assert adapted_row["schema"] == FUSION_QUANT_V1_SESSION_ROWS
        for key, value in original.items():
            assert adapted_row[key] == value

    for original, adapted_row in zip(result.checkpoint_rows, adapted["checkpoint_rows"], strict=True):
        assert adapted_row["schema"] == FUSION_QUANT_V1_CHECKPOINT_ROWS
        for key, value in original.items():
            assert adapted_row[key] == value

    for original, adapted_row in zip(result.event_rows, adapted["event_rows"], strict=True):
        assert adapted_row["schema"] == FUSION_QUANT_V1_EVENT_ROWS
        for key, value in original.items():
            assert adapted_row[key] == value

    for original, adapted_row in zip(result.outside_core_rows, adapted["outside_core_rows"], strict=True):
        assert adapted_row["schema"] == FUSION_QUANT_V1_OUTSIDE_CORE_ROWS
        for key, value in original.items():
            assert adapted_row[key] == value


def test_fusion_quant_v1_adapter_fails_closed_on_non_default_ladder():
    manifest = _manifest(checkpoint_minutes=(10, 20, 30, 60, 120))
    result = build_causal_atlas(_standard_sessions(), manifest)
    with pytest.raises(CausalAtlasError):
        to_fusion_quant_v1(result)


def test_hub_contract_has_versioned_rows_hashes_and_strict_json():
    result = build_causal_atlas(_standard_sessions(), _manifest())
    payload = to_hub_contract(result)

    assert payload["schema"] == "ep_market_hub.atlas.result.v1"
    assert payload["params_sha256"] == result.params_sha256
    assert payload["bars_sha256"] == result.bars_sha256
    assert payload["session_rows"][0]["schema"] == "ep_market_hub.atlas.session_rows.v1"
    assert payload["checkpoint_rows"][0]["schema"] == "ep_market_hub.atlas.checkpoint_rows.v1"
    assert payload["event_rows"][0]["schema"] == "ep_market_hub.atlas.event_rows.v1"
    json.dumps(payload, ensure_ascii=False, allow_nan=False)


def test_hub_contract_accepts_manifest_driven_ladders():
    manifest = _manifest(
        checkpoint_minutes=(10, 45),
        opening_range_minutes=(10,),
        atr_percentile_windows=(3,),
        event_outcome_horizons_minutes=(5, 20),
    )
    payload = to_hub_contract(build_causal_atlas(_standard_sessions(), manifest))

    assert "atr_percentile_3" in payload["session_rows"][0]
    assert "atr_percentile_20" not in payload["session_rows"][0]
    assert "opening_range_10_state" in payload["checkpoint_rows"][0]
    triggered = next(row for row in payload["event_rows"] if row["triggered"])
    assert "label_return_5m_pct" in triggered
    assert "label_return_15m_pct" not in triggered
