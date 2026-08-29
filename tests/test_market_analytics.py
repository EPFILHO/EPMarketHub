from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from market_analytics import (
    FEATURE_SCHEMA_VERSION,
    Bar,
    FeatureConfig,
    FeatureRow,
    compute_feature_rows,
    load_feature_artifact,
    save_feature_artifact,
)

SMALL_CONFIG = FeatureConfig(atr_period=3, volatility_window=3, trend_window=3, volume_window=3)


def make_bar(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float | None = 1000.0,
    volume_quality: str = "exchange",
    source_id: str = "SRC1",
    symbol: str = "TEST",
    timeframe: str = "M1",
) -> Bar:
    return Bar(
        source_id=source_id,
        symbol=symbol,
        timeframe=timeframe,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        volume_quality=volume_quality,
    )


def simple_series() -> list[Bar]:
    # OHLC simples, crescente, para valores conhecidos de TR/ATR.
    data = [
        (10.0, 11.0, 9.0, 10.5),
        (10.5, 12.0, 10.0, 11.5),
        (11.5, 13.0, 11.0, 12.5),
        (12.5, 12.8, 11.8, 12.0),
        (12.0, 12.2, 10.5, 10.8),
        (10.8, 11.5, 10.2, 11.2),
        (11.2, 11.9, 10.9, 11.6),
    ]
    return [make_bar(i, *row) for i, row in enumerate(data)]


# --- valores conhecidos -----------------------------------------------


def test_true_range_first_bar_uses_high_low_only():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    assert rows[0].true_range == pytest.approx(2.0)  # high-low = 11-9


def test_true_range_uses_previous_close():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    # bar 1: high=12, low=10, prev_close=10.5
    # TR = max(12-10, |12-10.5|, |10-10.5|) = max(2, 1.5, 0.5) = 2
    assert rows[1].true_range == pytest.approx(2.0)


def test_atr_is_exact_window_average_of_true_ranges():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)  # atr_period=3
    true_ranges = [row.true_range for row in rows]
    expected = sum(true_ranges[0:3]) / 3
    assert rows[2].atr == pytest.approx(expected)


def test_log_return_known_value():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    expected = math.log(11.5 / 10.5)
    assert rows[1].log_return == pytest.approx(expected)


def test_first_bar_has_no_log_return():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    assert rows[0].log_return is None


def test_close_position_known_value():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    # bar 0: close=10.5, low=9, high=11 -> (10.5-9)/(11-9) = 0.75
    assert rows[0].close_position == pytest.approx(0.75)


# --- warm-up: None até janela completa -----------------------------------


def test_atr_is_none_until_period_complete():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)  # atr_period=3
    assert rows[0].atr is None
    assert rows[1].atr is None
    assert rows[2].atr is not None


def test_realized_volatility_is_none_until_window_complete():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)  # volatility_window=3
    # retornos válidos só existem a partir do índice 1; precisa de 3 deles.
    assert rows[0].realized_volatility is None
    assert rows[1].realized_volatility is None
    assert rows[2].realized_volatility is None
    assert rows[3].realized_volatility is not None


def test_efficiency_and_trend_are_none_until_window_plus_one_closes():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)  # trend_window=3 -> precisa de 4 closes
    assert rows[0].efficiency_ratio is None
    assert rows[1].efficiency_ratio is None
    assert rows[2].efficiency_ratio is None
    assert rows[3].efficiency_ratio is not None
    assert rows[3].trend_strength is not None


def test_volume_relative_none_until_exact_window_of_previous_bars():
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)  # volume_window=3
    assert rows[0].volume_relative is None
    assert rows[1].volume_relative is None
    assert rows[2].volume_relative is None
    assert rows[3].volume_relative is not None
    expected = bars[3].volume / (sum(b.volume for b in bars[0:3]) / 3)
    assert rows[3].volume_relative == pytest.approx(expected)


# --- invariância de prefixo (sem leakage), linha inteira serializada -----


def test_prefix_invariance_no_leakage_full_row():
    bars = simple_series()
    full = compute_feature_rows(bars, SMALL_CONFIG)
    partial = compute_feature_rows(bars[:4], SMALL_CONFIG)
    for i in range(4):
        assert full[i].to_dict() == partial[i].to_dict()


# --- invariância de escala para métricas normalizadas -------------------


def test_scale_invariance_of_normalized_metrics():
    bars = simple_series()
    scale = 37.5
    scaled_bars = [
        make_bar(
            i,
            bar.open * scale,
            bar.high * scale,
            bar.low * scale,
            bar.close * scale,
            volume=bar.volume,
            volume_quality=bar.volume_quality,
        )
        for i, bar in enumerate(bars)
    ]
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    scaled_rows = compute_feature_rows(scaled_bars, SMALL_CONFIG)
    for row, scaled_row in zip(rows, scaled_rows, strict=True):
        assert row.log_return == pytest.approx(scaled_row.log_return)
        assert row.atr_normalized == pytest.approx(scaled_row.atr_normalized)
        assert row.normalized_range == pytest.approx(scaled_row.normalized_range)
        assert row.close_position == pytest.approx(scaled_row.close_position)
        assert row.volume_relative == (
            pytest.approx(scaled_row.volume_relative)
            if row.volume_relative is not None
            else scaled_row.volume_relative
        )
        if row.efficiency_ratio is not None:
            assert row.efficiency_ratio == pytest.approx(scaled_row.efficiency_ratio)
        if row.trend_strength is not None:
            assert row.trend_strength == pytest.approx(scaled_row.trend_strength)
        # A escala DEVE alterar as métricas em unidade de preço bruta.
        if row.true_range is not None and row.true_range > 0:
            assert scaled_row.true_range == pytest.approx(row.true_range * scale)


# --- volume: missing vs exchange vs tick_proxy sem mistura --------------


def test_volume_missing_never_becomes_zero_or_ratio():
    bars = [
        make_bar(0, 10, 11, 9, 10.5, volume=None, volume_quality="missing"),
        make_bar(1, 10.5, 12, 10, 11.5, volume=None, volume_quality="missing"),
        make_bar(2, 11.5, 13, 11, 12.5, volume=None, volume_quality="missing"),
    ]
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    for row in rows:
        assert row.volume_relative is None
        assert row.volume_quality == "missing"


def test_volume_relative_requires_full_window_of_same_quality():
    # window=3; bar índice 3 teria histórico [exchange, tick_proxy, tick_proxy]
    # nas 3 barras imediatamente anteriores -> qualidade mista -> None.
    bars = [
        make_bar(0, 10, 11, 9, 10.5, volume=100.0, volume_quality="exchange"),
        make_bar(1, 10.5, 12, 10, 11.5, volume=200.0, volume_quality="tick_proxy"),
        make_bar(2, 11.5, 13, 11, 12.5, volume=300.0, volume_quality="tick_proxy"),
        make_bar(3, 12.5, 12.8, 11.8, 12.0, volume=400.0, volume_quality="tick_proxy"),
    ]
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    assert rows[3].volume_relative is None


def test_volume_relative_pure_matching_window_is_computed():
    bars = [
        make_bar(0, 10, 11, 9, 10.5, volume=100.0, volume_quality="tick_proxy"),
        make_bar(1, 10.5, 12, 10, 11.5, volume=200.0, volume_quality="tick_proxy"),
        make_bar(2, 11.5, 13, 11, 12.5, volume=300.0, volume_quality="tick_proxy"),
        make_bar(3, 12.5, 12.8, 11.8, 12.0, volume=400.0, volume_quality="tick_proxy"),
    ]
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    expected = 400.0 / ((100.0 + 200.0 + 300.0) / 3)
    assert rows[3].volume_relative == pytest.approx(expected)


def test_volume_relative_missing_bar_inside_window_invalidates_it():
    bars = [
        make_bar(0, 10, 11, 9, 10.5, volume=100.0, volume_quality="exchange"),
        make_bar(1, 10.5, 12, 10, 11.5, volume=None, volume_quality="missing"),
        make_bar(2, 11.5, 13, 11, 12.5, volume=300.0, volume_quality="exchange"),
        make_bar(3, 12.5, 12.8, 11.8, 12.0, volume=400.0, volume_quality="exchange"),
    ]
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    assert rows[3].volume_relative is None


# --- validação de Bar ----------------------------------------------------


def test_bar_rejects_missing_quality_with_nonnull_volume():
    with pytest.raises(ValueError):
        make_bar(0, 10, 11, 9, 10.5, volume=5.0, volume_quality="missing")


def test_bar_rejects_exchange_quality_with_null_volume():
    with pytest.raises(ValueError):
        make_bar(0, 10, 11, 9, 10.5, volume=None, volume_quality="exchange")


def test_bar_rejects_negative_volume():
    with pytest.raises(ValueError):
        make_bar(0, 10, 11, 9, 10.5, volume=-1.0, volume_quality="exchange")


def test_bar_rejects_non_finite_ohlc():
    with pytest.raises(ValueError):
        make_bar(0, float("nan"), 11, 9, 10.5)
    with pytest.raises(ValueError):
        make_bar(0, 10, float("inf"), 9, 10.5)


def test_bar_rejects_non_finite_volume():
    with pytest.raises(ValueError):
        make_bar(0, 10, 11, 9, 10.5, volume=float("nan"), volume_quality="exchange")


def test_bar_rejects_high_below_max_open_close():
    with pytest.raises(ValueError):
        make_bar(0, 10, 10.5, 9, 11)  # close=11 > high=10.5


def test_bar_rejects_low_above_min_open_close():
    with pytest.raises(ValueError):
        make_bar(0, 10, 12, 9.5, 9)  # close=9 < low=9.5


def test_bar_rejects_empty_source_id():
    with pytest.raises(ValueError):
        make_bar(0, 10, 11, 9, 10.5, source_id="  ")


def test_bar_rejects_empty_symbol():
    with pytest.raises(ValueError):
        make_bar(0, 10, 11, 9, 10.5, symbol="")


def test_bar_rejects_empty_timeframe():
    with pytest.raises(ValueError):
        make_bar(0, 10, 11, 9, 10.5, timeframe="")


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError):
        Bar(
            source_id="SRC1",
            symbol="TEST",
            timeframe="M1",
            timestamp=datetime(2026, 1, 1),  # naive, sem tzinfo
            open=10,
            high=11,
            low=9,
            close=10.5,
            volume=100.0,
            volume_quality="exchange",
        )


# --- FeatureConfig ---------------------------------------------------------


@pytest.mark.parametrize(
    "field_name", ["atr_period", "volatility_window", "trend_window", "volume_window"]
)
@pytest.mark.parametrize("bad_value", [0, -1, 1.5])
def test_feature_config_rejects_non_positive_int(field_name, bad_value):
    kwargs = {"atr_period": 14, "volatility_window": 14, "trend_window": 14, "volume_window": 20}
    kwargs[field_name] = bad_value
    with pytest.raises(ValueError):
        FeatureConfig(**kwargs)


def test_feature_config_rejects_bool_as_int():
    with pytest.raises(ValueError):
        FeatureConfig(atr_period=True)


# --- validação de série em compute_feature_rows --------------------------


def test_compute_feature_rows_rejects_mixed_symbol():
    bars = [
        make_bar(0, 10, 11, 9, 10.5, symbol="AAA"),
        make_bar(1, 10.5, 12, 10, 11.5, symbol="BBB"),
    ]
    with pytest.raises(ValueError):
        compute_feature_rows(bars, SMALL_CONFIG)


def test_compute_feature_rows_rejects_mixed_source_id():
    bars = [
        make_bar(0, 10, 11, 9, 10.5, source_id="SRC1"),
        make_bar(1, 10.5, 12, 10, 11.5, source_id="SRC2"),
    ]
    with pytest.raises(ValueError):
        compute_feature_rows(bars, SMALL_CONFIG)


def test_compute_feature_rows_rejects_mixed_timeframe():
    bars = [
        make_bar(0, 10, 11, 9, 10.5, timeframe="M1"),
        make_bar(1, 10.5, 12, 10, 11.5, timeframe="M5"),
    ]
    with pytest.raises(ValueError):
        compute_feature_rows(bars, SMALL_CONFIG)


def test_compute_feature_rows_rejects_out_of_order_timestamps():
    early = make_bar(0, 10, 11, 9, 10.5)
    late = make_bar(1, 10.5, 12, 10, 11.5)
    with pytest.raises(ValueError):
        compute_feature_rows([late, early], SMALL_CONFIG)


def test_compute_feature_rows_rejects_duplicate_timestamps():
    bar = make_bar(0, 10, 11, 9, 10.5)
    duplicate = make_bar(0, 10.5, 12, 10, 11.5)  # mesmo timestamp (index=0)
    with pytest.raises(ValueError):
        compute_feature_rows([bar, duplicate], SMALL_CONFIG)


def test_compute_feature_rows_accepts_empty_series():
    assert compute_feature_rows([], SMALL_CONFIG) == []


# --- validação de FeatureRow -----------------------------------------------


def _base_row_kwargs() -> dict:
    return dict(
        schema_version=FEATURE_SCHEMA_VERSION,
        source_id="SRC1",
        symbol="TEST",
        timeframe="M1",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        true_range=1.0,
        atr=1.0,
        atr_normalized=0.1,
        log_return=0.01,
        realized_volatility=0.02,
        normalized_range=0.05,
        efficiency_ratio=0.5,
        trend_strength=0.3,
        close_position=0.5,
        volume_relative=1.2,
        volume_quality="exchange",
    )


def test_feature_row_rejects_wrong_schema_version():
    kwargs = _base_row_kwargs()
    kwargs["schema_version"] = FEATURE_SCHEMA_VERSION + 1
    with pytest.raises(ValueError):
        FeatureRow(**kwargs)


def test_feature_row_rejects_naive_timestamp():
    kwargs = _base_row_kwargs()
    kwargs["timestamp"] = datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        FeatureRow(**kwargs)


def test_feature_row_rejects_non_finite_value():
    kwargs = _base_row_kwargs()
    kwargs["atr"] = float("nan")
    with pytest.raises(ValueError):
        FeatureRow(**kwargs)


def test_feature_row_rejects_out_of_range_close_position():
    kwargs = _base_row_kwargs()
    kwargs["close_position"] = 1.5
    with pytest.raises(ValueError):
        FeatureRow(**kwargs)


def test_feature_row_rejects_out_of_range_trend_strength():
    kwargs = _base_row_kwargs()
    kwargs["trend_strength"] = -1.5
    with pytest.raises(ValueError):
        FeatureRow(**kwargs)


def test_feature_row_rejects_empty_source_id():
    kwargs = _base_row_kwargs()
    kwargs["source_id"] = ""
    with pytest.raises(ValueError):
        FeatureRow(**kwargs)


# --- schema e round-trip do artefato -------------------------------------


def test_artifact_roundtrip(tmp_path):
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    path = tmp_path / "TEST_M1_features.json"
    save_feature_artifact(
        path, source_id="SRC1", symbol="TEST", timeframe="M1", config=SMALL_CONFIG, rows=rows
    )

    loaded = load_feature_artifact(path)
    assert loaded["source_id"] == "SRC1"
    assert loaded["symbol"] == "TEST"
    assert loaded["timeframe"] == "M1"
    assert loaded["config"] == SMALL_CONFIG
    assert len(loaded["rows"]) == len(rows)
    for original, restored in zip(rows, loaded["rows"], strict=True):
        assert original.to_dict() == restored.to_dict()


def test_artifact_rejects_unknown_schema_version(tmp_path):
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    path = tmp_path / "TEST_M1_features.json"
    save_feature_artifact(
        path, source_id="SRC1", symbol="TEST", timeframe="M1", config=SMALL_CONFIG, rows=rows
    )

    text = path.read_text(encoding="utf-8").replace(
        f'"schema_version": {FEATURE_SCHEMA_VERSION}',
        f'"schema_version": {FEATURE_SCHEMA_VERSION + 999}',
        1,
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError):
        load_feature_artifact(path)


def test_artifact_save_rejects_row_with_mismatched_source_id(tmp_path):
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    tampered = list(rows)
    tampered[2] = _replace_row(rows[2], source_id="OTHER")
    path = tmp_path / "TEST_M1_features.json"
    with pytest.raises(ValueError):
        save_feature_artifact(
            path,
            source_id="SRC1",
            symbol="TEST",
            timeframe="M1",
            config=SMALL_CONFIG,
            rows=tampered,
        )
    assert not path.exists()


def _replace_row(row: FeatureRow, **overrides) -> FeatureRow:
    data = row.to_dict()
    data.update(overrides)
    data["timestamp"] = row.timestamp if "timestamp" not in overrides else overrides["timestamp"]
    kwargs = {
        "schema_version": data["schema_version"],
        "source_id": data["source_id"],
        "symbol": data["symbol"],
        "timeframe": data["timeframe"],
        "timestamp": data["timestamp"],
        "true_range": data["true_range"],
        "atr": data["atr"],
        "atr_normalized": data["atr_normalized"],
        "log_return": data["log_return"],
        "realized_volatility": data["realized_volatility"],
        "normalized_range": data["normalized_range"],
        "efficiency_ratio": data["efficiency_ratio"],
        "trend_strength": data["trend_strength"],
        "close_position": data["close_position"],
        "volume_relative": data["volume_relative"],
        "volume_quality": data["volume_quality"],
    }
    return FeatureRow(**kwargs)


def test_artifact_save_rejects_out_of_order_row_timestamps(tmp_path):
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    reordered = [rows[1], rows[0]] + rows[2:]
    path = tmp_path / "TEST_M1_features.json"
    with pytest.raises(ValueError):
        save_feature_artifact(
            path,
            source_id="SRC1",
            symbol="TEST",
            timeframe="M1",
            config=SMALL_CONFIG,
            rows=reordered,
        )
    assert not path.exists()


def test_artifact_load_rejects_row_count_mismatch(tmp_path):
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    path = tmp_path / "TEST_M1_features.json"
    save_feature_artifact(
        path, source_id="SRC1", symbol="TEST", timeframe="M1", config=SMALL_CONFIG, rows=rows
    )
    text = path.read_text(encoding="utf-8").replace(
        f'"row_count": {len(rows)}', f'"row_count": {len(rows) + 1}', 1
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_feature_artifact(path)


def test_artifact_load_rejects_header_symbol_not_matching_rows(tmp_path):
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    path = tmp_path / "TEST_M1_features.json"
    save_feature_artifact(
        path, source_id="SRC1", symbol="TEST", timeframe="M1", config=SMALL_CONFIG, rows=rows
    )
    text = path.read_text(encoding="utf-8").replace(
        '"symbol": "TEST",\n  "timeframe"', '"symbol": "OTHER",\n  "timeframe"', 1
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_feature_artifact(path)


def test_artifact_write_is_atomic_no_partial_file_left_on_error(tmp_path, monkeypatch):
    bars = simple_series()
    rows = compute_feature_rows(bars, SMALL_CONFIG)
    path = tmp_path / "TEST_M1_features.json"

    import market_analytics.storage as storage_module

    def boom(*args, **kwargs):
        raise OSError("disco cheio simulado")

    monkeypatch.setattr(storage_module.os, "fsync", boom)
    with pytest.raises(OSError):
        save_feature_artifact(
            path, source_id="SRC1", symbol="TEST", timeframe="M1", config=SMALL_CONFIG, rows=rows
        )

    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


# --- robustez de tipos, lacunas e artefatos vazios -----------------------


def test_realized_volatility_does_not_stitch_returns_across_invalid_gap():
    closes = [10.0, 11.0, 12.0, -1.0, 10.0, 11.0, 12.0]
    bars = [
        make_bar(index, close, close + 1.0, close - 1.0, close)
        for index, close in enumerate(closes)
    ]
    config = FeatureConfig(
        atr_period=2,
        volatility_window=2,
        trend_window=2,
        volume_window=2,
    )

    rows = compute_feature_rows(bars, config)

    assert rows[2].realized_volatility is not None
    assert rows[3].realized_volatility is None
    assert rows[4].realized_volatility is None
    assert rows[5].realized_volatility is None
    assert rows[6].realized_volatility is not None


@pytest.mark.parametrize("field_name,bad_value", [("open", True), ("close", "10")])
def test_bar_rejects_bool_and_non_numeric_ohlc(field_name, bad_value):
    kwargs = {
        "source_id": "SRC1",
        "symbol": "TEST",
        "timeframe": "M1",
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.0,
        "volume": 100.0,
        "volume_quality": "exchange",
    }
    kwargs[field_name] = bad_value
    with pytest.raises(ValueError):
        Bar(**kwargs)


def test_bar_rejects_bool_volume():
    with pytest.raises(ValueError):
        make_bar(0, 10.0, 11.0, 9.0, 10.0, volume=True)


def test_feature_row_rejects_bool_schema_version():
    kwargs = _base_row_kwargs()
    kwargs["schema_version"] = True
    with pytest.raises(ValueError):
        FeatureRow(**kwargs)


def test_feature_row_rejects_bool_numeric_feature():
    kwargs = _base_row_kwargs()
    kwargs["atr"] = True
    with pytest.raises(ValueError):
        FeatureRow(**kwargs)


@pytest.mark.parametrize(
    "payload",
    [
        {"atr_period": 3},
        {**SMALL_CONFIG.to_dict(), "future_field": 1},
        [],
    ],
)
def test_feature_config_from_dict_requires_exact_object_schema(payload):
    with pytest.raises(ValueError):
        FeatureConfig.from_dict(payload)


def test_empty_artifact_roundtrip_is_valid(tmp_path):
    path = tmp_path / "empty_features.json"
    save_feature_artifact(
        path,
        source_id="SRC1",
        symbol="TEST",
        timeframe="M1",
        config=SMALL_CONFIG,
        rows=[],
    )

    loaded = load_feature_artifact(path)
    assert loaded["rows"] == []
    assert loaded["config"] == SMALL_CONFIG


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(source_id=""),
        lambda payload: payload.update(rows={}),
        lambda payload: payload.update(row_count=True),
        lambda payload: payload.update(row_count=-1),
        lambda payload: payload["config"].update(future_field=1),
    ],
)
def test_empty_artifact_rejects_invalid_header_shapes(tmp_path, mutation):
    path = tmp_path / "empty_features.json"
    save_feature_artifact(
        path,
        source_id="SRC1",
        symbol="TEST",
        timeframe="M1",
        config=SMALL_CONFIG,
        rows=[],
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_feature_artifact(path)
