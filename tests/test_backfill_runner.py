from __future__ import annotations

import math
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq

from market_analytics.backfill_catalog import get_session, open_catalog
from market_analytics.backfill_runner import (
    BackfillSourceError,
    advance_backfill_job,
    run_session_backfill,
    start_backfill_job,
)
from market_analytics.backfill_writer import SessionTickWriter, build_schema
from market_analytics.tick_backfill import (
    BackfillSessionRequest,
    build_raw_metadata,
    raw_partition_dir,
)
from market_analytics.tick_diagnostics import TickRecord, TickWindow


def _catalog(tmp_path: Path):
    return open_catalog(tmp_path / "catalog" / "collection.sqlite3")


def _request(**overrides) -> BackfillSessionRequest:
    defaults: dict = dict(
        request_id="req-1",
        source_id="clear",
        logical_id="win",
        aliases=("WIN$",),
        tick_type="all",
        session_date=date(2026, 8, 28),
        chunk_seconds=900,  # limite máximo do contrato: 96 chunks por dia civil
    )
    defaults.update(overrides)
    return BackfillSessionRequest(**defaults)


def _final_path(tmp_path: Path, request: BackfillSessionRequest) -> Path:
    return (
        raw_partition_dir(
            tmp_path, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
        )
        / "ticks.parquet"
    )


def _one_tick_per_chunk(counter: dict | None = None):
    """Fonte falsa: devolve exatamente um tick por chunk, dentro da janela."""

    calls: list[TickWindow] = []

    def _fetch(chunk: TickWindow) -> list[TickRecord]:
        calls.append(chunk)
        time_msc = int(chunk.start_utc.timestamp() * 1000) + 500
        return [
            TickRecord(
                time=time_msc // 1000,
                time_msc=time_msc,
                bid=1.0,
                ask=1.1,
                last=0.0,
                volume=0.0,
                volume_real=0.0,
                flags=6,
            )
        ]

    _fetch.calls = calls  # type: ignore[attr-defined]
    return _fetch


def test_run_session_backfill_writes_one_final_file_and_completes_catalog(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    fetch = _one_tick_per_chunk()

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=fetch
    )

    assert result.state == "completed"
    assert result.summary.total_count == len(request.chunks())
    final_path = _final_path(tmp_path, request)
    assert final_path.exists()
    assert pq.ParquetFile(str(final_path)).metadata.num_rows == len(request.chunks())

    row = get_session(conn, source_id="clear", logical_id="win", session_date=request.session_date)
    assert row["state"] == "completed"
    assert row["sha256"] == result.promoted.sha256
    assert row["file_path"] == str(final_path)
    assert row["tick_count"] == result.summary.total_count


def test_run_session_backfill_never_sends_raw_ticks_through_a_queue_like_channel(tmp_path: Path) -> None:
    """Critério de aceitação: nenhum tick bruto atravessa IPC.

    `fetch_chunk` aqui simula a fronteira do worker: cada chamada devolve
    só os registros de um chunk, nunca o dia inteiro; o runner nunca pede
    mais que uma janela por vez.
    """

    conn = _catalog(tmp_path)
    request = _request(chunk_seconds=900)
    max_bulk = {"seen": 0}

    def _fetch(chunk: TickWindow) -> list[TickRecord]:
        # Cada chunk pedido é sempre <= 900s (o limite acordado na ordem).
        assert (chunk.end_utc - chunk.start_utc).total_seconds() <= 900
        max_bulk["seen"] += 1
        return []

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_fetch
    )

    assert result.state == "empty"
    assert max_bulk["seen"] == len(request.chunks())


def test_idempotent_rerun_of_a_completed_session_is_refused(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    fetch = _one_tick_per_chunk()
    first = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=fetch
    )
    assert first.state == "completed"
    original_bytes = _final_path(tmp_path, request).read_bytes()

    second = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert second.state == "failed"
    assert second.error_reason == "already_completed"
    assert _final_path(tmp_path, request).read_bytes() == original_bytes


def test_rebuild_true_explicitly_replaces_a_completed_session(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    rebuild_request = _request(rebuild=True)
    result = run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=rebuild_request,
        resolved_symbol="WIN$",
        fetch_chunk=_one_tick_per_chunk(),
    )

    assert result.state == "completed"
    row = get_session(conn, source_id="clear", logical_id="win", session_date=request.session_date)
    assert row["attempts"] == 2


def test_interruption_between_chunks_leaves_the_session_incomplete_and_discards_partial(
    tmp_path: Path,
) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    calls = {"n": 0}

    def _fetch(chunk: TickWindow) -> list[TickRecord]:
        calls["n"] += 1
        return []

    result = run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=request,
        resolved_symbol="WIN$",
        fetch_chunk=_fetch,
        should_stop=lambda: calls["n"] >= 3,
    )

    assert result.state == "interrupted"
    assert not _final_path(tmp_path, request).exists()
    assert not _final_path(tmp_path, request).with_suffix(".parquet.partial").exists()
    row = get_session(conn, source_id="clear", logical_id="win", session_date=request.session_date)
    assert row["state"] == "interrupted"
    # A retomada (nova tentativa, sem rebuild) refaz o dia do zero.
    complete = run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=request,
        resolved_symbol="WIN$",
        fetch_chunk=_one_tick_per_chunk(),
    )
    assert complete.state == "completed"


def test_resuming_after_interruption_never_downgrades_an_already_completed_day(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    day_one = _request(session_date=date(2026, 8, 27))
    day_two = _request(session_date=date(2026, 8, 28))
    run_session_backfill(
        conn=conn, data_root=tmp_path, request=day_one, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    original_bytes = _final_path(tmp_path, day_one).read_bytes()

    calls = {"n": 0}

    def _stop_soon(chunk: TickWindow) -> list[TickRecord]:
        calls["n"] += 1
        return []

    run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=day_two,
        resolved_symbol="WIN$",
        fetch_chunk=_stop_soon,
        should_stop=lambda: calls["n"] >= 2,
    )

    assert _final_path(tmp_path, day_one).read_bytes() == original_bytes
    row_one = get_session(conn, source_id="clear", logical_id="win", session_date=day_one.session_date)
    assert row_one["state"] == "completed"


def test_mt5_source_error_fails_with_structured_reason_and_discards_partial(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()

    def _fetch(chunk: TickWindow) -> list[TickRecord]:
        raise BackfillSourceError("mt5_error", "Invalid params", code=-2)

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_fetch
    )

    assert result.state == "failed"
    assert result.error_reason == "mt5_error"
    assert result.error_code == -2
    assert not _final_path(tmp_path, request).exists()
    row = get_session(conn, source_id="clear", logical_id="win", session_date=request.session_date)
    assert row["state"] == "failed"
    assert row["error_code"] == -2


def test_unexpected_exception_from_source_is_isolated_as_mt5_error(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()

    def _fetch(chunk: TickWindow) -> list[TickRecord]:
        raise RuntimeError("falha inesperada da API")

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_fetch
    )

    assert result.state == "failed"
    assert result.error_reason == "mt5_error"


def test_nan_price_fails_as_malformed_response_without_crashing(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()

    def _fetch(chunk: TickWindow) -> list[TickRecord]:
        time_msc = int(chunk.start_utc.timestamp() * 1000) + 500
        return [
            TickRecord(
                time=time_msc // 1000,
                time_msc=time_msc,
                bid=math.nan,
                ask=1.1,
                last=0.0,
                volume=0.0,
                volume_real=0.0,
                flags=6,
            )
        ]

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_fetch
    )

    assert result.state == "failed"
    assert result.error_reason == "malformed_response"
    assert not _final_path(tmp_path, request).exists()


def test_empty_day_is_marked_with_canonical_reason_without_guessing_holiday(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=lambda chunk: []
    )

    assert result.state == "empty"
    assert result.summary.empty_reason == "no_ticks_returned"
    row = get_session(conn, source_id="clear", logical_id="win", session_date=request.session_date)
    assert row["state"] == "empty"
    assert row["empty_reason"] == "no_ticks_returned"
    # Um dia vazio ainda produz um arquivo final válido (0 linhas), não uma
    # ausência silenciosa de artefato.
    assert _final_path(tmp_path, request).exists()


def test_disk_error_while_writing_a_chunk_fails_the_session_without_crashing(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    job, immediate = start_backfill_job(
        conn=conn,
        data_root=tmp_path,
        request=request,
        resolved_symbol="WIN$",
        fetch_chunk=_one_tick_per_chunk(),
    )
    assert immediate is None

    def _broken_write_table(*_args, **_kwargs):
        raise OSError("disco cheio (simulado)")

    job["writer"]._writer.write_table = _broken_write_table  # type: ignore[union-attr]

    outcome, result = advance_backfill_job(job)

    assert outcome == "failed"
    assert result.error_reason == "disk_error"
    assert not _final_path(tmp_path, request).exists()


def test_different_sources_and_timezones_never_collide_in_identity_or_path(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    clear_request = _request(source_id="clear", logical_id="win")
    fot_request = _request(
        request_id="req-2", source_id="fot", logical_id="nasdaq100", session_timezone="America/New_York"
    )

    clear_result = run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=clear_request,
        resolved_symbol="WIN$",
        fetch_chunk=_one_tick_per_chunk(),
    )
    fot_result = run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=fot_request,
        resolved_symbol="US100Cash",
        fetch_chunk=_one_tick_per_chunk(),
    )

    assert clear_result.state == "completed"
    assert fot_result.state == "completed"
    assert _final_path(tmp_path, clear_request) != _final_path(tmp_path, fot_request)
    assert _final_path(tmp_path, clear_request).exists()
    assert _final_path(tmp_path, fot_request).exists()


def test_digit_and_last_variations_are_preserved_without_assuming_exchange_volume(tmp_path: Path) -> None:
    """WIN/WDO e amostras FOT compartilham o mesmo pipeline genérico, sem
    tratar volume_real externo como volume de bolsa quando ele não for."""

    conn = _catalog(tmp_path)
    request = _request(source_id="fot", logical_id="eurusd", session_timezone="America/New_York")

    def _fetch(chunk: TickWindow) -> list[TickRecord]:
        time_msc = int(chunk.start_utc.timestamp() * 1000) + 500
        return [
            TickRecord(
                time=time_msc // 1000,
                time_msc=time_msc,
                bid=1.08765,
                ask=1.08789,
                last=0.0,  # Forex tipicamente não preenche last.
                volume=0.0,
                volume_real=0.0,  # sem volume centralizado nesta fonte.
                flags=6,
            )
        ]

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="EURUSD", fetch_chunk=_fetch
    )

    assert result.state == "completed"
    assert result.summary.non_zero_counts["last"]["non_zero"] == 0
    assert result.summary.non_zero_counts["volume_real"]["non_zero"] == 0
    assert result.summary.non_zero_counts["bid"]["non_zero"] == result.summary.total_count


# --- Correção de auditoria item 6: reconciliação arquivo↔catálogo na orquestração ---


def test_orphaned_promoted_file_alone_never_authorizes_a_common_caller_to_recover_it(tmp_path: Path) -> None:
    """Simula: o Parquet foi promovido, mas `complete_session` nunca
    confirmou, deixando a linha presa em `running` com o mesmo `attempt_id`
    do arquivo já promovido. Rejeição focal da quarta auditoria, item 1:
    essa identidade sozinha prova apenas autoria do arquivo, nunca que o
    dono morreu — um chamador comum de `start_backfill_job`/
    `run_session_backfill` nunca reconcilia essa linha sozinho; só recebe
    `backfill_busy`, preservando `running` intacta. A liberação legítima
    dessa sessão é exclusiva do manager, com a morte do worker/instância já
    confirmada (ver `core/worker_manager.py`)."""

    conn = _catalog(tmp_path)
    request = _request()
    first = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert first.state == "completed"
    final_path = _final_path(tmp_path, request)
    original_bytes = final_path.read_bytes()

    # O catálogo nunca recebeu a confirmação de conclusão.
    stuck_attempt_id = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )["attempt_id"]
    conn.execute(
        "UPDATE backfill_sessions SET state='running', sha256=NULL, tick_count=NULL "
        "WHERE source_id=? AND logical_id=? AND session_date=?",
        (request.source_id, request.logical_id, request.session_date.isoformat()),
    )

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert result.state == "failed"
    assert result.error_reason == "backfill_busy"
    assert result.error_reason != "already_completed"
    assert final_path.read_bytes() == original_bytes  # nunca reprocessado nem sobrescrito
    row = get_session(conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date)
    assert row["state"] == "running"  # preservada, nunca tomada por um chamador comum
    assert row["sha256"] is None
    assert row["attempt_id"] == stuck_attempt_id


def test_catalog_completed_without_file_is_recoverable_without_rebuild(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    final_path = _final_path(tmp_path, request)
    final_path.unlink()

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert result.state == "completed"
    assert final_path.exists()
    row = get_session(conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date)
    assert row["state"] == "completed"


def test_catalog_error_after_promotion_preserves_the_file_and_is_recoverable_next_time(
    tmp_path: Path, monkeypatch
) -> None:
    """O arquivo já foi promovido, validado e tem hash quando o catálogo
    falha ao confirmar: o resultado reporta a falha sem apagar/sobrescrever
    o artefato. `_finalize` ainda é dono do `attempt_id` corrente, então se
    autodeclara `failed` como melhor esforço (não é um evento tardio de
    outra tentativa) — a tentativa seguinte já reconcilia livremente, sem
    precisar de uma liberação externa de propriedade."""

    import market_analytics.backfill_runner as runner_module

    conn = _catalog(tmp_path)
    request = _request()

    def _boom(*args, **kwargs):
        raise runner_module.catalog.CatalogStateError("sqlite_error: falha simulada ao confirmar")

    monkeypatch.setattr(runner_module.catalog, "complete_session", _boom)

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert result.state == "failed"
    assert result.error_reason == "catalog_error"
    assert result.promoted is not None
    final_path = _final_path(tmp_path, request)
    assert final_path.exists()
    assert len(final_path.read_bytes()) > 0

    monkeypatch.undo()

    stuck_row = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert stuck_row["state"] == "failed"  # autodeclarada, não presa em "running"

    recovered = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert recovered.state == "failed"
    assert recovered.error_reason == "already_completed"
    row = get_session(conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date)
    assert row["state"] == "completed"


# --- Correção de auditoria item 8: isolamento de falhas secundárias do catálogo ---


def test_fail_session_also_failing_never_crashes_and_preserves_the_original_reason(
    tmp_path: Path, monkeypatch
) -> None:
    import market_analytics.backfill_runner as runner_module

    conn = _catalog(tmp_path)
    request = _request()

    def _boom_fetch(chunk: TickWindow) -> list[TickRecord]:
        raise BackfillSourceError("mt5_error", "falha original da fonte")

    def _boom_fail_session(*args, **kwargs):
        raise runner_module.catalog.CatalogStateError("sqlite_error: também quebrado")

    monkeypatch.setattr(runner_module.catalog, "fail_session", _boom_fail_session)

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_boom_fetch
    )

    assert result.state == "failed"
    assert result.error_reason == "mt5_error"
    assert result.error_message == "falha original da fonte"


def test_finalize_self_heal_also_failing_leaves_the_row_running_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    """Se `complete_session` E o autodeclarar-failed de melhor esforço
    também falharem (catálogo genuinamente inacessível), a linha fica
    `running` — sem liberação externa de propriedade, nada mais a fazer —
    mas o resultado ainda é reportado de forma estruturada, sem crash."""

    import market_analytics.backfill_runner as runner_module

    conn = _catalog(tmp_path)
    request = _request()

    def _boom(*args, **kwargs):
        raise runner_module.catalog.CatalogStateError("sqlite_error: catálogo genuinamente inacessível")

    monkeypatch.setattr(runner_module.catalog, "complete_session", _boom)
    monkeypatch.setattr(runner_module.catalog, "fail_session", _boom)

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert result.state == "failed"
    assert result.error_reason == "catalog_error"
    assert result.promoted is not None

    monkeypatch.undo()
    row = get_session(conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date)
    assert row["state"] == "running"


def test_interrupt_session_also_failing_never_crashes(tmp_path: Path, monkeypatch) -> None:
    import market_analytics.backfill_runner as runner_module

    conn = _catalog(tmp_path)
    request = _request()

    def _boom_interrupt(*args, **kwargs):
        raise runner_module.catalog.CatalogStateError("sqlite_error: também quebrado")

    monkeypatch.setattr(runner_module.catalog, "interrupt_session", _boom_interrupt)

    result = run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=request,
        resolved_symbol="WIN$",
        fetch_chunk=lambda chunk: [],
        should_stop=lambda: True,
    )

    assert result.state == "interrupted"


# --- Correção de auditoria item 3 (regressão adicional): sessão DST fim a fim ---


def test_full_25_hour_dst_session_completes_through_the_runner(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request(
        source_id="fot",
        logical_id="eurusd",
        session_date=date(2026, 11, 1),
        session_timezone="America/New_York",
    )
    expected_chunks = len(request.chunks())

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="EURUSD", fetch_chunk=_one_tick_per_chunk()
    )

    assert result.state == "completed"
    assert result.summary.total_count == expected_chunks
    # 25h == 90000s; a 900s por chunk, exatamente 100 chunks, sem gap/overlap.
    assert expected_chunks == 100


# --- Correção de auditoria item 6 (regressão adicional): rebuild interrompido fim a fim ---


def test_interrupted_rebuild_through_the_runner_recovers_the_last_completed_file(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    first = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert first.state == "completed"
    final_path = _final_path(tmp_path, request)
    original_bytes = final_path.read_bytes()

    rebuild_request = _request(rebuild=True)
    calls = {"n": 0}

    def _stop_soon(chunk: TickWindow) -> list[TickRecord]:
        calls["n"] += 1
        return []

    interrupted = run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=rebuild_request,
        resolved_symbol="WIN$",
        fetch_chunk=_stop_soon,
        should_stop=lambda: calls["n"] >= 2,
    )
    assert interrupted.state == "interrupted"
    # A reconstrução nunca chegou a promover um novo arquivo: o antigo
    # permanece intacto no disco, mesmo com o catálogo agora "interrupted".
    assert final_path.read_bytes() == original_bytes

    # A tentativa seguinte (mesmo sem rebuild) reconcilia o catálogo a
    # partir do arquivo físico intacto, em vez de recusar cegamente ou
    # perder o resultado anterior.
    recovered = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert recovered.state == "failed"
    assert recovered.error_reason == "already_completed"
    assert final_path.read_bytes() == original_bytes
    row = get_session(conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date)
    assert row["state"] == "completed"


# --- Correção de auditoria (segunda entrega) item 2: identidade semântica do artefato ---


def _write_file_with_metadata(
    final_path: Path, *, request: BackfillSessionRequest, resolved_symbol: str, attempt_id: str = "attempt-foreign"
) -> None:
    metadata = build_raw_metadata(
        request=request,
        resolved_symbol=resolved_symbol,
        collected_at=datetime(2026, 1, 1, tzinfo=UTC),
        attempt_id=attempt_id,
    )
    schema = build_schema(metadata=metadata)
    writer = SessionTickWriter(final_path, schema=schema)
    writer.open()
    writer.close_and_promote()


def test_wrong_identity_file_at_expected_path_is_refused_without_changing_catalog(tmp_path: Path) -> None:
    """Reprodução da evidência 2: um Parquet estruturalmente válido, mas com
    metadados de outra fonte/ativo/data, não pode ser aceito como se
    pertencesse à sessão do caminho atual."""

    conn = _catalog(tmp_path)
    request = _request(source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    foreign_request = _request(
        request_id="req-foreign",
        source_id="fot",
        logical_id="eurusd",
        session_date=date(2020, 1, 1),
        session_timezone="America/New_York",
    )
    final_path = _final_path(tmp_path, request)
    _write_file_with_metadata(final_path, request=foreign_request, resolved_symbol="EURUSD")

    job, result = start_backfill_job(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert job is None
    assert result.state == "failed"
    assert result.error_reason == "identity_mismatch"
    assert get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28)) is None
    # O arquivo estranho não foi apagado nem sobrescrito.
    assert final_path.exists()


def test_file_with_correct_schema_but_no_metadata_is_refused_without_changing_catalog(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    final_path = _final_path(tmp_path, request)
    writer = SessionTickWriter(final_path, schema=build_schema(metadata={}))
    writer.open()
    writer.close_and_promote()

    job, result = start_backfill_job(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert job is None
    assert result.error_reason == "identity_mismatch"
    assert get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28)) is None


def test_truncated_parquet_at_expected_path_produces_disk_error_not_a_crash(tmp_path: Path) -> None:
    conn = _catalog(tmp_path)
    request = _request()
    final_path = _final_path(tmp_path, request)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"garbage-not-parquet")

    job, result = start_backfill_job(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert job is None
    assert result.state == "failed"
    assert result.error_reason == "disk_error"
    assert get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28)) is None
    # O arquivo corrompido continua lá — a recusa não tentou "consertá-lo".
    assert final_path.read_bytes() == b"garbage-not-parquet"


# --- Correção de auditoria (segunda entrega) item 3: reprodução fim a fim da evidência 3 ---


def test_evidence_3_empty_rebuild_after_96_ticks_recovers_a_fully_coherent_row(
    tmp_path: Path, monkeypatch
) -> None:
    """Reprodução exata da evidência 3: completar 96 ticks, reconstruir
    vazia, simular falha de `complete_session` após a promoção, e conferir
    que a reconciliação seguinte não deixa nenhuma métrica antiga (96
    ticks) sobrevivendo na linha `empty`."""

    import market_analytics.backfill_runner as runner_module

    conn = _catalog(tmp_path)
    request = _request()
    first = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert first.state == "completed"
    assert first.summary.total_count == 96

    def _boom_complete(*args, **kwargs):
        raise runner_module.catalog.CatalogStateError("sqlite_error: falha simulada após a promoção")

    monkeypatch.setattr(runner_module.catalog, "complete_session", _boom_complete)

    rebuild_request = _request(rebuild=True)
    interrupted_by_failure = run_session_backfill(
        conn=conn,
        data_root=tmp_path,
        request=rebuild_request,
        resolved_symbol="WIN$",
        fetch_chunk=lambda chunk: [],
    )
    monkeypatch.undo()

    assert interrupted_by_failure.state == "failed"
    assert interrupted_by_failure.error_reason == "catalog_error"
    assert interrupted_by_failure.promoted is not None
    assert interrupted_by_failure.promoted.row_count == 0

    # _finalize se autodeclara failed com o attempt_id ainda corrente
    # (nunca fica preso em "running" por conta de uma falha só do catálogo).
    stuck_row = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert stuck_row["state"] == "failed"

    # A tentativa seguinte reconcilia o catálogo a partir do arquivo vazio
    # efetivamente presente — nunca mistura com o resumo dos 96 ticks antigos.
    recovered = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert recovered.state == "failed"
    assert recovered.error_reason == "already_completed"

    row = get_session(conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date)
    assert row["state"] == "empty"
    assert row["tick_count"] == 0
    assert row["first_tick_utc"] is None
    assert row["last_tick_utc"] is None
    assert row["non_zero_counts"]["bid"]["non_zero"] == 0
    assert row["flags_histogram"]["bid"] == 0
    assert row["empty_reason"] == "no_ticks_returned"


# --- Correção de auditoria (segunda entrega) item 5: classificação fiel de erro do catálogo ---


def test_evidence_4_sqlite_error_in_begin_attempt_is_never_reported_as_busy(tmp_path: Path, monkeypatch) -> None:
    import market_analytics.backfill_runner as runner_module

    conn = _catalog(tmp_path)
    request = _request()

    def _boom(*args, **kwargs):
        raise runner_module.catalog.CatalogStateError("sqlite_error: falha simulada de disco")

    monkeypatch.setattr(runner_module.catalog, "begin_attempt", _boom)

    job, result = start_backfill_job(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert job is None
    assert result.error_reason == "catalog_error"
    assert result.error_reason != "backfill_busy"


def test_genuine_backfill_busy_from_begin_attempt_keeps_its_reason(tmp_path: Path, monkeypatch) -> None:
    """Regressão inversa: a classificação fiel não pode transformar uma
    recusa legítima de concorrência em `catalog_error`."""

    import market_analytics.backfill_runner as runner_module

    conn = _catalog(tmp_path)
    request = _request()

    def _boom(*args, **kwargs):
        raise runner_module.catalog.CatalogStateError("backfill_busy: já existe uma tentativa em andamento.")

    monkeypatch.setattr(runner_module.catalog, "begin_attempt", _boom)

    job, result = start_backfill_job(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert job is None
    assert result.error_reason == "backfill_busy"


# --- Correção de auditoria (quarta entrega do Portão A) ---


def test_recovers_a_running_row_only_after_the_manager_confirms_the_owner_died(
    tmp_path: Path, monkeypatch
) -> None:
    """Item 1, corrigido pela rejeição focal: queda entre a promoção do
    Parquet e a confirmação da transição terminal no catálogo NUNCA é
    recuperada por um chamador comum de `run_session_backfill`/
    `start_backfill_job` só por identidade de `attempt_id` — isso recebe
    `backfill_busy`, preservando `running` (ver
    `test_orphaned_promoted_file_alone_never_authorizes_a_common_caller_to_recover_it`).
    A liberação legítima é exclusiva do manager, com a morte do
    worker/instância já confirmada: aqui ela é simulada diretamente pela
    mesma `interrupt_session` que `core/worker_manager.py` usa nesse caso,
    o que deixa a linha reconciliável normalmente pela tentativa seguinte."""

    import market_analytics.backfill_runner as runner_module
    from market_analytics import backfill_catalog

    conn = _catalog(tmp_path)
    request = _request()

    def _boom(*args, **kwargs):
        raise runner_module.catalog.CatalogStateError("sqlite_error: catálogo genuinamente inacessível")

    monkeypatch.setattr(runner_module.catalog, "complete_session", _boom)
    monkeypatch.setattr(runner_module.catalog, "fail_session", _boom)

    first = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert first.state == "failed"
    monkeypatch.undo()

    stuck_row = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert stuck_row["state"] == "running"  # queda comprovada

    # Um chamador comum, sozinho, nunca toma essa sessão.
    still_busy = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert still_busy.error_reason == "backfill_busy"
    assert get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )["state"] == "running"

    # O manager confirma a morte do dono e libera a propriedade no catálogo
    # (mesma chamada de `_interrupt_backfills_for_terminal`).
    backfill_catalog.interrupt_session(
        conn,
        source_id=request.source_id,
        logical_id=request.logical_id,
        session_date=request.session_date,
        attempt_id=stuck_row["attempt_id"],
        message="Worker terminou antes da conclusão do backfill.",
    )

    recovered = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert recovered.error_reason != "backfill_busy"
    assert recovered.error_reason == "already_completed"  # o dia já estava genuinamente concluído
    row = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert row["state"] == "completed"
    assert row["attempt_id"] == stuck_row["attempt_id"]
    assert row["sha256"] is not None


def test_recovery_never_touches_a_running_row_from_a_different_still_active_attempt(
    tmp_path: Path,
) -> None:
    """Uma linha `running` sem nenhum arquivo final promovido ainda (uma
    tentativa de verdade em andamento, escrevendo `.partial`) nunca é
    tocada pela recuperação da queda: continua recusada com backfill_busy,
    exatamente como antes desta correção."""

    conn = _catalog(tmp_path)
    request = _request()
    job, result = start_backfill_job(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert job is not None and result is None  # tentativa genuinamente ativa, nenhum arquivo final ainda

    second = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert second.error_reason == "backfill_busy"


def test_fast_path_rejects_a_footer_corrupted_file_of_the_same_size(tmp_path: Path) -> None:
    """Item 4: tamanho igual não basta — um rodapé Parquet corrompido do
    mesmo tamanho nunca pode ser aceito pelo caminho rápido nem devolver
    `already_completed`; deve sair como falha estruturada."""

    conn = _catalog(tmp_path)
    request = _request()
    run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    final_path = _final_path(tmp_path, request)
    original = bytearray(final_path.read_bytes())
    original[-8] ^= 0xFF  # corrompe um byte perto do rodapé, tamanho preservado
    final_path.write_bytes(bytes(original))

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert result.state == "failed"
    assert result.error_reason != "already_completed"


def test_fast_path_skips_rebuilding_the_summary_for_an_intact_already_audited_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Item 4 (contraparte): um arquivo íntegro já auditado (hash/contagem
    do footer batendo com o catálogo) usa o caminho rápido sem reconstruir
    o resumo tick a tick — `recompute_summary_from_file` nunca é chamado."""

    import market_analytics.backfill_runner as runner_module

    conn = _catalog(tmp_path)
    request = _request()
    first = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert first.state == "completed"

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("recompute_summary_from_file não deveria ser chamado no caminho rápido")

    monkeypatch.setattr(runner_module, "recompute_summary_from_file", _must_not_be_called)

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert result.state == "failed"
    assert result.error_reason == "already_completed"


# --- Rejeição focal da implementação da quarta ordem ---


def test_running_row_with_a_still_alive_owner_is_never_taken_by_another_caller(tmp_path: Path) -> None:
    """Item 1: o job A continua vivo — nunca terminou, nunca falhou — quando
    seu Parquet já foi promovido manualmente e o catálogo ainda está
    `running`. Igualdade de `attempt_id` entre arquivo e linha prova apenas
    autoria do arquivo, não que o dono morreu: um chamador comum de
    `start_backfill_job`/`run_session_backfill` nunca pode tomar essa
    sessão só por tê-la encontrado."""

    conn = _catalog(tmp_path)
    request = _request()
    fetch = _one_tick_per_chunk()

    job, immediate = start_backfill_job(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=fetch
    )
    assert job is not None and immediate is None  # job A vivo: reserva ativa, arquivo ainda não promovido

    # Job A promove seu Parquet manualmente, sem passar por `_finalize`/
    # `complete_session` — ele continua vivo, apenas ainda não confirmou a
    # transição terminal no catálogo.
    for chunk in job["chunks"]:
        job["writer"].write_chunk(fetch(chunk))
    job["writer"].close_and_promote()

    running_row = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert running_row["state"] == "running"
    assert running_row["attempt_id"] == job["attempt_id"]

    # Chamador B tenta a mesma sessão enquanto job A ainda está de pé.
    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=fetch
    )

    assert result.state == "failed"
    assert result.error_reason == "backfill_busy"
    row = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert row["state"] == "running"  # preservada, nunca tomada por B
    assert row["sha256"] is None
    assert row["attempt_id"] == job["attempt_id"]


def test_hash_divergent_from_a_terminal_row_is_refused_as_integrity_failure(tmp_path: Path) -> None:
    """Item 2: um Parquet estruturalmente válido, com os mesmos metadados e
    schema (mesmo `attempt_id`), mas com um `bid` alterado, muda o SHA-256.
    Para uma linha já `completed`, isso é falha estruturada de integridade —
    nunca reconciliação silenciosa que substitui o hash auditado e responde
    `already_completed`."""

    conn = _catalog(tmp_path)
    request = _request()
    first = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )
    assert first.state == "completed"
    completed_row = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    original_sha256 = completed_row["sha256"]
    original_tick_count = completed_row["tick_count"]
    final_path = _final_path(tmp_path, request)

    # Reescreve o Parquet fora do fluxo normal: mesmos metadados/schema/
    # attempt_id e a mesma contagem de ticks, mas um `bid` diferente no
    # primeiro tick — estruturalmente válido, hash diferente.
    metadata = build_raw_metadata(
        request=request,
        resolved_symbol="WIN$",
        collected_at=datetime.now(UTC),
        attempt_id=completed_row["attempt_id"],
    )
    schema = build_schema(metadata=metadata)
    tampered_writer = SessionTickWriter(final_path, schema=schema)
    tampered_writer.open()
    fetch = _one_tick_per_chunk()
    for index, chunk in enumerate(request.chunks()):
        records = fetch(chunk)
        if index == 0:
            records = [
                TickRecord(
                    time=records[0].time,
                    time_msc=records[0].time_msc,
                    bid=999.0,  # bid adulterado: mesmo schema, hash diferente
                    ask=records[0].ask,
                    last=records[0].last,
                    volume=records[0].volume,
                    volume_real=records[0].volume_real,
                    flags=records[0].flags,
                )
            ]
        tampered_writer.write_chunk(records)
    tampered_writer.close_and_promote()

    result = run_session_backfill(
        conn=conn, data_root=tmp_path, request=request, resolved_symbol="WIN$", fetch_chunk=_one_tick_per_chunk()
    )

    assert result.state == "failed"
    assert result.error_reason != "already_completed"
    assert result.error_reason == "integrity_error"
    row = get_session(
        conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert row["state"] == "completed"  # nunca rebaixada nem reconciliada
    assert row["sha256"] == original_sha256  # nunca substituído por hash não auditado
    assert row["tick_count"] == original_tick_count
