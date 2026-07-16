import json
from pathlib import Path

from core.models import TerminalProfile
from core.terminal_manager import TerminalManager
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


def test_registry_persists_paths_relative_to_current_installation(tmp_path: Path) -> None:
    root = tmp_path / "EPMarketHub"
    instances = root / "user_data" / "mt5_instances"
    path = root / "user_data" / "terminals.json"
    slug = "BROKER-SANDBOX-FAKE-RELATIVE"
    registry = TerminalRegistry(path, root_dir=root, instances_dir=instances)
    profile = TerminalProfile(
        id="terminal-relative",
        label="Conta relativa",
        broker_name="Broker Sandbox",
        account_login="FAKE-RELATIVE",
        instance_slug=slug,
        instance_dir=str(instances / slug),
        terminal_exe=str(instances / slug / "terminal64.exe"),
    )

    registry.upsert(profile)

    persisted = json.loads(path.read_text(encoding="utf-8"))[0]
    loaded = registry.get("terminal-relative")
    assert persisted["instance_dir"] == f"user_data/mt5_instances/{slug}"
    assert persisted["terminal_exe"] == f"user_data/mt5_instances/{slug}/terminal64.exe"
    assert loaded.instance_dir == str((instances / slug).resolve())
    assert loaded.terminal_exe == str((instances / slug / "terminal64.exe").resolve())


def test_migrates_legacy_absolute_path_after_installation_moves(tmp_path: Path) -> None:
    root = tmp_path / "new-installation"
    instances = root / "user_data" / "mt5_instances"
    path = root / "user_data" / "terminals.json"
    slug = "BROKER-SANDBOX-FAKE-MOVED"
    current_instance = instances / slug
    current_instance.mkdir(parents=True)
    (current_instance / "terminal64.exe").write_bytes(b"fake-terminal-for-tests")
    path.write_text(
        json.dumps(
            [
                TerminalProfile(
                    id="terminal-moved",
                    label="Conta movida",
                    broker_name="Broker Sandbox",
                    account_login="FAKE-MOVED",
                    instance_slug=slug,
                    instance_dir=f"D:/old-installation/user_data/mt5_instances/{slug}",
                    terminal_exe=(
                        f"D:/old-installation/user_data/mt5_instances/{slug}/terminal64.exe"
                    ),
                ).to_dict()
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    registry = TerminalRegistry(path, root_dir=root, instances_dir=instances)

    migrated = registry.migrate_paths()
    profile = registry.get("terminal-moved")
    manager = TerminalManager(instances, root / "MT5")
    renamed_dir, terminal_exe = manager.rename_instance(profile, "BROKER-UPDATED-FAKE-MOVED")

    persisted = json.loads(path.read_text(encoding="utf-8"))[0]
    assert migrated == 1
    assert persisted["instance_dir"] == f"user_data/mt5_instances/{slug}"
    assert profile.instance_dir == str(current_instance.resolve())
    assert renamed_dir == (instances / "BROKER-UPDATED-FAKE-MOVED").resolve()
    assert terminal_exe.is_file()
