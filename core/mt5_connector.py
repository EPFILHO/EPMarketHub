from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import TerminalConnectionStatus, TerminalProfile, TickSnapshot

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # pragma: no cover - permite abrir o app sem MT5 instalado
    mt5 = None


class MT5Connector:
    """Uma conexão persistente com exatamente um terminal MT5.

    A biblioteca MetaTrader5 mantém estado global por processo. Por isso cada
    instância desta classe deve viver em um worker/processo exclusivo.
    """

    def __init__(self, profile: TerminalProfile):
        self.profile = profile
        self.initialized = False

    def initialize(self) -> TerminalConnectionStatus:
        if mt5 is None:
            return TerminalConnectionStatus(
                terminal_id=self.profile.id,
                ok=False,
                message="Biblioteca MetaTrader5 não instalada. Rode: pip install MetaTrader5",
                terminal_path=self.profile.terminal_exe,
            )

        terminal_path = Path(self.profile.terminal_exe)
        if not terminal_path.exists():
            return TerminalConnectionStatus(
                terminal_id=self.profile.id,
                ok=False,
                message=f"terminal64.exe não encontrado: {terminal_path}",
                terminal_path=str(terminal_path),
            )

        if not self.initialized:
            initialized = mt5.initialize(
                path=str(terminal_path),
                portable=self.profile.portable,
            )
            if not initialized:
                code, msg = mt5.last_error()
                return TerminalConnectionStatus(
                    terminal_id=self.profile.id,
                    ok=False,
                    message=f"Falha ao inicializar MT5: {code} - {msg}",
                    terminal_path=str(terminal_path),
                )
            self.initialized = True

        return self.connection_status()

    def connection_status(self) -> TerminalConnectionStatus:
        if mt5 is None:
            return TerminalConnectionStatus(
                terminal_id=self.profile.id,
                ok=False,
                message="Biblioteca MetaTrader5 não instalada.",
                terminal_path=self.profile.terminal_exe,
            )

        if not self.initialized:
            return TerminalConnectionStatus(
                terminal_id=self.profile.id,
                ok=False,
                message="Conexão MT5 ainda não inicializada.",
                terminal_path=self.profile.terminal_exe,
            )

        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account is None:
            code, msg = mt5.last_error()
            suffix = f" ({code} - {msg})" if code or msg else ""
            return TerminalConnectionStatus(
                terminal_id=self.profile.id,
                ok=False,
                message="MT5 aberto, mas sem conta logada. Faça login manual no terminal." + suffix,
                terminal_path=self.profile.terminal_exe,
            )

        connected = bool(getattr(terminal, "connected", True)) if terminal else True
        if not connected:
            return TerminalConnectionStatus(
                terminal_id=self.profile.id,
                ok=False,
                message="Terminal encontrado, mas sem conexão com a corretora.",
                account_login=getattr(account, "login", None),
                server=getattr(account, "server", None),
                company=getattr(account, "company", None),
                balance=getattr(account, "balance", None),
                currency=getattr(account, "currency", None),
                terminal_path=getattr(terminal, "path", self.profile.terminal_exe) if terminal else self.profile.terminal_exe,
            )

        return TerminalConnectionStatus(
            terminal_id=self.profile.id,
            ok=True,
            message="Conexão persistente ativa.",
            account_login=getattr(account, "login", None),
            server=getattr(account, "server", None),
            company=getattr(account, "company", None),
            balance=getattr(account, "balance", None),
            currency=getattr(account, "currency", None),
            terminal_path=getattr(terminal, "path", self.profile.terminal_exe) if terminal else self.profile.terminal_exe,
        )

    def shutdown(self) -> None:
        if mt5 is not None and self.initialized:
            mt5.shutdown()
        self.initialized = False

    def list_symbol_states(self) -> dict[str, dict[str, Any]]:
        """Retorna símbolos e metadados úteis para escolher a variante ativa.

        ``visible``/``select`` indicam apenas a Observação do Mercado. O critério
        principal é ``trade_mode``: corretoras podem manter ``US30`` listado mas
        desativado, enquanto ``US30Cash`` está habilitado.
        """

        if mt5 is None:
            return {}
        if not self.initialized:
            status = self.initialize()
            if not status.ok:
                return {}
        rows = mt5.symbols_get() or []
        disabled_mode = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(getattr(row, "name", "") or "")
            if not name:
                continue
            trade_mode = getattr(row, "trade_mode", None)
            bid = getattr(row, "bid", None)
            ask = getattr(row, "ask", None)
            last = getattr(row, "last", None)
            quote_values = (bid, ask, last)
            has_quote = any(
                value is not None and isinstance(value, (int, float)) and value != 0
                for value in quote_values
            )
            result[name] = {
                "name": name,
                "trade_mode": trade_mode,
                "tradable": trade_mode is None or trade_mode != disabled_mode,
                "visible": bool(getattr(row, "visible", False)),
                "selected": bool(getattr(row, "select", False)),
                "has_quote": has_quote,
                "bid": bid,
                "ask": ask,
                "last": last,
            }
        return result

    def list_symbols(self) -> list[str]:
        return sorted(self.list_symbol_states())

    def resolve_symbol(self, aliases: list[str]) -> str | None:
        available = set(self.list_symbols())
        for alias in aliases:
            if alias in available:
                return alias
        lower_map = {name.lower(): name for name in available}
        for alias in aliases:
            found = lower_map.get(alias.lower())
            if found:
                return found
        return None

    def get_tick(self, symbol: str) -> TickSnapshot:
        if mt5 is None:
            return TickSnapshot(
                self.profile.id,
                symbol,
                None,
                None,
                None,
                None,
                False,
                "MetaTrader5 não instalado",
            )
        if not self.initialized:
            status = self.initialize()
            if not status.ok:
                return TickSnapshot(
                    self.profile.id,
                    symbol,
                    None,
                    None,
                    None,
                    None,
                    False,
                    status.message,
                )

        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            code, msg = mt5.last_error()
            return TickSnapshot(
                self.profile.id,
                symbol,
                None,
                None,
                None,
                None,
                False,
                f"Tick indisponível: {code} - {msg}",
            )
        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)
        spread = (ask - bid) if bid is not None and ask is not None else None
        ts = getattr(tick, "time", None)
        time_msc = getattr(tick, "time_msc", None)
        dt = datetime.fromtimestamp(ts).isoformat(timespec="milliseconds") if ts else None
        received_at = datetime.now().isoformat(timespec="milliseconds")
        return TickSnapshot(
            self.profile.id,
            symbol,
            bid,
            ask,
            spread,
            dt,
            True,
            "",
            time_msc=time_msc,
            received_at=received_at,
        )

    def get_rates(self, symbol: str, timeframe: Any, count: int = 200) -> list[dict[str, Any]]:
        if mt5 is None:
            return []
        if not self.initialized:
            status = self.initialize()
            if not status.ok:
                return []
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None:
            return []
        result: list[dict[str, Any]] = []
        for row in rates:
            item = dict(zip(row.dtype.names, row.tolist())) if hasattr(row, "dtype") else dict(row)
            if "time" in item:
                item["time_iso"] = datetime.fromtimestamp(item["time"]).isoformat(timespec="seconds")
            result.append(item)
        return result
