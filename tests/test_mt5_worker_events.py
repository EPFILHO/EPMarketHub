import queue

from core.mt5_worker import _emit


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
