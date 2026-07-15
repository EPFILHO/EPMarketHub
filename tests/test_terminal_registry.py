from pathlib import Path

from core.models import TerminalProfile
from core.terminal_registry import TerminalRegistry


def test_identity_is_case_and_spacing_insensitive(tmp_path: Path) -> None:
    registry = TerminalRegistry(tmp_path / "terminals.json")
    registry.upsert(
        TerminalProfile(
            id="terminal-1",
            label="Principal",
            broker_name="  FOT   Markets ",
            account_login=" 116486 ",
        )
    )

    duplicate = registry.find_by_identity("fot markets", "116486")

    assert duplicate is not None
    assert duplicate.id == "terminal-1"


def test_internal_id_survives_edit(tmp_path: Path) -> None:
    registry = TerminalRegistry(tmp_path / "terminals.json")
    profile = TerminalProfile(
        id="stable-id",
        label="Nome errado",
        broker_name="Corretora errada",
        account_login="42",
    )
    registry.upsert(profile)

    profile.label = "Conta B3"
    profile.broker_name = "Rico"
    registry.upsert(profile)

    loaded = registry.get("stable-id")
    assert loaded is not None
    assert loaded.label == "Conta B3"
    assert loaded.broker_name == "Rico"
