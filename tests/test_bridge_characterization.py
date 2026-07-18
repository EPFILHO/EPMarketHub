import json
import sys
from pathlib import Path
from types import ModuleType

from core.models import TerminalProfile
from core.terminal_manager import TerminalManager
from core.terminal_registry import TerminalRegistry
from core.worker_protocol import WorkerState


class BoundSignal:
    def __init__(self) -> None:
        self.values = []

    def emit(self, value) -> None:
        self.values.append(value)


class SignalDescriptor:
    def __init__(self, *args) -> None:
        self.name = ""

    def __set_name__(self, owner, name: str) -> None:
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        key = f"_fake_signal_{self.name}"
        return instance.__dict__.setdefault(key, BoundSignal())


def slot(*args, **kwargs):
    def decorate(function):
        return function

    return decorate


class FakeQObject:
    def __init__(self, *args, **kwargs) -> None:
        pass


class FakeQMainWindow(FakeQObject):
    pass


class FakeQTimer:
    @staticmethod
    def singleShot(interval, callback) -> None:
        pass


class FakeQUrl:
    @staticmethod
    def fromLocalFile(path: str) -> str:
        return path


class FakeQEvent:
    class Type:
        WindowStateChange = object()


def install_qt_stubs() -> None:
    pyside = ModuleType("PySide6")
    pyside.__path__ = []
    qtcore = ModuleType("PySide6.QtCore")
    qtcore.QEvent = FakeQEvent
    qtcore.QObject = FakeQObject
    qtcore.QTimer = FakeQTimer
    qtcore.QUrl = FakeQUrl
    qtcore.Signal = SignalDescriptor
    qtcore.Slot = slot

    qtgui = ModuleType("PySide6.QtGui")
    qtgui.QCloseEvent = type("QCloseEvent", (), {})
    qtgui.QColor = type("QColor", (), {})

    qtwebchannel = ModuleType("PySide6.QtWebChannel")
    qtwebchannel.QWebChannel = type("QWebChannel", (), {})

    qtwebengine = ModuleType("PySide6.QtWebEngineWidgets")
    qtwebengine.QWebEngineView = type("QWebEngineView", (), {})

    qtwidgets = ModuleType("PySide6.QtWidgets")
    qtwidgets.QMainWindow = FakeQMainWindow

    pyside.QtCore = qtcore
    pyside.QtGui = qtgui
    pyside.QtWebChannel = qtwebchannel
    pyside.QtWebEngineWidgets = qtwebengine
    pyside.QtWidgets = qtwidgets
    sys.modules.update(
        {
            "PySide6": pyside,
            "PySide6.QtCore": qtcore,
            "PySide6.QtGui": qtgui,
            "PySide6.QtWebChannel": qtwebchannel,
            "PySide6.QtWebEngineWidgets": qtwebengine,
            "PySide6.QtWidgets": qtwidgets,
        }
    )


install_qt_stubs()

from gui.main_window import MainWindow, MarketHubBridge  # noqa: E402


class FakeSymbolRegistry:
    def list(self, enabled_only: bool = False) -> list:
        return []


class FakeTerminalManager:
    def __init__(self) -> None:
        self.open_ids = set()
        self.launched = []
        self.stopped = []
        self.remembered = []
        self.created = []
        self.renamed = []
        self.rolled_back_renames = []

    @staticmethod
    def build_instance_slug(broker_name: str, account_login: str) -> str:
        return TerminalManager.build_instance_slug(broker_name, account_login)

    def remember(self, profile: TerminalProfile) -> None:
        self.remembered.append(profile.id)

    def is_running(self, terminal_id: str, profile=None) -> bool:
        return terminal_id in self.open_ids

    def running_count(self, profiles) -> int:
        return sum(profile.id in self.open_ids for profile in profiles)

    def launch(self, profile: TerminalProfile) -> None:
        self.launched.append(profile.id)
        self.open_ids.add(profile.id)

    def stop(self, terminal_id: str, profile=None) -> bool:
        self.stopped.append(terminal_id)
        was_open = terminal_id in self.open_ids
        self.open_ids.discard(terminal_id)
        return was_open

    def create_instance_from_base(self, slug: str) -> Path:
        self.created.append(slug)
        return Path("sandbox") / slug / "terminal64.exe"

    def rename_instance(self, profile: TerminalProfile, new_slug: str) -> tuple[Path, Path]:
        self.renamed.append((profile.id, new_slug))
        target = Path(profile.instance_dir).parent / new_slug
        return target, target / "terminal64.exe"

    def rollback_rename(self, current_dir: Path, original_dir: Path) -> None:
        self.rolled_back_renames.append((current_dir, original_dir))


class FakeWorkerManager:
    max_workers = 3

    def __init__(self) -> None:
        self.running_ids = set()
        self.started = []
        self.stopped = []

    def active_count(self) -> int:
        return len(self.running_ids)

    def is_running(self, terminal_id: str) -> bool:
        return terminal_id in self.running_ids

    def start_worker(self, profile: TerminalProfile, symbols) -> tuple[bool, str]:
        self.started.append(profile.id)
        if profile.id in self.running_ids:
            return False, "A leitura deste terminal já está ativa."
        if self.active_count() >= self.max_workers:
            return False, "Limite de 3 conexões MT5 simultâneas atingido."
        self.running_ids.add(profile.id)
        return True, "Leitura persistente iniciada."

    def stop_worker(self, terminal_id: str) -> tuple[bool, str]:
        self.stopped.append(terminal_id)
        was_running = terminal_id in self.running_ids
        self.running_ids.discard(terminal_id)
        return was_running, "Leitura encerrada."

    def state(self, terminal_id: str) -> WorkerState:
        return WorkerState(
            terminal_id=terminal_id,
            state="connected" if terminal_id in self.running_ids else "stopped",
            alive=terminal_id in self.running_ids,
        )

    def poll_events(self) -> list:
        return []

    def live_streams_payload(self) -> dict:
        return {}

    def clear_live_streams_for_terminal(self, terminal_id: str) -> int:
        return 0


def make_profile(tmp_path: Path, terminal_id: str) -> TerminalProfile:
    slug = f"BROKER-SANDBOX-FAKE-{terminal_id.upper()}"
    instance_dir = tmp_path / "instances" / slug
    return TerminalProfile(
        id=terminal_id,
        label=f"Terminal de teste {terminal_id}",
        broker_name="Broker Sandbox",
        account_login=f"FAKE-{terminal_id.upper()}",
        instance_slug=slug,
        instance_dir=str(instance_dir),
        terminal_exe=str(instance_dir / "terminal64.exe"),
    )


def build_bridge(tmp_path: Path, terminal_ids: list[str]):
    registry = TerminalRegistry(tmp_path / "terminals.json")
    for terminal_id in terminal_ids:
        registry.upsert(make_profile(tmp_path, terminal_id))
    terminal_manager = FakeTerminalManager()
    worker_manager = FakeWorkerManager()
    bridge = MarketHubBridge(
        terminal_registry=registry,
        symbol_registry=FakeSymbolRegistry(),
        terminal_manager=terminal_manager,
        worker_manager=worker_manager,
    )
    return bridge, terminal_manager, worker_manager


def test_start_selected_workers_opens_only_requested_terminals(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(
        tmp_path, ["one", "two", "three", "four"]
    )

    response = json.loads(bridge.startSelectedWorkers('["two", "four"]'))

    assert response["ok"] is True
    assert terminal_manager.launched == ["two", "four"]
    assert worker_manager.started == ["two", "four"]
    assert terminal_manager.open_ids == {"two", "four"}
    assert worker_manager.running_ids == {"two", "four"}


def test_close_selected_terminals_stops_only_requested_terminal(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(
        tmp_path, ["one", "two", "three"]
    )
    terminal_manager.open_ids.update({"one", "two", "three"})
    worker_manager.running_ids.update({"one", "two", "three"})

    response = json.loads(bridge.closeSelectedTerminals('["two"]'))

    assert response["ok"] is True
    assert terminal_manager.open_ids == {"one", "three"}
    assert worker_manager.running_ids == {"one", "three"}
    assert terminal_manager.stopped == ["two"]
    assert worker_manager.stopped == ["two"]


def test_create_terminal_rejects_duplicate_broker_and_account(tmp_path: Path) -> None:
    bridge, terminal_manager, _ = build_bridge(tmp_path, ["one"])

    response = json.loads(
        bridge.createTerminal("Duplicado", "  broker   sandbox ", " fake-one ")
    )

    assert response["ok"] is False
    assert terminal_manager.created == []


class FailingUpsertRegistry(TerminalRegistry):
    def upsert(self, profile: TerminalProfile) -> TerminalProfile:
        raise OSError("falha simulada ao salvar cadastro")


def test_create_terminal_removes_new_instance_if_registry_save_fails(tmp_path: Path) -> None:
    base_dir = tmp_path / "MT5"
    instances_dir = tmp_path / "user_data" / "mt5_instances"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_bytes(b"fake-terminal-for-tests")
    registry = FailingUpsertRegistry(tmp_path / "terminals.json")
    terminal_manager = TerminalManager(instances_dir, base_dir)
    bridge = MarketHubBridge(
        terminal_registry=registry,
        symbol_registry=FakeSymbolRegistry(),
        terminal_manager=terminal_manager,
        worker_manager=FakeWorkerManager(),
    )

    response = json.loads(bridge.createTerminal("Teste", "Broker Sandbox", "FAKE-NEW"))

    assert response["ok"] is False
    assert "falha simulada ao salvar cadastro" in response["message"]
    assert list(instances_dir.iterdir()) == []


def test_update_terminal_rejects_open_mt5(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    original = bridge.terminal_registry.get("one")
    terminal_manager.open_ids.add("one")

    response = json.loads(
        bridge.updateTerminal(
            "one",
            "Apelido alterado",
            original.broker_name,
            original.account_login,
        )
    )

    restored = bridge.terminal_registry.get("one")
    assert response["ok"] is False
    assert response["message"] == "Feche o MT5 e pare a leitura antes de editar este terminal."
    assert restored.label == original.label
    assert terminal_manager.stopped == []
    assert worker_manager.stopped == []


def test_update_terminal_rejects_active_worker_with_mt5_state_stale(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    original = bridge.terminal_registry.get("one")
    worker_manager.running_ids.add("one")

    response = json.loads(
        bridge.updateTerminal(
            "one",
            "Apelido alterado",
            original.broker_name,
            original.account_login,
        )
    )

    assert response["ok"] is False
    assert response["message"] == "Feche o MT5 e pare a leitura antes de editar este terminal."
    assert worker_manager.running_ids == {"one"}
    assert terminal_manager.stopped == []
    assert worker_manager.stopped == []


def test_update_terminal_renames_instance_when_fully_stopped(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])

    response = json.loads(
        bridge.updateTerminal(
            "one",
            "Apelido alterado",
            "Broker Atualizado",
            "FAKE-UPDATED",
        )
    )

    updated = bridge.terminal_registry.get("one")
    assert response["ok"] is True
    assert updated.label == "Apelido alterado"
    assert updated.instance_slug == "BROKER-ATUALIZADO-FAKE-UPDATED"
    assert terminal_manager.renamed == [("one", "BROKER-ATUALIZADO-FAKE-UPDATED")]
    assert terminal_manager.stopped == []
    assert worker_manager.started == []


def test_update_terminal_rolls_back_rename_if_registry_save_fails(tmp_path: Path) -> None:
    bridge, terminal_manager, _ = build_bridge(tmp_path, ["one"])
    original = bridge.terminal_registry.get("one")
    original_save = bridge.terminal_registry._save
    save_attempts = 0

    def fail_first_save(rows) -> None:
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise OSError("falha simulada ao salvar edição")
        original_save(rows)

    bridge.terminal_registry._save = fail_first_save

    response = json.loads(
        bridge.updateTerminal(
            "one",
            "Apelido alterado",
            "Broker Atualizado",
            "FAKE-UPDATED",
        )
    )

    restored = bridge.terminal_registry.get("one")
    assert response["ok"] is False
    assert "falha simulada ao salvar edição" in response["message"]
    assert restored.label == original.label
    assert restored.broker_name == original.broker_name
    assert restored.account_login == original.account_login
    assert restored.instance_slug == original.instance_slug
    assert restored.instance_dir == original.instance_dir
    assert len(terminal_manager.rolled_back_renames) == 1
    assert save_attempts == 2


class CountingTimer:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class CountingWorkerManager:
    def __init__(self) -> None:
        self.clear_calls = 0
        self.stop_calls = 0

    def clear_all_live_streams(self) -> None:
        self.clear_calls += 1

    def stop_all(self) -> None:
        self.stop_calls += 1


class CountingTerminalManager:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop_all(self, profiles) -> int:
        self.stop_calls += 1
        return len(profiles)


class StaticRegistry:
    def list(self) -> list:
        return []


def test_main_window_shutdown_is_idempotent() -> None:
    window = MainWindow.__new__(MainWindow)
    window._shutdown_done = False
    window.worker_poll_timer = CountingTimer()
    window.worker_manager = CountingWorkerManager()
    window.terminal_manager = CountingTerminalManager()
    window.terminal_registry = StaticRegistry()

    window.shutdown()
    window.shutdown()

    assert window.worker_poll_timer.stop_calls == 1
    assert window.worker_manager.clear_calls == 1
    assert window.worker_manager.stop_calls == 1
    assert window.terminal_manager.stop_calls == 1
