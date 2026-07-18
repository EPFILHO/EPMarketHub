from pathlib import Path

from core.terminal_states import (
    RECONNECT_ATTENTION_ATTEMPTS,
    InstanceIntegrityState,
    ProcessState,
    TerminalProcessStateMachine,
    WorkerConnectionState,
    account_identity_matches,
    classify_initialize_failure,
    state_after_reconnect_attempts,
    terminal_path_matches,
)


def test_instance_integrity_vocabulary_is_centralized() -> None:
    assert {state.value for state in InstanceIntegrityState} == {
        "ready",
        "directory_missing",
        "executable_missing",
        "invalid_path",
    }


def test_classifies_authentication_and_configuration_failures() -> None:
    assert (
        classify_initialize_failure(-6, "Terminal: Authorization failed")
        == WorkerConnectionState.AUTHENTICATION_FAILED.value
    )
    assert (
        classify_initialize_failure(None, "Biblioteca MetaTrader5 não instalada")
        == WorkerConnectionState.CONFIGURATION_ERROR.value
    )


def test_repeated_transient_failure_requires_attention_without_hiding_permanent_state() -> None:
    assert (
        state_after_reconnect_attempts(
            WorkerConnectionState.RECONNECTING.value,
            RECONNECT_ATTENTION_ATTEMPTS,
        )
        == WorkerConnectionState.ATTENTION_REQUIRED.value
    )
    assert (
        state_after_reconnect_attempts(
            WorkerConnectionState.AUTHENTICATION_FAILED.value,
            RECONNECT_ATTENTION_ATTEMPTS,
        )
        == WorkerConnectionState.AUTHENTICATION_FAILED.value
    )


def test_account_identity_accepts_numeric_format_but_rejects_another_account() -> None:
    assert account_identity_matches("000123", 123) is True
    assert account_identity_matches("123", 456) is False


def test_terminal_path_accepts_directory_or_executable(tmp_path: Path) -> None:
    terminal_exe = tmp_path / "instance" / "terminal64.exe"

    assert terminal_path_matches(str(terminal_exe), str(terminal_exe.parent)) is True
    assert terminal_path_matches(str(terminal_exe), str(terminal_exe)) is True
    assert terminal_path_matches(str(terminal_exe), str(tmp_path / "other")) is False


def test_process_state_machine_keeps_transitions_and_detects_duplicates() -> None:
    states = TerminalProcessStateMachine()
    states.set("one", ProcessState.OPENING)

    assert states.resolve("one", running=True, process_count=1) == ProcessState.OPENING.value

    states.clear("one")
    assert states.resolve("one", running=True, process_count=1) == ProcessState.OPEN.value

    states.set("one", ProcessState.CLOSE_FAILED)
    assert (
        states.resolve("one", running=True, process_count=1)
        == ProcessState.CLOSE_FAILED.value
    )
    assert states.resolve("one", running=False, process_count=0) == ProcessState.CLOSED.value
    assert (
        states.resolve("one", running=True, process_count=2)
        == ProcessState.DUPLICATE.value
    )


def test_completing_startup_does_not_hide_process_failures() -> None:
    states = TerminalProcessStateMachine()
    states.set("opening", ProcessState.OPENING)
    states.set("reopening", ProcessState.REOPENING)
    states.set("launch-failed", ProcessState.LAUNCH_FAILED)
    states.set("close-failed", ProcessState.CLOSE_FAILED)

    for terminal_id in ("opening", "reopening", "launch-failed", "close-failed"):
        states.complete_startup(terminal_id)

    assert states.resolve("opening", running=True, process_count=1) == ProcessState.OPEN.value
    assert states.resolve("reopening", running=True, process_count=1) == ProcessState.OPEN.value
    assert (
        states.resolve("launch-failed", running=False, process_count=0)
        == ProcessState.LAUNCH_FAILED.value
    )
    assert (
        states.resolve("close-failed", running=True, process_count=1)
        == ProcessState.CLOSE_FAILED.value
    )
