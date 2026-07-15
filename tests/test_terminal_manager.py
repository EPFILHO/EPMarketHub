from pathlib import Path

from core.terminal_manager import TerminalManager


def build_manager(tmp_path: Path) -> TerminalManager:
    base = tmp_path / "MT5"
    instances = tmp_path / "user_data" / "mt5_instances"
    base.mkdir(parents=True)
    (base / "terminal64.exe").write_bytes(b"fake-terminal-for-tests")
    return TerminalManager(instances, base)


def test_instance_slug_is_uppercase_and_safe() -> None:
    slug = TerminalManager.build_instance_slug("Corretora Demo", "CONTA / TESTE 001")
    assert slug == "CORRETORA-DEMO-CONTA-TESTE-001"


def test_instance_slug_collapses_spaces_and_unsafe_characters() -> None:
    slug = TerminalManager.build_instance_slug("  Broker   Sandbox  ", " FAKE#002 ")
    assert slug == "BROKER-SANDBOX-FAKE-002"


def test_create_instance_copies_only_terminal64(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)

    terminal_exe = manager.create_instance_from_base("RICO-123")

    assert terminal_exe.is_file()
    assert terminal_exe.parent.name == "RICO-123"
    assert [path.name for path in terminal_exe.parent.iterdir()] == ["terminal64.exe"]
