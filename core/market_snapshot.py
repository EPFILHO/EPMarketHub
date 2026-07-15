from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any, Iterable

from .models import SymbolDefinition, TerminalProfile
from .mt5_connector import MT5Connector


def resolve_symbol_aliases(
    aliases: list[str],
    available: set[str],
    symbol_states: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    """Resolve o melhor alias disponível, priorizando símbolos ativos.

    O MT5 pode expor simultaneamente, por exemplo, ``US30`` desativado e
    ``US30Cash`` tradável. Apenas estar listado em ``symbols_get()`` não torna
    um símbolo adequado. Quando os metadados estão disponíveis, a seleção
    prioriza:

    1. símbolo com negociação habilitada (``trade_mode`` não desativado);
    2. símbolo que já possui cotação válida;
    3. correspondência exata antes de padrão;
    4. ordem dos aliases cadastrados.

    Se todos os nomes encontrados estiverem desativados, retorna ``None`` em
    vez de escolher silenciosamente o contrato errado.
    """

    cleaned = [str(alias).strip() for alias in aliases if str(alias).strip()]
    states = symbol_states or {}
    lower_map = {name.casefold(): name for name in available}
    candidates: dict[str, tuple[int, int]] = {}

    def register(name: str, match_kind: int, alias_index: int) -> None:
        current = candidates.get(name)
        rank = (match_kind, alias_index)
        if current is None or rank < current:
            candidates[name] = rank

    for alias_index, alias in enumerate(cleaned):
        wildcard = "*" in alias or "?" in alias
        if not wildcard:
            if alias in available:
                register(alias, 0, alias_index)
            found = lower_map.get(alias.casefold())
            if found:
                register(found, 1, alias_index)
            continue

        pattern = alias.casefold()
        for name in available:
            if fnmatchcase(name.casefold(), pattern):
                register(name, 2, alias_index)

    if not candidates:
        return None

    def candidate_rank(item: tuple[str, tuple[int, int]]) -> tuple[int, int, int, int, int, str]:
        name, (match_kind, alias_index) = item
        state = states.get(name, {})
        tradable = bool(state.get("tradable", True))
        has_quote = bool(state.get("has_quote", False))
        visible = bool(state.get("visible", False))
        return (
            0 if tradable else 1,
            0 if has_quote else 1,
            match_kind,
            alias_index,
            0 if visible else 1,
            name.casefold(),
        )

    ordered = sorted(candidates.items(), key=candidate_rank)
    for name, _ in ordered:
        if bool(states.get(name, {}).get("tradable", True)):
            return name
    return None


def build_snapshot_from_connector(
    profile: TerminalProfile,
    connector: MT5Connector,
    symbols: Iterable[SymbolDefinition],
) -> dict[str, Any]:
    """Monta um snapshot usando uma conexão MT5 já inicializada.

    Esta função não inicializa nem encerra a biblioteca MetaTrader5. O worker é
    o dono exclusivo da conexão e a mantém viva durante todo o seu ciclo.
    """

    status = connector.connection_status()
    ticks: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []

    if status.ok:
        symbol_states = connector.list_symbol_states()
        available = set(symbol_states)
        for logical in symbols:
            if not logical.enabled:
                continue
            symbol = resolve_symbol_aliases(logical.aliases, available, symbol_states)
            resolved.append(
                {
                    "logical_id": logical.id,
                    "name": logical.name,
                    "category": logical.category,
                    "symbol": symbol,
                    "aliases": logical.aliases,
                    "found": bool(symbol),
                }
            )
            if symbol:
                tick = connector.get_tick(symbol)
                row = tick.to_dict()
                row.update(
                    {
                        "logical_id": logical.id,
                        "name": logical.name,
                        "category": logical.category,
                    }
                )
                ticks.append(row)

    return {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "terminal": profile.to_dict(),
        "status": status.to_dict(),
        "resolved_symbols": resolved,
        "ticks": ticks,
    }


class MarketSnapshotService:
    """Compatibilidade para testes unitários com apenas um terminal.

    A GUI multi-MT5 usa workers persistentes e não deve chamar este serviço.
    """

    def __init__(self, symbol_registry):
        self.symbol_registry = symbol_registry

    def build_for_terminal(self, profile: TerminalProfile) -> dict[str, Any]:
        connector = MT5Connector(profile)
        try:
            connector.initialize()
            return build_snapshot_from_connector(
                profile,
                connector,
                self.symbol_registry.list(enabled_only=True),
            )
        finally:
            connector.shutdown()
