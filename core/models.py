from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class TerminalProfile:
    """Cadastro de uma instância MT5 controlada pelo EP Market Hub.

    Importante: esta estrutura NÃO armazena senha.
    O login deve ser feito manualmente no MT5 pelo usuário final.
    """

    id: str
    label: str
    broker_name: str = ""
    account_login: str = ""
    server: str = ""
    instance_slug: str = ""
    instance_dir: str = ""
    terminal_exe: str = ""
    enabled: bool = True
    portable: bool = True
    notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TerminalProfile":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in data.items() if k in allowed}
        return cls(**clean)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SymbolDefinition:
    """Ativo lógico do Market Hub.

    Ex.: Nasdaq 100 pode aparecer como US100, NAS100, USTEC etc.
    A interface deve permitir que o usuário escolha o símbolo real por terminal.
    """

    id: str
    name: str
    category: str
    aliases: list[str] = field(default_factory=list)
    role: list[str] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SymbolDefinition":
        return cls(
            id=str(data.get("id", "")).strip(),
            name=str(data.get("name", "")).strip(),
            category=str(data.get("category", "Personalizados")).strip(),
            aliases=[str(value).strip() for value in data.get("aliases", []) if str(value).strip()],
            role=[str(value).strip() for value in data.get("role", []) if str(value).strip()],
            enabled=bool(data.get("enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TerminalConnectionStatus:
    terminal_id: str
    ok: bool
    message: str
    account_login: Optional[int] = None
    server: Optional[str] = None
    company: Optional[str] = None
    balance: Optional[float] = None
    currency: Optional[str] = None
    terminal_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TickSnapshot:
    terminal_id: str
    symbol: str
    bid: Optional[float]
    ask: Optional[float]
    spread: Optional[float]
    time: Optional[str]
    ok: bool = True
    message: str = ""
    time_msc: Optional[int] = None
    received_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
