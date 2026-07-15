from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .json_store import load_json, save_json_atomic
from .models import SymbolDefinition


class SymbolRegistry:
    """Cadastro de ativos lógicos, editável pela interface."""

    def __init__(self, symbols_file: Path):
        self.symbols_file = symbols_file

    def list(self, enabled_only: bool = False) -> list[SymbolDefinition]:
        rows = load_json(self.symbols_file, [])
        if not isinstance(rows, list):
            return []
        symbols = [SymbolDefinition.from_dict(row) for row in rows if isinstance(row, dict)]
        if enabled_only:
            symbols = [s for s in symbols if s.enabled]
        return symbols

    def get(self, symbol_id: str) -> SymbolDefinition | None:
        return next((symbol for symbol in self.list() if symbol.id == symbol_id), None)

    def upsert(self, symbol: SymbolDefinition) -> SymbolDefinition:
        rows = self.list()
        replaced = False
        for idx, existing in enumerate(rows):
            if existing.id == symbol.id:
                rows[idx] = symbol
                replaced = True
                break
        if not replaced:
            rows.append(symbol)
        self._save(rows)
        return symbol

    def ensure_defaults(self, defaults: Iterable[SymbolDefinition]) -> int:
        """Acrescenta ativos ausentes e migra o contrato digitado incorretamente."""

        rows = self.list()
        changed = False

        # Migração da Base 0.3.1: WING26 foi um erro de digitação; o correto é WINQ26.
        old = next((row for row in rows if row.id == "wing26"), None)
        current = next((row for row in rows if row.id == "winq26"), None)
        if old and not current:
            old.id = "winq26"
            old.name = "Índice Futuro Atual (WINQ26)"
            old.aliases = ["WINQ26"]
            changed = True
        elif old and current:
            rows = [row for row in rows if row.id != "wing26"]
            changed = True

        by_id = {row.id: row for row in rows}
        existing_ids = set(by_id)
        added = 0
        for symbol in defaults:
            existing = by_id.get(symbol.id)
            if existing is not None:
                # Defaults novos (por exemplo, US30Cash) entram sem apagar aliases
                # personalizados já cadastrados pelo usuário.
                merged_aliases = list(existing.aliases)
                alias_keys = {alias.casefold() for alias in merged_aliases}
                for alias in symbol.aliases:
                    if alias.casefold() not in alias_keys:
                        merged_aliases.append(alias)
                        alias_keys.add(alias.casefold())
                if merged_aliases != existing.aliases:
                    existing.aliases = merged_aliases
                    changed = True
                continue
            rows.append(symbol)
            by_id[symbol.id] = symbol
            existing_ids.add(symbol.id)
            added += 1
            changed = True
        if changed:
            self._save(rows)
        return added

    def remove(self, symbol_id: str) -> bool:
        rows = self.list()
        new_rows = [s for s in rows if s.id != symbol_id]
        self._save(new_rows)
        return len(new_rows) != len(rows)

    def _save(self, rows: list[SymbolDefinition]) -> None:
        save_json_atomic(self.symbols_file, [row.to_dict() for row in rows])
