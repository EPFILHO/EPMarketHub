from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class WorkerState:
    """Estado observável de um worker MT5 no processo principal."""

    terminal_id: str
    state: str = "stopped"
    connected: bool = False
    alive: bool = False
    message: str = "Leitura parada."
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
    timestamp: str = field(default_factory=now_iso)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
