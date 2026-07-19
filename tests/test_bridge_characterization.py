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
    single_shots = []

    @staticmethod
    def singleShot(interval, callback) -> None:
        FakeQTimer.single_shots.append((interval, callback))


class FakeQCoreApplication:
    process_events_calls = 0

    @classmethod
    def processEvents(cls, *args) -> None:
        cls.process_events_calls += 1


class FakeQEventLoop:
    class ProcessEventsFlag:
        ExcludeUserInputEvents = object()


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
    qtcore.QCoreApplication = FakeQCoreApplication
    qtcore.QEvent = FakeQEvent
    qtcore.QEventLoop = FakeQEventLoop
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
        self.launch_minimized = []
        self.stopped = []
        self.remembered = []
        self.created = []
        self.renamed = []
        self.rolled_back_renames = []
        self.instance_states = {}
        self.process_counts = {}
        self.failed_stop_ids = set()
        self.failed_launch_ids = set()
        self.repaired = []
        self.forgotten = []

    @staticmethod
    def build_instance_slug(broker_name: str, account_login: str) -> str:
        return TerminalManager.build_instance_slug(broker_name, account_login)

    def remember(self, profile: TerminalProfile) -> None:
        self.remembered.append(profile.id)

    def instance_status(self, profile: TerminalProfile) -> dict:
        state = self.instance_states.get(profile.id, "ready")
        messages = {
            "ready": "Instância local pronta.",
            "directory_missing": "A pasta local desta instância não foi encontrada.",
            "executable_missing": "A pasta existe, mas o terminal64.exe não foi encontrado.",
        }
        return {
            "ready": state == "ready",
            "state": state,
            "path": profile.instance_dir,
            "terminal_exe": profile.terminal_exe,
            "message": messages.get(state, "Instância local indisponível."),
        }

    def is_running(self, terminal_id: str, profile=None) -> bool:
        return terminal_id in self.open_ids

    def process_count(self, profile: TerminalProfile) -> int:
        return self.process_counts.get(profile.id, int(profile.id in self.open_ids))

    def running_count(self, profiles) -> int:
        return sum(profile.id in self.open_ids for profile in profiles)

    def launch(self, profile: TerminalProfile, minimized: bool = True) -> None:
        self.launched.append(profile.id)
        self.launch_minimized.append(minimized)
        if profile.id in self.failed_launch_ids:
            raise OSError("falha simulada ao abrir terminal")
        self.open_ids.add(profile.id)

    def stop(self, terminal_id: str, profile=None) -> bool:
        self.stopped.append(terminal_id)
        was_open = terminal_id in self.open_ids
        if terminal_id in self.failed_stop_ids:
            return False
        self.open_ids.discard(terminal_id)
        return was_open

    def create_instance_from_base(self, slug: str) -> Path:
        self.created.append(slug)
        return Path("sandbox") / slug / "terminal64.exe"

    def repair_instance_from_base(self, profile: TerminalProfile) -> Path:
        self.repaired.append(profile.id)
        self.instance_states[profile.id] = "ready"
        return Path(profile.terminal_exe)

    def forget(self, terminal_id: str) -> None:
        self.forgotten.append(terminal_id)

    def rename_instance(self, profile: TerminalProfile, new_slug: str) -> tuple[Path, Path]:
        self.renamed.append((profile.id, new_slug))
        target = Path(profile.instance_dir).parent / new_slug
        return target, target / "terminal64.exe"

    def rollback_rename(self, current_dir: Path, original_dir: Path) -> None:
        self.rolled_back_renames.append((current_dir, original_dir))


class FakeWorkerManager:
    def __init__(self, max_workers: int = 3) -> None:
        self.max_workers = max_workers
        self.running_ids = set()
        self.failed_stop_ids = set()
        self.started = []
        self.stopped = []
        self.events = []
        self.forgotten = []
        self.stopping_ids = set()

    def active_count(self) -> int:
        return len(self.running_ids)

    def is_running(self, terminal_id: str) -> bool:
        return terminal_id in self.running_ids

    def start_worker(self, profile: TerminalProfile, symbols) -> tuple[bool, str]:
        self.started.append(profile.id)
        if profile.id in self.running_ids:
            return False, "A leitura deste terminal já está ativa."
        if self.active_count() >= self.max_workers:
            return False, f"Limite de {self.max_workers} conexões MT5 simultâneas atingido."
        self.running_ids.add(profile.id)
        return True, "Leitura persistente iniciada."

    def stop_worker(self, terminal_id: str) -> tuple[bool, str]:
        self.stopped.append(terminal_id)
        was_running = terminal_id in self.running_ids
        if terminal_id in self.failed_stop_ids:
            return False, "Não foi possível confirmar o encerramento do worker."
        self.running_ids.discard(terminal_id)
        self.stopping_ids.discard(terminal_id)
        return was_running, "Leitura encerrada."

    def mark_stopping(self, terminal_id: str) -> bool:
        if terminal_id not in self.running_ids:
            return False
        self.stopping_ids.add(terminal_id)
        return True

    def state(self, terminal_id: str) -> WorkerState:
        stopping = terminal_id in self.stopping_ids
        return WorkerState(
            terminal_id=terminal_id,
            state=("stopping" if stopping else "connected") if terminal_id in self.running_ids else "stopped",
            connected=terminal_id in self.running_ids and not stopping,
            alive=terminal_id in self.running_ids,
        )

    def states_payload(self, terminal_ids) -> dict:
        return {terminal_id: self.state(terminal_id).to_dict() for terminal_id in terminal_ids}

    def poll_events(self) -> list:
        events = list(self.events)
        self.events.clear()
        return events

    def live_streams_payload(self) -> dict:
        return {}

    def clear_live_streams_for_terminal(self, terminal_id: str) -> int:
        return 0

    def forget_terminal(self, terminal_id: str) -> None:
        self.forgotten.append(terminal_id)


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


def build_bridge(tmp_path: Path, terminal_ids: list[str], max_workers: int = 3):
    registry = TerminalRegistry(tmp_path / "terminals.json")
    for terminal_id in terminal_ids:
        registry.upsert(make_profile(tmp_path, terminal_id))
    terminal_manager = FakeTerminalManager()
    worker_manager = FakeWorkerManager(max_workers=max_workers)
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


def test_runtime_limit_payload_uses_injected_product_policy(tmp_path: Path) -> None:
    bridge, _, _ = build_bridge(tmp_path, ["one", "two", "three"], max_workers=4)

    response = json.loads(bridge.getRuntimeLimits())

    assert response["ok"] is True
    assert response["data"]["max_active_mt5"] == 4


def test_newly_launched_terminal_remains_opening_until_first_worker_status(
    tmp_path: Path,
) -> None:
    bridge, _, worker_manager = build_bridge(tmp_path, ["one"])

    response = json.loads(bridge.launchTerminal("one"))
    opening = json.loads(bridge.getTerminals())["data"][0]
    worker_manager.events.append(
        {
            "terminal_id": "one",
            "event": "status",
            "data": {"state": "connected", "alive": True, "connected": True},
        }
    )
    connected = json.loads(bridge.getTerminals())["data"][0]

    assert response["ok"] is True
    assert opening["process_state"] == "opening"
    assert connected["process_state"] == "open"


def test_duplicate_terminal_processes_are_exposed_as_kernel_error(tmp_path: Path) -> None:
    bridge, terminal_manager, _ = build_bridge(tmp_path, ["one"])
    terminal_manager.open_ids.add("one")
    terminal_manager.process_counts["one"] = 2

    terminal = json.loads(bridge.getTerminals())["data"][0]

    assert terminal["running"] is True
    assert terminal["process_count"] == 2
    assert terminal["process_state"] == "duplicate_process"

    start_response = json.loads(bridge.startWorker("one"))
    assert start_response["ok"] is False
    assert "2 processos" in start_response["message"]


def test_launch_failure_remains_visible_in_process_state(tmp_path: Path) -> None:
    bridge, terminal_manager, _ = build_bridge(tmp_path, ["one"])
    terminal_manager.failed_launch_ids.add("one")

    response = json.loads(bridge.launchTerminal("one"))
    terminal = json.loads(bridge.getTerminals())["data"][0]

    assert response["ok"] is False
    assert terminal["running"] is False
    assert terminal["process_state"] == "launch_failed"


def test_worker_restart_request_reopens_terminal_minimized_once(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    worker_manager.running_ids.add("one")
    restart_event = {
        "terminal_id": "one",
        "event": "terminal_restart_required",
        "data": {
            "state": "reopening_terminal",
            "alive": True,
            "connected": False,
        },
    }
    worker_manager.events.append(restart_event)

    bridge.poll_worker_events()

    assert terminal_manager.launched == ["one"]
    assert terminal_manager.launch_minimized == [True]
    assert terminal_manager.open_ids == {"one"}

    worker_manager.events.append(restart_event)
    bridge.poll_worker_events()

    assert terminal_manager.launched == ["one"]


def test_worker_restart_stops_when_instance_disappeared(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    terminal_manager.instance_states["one"] = "directory_missing"
    worker_manager.running_ids.add("one")
    worker_manager.events.append(
        {
            "terminal_id": "one",
            "event": "terminal_restart_required",
            "data": {
                "state": "reopening_terminal",
                "alive": True,
                "connected": False,
            },
        }
    )

    bridge.poll_worker_events()

    assert terminal_manager.launched == []
    assert worker_manager.running_ids == set()
    assert worker_manager.stopped == ["one"]


def test_terminal_getter_dispatches_restart_event_instead_of_discarding_it(
    tmp_path: Path,
) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    worker_manager.running_ids.add("one")
    worker_manager.events.append(
        {
            "terminal_id": "one",
            "event": "terminal_restart_required",
            "data": {"state": "reopening_terminal", "alive": True, "connected": False},
        }
    )

    response = json.loads(bridge.getTerminals())

    assert response["ok"] is True
    assert terminal_manager.launched == ["one"]
    assert terminal_manager.launch_minimized == [True]


def test_missing_instance_cannot_be_opened_but_confirmed_delete_removes_registration(
    tmp_path: Path,
) -> None:
    bridge, terminal_manager, _ = build_bridge(tmp_path, ["one"])
    terminal_manager.instance_states["one"] = "directory_missing"

    payload = bridge._terminals_payload()[0]
    launch_response = json.loads(bridge.launchTerminal("one"))
    delete_response = json.loads(bridge.deleteTerminal("one", "EXCLUIR"))

    assert payload["instance_status"]["state"] == "directory_missing"
    assert launch_response["ok"] is False
    assert launch_response["data"]["reason"] == "instance_unavailable"
    assert "Clique no botão Resolver" in launch_response["message"]
    assert delete_response["ok"] is True
    assert bridge.terminal_registry.get("one") is None


def test_edit_missing_instance_returns_structured_resolver_instruction(tmp_path: Path) -> None:
    bridge, terminal_manager, _ = build_bridge(tmp_path, ["one"])
    terminal_manager.instance_states["one"] = "directory_missing"
    original = bridge.terminal_registry.get("one")

    response = json.loads(
        bridge.updateTerminal(
            "one",
            original.label,
            original.broker_name,
            original.account_login,
        )
    )

    assert response["ok"] is False
    assert response["data"]["reason"] == "instance_unavailable"
    assert "Clique no botão Resolver" in response["message"]
    assert bridge.terminal_registry.get("one") is not None


def test_missing_instance_can_be_recreated_without_changing_registry_identity(
    tmp_path: Path,
) -> None:
    bridge, terminal_manager, _ = build_bridge(tmp_path, ["one"])
    terminal_manager.instance_states["one"] = "directory_missing"
    original = bridge.terminal_registry.get("one")

    response = json.loads(bridge.recreateTerminalInstance("one"))
    restored = bridge.terminal_registry.get("one")

    assert response["ok"] is True
    assert terminal_manager.repaired == ["one"]
    assert restored.broker_name == original.broker_name
    assert restored.account_login == original.account_login
    assert response["data"]["instance_status"]["state"] == "ready"


def test_missing_instance_registration_can_be_removed_without_touching_folder(
    tmp_path: Path,
) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    terminal_manager.instance_states["one"] = "executable_missing"

    response = json.loads(bridge.removeMissingTerminal("one"))

    assert response["ok"] is True
    assert bridge.terminal_registry.get("one") is None
    assert terminal_manager.forgotten == ["one"]
    assert worker_manager.forgotten == ["one"]


def test_launch_does_not_open_mt5_when_worker_capacity_is_full(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(
        tmp_path,
        ["one", "two", "three"],
        max_workers=2,
    )
    worker_manager.running_ids.update({"one", "two"})
    terminal_manager.open_ids.add("one")

    response = json.loads(bridge.launchTerminal("three"))

    assert response["ok"] is False
    assert "até 2" in response["message"]
    assert terminal_manager.launched == []


def test_disabled_terminal_cannot_be_opened_or_started(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    profile = bridge.terminal_registry.get("one")
    profile.enabled = False
    bridge.terminal_registry.upsert(profile)

    launch_response = json.loads(bridge.launchTerminal("one"))
    terminal_manager.open_ids.add("one")
    worker_response = json.loads(bridge.startWorker("one"))

    assert launch_response["ok"] is False
    assert worker_response["ok"] is False
    assert terminal_manager.launched == []
    assert worker_manager.started == []


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


def test_stop_terminal_distinguishes_close_failure_from_already_closed(tmp_path: Path) -> None:
    bridge, terminal_manager, _ = build_bridge(tmp_path, ["one"])
    terminal_manager.open_ids.add("one")
    terminal_manager.failed_stop_ids.add("one")

    response = json.loads(bridge.stopTerminal("one"))
    terminal = json.loads(bridge.getTerminals())["data"][0]

    assert response["ok"] is False
    assert response["data"]["mt5_running"] is True
    assert terminal["process_state"] == "close_failed"


def test_stop_terminal_publishes_closing_before_blocking_operation(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    terminal_manager.open_ids.add("one")
    worker_manager.running_ids.add("one")
    previous_calls = FakeQCoreApplication.process_events_calls

    bridge.stopTerminal("one")

    assert FakeQCoreApplication.process_events_calls == previous_calls + 1
    published = json.loads(bridge.terminalsChanged.values[-2])[0]
    assert published["process_state"] == "closing"
    worker_states = json.loads(bridge.workerStatesChanged.values[-1])
    assert worker_states["one"]["state"] == "stopping"


def test_app_shutdown_publishes_closing_and_stopping_for_all_active_terminals(
    tmp_path: Path,
) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one", "two"])
    terminal_manager.open_ids.update({"one", "two"})
    worker_manager.running_ids.update({"one", "two"})

    bridge.publish_shutdown_transitions()

    terminals = json.loads(bridge.terminalsChanged.values[-1])
    states = json.loads(bridge.workerStatesChanged.values[-1])
    assert {terminal["process_state"] for terminal in terminals} == {"closing"}
    assert {state["state"] for state in states.values()} == {"stopping"}


def test_late_worker_event_does_not_hide_terminal_close_failure(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    terminal_manager.open_ids.add("one")
    terminal_manager.failed_stop_ids.add("one")

    bridge.stopTerminal("one")
    worker_manager.events.append(
        {
            "terminal_id": "one",
            "event": "stopped",
            "data": {"state": "stopped", "alive": False, "connected": False},
        }
    )
    terminal = json.loads(bridge.getTerminals())["data"][0]

    assert terminal["running"] is True
    assert terminal["process_state"] == "close_failed"


def test_close_selected_reports_worker_that_resists_shutdown(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one", "two"])
    terminal_manager.open_ids.update({"one", "two"})
    worker_manager.running_ids.update({"one", "two"})
    worker_manager.failed_stop_ids.add("one")

    response = json.loads(bridge.closeSelectedTerminals('["one", "two"]'))

    assert response["ok"] is False
    assert "1 falha" in response["message"]
    assert worker_manager.running_ids == {"one"}
    assert terminal_manager.open_ids == {"one"}
    assert terminal_manager.stopped == ["two"]


def test_stop_worker_reports_resistant_process_as_failure(tmp_path: Path) -> None:
    bridge, _, worker_manager = build_bridge(tmp_path, ["one"])
    worker_manager.running_ids.add("one")
    worker_manager.failed_stop_ids.add("one")

    response = json.loads(bridge.stopWorker("one"))

    assert response["ok"] is False
    assert "confirmar" in response["message"]
    assert response["data"]["alive"] is True


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


def test_create_detects_orphan_folder_and_adopts_it_without_overwriting_files(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "MT5"
    instances_dir = tmp_path / "user_data" / "mt5_instances"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_bytes(b"base-terminal")
    terminal_manager = TerminalManager(instances_dir, base_dir)
    terminal_exe = terminal_manager.create_instance_from_base("BROKER-SANDBOX-FAKE-NEW")
    marker = terminal_exe.parent / "sessao-recuperada.dat"
    marker.write_bytes(b"preservar")
    registry = TerminalRegistry(tmp_path / "terminals.json")
    bridge = MarketHubBridge(
        terminal_registry=registry,
        symbol_registry=FakeSymbolRegistry(),
        terminal_manager=terminal_manager,
        worker_manager=FakeWorkerManager(),
    )

    create_response = json.loads(
        bridge.createTerminal("Recuperada", "Broker Sandbox", "FAKE-NEW")
    )
    adopt_response = json.loads(
        bridge.adoptTerminalInstance("Recuperada", "Broker Sandbox", "FAKE-NEW")
    )

    assert create_response["ok"] is False
    assert create_response["data"]["reason"] == "orphan_instance"
    assert adopt_response["ok"] is True
    assert marker.read_bytes() == b"preservar"
    assert terminal_exe.read_bytes() == b"base-terminal"
    assert registry.find_by_identity("Broker Sandbox", "FAKE-NEW") is not None


def test_adopting_incomplete_orphan_repairs_executable_and_preserves_contents(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "MT5"
    instances_dir = tmp_path / "user_data" / "mt5_instances"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_bytes(b"base-terminal")
    instance_dir = instances_dir / "BROKER-SANDBOX-FAKE-NEW"
    instance_dir.mkdir(parents=True)
    marker = instance_dir / "sessao-recuperada.dat"
    marker.write_bytes(b"preservar")
    registry = TerminalRegistry(tmp_path / "terminals.json")
    terminal_manager = TerminalManager(instances_dir, base_dir)
    bridge = MarketHubBridge(
        terminal_registry=registry,
        symbol_registry=FakeSymbolRegistry(),
        terminal_manager=terminal_manager,
        worker_manager=FakeWorkerManager(),
    )

    response = json.loads(
        bridge.adoptTerminalInstance("Recuperada", "Broker Sandbox", "FAKE-NEW")
    )

    assert response["ok"] is True
    assert marker.read_bytes() == b"preservar"
    assert (instance_dir / "terminal64.exe").read_bytes() == b"base-terminal"


def test_create_handles_folder_restored_during_creation_as_orphan(
    tmp_path: Path, monkeypatch
) -> None:
    base_dir = tmp_path / "MT5"
    instances_dir = tmp_path / "user_data" / "mt5_instances"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_bytes(b"base-terminal")
    terminal_manager = TerminalManager(instances_dir, base_dir)
    registry = TerminalRegistry(tmp_path / "terminals.json")
    bridge = MarketHubBridge(
        terminal_registry=registry,
        symbol_registry=FakeSymbolRegistry(),
        terminal_manager=terminal_manager,
        worker_manager=FakeWorkerManager(),
    )
    instance_dir = instances_dir / "BROKER-SANDBOX-FAKE-NEW"

    def restore_folder_then_fail(_instance_slug: str) -> Path:
        instance_dir.mkdir()
        (instance_dir / "terminal64.exe").write_bytes(b"restored-terminal")
        raise FileExistsError("pasta restaurada durante a criação")

    monkeypatch.setattr(
        terminal_manager,
        "create_instance_from_base",
        restore_folder_then_fail,
    )

    response = json.loads(bridge.createTerminal("Recuperada", "Broker Sandbox", "FAKE-NEW"))

    assert response["ok"] is False
    assert response["data"]["reason"] == "orphan_instance"
    assert (instance_dir / "terminal64.exe").read_bytes() == b"restored-terminal"
    assert registry.list() == []


def test_adoption_requires_orphan_mt5_to_be_closed(tmp_path: Path, monkeypatch) -> None:
    base_dir = tmp_path / "MT5"
    instances_dir = tmp_path / "user_data" / "mt5_instances"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_bytes(b"base-terminal")
    terminal_manager = TerminalManager(instances_dir, base_dir)
    terminal_manager.create_instance_from_base("BROKER-SANDBOX-FAKE-NEW")
    registry = TerminalRegistry(tmp_path / "terminals.json")
    bridge = MarketHubBridge(
        terminal_registry=registry,
        symbol_registry=FakeSymbolRegistry(),
        terminal_manager=terminal_manager,
        worker_manager=FakeWorkerManager(),
    )
    monkeypatch.setattr(terminal_manager, "is_executable_running", lambda _path: True)

    response = json.loads(
        bridge.adoptTerminalInstance("Recuperada", "Broker Sandbox", "FAKE-NEW")
    )

    assert response["ok"] is False
    assert response["message"] == "Feche o MT5 desta pasta antes de recuperar o cadastro."
    assert registry.list() == []


def test_confirmed_delete_removes_folder_with_missing_executable(tmp_path: Path) -> None:
    base_dir = tmp_path / "MT5"
    instances_dir = tmp_path / "user_data" / "mt5_instances"
    base_dir.mkdir()
    (base_dir / "terminal64.exe").write_bytes(b"base-terminal")
    instance_dir = instances_dir / "BROKER-SANDBOX-FAKE-ONE"
    instance_dir.mkdir(parents=True)
    (instance_dir / "remaining-session.dat").write_bytes(b"runtime")
    profile = TerminalProfile(
        id="one",
        label="Terminal one",
        broker_name="Broker Sandbox",
        account_login="FAKE-ONE",
        instance_slug=instance_dir.name,
        instance_dir=str(instance_dir),
        terminal_exe=str(instance_dir / "terminal64.exe"),
    )
    registry = TerminalRegistry(tmp_path / "terminals.json")
    registry.upsert(profile)
    bridge = MarketHubBridge(
        terminal_registry=registry,
        symbol_registry=FakeSymbolRegistry(),
        terminal_manager=TerminalManager(instances_dir, base_dir),
        worker_manager=FakeWorkerManager(),
    )

    response = json.loads(bridge.deleteTerminal("one", "EXCLUIR"))

    assert response["ok"] is True
    assert registry.get("one") is None
    assert not instance_dir.exists()


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


def test_delete_terminal_rejects_active_worker_with_mt5_state_stale(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    worker_manager.running_ids.add("one")

    response = json.loads(bridge.deleteTerminal("one", "EXCLUIR"))

    assert response["ok"] is False
    assert response["message"] == "Feche o MT5 e pare a leitura antes de excluir a instância local."
    assert bridge.terminal_registry.get("one") is not None
    assert terminal_manager.stopped == []


def test_stop_terminal_reports_worker_that_remains_alive(tmp_path: Path) -> None:
    bridge, terminal_manager, worker_manager = build_bridge(tmp_path, ["one"])
    terminal_manager.open_ids.add("one")
    worker_manager.running_ids.add("one")
    worker_manager.failed_stop_ids.add("one")

    response = json.loads(bridge.stopTerminal("one"))

    assert response["ok"] is False
    assert response["data"]["worker_running"] is True
    assert terminal_manager.open_ids == {"one"}
    assert terminal_manager.stopped == []
    assert worker_manager.running_ids == {"one"}


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


class FailingShutdownWorkerManager:
    def __init__(self) -> None:
        self.clear_calls = 0
        self.stop_calls = 0

    def clear_all_live_streams(self) -> None:
        self.clear_calls += 1
        raise RuntimeError("falha simulada ao limpar fluxos")

    def stop_all(self) -> None:
        self.stop_calls += 1
        raise RuntimeError("falha simulada ao parar workers")


class CloseRequestEvent:
    def __init__(self) -> None:
        self.accepted = False
        self.ignored = False

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True


class CloseTransitionBridge:
    def __init__(self) -> None:
        self.publish_calls = 0

    def publish_shutdown_transitions(self) -> None:
        self.publish_calls += 1


class CloseTransitionPage:
    def __init__(self) -> None:
        self.scripts = []

    def runJavaScript(self, script: str) -> None:
        self.scripts.append(script)


class CloseTransitionWebView:
    def __init__(self) -> None:
        self._page = CloseTransitionPage()
        self.update_calls = 0
        self.repaint_calls = 0

    def page(self) -> CloseTransitionPage:
        return self._page

    def update(self) -> None:
        self.update_calls += 1

    def repaint(self) -> None:
        self.repaint_calls += 1


def test_close_event_keeps_window_visible_until_badges_are_painted() -> None:
    window = MainWindow.__new__(MainWindow)
    window._shutdown_done = False
    window._close_requested = False
    window.worker_poll_timer = CountingTimer()
    window.bridge = CloseTransitionBridge()
    window.web_view = CloseTransitionWebView()
    event = CloseRequestEvent()
    FakeQTimer.single_shots.clear()

    window.closeEvent(event)

    assert event.ignored is True
    assert event.accepted is False
    assert window.bridge.publish_calls == 1
    assert "showShutdownTransitions()" in window.web_view.page().scripts[-1]
    assert window.web_view.update_calls == 1
    assert window.web_view.repaint_calls == 1
    assert FakeQTimer.single_shots[-1][0] == 150


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


def test_main_window_shutdown_continues_after_worker_failures() -> None:
    window = MainWindow.__new__(MainWindow)
    window._shutdown_done = False
    window.worker_poll_timer = CountingTimer()
    window.worker_manager = FailingShutdownWorkerManager()
    window.terminal_manager = CountingTerminalManager()
    window.terminal_registry = StaticRegistry()

    window.shutdown()

    assert window.worker_poll_timer.stop_calls == 1
    assert window.worker_manager.clear_calls == 1
    assert window.worker_manager.stop_calls == 1
    assert window.terminal_manager.stop_calls == 1
