import queue
from pathlib import Path

from core.models import TerminalProfile
from core.mt5_worker import _emit, _emit_terminal_restart_required, _terminal_process_running
from core.worker_protocol import WORKER_PROTOCOL_VERSION


class CongestedQueue:
    def __init__(self, critical_succeeds: bool) -> None:
        self.critical_succeeds = critical_succeeds
        self.blocking_calls = []

    def put_nowait(self, payload) -> None:
        raise queue.Full

    def put(self, payload, timeout: float) -> None:
        self.blocking_calls.append((payload, timeout))
        if not self.critical_succeeds:
            raise queue.Full


def test_lossy_event_is_dropped_without_blocking_when_queue_is_full() -> None:
    event_queue = CongestedQueue(critical_succeeds=True)

    delivered = _emit(event_queue, "terminal-fake", "live_tick", {"tick": {}})

    assert delivered is False
    assert event_queue.blocking_calls == []


def test_critical_event_gets_bounded_delivery_attempt() -> None:
    event_queue = CongestedQueue(critical_succeeds=True)

    delivered = _emit(event_queue, "terminal-fake", "error", {"message": "falha"})

    assert delivered is True
    assert len(event_queue.blocking_calls) == 1
    assert event_queue.blocking_calls[0][1] == 0.25


def test_critical_event_failure_remains_non_blocking() -> None:
    event_queue = CongestedQueue(critical_succeeds=False)

    delivered = _emit(event_queue, "terminal-fake", "stopped", {})

    assert delivered is False
    assert len(event_queue.blocking_calls) == 1


class FakeProcess:
    def __init__(self, executable: str | None) -> None:
        self.info = {"exe": executable}


def test_terminal_process_detection_matches_exact_executable(monkeypatch, tmp_path: Path) -> None:
    terminal = tmp_path / "instance" / "terminal64.exe"
    other = tmp_path / "other" / "terminal64.exe"
    monkeypatch.setattr(
        "core.mt5_worker.psutil.process_iter",
        lambda fields: [FakeProcess(str(other)), FakeProcess(str(terminal))],
    )

    assert _terminal_process_running(str(terminal)) is True


def test_terminal_process_detection_does_not_accept_same_filename_elsewhere(
    monkeypatch,
    tmp_path: Path,
) -> None:
    terminal = tmp_path / "instance" / "terminal64.exe"
    other = tmp_path / "other" / "terminal64.exe"
    monkeypatch.setattr(
        "core.mt5_worker.psutil.process_iter",
        lambda fields: [FakeProcess(str(other)), FakeProcess(None)],
    )

    assert _terminal_process_running(str(terminal)) is False


def test_emitted_event_identifies_protocol_version() -> None:
    class Queue:
        payload = None

        def put_nowait(self, payload) -> None:
            self.payload = payload

    event_queue = Queue()

    assert _emit(event_queue, "terminal-fake", "heartbeat", {"pid": 123}) is True
    assert event_queue.payload["protocol_version"] == WORKER_PROTOCOL_VERSION


def test_restart_request_exposes_reopening_terminal_state() -> None:
    class Queue:
        payload = None

        def put_nowait(self, payload) -> None:
            self.payload = payload

    event_queue = Queue()
    profile = TerminalProfile(id="terminal-fake", label="Fake")

    _emit_terminal_restart_required(event_queue, profile, reconnect_attempts=2)

    assert event_queue.payload["event"] == "terminal_restart_required"
    assert event_queue.payload["data"]["state"] == "reopening_terminal"
    assert event_queue.payload["data"]["reconnect_attempts"] == 2
