from __future__ import annotations

import queue
from datetime import date
from pathlib import Path

import pytest

from core.models import TerminalProfile
from core.worker_manager import MT5WorkerManager
from core.worker_protocol import WORKER_PROTOCOL_VERSION
from market_analytics.backfill_catalog import begin_attempt, get_session, open_catalog
from market_analytics.tick_backfill import BackfillSessionRequest, catalog_db_path

SESSION_DATE = date(2026, 8, 28)


class FakeQueue:
    def __init__(self) -> None:
        self.items: list = []
        self.closed = False

    def put_nowait(self, item) -> None:
        self.items.append(item)

    def get_nowait(self):
        if self.items:
            return self.items.pop(0)
        raise queue.Empty

    def close(self) -> None:
        self.closed = True

    def join_thread(self) -> None:
        pass


class FakeEvent:
    def __init__(self) -> None:
        self.is_set = False

    def set(self) -> None:
        self.is_set = True


class FakeProcess:
    next_pid = 3000

    def __init__(self, **kwargs) -> None:
        self.alive = False
        self.exitcode = None
        self.pid = None

    def start(self) -> None:
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout=None) -> None:
        self.alive = False
        self.exitcode = 0

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False


class FakeContext:
    def Queue(self, maxsize=0) -> FakeQueue:
        return FakeQueue()

    def Event(self) -> FakeEvent:
        return FakeEvent()

    def Process(self, **kwargs) -> FakeProcess:
        return FakeProcess(**kwargs)


@pytest.fixture
def manager(monkeypatch, tmp_path: Path) -> MT5WorkerManager:
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: FakeContext())
    return MT5WorkerManager(max_workers=3, backfill_data_root=tmp_path)


def profile(terminal_id: str) -> TerminalProfile:
    return TerminalProfile(
        id=terminal_id, label=f"Terminal {terminal_id}", terminal_exe=str(Path("sandbox") / terminal_id / "terminal64.exe")
    )


def make_request(**overrides) -> BackfillSessionRequest:
    defaults: dict = dict(
        request_id="bf-1",
        source_id="clear",
        logical_id="win",
        aliases=("WIN$",),
        tick_type="all",
        session_date=SESSION_DATE,
        chunk_seconds=900,
    )
    defaults.update(overrides)
    return BackfillSessionRequest(**defaults)


def test_request_requires_a_running_worker(manager: MT5WorkerManager) -> None:
    sent, message = manager.request_backfill("absent", make_request())

    assert sent is False
    assert "Inicie a leitura" in message


def test_request_sends_command_and_registers_pending_entry(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    request = make_request()

    sent, _ = manager.request_backfill("terminal-1", request)

    assert sent is True
    command = manager._handles["terminal-1"].command_queue.items[-1]
    assert command["action"] == "start_backfill"
    assert command["request"]["request_id"] == "bf-1"
    status = manager.backfill_status("bf-1")
    assert status["state"] == "pending"
    assert status["terminal_id"] == "terminal-1"
    assert status["source_id"] == "clear"
    assert status["logical_id"] == "win"
    assert status["session_date"] == SESSION_DATE


def test_repeating_same_request_id_with_same_parameters_does_not_resend(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.request_backfill("terminal-1", make_request())

    sent, message = manager.request_backfill("terminal-1", make_request())

    assert sent is True
    assert "reaproveitando" in message
    assert len(manager._handles["terminal-1"].command_queue.items) == 1


def test_repeating_same_request_id_with_different_parameters_conflicts(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.request_backfill("terminal-1", make_request())

    sent, message = manager.request_backfill("terminal-1", make_request(chunk_seconds=300))

    assert sent is False
    assert "request_id_conflict" in message
    assert len(manager._handles["terminal-1"].command_queue.items) == 1


def test_same_request_id_sent_to_a_different_terminal_conflicts(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.start_worker(profile("terminal-2"), [])

    first_sent, _ = manager.request_backfill("terminal-1", make_request())
    second_sent, message = manager.request_backfill("terminal-2", make_request())

    assert first_sent is True
    assert second_sent is False
    assert "request_id_conflict" in message
    assert manager._handles["terminal-2"].command_queue.items == []


def test_backfill_events_update_status_without_touching_worker_state(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    current_pid = manager.state("terminal-1").pid
    manager.request_backfill("terminal-1", make_request())
    baseline_state = manager.state("terminal-1").state

    for event_type, extra in (
        ("backfill_accepted", {"resolved_symbol": "WIN$"}),
        ("backfill_progress", {"chunk_index": 3, "chunk_count": 96}),
        ("backfill_completed", {"state": "completed", "row_count": 96}),
    ):
        manager.event_queue.items.append(
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "terminal_id": "terminal-1",
                "event": event_type,
                "data": {"request_id": "bf-1", "pid": current_pid, **extra},
            }
        )
        manager.poll_events()

    status = manager.backfill_status("bf-1")
    assert status["state"] == "completed"
    assert status["result"]["row_count"] == 96
    assert manager.state("terminal-1").state == baseline_state


def test_failed_backfill_event_does_not_become_a_generic_worker_error(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    current_pid = manager.state("terminal-1").pid
    manager.request_backfill("terminal-1", make_request())

    manager.event_queue.items.append(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "terminal_id": "terminal-1",
            "event": "backfill_failed",
            "data": {"request_id": "bf-1", "pid": current_pid, "reason": "symbol_not_found"},
        }
    )
    events = manager.poll_events()

    assert events[0]["event"] == "backfill_failed"
    assert manager.backfill_status("bf-1")["state"] == "failed"
    assert manager.backfill_status("bf-1")["error"]["reason"] == "symbol_not_found"
    assert manager.state("terminal-1").state == "starting"


def test_stop_backfill_sends_command_only_while_pending_or_running(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.request_backfill("terminal-1", make_request())

    sent, _ = manager.stop_backfill("bf-1")

    assert sent is True
    command = manager._handles["terminal-1"].command_queue.items[-1]
    assert command["action"] == "stop_backfill"
    assert command["request_id"] == "bf-1"


def test_stop_backfill_refuses_an_unknown_request(manager: MT5WorkerManager) -> None:
    sent, message = manager.stop_backfill("absent")

    assert sent is False
    assert "não está registrado" in message


def test_dead_worker_interrupts_pending_backfill_and_releases_catalog_lock(
    manager: MT5WorkerManager, tmp_path: Path
) -> None:
    manager.start_worker(profile("terminal-1"), [])
    request = make_request()
    manager.request_backfill("terminal-1", request)

    # Simula o worker real tendo reservado a sessão no catálogo persistente
    # (BEGIN_ATTEMPT já rodou dentro do processo do worker) antes de morrer.
    worker_side_conn = open_catalog(catalog_db_path(tmp_path))
    started = begin_attempt(
        worker_side_conn,
        source_id=request.source_id,
        logical_id=request.logical_id,
        session_date=request.session_date,
        session_timezone=request.session_timezone,
        tick_type=request.tick_type,
        requested_start_utc=request.session_window().start_utc,
        requested_end_utc=request.session_window().end_utc,
        rebuild=False,
    )
    worker_side_conn.close()

    # E que o evento backfill_accepted correspondente já tinha chegado ao
    # manager, carregando o attempt_id opaco gerado por begin_attempt — sem
    # isso o manager não saberia qual tentativa liberar no catálogo.
    current_pid = manager.state("terminal-1").pid
    manager.event_queue.items.append(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "terminal_id": "terminal-1",
            "event": "backfill_accepted",
            "data": {"request_id": "bf-1", "pid": current_pid, "attempt_id": started["attempt_id"]},
        }
    )
    manager.poll_events()
    assert manager.backfill_status("bf-1")["attempt_id"] == started["attempt_id"]

    process = manager._handles["terminal-1"].process
    process.alive = False
    process.exitcode = 9

    manager.poll_events()

    status = manager.backfill_status("bf-1")
    assert status["state"] == "interrupted"
    assert manager.state("terminal-1").state == "worker_crashed"

    # A sessão não fica presa em "running": uma nova tentativa sem rebuild
    # volta a ser aceita pelo catálogo.
    verify_conn = open_catalog(catalog_db_path(tmp_path))
    row = get_session(verify_conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date)
    assert row["state"] == "interrupted"
    begin_attempt(
        verify_conn,
        source_id=request.source_id,
        logical_id=request.logical_id,
        session_date=request.session_date,
        session_timezone=request.session_timezone,
        tick_type=request.tick_type,
        requested_start_utc=request.session_window().start_utc,
        requested_end_utc=request.session_window().end_utc,
        rebuild=False,
    )


def test_unresponsive_worker_never_releases_backfill_catalog_ownership(
    manager: MT5WorkerManager, tmp_path: Path
) -> None:
    """Correção da quarta auditoria, item 2: ausência temporária de
    heartbeat com o processo ainda vivo marca a UI como sem resposta, mas
    nunca chama `interrupt_session` — a propriedade do catálogo (a linha
    `running`) tem que sobreviver intacta a uma chamada MT5 síncrona longa."""

    manager.start_worker(profile("terminal-1"), [])
    request = make_request()
    manager.request_backfill("terminal-1", request)

    # O worker real reservou a sessão no catálogo persistente e o evento de
    # aceite já chegou ao manager, exatamente como no fluxo normal.
    worker_side_conn = open_catalog(catalog_db_path(tmp_path))
    started = begin_attempt(
        worker_side_conn,
        source_id=request.source_id,
        logical_id=request.logical_id,
        session_date=request.session_date,
        session_timezone=request.session_timezone,
        tick_type=request.tick_type,
        requested_start_utc=request.session_window().start_utc,
        requested_end_utc=request.session_window().end_utc,
        rebuild=False,
    )
    worker_side_conn.close()

    current_pid = manager.state("terminal-1").pid
    manager.event_queue.items.append(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "terminal_id": "terminal-1",
            "event": "backfill_accepted",
            "data": {"request_id": "bf-1", "pid": current_pid, "attempt_id": started["attempt_id"]},
        }
    )
    manager.poll_events()
    assert manager.backfill_status("bf-1")["state"] == "running"

    manager._last_activity["terminal-1"] -= manager.unresponsive_seconds + 1
    manager.poll_events()

    # A UI reflete "sem resposta"...
    assert manager.state("terminal-1").state == "unresponsive"
    # ...mas o backfill continua "running" em memória, e o catálogo nunca
    # foi tocado (o worker segue vivo e dono da sessão).
    status = manager.backfill_status("bf-1")
    assert status["state"] == "running"
    assert manager._backfill_catalog_conn is None

    verify_conn = open_catalog(catalog_db_path(tmp_path))
    row = get_session(
        verify_conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert row["state"] == "running"
    assert row["attempt_id"] == started["attempt_id"]


def test_dead_worker_recovers_a_lost_accepted_event_from_the_catalog_itself(
    manager: MT5WorkerManager, tmp_path: Path
) -> None:
    """Correção da quarta auditoria, item 3: o worker morre depois de
    `begin_attempt`, mas antes do evento `backfill_accepted` chegar — o
    manager nunca aprendeu o `attempt_id` (bookkeeping em memória fica com
    `attempt_id=None`). Na morte confirmada, o manager consulta a sessão
    correspondente diretamente no catálogo e recupera o `attempt_id` de lá,
    liberando a linha `running`."""

    manager.start_worker(profile("terminal-1"), [])
    request = make_request()
    manager.request_backfill("terminal-1", request)
    assert manager.backfill_status("bf-1")["attempt_id"] is None  # evento de aceite nunca chegou

    # O worker real reservou a sessão no catálogo persistente antes de
    # morrer, sem que o evento `backfill_accepted` alcançasse o manager.
    worker_side_conn = open_catalog(catalog_db_path(tmp_path))
    started = begin_attempt(
        worker_side_conn,
        source_id=request.source_id,
        logical_id=request.logical_id,
        session_date=request.session_date,
        session_timezone=request.session_timezone,
        tick_type=request.tick_type,
        requested_start_utc=request.session_window().start_utc,
        requested_end_utc=request.session_window().end_utc,
        rebuild=False,
    )
    worker_side_conn.close()

    process = manager._handles["terminal-1"].process
    process.alive = False
    process.exitcode = 9

    manager.poll_events()

    status = manager.backfill_status("bf-1")
    assert status["state"] == "interrupted"

    verify_conn = open_catalog(catalog_db_path(tmp_path))
    row = get_session(
        verify_conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert row["state"] == "interrupted"
    assert row["attempt_id"] == started["attempt_id"]

    # A sessão não fica presa em "running": uma nova tentativa volta a ser aceita.
    begin_attempt(
        verify_conn,
        source_id=request.source_id,
        logical_id=request.logical_id,
        session_date=request.session_date,
        session_timezone=request.session_timezone,
        tick_type=request.tick_type,
        requested_start_utc=request.session_window().start_utc,
        requested_end_utc=request.session_window().end_utc,
        rebuild=False,
    )


def test_dead_worker_with_a_lost_accepted_event_never_touches_another_workers_session(
    manager: MT5WorkerManager, tmp_path: Path
) -> None:
    """A recuperação do evento de aceite perdido nunca libera a sessão de
    outro dono: se OUTRO pedido rastreado por este manager (outro terminal)
    ainda reivindica ativamente a mesma sessão — e portanto pode ser o dono
    real da linha `running` do catálogo, não o worker morto — a
    ambiguidade nunca é resolvida liberando a sessão às cegas."""

    manager.start_worker(profile("terminal-1"), [])
    manager.start_worker(profile("terminal-2"), [])
    request = make_request()
    manager.request_backfill("terminal-1", request)
    # Mesma sessão (source_id/logical_id/session_date), outro terminal, outro
    # request_id — o manager permite isso e, sem essa checagem, não teria
    # como saber qual dos dois de fato virou o dono "running" no catálogo.
    manager.request_backfill("terminal-2", make_request(request_id="bf-2"))

    # Um dos dois workers reservou a sessão no catálogo persistente antes de
    # o terminal-1 morrer — não importa qual, o teste é sobre a ambiguidade.
    worker_side_conn = open_catalog(catalog_db_path(tmp_path))
    started = begin_attempt(
        worker_side_conn,
        source_id=request.source_id,
        logical_id=request.logical_id,
        session_date=request.session_date,
        session_timezone=request.session_timezone,
        tick_type=request.tick_type,
        requested_start_utc=request.session_window().start_utc,
        requested_end_utc=request.session_window().end_utc,
        rebuild=False,
    )
    worker_side_conn.close()

    process = manager._handles["terminal-1"].process
    process.alive = False
    process.exitcode = 9

    manager.poll_events()

    verify_conn = open_catalog(catalog_db_path(tmp_path))
    row = get_session(
        verify_conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
    )
    assert row["state"] == "running"
    assert row["attempt_id"] == started["attempt_id"]


def test_forget_terminal_clears_its_backfills(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.request_backfill("terminal-1", make_request())

    manager.forget_terminal("terminal-1")

    assert manager.backfill_status("bf-1") is None
