"""Testes do materializador incremental do Atlas (DEV-008B.1A) com fixtures
sintéticas. Nenhum dado real, MT5 ou caminho `D:\\EPData` é usado aqui —
os segmentos M1 Parquet são gerados em memória, num `tmp_path` isolado.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psutil
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_analytics.atlas_incremental import (
    DatasetLock,
    DatasetSpec,
    LockError,
    ManifestError,
    MaterializationError,
    RegressionError,
    SegmentSpec,
    SegmentValidationError,
    assert_output_outside_repo,
    load_materialization_manifest_file,
    materialize_dataset,
)

SESSION_START = "12:00"
SESSION_END = "13:00"  # 60 minutos, uma barra por minuto
SESSION_MINUTES = 60


def _session_rows(
    session_date: str,
    start_hhmm: str,
    minutes: int,
    base_price: float,
    *,
    symbol: str = "SYN$",
    source_id: str = "SYN",
    quality: str = "exchange",
) -> list[dict]:
    """Sessão sintética com um pequeno "V", uma barra por minuto, sem furos --
    mesma forma usada em `tests/test_causal_atlas.py`."""

    hour, minute = int(start_hhmm[:2]), int(start_hhmm[3:5])
    start = datetime.fromisoformat(f"{session_date}T00:00:00+00:00") + timedelta(hours=hour, minutes=minute)
    rows: list[dict] = []
    price = base_price
    for index in range(minutes):
        drift = 0.5 if index < minutes / 2 else -0.5
        price = price + drift
        timestamp = start + timedelta(minutes=index)
        rows.append(
            {
                "timestamp": timestamp,
                "open": price - 0.25,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price,
                "volume": 100.0 + index,
                "volume_quality": quality,
                "symbol": symbol,
                "source_id": source_id,
            }
        )
    return rows


def _write_segment_parquet(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp_utc": pa.array([row["timestamp"] for row in rows], type=pa.timestamp("us", tz="UTC")),
            "open": [row["open"] for row in rows],
            "high": [row["high"] for row in rows],
            "low": [row["low"] for row in rows],
            "close": [row["close"] for row in rows],
            "volume": [row["volume"] for row in rows],
            "volume_quality": [row["volume_quality"] for row in rows],
            "symbol": [row["symbol"] for row in rows],
            "source_id": [row["source_id"] for row in rows],
        }
    )
    pq.write_table(table, str(path))


def _write_sessions(
    path: Path,
    session_dates: list[str],
    *,
    source_id: str = "SYN",
    symbol: str = "SYN$",
    quality: str = "exchange",
    base_prices: list[float] | None = None,
) -> None:
    if base_prices is None:
        base_prices = [100_000.0 + index * 50 for index in range(len(session_dates))]
    rows: list[dict] = []
    for session_date, price in zip(session_dates, base_prices, strict=True):
        rows.extend(
            _session_rows(session_date, SESSION_START, SESSION_MINUTES, price, symbol=symbol, source_id=source_id, quality=quality)
        )
    _write_segment_parquet(path, rows)


def _segment(
    segment_id: str, path: Path, source_id: str, start_date: str, end_date: str, expected_sha256: str | None = None
) -> SegmentSpec:
    return SegmentSpec(
        segment_id=segment_id,
        path=str(path),
        source_id=source_id,
        allowed_start_date=date.fromisoformat(start_date),
        allowed_end_date=date.fromisoformat(end_date),
        expected_sha256=expected_sha256,
    )


def _dataset(segments: list[SegmentSpec], **overrides) -> DatasetSpec:
    defaults = dict(
        logical_id="syn",
        symbol="SYN$",
        series_kind="continuous_proportional",
        source_symbol="SYN$",
        adjustment_method="proportional",
        expected_session_start=SESSION_START,
        expected_session_end=SESSION_END,
        input_kind="m1_segments",
        segments=tuple(segments),
    )
    defaults.update(overrides)
    return DatasetSpec(**defaults)


def _generation_sessions(dataset_root: Path, run_id: str) -> dict[str, str]:
    manifest = json.loads((dataset_root / "generations" / run_id / "manifest.json").read_text(encoding="utf-8"))
    return {item["session_date"]: item["object_sha256"] for item in manifest["sessions"]}


# --- 1. primeira execução -----------------------------------------------


def test_first_execution_materializes_all_sessions(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02", "2026-02-03", "2026-02-04"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out" / "syn"

    report = materialize_dataset(_dataset([segment]), dataset_root)

    assert report.status == "append"
    assert report.total_sessions == 3
    assert report.sessions_added == ["2026-02-02", "2026-02-03", "2026-02-04"]
    assert (dataset_root / "current.json").is_file()
    assert len(list((dataset_root / "generations").iterdir())) == 1
    assert len(list((dataset_root / "objects").glob("*.json"))) == 3


# --- 2. execução idêntica: no_change, bytes/mtimes preservados -----------


def test_second_identical_execution_is_no_change_and_preserves_bytes(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02", "2026-02-03"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out" / "syn"

    first = materialize_dataset(_dataset([segment]), dataset_root)
    object_files = sorted((dataset_root / "objects").glob("*.json"))
    mtimes_before = {path: path.stat().st_mtime_ns for path in object_files}
    current_before = (dataset_root / "current.json").read_bytes()

    second = materialize_dataset(_dataset([segment]), dataset_root)

    assert second.status == "no_change"
    assert second.new_generation == first.new_generation
    assert {path: path.stat().st_mtime_ns for path in object_files} == mtimes_before
    assert (dataset_root / "current.json").read_bytes() == current_before
    assert len(list((dataset_root / "generations").iterdir())) == 1


# --- 3. append cria só os objetos novos -----------------------------------


def test_append_creates_only_new_objects_and_generation(tmp_path):
    seg1_path = tmp_path / "seg1.parquet"
    _write_sessions(seg1_path, ["2026-02-02", "2026-02-03"])
    segment1 = _segment("seg1", seg1_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out" / "syn"
    materialize_dataset(_dataset([segment1]), dataset_root)

    objects_before = set((dataset_root / "objects").glob("*.json"))

    seg2_path = tmp_path / "seg2.parquet"
    _write_sessions(seg2_path, ["2026-03-02"], base_prices=[100_200.0])
    segment2 = _segment("seg2", seg2_path, "SYN", "2026-03-01", "2026-03-31")

    second = materialize_dataset(_dataset([segment1, segment2]), dataset_root)

    assert second.status == "append"
    assert second.sessions_added == ["2026-03-02"]
    assert second.sessions_recalculated == []
    objects_after = set((dataset_root / "objects").glob("*.json"))
    assert len(objects_after - objects_before) == 1
    for path in objects_before:
        assert path.exists()


# --- 4. correção histórica: preserva prefixo, recalcula sufixo -----------


def test_historical_correction_preserves_prefix_and_recalculates_suffix(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    dates = ["2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05"]
    base_prices = [100_000.0, 100_050.0, 100_100.0, 100_150.0]
    _write_sessions(seg_path, dates, base_prices=base_prices)
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out" / "syn"

    first = materialize_dataset(_dataset([segment]), dataset_root)
    sessions_before = _generation_sessions(dataset_root, first.new_generation)

    corrected_prices = list(base_prices)
    corrected_prices[1] = 100_900.0  # só a sessão 2026-02-03 muda
    _write_sessions(seg_path, dates, base_prices=corrected_prices)

    second = materialize_dataset(_dataset([segment]), dataset_root)

    assert second.status == "historical_correction"
    assert second.first_changed_session_date == "2026-02-03"
    assert second.sessions_recalculated == ["2026-02-03", "2026-02-04", "2026-02-05"]
    assert second.sessions_added == []

    sessions_after = _generation_sessions(dataset_root, second.new_generation)
    assert sessions_after["2026-02-02"] == sessions_before["2026-02-02"]
    assert sessions_after["2026-02-03"] != sessions_before["2026-02-03"]
    assert sessions_after["2026-02-04"] != sessions_before["2026-02-04"]


def test_historical_session_insertion_is_correction_not_regression(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02", "2026-02-04"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out"
    materialize_dataset(_dataset([segment]), dataset_root)

    _write_sessions(seg_path, ["2026-02-02", "2026-02-03", "2026-02-04"])
    report = materialize_dataset(_dataset([segment]), dataset_root)

    assert report.status == "historical_correction"
    assert report.first_changed_session_date == "2026-02-03"
    assert report.sessions_added == ["2026-02-03"]
    assert report.sessions_recalculated == ["2026-02-04"]


# --- 5. remoção/regressão falha fechado -----------------------------------


def test_removal_or_regression_fails_closed(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    dates = ["2026-02-02", "2026-02-03", "2026-02-04"]
    _write_sessions(seg_path, dates)
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out" / "syn"
    materialize_dataset(_dataset([segment]), dataset_root)

    _write_sessions(seg_path, dates[:2])

    with pytest.raises(RegressionError):
        materialize_dataset(_dataset([segment]), dataset_root)


# --- 6. interrupção antes do ponteiro preserva a geração anterior --------


def test_interruption_before_promotion_keeps_previous_generation(tmp_path, monkeypatch):
    seg_path = tmp_path / "seg1.parquet"
    dates = ["2026-02-02", "2026-02-03"]
    _write_sessions(seg_path, dates)
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out" / "syn"

    first = materialize_dataset(_dataset([segment]), dataset_root)
    current_before = (dataset_root / "current.json").read_bytes()

    _write_sessions(seg_path, dates + ["2026-02-04"])

    import market_analytics.atlas_incremental as atlas_incremental

    original_write = atlas_incremental._atomic_write_json

    def _boom(path, payload):
        if path.name == "current.json":
            raise OSError("falha simulada antes da promoção")
        return original_write(path, payload)

    monkeypatch.setattr(atlas_incremental, "_atomic_write_json", _boom)

    with pytest.raises(OSError):
        materialize_dataset(_dataset([segment]), dataset_root)

    assert (dataset_root / "current.json").read_bytes() == current_before

    monkeypatch.setattr(atlas_incremental, "_atomic_write_json", original_write)
    again = materialize_dataset(_dataset([segment]), dataset_root)
    assert again.status == "append"
    assert again.previous_generation == first.new_generation


# --- 7. lock órfão recuperável, lock vivo não roubado ---------------------


def test_orphan_lock_is_recovered_and_live_lock_is_not_stolen(tmp_path):
    dataset_root = tmp_path / "syn"
    dataset_root.mkdir(parents=True)
    lock_path = dataset_root / "lock.json"

    real_pid = os.getpid()
    real_started_at = psutil.Process(real_pid).create_time()

    lock_path.write_text(
        json.dumps({"schema": "x", "pid": real_pid, "process_started_at": real_started_at, "acquired_at_utc": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(LockError):
        DatasetLock(dataset_root).acquire()

    lock_path.write_text(
        json.dumps(
            {"schema": "x", "pid": real_pid, "process_started_at": real_started_at - 999_999, "acquired_at_utc": "x"}
        ),
        encoding="utf-8",
    )
    lock = DatasetLock(dataset_root)
    lock.acquire()
    lock.release()


# --- 8. segmento sobreposto ou identidade divergente é recusado ----------


def test_overlapping_segments_are_refused(tmp_path):
    seg1 = _segment("seg1", tmp_path / "a.parquet", "SYN", "2026-02-01", "2026-02-15")
    seg2 = _segment("seg2", tmp_path / "b.parquet", "SYN", "2026-02-10", "2026-02-28")
    with pytest.raises(ManifestError):
        _dataset([seg1, seg2])


def test_segment_symbol_divergent_from_dataset_is_refused(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02"], symbol="OTHER$")
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    with pytest.raises(SegmentValidationError):
        materialize_dataset(_dataset([segment]), tmp_path / "out")


def test_segment_expected_sha256_mismatch_is_refused(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28", expected_sha256="0" * 64)
    with pytest.raises(SegmentValidationError):
        materialize_dataset(_dataset([segment]), tmp_path / "out")


def test_non_m1_timeframe_is_refused_when_column_is_present(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02"])
    table = pq.read_table(seg_path).append_column("timeframe", pa.array(["M5"] * SESSION_MINUTES))
    pq.write_table(table, seg_path)
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")

    with pytest.raises(SegmentValidationError, match="não é M1"):
        materialize_dataset(_dataset([segment]), tmp_path / "out")


def test_current_session_is_not_materialized(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")

    with pytest.raises(SegmentValidationError, match="somente sessões anteriores"):
        materialize_dataset(
            _dataset([segment]),
            tmp_path / "out",
            now=lambda: datetime(2026, 2, 2, 23, 0, tzinfo=UTC),
        )


# --- 9. transição de fonte só entre sessões, e auditada -------------------


def test_source_transition_between_segments_is_audited(tmp_path):
    seg1_path = tmp_path / "seg1.parquet"
    _write_sessions(seg1_path, ["2026-02-02"], source_id="alpha")
    seg2_path = tmp_path / "seg2.parquet"
    _write_sessions(seg2_path, ["2026-02-03"], source_id="beta", base_prices=[100_050.0])
    segment1 = _segment("seg1", seg1_path, "alpha", "2026-02-01", "2026-02-02")
    segment2 = _segment("seg2", seg2_path, "beta", "2026-02-03", "2026-02-28")

    report = materialize_dataset(_dataset([segment1, segment2]), tmp_path / "out")

    assert report.issues.get("source_transition") == 1


# --- 10. saída incremental == reconstrução integral limpa -----------------


def test_incremental_output_matches_full_clean_rebuild(tmp_path):
    dates_a = ["2026-02-02", "2026-02-03"]
    dates_b = ["2026-02-04", "2026-02-05"]
    seg_a_path = tmp_path / "seg_a.parquet"
    seg_b_path = tmp_path / "seg_b.parquet"
    _write_sessions(seg_a_path, dates_a, base_prices=[100_000.0, 100_050.0])
    _write_sessions(seg_b_path, dates_b, base_prices=[100_100.0, 100_150.0])
    segment_a = _segment("seg_a", seg_a_path, "SYN", "2026-02-01", "2026-02-03")
    segment_b = _segment("seg_b", seg_b_path, "SYN", "2026-02-04", "2026-02-28")

    full_root = tmp_path / "full"
    full_report = materialize_dataset(_dataset([segment_a, segment_b]), full_root)

    incremental_root = tmp_path / "incremental"
    materialize_dataset(_dataset([segment_a]), incremental_root)
    incremental_report = materialize_dataset(_dataset([segment_a, segment_b]), incremental_root)

    full_sessions = _generation_sessions(full_root, full_report.new_generation)
    incremental_sessions = _generation_sessions(incremental_root, incremental_report.new_generation)
    assert full_sessions == incremental_sessions

    for session_date, object_hash in full_sessions.items():
        full_object = (full_root / "objects" / f"{object_hash}.json").read_text(encoding="utf-8")
        incremental_object = (incremental_root / "objects" / f"{object_hash}.json").read_text(encoding="utf-8")
        assert full_object == incremental_object, session_date


# --- 11. nenhum arquivo gravado no repositório -----------------------------


def test_output_root_inside_repository_is_refused():
    from market_analytics.atlas_incremental import _repo_root

    with pytest.raises(MaterializationError):
        assert_output_outside_repo(_repo_root() / "tmp_atlas_test_output")


# --- 12. hashes determinísticos + JSON estrito -----------------------------


def test_input_inventory_hash_is_deterministic(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset = _dataset([segment])

    report_a = materialize_dataset(dataset, tmp_path / "out_a")
    report_b = materialize_dataset(dataset, tmp_path / "out_b")

    assert report_a.input_inventory_sha256 == report_b.input_inventory_sha256
    assert report_a.params_sha256 == report_b.params_sha256


def test_generation_manifest_is_strict_json(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out"
    report = materialize_dataset(_dataset([segment]), dataset_root)

    manifest_path = dataset_root / "generations" / report.new_generation / "manifest.json"
    # json.loads já recusaria NaN/Infinity e vírgulas penduradas -- só a
    # ausência de exceção aqui já prova "JSON estrito".
    json.loads(manifest_path.read_text(encoding="utf-8"))


def test_corrupted_current_object_fails_closed(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out"
    report = materialize_dataset(_dataset([segment]), dataset_root)
    object_hash = next(iter(_generation_sessions(dataset_root, report.new_generation).values()))
    (dataset_root / "objects" / f"{object_hash}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(MaterializationError, match="objeto corrompido"):
        materialize_dataset(_dataset([segment]), dataset_root)


def test_manifest_rejects_non_json_nan(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema": NaN}', encoding="utf-8")

    with pytest.raises(ManifestError, match="JSON inválido"):
        load_materialization_manifest_file(manifest_path)


def test_provenance_only_change_is_persisted_once(tmp_path):
    first_path = tmp_path / "first.parquet"
    second_path = tmp_path / "second.parquet"
    _write_sessions(first_path, ["2026-02-02"])
    second_path.write_bytes(first_path.read_bytes())
    dataset_root = tmp_path / "out"
    first_segment = _segment("seg1", first_path, "SYN", "2026-02-01", "2026-02-28")
    second_segment = _segment("seg1", second_path, "SYN", "2026-02-01", "2026-02-28")
    materialize_dataset(_dataset([first_segment]), dataset_root)

    correction = materialize_dataset(_dataset([second_segment]), dataset_root)
    unchanged = materialize_dataset(_dataset([second_segment]), dataset_root)

    assert correction.status == "historical_correction"
    assert unchanged.status == "no_change"
    assert unchanged.new_generation == correction.new_generation


# --- extras: dry-run, manifesto de exemplo, CLI ---------------------------


def test_dry_run_never_writes(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02", "2026-02-03"])
    segment = _segment("seg1", seg_path, "SYN", "2026-02-01", "2026-02-28")
    dataset_root = tmp_path / "out"

    report = materialize_dataset(_dataset([segment]), dataset_root, dry_run=True)

    assert report.status == "append"
    assert report.new_generation is None
    assert not dataset_root.exists()


def test_example_manifest_file_parses():
    path = Path(__file__).resolve().parents[1] / "market_analytics" / "manifests" / "atlas.example.json"
    manifest = load_materialization_manifest_file(path)
    assert manifest.manifest_id == "example"
    assert manifest.dataset("example_continuous").symbol == "EXAMPLE$"


def test_cli_smoke(tmp_path):
    seg_path = tmp_path / "seg1.parquet"
    _write_sessions(seg_path, ["2026-02-02"])
    manifest = {
        "schema": "ep_market_hub.atlas.materialization_manifest.v1",
        "manifest_id": "cli_smoke",
        "output_root": str(tmp_path / "out"),
        "datasets": [
            {
                "logical_id": "syn",
                "symbol": "SYN$",
                "series_kind": "continuous_proportional",
                "source_symbol": "SYN$",
                "adjustment_method": "proportional",
                "expected_session_start": SESSION_START,
                "expected_session_end": SESSION_END,
                "input_kind": "m1_segments",
                "segments": [
                    {
                        "segment_id": "seg1",
                        "path": str(seg_path),
                        "source_id": "SYN",
                        "allowed_start_date": "2026-02-01",
                        "allowed_end_date": "2026-02-28",
                    }
                ],
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "tools" / "update_market_atlas.py"),
            "--manifest", str(manifest_path),
            "--output-root", str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["datasets"][0]["status"] == "append"
    assert payload["datasets"][0]["total_sessions"] == 1
