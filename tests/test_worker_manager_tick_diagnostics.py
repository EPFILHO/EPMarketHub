from __future__ import annotations

import queue
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.models import TerminalProfile
from core.worker_manager import MT5WorkerManager
from core.worker_protocol import WORKER_PROTOCOL_VERSION
from market_analytics.tick_diagnostics import TickWindow, TickWindowRequest

START_UTC = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)


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
    next_pid = 2000

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
def manager(monkeypatch) -> MT5WorkerManager:
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: FakeContext())
    return MT5WorkerManager(max_workers=3)


def profile(terminal_id: str) -> TerminalProfile:
    return TerminalProfile(
        id=terminal_id,
        label=f"Terminal {terminal_id}",
        terminal_exe=str(Path("sandbox") / terminal_id / "terminal64.exe"),
    )


def make_request(**overrides) -> TickWindowRequest:
    window = TickWindow(start_utc=START_UTC, end_utc=START_UTC + timedelta(minutes=2))
    defaults = dict(
        request_id="req-1",
        logical_id="win",
        aliases=("WIN$",),
        tick_type="all",
        windows=(window,),
        chunk_seconds=60,
    )
    defaults.update(overrides)
    return TickWindowRequest(**defaults)


def test_request_requires_a_running_worker(manager: MT5WorkerManager) -> None:
    sent, message = manager.request_tick_diagnostic("absent", make_request())

    assert sent is False
    assert "Inicie a leitura" in message


def test_request_sends_command_and_registers_pending_entry(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    request = make_request()

    sent, _ = manager.request_tick_diagnostic("terminal-1", request)

    assert sent is True
    command = manager._handles["terminal-1"].command_queue.items[-1]
    assert command["action"] == "diagnose_ticks"
    assert command["request"]["request_id"] == "req-1"
    status = manager.tick_diagnostic_status("req-1")
    assert status["state"] == "pending"
    assert status["terminal_id"] == "terminal-1"


def test_repeating_same_request_id_with_same_parameters_does_not_resend(
    manager: MT5WorkerManager,
) -> None:
    manager.start_worker(profile("terminal-1"), [])
    request = make_request()
    manager.request_tick_diagnostic("terminal-1", request)

    sent, message = manager.request_tick_diagnostic("terminal-1", make_request())

    assert sent is True
    assert "reaproveitando" in message
    assert len(manager._handles["terminal-1"].command_queue.items) == 1


def test_repeating_same_request_id_with_different_parameters_conflicts(
    manager: MT5WorkerManager,
) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.request_tick_diagnostic("terminal-1", make_request())

    sent, message = manager.request_tick_diagnostic("terminal-1", make_request(chunk_seconds=90))

    assert sent is False
    assert "request_id_conflict" in message
    assert len(manager._handles["terminal-1"].command_queue.items) == 1


def test_same_request_id_and_payload_sent_to_a_different_terminal_conflicts(
    manager: MT5WorkerManager,
) -> None:
    """COR-DEV-001 item 1 (regressão): a identidade da solicitação inclui o
    terminal. O mesmo request_id com payload idêntico não pode ser
    reaproveitado em outro terminal_id, e nenhum comando pode ser enviado ao
    segundo worker."""

    manager.start_worker(profile("terminal-1"), [])
    manager.start_worker(profile("terminal-2"), [])
    request = make_request()

    first_sent, _ = manager.request_tick_diagnostic("terminal-1", request)
    second_sent, message = manager.request_tick_diagnostic("terminal-2", make_request())

    assert first_sent is True
    assert second_sent is False
    assert "request_id_conflict" in message
    assert len(manager._handles["terminal-1"].command_queue.items) == 1
    assert manager._handles["terminal-2"].command_queue.items == []
    assert manager.tick_diagnostic_status("req-1")["terminal_id"] == "terminal-1"


def test_conflict_persists_even_after_the_original_request_completed(
    manager: MT5WorkerManager,
) -> None:
    manager.start_worker(profile("terminal-1"), [])
    current_pid = manager.state("terminal-1").pid
    manager.request_tick_diagnostic("terminal-1", make_request())
    manager.event_queue.items.append(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "terminal_id": "terminal-1",
            "event": "tick_diagnostic_completed",
            "data": {"request_id": "req-1", "pid": current_pid},
        }
    )
    manager.poll_events()
    assert manager.tick_diagnostic_status("req-1")["state"] == "completed"

    sent, message = manager.request_tick_diagnostic("terminal-1", make_request(chunk_seconds=90))

    assert sent is False
    assert "request_id_conflict" in message


def test_diagnostic_events_update_status_without_touching_worker_state(
    manager: MT5WorkerManager,
) -> None:
    manager.start_worker(profile("terminal-1"), [])
    current_pid = manager.state("terminal-1").pid
    manager.request_tick_diagnostic("terminal-1", make_request())
    baseline_state = manager.state("terminal-1").state

    for event_type, extra in (
        ("tick_diagnostic_accepted", {"resolved_symbol": "WIN$"}),
        ("tick_diagnostic_window_result", {"summary": {"total_count": 3}}),
        ("tick_diagnostic_completed", {}),
    ):
        manager.event_queue.items.append(
            {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "terminal_id": "terminal-1",
                "event": event_type,
                "data": {"request_id": "req-1", "pid": current_pid, **extra},
            }
        )
        manager.poll_events()

    status = manager.tick_diagnostic_status("req-1")
    assert status["state"] == "completed"
    assert status["windows"] == [{"total_count": 3}]
    # O estado de conexão do terminal nunca foi tocado por esses eventos.
    assert manager.state("terminal-1").state == baseline_state


def test_failed_diagnostic_event_does_not_become_a_generic_worker_error(
    manager: MT5WorkerManager,
) -> None:
    manager.start_worker(profile("terminal-1"), [])
    current_pid = manager.state("terminal-1").pid
    manager.request_tick_diagnostic("terminal-1", make_request())

    manager.event_queue.items.append(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "terminal_id": "terminal-1",
            "event": "tick_diagnostic_failed",
            "data": {"request_id": "req-1", "pid": current_pid, "reason": "symbol_not_found"},
        }
    )
    events = manager.poll_events()

    assert events[0]["event"] == "tick_diagnostic_failed"
    assert manager.tick_diagnostic_status("req-1")["state"] == "failed"
    assert manager.tick_diagnostic_status("req-1")["error"]["reason"] == "symbol_not_found"
    assert manager.state("terminal-1").state == "starting"


def test_failed_diagnostic_event_preserves_code_and_message_separately(
    manager: MT5WorkerManager,
) -> None:
    """COR-DEV-001 item 5 (regressão): `code`/`message` de `last_error()`
    chegam íntegros ao estado guardado pelo manager, não concatenados."""

    manager.start_worker(profile("terminal-1"), [])
    current_pid = manager.state("terminal-1").pid
    manager.request_tick_diagnostic("terminal-1", make_request())

    manager.event_queue.items.append(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "terminal_id": "terminal-1",
            "event": "tick_diagnostic_failed",
            "data": {
                "request_id": "req-1",
                "pid": current_pid,
                "reason": "mt5_error",
                "message": "Invalid params",
                "code": -2,
            },
        }
    )
    manager.poll_events()

    error = manager.tick_diagnostic_status("req-1")["error"]
    assert error["code"] == -2
    assert error["message"] == "Invalid params"


def test_dead_worker_interrupts_pending_tick_diagnostic(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.request_tick_diagnostic("terminal-1", make_request())
    process = manager._handles["terminal-1"].process
    process.alive = False
    process.exitcode = 7

    manager.poll_events()

    status = manager.tick_diagnostic_status("req-1")
    assert status["state"] == "interrupted"
    assert status["error"]["reason"] == "worker_unavailable"
    assert manager.state("terminal-1").state == "worker_crashed"


def test_unresponsive_worker_interrupts_pending_tick_diagnostic(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.request_tick_diagnostic("terminal-1", make_request())
    manager._last_activity["terminal-1"] -= manager.unresponsive_seconds + 1

    manager.poll_events()

    status = manager.tick_diagnostic_status("req-1")
    assert status["state"] == "interrupted"
    assert manager.state("terminal-1").state == "unresponsive"


def test_forget_terminal_clears_its_tick_diagnostics(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("terminal-1"), [])
    manager.request_tick_diagnostic("terminal-1", make_request())

    manager.forget_terminal("terminal-1")

    assert manager.tick_diagnostic_status("req-1") is None
