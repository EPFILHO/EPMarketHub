"""Testes do adaptador de ticks contratuais -> M1 do Atlas (DEV-008B.1B) com
fixtures sintéticas. Nenhum teste aqui toca `D:\\EPData`, um terminal MT5 real
ou qualquer API MT5/Qt: tudo é construído em `tmp_path` com pyarrow puro.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_analytics import atlas_incremental, quant_mvp
from market_analytics.backfill_writer import RAW_ARROW_FIELDS, build_schema
from market_analytics.tick_atlas_adapter import (
    FIXED_ADJUSTMENT_METHOD,
    FIXED_SERIES_KIND,
    MANIFEST_SCHEMA,
    TickAdapterError,
    TickAdapterManifest,
    TickManifestError,
    TickRegressionError,
    load_tick_adapter_manifest_file,
    run_tick_atlas_adapter,
)

SOURCE_ID = "clear"
LOGICAL_ID = "test_contract"
SYMBOL = "TESTV26"
NOW = lambda: datetime(2026, 9, 10, 12, 0, tzinfo=UTC)  # noqa: E731


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


def _ms(session_date: str, hour: int, minute: int, second: int = 0) -> int:
    year, month, day = (int(part) for part in session_date.split("-"))
    dt = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _session_path(root: Path, session_date: str) -> Path:
    year, month, _day = session_date.split("-")
    return root / f"year={year}" / f"month={month}" / f"session_date={session_date}" / "ticks.parquet"


def _write_ticks(path: Path, rows: list[dict[str, object]], metadata: dict[str, str]) -> None:
    schema = build_schema(metadata=metadata)
    columns = {name: [row[name] for row in rows] for name, _dtype in RAW_ARROW_FIELDS}
    table = pa.table(columns, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path))


def _default_session_rows(session_date: str, *, n: int = 3, base_price: float = 5000.0) -> list[dict[str, object]]:
    return [
        _row(_ms(session_date, 13, minute), last=base_price + minute, volume_real=10.0 + minute)
        for minute in range(n)
    ]


def _write_session(root: Path, session_date: str, *, rows: list[dict[str, object]] | None = None, **meta_overrides: str) -> Path:
    path = _session_path(root, session_date)
    _write_ticks(path, rows if rows is not None else _default_session_rows(session_date), _metadata(session_date, **meta_overrides))
    return path


def _manifest(tmp_path: Path, **overrides: object) -> TickAdapterManifest:
    defaults: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "manifest_id": "test_contract_adapter",
        "input_root": str(tmp_path / "raw" / SOURCE_ID / LOGICAL_ID),
        "derived_m1_root": str(tmp_path / "derived"),
        "atlas_output_root": str(tmp_path / "atlas_out"),
        "logical_id": LOGICAL_ID,
        "source_id": SOURCE_ID,
        "resolved_symbol": SYMBOL,
        "contract_id": SYMBOL,
        "series_kind": FIXED_SERIES_KIND,
        "source_symbol": SYMBOL,
        "adjustment_method": FIXED_ADJUSTMENT_METHOD,
        "session_timezone": "America/Sao_Paulo",
        "expected_session_start": "09:00",
        "expected_session_end": "18:00",
    }
    defaults.update(overrides)
    return TickAdapterManifest.from_dict(defaults)


def _input_root(tmp_path: Path) -> Path:
    return tmp_path / "raw" / SOURCE_ID / LOGICAL_ID


def _derived_root(tmp_path: Path) -> Path:
    return tmp_path / "derived" / LOGICAL_ID


def _current_generation_root(tmp_path: Path) -> Path:
    derived_root = _derived_root(tmp_path)
    current = json.loads((derived_root / "current.json").read_text(encoding="utf-8"))
    return derived_root / "generations" / current["current_generation"]


def _published_m1_path(tmp_path: Path, session_date: str) -> Path:
    state = json.loads((_current_generation_root(tmp_path) / "state.json").read_text(encoding="utf-8"))
    return Path(state["sessions"][session_date]["m1_path"])


# ---------------------------------------------------------------------------
# 1. Paridade exata do M1 com process_session (mesmo núcleo reaproveitado)
# ---------------------------------------------------------------------------


def test_read_session_ticks_to_m1_matches_process_session_exactly(tmp_path: Path) -> None:
    session_date = "2026-08-20"
    root = tmp_path / "raw" / "clear" / "win"
    path = _session_path(root, session_date)
    rows = [
        _row(_ms(session_date, 13, 0), last=5000.0, volume_real=10.0),
        _row(_ms(session_date, 13, 0), last=5000.0, volume_real=10.0),  # duplicata exata
        _row(_ms(session_date, 13, 1), last=5010.0, volume_real=5.0),
    ]
    metadata = {
        "schema": "ep_market_hub.raw_ticks",
        "schema_version": "1",
        "source_id": "clear",
        "logical_id": "win",
        "resolved_symbol": "WIN$",
        "session_date": session_date,
    }
    _write_ticks(path, rows, metadata)

    outcome = quant_mvp.process_session(path, session_date=date.fromisoformat(session_date))
    generic = quant_mvp.read_session_ticks_to_m1(
        path,
        session_date=date.fromisoformat(session_date),
        source_id="clear",
        symbol="WIN$",
        validate_metadata=quant_mvp.validate_session_metadata,
    )

    assert generic.m1_bars == outcome.bars_by_timeframe["M1"]
    assert generic.stats == outcome.stats
    assert generic.first_tick_utc == outcome.first_tick_utc
    assert generic.last_tick_utc == outcome.last_tick_utc


# ---------------------------------------------------------------------------
# 2. Leitura em batches / dedup na fronteira
# ---------------------------------------------------------------------------


def test_read_session_ticks_to_m1_dedups_across_batch_boundary(tmp_path: Path) -> None:
    session_date = "2026-08-20"
    path = _session_path(tmp_path / "raw" / SOURCE_ID / LOGICAL_ID, session_date)
    rows = [
        _row(_ms(session_date, 13, 0, 0), last=5000.0, volume_real=1.0),
        _row(_ms(session_date, 13, 0, 0), last=5000.0, volume_real=1.0),  # duplicata adjacente
        _row(_ms(session_date, 13, 0, 1), last=5001.0, volume_real=1.0),
        _row(_ms(session_date, 13, 0, 2), last=5002.0, volume_real=1.0),
    ]
    _write_ticks(path, rows, _metadata(session_date))

    def _validate(metadata, *, path, session_date):
        quant_mvp.validate_raw_tick_metadata(
            metadata,
            path=path,
            session_date=session_date,
            expected_source_id=SOURCE_ID,
            expected_logical_id=LOGICAL_ID,
            expected_resolved_symbol=SYMBOL,
        )

    result = quant_mvp.read_session_ticks_to_m1(
        path,
        session_date=date.fromisoformat(session_date),
        source_id=SOURCE_ID,
        symbol=SYMBOL,
        batch_size=1,  # força que a duplicata caia em batches distintos
        validate_metadata=_validate,
    )

    assert result.stats == quant_mvp.SessionTickStats(ticks_read=4, ticks_valid=3, ticks_duplicated=1)
    assert len(result.m1_bars) == 1
    assert (result.m1_bars[0].open, result.m1_bars[0].close) == (5000.0, 5002.0)


# ---------------------------------------------------------------------------
# 3. Primeira execução
# ---------------------------------------------------------------------------


def test_first_execution_materializes_all_eligible_sessions(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    _write_session(root, "2026-09-02")
    manifest = _manifest(tmp_path)

    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert report["status"] == "append"
    assert report["sessions_added"] == ["2026-09-01", "2026-09-02"]
    assert report["sessions_recalculated"] == []
    assert report["sessions_rejected"] == []

    for session_date in ("2026-09-01", "2026-09-02"):
        assert _published_m1_path(tmp_path, session_date).is_file()
    first_m1 = _published_m1_path(tmp_path, "2026-09-01")
    first_table = pq.read_table(first_m1, columns=["timestamp_utc"])
    assert first_table.column("timestamp_utc")[0].as_py().hour == 10  # 13:00 UTC -> 10:00 São Paulo
    metadata = {
        key.decode(): value.decode()
        for key, value in (pq.ParquetFile(first_m1).schema_arrow.metadata or {}).items()
    }
    assert metadata["timestamp_policy"] == "session_wall_clock_relabelled_utc"
    assert (_current_generation_root(tmp_path) / "state.json").is_file()
    atlas_manifest_path = Path(report["atlas_manifest_path"])
    assert atlas_manifest_path.is_file()

    loaded = atlas_incremental.load_materialization_manifest_file(atlas_manifest_path)
    assert loaded.output_root == str(tmp_path / "atlas_out")
    dataset = loaded.dataset(LOGICAL_ID)
    assert dataset.series_kind == "individual_contract"
    assert dataset.contract_id == SYMBOL
    assert len(dataset.segments) == 2
    for segment in dataset.segments:
        assert segment.allowed_start_date == segment.allowed_end_date
        assert segment.expected_sha256 is not None


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    manifest = _manifest(tmp_path)

    report = run_tick_atlas_adapter(manifest=manifest, dry_run=True, now=NOW)

    assert report["status"] == "append"
    assert report["sessions_added"] == ["2026-09-01"]
    assert not _derived_root(tmp_path).exists()


# ---------------------------------------------------------------------------
# 4. no_change preserva bytes/mtimes
# ---------------------------------------------------------------------------


def test_second_identical_run_is_no_change_and_preserves_bytes(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    _write_session(root, "2026-09-02")
    manifest = _manifest(tmp_path)

    run_tick_atlas_adapter(manifest=manifest, now=NOW)
    derived_root = _derived_root(tmp_path)
    m1_paths = sorted((derived_root / "objects").glob("*.parquet"))
    mtimes_before = {p: p.stat().st_mtime_ns for p in m1_paths}
    generation_root = _current_generation_root(tmp_path)
    state_path = generation_root / "state.json"
    state_before = state_path.read_bytes()
    state_mtime_before = state_path.stat().st_mtime_ns
    manifest_path = generation_root / "atlas_materialization_manifest.json"
    manifest_before = manifest_path.read_bytes()
    manifest_mtime_before = manifest_path.stat().st_mtime_ns

    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert report["status"] == "no_change"
    assert report["sessions_added"] == []
    assert report["sessions_recalculated"] == []
    assert {p: p.stat().st_mtime_ns for p in m1_paths} == mtimes_before
    assert state_path.read_bytes() == state_before
    assert state_path.stat().st_mtime_ns == state_mtime_before
    assert manifest_path.read_bytes() == manifest_before
    assert manifest_path.stat().st_mtime_ns == manifest_mtime_before


# ---------------------------------------------------------------------------
# 5. Append
# ---------------------------------------------------------------------------


def test_append_only_touches_new_session(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    manifest = _manifest(tmp_path)
    run_tick_atlas_adapter(manifest=manifest, now=NOW)

    first_m1 = _published_m1_path(tmp_path, "2026-09-01")
    mtime_before = first_m1.stat().st_mtime_ns

    _write_session(root, "2026-09-02")
    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert report["status"] == "append"
    assert report["sessions_added"] == ["2026-09-02"]
    assert report["sessions_recalculated"] == []
    assert first_m1.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# 6. Correção histórica
# ---------------------------------------------------------------------------


def test_historical_correction_recalculates_only_changed_session(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    _write_session(root, "2026-09-02")
    manifest = _manifest(tmp_path)
    first = run_tick_atlas_adapter(manifest=manifest, now=NOW)
    assert first["status"] == "append"

    untouched_m1 = _published_m1_path(tmp_path, "2026-09-02")
    untouched_mtime = untouched_m1.stat().st_mtime_ns
    changed_m1 = _published_m1_path(tmp_path, "2026-09-01")
    bytes_before = changed_m1.read_bytes()

    _write_session(root, "2026-09-01", rows=_default_session_rows("2026-09-01", base_price=6000.0))
    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert report["status"] == "historical_correction"
    assert report["sessions_recalculated"] == ["2026-09-01"]
    assert report["sessions_added"] == []
    changed_m1_after = _published_m1_path(tmp_path, "2026-09-01")
    assert changed_m1_after != changed_m1
    assert changed_m1_after.read_bytes() != bytes_before
    assert changed_m1.read_bytes() == bytes_before  # objeto antigo continua válido/imutável
    assert untouched_m1.stat().st_mtime_ns == untouched_mtime


def test_manifest_change_creates_generation_without_rewriting_identical_m1(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    first_manifest = _manifest(tmp_path)
    first = run_tick_atlas_adapter(manifest=first_manifest, now=NOW)
    first_m1 = _published_m1_path(tmp_path, "2026-09-01")
    first_mtime = first_m1.stat().st_mtime_ns

    changed_manifest = _manifest(tmp_path, checkpoint_minutes=[10, 30, 60])
    changed = run_tick_atlas_adapter(manifest=changed_manifest, now=NOW)

    assert changed["status"] == "historical_correction"
    assert changed["previous_generation"] == first["new_generation"]
    assert changed["new_generation"] != first["new_generation"]
    assert _published_m1_path(tmp_path, "2026-09-01") == first_m1
    assert first_m1.stat().st_mtime_ns == first_mtime


def test_corrupted_published_m1_fails_closed(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    manifest = _manifest(tmp_path)
    run_tick_atlas_adapter(manifest=manifest, now=NOW)
    _published_m1_path(tmp_path, "2026-09-01").write_bytes(b"corrompido")

    with pytest.raises(TickAdapterError, match="objeto M1 existente corrompido"):
        run_tick_atlas_adapter(manifest=manifest, now=NOW)


# ---------------------------------------------------------------------------
# 7. Remoção falha fechado
# ---------------------------------------------------------------------------


def test_removed_published_session_fails_closed(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    _write_session(root, "2026-09-02")
    manifest = _manifest(tmp_path)
    run_tick_atlas_adapter(manifest=manifest, now=NOW)

    import shutil

    shutil.rmtree(_session_path(root, "2026-09-01").parent)

    with pytest.raises(TickRegressionError):
        run_tick_atlas_adapter(manifest=manifest, now=NOW)


def test_published_session_becoming_corrupted_fails_closed(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    manifest = _manifest(tmp_path)
    run_tick_atlas_adapter(manifest=manifest, now=NOW)

    _session_path(root, "2026-09-01").write_bytes(b"not parquet")

    with pytest.raises(TickRegressionError):
        run_tick_atlas_adapter(manifest=manifest, now=NOW)


# ---------------------------------------------------------------------------
# 8. Identidade divergente / corrompido / sem tick válido: rejeitados sem publicação
# ---------------------------------------------------------------------------


def test_new_session_with_divergent_identity_is_rejected_not_published(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01", logical_id="other_contract")
    manifest = _manifest(tmp_path)

    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert report["status"] == "no_change"
    assert report["sessions_added"] == []
    assert len(report["sessions_rejected"]) == 1
    assert report["sessions_rejected"][0]["session_date"] == "2026-09-01"
    assert not _derived_root(tmp_path).joinpath("session_date=2026-09-01").exists()


def test_new_session_corrupted_file_is_rejected_not_published(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    path = _session_path(root, "2026-09-01")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not parquet")
    manifest = _manifest(tmp_path)

    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert report["status"] == "no_change"
    assert len(report["sessions_rejected"]) == 1


def test_new_session_without_valid_ticks_is_rejected_not_published(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    rows = [_row(_ms("2026-09-01", 13, minute), last=math.nan) for minute in range(3)]
    _write_session(root, "2026-09-01", rows=rows)
    manifest = _manifest(tmp_path)

    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert report["status"] == "no_change"
    assert len(report["sessions_rejected"]) == 1
    assert "válido" in report["sessions_rejected"][0]["reason"]


# ---------------------------------------------------------------------------
# 9. Sessão corrente excluída
# ---------------------------------------------------------------------------


def test_current_session_is_excluded(tmp_path: Path) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    _write_session(root, "2026-09-10")  # == NOW().date()
    manifest = _manifest(tmp_path)

    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert report["sessions_added"] == ["2026-09-01"]
    assert report["sessions_excluded_current"] == ["2026-09-10"]
    assert not _derived_root(tmp_path).joinpath("session_date=2026-09-10").exists()


# ---------------------------------------------------------------------------
# 10. Interrupção preserva estado/manifesto anterior
# ---------------------------------------------------------------------------


def test_interruption_before_state_commit_preserves_previous_state(tmp_path: Path, monkeypatch) -> None:
    root = _input_root(tmp_path)
    _write_session(root, "2026-09-01")
    manifest = _manifest(tmp_path)
    run_tick_atlas_adapter(manifest=manifest, now=NOW)

    derived_root = _derived_root(tmp_path)
    generation_before = _current_generation_root(tmp_path)
    state_path_before = generation_before / "state.json"
    state_before = state_path_before.read_bytes()
    current_before = (derived_root / "current.json").read_bytes()

    _write_session(root, "2026-09-02")

    import market_analytics.tick_atlas_adapter as tick_atlas_adapter

    original_write = tick_atlas_adapter._atomic_write_json

    def _boom(path, payload):
        if path.name == "current.json":
            raise OSError("falha simulada antes do commit de current.json")
        return original_write(path, payload)

    monkeypatch.setattr(tick_atlas_adapter, "_atomic_write_json", _boom)

    with pytest.raises(OSError):
        run_tick_atlas_adapter(manifest=manifest, now=NOW)

    assert (derived_root / "current.json").read_bytes() == current_before
    assert state_path_before.read_bytes() == state_before

    monkeypatch.setattr(tick_atlas_adapter, "_atomic_write_json", original_write)
    report = run_tick_atlas_adapter(manifest=manifest, now=NOW)
    assert report["status"] == "append"
    assert report["sessions_added"] == ["2026-09-02"]


# ---------------------------------------------------------------------------
# Manifesto do adaptador: estrito, versionado, falha fechado em campo desconhecido
# ---------------------------------------------------------------------------


def test_manifest_rejects_unknown_field(tmp_path: Path) -> None:
    with pytest.raises(TickManifestError):
        _manifest(tmp_path, unexpected_field="x")


def test_manifest_rejects_wrong_series_kind(tmp_path: Path) -> None:
    with pytest.raises(TickManifestError):
        _manifest(tmp_path, series_kind="continuous_proportional")


def test_manifest_rejects_wrong_adjustment_method(tmp_path: Path) -> None:
    with pytest.raises(TickManifestError):
        _manifest(tmp_path, adjustment_method="proportional")


def test_load_tick_adapter_manifest_file_round_trips(tmp_path: Path) -> None:
    manifest_dict = {
        "schema": MANIFEST_SCHEMA,
        "manifest_id": "test_contract_adapter",
        "input_root": str(tmp_path / "raw" / SOURCE_ID / LOGICAL_ID),
        "derived_m1_root": str(tmp_path / "derived"),
        "atlas_output_root": str(tmp_path / "atlas_out"),
        "logical_id": LOGICAL_ID,
        "source_id": SOURCE_ID,
        "resolved_symbol": SYMBOL,
        "contract_id": SYMBOL,
        "series_kind": FIXED_SERIES_KIND,
        "source_symbol": SYMBOL,
        "adjustment_method": FIXED_ADJUSTMENT_METHOD,
        "session_timezone": "America/Sao_Paulo",
        "expected_session_start": "09:00",
        "expected_session_end": "18:00",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_dict), encoding="utf-8")

    manifest = load_tick_adapter_manifest_file(manifest_path)
    assert manifest.logical_id == LOGICAL_ID
    assert manifest.contract_id == SYMBOL
