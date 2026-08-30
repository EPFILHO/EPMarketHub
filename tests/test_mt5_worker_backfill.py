from __future__ import annotations

import queue
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow.parquet as pq

from core.models import TerminalConnectionStatus, TerminalProfile
from core.mt5_worker import mt5_worker_main
from core.terminal_states import WorkerConnectionState
from core.worker_protocol import worker_command
from market_analytics.backfill_writer import SessionTickWriter, build_schema
from market_analytics.tick_backfill import (
    BackfillSessionRequest,
    build_raw_metadata,
    raw_partition_dir,
)
from market_analytics.tick_diagnostics import TickWindow, TickWindowRequest

SESSION_DATE = date(2026, 8, 28)


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


class ScriptedCommandQueue:
    """Entrega comandos agendados por número da chamada de get_nowait()."""

    def __init__(self, scheduled: dict[int, dict]) -> None:
        self._scheduled = scheduled
        self._calls = 0

    def get_nowait(self):
        self._calls += 1
        command = self._scheduled.get(self._calls)
        if command is None:
            raise queue.Empty
        return command


def _connected_status(profile_id: str) -> TerminalConnectionStatus:
    return TerminalConnectionStatus(
        terminal_id=profile_id, ok=True, state=WorkerConnectionState.CONNECTED.value, message="Connected."
    )


class FakeConnector:
    def __init__(self, profile: TerminalProfile) -> None:
        self.profile = profile
        self.initialized = False
        self.status_calls = 0
        self.copy_calls: list[tuple] = []
        self.symbol_states = {
            "WIN$": {"tradable": False, "has_quote": True, "visible": True, "selected": True}
        }
        self.stop_event: StopEvent | None = None
        self.stop_after_status_calls = 400
        self.disconnect_after_copy_calls: int | None = None

    def initialize(self) -> TerminalConnectionStatus:
        self.initialized = True
        return _connected_status(self.profile.id)

    def connection_status(self) -> TerminalConnectionStatus:
        self.status_calls += 1
        if self.stop_event is not None and self.status_calls >= self.stop_after_status_calls:
            self.stop_event.set()
        if self.disconnect_after_copy_calls is not None and len(self.copy_calls) >= self.disconnect_after_copy_calls:
            return TerminalConnectionStatus(
                terminal_id=self.profile.id,
                ok=False,
                state=WorkerConnectionState.BROKER_DISCONNECTED.value,
                message="Sem conexão com a corretora.",
            )
        return _connected_status(self.profile.id)

    def shutdown(self) -> None:
        self.initialized = False

    def list_symbol_states(self) -> dict:
        return self.symbol_states

    def copy_ticks_chunk(self, symbol, tick_type, start_utc, end_utc):
        self.copy_calls.append((symbol, tick_type, start_utc, end_utc))
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


def _make_request(request_id: str = "bf-1", **overrides) -> BackfillSessionRequest:
    defaults: dict = dict(
        request_id=request_id,
        source_id="clear",
        logical_id="win",
        aliases=("WIN$",),
        tick_type="all",
        session_date=SESSION_DATE,
        chunk_seconds=900,
    )
    defaults.update(overrides)
    return BackfillSessionRequest(**defaults)


def _run_worker(
    monkeypatch,
    connector: FakeConnector,
    scheduled: dict[int, dict],
    data_root: Path,
    *,
    stop_after_status_calls: int = 400,
) -> EventQueue:
    stop_event = StopEvent()
    connector.stop_event = stop_event
    connector.stop_after_status_calls = stop_after_status_calls
    event_queue = EventQueue()
    command_queue = ScriptedCommandQueue(scheduled)

    monkeypatch.setattr("core.mt5_worker.MT5Connector", lambda profile: connector)
    monkeypatch.setattr("core.mt5_worker._terminal_process_running", lambda _path: True)
    monkeypatch.setattr("core.mt5_worker.time.sleep", lambda _s: None)

    mt5_worker_main(
        TerminalProfile(id="terminal-fake", label="Fake", terminal_exe="sandbox/terminal64.exe").to_dict(),
        [],
        command_queue,
        event_queue,
        stop_event,
        backfill_data_root=str(data_root),
    )
    return event_queue


def _event_types(event_queue: EventQueue) -> list[str]:
    return [item["event"] for item in event_queue.items]


def _final_path(data_root: Path, request: BackfillSessionRequest) -> Path:
    return (
        raw_partition_dir(data_root, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date)
        / "ticks.parquet"
    )


def test_backfill_completes_and_writes_final_file(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    command = worker_command("start_backfill", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_accepted" in types
    assert "backfill_completed" in types
    assert "backfill_failed" not in types
    accepted = next(item for item in event_queue.items if item["event"] == "backfill_accepted")
    assert accepted["data"]["attempt_id"]  # propriedade por tentativa: persistida no job/evento
    completed = next(item for item in event_queue.items if item["event"] == "backfill_completed")
    assert completed["data"]["state"] == "completed"
    assert completed["data"]["row_count"] == len(request.chunks())
    assert isinstance(completed["data"]["pid"], int)

    final_path = _final_path(tmp_path, request)
    assert final_path.exists()
    parquet_file = pq.ParquetFile(str(final_path))
    assert parquet_file.metadata.num_rows == len(request.chunks())
    # ... e também nos metadados embutidos do Parquet.
    file_attempt_id = parquet_file.schema_arrow.metadata[b"attempt_id"].decode("utf-8")
    assert file_attempt_id == accepted["data"]["attempt_id"]
    assert types.count("backfill_progress") == len(request.chunks()) - 1


def test_backfill_rejects_string_false_as_rebuild_without_touching_disk(monkeypatch, tmp_path: Path) -> None:
    """Regressão mínima da auditoria: payload `rebuild="false"` é recusado
    como `invalid_request`, nunca vira `True` nem chega perto do disco."""

    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    payload = _make_request().to_dict()
    payload["rebuild"] = "false"
    command = worker_command("start_backfill", request=payload)

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "backfill_failed")
    assert failed["data"]["reason"] == "invalid_request"
    assert "backfill_accepted" not in types
    assert not (tmp_path / "raw").exists()


def test_backfill_rejects_purely_numeric_source_id(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    payload = _make_request().to_dict()
    payload["source_id"] = "123456"
    command = worker_command("start_backfill", request=payload)

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "backfill_failed")
    assert failed["data"]["reason"] == "invalid_request"


def test_backfill_catalog_open_failure_produces_backfill_failed_not_generic_error(
    monkeypatch, tmp_path: Path
) -> None:
    """Falha do SQLite ao abrir o catálogo vira `backfill_failed`
    estruturado — nunca um `error` genérico nem um crash do worker."""

    import core.mt5_worker as worker_module

    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    command = worker_command("start_backfill", request=request.to_dict())

    def _boom(_path):
        raise worker_module.CatalogStateError("sqlite_error: falha simulada ao abrir o catálogo")

    monkeypatch.setattr(worker_module, "open_catalog", _boom)

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "backfill_failed")
    assert failed["data"]["reason"] == "catalog_error"
    assert "error" not in types
    assert types[-1] == "stopped"


def test_backfill_with_a_corrupt_preexisting_file_fails_structured_through_the_whole_worker(
    monkeypatch, tmp_path: Path
) -> None:
    """Correção de auditoria (evidência 1, segunda entrega): um arquivo
    truncado/corrompido no caminho esperado precisa produzir
    `backfill_failed`/`disk_error` mesmo passando pelo worker completo —
    nunca um `error` genérico nem um crash."""

    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    final_path = _final_path(tmp_path, request)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"xyz")
    command = worker_command("start_backfill", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "backfill_failed")
    assert failed["data"]["reason"] == "disk_error"
    assert "backfill_accepted" not in types
    assert "error" not in types
    assert types[-1] == "stopped"
    # O arquivo corrompido não foi apagado nem "consertado" silenciosamente.
    assert final_path.read_bytes() == b"xyz"


def test_backfill_with_wrong_identity_file_at_expected_path_fails_through_the_whole_worker(
    monkeypatch, tmp_path: Path
) -> None:
    """Correção de auditoria (evidência 2, segunda entrega): um Parquet
    válido de outra sessão no caminho esperado precisa ser recusado como
    `identity_mismatch`, sem virar `completed`/`empty` silenciosamente."""

    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    foreign_request = _make_request(
        request_id="bf-foreign", source_id="fot", logical_id="eurusd", session_date=date(2020, 1, 1)
    )
    final_path = _final_path(tmp_path, request)
    metadata = build_raw_metadata(
        request=foreign_request,
        resolved_symbol="EURUSD",
        collected_at=datetime(2020, 1, 1, tzinfo=UTC),
        attempt_id="attempt-foreign",
    )
    writer = SessionTickWriter(final_path, schema=build_schema(metadata=metadata))
    writer.open()
    writer.close_and_promote()
    command = worker_command("start_backfill", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "backfill_failed")
    assert failed["data"]["reason"] == "identity_mismatch"
    assert "backfill_accepted" not in types
    assert "error" not in types


def test_backfill_symbol_not_found_fails_without_crashing_worker(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    connector.symbol_states = {}
    request = _make_request()
    command = worker_command("start_backfill", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "backfill_failed")
    assert failed["data"]["reason"] == "symbol_not_found"
    assert "error" not in types
    assert connector.copy_calls == []


def test_backfill_refuses_start_with_live_stream_active(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    live_command = worker_command(
        "set_live_stream", slot_id="slot-1", symbol={"logical_id": "win", "aliases": ["WIN$"]}
    )
    backfill_command = worker_command("start_backfill", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: live_command, 3: backfill_command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "backfill_failed")
    assert failed["data"]["reason"] == "live_stream_active"
    assert connector.copy_calls == []


def test_second_different_backfill_while_busy_is_rejected(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    first = _make_request(request_id="bf-a")
    second = _make_request(request_id="bf-b", logical_id="wdo")
    first_command = worker_command("start_backfill", request=first.to_dict())
    second_command = worker_command("start_backfill", request=second.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: first_command, 3: second_command}, tmp_path)

    failed = [item for item in event_queue.items if item["event"] == "backfill_failed"]
    assert any(item["data"]["reason"] == "backfill_busy" for item in failed)


def test_repeating_same_request_id_with_different_parameters_conflicts(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    first = _make_request()
    second = _make_request(chunk_seconds=300)
    first_command = worker_command("start_backfill", request=first.to_dict())
    second_command = worker_command("start_backfill", request=second.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: first_command, 3: second_command}, tmp_path)

    failed = [item for item in event_queue.items if item["event"] == "backfill_failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["reason"] == "request_id_conflict"


def test_repeating_same_request_id_and_payload_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    command = worker_command("start_backfill", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: command, 3: command}, tmp_path)

    types = _event_types(event_queue)
    assert types.count("backfill_accepted") == 1
    assert "backfill_failed" not in types


def test_stop_backfill_between_chunks_interrupts_and_discards_partial(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    start_command = worker_command("start_backfill", request=request.to_dict())
    stop_command = worker_command("stop_backfill", request_id=request.request_id)

    event_queue = _run_worker(monkeypatch, connector, {2: start_command, 5: stop_command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_interrupted" in types
    assert "backfill_completed" not in types
    interrupted = next(item for item in event_queue.items if item["event"] == "backfill_interrupted")
    assert interrupted["data"]["state"] == "interrupted"

    final_path = _final_path(tmp_path, request)
    assert not final_path.exists()
    assert not final_path.with_name("ticks.parquet.partial").exists()
    # Menos chunks que o total foram de fato buscados antes da parada.
    assert 0 < len(connector.copy_calls) < len(request.chunks())


def test_backfill_not_started_while_terminal_disconnected(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    command = worker_command("start_backfill", request=request.to_dict())

    # Entregue no primeiro dreno de comandos, antes de qualquer tentativa de conexão.
    event_queue = _run_worker(monkeypatch, connector, {1: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_failed" in types
    failed = next(item for item in event_queue.items if item["event"] == "backfill_failed")
    assert failed["data"]["reason"] == "terminal_disconnected"


def test_connection_lost_mid_backfill_interrupts_without_generic_error(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    connector.disconnect_after_copy_calls = 3
    request = _make_request()
    command = worker_command("start_backfill", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_interrupted" in types
    assert "backfill_completed" not in types
    assert "error" not in types
    final_path = _final_path(tmp_path, request)
    assert not final_path.exists()


def _make_diagnose_command(request_id: str = "diag-1") -> dict:
    window = TickWindow(start_utc=datetime(2026, 3, 2, 13, 0, tzinfo=UTC), end_utc=datetime(2026, 3, 2, 13, 2, tzinfo=UTC))
    request = TickWindowRequest(
        request_id=request_id,
        logical_id="win",
        aliases=("WIN$",),
        tick_type="all",
        windows=(window,),
        chunk_seconds=60,
    )
    return worker_command("diagnose_ticks", request=request.to_dict())


# --- Correção de auditoria item 4: exclusão mútua bidirecional ---


def test_diagnose_ticks_is_refused_while_a_backfill_is_active(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    backfill_request = _make_request()
    start_backfill = worker_command("start_backfill", request=backfill_request.to_dict())
    diagnose = _make_diagnose_command()

    event_queue = _run_worker(monkeypatch, connector, {2: start_backfill, 3: diagnose}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_accepted" in types
    failed = [item for item in event_queue.items if item["event"] == "tick_diagnostic_failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["reason"] == "backfill_active"
    assert "tick_diagnostic_accepted" not in types


def test_start_backfill_is_refused_while_a_tick_diagnostic_is_active(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    diagnose = _make_diagnose_command()
    backfill_request = _make_request()
    start_backfill = worker_command("start_backfill", request=backfill_request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: diagnose, 3: start_backfill}, tmp_path)

    types = _event_types(event_queue)
    assert "tick_diagnostic_accepted" in types
    failed = [item for item in event_queue.items if item["event"] == "backfill_failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["reason"] == "diagnostic_active"
    assert "backfill_accepted" not in types
    assert connector.copy_calls  # o diagnóstico seguiu coletando normalmente


def test_set_live_stream_is_refused_while_a_backfill_is_active(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    backfill_request = _make_request()
    start_backfill = worker_command("start_backfill", request=backfill_request.to_dict())
    live_command = worker_command(
        "set_live_stream", slot_id="slot-1", symbol={"logical_id": "win", "aliases": ["WIN$"]}
    )

    event_queue = _run_worker(monkeypatch, connector, {2: start_backfill, 3: live_command}, tmp_path)

    types = _event_types(event_queue)
    assert "backfill_accepted" in types
    live_statuses = [item for item in event_queue.items if item["event"] == "live_status"]
    assert len(live_statuses) == 1
    assert live_statuses[0]["data"]["state"] == "rejected_backfill_active"
    assert "live_tick" not in types  # nenhum polling chegou a começar


def test_mutual_exclusion_rejections_never_touch_worker_state(monkeypatch, tmp_path: Path) -> None:
    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    backfill_request = _make_request()
    start_backfill = worker_command("start_backfill", request=backfill_request.to_dict())
    diagnose = _make_diagnose_command()

    event_queue = _run_worker(monkeypatch, connector, {2: start_backfill, 3: diagnose}, tmp_path)

    # "status"/"error" genéricos do worker não devem carregar o motivo da
    # recusa cruzada — ela vive só no evento específico da funcionalidade.
    generic_events = [item for item in event_queue.items if item["event"] in ("status", "error")]
    assert all("backfill_active" not in str(item) for item in generic_events)


def test_backfill_never_leaks_account_or_credentials_in_events(monkeypatch, tmp_path: Path) -> None:
    """Só os eventos de backfill (não o stream inteiro do worker, que já
    carrega campos genéricos de status/conexão) precisam ser livres de
    conta/credenciais — ver critério de aceitação do Portão A."""

    connector = FakeConnector(TerminalProfile(id="terminal-fake", label="Fake"))
    request = _make_request()
    command = worker_command("start_backfill", request=request.to_dict())

    event_queue = _run_worker(monkeypatch, connector, {2: command}, tmp_path)

    backfill_events = [
        item for item in event_queue.items if item["event"].startswith("backfill_")
    ]
    assert backfill_events
    blob = str(backfill_events).lower()
    for forbidden in ("account_login", "password", "senha", "account"):
        assert forbidden not in blob
