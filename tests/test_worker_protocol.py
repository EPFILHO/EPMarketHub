import pytest

from core.worker_protocol import (
    WORKER_PROTOCOL_VERSION,
    WorkerEvent,
    valid_worker_command,
    valid_worker_event,
    worker_command,
)


def test_worker_command_has_version_and_known_action() -> None:
    command = worker_command("snapshot")

    assert command == {
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "action": "snapshot",
    }
    assert valid_worker_command(command) is True


def test_unknown_worker_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="desconhecido"):
        worker_command("unknown")


def test_command_from_another_protocol_version_is_rejected() -> None:
    command = worker_command("stop")
    command["protocol_version"] += 1

    assert valid_worker_command(command) is False


def test_worker_event_has_version_and_valid_payload() -> None:
    event = WorkerEvent(
        terminal_id="terminal-fake",
        event="status",
        data={"pid": 123, "state": "connected"},
    ).to_dict()

    assert event["protocol_version"] == WORKER_PROTOCOL_VERSION
    assert valid_worker_event(event) is True


@pytest.mark.parametrize(
    "change",
    [
        {"protocol_version": 99},
        {"terminal_id": ""},
        {"event": "unknown"},
        {"data": "invalid"},
    ],
)
def test_invalid_worker_event_is_rejected(change: dict) -> None:
    event = WorkerEvent(
        terminal_id="terminal-fake",
        event="heartbeat",
        data={"pid": 123},
    ).to_dict()
    event.update(change)

    assert valid_worker_event(event) is False
