import queue
from pathlib import Path

import pytest

from core.config import MAX_ACTIVE_TERMINALS
from core.models import TerminalProfile
from core.worker_manager import MT5WorkerManager
from core.worker_protocol import WORKER_PROTOCOL_VERSION


class FakeQueue:
    def __init__(self) -> None:
        self.items = []
        self.closed = False
        self.close_calls = 0
        self.full = False
        self.broken = False

    def put_nowait(self, item) -> None:
        if self.broken:
            raise ValueError("fila fechada")
        if self.full:
            raise queue.Full
        self.items.append(item)

    def get_nowait(self):
        if self.broken:
            raise ValueError("fila fechada")
        if self.items:
            return self.items.pop(0)
        raise queue.Empty

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    def join_thread(self) -> None:
        pass


class FakeEvent:
    def __init__(self) -> None:
        self.is_set = False
        self.broken = False

    def set(self) -> None:
        if self.broken:
            raise OSError("evento fechado")
        self.is_set = True


class FakeProcess:
    next_pid = 1000

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
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
        self.exitcode = -1

    def kill(self) -> None:
        self.alive = False
        self.exitcode = -9


class FailingStartProcess(FakeProcess):
    def start(self) -> None:
        raise OSError("falha simulada ao criar processo")


class KillRequiredProcess(FakeProcess):
    def join(self, timeout=None) -> None:
        pass

    def terminate(self) -> None:
        pass


class ResistantProcess(KillRequiredProcess):
    def kill(self) -> None:
        pass


class FakeContext:
    def __init__(self, process_type=FakeProcess) -> None:
        self.process_type = process_type
        self.queues = []

    def Queue(self, maxsize=0) -> FakeQueue:
        result = FakeQueue()
        self.queues.append(result)
        return result

    def Event(self) -> FakeEvent:
        return FakeEvent()

    def Process(self, **kwargs) -> FakeProcess:
        return self.process_type(**kwargs)


@pytest.fixture
def manager(monkeypatch) -> MT5WorkerManager:
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: FakeContext())
    return MT5WorkerManager(max_workers=3)


def profile(terminal_id: str, terminal_exe: str | None = None) -> TerminalProfile:
    path = terminal_exe or str(Path("sandbox") / terminal_id / "terminal64.exe")
    return TerminalProfile(
        id=terminal_id,
        label=f"Terminal {terminal_id}",
        broker_name="Broker Sandbox",
        account_login=f"FAKE-{terminal_id}",
        terminal_exe=path,
    )


def test_rejects_fourth_active_worker(manager: MT5WorkerManager) -> None:
    for terminal_id in ("one", "two", "three"):
        started, _ = manager.start_worker(profile(terminal_id), [])
        assert started is True

    started, message = manager.start_worker(profile("four"), [])

    assert started is False
    assert "Limite de 3" in message
    assert manager.active_count() == 3


@pytest.mark.parametrize("limit", [2, 3, 4])
def test_enforces_injected_product_limit(monkeypatch, limit: int) -> None:
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: FakeContext())
    manager = MT5WorkerManager(max_workers=limit)

    for index in range(limit):
        started, _ = manager.start_worker(profile(f"terminal-{index}"), [])
        assert started is True

    started, message = manager.start_worker(profile("beyond-limit"), [])

    assert started is False
    assert f"Limite de {limit}" in message
    assert manager.active_count() == limit


def test_default_limit_comes_from_central_product_policy(monkeypatch) -> None:
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: FakeContext())

    assert MT5WorkerManager().max_workers == MAX_ACTIVE_TERMINALS


def test_prevents_duplicate_worker_for_same_terminal(manager: MT5WorkerManager) -> None:
    terminal = profile("same")

    first_started, _ = manager.start_worker(terminal, [])
    second_started, message = manager.start_worker(terminal, [])

    assert first_started is True
    assert second_started is False
    assert "já está ativa" in message
    assert manager.active_count() == 1


def test_prevents_different_ids_from_sharing_terminal_executable(
    manager: MT5WorkerManager,
) -> None:
    shared_path = str(Path("sandbox") / "shared" / "terminal64.exe")
    manager.start_worker(profile("first", shared_path), [])

    started, message = manager.start_worker(profile("second", shared_path), [])

    assert started is False
    assert "terminal64.exe" in message
    assert manager.active_count() == 1


def test_stopping_one_worker_keeps_other_workers_alive(manager: MT5WorkerManager) -> None:
    for terminal_id in ("one", "two", "three"):
        manager.start_worker(profile(terminal_id), [])

    stopped, _ = manager.stop_worker("two")

    assert stopped is True
    assert manager.is_running("one") is True
    assert manager.is_running("two") is False
    assert manager.is_running("three") is True
    assert manager.active_count() == 2


def test_start_failure_releases_command_queue_and_records_error(monkeypatch) -> None:
    context = FakeContext(FailingStartProcess)
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: context)
    manager = MT5WorkerManager()

    started, message = manager.start_worker(profile("failing"), [])

    assert started is False
    assert "falha simulada" in message
    assert manager.is_running("failing") is False
    assert manager.state("failing").state == "worker_start_failed"
    assert context.queues[-1].closed is True


def test_full_command_queue_does_not_prevent_stop_event(monkeypatch) -> None:
    context = FakeContext()
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: context)
    manager = MT5WorkerManager()
    manager.start_worker(profile("full-queue"), [])
    handle = manager._handles["full-queue"]
    handle.command_queue.full = True

    stopped, _ = manager.stop_worker("full-queue", timeout=0)

    assert stopped is True
    assert handle.stop_event.is_set is True
    assert manager.is_running("full-queue") is False


def test_closed_command_queue_does_not_prevent_stop_event(monkeypatch) -> None:
    context = FakeContext()
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: context)
    manager = MT5WorkerManager()
    manager.start_worker(profile("closed-queue"), [])
    handle = manager._handles["closed-queue"]
    handle.command_queue.broken = True

    stopped, _ = manager.stop_worker("closed-queue", timeout=0)

    assert stopped is True
    assert handle.stop_event.is_set is True
    assert manager.is_running("closed-queue") is False


def test_broken_stop_event_falls_back_to_process_termination(monkeypatch) -> None:
    context = FakeContext(KillRequiredProcess)
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: context)
    manager = MT5WorkerManager()
    manager.start_worker(profile("broken-event"), [])
    handle = manager._handles["broken-event"]
    handle.stop_event.broken = True

    stopped, _ = manager.stop_worker("broken-event", timeout=0)

    assert stopped is True
    assert manager.is_running("broken-event") is False


def test_send_command_reports_closed_worker_queue(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("closed-command"), [])
    manager._handles["closed-command"].command_queue.broken = True

    sent, message = manager.request_snapshot("closed-command")

    assert sent is False
    assert "comunicação" in message


def test_poll_events_tolerates_closed_event_queue(manager: MT5WorkerManager) -> None:
    manager.event_queue.broken = True

    assert manager.poll_events() == []


def test_stop_all_is_idempotent_and_prevents_restart(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("one"), [])

    manager.stop_all()
    manager.stop_all()
    restarted, message = manager.start_worker(profile("two"), [])

    assert manager.event_queue.close_calls == 1
    assert restarted is False
    assert "encerrado" in message


def test_kill_is_used_when_worker_ignores_terminate(monkeypatch) -> None:
    context = FakeContext(KillRequiredProcess)
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: context)
    manager = MT5WorkerManager()
    manager.start_worker(profile("kill-required"), [])

    stopped, _ = manager.stop_worker("kill-required", timeout=0)

    assert stopped is True
    assert manager.is_running("kill-required") is False


def test_resistant_worker_is_not_reported_as_stopped(monkeypatch) -> None:
    context = FakeContext(ResistantProcess)
    monkeypatch.setattr("core.worker_manager.mp.get_context", lambda method: context)
    manager = MT5WorkerManager()
    manager.start_worker(profile("resistant"), [])

    stopped, message = manager.stop_worker("resistant", timeout=0)

    assert stopped is False
    assert "confirmar" in message
    assert manager.is_running("resistant") is True
    assert manager.state("resistant").state == "stop_failed"
    assert manager.state("resistant").alive is True


def test_ignores_residual_event_from_previous_worker_pid(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("current"), [])
    current_pid = manager.state("current").pid

    manager._apply_event(
        {
            "terminal_id": "current",
            "event": "heartbeat",
            "data": {
                "pid": current_pid + 100,
                "state": "connected",
                "connected": True,
                "message": "evento antigo",
            },
        }
    )

    state = manager.state("current")
    assert state.pid == current_pid
    assert state.state == "starting"
    assert state.connected is False


def test_residual_event_is_not_forwarded_to_bridge(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("current"), [])
    current_pid = manager.state("current").pid
    manager.event_queue.items.append(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "terminal_id": "current",
            "event": "heartbeat",
            "data": {
                "pid": current_pid + 100,
                "state": "connected",
                "connected": True,
            },
        }
    )

    assert manager.poll_events() == []


def test_current_worker_event_is_applied_and_forwarded(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("current"), [])
    current_pid = manager.state("current").pid
    event = {
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "terminal_id": "current",
        "event": "heartbeat",
        "data": {
            "pid": current_pid,
            "state": "connected",
            "connected": True,
        },
    }
    manager.event_queue.items.append(event)

    assert manager.poll_events() == [event]
    assert manager.state("current").connected is True


def test_alive_worker_without_activity_becomes_unresponsive(
    manager: MT5WorkerManager,
) -> None:
    manager.start_worker(profile("silent"), [])
    manager._last_activity["silent"] -= manager.unresponsive_seconds + 1

    events = manager.poll_events()

    assert len(events) == 1
    assert events[0]["event"] == "status"
    assert events[0]["data"]["state"] == "unresponsive"
    assert manager.state("silent").alive is True
    assert manager.state("silent").connected is False


def test_new_worker_event_recovers_unresponsive_state(manager: MT5WorkerManager) -> None:
    manager.start_worker(profile("silent"), [])
    current_pid = manager.state("silent").pid
    manager._last_activity["silent"] -= manager.unresponsive_seconds + 1
    manager.poll_events()
    manager.event_queue.items.append(
        {
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "terminal_id": "silent",
            "event": "heartbeat",
            "data": {
                "pid": current_pid,
                "state": "connected",
                "alive": True,
                "connected": True,
            },
        }
    )

    events = manager.poll_events()

    assert len(events) == 1
    assert manager.state("silent").state == "connected"
    assert manager.state("silent").connected is True


def test_unexpected_process_death_synthesizes_error_and_cleans_handle(
    manager: MT5WorkerManager,
) -> None:
    manager.start_worker(profile("dead"), [])
    process = manager._handles["dead"].process
    process.alive = False
    process.exitcode = 7

    events = manager.poll_events()

    assert len(events) == 1
    assert events[0]["event"] == "error"
    assert "código 7" in events[0]["data"]["message"]
    assert manager.state("dead").state == "worker_crashed"
    assert manager.state("dead").alive is False
    assert "dead" not in manager._handles


def test_dead_process_clears_alive_flag_after_worker_error_event(
    manager: MT5WorkerManager,
) -> None:
    manager.start_worker(profile("dead-after-error"), [])
    process = manager._handles["dead-after-error"].process
    manager._apply_event(
        {
            "terminal_id": "dead-after-error",
            "event": "error",
            "data": {
                "pid": process.pid,
                "state": "worker_crashed",
                "alive": False,
                "connected": False,
                "message": "falha simulada",
            },
        }
    )
    assert manager.state("dead-after-error").alive is True
    process.alive = False
    process.exitcode = 1

    manager.poll_events()

    assert manager.state("dead-after-error").state == "worker_crashed"
    assert manager.state("dead-after-error").alive is False
