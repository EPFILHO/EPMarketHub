from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class InstanceIntegrityState(StrEnum):
    """Integridade da pasta controlada registrada para uma instância MT5."""

    READY = "ready"
    DIRECTORY_MISSING = "directory_missing"
    EXECUTABLE_MISSING = "executable_missing"
    INVALID_PATH = "invalid_path"


class ProcessState(StrEnum):
    """Ciclo de vida observável do processo terminal64.exe."""

    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    REOPENING = "reopening"
    LAUNCH_FAILED = "launch_failed"
    CLOSE_FAILED = "close_failed"
    DUPLICATE = "duplicate_process"


class WorkerConnectionState(StrEnum):
    """Estado do worker e de sua conexão exclusiva com um MT5."""

    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    CONNECTED = "connected"
    WAITING_LOGIN = "waiting_login"
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCOUNT_MISMATCH = "account_mismatch"
    BROKER_DISCONNECTED = "broker_disconnected"
    RECONNECTING = "reconnecting"
    REOPENING_TERMINAL = "reopening_terminal"
    CONFIGURATION_ERROR = "configuration_error"
    TERMINAL_MISMATCH = "terminal_mismatch"
    UNRESPONSIVE = "unresponsive"
    ATTENTION_REQUIRED = "attention_required"
    WORKER_START_FAILED = "worker_start_failed"
    WORKER_CRASHED = "worker_crashed"
    STOP_FAILED = "stop_failed"
    ERROR = "error"


WORKER_UNRESPONSIVE_SECONDS = 15.0
RECONNECT_ATTENTION_ATTEMPTS = 12

_AUTHENTICATION_MARKERS = (
    "auth failed",
    "authentication failed",
    "authorization failed",
    "invalid account",
    "invalid login",
    "invalid password",
)
_COMMUNICATION_MARKERS = (
    "ipc",
    "send failed",
    "internal fail",
    "pipe",
    "timeout",
)
_CONFIGURATION_MARKERS = (
    "not installed",
    "não instalada",
    "nao instalada",
    "terminal64.exe não encontrado",
    "terminal64.exe nao encontrado",
)

RETRYABLE_CONNECTION_STATES = frozenset(
    {
        WorkerConnectionState.BROKER_DISCONNECTED.value,
        WorkerConnectionState.RECONNECTING.value,
    }
)

PROCESS_OPEN_STATES = frozenset(
    {
        ProcessState.OPEN.value,
        ProcessState.OPENING.value,
        ProcessState.CLOSING.value,
        ProcessState.REOPENING.value,
        ProcessState.CLOSE_FAILED.value,
        ProcessState.DUPLICATE.value,
    }
)


def classify_initialize_failure(code: int | None, message: str | None) -> str:
    """Classifica falhas do ``MetaTrader5.initialize`` sem esconder erros permanentes."""

    text = str(message or "").casefold()
    if code == -6 or any(marker in text for marker in _AUTHENTICATION_MARKERS):
        return WorkerConnectionState.AUTHENTICATION_FAILED.value
    if any(marker in text for marker in _CONFIGURATION_MARKERS):
        return WorkerConnectionState.CONFIGURATION_ERROR.value
    return WorkerConnectionState.RECONNECTING.value


def is_communication_error(message: str | None) -> bool:
    text = str(message or "").casefold()
    return any(marker in text for marker in _COMMUNICATION_MARKERS)


def state_after_reconnect_attempts(state: str, reconnect_attempts: int) -> str:
    if state in RETRYABLE_CONNECTION_STATES and reconnect_attempts >= RECONNECT_ATTENTION_ATTEMPTS:
        return WorkerConnectionState.ATTENTION_REQUIRED.value
    return state


def account_identity_matches(expected: str, connected: int | str | None) -> bool:
    expected_text = str(expected or "").strip()
    connected_text = str(connected or "").strip()
    if not expected_text or not connected_text:
        return True
    if expected_text.isdecimal() and connected_text.isdecimal():
        return int(expected_text) == int(connected_text)
    return expected_text.casefold() == connected_text.casefold()


def terminal_path_matches(expected_executable: str, connected_path: str | None) -> bool:
    """Aceita o diretório ou o executável retornado por ``terminal_info().path``."""

    if not connected_path:
        return True
    expected = Path(expected_executable).resolve()
    connected = Path(connected_path).resolve()
    if connected.name.casefold() == "terminal64.exe":
        connected = connected.parent
    return str(expected.parent).casefold() == str(connected).casefold()


class TerminalProcessStateMachine:
    """Mantém somente transições que não podem ser inferidas da tabela de processos."""

    def __init__(self) -> None:
        self._transitions: dict[str, str] = {}

    def set(self, terminal_id: str, state: ProcessState | str) -> None:
        self._transitions[terminal_id] = str(state)

    def clear(self, terminal_id: str) -> None:
        self._transitions.pop(terminal_id, None)

    def complete_startup(self, terminal_id: str) -> None:
        """Conclui apenas abertura/reabertura, preservando falhas operacionais."""

        if self._transitions.get(terminal_id) in {
            ProcessState.OPENING.value,
            ProcessState.REOPENING.value,
        }:
            self.clear(terminal_id)

    def forget(self, terminal_id: str) -> None:
        self.clear(terminal_id)

    def resolve(
        self,
        terminal_id: str,
        *,
        running: bool,
        process_count: int,
    ) -> str:
        if process_count > 1:
            return ProcessState.DUPLICATE.value

        transition = self._transitions.get(terminal_id)
        if transition == ProcessState.CLOSING.value and not running:
            self.clear(terminal_id)
            return ProcessState.CLOSED.value
        if transition == ProcessState.CLOSE_FAILED.value and not running:
            self.clear(terminal_id)
            return ProcessState.CLOSED.value
        if transition == ProcessState.LAUNCH_FAILED.value and running:
            self.clear(terminal_id)
            return ProcessState.OPEN.value
        if transition in {
            ProcessState.OPENING.value,
            ProcessState.REOPENING.value,
        } and not running:
            return transition
        if transition:
            return transition
        return ProcessState.OPEN.value if running else ProcessState.CLOSED.value
