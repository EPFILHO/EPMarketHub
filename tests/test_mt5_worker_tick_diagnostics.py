from __future__ import annotations

import queue
from datetime import UTC, datetime, timedelta

from core.models import TerminalConnectionStatus, TerminalProfile
from core.mt5_connector import MT5TicksError
from core.mt5_worker import mt5_worker_main
from core.terminal_states import WorkerConnectionState
from core.worker_protocol import worker_command
from market_analytics.tick_diagnostics import TickWindow, TickWindowRequest

START_UTC = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)


class StopEvent:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True


class EventQueue:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_nowait(self, payload) -> None:
        self.items.append(payload)

    def put(self, payload, timeout: float) -> None:  # pragma: no cover - eventos críticos
        self.items.append(payload)


class DelayedCommandQueue:
    """Entrega um único comando somente na N-ésima chamada de get_nowait()."""

    def __init__(self, command: dict, deliver_on_call: int) -> None:
        self._command = command
        self._deliver_on_call = deliver_on_call
        self._calls = 0

    def get_nowait(self):
        self._calls += 1
        if self._calls == self._deliver_on_call:
            return self._command
        raise queue.Empty


def _connected_status(profile_id: str) -> TerminalConnectionStatus:
    return TerminalConnectionStatus(
        terminal_id=profile_id,
        ok=True,
        state=WorkerConnectionState.CONNECTED.value,
        message="Connected.",
    )


class FakeConnector:
    def __init__(self, profile: TerminalProfile) -> None:
        self.profile = profile
        self.initialized = False
        self.status_calls = 0
        self.copy_calls: list[tuple] = []
        self.symbol_states = {
            "WIN$": {"tradable": True, "has_quote": True, "visible": True, "selected": True}
        }
        self.stop_event: StopEvent | None = None
        self.stop_after_status_calls = 6
        self.tick_error: MT5TicksError | None = None
        self.raw_exception: Exception | None = None
        self.malformed_row: dict | None = None

    def initialize(self) -> TerminalConnectionStatus:
        self.initialized = True
        return _connected_status(self.profile.id)

    def connection_status(self) -> TerminalConnectionStatus:
        self.status_calls += 1
        if self.stop_event is not None and self.status_calls >= self.stop_after_status_calls:
            self.stop_event.set()
        return _connected_status(self.profile.id)

    def shutdown(self) -> None:
        self.initialized = False

    def list_symbol_states(self) -> dict:
        return self.symbol_states

    def copy_ticks_chunk(self, symbol, tick_type, start_utc, end_utc):
        self.copy_calls.append((symbol, tick_type, start_utc, end_utc))
        if self.raw_exception is not None:
            raise self.raw_exception
        if self.tick_error is not None:
            raise self.tick_error
        if self.malformed_row is not None:
            return [self.malformed_row]
        time_msc = int(start_utc.timestamp() * 1000) + 500
        return [
            {
                "time": time_msc // 1000,
                "time_msc": time_msc,
                "bid": 1.0,
                "ask": 1.1,
                "last": 0.0,
                "volume": 0.0,
                "volume_real": 0.0,
                "flags": 6,
            }
        ]


def _make_request(request_id: str = "req-1", **overrides) -> TickWindowRequest:
    window = TickWindow(start_utc=START_UTC, end_utc=START_UTC + timedelta(minutes=2))
    defaults = dict(
        request_id=request_id,
        logical_id="win",
        aliases=("WIN$",),
        tick_type="all",
        windows=(window,),
        chunk_seconds=60,
    )
    defaults.update(overrides)
    return TickWindowRequest(**defaults)


def _run_worker(
    monkeypatch,
    connector: FakeConnector,
    commands: list[dict],
    *,
    deliver_on_call: int = 2,
    stop_after_status_calls: int = 6,
) -> EventQueue:
    stop_event = StopEvent()
    connector.stop_event = stop_event
    connector.stop_after_status_calls = stop_after_status_calls
    event_queue = EventQueue()
    command_queue = (
        DelayedCommandQueue(commands[0], deliver_on_call)
        if commands
        else DelayedCommandQueue({}, 10**9)
    )

    monkeypatch.setattr("core.mt5_worker.MT5Connector", lambda profile: connector)
    monkeypatch.setattr("core.mt5_worker._terminal_process_running", lambda _path: True)
    monkeypatch.setattr("core.mt5_worker.time.sleep", lambda _s: None)

    mt5_worker_main(
        TerminalProfile(
            id="terminal-fake", label="Fake", terminal_exe="sandbox/terminal64.exe"
        ).to_dict(),
        [],
        command_queue,
        event_queue,
        stop_event,
    )
    return event_queue


def _event_types(event_queue: EventQueue) -> list[str]:
    return [item["event"] for item in event_queue.items]


def test_tick_diagnostic_completes_with_one_chunk_per_loop_iteration(monkeypatch) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    command = worker_command("diagnose_ticks", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, [command])

    types = _event_types(event_queue)
    assert "tick_diagnostic_accepted" in types
    assert types.count("tick_diagnostic_window_result") == 1
    assert "tick_diagnostic_completed" in types
    assert "error" not in types
    # Janela de 2 minutos com chunks de 60s == exatamente 2 chunks; nunca mais
    # de um chunk resolvido por chamada de copy_ticks_chunk por iteração.
    assert len(connector.copy_calls) == 2

    window_result = next(
        item for item in event_queue.items if item["event"] == "tick_diagnostic_window_result"
    )
    assert window_result["data"]["summary"]["total_count"] == 2
    assert isinstance(window_result["data"]["pid"], int)  # campo único "pid", sem "worker_pid"
    assert "worker_pid" not in window_result["data"]
    assert "worker_pid" not in window_result["data"]["summary"]


def test_tick_diagnostic_symbol_not_found_fails_without_crashing_worker(monkeypatch) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    connector.symbol_states = {}  # nenhum símbolo disponível
    request = _make_request()
    command = worker_command("diagnose_ticks", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, [command])

    types = _event_types(event_queue)
    assert "tick_diagnostic_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "tick_diagnostic_failed")
    assert failed["data"]["reason"] == "symbol_not_found"
    # COR-DEV-002 item 4: a mensagem não afirma que o alias precisava ser
    # ativo/negociável; ausência histórica é sobre estar listado ou não.
    assert "negoci" not in failed["data"]["message"].lower()
    assert "ativo/tradável" not in failed["data"]["message"]
    assert "error" not in types
    assert connector.copy_calls == []


def test_tick_diagnostic_accepts_non_tradable_win_dollar_and_collects_from_it(monkeypatch) -> None:
    """COR-DEV-002: WIN$ listado e exato, porém não negociável na Clear, deve
    ser aceito pela resolução histórica do diagnóstico e usado na coleta —
    diferente do resolvedor operacional, que o recusaria."""

    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    connector.symbol_states = {
        "WIN$": {"tradable": False, "has_quote": True, "visible": True, "selected": True}
    }
    request = _make_request()
    command = worker_command("diagnose_ticks", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, [command])

    types = _event_types(event_queue)
    assert "tick_diagnostic_failed" not in types
    accepted = next(
        item for item in event_queue.items if item["event"] == "tick_diagnostic_accepted"
    )
    assert accepted["data"]["resolved_symbol"] == "WIN$"
    assert connector.copy_calls
    assert all(call[0] == "WIN$" for call in connector.copy_calls)


def test_tick_diagnostic_mt5_error_fails_without_contaminating_connection_state(
    monkeypatch,
) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    connector.tick_error = MT5TicksError("mt5_error", "Invalid params", code=-2)
    request = _make_request()
    command = worker_command("diagnose_ticks", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, [command])

    types = _event_types(event_queue)
    failed = next(item for item in event_queue.items if item["event"] == "tick_diagnostic_failed")
    assert failed["data"]["reason"] == "mt5_error"
    # code e message preservados separados (COR-DEV-001 item 5), não
    # reduzidos a uma string concatenada.
    assert failed["data"]["code"] == -2
    assert failed["data"]["message"] == "Invalid params"
    # A falha do diagnóstico nunca vira um "error" genérico do worker, e o
    # loop termina graciosamente (não crasha) depois dela.
    assert "error" not in types
    assert types[-1] == "stopped"


def test_tick_diagnostic_generic_exception_from_copy_ticks_chunk_is_isolated(monkeypatch) -> None:
    """COR-DEV-001 item 6 (regressão): uma exceção comum não normalizada pelo
    conector (aqui simulada diretamente no fake) não pode escapar como crash
    do worker nem virar `error` genérico."""

    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    connector.raw_exception = RuntimeError("falha inesperada da API")
    request = _make_request()
    command = worker_command("diagnose_ticks", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, [command])

    types = _event_types(event_queue)
    failed = next(item for item in event_queue.items if item["event"] == "tick_diagnostic_failed")
    assert failed["data"]["reason"] == "mt5_error"
    assert "error" not in types
    assert types[-1] == "stopped"


def test_tick_diagnostic_malformed_response_fails_explicitly(monkeypatch) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    connector.malformed_row = {"bid": 1.0}  # sem time/time_msc/ask/last/volume/flags
    request = _make_request()
    command = worker_command("diagnose_ticks", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, [command])

    types = _event_types(event_queue)
    failed = next(item for item in event_queue.items if item["event"] == "tick_diagnostic_failed")
    assert failed["data"]["reason"] == "malformed_response"
    assert "error" not in types


def test_tick_diagnostic_nan_price_fails_as_malformed_without_crashing_worker(
    monkeypatch,
) -> None:
    """COR-DEV-001 item 4 (regressão): NaN/infinito em um preço/volume vira
    `malformed_response`, nunca é aceito silenciosamente nem derruba o worker."""

    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    time_msc = int(START_UTC.timestamp() * 1000) + 500
    connector.malformed_row = {
        "time": time_msc // 1000,
        "time_msc": time_msc,
        "bid": float("nan"),
        "ask": 1.1,
        "last": 0.0,
        "volume": 0.0,
        "volume_real": 0.0,
        "flags": 6,
    }
    request = _make_request()
    command = worker_command("diagnose_ticks", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, [command])

    types = _event_types(event_queue)
    failed = next(item for item in event_queue.items if item["event"] == "tick_diagnostic_failed")
    assert failed["data"]["reason"] == "malformed_response"
    assert "error" not in types
    assert types[-1] == "stopped"


def test_repeating_same_request_id_with_same_parameters_is_idempotent(monkeypatch) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    command = worker_command("diagnose_ticks", request=request.to_dict())

    stop_event = StopEvent()
    connector.stop_event = stop_event
    connector.stop_after_status_calls = 8
    event_queue = EventQueue()

    calls = {"n": 0}

    class TwoDeliveryQueue:
        def get_nowait(self):
            calls["n"] += 1
            if calls["n"] in (2, 3):
                return command
            raise queue.Empty

    monkeypatch.setattr("core.mt5_worker.MT5Connector", lambda profile: connector)
    monkeypatch.setattr("core.mt5_worker._terminal_process_running", lambda _path: True)
    monkeypatch.setattr("core.mt5_worker.time.sleep", lambda _s: None)

    mt5_worker_main(
        TerminalProfile(
            id="terminal-fake", label="Fake", terminal_exe="sandbox/terminal64.exe"
        ).to_dict(),
        [],
        TwoDeliveryQueue(),
        event_queue,
        stop_event,
    )

    types = _event_types(event_queue)
    assert types.count("tick_diagnostic_accepted") == 1
    assert "tick_diagnostic_failed" not in types


def test_repeating_same_request_id_with_different_parameters_conflicts(monkeypatch) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    first = _make_request()
    second = _make_request(chunk_seconds=90)
    first_command = worker_command("diagnose_ticks", request=first.to_dict())
    second_command = worker_command("diagnose_ticks", request=second.to_dict())

    stop_event = StopEvent()
    connector.stop_event = stop_event
    connector.stop_after_status_calls = 8
    event_queue = EventQueue()
    calls = {"n": 0}

    class TwoCommandsQueue:
        def get_nowait(self):
            calls["n"] += 1
            if calls["n"] == 2:
                return first_command
            if calls["n"] == 3:
                return second_command
            raise queue.Empty

    monkeypatch.setattr("core.mt5_worker.MT5Connector", lambda profile: connector)
    monkeypatch.setattr("core.mt5_worker._terminal_process_running", lambda _path: True)
    monkeypatch.setattr("core.mt5_worker.time.sleep", lambda _s: None)

    mt5_worker_main(
        TerminalProfile(
            id="terminal-fake", label="Fake", terminal_exe="sandbox/terminal64.exe"
        ).to_dict(),
        [],
        TwoCommandsQueue(),
        event_queue,
        stop_event,
    )

    failed = [item for item in event_queue.items if item["event"] == "tick_diagnostic_failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["reason"] == "request_id_conflict"


def test_a_second_different_request_id_while_busy_is_rejected(monkeypatch) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    first = _make_request(request_id="req-a")
    second = _make_request(request_id="req-b")
    first_command = worker_command("diagnose_ticks", request=first.to_dict())
    second_command = worker_command("diagnose_ticks", request=second.to_dict())

    stop_event = StopEvent()
    connector.stop_event = stop_event
    connector.stop_after_status_calls = 8
    event_queue = EventQueue()
    calls = {"n": 0}

    class TwoCommandsQueue:
        def get_nowait(self):
            calls["n"] += 1
            if calls["n"] == 2:
                return first_command
            if calls["n"] == 3:
                return second_command
            raise queue.Empty

    monkeypatch.setattr("core.mt5_worker.MT5Connector", lambda profile: connector)
    monkeypatch.setattr("core.mt5_worker._terminal_process_running", lambda _path: True)
    monkeypatch.setattr("core.mt5_worker.time.sleep", lambda _s: None)

    mt5_worker_main(
        TerminalProfile(
            id="terminal-fake", label="Fake", terminal_exe="sandbox/terminal64.exe"
        ).to_dict(),
        [],
        TwoCommandsQueue(),
        event_queue,
        stop_event,
    )

    failed = [item for item in event_queue.items if item["event"] == "tick_diagnostic_failed"]
    assert any(item["data"]["reason"] == "diagnostic_busy" for item in failed)
