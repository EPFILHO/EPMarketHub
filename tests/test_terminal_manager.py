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


def test_instance_status_distinguishes_missing_directory_and_executable(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    instance_dir = manager.instances_dir / "BROKER-FAKE"
    profile = TerminalProfile(
        id="fake",
        label="Fake",
        instance_dir=str(instance_dir),
        terminal_exe=str(instance_dir / "terminal64.exe"),
    )

    assert manager.instance_status(profile)["state"] == "directory_missing"

    instance_dir.mkdir()

    assert manager.instance_status(profile)["state"] == "executable_missing"

    (instance_dir / "terminal64.exe").write_bytes(b"fake")

    assert manager.instance_status(profile)["state"] == "ready"


def test_instances_root_cannot_be_treated_as_a_terminal_instance(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    profile = TerminalProfile(
        id="invalid",
        label="Invalid",
        instance_dir=str(manager.instances_dir),
        terminal_exe=str(manager.instances_dir / "terminal64.exe"),
    )

    assert manager.instance_status(profile)["state"] == "invalid_path"


def test_existing_non_directory_instance_path_is_invalid(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    instance_path = manager.instances_dir / "BROKER-FAKE"
    instance_path.write_bytes(b"not-a-directory")
    profile = TerminalProfile(
        id="invalid",
        label="Invalid",
        instance_dir=str(instance_path),
        terminal_exe=str(instance_path / "terminal64.exe"),
    )

    assert manager.instance_status(profile)["state"] == "invalid_path"


def test_repair_instance_recreates_missing_directory_from_base(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    instance_dir = manager.instances_dir / "BROKER-FAKE"
    profile = TerminalProfile(
        id="fake",
        label="Fake",
        instance_dir=str(instance_dir),
        terminal_exe=str(instance_dir / "terminal64.exe"),
    )

    terminal_exe = manager.repair_instance_from_base(profile)

    assert terminal_exe.read_bytes() == b"fake-terminal-for-tests"
    assert manager.instance_status(profile)["state"] == "ready"


def test_repair_instance_preserves_existing_directory_contents(tmp_path: Path) -> None:
    manager = build_manager(tmp_path)
    instance_dir = manager.instances_dir / "BROKER-FAKE"
    instance_dir.mkdir()
    marker = instance_dir / "config-preservada.dat"
    marker.write_bytes(b"preservar")
    profile = TerminalProfile(
        id="fake",
        label="Fake",
        instance_dir=str(instance_dir),
        terminal_exe=str(instance_dir / "terminal64.exe"),
    )

    manager.repair_instance_from_base(profile)

    assert marker.read_bytes() == b"preservar"
    assert (instance_dir / "terminal64.exe").is_file()


def test_launch_requests_minimized_window_on_windows(tmp_path: Path, monkeypatch) -> None:
    manager = build_manager(tmp_path)
    terminal_exe = manager.create_instance_from_base("BROKER-FAKE")
    profile = TerminalProfile(
        id="fake",
        label="Fake",
        instance_dir=str(terminal_exe.parent),
        terminal_exe=str(terminal_exe),
    )
    captured = {}

    class StartupInfo:
        dwFlags = 0
        wShowWindow = 0

    class Process:
        def poll(self):
            return None

    def popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr("core.terminal_manager.sys.platform", "win32")
    monkeypatch.setattr("core.terminal_manager.subprocess.STARTUPINFO", StartupInfo, raising=False)
    monkeypatch.setattr("core.terminal_manager.subprocess.STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr("core.terminal_manager.subprocess.Popen", popen)
    monkeypatch.setattr(manager, "_find_processes", lambda profile: [])

    manager.launch(profile, minimized=True)

    assert captured["args"] == [str(terminal_exe), "/portable"]
    assert captured["startupinfo"].dwFlags == 1
    assert captured["startupinfo"].wShowWindow == 6


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


def test_stop_all_counts_only_terminals_confirmed_closed(tmp_path: Path, monkeypatch) -> None:
    manager = build_manager(tmp_path)
    profiles = [
        TerminalProfile(id="closed", label="Closed"),
        TerminalProfile(id="resistant", label="Resistant"),
    ]
    monkeypatch.setattr(manager, "stop", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        manager,
        "is_running",
        lambda terminal_id, profile=None: terminal_id == "resistant",
    )

    stopped = manager.stop_all(profiles, timeout=0)

    assert stopped == 1


def test_process_count_exposes_duplicate_executables(tmp_path: Path, monkeypatch) -> None:
    manager = build_manager(tmp_path)
    profile = TerminalProfile(id="duplicate", label="Duplicate")
    monkeypatch.setattr(manager, "_find_processes", lambda _profile: [object(), object()])

    assert manager.process_count(profile) == 2


def test_process_count_includes_just_launched_tracked_process(tmp_path: Path, monkeypatch) -> None:
    manager = build_manager(tmp_path)
    profile = TerminalProfile(id="opening", label="Opening")

    class TrackedProcess:
        pid = 777

        @staticmethod
        def poll():
            return None

    manager._processes[profile.id] = TrackedProcess()
    monkeypatch.setattr(manager, "_find_processes", lambda _profile: [])

    assert manager.process_count(profile) == 1
