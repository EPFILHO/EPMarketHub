from pathlib import Path

from core.models import TerminalProfile
from core.terminal_registry import TerminalRegistry


def test_identity_is_case_and_spacing_insensitive(tmp_path: Path) -> None:
    registry = TerminalRegistry(tmp_path / "terminals.json")
    registry.upsert(
        TerminalProfile(
            id="terminal-1",
            label="Principal",
            broker_name="  Broker   Sandbox ",
            account_login=" FAKE-001 ",
        )
    )

    duplicate = registry.find_by_identity("broker sandbox", "fake-001")

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


def test_registry_accepts_more_terminals_than_runtime_worker_limit(tmp_path: Path) -> None:
    registry = TerminalRegistry(tmp_path / "terminals.json")

    for index in range(8):
        registry.upsert(
            TerminalProfile(
                id=f"terminal-{index}",
                label=f"Terminal de teste {index}",
                broker_name="Broker Sandbox",
                account_login=f"FAKE-{index:03d}",
            )
        )

    assert len(registry.list()) == 8


def test_terminal_registry_round_trip_uses_only_fake_data(tmp_path: Path) -> None:
    path = tmp_path / "terminals.json"
    registry = TerminalRegistry(path)
    profile = TerminalProfile(
        id="terminal-fake",
        label="Conta de demonstração",
        broker_name="Broker Sandbox",
        account_login="FAKE-ROUNDTRIP",
        instance_slug="BROKER-SANDBOX-FAKE-ROUNDTRIP",
        instance_dir="C:/sandbox/BROKER-SANDBOX-FAKE-ROUNDTRIP",
        terminal_exe="C:/sandbox/BROKER-SANDBOX-FAKE-ROUNDTRIP/terminal64.exe",
    )

    registry.upsert(profile)
    loaded = TerminalRegistry(path).get("terminal-fake")

    assert loaded is not None
    assert loaded.to_dict() == profile.to_dict()
    assert "password" not in path.read_text(encoding="utf-8").casefold()
