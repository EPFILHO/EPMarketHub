"""Testes do MVP quantitativo WIN (DEV-006) com Parquets sintéticos pequenos.

Nenhum teste aqui toca `D:\\EPData`, um terminal MT5 real ou qualquer API
MT5/Qt: todas as fixtures são construídas em `tmp_path` com pyarrow puro.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_analytics import backfill_writer, quant_mvp
from market_analytics.config import FeatureConfig
from market_analytics.quant_mvp import (
    TIMEFRAMES,
    QuantMvpError,
    SessionRejectedError,
    SessionTickStats,
    discover_sessions,
    process_session,
    run_quant_mvp,
)

SOURCE_ID = "clear"
LOGICAL_ID = "win"
SYMBOL = "WIN$"


def _metadata(session_date: str, **overrides: str) -> dict[str, str]:
    base = {
        "schema": "ep_market_hub.raw_ticks",
        "schema_version": "1",
        "source_id": SOURCE_ID,
        "logical_id": LOGICAL_ID,
        "resolved_symbol": SYMBOL,
        "session_date": session_date,
    }
    base.update(overrides)
    return base


def _row(
    time_msc: int,
    *,
    last: float = 5000.0,
    bid: float = 4999.0,
    ask: float = 5001.0,
    volume: float = 1.0,
    volume_real: float = 10.0,
    flags: int = 1080,
) -> dict[str, object]:
    return {
        "time": time_msc // 1000,
        "time_msc": time_msc,
        "bid": bid,
        "ask": ask,
        "last": last,
        "volume": volume,
        "volume_real": volume_real,
        "flags": flags,
    }


def _session_path(root: Path, session_date: str) -> Path:
    year, month, _day = session_date.split("-")
    return root / f"year={year}" / f"month={month}" / f"session_date={session_date}" / "ticks.parquet"


def _write_ticks(path: Path, rows: list[dict[str, object]], metadata: dict[str, str]) -> None:
    """Escreve um `ticks.parquet` sintético diretamente via pyarrow.

    Ao contrário de `SessionTickWriter`/`records_to_table` (que validam e
    rejeitariam NaN/valores inválidos antes de escrever), esta função
    escreve exatamente o que for passado — necessário para simular ticks
    brutos anômalos (NaN, negativos) que a política de preço/volume do
    DEV-006 precisa saneou explicitamente na leitura.
    """

    schema = backfill_writer.build_schema(metadata=metadata)
    columns = {name: [row[name] for row in rows] for name, _dtype in backfill_writer.RAW_ARROW_FIELDS}
    table = pa.table(columns, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path))


def _ms(session_date: str, hour: int, minute: int, second: int = 0, millisecond: int = 0) -> int:
    year, month, day = (int(part) for part in session_date.split("-"))
    dt = datetime(year, month, day, hour, minute, second, millisecond * 1000, tzinfo=UTC)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Descoberta
# ---------------------------------------------------------------------------


def test_discover_sessions_only_matches_the_win_partition_layout(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    _write_ticks(_session_path(root, "2026-01-06"), [_row(_ms("2026-01-06", 13, 0))], _metadata("2026-01-06"))
    _write_ticks(_session_path(root, "2026-01-05"), [_row(_ms("2026-01-05", 13, 0))], _metadata("2026-01-05"))
    # Decoys que não devem ser descobertos.
    (root / "year=2026" / "month=01" / "not_a_session_dir").mkdir(parents=True)
    (root / "year=2026" / "month=01" / "not_a_session_dir" / "ticks.parquet").write_bytes(b"not parquet")
    (root / "README.txt").write_text("decoy")

    found = discover_sessions(root)

    assert [session_date.isoformat() for session_date, _path in found] == ["2026-01-05", "2026-01-06"]


# ---------------------------------------------------------------------------
# Agregação OHLCV, derivação de timeframes, política de preço/volume, dedup, tie
# ---------------------------------------------------------------------------


def test_process_session_builds_m1_and_derives_m5_with_price_volume_policy(tmp_path: Path) -> None:
    session_date = "2026-02-02"
    root = tmp_path / "raw" / "clear" / "win"
    path = _session_path(root, session_date)

    t0 = _ms(session_date, 13, 0, 0)
    rows = [
        _row(t0, last=5000.0, volume_real=10.0),  # bucket 13:00, válido
        _row(t0, last=5000.0, volume_real=10.0),  # duplicata exata adjacente
        _row(t0, last=5010.0, volume_real=3.0),  # empate de time_msc, conteúdo diferente: válido
        _row(t0 + 10_000, last=math.nan, volume_real=1.0),  # last inválido: descartado
        _row(t0 + 20_000, last=5005.0, volume_real=-5.0),  # volume_real inválido -> contribui 0
        _row(t0 + 65_000, last=5020.0, volume_real=20.0),  # bucket 13:01
        _row(t0 + 301_000, last=5030.0, volume_real=5.0),  # bucket 13:05 (novo M5)
    ]
    _write_ticks(path, rows, _metadata(session_date))

    outcome = process_session(path, session_date=date.fromisoformat(session_date))

    assert outcome.stats == SessionTickStats(ticks_read=7, ticks_valid=5, ticks_duplicated=1)

    m1_bars = outcome.bars_by_timeframe["M1"]
    assert len(m1_bars) == 3
    bucket_00, bucket_01, bucket_05 = m1_bars
    assert (bucket_00.open, bucket_00.high, bucket_00.low, bucket_00.close, bucket_00.volume) == (
        5000.0,
        5010.0,
        5000.0,
        5005.0,
        13.0,
    )
    assert (bucket_01.open, bucket_01.close, bucket_01.volume) == (5020.0, 5020.0, 20.0)
    assert (bucket_05.open, bucket_05.close, bucket_05.volume) == (5030.0, 5030.0, 5.0)

    m5_bars = outcome.bars_by_timeframe["M5"]
    assert len(m5_bars) == 2
    first, second = m5_bars
    assert (first.open, first.high, first.low, first.close, first.volume) == (5000.0, 5020.0, 5000.0, 5020.0, 33.0)
    assert (second.open, second.close, second.volume) == (5030.0, 5030.0, 5.0)

    for timeframe in ("M15", "M30", "H1"):
        assert len(outcome.bars_by_timeframe[timeframe]) == 1


def test_duplicate_exact_adjacent_across_batch_boundary_is_removed_once(tmp_path: Path) -> None:
    session_date = "2026-02-03"
    root = tmp_path / "raw" / "clear" / "win"
    path = _session_path(root, session_date)
    t0 = _ms(session_date, 10, 0, 0)
    rows = [
        _row(t0, last=5000.0),
        _row(t0, last=5000.0),  # duplicata exata; batch_size=1 força a fronteira aqui
        _row(t0 + 1_000, last=5001.0),
    ]
    _write_ticks(path, rows, _metadata(session_date))

    outcome = process_session(path, session_date=date.fromisoformat(session_date), batch_size=1)

    assert outcome.stats == SessionTickStats(ticks_read=3, ticks_valid=2, ticks_duplicated=1)


# ---------------------------------------------------------------------------
# Metadado inválido
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "outra.coisa"},
        {"schema_version": "99"},
        {"source_id": "fot"},
        {"logical_id": "wdo"},
        {"resolved_symbol": "WDO$"},
        {"session_date": "2026-02-09"},
    ],
)
def test_process_session_rejects_invalid_metadata(tmp_path: Path, overrides: dict[str, str]) -> None:
    session_date = "2026-02-08"
    root = tmp_path / "raw" / "clear" / "win"
    path = _session_path(root, session_date)
    metadata = _metadata(session_date)
    metadata.update(overrides)
    _write_ticks(path, [_row(_ms(session_date, 13, 0))], metadata)

    with pytest.raises(SessionRejectedError):
        process_session(path, session_date=date.fromisoformat(session_date))


def test_process_session_rejects_out_of_order_timestamps(tmp_path: Path) -> None:
    session_date = "2026-02-10"
    root = tmp_path / "raw" / "clear" / "win"
    path = _session_path(root, session_date)
    t0 = _ms(session_date, 13, 0)
    _write_ticks(path, [_row(t0), _row(t0 - 1_000)], _metadata(session_date))

    with pytest.raises(SessionRejectedError):
        process_session(path, session_date=date.fromisoformat(session_date))


def test_run_quant_mvp_skips_rejected_sessions_and_reports_alerts(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    good_date = "2026-03-01"
    bad_date = "2026-03-02"
    _write_ticks(
        _session_path(root, good_date),
        [_row(_ms(good_date, 13, 0)), _row(_ms(good_date, 13, 1))],
        _metadata(good_date),
    )
    _write_ticks(
        _session_path(root, bad_date),
        [_row(_ms(bad_date, 13, 0))],
        _metadata(bad_date, source_id="fot"),
    )

    output_root = tmp_path / "analytics" / "win_mvp"
    summary = run_quant_mvp(input_root=root, output_root=output_root)

    assert summary["totals"]["sessions_discovered"] == 2
    assert summary["totals"]["sessions_processed"] == 1
    assert summary["totals"]["sessions_rejected"] == 1
    assert len(summary["alerts"]) == 1
    assert bad_date in summary["alerts"][0] or "fot" in summary["alerts"][0]


# ---------------------------------------------------------------------------
# Pipeline completo: artefatos, determinismo, promoção atômica/limpeza
# ---------------------------------------------------------------------------


def _build_two_good_sessions(root: Path) -> None:
    for session_date, base_hour in (("2026-04-01", 13), ("2026-04-02", 14)):
        rows = [
            _row(_ms(session_date, base_hour, minute), last=5000.0 + minute, volume_real=float(minute + 1))
            for minute in range(3)
        ]
        _write_ticks(_session_path(root, session_date), rows, _metadata(session_date))


def test_run_quant_mvp_writes_all_expected_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    _build_two_good_sessions(root)
    output_root = tmp_path / "analytics" / "win_mvp"

    run_quant_mvp(input_root=root, output_root=output_root, feature_config=FeatureConfig(
        atr_period=2, volatility_window=2, trend_window=2, volume_window=2,
    ))

    for timeframe in TIMEFRAMES:
        assert (output_root / f"bars_features_{timeframe}.parquet").exists()
    assert (output_root / "session_summary.csv").exists()
    assert (output_root / "feature_summary.csv").exists()
    assert (output_root / "run_summary.json").exists()

    run_summary = json.loads((output_root / "run_summary.json").read_text(encoding="utf-8"))
    assert run_summary["schema"] == "ep_market_hub.quant_mvp_run"
    assert run_summary["totals"]["sessions_processed"] == 2
    # Reconciliação com os metadados do Parquet de origem.
    for entry in run_summary["inputs"]:
        assert entry["row_count"] == 3

    m1_table = pq.read_table(str(output_root / "bars_features_M1.parquet"))
    assert m1_table.num_rows == 6  # 2 sessões x 3 ticks (cada minuto vira uma barra M1)
    assert set(run_summary["artifacts"]) == {
        "bars_features_M1",
        "bars_features_M5",
        "bars_features_M15",
        "bars_features_M30",
        "bars_features_H1",
        "session_summary",
        "feature_summary",
    }


def test_run_quant_mvp_is_deterministic_across_two_runs(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    _build_two_good_sessions(root)
    output_root = tmp_path / "analytics" / "win_mvp"

    first = run_quant_mvp(input_root=root, output_root=output_root)
    first_m1 = (output_root / "bars_features_M1.parquet").read_bytes()
    first_session_csv = (output_root / "session_summary.csv").read_text(encoding="utf-8")
    first_feature_csv = (output_root / "feature_summary.csv").read_text(encoding="utf-8")

    second = run_quant_mvp(input_root=root, output_root=output_root)
    second_m1 = (output_root / "bars_features_M1.parquet").read_bytes()
    second_session_csv = (output_root / "session_summary.csv").read_text(encoding="utf-8")
    second_feature_csv = (output_root / "feature_summary.csv").read_text(encoding="utf-8")

    assert first_m1 == second_m1
    assert first_session_csv == second_session_csv
    assert first_feature_csv == second_feature_csv

    first.pop("generated_at_utc")
    first.pop("duration_seconds")
    second.pop("generated_at_utc")
    second.pop("duration_seconds")
    assert first == second


def test_run_quant_mvp_promotes_atomically_and_leaves_no_temp_dir_on_success(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    _build_two_good_sessions(root)
    output_root = tmp_path / "analytics" / "win_mvp"

    run_quant_mvp(input_root=root, output_root=output_root)

    leftovers = list(output_root.parent.glob(".quant_mvp_tmp_*"))
    assert leftovers == []


def test_run_quant_mvp_cleans_up_and_preserves_previous_output_on_failure(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    _build_two_good_sessions(root)
    output_root = tmp_path / "analytics" / "win_mvp"

    # Primeira execução bem-sucedida, para provar que uma falha subsequente
    # não apaga uma saída anterior válida.
    run_quant_mvp(input_root=root, output_root=output_root)
    previous_run_summary = (output_root / "run_summary.json").read_bytes()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("falha simulada de escrita")

    monkeypatch.setattr(quant_mvp, "_write_csv", _boom)

    with pytest.raises(RuntimeError):
        run_quant_mvp(input_root=root, output_root=output_root)

    assert (output_root / "run_summary.json").read_bytes() == previous_run_summary
    leftovers = list(output_root.parent.glob(".quant_mvp_tmp_*"))
    assert leftovers == []


def test_run_quant_mvp_raises_when_no_sessions_are_discovered(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    root.mkdir(parents=True)
    with pytest.raises(QuantMvpError):
        run_quant_mvp(input_root=root, output_root=tmp_path / "analytics" / "win_mvp")


def test_quant_mvp_module_never_imports_mt5_or_qt() -> None:
    source = Path(quant_mvp.__file__).read_text(encoding="utf-8")
    assert "import MetaTrader5" not in source
    assert "import PySide6" not in source
    assert "QWebChannel" not in source


# ---------------------------------------------------------------------------
# Auditoria Codex: normalização/rejeição de entrada-saída destrutiva (item 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch_size", [0, -1, -1000])
def test_run_quant_mvp_rejects_non_positive_batch_size(tmp_path: Path, batch_size: int) -> None:
    # Nada é lido/criado no disco antes da validação: o `input_root` sequer
    # precisa existir para este teste ser significativo.
    with pytest.raises(QuantMvpError):
        run_quant_mvp(
            input_root=tmp_path / "raw" / "clear" / "win",
            output_root=tmp_path / "analytics" / "win_mvp",
            batch_size=batch_size,
        )


def test_run_quant_mvp_rejects_output_equal_to_input(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    with pytest.raises(QuantMvpError):
        run_quant_mvp(input_root=root, output_root=root)


def test_run_quant_mvp_rejects_output_nested_inside_input(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    with pytest.raises(QuantMvpError):
        run_quant_mvp(input_root=root, output_root=root / "analytics" / "win_mvp")


def test_run_quant_mvp_rejects_input_nested_inside_output(tmp_path: Path) -> None:
    output_root = tmp_path / "analytics"
    input_root = output_root / "raw" / "clear" / "win"
    with pytest.raises(QuantMvpError):
        run_quant_mvp(input_root=input_root, output_root=output_root)


def test_run_quant_mvp_rejects_output_equal_to_a_volume_root(tmp_path: Path) -> None:
    volume_root = Path(tmp_path.anchor)
    with pytest.raises(QuantMvpError):
        run_quant_mvp(input_root=tmp_path / "raw" / "clear" / "win", output_root=volume_root)


def test_paths_overlap_helper_detects_both_directions(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = a / "b"
    assert quant_mvp._paths_overlap(a, a) is True
    assert quant_mvp._paths_overlap(a, b) is True
    assert quant_mvp._paths_overlap(b, a) is True
    assert quant_mvp._paths_overlap(tmp_path / "x", tmp_path / "y") is False


# ---------------------------------------------------------------------------
# Auditoria Codex: promoção transacional recuperável (item 2)
# ---------------------------------------------------------------------------


def test_promote_output_leaves_no_backup_dir_after_two_successful_runs(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    _build_two_good_sessions(root)
    output_root = tmp_path / "analytics" / "win_mvp"

    run_quant_mvp(input_root=root, output_root=output_root)
    run_quant_mvp(input_root=root, output_root=output_root)

    siblings = {p.name for p in output_root.parent.iterdir()}
    assert siblings == {"win_mvp"}


def test_promote_output_restores_previous_output_when_second_swap_fails(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    _build_two_good_sessions(root)
    output_root = tmp_path / "analytics" / "win_mvp"

    run_quant_mvp(input_root=root, output_root=output_root)
    previous_run_summary = (output_root / "run_summary.json").read_bytes()
    previous_m1 = (output_root / "bars_features_M1.parquet").read_bytes()

    real_rename = quant_mvp.os.rename
    call_count = {"n": 0}

    def flaky_rename(src, dst):
        call_count["n"] += 1
        # A 1ª chamada move a saída antiga para o backup (deve funcionar).
        # A 2ª chamada moveria o temporário para a saída final: falha aqui
        # simula exatamente "logo depois de mover a saída antiga".
        if call_count["n"] == 2:
            raise OSError("falha simulada exatamente após mover a saída antiga")
        return real_rename(src, dst)

    monkeypatch.setattr(quant_mvp.os, "rename", flaky_rename)

    with pytest.raises(OSError):
        run_quant_mvp(input_root=root, output_root=output_root)

    # A saída anterior foi restaurada integralmente, e nem backup nem
    # temporário sobraram órfãos ao lado dela.
    assert (output_root / "run_summary.json").read_bytes() == previous_run_summary
    assert (output_root / "bars_features_M1.parquet").read_bytes() == previous_m1
    siblings = {p.name for p in output_root.parent.iterdir()}
    assert siblings == {"win_mvp"}


def test_promote_output_unit_restores_backup_on_failure(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "out"
    output_root.mkdir()
    (output_root / "marker.txt").write_text("saída antiga")
    temp_dir = tmp_path / ".quant_mvp_tmp_xyz"
    temp_dir.mkdir()
    (temp_dir / "marker.txt").write_text("saída nova")

    real_rename = quant_mvp.os.rename

    def fail_second_rename(src, dst):
        if Path(src) == temp_dir:
            raise OSError("falha simulada")
        return real_rename(src, dst)

    monkeypatch.setattr(quant_mvp.os, "rename", fail_second_rename)

    with pytest.raises(OSError):
        quant_mvp._promote_output(temp_dir, output_root)

    assert output_root.exists()
    assert (output_root / "marker.txt").read_text() == "saída antiga"
    assert temp_dir.exists()  # não é responsabilidade de _promote_output limpar o temp
    siblings = {p.name for p in tmp_path.iterdir()}
    assert siblings == {"out", temp_dir.name}


def test_promote_output_unit_moves_temp_dir_when_no_previous_output(tmp_path: Path) -> None:
    output_root = tmp_path / "out"
    temp_dir = tmp_path / ".quant_mvp_tmp_xyz"
    temp_dir.mkdir()
    (temp_dir / "marker.txt").write_text("saída nova")

    quant_mvp._promote_output(temp_dir, output_root)

    assert output_root.exists()
    assert (output_root / "marker.txt").read_text() == "saída nova"
    assert not temp_dir.exists()


# ---------------------------------------------------------------------------
# Auditoria Codex: timestamps exatos de observação (item 3)
# ---------------------------------------------------------------------------


def test_session_summary_preserves_exact_first_and_last_tick_timestamps(tmp_path: Path) -> None:
    session_date = "2026-06-01"
    root = tmp_path / "raw" / "clear" / "win"
    path = _session_path(root, session_date)
    first_ms = _ms(session_date, 13, 0, 7, 500)  # 13:00:07.500 — não é o início do minuto
    last_ms = _ms(session_date, 13, 4, 59, 900)  # 13:04:59.900 — não é o início do minuto
    _write_ticks(
        path,
        [_row(first_ms, last=5000.0), _row(last_ms, last=5010.0)],
        _metadata(session_date),
    )

    outcome = process_session(path, session_date=date.fromisoformat(session_date))

    expected_first = datetime.fromtimestamp(first_ms / 1000.0, tz=UTC)
    expected_last = datetime.fromtimestamp(last_ms / 1000.0, tz=UTC)
    assert outcome.first_tick_utc == expected_first
    assert outcome.last_tick_utc == expected_last
    # Nunca o início do minuto da barra M1 correspondente.
    assert outcome.first_tick_utc != outcome.bars_by_timeframe["M1"][0].timestamp

    rows = quant_mvp.build_session_summary([outcome])
    assert rows[0]["first_observation_utc"] == expected_first.isoformat()
    assert rows[0]["last_observation_utc"] == expected_last.isoformat()


def test_session_summary_observation_is_none_without_valid_ticks(tmp_path: Path) -> None:
    session_date = "2026-06-02"
    root = tmp_path / "raw" / "clear" / "win"
    path = _session_path(root, session_date)
    _write_ticks(path, [_row(_ms(session_date, 13, 0), last=0.0)], _metadata(session_date))

    outcome = process_session(path, session_date=date.fromisoformat(session_date))

    assert outcome.first_tick_utc is None
    assert outcome.last_tick_utc is None
    rows = quant_mvp.build_session_summary([outcome])
    assert rows[0]["first_observation_utc"] is None
    assert rows[0]["last_observation_utc"] is None


# ---------------------------------------------------------------------------
# Auditoria Codex: timestamp_utc como tipo Arrow timestamp tz-aware (item 4)
# ---------------------------------------------------------------------------


def test_bars_features_parquet_stores_timestamp_as_arrow_timestamp(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "clear" / "win"
    _build_two_good_sessions(root)
    output_root = tmp_path / "analytics" / "win_mvp"

    run_quant_mvp(input_root=root, output_root=output_root)

    table = pq.read_table(str(output_root / "bars_features_M1.parquet"))
    field = table.schema.field("timestamp_utc")
    assert pa.types.is_timestamp(field.type)
    assert field.type.tz == "UTC"

    values = table.column("timestamp_utc").to_pylist()
    assert all(value.tzinfo is not None for value in values)
    assert values == sorted(values)
