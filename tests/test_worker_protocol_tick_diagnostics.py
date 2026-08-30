from core.worker_protocol import (
    WORKER_EVENT_TYPES,
    WORKER_IMMEDIATE_STATE_EVENT_TYPES,
    WORKER_STATE_EVENT_TYPES,
    WorkerEvent,
    valid_worker_command,
    valid_worker_event,
    worker_command,
)

TICK_DIAGNOSTIC_EVENTS = {
    "tick_diagnostic_accepted",
    "tick_diagnostic_window_result",
    "tick_diagnostic_completed",
    "tick_diagnostic_failed",
}


def test_diagnose_ticks_command_is_accepted() -> None:
    command = worker_command("diagnose_ticks", request={"request_id": "r1"})

    assert valid_worker_command(command) is True


def test_diagnose_ticks_command_from_wrong_protocol_version_is_rejected() -> None:
    command = worker_command("diagnose_ticks", request={})
    command["protocol_version"] += 1

    assert valid_worker_command(command) is False


def test_tick_diagnostic_events_are_valid_worker_events() -> None:
    for event_type in TICK_DIAGNOSTIC_EVENTS:
        event = WorkerEvent(
            terminal_id="terminal-fake",
            event=event_type,
            data={"request_id": "r1", "pid": 123},
        ).to_dict()

        assert valid_worker_event(event) is True


def test_tick_diagnostic_events_are_registered_in_worker_event_types() -> None:
    assert TICK_DIAGNOSTIC_EVENTS.issubset(WORKER_EVENT_TYPES)


def test_tick_diagnostic_events_do_not_drive_connection_state_classification() -> None:
    """Falha de diagnóstico não pode ser confundida com falha de conexão do
    worker: os eventos de diagnóstico ficam fora dos conjuntos que alimentam
    o estado de conexão observável do terminal."""

    assert TICK_DIAGNOSTIC_EVENTS.isdisjoint(WORKER_STATE_EVENT_TYPES)
    assert TICK_DIAGNOSTIC_EVENTS.isdisjoint(WORKER_IMMEDIATE_STATE_EVENT_TYPES)


def test_generic_error_event_is_not_reused_for_diagnostic_failure() -> None:
    assert "error" not in TICK_DIAGNOSTIC_EVENTS
