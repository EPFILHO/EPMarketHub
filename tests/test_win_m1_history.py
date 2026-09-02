"""Testes do produtor histórico M1/features do WIN$ (DEV-007).

Nenhum teste aqui toca `D:\\EPData`, `D:\\EP\\EPMarketHub`, um terminal MT5
real ou o repositório Fusion-Quant: todo provider é falso (`FakeRatesProvider`)
e todo inventário é um fixture JSON pequeno construído em `tmp_path`.
`Mt5RatesProvider` é exercitado só contra um módulo `MetaTrader5` falso
injetado em `sys.modules` — nunca a biblioteca real, nunca um terminal real.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from market_analytics import win_m1_features
from market_analytics.config import FeatureConfig
from market_analytics.win_m1_collector import (
    FROZEN_WINDOW_END_EXCLUSIVE,
    FROZEN_WINDOW_START,
    MonthRejectedError,
    Mt5RatesProvider,
    build_month_bars,
    fetch_and_validate_month,
    month_windows,
    volume_for_row,
)
from market_analytics.win_m1_features import (
    HistoryProducerError,
    assert_frozen_protocol,
    assert_output_within_allowed_root,
    available_at_utc,
    collect_validated_history,
    preflight,
    run_m1_history_producer,
)
from market_analytics.win_m1_inventory import (
    EXPECTED_INVENTORY_MONTHS,
    INVENTORY_END_MONTH,
    INVENTORY_SCHEMA,
    INVENTORY_START_MONTH,
    INVENTORY_SYMBOL,
    INVENTORY_TIMEFRAME,
    InventoryHashMismatchError,
    InventoryMonthMismatchError,
    InventoryValidationError,
    RawM1Row,
    WinCopy2Inventory,
    compute_month_fingerprint,
    fingerprint_human_prefix,
    load_inventory_file,
)

SOURCE_ID = "clear_research"
SYMBOL = "WIN$"

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fingerprint dummy (formato válido, valor arbitrário) para meses do
# inventário que um teste não exercita — nunca comparado contra barras reais.
_DUMMY_FINGERPRINT = "0" * 64


# ---------------------------------------------------------------------------
# Fixtures compartilhadas
# ---------------------------------------------------------------------------


def _row(dt: datetime, price: float, *, tick_volume: int = 10, spread: int = 5, real_volume: int = 0) -> tuple:
    return (int(dt.timestamp()), price, price + 1.0, price - 1.0, price, tick_volume, spread, real_volume)


def _month_rows(year: int, month: int, *, day: int, hour: int, minute: int, count: int, price_start: float = 5000.0) -> list[tuple]:
    start = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return [_row(start + timedelta(minutes=i), price_start + i) for i in range(count)]


def _jan_rows() -> list[tuple]:
    # Dois buckets M5 completos: 00:00-00:04 e 00:05-00:09.
    return _month_rows(2026, 1, day=1, hour=0, minute=0, count=10, price_start=5000.0)


def _feb_rows() -> list[tuple]:
    # Um bucket M5 completo: 00:00-00:04/fev.
    return _month_rows(2026, 2, day=1, hour=0, minute=0, count=5, price_start=5100.0)


class FakeRatesProvider:
    def __init__(self, rows_by_month: dict[str, list[tuple] | None]) -> None:
        self._rows_by_month = rows_by_month
        self.calls: list[tuple[str, datetime, datetime]] = []

    def copy_rates_range(self, *, symbol: str, date_from: datetime, date_to: datetime):
        self.calls.append((symbol, date_from, date_to))
        month = f"{date_from.year:04d}-{date_from.month:02d}"
        return self._rows_by_month.get(month)


def _fingerprint_of(rows: list[tuple]) -> str:
    return compute_month_fingerprint([RawM1Row(*row) for row in rows])


def _month_record(month: str, *, bars: int, bars_fingerprint: str) -> dict:
    """Registro de mês no schema real completo (10 campos), coerente:
    `first`/`last_timestamp_utc` só não-null quando `bars>0`, ambos dentro
    do próprio `month`, `first <= last`, e `status` travado em
    `"available_full_span_candidate"` sempre que houver barras."""

    has_bars = bars > 0
    return {
        "requested_month": month,
        "status": "available_full_span_candidate" if has_bars else "no_bars_in_range",
        "bars": bars,
        "first_timestamp_utc": f"{month}-01T00:00:00+00:00" if has_bars else None,
        "last_timestamp_utc": f"{month}-01T00:10:00+00:00" if has_bars else None,
        "outside_range_count": 0,
        "bars_fingerprint": bars_fingerprint,
        "query_attempts": 1,
        "stable": True,
        "mt5_last_error": [1, "Success"],
    }


def _provenance_fields() -> dict:
    """Campos factuais de proveniência exigidos pelo schema real observado
    (auditoria Codex, 3ª rodada) — nunca usados para aceitar/rejeitar
    barras, só validados quanto a tipo/valor."""

    return {
        "terminal_path": r"C:\fake\Fusion-Quant\runtime\mt5-clear-research\terminal64.exe",
        "terminal_connected": True,
        "terminal_build": 4200,
        "mt5_version": [500, 4200, "1 Jan 2026"],
        "notes": [],
    }


def _write_inventory(path: Path, overrides: dict[str, tuple[int, str]]) -> str:
    """Grava um inventário fixture no schema REAL do Fusion Quant (jan..jul/2026).

    `overrides` fornece `(bars, bars_fingerprint)` exatos para os meses
    efetivamente exercitados pelo teste; os demais dos sete meses
    obrigatórios recebem um registro dummy válido (nunca comparado, já que
    os testes só solicitam os meses presentes em `overrides`/na janela
    pedida). Devolve o SHA-256 exato dos bytes gravados.
    """

    months = []
    for month in EXPECTED_INVENTORY_MONTHS:
        if month in overrides:
            bars, fingerprint = overrides[month]
        else:
            bars, fingerprint = 1, _DUMMY_FINGERPRINT
        months.append(_month_record(month, bars=bars, bars_fingerprint=fingerprint))
    data = {
        "schema": INVENTORY_SCHEMA,
        "symbol": INVENTORY_SYMBOL,
        "timeframe": INVENTORY_TIMEFRAME,
        "start_month": INVENTORY_START_MONTH,
        "end_month": INVENTORY_END_MONTH,
        "months": months,
        **_provenance_fields(),
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    raw_bytes = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw_bytes)
    return hashlib.sha256(raw_bytes).hexdigest()


def _small_config() -> FeatureConfig:
    return FeatureConfig(atr_period=2, volatility_window=2, trend_window=2, volume_window=2)


def _two_month_inventory_and_provider(tmp_path: Path) -> tuple:
    jan_rows, feb_rows = _jan_rows(), _feb_rows()
    jan_fp, feb_fp = _fingerprint_of(jan_rows), _fingerprint_of(feb_rows)
    inventory_path = tmp_path / "inventory" / "inventory.json"
    expected_sha = _write_inventory(
        inventory_path, {"2026-01": (len(jan_rows), jan_fp), "2026-02": (len(feb_rows), feb_fp)}
    )
    provider = FakeRatesProvider({"2026-01": jan_rows, "2026-02": feb_rows})
    inventory = load_inventory_file(inventory_path, expected_sha256=expected_sha)
    return provider, inventory, inventory_path, expected_sha


def _output_and_terminal(tmp_path: Path) -> tuple[Path, str]:
    return tmp_path / "analytics" / "win_m1_history", str(tmp_path / "terminal" / "terminal64.exe")


# ---------------------------------------------------------------------------
# month_windows / fim de solicitação inclusivo ao provider (item 5)
# ---------------------------------------------------------------------------


def test_month_windows_frozen_range_has_seven_semiopen_months() -> None:
    windows = month_windows(FROZEN_WINDOW_START, FROZEN_WINDOW_END_EXCLUSIVE)

    assert [label for label, _start, _end, _request_end in windows] == [
        "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
    ]
    _first_label, first_start, first_end, first_request_end = windows[0]
    assert first_start == datetime(2026, 1, 1, tzinfo=UTC)
    assert first_end == datetime(2026, 2, 1, tzinfo=UTC)
    assert first_request_end == datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC)


def test_month_windows_request_end_is_inclusive_last_second_of_each_month() -> None:
    windows = month_windows(date(2026, 1, 1), date(2026, 3, 1))

    assert windows[0][3] == datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC)  # janeiro: 31 dias
    assert windows[1][3] == datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC)  # fev/2026: não bissexto, 28 dias


def test_month_windows_rejects_non_first_of_month_boundaries() -> None:
    with pytest.raises(ValueError):
        month_windows(date(2026, 1, 15), date(2026, 8, 1))
    with pytest.raises(ValueError):
        month_windows(date(2026, 1, 1), date(2026, 7, 15))
    with pytest.raises(ValueError):
        month_windows(date(2026, 8, 1), date(2026, 1, 1))


def test_fetch_and_validate_month_sends_provider_the_inclusive_request_end_never_next_month_start() -> None:
    """Item 5: copy_rates_range é inclusivo — nunca enviar 00:00 do mês seguinte."""

    rows = _jan_rows()
    provider = FakeRatesProvider({"2026-01": rows})
    label, window_start, window_end, request_end = month_windows(date(2026, 1, 1), date(2026, 2, 1))[0]

    fetch_and_validate_month(
        provider, symbol=SYMBOL, month=label,
        window_start=window_start, window_end=window_end, request_end=request_end,
    )

    assert provider.calls == [(SYMBOL, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC))]
    assert provider.calls[0][2] != datetime(2026, 2, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fetch_and_validate_month — provider falso: válido, vazio, fora da faixa,
# duplicado e OHLC inválido/não finito.
# ---------------------------------------------------------------------------


def _jan_window() -> tuple[str, datetime, datetime, datetime]:
    return month_windows(date(2026, 1, 1), date(2026, 2, 1))[0]


def test_fetch_and_validate_month_accepts_a_well_formed_month() -> None:
    rows = _jan_rows()
    provider = FakeRatesProvider({"2026-01": rows})
    label, window_start, window_end, request_end = _jan_window()

    result = fetch_and_validate_month(
        provider, symbol=SYMBOL, month=label,
        window_start=window_start, window_end=window_end, request_end=request_end,
    )

    assert len(result) == 10
    assert [row.time for row in result] == sorted(row.time for row in result)


def test_fetch_and_validate_month_rejects_unexpected_empty_response() -> None:
    provider = FakeRatesProvider({"2026-01": None})
    label, window_start, window_end, request_end = _jan_window()

    with pytest.raises(MonthRejectedError, match="vazio inesperado"):
        fetch_and_validate_month(
            provider, symbol=SYMBOL, month=label,
            window_start=window_start, window_end=window_end, request_end=request_end,
        )


def test_fetch_and_validate_month_rejects_timestamp_outside_the_month() -> None:
    rows = _jan_rows()
    rows.append(_row(datetime(2026, 2, 1, 0, 0, tzinfo=UTC), 5200.0))  # cai em fevereiro
    provider = FakeRatesProvider({"2026-01": rows})
    label, window_start, window_end, request_end = _jan_window()

    with pytest.raises(MonthRejectedError, match="fora do mês"):
        fetch_and_validate_month(
            provider, symbol=SYMBOL, month=label,
            window_start=window_start, window_end=window_end, request_end=request_end,
        )


def test_fetch_and_validate_month_rejects_duplicated_timestamp() -> None:
    rows = _jan_rows()
    rows.append(_row(datetime(2026, 1, 1, 0, 4, tzinfo=UTC), 5099.0))  # duplica 00:04
    provider = FakeRatesProvider({"2026-01": rows})
    label, window_start, window_end, request_end = _jan_window()

    with pytest.raises(MonthRejectedError, match="duplicado ou não crescente"):
        fetch_and_validate_month(
            provider, symbol=SYMBOL, month=label,
            window_start=window_start, window_end=window_end, request_end=request_end,
        )


def test_fetch_and_validate_month_rejects_non_finite_ohlc() -> None:
    rows = _jan_rows()
    bad = list(rows[0])
    bad[4] = math.nan  # close
    rows[0] = tuple(bad)
    provider = FakeRatesProvider({"2026-01": rows})
    label, window_start, window_end, request_end = _jan_window()

    with pytest.raises(MonthRejectedError, match="linha #0 inválida"):
        fetch_and_validate_month(
            provider, symbol=SYMBOL, month=label,
            window_start=window_start, window_end=window_end, request_end=request_end,
        )


def test_build_month_bars_rejects_ohlc_inconsistent_with_bar_invariants() -> None:
    rows = _jan_rows()
    # high menor que close: inconsistente, mas cada campo isolado é finito.
    bad = list(rows[0])
    bad[1], bad[2], bad[3], bad[4] = 5000.0, 5000.5, 4999.0, 5001.0  # open, high, low, close
    rows[0] = tuple(bad)
    raw_rows = [RawM1Row(*row) for row in rows]

    with pytest.raises(MonthRejectedError, match="OHLC inválido"):
        build_month_bars("2026-01", raw_rows, source_id=SOURCE_ID, symbol=SYMBOL)


# ---------------------------------------------------------------------------
# Qualidade de volume (barra M1 -> Bar)
# ---------------------------------------------------------------------------


def test_volume_for_row_prefers_real_volume_when_non_negative() -> None:
    row = RawM1Row(time=0, open=1.0, high=2.0, low=0.5, close=1.5, tick_volume=7, spread=1, real_volume=42)
    volume, quality = volume_for_row(row)
    assert (volume, quality) == (42.0, "exchange")


def test_volume_for_row_falls_back_to_tick_volume_when_real_volume_invalid() -> None:
    row = RawM1Row(time=0, open=1.0, high=2.0, low=0.5, close=1.5, tick_volume=7, spread=1, real_volume=-1)
    volume, quality = volume_for_row(row)
    assert (volume, quality) == (7.0, "tick_proxy")


def test_build_month_bars_propagates_volume_quality_into_bar() -> None:
    raw_rows = [
        RawM1Row(time=0, open=10.0, high=11.0, low=9.0, close=10.5, tick_volume=3, spread=1, real_volume=99),
        RawM1Row(time=60, open=10.5, high=11.5, low=9.5, close=11.0, tick_volume=4, spread=1, real_volume=-5),
    ]
    bars = build_month_bars("2026-01", raw_rows, source_id=SOURCE_ID, symbol=SYMBOL)
    assert (bars[0].volume, bars[0].volume_quality) == (99.0, "exchange")
    assert (bars[1].volume, bars[1].volume_quality) == (4.0, "tick_proxy")


# ---------------------------------------------------------------------------
# Regressão (auditoria Codex do DEV-007): aggregate_bars não pode presumir
# "exchange" — ver também tests/test_quant_mvp.py (nível de componente).
# ---------------------------------------------------------------------------


def test_aggregate_bars_does_not_turn_five_tick_proxy_m1_into_an_exchange_m5() -> None:
    from market_analytics.bars import Bar
    from market_analytics.quant_mvp import aggregate_bars

    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    bars = [
        Bar(
            source_id=SOURCE_ID, symbol=SYMBOL, timeframe="M1", timestamp=base + timedelta(minutes=i),
            open=100.0 + i, high=101.0 + i, low=99.0 + i, close=100.5 + i,
            volume=10.0, volume_quality="tick_proxy",
        )
        for i in range(5)
    ]

    m5 = aggregate_bars(bars, "M5")

    assert len(m5) == 1
    assert m5[0].volume_quality == "tick_proxy"
    assert m5[0].volume == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Fingerprint — algoritmo EXATO do Fusion Quant (item 1)
# ---------------------------------------------------------------------------


def test_compute_month_fingerprint_matches_independent_fixture_value() -> None:
    """Valor esperado calculado independentemente (sha256sum sobre as mesmas
    linhas canônicas, fora do código sob teste) — nunca pela própria função.

    Linhas: "{time}|{open:.10f}|{high:.10f}|{low:.10f}|{close:.10f}|"
    "{tick_volume}|{spread}|{real_volume}", cada uma seguida de um "\\n",
    concatenadas e hasheadas em SHA-256, maiúsculo.
    """

    rows = [
        RawM1Row(time=1767225600, open=5000.0, high=5001.0, low=4999.0, close=5000.5, tick_volume=100, spread=5, real_volume=10),
        RawM1Row(time=1767225660, open=5000.5, high=5002.0, low=5000.0, close=5001.5, tick_volume=120, spread=6, real_volume=20),
    ]

    fingerprint = compute_month_fingerprint(rows)

    assert fingerprint == "AC28F97C81BA9CE616D52C0B7DC8053365A36DCF98C637DB3B92D6CC82F654BC"


def test_compute_month_fingerprint_is_deterministic_and_sensitive_to_any_field() -> None:
    rows = [RawM1Row(*row) for row in _jan_rows()]
    fp1 = compute_month_fingerprint(rows)
    fp2 = compute_month_fingerprint(rows)
    assert fp1 == fp2
    assert len(fp1) == 64
    assert fp1 == fp1.upper()

    mutated = list(rows)
    mutated[0] = RawM1Row(
        time=mutated[0].time, open=mutated[0].open, high=mutated[0].high, low=mutated[0].low,
        close=mutated[0].close, tick_volume=mutated[0].tick_volume, spread=mutated[0].spread + 1,
        real_volume=mutated[0].real_volume,
    )
    assert compute_month_fingerprint(mutated) != fp1


def test_fingerprint_human_prefix_is_sixteen_uppercase_hex_chars() -> None:
    rows = [RawM1Row(*row) for row in _jan_rows()]
    fp = compute_month_fingerprint(rows)
    prefix = fingerprint_human_prefix(fp)
    assert prefix == fp[:16].upper()
    assert len(prefix) == 16


# ---------------------------------------------------------------------------
# Schema real do inventário (item 2) — estrito, sem tolerância especulativa.
# ---------------------------------------------------------------------------


def _real_shaped_inventory_dict(jan_fp: str) -> dict:
    """Fixture no formato real relatado pela auditoria Codex, INCLUINDO os
    campos de proveniência (`terminal_path`/`terminal_connected`/
    `terminal_build`/`mt5_version`/`notes`) que a 2ª rodada não conhecia.

    Antes desta correção, um arquivo exatamente neste formato era rejeitado
    com `InventoryValidationError: campo(s) desconhecido(s)` para esses
    cinco campos.
    """

    months = [_month_record(month, bars=1, bars_fingerprint=_DUMMY_FINGERPRINT) for month in EXPECTED_INVENTORY_MONTHS]
    months[0] = _month_record("2026-01", bars=10, bars_fingerprint=jan_fp)
    return {
        "schema": INVENTORY_SCHEMA,
        "symbol": INVENTORY_SYMBOL,
        "timeframe": INVENTORY_TIMEFRAME,
        "start_month": INVENTORY_START_MONTH,
        "end_month": INVENTORY_END_MONTH,
        "months": months,
        **_provenance_fields(),
    }


def test_real_shaped_inventory_fixture_is_accepted(tmp_path: Path) -> None:
    """Fixture top-level completa, igual ao formato real (inclui
    terminal_path/terminal_connected/terminal_build/mt5_version/notes) —
    reprodução exata do achado que bloqueava a auditoria real."""

    jan_fp = _fingerprint_of(_jan_rows())
    data = _real_shaped_inventory_dict(jan_fp)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    raw_bytes = text.encode("utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_bytes(raw_bytes)
    expected_sha = hashlib.sha256(raw_bytes).hexdigest()

    inventory = load_inventory_file(inventory_path, expected_sha256=expected_sha)

    assert inventory.months["2026-01"].bars == 10
    assert inventory.months["2026-01"].bars_fingerprint == jan_fp
    assert set(inventory.months) == set(EXPECTED_INVENTORY_MONTHS)
    assert inventory.terminal_path == data["terminal_path"]
    assert inventory.terminal_connected is True
    assert inventory.terminal_build == 4200
    assert inventory.mt5_version == [500, 4200, "1 Jan 2026"]
    assert inventory.notes == []


def test_real_shaped_inventory_fixture_accepts_null_mt5_version(tmp_path: Path) -> None:
    jan_fp = _fingerprint_of(_jan_rows())
    data = _real_shaped_inventory_dict(jan_fp)
    data["mt5_version"] = None
    text = json.dumps(data, ensure_ascii=False, indent=2)
    raw_bytes = text.encode("utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_bytes(raw_bytes)
    expected_sha = hashlib.sha256(raw_bytes).hexdigest()

    inventory = load_inventory_file(inventory_path, expected_sha256=expected_sha)

    assert inventory.mt5_version is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("terminal_path", ""),
        ("terminal_path", 123),
        ("terminal_connected", False),
        ("terminal_connected", "true"),
        ("terminal_build", 0),
        ("terminal_build", -1),
        ("terminal_build", "4200"),
        ("mt5_version", []),
        ("mt5_version", {"major": 5}),
        ("mt5_version", [True]),
        ("notes", "no notes"),
        ("notes", [1, 2]),
    ],
)
def test_inventory_rejects_invalid_provenance_field_types(field: str, value) -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data[field] = value
    with pytest.raises(InventoryValidationError):
        WinCopy2Inventory.from_dict(data)


def test_inventory_still_rejects_truly_unknown_fields() -> None:
    """Continua estrito: só os onze campos top-level conhecidos são aceitos."""

    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["some_future_field"] = "nope"
    with pytest.raises(InventoryValidationError, match="desconhecido"):
        WinCopy2Inventory.from_dict(data)


def test_load_inventory_file_accepts_matching_hash(tmp_path: Path) -> None:
    jan_fp = _fingerprint_of(_jan_rows())
    inventory_path = tmp_path / "inventory.json"
    expected_sha = _write_inventory(inventory_path, {"2026-01": (10, jan_fp)})

    inventory = load_inventory_file(inventory_path, expected_sha256=expected_sha)

    assert inventory.months["2026-01"].bars == 10
    assert inventory.months["2026-01"].bars_fingerprint == jan_fp
    assert inventory.schema == INVENTORY_SCHEMA


def test_load_inventory_file_rejects_hash_mismatch(tmp_path: Path) -> None:
    jan_fp = _fingerprint_of(_jan_rows())
    inventory_path = tmp_path / "inventory.json"
    _write_inventory(inventory_path, {"2026-01": (10, jan_fp)})

    wrong_sha = "0" * 64
    with pytest.raises(InventoryHashMismatchError):
        load_inventory_file(inventory_path, expected_sha256=wrong_sha)


def test_load_inventory_file_rejects_malformed_json(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.json"
    raw_bytes = b"{not json"
    inventory_path.write_bytes(raw_bytes)
    expected_sha = hashlib.sha256(raw_bytes).hexdigest()

    with pytest.raises(InventoryValidationError):
        load_inventory_file(inventory_path, expected_sha256=expected_sha)


def test_inventory_rejects_old_tolerant_shape_bar_count_and_fingerprint_keys() -> None:
    """A 1ª entrega tolerava `bar_count`/`fingerprint` (não `bars`/`bars_fingerprint`)
    e `months` como objeto. Essa tolerância foi removida — este formato antigo
    deve ser recusado agora."""

    data = {
        "schema": "fusion_quant.win_copy2_monthly_retro.inventory_m1",
        "months": {"2026-01": {"bar_count": 10, "fingerprint": "A" * 64}},
    }
    with pytest.raises(InventoryValidationError):
        WinCopy2Inventory.from_dict(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "wrong-schema"),
        ("symbol", "WDO$"),
        ("timeframe", "M5"),
        ("start_month", "2026-02"),
        ("end_month", "2026-08"),
    ],
)
def test_inventory_rejects_wrong_top_level_identity_field(field: str, value: str) -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data[field] = value
    with pytest.raises(InventoryValidationError):
        WinCopy2Inventory.from_dict(data)


def test_inventory_rejects_missing_month() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"] = [record for record in data["months"] if record["requested_month"] != "2026-04"]
    with pytest.raises(InventoryValidationError, match="ausentes"):
        WinCopy2Inventory.from_dict(data)


def test_inventory_rejects_extra_month_outside_jan_jul() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"].append(_month_record("2026-08", bars=1, bars_fingerprint=_DUMMY_FINGERPRINT))
    with pytest.raises(InventoryValidationError, match="inesperados"):
        WinCopy2Inventory.from_dict(data)


def test_inventory_rejects_duplicated_month() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"].append(_month_record("2026-01", bars=1, bars_fingerprint=_DUMMY_FINGERPRINT))
    with pytest.raises(InventoryValidationError, match="duplicado"):
        WinCopy2Inventory.from_dict(data)


def test_inventory_rejects_unstable_month() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"][1]["stable"] = False
    with pytest.raises(InventoryValidationError, match="stable"):
        WinCopy2Inventory.from_dict(data)


def test_inventory_rejects_month_with_outside_range_bars() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"][1]["outside_range_count"] = 3
    with pytest.raises(InventoryValidationError, match="outside_range_count"):
        WinCopy2Inventory.from_dict(data)


def test_inventory_rejects_unknown_field_in_month_record() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"][1]["extra_field"] = "nope"
    with pytest.raises(InventoryValidationError, match="desconhecido"):
        WinCopy2Inventory.from_dict(data)


def test_inventory_rejects_month_record_missing_a_required_field() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    del data["months"][1]["status"]
    with pytest.raises(InventoryValidationError, match="ausente"):
        WinCopy2Inventory.from_dict(data)


def test_real_shaped_month_record_accepts_full_ten_field_fixture() -> None:
    """Reprodução exata do achado: o registro mensal real completo (10
    campos: requested_month/status/bars/first_timestamp_utc/
    last_timestamp_utc/outside_range_count/bars_fingerprint/query_attempts/
    stable/mt5_last_error) é aceito e os 4 campos novos ficam disponíveis."""

    jan_fp = _fingerprint_of(_jan_rows())
    data = _real_shaped_inventory_dict(jan_fp)

    inventory = WinCopy2Inventory.from_dict(data)

    jan_entry = inventory.months["2026-01"]
    assert jan_entry.first_timestamp_utc == "2026-01-01T00:00:00+00:00"
    assert jan_entry.last_timestamp_utc == "2026-01-01T00:10:00+00:00"
    assert jan_entry.query_attempts == 1
    assert jan_entry.mt5_last_error == [1, "Success"]
    assert jan_entry.status == "available_full_span_candidate"


@pytest.mark.parametrize(
    "field,value",
    [
        ("first_timestamp_utc", None),
        ("first_timestamp_utc", "2026-01-01"),  # não timezone-aware
        ("first_timestamp_utc", "not-a-date"),
        ("first_timestamp_utc", "2026-03-01T00:00:00+00:00"),  # mês errado (registro é de 2026-02)
        ("last_timestamp_utc", None),
        ("last_timestamp_utc", "2026-03-01T00:00:00+00:00"),  # mês errado (registro é de 2026-02)
        ("query_attempts", 0),
        ("query_attempts", -1),
        ("query_attempts", "1"),
        ("mt5_last_error", None),
        ("mt5_last_error", [1]),
        ("mt5_last_error", [1, 2, 3]),
        ("mt5_last_error", ["1", "erro"]),
        ("mt5_last_error", [1, 2]),  # mensagem não é string
        ("status", "some_other_status"),
    ],
)
def test_inventory_rejects_invalid_month_record_field_values(field: str, value) -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"][1][field] = value  # 2026-02, bars=1 (>0): exige os campos coerentes
    with pytest.raises(InventoryValidationError):
        WinCopy2Inventory.from_dict(data)


def test_inventory_accepts_null_first_last_timestamp_only_when_bars_zero() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"][1]["bars"] = 0
    data["months"][1]["first_timestamp_utc"] = None
    data["months"][1]["last_timestamp_utc"] = None
    data["months"][1]["status"] = "no_bars_in_range"

    inventory = WinCopy2Inventory.from_dict(data)

    assert inventory.months["2026-02"].bars == 0
    assert inventory.months["2026-02"].first_timestamp_utc is None
    assert inventory.months["2026-02"].last_timestamp_utc is None


def test_inventory_rejects_null_timestamps_when_bars_positive() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    data["months"][1]["first_timestamp_utc"] = None
    data["months"][1]["last_timestamp_utc"] = None
    # bars permanece 1 (>0): first/last não podem ser null.
    with pytest.raises(InventoryValidationError, match="não podem ser null"):
        WinCopy2Inventory.from_dict(data)


def test_inventory_rejects_first_timestamp_after_last_within_same_month() -> None:
    data = _real_shaped_inventory_dict(_DUMMY_FINGERPRINT)
    # Ambos dentro de 2026-02 (mesmo mês do registro), mas first > last.
    data["months"][1]["first_timestamp_utc"] = "2026-02-01T00:10:00+00:00"
    data["months"][1]["last_timestamp_utc"] = "2026-02-01T00:00:00+00:00"
    with pytest.raises(InventoryValidationError, match="posterior a"):
        WinCopy2Inventory.from_dict(data)


def test_assert_month_matches_inventory_rejects_count_and_fingerprint_divergence() -> None:
    jan_rows = [RawM1Row(*row) for row in _jan_rows()]
    jan_fp = compute_month_fingerprint(jan_rows)
    data = _real_shaped_inventory_dict(jan_fp)
    inventory = WinCopy2Inventory.from_dict(data)

    from market_analytics.win_m1_inventory import assert_month_matches_inventory

    assert_month_matches_inventory(inventory, "2026-01", jan_rows)  # não levanta

    with pytest.raises(InventoryMonthMismatchError, match="contagem divergente"):
        assert_month_matches_inventory(inventory, "2026-01", jan_rows[:-1])

    tampered = list(jan_rows)
    tampered[0] = RawM1Row(
        time=tampered[0].time, open=tampered[0].open + 1.0, high=tampered[0].high + 1.0,
        low=tampered[0].low, close=tampered[0].close, tick_volume=tampered[0].tick_volume,
        spread=tampered[0].spread, real_volume=tampered[0].real_volume,
    )
    with pytest.raises(InventoryMonthMismatchError, match="fingerprint divergente"):
        assert_month_matches_inventory(inventory, "2026-01", tampered)


# ---------------------------------------------------------------------------
# collect_validated_history: tudo-ou-nada por mês e continuidade
# ---------------------------------------------------------------------------


def test_collect_validated_history_rejects_month_missing_from_inventory(tmp_path: Path) -> None:
    """Meses fora de jan..jul (ex.: agosto) nunca existem no inventário
    (schema estrito) — pedir uma janela que os inclua deve recusar com
    'ausente', mesmo que o provider tenha dados para eles."""

    aug_rows = _month_rows(2026, 8, day=1, hour=0, minute=0, count=3, price_start=5200.0)
    inventory_path = tmp_path / "inventory.json"
    expected_sha = _write_inventory(inventory_path, {})
    inventory = load_inventory_file(inventory_path, expected_sha256=expected_sha)
    provider = FakeRatesProvider({"2026-08": aug_rows})

    with pytest.raises(InventoryMonthMismatchError, match="ausente"):
        collect_validated_history(
            provider, inventory, symbol=SYMBOL, source_id=SOURCE_ID,
            start_date=date(2026, 8, 1), end_date_exclusive=date(2026, 9, 1),
        )


def test_collect_validated_history_matches_inventory_and_is_continuous(tmp_path: Path) -> None:
    provider, inventory, _path, _sha = _two_month_inventory_and_provider(tmp_path)

    outcomes = collect_validated_history(
        provider, inventory, symbol=SYMBOL, source_id=SOURCE_ID,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1),
    )

    assert [outcome.month for outcome in outcomes] == ["2026-01", "2026-02"]
    all_bars = [bar for outcome in outcomes for bar in outcome.bars]
    timestamps = [bar.timestamp for bar in all_bars]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)


# ---------------------------------------------------------------------------
# preflight / protocolo congelado / destino restrito (item 6)
# ---------------------------------------------------------------------------


def test_assert_frozen_protocol_accepts_exact_frozen_values() -> None:
    assert_frozen_protocol(symbol="WIN$", start_date=FROZEN_WINDOW_START, end_date_exclusive=FROZEN_WINDOW_END_EXCLUSIVE)


def test_assert_frozen_protocol_rejects_wrong_symbol() -> None:
    with pytest.raises(HistoryProducerError, match="symbol"):
        assert_frozen_protocol(symbol="WDO$", start_date=FROZEN_WINDOW_START, end_date_exclusive=FROZEN_WINDOW_END_EXCLUSIVE)


def test_assert_frozen_protocol_rejects_narrower_window() -> None:
    with pytest.raises(HistoryProducerError, match="janela"):
        assert_frozen_protocol(symbol="WIN$", start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1))


def test_assert_output_within_allowed_root_accepts_nested_path(tmp_path: Path) -> None:
    allowed = tmp_path / "EPData" / "MarketHub"
    output = allowed / "analytics" / "win_m1_history"

    resolved = assert_output_within_allowed_root(output, allowed)

    assert resolved == output.resolve()


def test_assert_output_within_allowed_root_rejects_outside_path(tmp_path: Path) -> None:
    allowed = tmp_path / "EPData" / "MarketHub"
    output = tmp_path / "elsewhere"

    with pytest.raises(HistoryProducerError, match="deve estar dentro"):
        assert_output_within_allowed_root(output, allowed)


def test_preflight_never_touches_mt5_and_fails_fast_on_bad_output_root(tmp_path: Path) -> None:
    _provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    _output_root, terminal_path = _output_and_terminal(tmp_path)

    with pytest.raises(HistoryProducerError, match="repositório"):
        preflight(
            output_root=REPO_ROOT, terminal_path=terminal_path, inventory_path=inventory_path,
            inventory_sha256=expected_sha, symbol=SYMBOL,
            start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1),
            require_frozen_protocol=False,
        )


def test_preflight_rejects_symbol_argument_diverging_from_loaded_inventory(tmp_path: Path) -> None:
    """Item 9: identidade do inventário carregado vs. o `symbol` solicitado."""

    _provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    output_root, terminal_path = _output_and_terminal(tmp_path)

    with pytest.raises(HistoryProducerError, match="symbol"):
        preflight(
            output_root=output_root, terminal_path=terminal_path, inventory_path=inventory_path,
            inventory_sha256=expected_sha, symbol="WDO$",
            start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1),
            require_frozen_protocol=False,
        )


def test_preflight_returns_resolved_output_and_loaded_inventory(tmp_path: Path) -> None:
    _provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    output_root, terminal_path = _output_and_terminal(tmp_path)

    resolved_output, inventory = preflight(
        output_root=output_root, terminal_path=terminal_path, inventory_path=inventory_path,
        inventory_sha256=expected_sha, symbol=SYMBOL,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1),
        require_frozen_protocol=False,
    )

    assert resolved_output == output_root.resolve()
    assert isinstance(inventory, WinCopy2Inventory)


# ---------------------------------------------------------------------------
# Mt5RatesProvider — falhas claras antes de qualquer consulta de barras
# (item 6), testadas só contra um `MetaTrader5` FALSO em sys.modules.
# ---------------------------------------------------------------------------


def _fake_mt5_module(*, initialize_ok: bool = True, connected: bool = True, symbol_select_ok: bool = True, rates=None):
    module = types.ModuleType("MetaTrader5")
    module.TIMEFRAME_M1 = 1
    module.calls = []

    def initialize(path=None):
        module.calls.append(("initialize", path))
        return initialize_ok

    def terminal_info():
        return types.SimpleNamespace(connected=True) if connected else None

    def symbol_select(symbol, enable):
        module.calls.append(("symbol_select", symbol, enable))
        return symbol_select_ok

    def last_error():
        return (1, "erro simulado")

    def shutdown():
        module.calls.append(("shutdown",))

    def copy_rates_range(symbol, timeframe, date_from, date_to):
        module.calls.append(("copy_rates_range", symbol, timeframe, date_from, date_to))
        return rates

    module.initialize = initialize
    module.terminal_info = terminal_info
    module.symbol_select = symbol_select
    module.last_error = last_error
    module.shutdown = shutdown
    module.copy_rates_range = copy_rates_range
    return module


def test_mt5_rates_provider_fails_clearly_when_initialize_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_mt5_module(initialize_ok=False)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    provider = Mt5RatesProvider(r"C:\fake\terminal64.exe", symbol="WIN$")

    with pytest.raises(RuntimeError, match="falha ao inicializar"):
        with provider:
            pass


def test_mt5_rates_provider_fails_clearly_when_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_mt5_module(connected=False)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    provider = Mt5RatesProvider(r"C:\fake\terminal64.exe", symbol="WIN$")

    with pytest.raises(RuntimeError, match="não conectado"):
        with provider:
            pass
    assert ("shutdown",) in fake.calls


def test_mt5_rates_provider_fails_clearly_when_symbol_select_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_mt5_module(symbol_select_ok=False)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    provider = Mt5RatesProvider(r"C:\fake\terminal64.exe", symbol="WIN$")

    with pytest.raises(RuntimeError, match="falha ao selecionar"):
        with provider:
            pass
    assert ("shutdown",) in fake.calls
    assert not any(call[0] == "copy_rates_range" for call in fake.calls)


def test_mt5_rates_provider_succeeds_and_converts_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_row = {
        "time": 1767225600, "open": 5000.0, "high": 5001.0, "low": 4999.0, "close": 5000.5,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    }
    fake = _fake_mt5_module(rates=[raw_row])
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)

    with Mt5RatesProvider(r"C:\fake\terminal64.exe", symbol="WIN$") as provider:
        result = provider.copy_rates_range(
            symbol="WIN$", date_from=datetime(2026, 1, 1, tzinfo=UTC), date_to=datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC)
        )

    assert result == [(1767225600, 5000.0, 5001.0, 4999.0, 5000.5, 10, 5, 0)]
    assert ("shutdown",) in fake.calls
    assert ("symbol_select", "WIN$", True) in fake.calls


# ---------------------------------------------------------------------------
# run_m1_history_producer: artefatos, M1->M5 via aggregate_bars,
# available_at_utc/look-ahead, warm-up contínuo, determinismo, promoção
# atômica sem artefato parcial após falha.
# ---------------------------------------------------------------------------


def test_run_m1_history_producer_writes_expected_artifacts(tmp_path: Path) -> None:
    provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    output_root, terminal_path = _output_and_terminal(tmp_path)

    summary = run_m1_history_producer(
        provider=provider, terminal_path=terminal_path, inventory_path=inventory_path,
        inventory_sha256=expected_sha, output_root=output_root, source_id=SOURCE_ID, symbol=SYMBOL,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1), feature_config=_small_config(),
    )

    assert (output_root / "m1" / "year=2026" / "month=01" / "bars_m1.parquet").exists()
    assert (output_root / "m1" / "year=2026" / "month=02" / "bars_m1.parquet").exists()
    assert (output_root / "bars_features_M5.parquet").exists()
    assert (output_root / "coverage_months.csv").exists()
    assert (output_root / "feature_summary.csv").exists()
    assert (output_root / "run_summary.json").exists()

    assert summary["totals"]["bars_m1"] == 15
    assert summary["totals"]["bars_m5"] == 3

    jan_table = pq.read_table(str(output_root / "m1" / "year=2026" / "month=01" / "bars_m1.parquet"))
    assert jan_table.num_rows == 10
    assert set(jan_table.column_names) >= {"tick_volume", "spread", "real_volume", "volume", "volume_quality"}

    metadata = jan_table.schema.metadata
    decoded = {k.decode(): v.decode() for k, v in metadata.items()}
    assert decoded["schema"] == "ep_market_hub.win_m1_bars"
    assert decoded["month"] == "2026-01"
    assert "generated_at_utc" not in decoded  # item 3: nenhum campo volátil no Parquet mensal

    features_table = pq.read_table(str(output_root / "bars_features_M5.parquet"))
    features_metadata = {k.decode(): v.decode() for k, v in features_table.schema.metadata.items()}
    assert features_metadata["schema"] == "ep_market_hub.win_m1_features_m5"
    assert features_metadata["source_id"] == SOURCE_ID
    assert features_metadata["resolved_symbol"] == SYMBOL
    assert "generated_at_utc" not in features_metadata


def test_run_m1_history_producer_aggregates_m1_to_m5_via_aggregate_bars(tmp_path: Path) -> None:
    provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    output_root, terminal_path = _output_and_terminal(tmp_path)

    run_m1_history_producer(
        provider=provider, terminal_path=terminal_path, inventory_path=inventory_path,
        inventory_sha256=expected_sha, output_root=output_root, source_id=SOURCE_ID, symbol=SYMBOL,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1), feature_config=_small_config(),
    )

    features_table = pq.read_table(str(output_root / "bars_features_M5.parquet")).to_pylist()
    assert [row["timeframe"] for row in features_table] == ["M5", "M5", "M5"]
    timestamps = [row["timestamp_utc"] for row in features_table]
    assert timestamps == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        datetime(2026, 2, 1, 0, 0, tzinfo=UTC),
    ]
    # Primeira barra M5 de janeiro: open do 1º M1 (5000.0), close do 5º M1 (5004.0).
    assert features_table[0]["open"] == pytest.approx(5000.0)
    assert features_table[0]["close"] == pytest.approx(5004.0)
    # As barras M1 desta fixture são todas volume_quality="exchange" (real_volume=0 >= 0).
    assert [row["volume_quality"] for row in features_table] == ["exchange", "exchange", "exchange"]


def test_available_at_utc_equals_bucket_close_and_blocks_lookahead() -> None:
    from market_analytics.bars import Bar

    bar_opened_at_0900 = Bar(
        source_id=SOURCE_ID, symbol=SYMBOL, timeframe="M5", timestamp=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
        open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0, volume_quality="tick_proxy",
    )
    bar_opened_at_0905 = Bar(
        source_id=SOURCE_ID, symbol=SYMBOL, timeframe="M5", timestamp=datetime(2026, 1, 5, 9, 5, tzinfo=UTC),
        open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0, volume_quality="tick_proxy",
    )

    assert available_at_utc(bar_opened_at_0900) == datetime(2026, 1, 5, 9, 5, tzinfo=UTC)
    assert available_at_utc(bar_opened_at_0905) == datetime(2026, 1, 5, 9, 10, tzinfo=UTC)

    # Decisão às 09:06: a barra aberta às 09:05 ainda não está disponível
    # (só fecha às 09:10); a última disponível é a aberta às 09:00.
    decision_instant = datetime(2026, 1, 5, 9, 6, tzinfo=UTC)
    usable = [bar for bar in (bar_opened_at_0900, bar_opened_at_0905) if available_at_utc(bar) <= decision_instant]
    assert usable == [bar_opened_at_0900]


def test_run_m1_history_producer_available_at_utc_column_matches_bucket_close(tmp_path: Path) -> None:
    provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    output_root, terminal_path = _output_and_terminal(tmp_path)

    run_m1_history_producer(
        provider=provider, terminal_path=terminal_path, inventory_path=inventory_path,
        inventory_sha256=expected_sha, output_root=output_root, source_id=SOURCE_ID, symbol=SYMBOL,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1), feature_config=_small_config(),
    )

    rows = pq.read_table(str(output_root / "bars_features_M5.parquet")).to_pylist()
    for row in rows:
        assert row["available_at_utc"] == row["timestamp_utc"] + timedelta(minutes=5)


def test_warmup_is_continuous_across_month_boundary_not_reset(tmp_path: Path) -> None:
    """Prova de ausência de reset artificial: com atr_period=2, a 3ª barra M5
    (1ª de fevereiro) só tem ATR não-None se a 2ª barra M5 (última de
    janeiro) ainda contribuiu com seu true range — ou seja, o warm-up
    atravessou a fronteira do mês em vez de reiniciar."""

    provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    output_root, terminal_path = _output_and_terminal(tmp_path)

    run_m1_history_producer(
        provider=provider, terminal_path=terminal_path, inventory_path=inventory_path,
        inventory_sha256=expected_sha, output_root=output_root, source_id=SOURCE_ID, symbol=SYMBOL,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1), feature_config=_small_config(),
    )

    rows = pq.read_table(str(output_root / "bars_features_M5.parquet")).to_pylist()
    assert rows[0]["atr"] is None  # só 1 true range disponível
    assert rows[1]["atr"] is not None  # 2 true ranges: warm-up completo dentro de janeiro
    assert rows[2]["atr"] is not None  # 3ª barra (fevereiro) usa TR da 2ª barra (janeiro): sem reset


def test_prefix_invariance_single_month_matches_prefix_of_two_month_run(tmp_path: Path) -> None:
    """A barra M5 de janeiro calculada isoladamente é idêntica à mesma barra
    calculada como prefixo da série contínua jan+fev — nenhuma informação de
    fevereiro poderia ter vazado para trás."""

    provider_jan_only = FakeRatesProvider({"2026-01": _jan_rows()})
    jan_fp = _fingerprint_of(_jan_rows())
    inventory_jan_path = tmp_path / "inv_jan.json"
    expected_sha_jan = _write_inventory(inventory_jan_path, {"2026-01": (10, jan_fp)})
    inventory_jan = load_inventory_file(inventory_jan_path, expected_sha256=expected_sha_jan)

    jan_only_outcomes = collect_validated_history(
        provider_jan_only, inventory_jan, symbol=SYMBOL, source_id=SOURCE_ID,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 2, 1),
    )
    from market_analytics.pipeline import compute_feature_rows
    from market_analytics.quant_mvp import aggregate_bars

    jan_only_m1 = [bar for outcome in jan_only_outcomes for bar in outcome.bars]
    jan_only_m5 = aggregate_bars(jan_only_m1, "M5")
    jan_only_features = compute_feature_rows(jan_only_m5, _small_config())

    provider_two_months, inventory_two_months, _path, _sha = _two_month_inventory_and_provider(tmp_path)
    two_month_outcomes = collect_validated_history(
        provider_two_months, inventory_two_months, symbol=SYMBOL, source_id=SOURCE_ID,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1),
    )
    two_month_m1 = [bar for outcome in two_month_outcomes for bar in outcome.bars]
    two_month_m5 = aggregate_bars(two_month_m1, "M5")
    two_month_features = compute_feature_rows(two_month_m5, _small_config())

    assert [row.to_dict() for row in jan_only_features] == [row.to_dict() for row in two_month_features[:2]]


def test_run_m1_history_producer_is_deterministic_across_two_runs(tmp_path: Path) -> None:
    """Item 3: duas execuções sobre a mesma entrada produzem artefatos
    byte-idênticos — inclusive os hashes registrados em run_summary.json,
    nunca ignorados/descartados na comparação."""

    provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    output_root_1, terminal_path = _output_and_terminal(tmp_path)
    output_root_2 = tmp_path / "analytics" / "win_m1_history_2"

    summary_1 = run_m1_history_producer(
        provider=provider, terminal_path=terminal_path, inventory_path=inventory_path,
        inventory_sha256=expected_sha, output_root=output_root_1, source_id=SOURCE_ID, symbol=SYMBOL,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1), feature_config=_small_config(),
    )
    summary_2 = run_m1_history_producer(
        provider=provider, terminal_path=terminal_path, inventory_path=inventory_path,
        inventory_sha256=expected_sha, output_root=output_root_2, source_id=SOURCE_ID, symbol=SYMBOL,
        start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1), feature_config=_small_config(),
    )

    # Só horário/duração de execução e o caminho absoluto de saída variam.
    volatile_keys = {"generated_at_utc", "duration_seconds", "output_root"}

    def _strip(summary: dict) -> dict:
        return {k: v for k, v in summary.items() if k not in volatile_keys}

    assert _strip(summary_1) == _strip(summary_2)
    # Hashes de artefato (incluindo os Parquets mensais M1) não são
    # ignorados: precisam bater de verdade, não só o resto do resumo.
    assert summary_1["artifacts"] == summary_2["artifacts"]

    for relative in (
        "bars_features_M5.parquet",
        "m1/year=2026/month=01/bars_m1.parquet",
        "m1/year=2026/month=02/bars_m1.parquet",
        "coverage_months.csv",
        "feature_summary.csv",
    ):
        bytes_1 = (output_root_1 / relative).read_bytes()
        bytes_2 = (output_root_2 / relative).read_bytes()
        assert bytes_1 == bytes_2, f"{relative} não é byte-idêntico entre execuções"


def test_run_m1_history_producer_rejects_output_root_overlapping_repo(tmp_path: Path) -> None:
    provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    _output_root, terminal_path = _output_and_terminal(tmp_path)

    with pytest.raises(HistoryProducerError, match="repositório"):
        run_m1_history_producer(
            provider=provider, terminal_path=terminal_path, inventory_path=inventory_path,
            inventory_sha256=expected_sha, output_root=win_m1_features._repo_root(),
            source_id=SOURCE_ID, symbol=SYMBOL,
            start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1),
        )


def test_run_m1_history_producer_rejects_output_root_overlapping_terminal_dir(tmp_path: Path) -> None:
    provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    terminal_dir = tmp_path / "terminal"
    terminal_dir.mkdir()

    with pytest.raises(HistoryProducerError, match="terminal"):
        run_m1_history_producer(
            provider=provider, terminal_path=str(terminal_dir / "terminal64.exe"), inventory_path=inventory_path,
            inventory_sha256=expected_sha, output_root=terminal_dir, source_id=SOURCE_ID, symbol=SYMBOL,
            start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1),
        )


def test_run_m1_history_producer_leaves_no_partial_artifact_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _inventory, inventory_path, expected_sha = _two_month_inventory_and_provider(tmp_path)
    output_root, terminal_path = _output_and_terminal(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("falha simulada ao escrever features")

    monkeypatch.setattr(win_m1_features, "_write_features_parquet", _boom)

    with pytest.raises(RuntimeError, match="falha simulada"):
        run_m1_history_producer(
            provider=provider, terminal_path=terminal_path, inventory_path=inventory_path,
            inventory_sha256=expected_sha, output_root=output_root, source_id=SOURCE_ID, symbol=SYMBOL,
            start_date=date(2026, 1, 1), end_date_exclusive=date(2026, 3, 1),
        )

    assert not output_root.exists()
    siblings = list(output_root.parent.iterdir()) if output_root.parent.exists() else []
    assert siblings == []


def test_promote_output_restores_previous_output_when_second_swap_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_root = tmp_path / "out"
    output_root.mkdir()
    (output_root / "marker.txt").write_text("saída antiga")
    temp_dir = tmp_path / ".win_m1_history_tmp_xyz"
    temp_dir.mkdir()
    (temp_dir / "marker.txt").write_text("saída nova")

    real_rename = win_m1_features.os.rename

    def fail_second_rename(src, dst):
        if Path(src) == temp_dir:
            raise OSError("falha simulada")
        return real_rename(src, dst)

    monkeypatch.setattr(win_m1_features.os, "rename", fail_second_rename)

    with pytest.raises(OSError):
        win_m1_features._promote_output(temp_dir, output_root)

    assert output_root.exists()
    assert (output_root / "marker.txt").read_text() == "saída antiga"
    siblings = {p.name for p in tmp_path.iterdir()}
    assert siblings == {"out", temp_dir.name}


def test_promote_output_moves_temp_dir_when_no_previous_output(tmp_path: Path) -> None:
    output_root = tmp_path / "out"
    temp_dir = tmp_path / ".win_m1_history_tmp_xyz"
    temp_dir.mkdir()
    (temp_dir / "marker.txt").write_text("saída nova")

    win_m1_features._promote_output(temp_dir, output_root)

    assert output_root.exists()
    assert (output_root / "marker.txt").read_text() == "saída nova"
    assert not temp_dir.exists()


# ---------------------------------------------------------------------------
# CLI: bootstrap de sys.path (item 4) — subprocess real, sem instalar o
# pacote e sem MT5 (só --help, que nunca chega a construir Mt5RatesProvider).
# ---------------------------------------------------------------------------


def test_cli_help_runs_without_module_not_found_error() -> None:
    script = REPO_ROOT / "tools" / "collect_win_m1_history.py"

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT.parent),
    )

    assert "ModuleNotFoundError" not in result.stderr
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
