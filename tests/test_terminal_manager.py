import subprocess
from pathlib import Path

from core.models import TerminalProfile
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


class KillRequiredProcess:
    pid = 4242

    def __init__(self, kill_succeeds: bool) -> None:
        self.kill_succeeds = kill_succeeds
        self.kill_calls = 0

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: int) -> None:
        if self.kill_calls and self.kill_succeeds:
            return
        raise subprocess.TimeoutExpired("terminal64.exe", timeout)


def test_close_process_confirms_kill_result(monkeypatch) -> None:
    monkeypatch.setattr(TerminalManager, "_post_windows_close", lambda pid: False)
    process = KillRequiredProcess(kill_succeeds=True)

    stopped = TerminalManager._close_process(process, timeout=0)

    assert stopped is True
    assert process.kill_calls == 1


def test_close_process_does_not_report_success_if_kill_times_out(monkeypatch) -> None:
    monkeypatch.setattr(TerminalManager, "_post_windows_close", lambda pid: False)
    process = KillRequiredProcess(kill_succeeds=False)

    stopped = TerminalManager._close_process(process, timeout=0)

    assert stopped is False
    assert process.kill_calls == 1


def test_stop_all_continues_after_one_terminal_fails(tmp_path: Path, monkeypatch) -> None:
    manager = build_manager(tmp_path)
    profiles = [
        TerminalProfile(id="one", label="One"),
        TerminalProfile(id="two", label="Two"),
        TerminalProfile(id="three", label="Three"),
    ]
    stopped_ids = []

    def stop(terminal_id: str, **kwargs) -> bool:
        if terminal_id == "two":
            raise OSError("falha simulada")
        stopped_ids.append(terminal_id)
        return True

    monkeypatch.setattr(manager, "stop", stop)

    stopped = manager.stop_all(profiles, timeout=0)

    assert stopped == 2
    assert stopped_ids == ["one", "three"]
