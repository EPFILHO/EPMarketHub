from __future__ import annotations

import math
import os
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from market_analytics import backfill_writer
from market_analytics.backfill_writer import (
    BackfillWriteError,
    SessionTickWriter,
    build_schema,
    discard_partial,
    ensure_rebuild_allowed,
    inspect_final_file,
    recompute_summary_from_file,
)
from market_analytics.tick_diagnostics import TickRecord, TickWindow


def _record(time_msc: int, **overrides) -> TickRecord:
    defaults = dict(
        time=time_msc // 1000,
        time_msc=time_msc,
        bid=1.0,
        ask=1.1,
        last=0.0,
        volume=0.0,
        volume_real=0.0,
        flags=6,
    )
    defaults.update(overrides)
    return TickRecord(**defaults)


def _schema():
    return build_schema(metadata={"source_id": "clear", "logical_id": "win"})


def test_writer_promotes_final_file_with_one_row_group_per_chunk(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())

    writer.open()
    writer.write_chunk([_record(1_000), _record(2_000)])
    writer.write_chunk([_record(3_000)])
    promoted = writer.close_and_promote()

    assert promoted.path == final_path
    assert promoted.row_count == 3
    assert final_path.exists()
    assert not writer.partial_path.exists()

    parquet_file = pq.ParquetFile(str(final_path))
    assert parquet_file.metadata.num_rows == 3
    # Memória limitada por chunk: cada chunk escrito vira seu próprio row
    # group, nunca um único bloco acumulado do dia inteiro em RAM.
    assert parquet_file.metadata.num_row_groups == 2


def test_writer_preserves_raw_fields_without_transformation(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk(
        [
            _record(1_500, bid=5321.5, ask=5321.75, last=5321.5, volume=3.0, volume_real=1250.0, flags=30),
            # Zeros propositais (Forex/CFD sem last, volume centralizado ausente).
            _record(1_600, bid=1.0987, ask=1.0989, last=0.0, volume=0.0, volume_real=0.0, flags=6),
        ]
    )
    writer.close_and_promote()

    table = pq.read_table(str(final_path))
    rows = table.to_pylist()
    assert rows[0]["bid"] == 5321.5
    assert rows[0]["volume_real"] == 1250.0
    assert rows[1]["last"] == 0.0
    assert rows[1]["volume"] == 0.0


def test_writer_never_deduplicates_ties_or_repeated_records(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    duplicate = _record(2_000)
    writer.write_chunk([duplicate, duplicate])
    promoted = writer.close_and_promote()

    assert promoted.row_count == 2
    assert pq.ParquetFile(str(final_path)).metadata.num_rows == 2


def test_write_chunk_rejects_nan_price_without_touching_final_file(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000)])

    with pytest.raises(ValueError):
        writer.write_chunk([_record(2_000, bid=math.nan)])

    # A escrita malformada não deixou o arquivo final corrompido: ele nunca
    # chegou a ser promovido.
    assert not final_path.exists()
    writer.abort()
    assert not writer.partial_path.exists()


def test_close_and_promote_never_overwrites_final_file_before_validation(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    original = SessionTickWriter(final_path, schema=_schema())
    original.open()
    original.write_chunk([_record(1_000)])
    original.close_and_promote()
    original_bytes = final_path.read_bytes()

    # Simula divergência entre contagem esperada e o que foi de fato
    # persistido no parcial: o arquivo final anterior não pode ser tocado.
    second = SessionTickWriter(final_path, schema=_schema())
    second.open()
    second.write_chunk([_record(5_000), _record(6_000)])
    second.row_count = 99  # força divergência de contagem na validação

    with pytest.raises(BackfillWriteError):
        second.close_and_promote()

    assert final_path.read_bytes() == original_bytes
    assert not second.partial_path.exists()


def test_ensure_rebuild_allowed_refuses_silent_overwrite(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    final_path.write_bytes(b"not-empty")

    with pytest.raises(BackfillWriteError):
        ensure_rebuild_allowed(final_path, rebuild=False)

    # rebuild=True autoriza explicitamente a reconstrução.
    ensure_rebuild_allowed(final_path, rebuild=True)


def test_open_discards_a_stray_partial_from_an_earlier_interrupted_attempt(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    partial_path = final_path.with_name("ticks.parquet.partial")
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_bytes(b"leftover-from-a-crash")

    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000)])
    promoted = writer.close_and_promote()

    assert promoted.row_count == 1
    assert pq.ParquetFile(str(final_path)).metadata.num_rows == 1


def test_discard_partial_is_a_no_op_when_nothing_exists(tmp_path: Path) -> None:
    discard_partial(tmp_path / "absent.partial")  # não deve levantar


def test_abort_before_any_chunk_leaves_no_files_behind(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.abort()

    assert not final_path.exists()
    assert not writer.partial_path.exists()


def test_schema_metadata_round_trips_through_the_final_file(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    schema = build_schema(
        metadata={
            "source_id": "clear",
            "logical_id": "win",
            "resolved_symbol": "WIN$",
            "session_timezone": "America/Sao_Paulo",
        }
    )
    writer = SessionTickWriter(final_path, schema=schema)
    writer.open()
    writer.write_chunk([_record(1_000)])
    writer.close_and_promote()

    read_schema = pq.ParquetFile(str(final_path)).schema_arrow
    assert read_schema.metadata[b"resolved_symbol"] == b"WIN$"
    assert read_schema.metadata[b"session_timezone"] == b"America/Sao_Paulo"


# --- Correção de auditoria item 7: durabilidade e handles no Windows ---


def test_discard_partial_propagates_a_real_removal_failure(tmp_path: Path, monkeypatch) -> None:
    """Falha ao descartar um `.partial` antigo não pode ser engolida como se
    a retomada estivesse limpa."""

    partial_path = tmp_path / "ticks.parquet.partial"
    partial_path.write_bytes(b"stuck")

    def _boom(self, missing_ok=False):
        raise PermissionError("simulated: file locked by another process")

    monkeypatch.setattr(Path, "unlink", _boom)

    with pytest.raises(BackfillWriteError):
        discard_partial(partial_path)


def test_open_propagates_discard_partial_failure_instead_of_pretending_clean(
    tmp_path: Path, monkeypatch
) -> None:
    final_path = tmp_path / "ticks.parquet"
    partial_path = final_path.with_name("ticks.parquet.partial")
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_bytes(b"stuck")

    def _boom(self, missing_ok=False):
        raise PermissionError("simulated: file locked")

    monkeypatch.setattr(Path, "unlink", _boom)

    writer = SessionTickWriter(final_path, schema=_schema())
    with pytest.raises(BackfillWriteError):
        writer.open()


def test_close_and_promote_fsyncs_the_partial_before_promotion(tmp_path: Path, monkeypatch) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000)])

    calls: list[int] = []
    real_fsync = os.fsync

    def _tracking_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(backfill_writer.os, "fsync", _tracking_fsync)

    promoted = writer.close_and_promote()

    assert calls, "os.fsync deveria ter sido chamado antes da promoção"
    assert promoted.row_count == 1


def test_close_and_promote_explicitly_closes_the_parquet_file_handle(tmp_path: Path, monkeypatch) -> None:
    """Não pode depender só do refcount do CPython para soltar o handle de
    leitura antes do `os.replace` — precisa chamar `.close()` explicitamente."""

    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000)])

    close_calls = {"n": 0}
    real_parquet_file_cls = pq.ParquetFile

    class _TrackingParquetFile(real_parquet_file_cls):
        def close(self, *args, **kwargs):
            close_calls["n"] += 1
            return super().close(*args, **kwargs)

    monkeypatch.setattr(backfill_writer.pq, "ParquetFile", _TrackingParquetFile)

    writer.close_and_promote()

    assert close_calls["n"] == 1


def test_abort_propagates_writer_close_failure(tmp_path: Path, monkeypatch) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000)])

    def _boom(*args, **kwargs):
        raise OSError("simulated close failure")

    monkeypatch.setattr(writer._writer, "close", _boom)

    with pytest.raises(BackfillWriteError):
        writer.abort()


# --- Correção de auditoria item 6: leitura para reconciliação (inspect_final_file) ---


def test_inspect_final_file_returns_none_when_file_is_absent(tmp_path: Path) -> None:
    assert inspect_final_file(tmp_path / "ticks.parquet") is None


def test_inspect_final_file_reads_back_hash_and_row_count(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000), _record(2_000)])
    promoted = writer.close_and_promote()

    inspected = inspect_final_file(final_path)

    assert inspected is not None
    assert inspected.row_count == 2
    assert inspected.sha256 == promoted.sha256
    assert inspected.size_bytes == promoted.size_bytes


def test_inspect_final_file_ignores_metadata_differences(tmp_path: Path) -> None:
    """`collected_at_utc` varia legitimamente entre coletas; a reconciliação
    não pode recusar um arquivo bom só por causa disso."""

    final_path = tmp_path / "ticks.parquet"
    schema_a = build_schema(metadata={"collected_at_utc": "2026-08-28T00:00:00+00:00"})
    writer = SessionTickWriter(final_path, schema=schema_a)
    writer.open()
    writer.write_chunk([_record(1_000)])
    writer.close_and_promote()

    inspected = inspect_final_file(final_path)

    assert inspected is not None
    assert inspected.row_count == 1


def test_inspect_final_file_rejects_a_structurally_incompatible_file(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    # Schema com um campo a menos que o bruto v1 — estruturalmente incompatível.
    incompatible_schema = build_schema(metadata={}).remove(7)
    import pyarrow as pa

    table = pa.table(
        {name: [] for name in incompatible_schema.names},
        schema=incompatible_schema,
    )
    final_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(final_path))

    with pytest.raises(BackfillWriteError):
        inspect_final_file(final_path)


# --- Correção de auditoria (segunda entrega) item 1: inspeção Parquet estruturada ---


def test_inspect_final_file_on_a_truncated_file_raises_structured_error_not_arrow_invalid(
    tmp_path: Path,
) -> None:
    """Evidência reproduzida pela auditoria: um arquivo de poucos bytes
    levantava `pyarrow.lib.ArrowInvalid` cru em vez de `BackfillWriteError`."""

    final_path = tmp_path / "ticks.parquet"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"xyz")

    with pytest.raises(BackfillWriteError):
        inspect_final_file(final_path)


def test_inspect_final_file_on_a_truncated_file_never_leaves_a_stuck_handle(tmp_path: Path) -> None:
    """No Windows, um handle preso impediria remover/reescrever o arquivo
    logo em seguida à falha de leitura."""

    final_path = tmp_path / "ticks.parquet"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"xyz")

    with pytest.raises(BackfillWriteError):
        inspect_final_file(final_path)

    # Se o handle tivesse ficado preso, isto falharia no Windows.
    final_path.unlink()
    final_path.write_bytes(b"outra tentativa")
    final_path.unlink()


def test_close_and_promote_validation_also_normalizes_arrow_errors(tmp_path: Path, monkeypatch) -> None:
    """Mesma disciplina de `inspect_final_file` aplicada à validação do
    parcial em `close_and_promote` (item 1 da segunda auditoria)."""

    import pyarrow as pa

    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000)])

    def _boom(*args, **kwargs):
        raise pa.ArrowInvalid("simulated corruption during validation")

    monkeypatch.setattr(backfill_writer.pq, "ParquetFile", _boom)

    with pytest.raises(BackfillWriteError):
        writer.close_and_promote()
    # O parcial corrompido foi descartado, e o arquivo final nunca foi criado.
    assert not final_path.exists()
    assert not writer.partial_path.exists()


# --- Correção de auditoria (segunda entrega) item 3: recompute_summary_from_file ---


def _session_window() -> TickWindow:
    from datetime import UTC, datetime

    return TickWindow(start_utc=datetime(2026, 8, 28, 3, tzinfo=UTC), end_utc=datetime(2026, 8, 29, 3, tzinfo=UTC))


def test_recompute_summary_from_file_matches_the_actual_content(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000, bid=5321.5), _record(2_000, bid=5321.75)])
    writer.write_chunk([_record(3_000, bid=5322.0)])
    writer.close_and_promote()

    summary = recompute_summary_from_file(
        final_path,
        request_id="req-1",
        source_id="clear",
        logical_id="win",
        resolved_symbol="WIN$",
        tick_type="all",
        window=_session_window(),
    )

    assert summary.total_count == 3
    assert summary.non_zero_counts["bid"]["non_zero"] == 3
    assert summary.resolved_symbol == "WIN$"


def test_recompute_summary_from_file_reads_in_bounded_batches(tmp_path: Path, monkeypatch) -> None:
    final_path = tmp_path / "ticks.parquet"
    writer = SessionTickWriter(final_path, schema=_schema())
    writer.open()
    writer.write_chunk([_record(1_000), _record(2_000), _record(3_000)])
    writer.close_and_promote()

    seen_batch_sizes: list[int] = []
    real_iter_batches = pq.ParquetFile.iter_batches

    def _tracking_iter_batches(self, batch_size=None, **kwargs):
        seen_batch_sizes.append(batch_size)
        return real_iter_batches(self, batch_size=batch_size, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", _tracking_iter_batches)

    recompute_summary_from_file(
        final_path,
        request_id="req-1",
        source_id="clear",
        logical_id="win",
        resolved_symbol="WIN$",
        tick_type="all",
        window=_session_window(),
        batch_size=1,
    )

    assert seen_batch_sizes == [1]


def test_recompute_summary_from_file_on_a_corrupt_file_raises_structured_error(tmp_path: Path) -> None:
    final_path = tmp_path / "ticks.parquet"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"not a parquet file")

    with pytest.raises(BackfillWriteError):
        recompute_summary_from_file(
            final_path,
            request_id="req-1",
            source_id="clear",
            logical_id="win",
            resolved_symbol="WIN$",
            tick_type="all",
            window=_session_window(),
        )
