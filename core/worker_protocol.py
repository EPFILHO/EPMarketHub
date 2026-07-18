from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .terminal_states import WorkerConnectionState

WORKER_PROTOCOL_VERSION = 1

WORKER_COMMAND_TYPES = frozenset(
    {
        "stop",
        "snapshot",
        "update_symbols",
        "reconnect",
        "set_live_stream",
        "clear_live_stream",
        "clear_all_live_streams",
    }
)

WORKER_EVENT_TYPES = frozenset(
    {
        "started",
        "status",
        "snapshot",
        "live_status",
        "live_tick",
        "heartbeat",
        "terminal_restart_required",
        "error",
        "stopped",
    }
)

WORKER_STATE_EVENT_TYPES = frozenset(
    {
        "started",
        "status",
        "snapshot",
        "heartbeat",
        "terminal_restart_required",
        "error",
        "stopped",
    }
)

WORKER_IMMEDIATE_STATE_EVENT_TYPES = frozenset(
    {"started", "status", "terminal_restart_required", "error", "stopped"}
)


def worker_command(action: str, **data: Any) -> dict[str, Any]:
    if action not in WORKER_COMMAND_TYPES:
        raise ValueError(f"Comando de worker desconhecido: {action}")
    return {
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "action": action,
        **data,
    }


def valid_worker_command(command: Any) -> bool:
    return bool(
        isinstance(command, dict)
        and command.get("protocol_version") == WORKER_PROTOCOL_VERSION
        and command.get("action") in WORKER_COMMAND_TYPES
    )


def valid_worker_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    return bool(
        event.get("protocol_version") == WORKER_PROTOCOL_VERSION
        and str(event.get("terminal_id", "")).strip()
        and event.get("event") in WORKER_EVENT_TYPES
        and isinstance(event.get("data"), dict)
    )


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class WorkerState:
    """Estado observável de um worker MT5 no processo principal."""

    terminal_id: str
    state: str = WorkerConnectionState.STOPPED.value
    connected: bool = False
    alive: bool = False
    message: str = "Desconectado."
    pid: int | None = None
    account_login: int | None = None
    server: str | None = None
    company: str | None = None
    balance: float | None = None
    currency: str | None = None
    terminal_path: str | None = None
    started_at: str | None = None
    last_heartbeat: str | None = None
    last_snapshot: str | None = None
    reconnect_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def update(self, values: dict[str, Any]) -> None:
        allowed = set(self.__dataclass_fields__)
        for key, value in values.items():
            if key in allowed:
                setattr(self, key, value)


@dataclass
class WorkerEvent:
    terminal_id: str
    event: str
    protocol_version: int = WORKER_PROTOCOL_VERSION
    timestamp: str = field(default_factory=now_iso)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
