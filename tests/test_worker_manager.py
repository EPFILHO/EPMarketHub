import queue
from pathlib import Path

import pytest

from core.models import TerminalProfile
from core.worker_manager import MT5WorkerManager


class FakeQueue:
    def __init__(self) -> None:
        self.items = []
        self.closed = False

    def put_nowait(self, item) -> None:
        self.items.append(item)

    def get_nowait(self):
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
