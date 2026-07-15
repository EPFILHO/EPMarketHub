from pathlib import Path

from core.default_symbols import DEFAULT_SYMBOLS
from core.models import SymbolDefinition
from core.symbol_registry import SymbolRegistry


def test_ensure_defaults_preserves_custom_aliases() -> None:
    custom = SymbolDefinition(
        id="dowjones",
        name="Dow Jones personalizado",
        category="Teste",
        aliases=["CUSTOM.US30", "US30"],
    )
    registry = SymbolRegistry.__new__(SymbolRegistry)
    rows = [custom]
    registry.list = lambda enabled_only=False: rows
    registry._save = lambda values: rows.__setitem__(slice(None), values)

    registry.ensure_defaults(DEFAULT_SYMBOLS)

    loaded = next(symbol for symbol in rows if symbol.id == "dowjones")
    assert loaded.aliases[0] == "CUSTOM.US30"
    assert "US30Cash" in loaded.aliases
    assert loaded.name == "Dow Jones personalizado"


def test_ensure_defaults_migrates_wing26_to_winq26(tmp_path: Path) -> None:
    path = tmp_path / "symbols.json"
    registry = SymbolRegistry(path)
    registry.upsert(
        SymbolDefinition(
            id="wing26",
            name="Contrato antigo de teste",
            category="Brasil / B3",
            aliases=["WING26"],
        )
    )

    registry.ensure_defaults(DEFAULT_SYMBOLS)

    assert registry.get("wing26") is None
    migrated = registry.get("winq26")
    assert migrated is not None
    assert migrated.aliases == ["WINQ26"]


def test_symbol_registry_round_trip_preserves_fake_definition(tmp_path: Path) -> None:
    path = tmp_path / "symbols.json"
    registry = SymbolRegistry(path)
    expected = SymbolDefinition(
        id="asset-fake",
        name="Ativo de demonstração",
        category="Sandbox",
        aliases=["FAKE.A", "FAKE-A*"],
        role=["test_only"],
        enabled=False,
    )

    registry.upsert(expected)
    loaded = SymbolRegistry(path).get("asset-fake")

    assert loaded is not None
    assert loaded.to_dict() == expected.to_dict()
