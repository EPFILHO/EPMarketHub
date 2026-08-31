from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import market_analytics.backfill_catalog as catalog_module
from market_analytics.backfill_catalog import (
    CatalogStateError,
    begin_attempt,
    complete_session,
    fail_session,
    get_session,
    interrupt_session,
    list_running_sessions,
    open_catalog,
    reconcile_session,
)
from market_analytics.tick_diagnostics import TickRecord, TickWindow, TickWindowAccumulator

WINDOW = TickWindow(start_utc=datetime(2026, 8, 28, 3, tzinfo=UTC), end_utc=datetime(2026, 8, 29, 3, tzinfo=UTC))


def _conn(tmp_path: Path):
    return open_catalog(tmp_path / "catalog" / "collection.sqlite3")


def _summary(total_count: int = 2):
    accumulator = TickWindowAccumulator()
    for i in range(total_count):
        accumulator.consume(
            TickRecord(
                time=1000 + i,
                time_msc=(1000 + i) * 1000,
                bid=1.0,
                ask=1.1,
                last=0.0,
                volume=0.0,
                volume_real=0.0,
                flags=6,
            )
        )
    return accumulator.finalize(
        window=WINDOW,
        request_id="req-1",
        pid=None,
        source_id="clear",
        logical_id="win",
        resolved_symbol="WIN$",
        tick_type="all",
    )


def test_open_catalog_migrates_v1_owner_columns_without_losing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog" / "collection.sqlite3"
    db_path.parent.mkdir(parents=True)
    legacy_schema = catalog_module._SCHEMA_SQL
    for definition in (
        "    owner_pid INTEGER,\n",
        "    owner_process_started_at REAL,\n",
        "    owner_terminal_id TEXT,\n",
    ):
        legacy_schema = legacy_schema.replace(definition, "")
    legacy = sqlite3.connect(db_path)
    legacy.execute(legacy_schema)
    legacy.execute(
        """
        INSERT INTO backfill_sessions (
            source_id, logical_id, session_date, session_timezone, tick_type,
            state, attempts, catalog_schema_version, created_at, updated_at
        ) VALUES ('clear', 'win', '2026-08-28', 'America/Sao_Paulo', 'all',
                  'interrupted', 1, 1, '2026-08-30T00:00:00+00:00', '2026-08-30T00:00:00+00:00')
        """
    )
    legacy.commit()
    legacy.close()

    migrated = open_catalog(db_path)

    columns = {row[1] for row in migrated.execute("PRAGMA table_info(backfill_sessions)")}
    assert catalog_module._SCHEMA_V2_COLUMNS.keys() <= columns
    row = get_session(migrated, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["state"] == "interrupted"
    assert row["owner_pid"] is None


def test_begin_attempt_creates_a_running_row(tmp_path: Path) -> None:
    conn = _conn(tmp_path)

    row = begin_attempt(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
        rebuild=False,
    )

    assert row["state"] == "running"
    assert row["attempts"] == 1
    assert row["session_date"] == date(2026, 8, 28)
    assert row["attempt_id"]  # gerado, opaco, não vazio


def test_begin_attempt_persists_process_owner_identity(tmp_path: Path) -> None:
    conn = _conn(tmp_path)

    row = begin_attempt(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
        rebuild=False,
        owner_pid=4321,
        owner_process_started_at=1234.5,
        owner_terminal_id="clear-main",
    )

    assert row["owner_pid"] == 4321
    assert row["owner_process_started_at"] == 1234.5
    assert row["owner_terminal_id"] == "clear-main"
    assert list_running_sessions(conn) == [row]


@pytest.mark.parametrize(
    ("owner_pid", "owner_process_started_at", "owner_terminal_id"),
    [
        (4321, None, "clear-main"),
        (None, 1234.5, "clear-main"),
        (True, 1234.5, "clear-main"),
        (4321, True, "clear-main"),
        (4321, 1234.5, ""),
        (None, None, "clear-main"),
    ],
)
def test_begin_attempt_rejects_incomplete_or_invalid_owner_identity(
    tmp_path: Path,
    owner_pid,
    owner_process_started_at,
    owner_terminal_id,
) -> None:
    conn = _conn(tmp_path)

    with pytest.raises(ValueError):
        begin_attempt(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            session_timezone="America/Sao_Paulo",
            tick_type="all",
            requested_start_utc=WINDOW.start_utc,
            requested_end_utc=WINDOW.end_utc,
            rebuild=False,
            owner_pid=owner_pid,
            owner_process_started_at=owner_process_started_at,
            owner_terminal_id=owner_terminal_id,
        )


def test_begin_attempt_generates_a_new_attempt_id_on_every_call(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    kwargs = dict(
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
        rebuild=False,
    )
    first = begin_attempt(conn, **kwargs)
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=first["attempt_id"],
        message="parado",
    )
    second = begin_attempt(conn, **kwargs)

    assert first["attempt_id"] != second["attempt_id"]


def test_begin_attempt_refuses_a_second_concurrent_running_attempt(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    kwargs = dict(
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
        rebuild=False,
    )
    begin_attempt(conn, **kwargs)

    with pytest.raises(CatalogStateError, match="backfill_busy"):
        begin_attempt(conn, **kwargs)


def test_complete_then_begin_attempt_refuses_without_rebuild(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    kwargs = dict(
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
    )
    started = begin_attempt(conn, rebuild=False, **kwargs)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=123,
        sha256="deadbeef",
        schema_version=1,
        collector_version="dev-002-gate-a",
    )

    with pytest.raises(CatalogStateError, match="already_completed"):
        begin_attempt(conn, rebuild=False, **kwargs)

    # Nunca rebaixa nem sobrescreve silenciosamente um dia concluído:
    # a linha completed continua intacta após a tentativa recusada.
    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["state"] == "completed"

    # rebuild=True autoriza explicitamente refazer.
    rebuilt = begin_attempt(conn, rebuild=True, **kwargs)
    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["state"] == "running"
    assert row["attempts"] == 2
    assert rebuilt["attempt_id"] != started["attempt_id"]


def test_complete_session_marks_empty_reason_for_zero_ticks(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    started = begin_attempt(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
        rebuild=False,
    )

    row = complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=0),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=10,
        sha256="deadbeef",
        schema_version=1,
        collector_version="dev-002-gate-a",
    )

    assert row["state"] == "empty"
    assert row["empty_reason"] == "no_ticks_returned"
    assert row["tick_count"] == 0


def test_fail_session_records_structured_error(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    started = begin_attempt(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
        rebuild=False,
    )

    row = fail_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        reason="mt5_error",
        message="Invalid params",
        code=-2,
    )

    assert row["state"] == "failed"
    assert row["error_reason"] == "mt5_error"
    assert row["error_message"] == "Invalid params"
    assert row["error_code"] == -2


def test_interrupt_session_does_not_touch_other_completed_sessions(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    completed_kwargs = dict(
        source_id="clear",
        logical_id="win",
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
        rebuild=False,
    )
    day_one = begin_attempt(conn, session_date=date(2026, 8, 27), **completed_kwargs)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 27),
        attempt_id=day_one["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "prev.parquet",
        file_size_bytes=1,
        sha256="abc",
        schema_version=1,
        collector_version="dev-002-gate-a",
    )
    day_two = begin_attempt(conn, session_date=date(2026, 8, 28), **completed_kwargs)

    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=day_two["attempt_id"],
        message="Interrompido a pedido entre chunks.",
    )

    interrupted = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    previous = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 27))
    assert interrupted["state"] == "interrupted"
    assert previous["state"] == "completed"


def test_sources_and_logical_ids_never_collide_in_the_same_catalog(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    common = dict(
        session_date=date(2026, 8, 28),
        session_timezone="America/Sao_Paulo",
        tick_type="all",
        requested_start_utc=WINDOW.start_utc,
        requested_end_utc=WINDOW.end_utc,
        rebuild=False,
    )
    begin_attempt(conn, source_id="clear", logical_id="win", **common)
    begin_attempt(conn, source_id="clear", logical_id="wdo", **common)
    begin_attempt(conn, source_id="fot", logical_id="win", **common)

    assert get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert get_session(conn, source_id="clear", logical_id="wdo", session_date=date(2026, 8, 28))
    assert get_session(conn, source_id="fot", logical_id="win", session_date=date(2026, 8, 28))


def test_catalog_never_stores_raw_ticks(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(backfill_sessions)").fetchall()}

    for forbidden in ("bid", "ask", "last", "raw_ticks", "account_login", "password"):
        assert forbidden not in columns


# --- Correção de auditoria item 5: transições terminais condicionais ---

_COMMON_KWARGS = dict(
    session_timezone="America/Sao_Paulo",
    tick_type="all",
    requested_start_utc=WINDOW.start_utc,
    requested_end_utc=WINDOW.end_utc,
    rebuild=False,
)


def test_fail_session_after_completed_is_refused_and_row_stays_completed(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=1,
        sha256="abc",
        schema_version=1,
        collector_version="v",
    )

    with pytest.raises(CatalogStateError, match="invalid_transition"):
        fail_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            attempt_id=started["attempt_id"],
            reason="mt5_error",
            message="tardio",
        )

    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["state"] == "completed"
    assert row["error_reason"] is None


def test_interrupt_session_after_empty_is_refused_and_row_stays_empty(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=0),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=1,
        sha256="abc",
        schema_version=1,
        collector_version="v",
    )

    with pytest.raises(CatalogStateError, match="invalid_transition"):
        interrupt_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            attempt_id=started["attempt_id"],
            message="tardio",
        )

    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["state"] == "empty"


def test_complete_session_refuses_a_session_that_is_no_longer_running(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "a.parquet",
        file_size_bytes=1,
        sha256="first",
        schema_version=1,
        collector_version="v",
    )

    with pytest.raises(CatalogStateError, match="invalid_transition"):
        complete_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            attempt_id=started["attempt_id"],
            resolved_symbol="WIN$",
            summary=_summary(),
            file_path=tmp_path / "b.parquet",
            file_size_bytes=2,
            sha256="second",
            schema_version=1,
            collector_version="v",
        )

    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["sha256"] == "first"


def test_terminal_transition_without_begin_attempt_raises_no_such_session(tmp_path: Path) -> None:
    conn = _conn(tmp_path)

    with pytest.raises(CatalogStateError, match="no_such_session"):
        fail_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            attempt_id="attempt-x",
            reason="mt5_error",
            message="x",
        )


def test_interrupted_session_can_be_completed_by_a_fresh_begin_attempt(tmp_path: Path) -> None:
    """Uma sessão interrompida volta a `running` numa nova tentativa e pode
    ser concluída normalmente — só a transição a partir de um estado
    terminal já registrado, ou de uma tentativa superada, é que é recusada."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        message="parado",
    )

    resumed = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    row = complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=resumed["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=1,
        sha256="abc",
        schema_version=1,
        collector_version="v",
    )

    assert row["state"] == "completed"


# --- Correção de auditoria (terceira entrega) item 1: propriedade por tentativa ---


def test_complete_session_from_a_stale_attempt_id_is_refused_while_a_newer_attempt_runs(tmp_path: Path) -> None:
    """Reprodução do item 1: um evento tardio da tentativa A nunca pode
    atingir a tentativa B que a sucedeu, mesmo com B também `running`."""

    conn = _conn(tmp_path)
    attempt_a = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    # O manager detecta (talvez por engano) o worker como morto e libera a sessão.
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=attempt_a["attempt_id"],
        message="worker morto (detecção)",
    )
    attempt_b = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)

    # A tentativa A, na verdade ainda viva, tenta concluir tardiamente.
    with pytest.raises(CatalogStateError, match="stale_attempt"):
        complete_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            attempt_id=attempt_a["attempt_id"],
            resolved_symbol="WIN$",
            summary=_summary(),
            file_path=tmp_path / "stale.parquet",
            file_size_bytes=1,
            sha256="stale",
            schema_version=1,
            collector_version="v",
        )

    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["attempt_id"] == attempt_b["attempt_id"]
    assert row["state"] == "running"
    assert row["sha256"] is None


def test_fail_session_from_a_stale_attempt_id_is_refused_while_a_newer_attempt_runs(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    attempt_a = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=attempt_a["attempt_id"],
        message="worker morto (detecção)",
    )
    attempt_b = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)

    with pytest.raises(CatalogStateError, match="stale_attempt"):
        fail_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            attempt_id=attempt_a["attempt_id"],
            reason="mt5_error",
            message="tardio",
        )

    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["attempt_id"] == attempt_b["attempt_id"]
    assert row["state"] == "running"


# --- Correção de auditoria item 6 / terceira entrega item 2: reconciliação arquivo↔catálogo ---


def test_reconcile_session_returns_none_when_session_does_not_exist(tmp_path: Path) -> None:
    conn = _conn(tmp_path)

    result = reconcile_session(
        conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), file_present=False
    )

    assert result is None


def test_reconcile_session_promotes_a_stuck_running_row_when_the_file_is_valid(tmp_path: Path) -> None:
    """Simula: o Parquet foi promovido, mas `complete_session` nunca
    confirmou (ex.: o processo caiu logo depois). Aqui a sessão já não está
    mais `running` (foi interrompida antes) — o caso `running` de verdade é
    coberto por `test_reconcile_session_never_touches_a_running_row`."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        message="processo caiu antes de complete_session",
    )

    row = reconcile_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        file_present=True,
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=42),
        file_size_bytes=1234,
        file_sha256="realhash",
        file_path=tmp_path / "ticks.parquet",
        schema_version=1,
        collector_version="v",
    )

    assert row["state"] == "completed"
    assert row["sha256"] == "realhash"
    assert row["tick_count"] == 42
    assert row["reconciled"] == 1
    assert row["resolved_symbol"] == "WIN$"
    assert row["attempt_id"] == started["attempt_id"]


def test_reconcile_session_never_touches_a_running_row(tmp_path: Path) -> None:
    """Reprodução da evidência 1 (terceira auditoria): reconciliar nunca
    pode sobrepor uma tentativa ativa — a segunda chamada deve receber
    `backfill_busy` de `begin_attempt`, nunca uma reconciliação silenciosa."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)

    row = reconcile_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        file_present=True,
        attempt_id="some-other-attempt",
        resolved_symbol="WIN$",
        summary=_summary(total_count=99),
        file_size_bytes=1,
        file_sha256="shouldnotbewritten",
        file_path=tmp_path / "ticks.parquet",
        schema_version=1,
        collector_version="v",
    )

    assert row["state"] == "running"
    assert row["attempt_id"] == started["attempt_id"]
    assert row["sha256"] is None
    assert row["tick_count"] is None

    with pytest.raises(CatalogStateError, match="backfill_busy"):
        begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)


def test_reconcile_session_marks_empty_when_reconciled_file_has_zero_rows(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        message="parado",
    )

    row = reconcile_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        file_present=True,
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=0),
        file_size_bytes=200,
        file_sha256="emptyhash",
        file_path=tmp_path / "ticks.parquet",
        schema_version=1,
        collector_version="v",
    )

    assert row["state"] == "empty"
    assert row["empty_reason"] == "no_ticks_returned"


def test_reconcile_session_demotes_a_completed_row_whose_file_disappeared(tmp_path: Path) -> None:
    """Catálogo `completed` sem arquivo correspondente é rebaixado para um
    estado recuperável — nunca deixado apontando para nada."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=1,
        sha256="abc",
        schema_version=1,
        collector_version="v",
    )

    row = reconcile_session(
        conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), file_present=False
    )

    assert row["state"] == "failed"
    assert row["error_reason"] == "reconciliation_missing_file"

    # E, sendo `failed` (não mais `completed`), uma nova tentativa sem
    # rebuild volta a ser aceita — a ausência do arquivo libera a sessão.
    fresh = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    assert fresh["state"] == "running"


def test_reconcile_session_still_rewrites_when_hash_matches_but_metrics_diverge(tmp_path: Path) -> None:
    """Reprodução da evidência 3 (segunda auditoria) e do item 5 (terceira
    auditoria): `state+sha256` iguais não bastam. Um campo adulterado por
    fora do fluxo normal é corrigido mesmo sem o hash ter mudado."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=2),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=1,
        sha256="matching-hash",
        schema_version=1,
        collector_version="v",
    )
    # Adultera métricas diretamente, sem tocar o hash — simula uma
    # divergência que o fluxo normal nunca produziria sozinho.
    conn.execute(
        "UPDATE backfill_sessions SET first_tick_utc=?, non_zero_counts=? "
        "WHERE source_id=? AND logical_id=? AND session_date=?",
        ("2020-01-01T00:00:00+00:00", '{"bid": {"non_zero": 999, "total": 999}}', "clear", "win", "2026-08-28"),
    )
    before = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert before["non_zero_counts"]["bid"]["non_zero"] == 999  # confirma a adulteração

    # A linha já está `completed` (não `running`); reconcile_session pode
    # atuar sobre ela diretamente, sem passar por interrupt_session.
    after = reconcile_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        file_present=True,
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=2),
        file_size_bytes=1,
        file_sha256="matching-hash",
        file_path=tmp_path / "ticks.parquet",
        schema_version=1,
        collector_version="v",
    )

    assert after["sha256"] == "matching-hash"  # hash não mudou...
    assert after["non_zero_counts"]["bid"]["non_zero"] == 2  # ...mas a métrica foi corrigida


def test_reconciled_row_after_interrupted_rebuild_preserves_last_completed_artifact(tmp_path: Path) -> None:
    """rebuild interrompido: o arquivo antigo nunca chegou a ser
    substituído (a promoção é atômica só no final), então a reconciliação
    da próxima tentativa deve recuperar o `completed` a partir dele."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=1,
        sha256="original-good-file",
        schema_version=1,
        collector_version="v",
    )

    # rebuild=True começa uma nova tentativa (running) sem tocar o arquivo.
    rebuild_kwargs = dict(_COMMON_KWARGS)
    rebuild_kwargs["rebuild"] = True
    rebuilt = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **rebuild_kwargs)
    # ... e é interrompida antes de promover um novo arquivo.
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=rebuilt["attempt_id"],
        message="parado no meio",
    )

    # O arquivo físico nunca mudou (o `.partial` nunca chegou a substituí-lo)
    # — seu attempt_id embutido continua sendo o da tentativa original.
    recovered = reconcile_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        file_present=True,
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=2),
        file_size_bytes=1,
        file_sha256="original-good-file",
        file_path=tmp_path / "ticks.parquet",
        schema_version=1,
        collector_version="v",
    )

    assert recovered["state"] == "completed"
    assert recovered["sha256"] == "original-good-file"
    assert recovered["attempt_id"] == started["attempt_id"]


# --- Correção de auditoria item 8: erros do SQLite nunca escapam crus ---


def test_operations_on_a_closed_connection_raise_catalog_state_error_not_sqlite3_error(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    conn.close()

    with pytest.raises(CatalogStateError):
        begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 29), **_COMMON_KWARGS)
    with pytest.raises(CatalogStateError):
        get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    with pytest.raises(CatalogStateError):
        fail_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            attempt_id=started["attempt_id"],
            reason="mt5_error",
            message="x",
        )
    with pytest.raises(CatalogStateError):
        interrupt_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            attempt_id=started["attempt_id"],
            message="x",
        )
    with pytest.raises(CatalogStateError):
        reconcile_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), file_present=False)


def test_open_catalog_wraps_sqlite_errors_as_catalog_state_error(tmp_path: Path, monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated corruption")

    monkeypatch.setattr(sqlite3, "connect", _boom)

    with pytest.raises(CatalogStateError):
        open_catalog(tmp_path / "catalog" / "collection.sqlite3")


# --- Correção de auditoria item 9: concorrência SQLite real (duas conexões) ---


def test_two_real_connections_racing_begin_attempt_only_one_wins(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog" / "collection.sqlite3"
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def _attempt() -> None:
        conn = open_catalog(db_path)
        barrier.wait(timeout=5)
        try:
            begin_attempt(
                conn,
                source_id="clear",
                logical_id="win",
                session_date=date(2026, 8, 28),
                **_COMMON_KWARGS,
            )
            results.append(True)
        except CatalogStateError:
            results.append(False)
        finally:
            conn.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    assert results.count(True) == 1
    assert results.count(False) == 1

    conn = open_catalog(db_path)
    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["state"] == "running"
    assert row["attempts"] == 1


def test_two_real_connections_racing_rebuild_only_one_wins_and_the_loser_never_writes(tmp_path: Path) -> None:
    """Reprodução da evidência 1 (terceira auditoria) com conexões reais:
    duas conexões chamando `rebuild=True` na mesma sessão já `completed`
    terminam com um único dono; a perdedora nunca chega a reconciliar nem
    escrever nada."""

    db_path = tmp_path / "catalog" / "collection.sqlite3"
    setup_conn = open_catalog(db_path)
    started = begin_attempt(setup_conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        setup_conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=1,
        sha256="original",
        schema_version=1,
        collector_version="v",
    )
    setup_conn.close()

    rebuild_kwargs = dict(_COMMON_KWARGS)
    rebuild_kwargs["rebuild"] = True
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def _attempt() -> None:
        conn = open_catalog(db_path)
        barrier.wait(timeout=5)
        try:
            begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **rebuild_kwargs)
            results.append(True)
        except CatalogStateError:
            results.append(False)
        finally:
            conn.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results.count(True) == 1
    assert results.count(False) == 1

    conn = open_catalog(db_path)
    row = get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["state"] == "running"
    assert row["attempts"] == 2  # só uma nova tentativa foi de fato aceita


def test_lock_timeout_becomes_a_controlled_catalog_state_error(tmp_path: Path) -> None:
    """Uma conexão que segura o lock de escrita além do `busy_timeout` da
    outra produz um `CatalogStateError` controlado — nunca um travamento
    indefinido nem uma exceção crua do driver."""

    db_path = tmp_path / "catalog" / "collection.sqlite3"
    holder = open_catalog(db_path)
    holder.execute("PRAGMA busy_timeout=200")
    holder.execute("BEGIN IMMEDIATE")

    waiter = open_catalog(db_path)
    waiter.execute("PRAGMA busy_timeout=200")
    try:
        with pytest.raises(CatalogStateError, match="catalog_locked"):
            begin_attempt(
                waiter,
                source_id="clear",
                logical_id="win",
                session_date=date(2026, 8, 28),
                **_COMMON_KWARGS,
            )
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        waiter.close()


def test_late_interrupt_from_a_second_connection_never_rebases_a_completion(tmp_path: Path) -> None:
    """Simula um detector de worker morto (segunda conexão, possivelmente
    outra thread) tentando interromper uma sessão que já terminou de
    verdade entre a leitura de estado dele e a chamada."""

    db_path = tmp_path / "catalog" / "collection.sqlite3"
    conn_a = open_catalog(db_path)
    started = begin_attempt(conn_a, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn_a,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(),
        file_path=tmp_path / "ticks.parquet",
        file_size_bytes=1,
        sha256="abc",
        schema_version=1,
        collector_version="v",
    )
    conn_a.close()

    errors: list[CatalogStateError] = []

    def _late_interrupt() -> None:
        conn_b = open_catalog(db_path)
        try:
            interrupt_session(
                conn_b,
                source_id="clear",
                logical_id="win",
                session_date=date(2026, 8, 28),
                attempt_id=started["attempt_id"],
                message="detector de worker morto, tardio",
            )
        except CatalogStateError as exc:
            errors.append(exc)
        finally:
            conn_b.close()

    thread = threading.Thread(target=_late_interrupt)
    thread.start()
    thread.join(timeout=5)

    assert len(errors) == 1
    conn_c = open_catalog(db_path)
    row = get_session(conn_c, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))
    assert row["state"] == "completed"


# --- Correção de auditoria (segunda entrega) item 4: begin_attempt limpa a nova tentativa ---

_METRIC_COLUMNS = (
    "first_tick_utc",
    "last_tick_utc",
    "tick_count",
    "non_zero_counts",
    "flags_histogram",
    "out_of_order_count",
    "exact_duplicate_count",
    "time_msc_tie_count",
    "largest_gaps_seconds",
    "empty_reason",
    "file_path",
    "file_size_bytes",
    "sha256",
    "schema_version",
    "collector_version",
)


def test_begin_attempt_clears_every_stale_metric_from_a_previous_completion(tmp_path: Path) -> None:
    """Reprodução da auditoria: uma reconstrução não pode herdar
    first_tick_utc/non_zero_counts/flags_histogram/hash de uma conclusão
    anterior enquanto está `running`."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=96),
        file_path=tmp_path / "a.parquet",
        file_size_bytes=999,
        sha256="old-96-tick-hash",
        schema_version=1,
        collector_version="v",
    )

    rebuild_kwargs = dict(_COMMON_KWARGS)
    rebuild_kwargs["rebuild"] = True
    row = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **rebuild_kwargs)

    assert row["state"] == "running"
    assert row["reconciled"] == 0
    assert row["attempt_id"] != started["attempt_id"]
    for column in _METRIC_COLUMNS:
        assert row[column] is None, f"{column} deveria ter sido limpo, mas continua {row[column]!r}"


def test_begin_attempt_clears_stale_fields_even_on_a_fresh_session(tmp_path: Path) -> None:
    """A primeira tentativa de uma sessão nunca é criada com resíduo."""

    conn = _conn(tmp_path)
    row = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)

    for column in _METRIC_COLUMNS:
        assert row[column] is None


# --- Correção de auditoria (segunda entrega) item 5: fechamento em falha de inicialização ---


def test_open_catalog_closes_a_partially_opened_connection_on_pragma_failure(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, sqlite3.Connection] = {}

    class _FailingConnection(sqlite3.Connection):
        def execute(self, sql, *params):  # type: ignore[override]
            captured.setdefault("conn", self)
            if isinstance(sql, str) and sql.strip().upper().startswith("PRAGMA JOURNAL_MODE"):
                raise sqlite3.OperationalError("simulated failure during initialization")
            return super().execute(sql, *params)

    real_connect = sqlite3.connect

    def _connect(path, **kwargs):
        return real_connect(path, factory=_FailingConnection, **kwargs)

    import market_analytics.backfill_catalog as catalog_module

    monkeypatch.setattr(catalog_module.sqlite3, "connect", _connect)

    with pytest.raises(CatalogStateError):
        open_catalog(tmp_path / "catalog" / "collection.sqlite3")

    assert "conn" in captured
    with pytest.raises(sqlite3.ProgrammingError):
        captured["conn"].execute("SELECT 1")


# --- Correção de auditoria (segunda entrega) item 5: dados inválidos do catálogo ---


def test_get_session_normalizes_corrupt_json_into_catalog_state_error(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    conn.execute(
        "UPDATE backfill_sessions SET non_zero_counts=? WHERE source_id=? AND logical_id=? AND session_date=?",
        ("{not valid json", "clear", "win", "2026-08-28"),
    )

    with pytest.raises(CatalogStateError, match="catalog_corrupt"):
        get_session(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28))


# --- Correção de auditoria (segunda entrega) item 3: reconcile_session exige argumentos coerentes ---


def test_reconcile_session_requires_all_file_fields_when_file_present(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        message="parado",
    )

    with pytest.raises(ValueError):
        reconcile_session(
            conn,
            source_id="clear",
            logical_id="win",
            session_date=date(2026, 8, 28),
            file_present=True,
            # attempt_id/resolved_symbol/summary/etc. faltando de propósito.
        )


def test_reconcile_session_never_leaks_stale_metrics_from_a_previous_completion(tmp_path: Path) -> None:
    """Reprodução direta da evidência 3 (segunda auditoria): reconstruir
    vazia uma sessão que tinha 96 ticks não pode deixar first_tick_utc/
    non_zero_counts/flags_histogram antigos sobrevivendo à nova linha
    `empty`."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=96),
        file_path=tmp_path / "a.parquet",
        file_size_bytes=999,
        sha256="old-96-tick-hash",
        schema_version=1,
        collector_version="v",
    )

    rebuild_kwargs = dict(_COMMON_KWARGS)
    rebuild_kwargs["rebuild"] = True
    rebuilt = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **rebuild_kwargs)
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=rebuilt["attempt_id"],
        message="parado no meio, antes de promover o novo (vazio) arquivo",
    )

    row = reconcile_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        file_present=True,
        attempt_id=rebuilt["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=0),
        file_size_bytes=10,
        file_sha256="new-empty-file-hash",
        file_path=tmp_path / "b.parquet",
        schema_version=1,
        collector_version="v",
    )

    assert row["state"] == "empty"
    assert row["tick_count"] == 0
    assert row["first_tick_utc"] is None
    assert row["last_tick_utc"] is None
    assert row["non_zero_counts"]["bid"]["non_zero"] == 0
    assert row["flags_histogram"]["bid"] == 0
    assert row["sha256"] == "new-empty-file-hash"
    assert row["empty_reason"] == "no_ticks_returned"


def test_reconcile_session_inverse_direction_also_clears_stale_empty_fields(tmp_path: Path) -> None:
    """O inverso da evidência 3: reconstruir uma sessão vazia com um
    arquivo agora não vazio não pode manter `empty_reason` nem contagens
    zeradas antigas."""

    conn = _conn(tmp_path)
    started = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **_COMMON_KWARGS)
    complete_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=started["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=0),
        file_path=tmp_path / "a.parquet",
        file_size_bytes=10,
        sha256="old-empty-hash",
        schema_version=1,
        collector_version="v",
    )

    rebuild_kwargs = dict(_COMMON_KWARGS)
    rebuild_kwargs["rebuild"] = True
    rebuilt = begin_attempt(conn, source_id="clear", logical_id="win", session_date=date(2026, 8, 28), **rebuild_kwargs)
    interrupt_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        attempt_id=rebuilt["attempt_id"],
        message="parado no meio",
    )

    row = reconcile_session(
        conn,
        source_id="clear",
        logical_id="win",
        session_date=date(2026, 8, 28),
        file_present=True,
        attempt_id=rebuilt["attempt_id"],
        resolved_symbol="WIN$",
        summary=_summary(total_count=5),
        file_size_bytes=500,
        file_sha256="new-5-tick-hash",
        file_path=tmp_path / "b.parquet",
        schema_version=1,
        collector_version="v",
    )

    assert row["state"] == "completed"
    assert row["tick_count"] == 5
    assert row["empty_reason"] is None
    assert row["non_zero_counts"]["bid"]["non_zero"] == 5
