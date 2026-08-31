from __future__ import annotations

import logging
import os
import queue
import time
import traceback
from multiprocessing.synchronize import Event as EventType
from pathlib import Path
from typing import Any

import psutil

from market_analytics.backfill_catalog import CatalogStateError, open_catalog
from market_analytics.backfill_runner import (
    BackfillSessionResult,
    BackfillSourceError,
    advance_backfill_job,
    interrupt_backfill_job,
    start_backfill_job,
)
from market_analytics.tick_backfill import BackfillSessionRequest, catalog_db_path
from market_analytics.tick_diagnostics import (
    TickRecord,
    TickWindow,
    TickWindowAccumulator,
    TickWindowRequest,
)

from .config import DEFAULT_MARKET_DATA_ROOT
from .market_snapshot import (
    build_snapshot_from_connector,
    resolve_historical_symbol_alias,
    resolve_symbol_aliases,
)
from .models import SymbolDefinition, TerminalProfile
from .mt5_connector import MT5Connector, MT5TicksError
from .terminal_states import (
    IPC_ATTACHED_STATES,
    WorkerConnectionState,
    state_after_reconnect_attempts,
)
from .worker_protocol import WorkerEvent, now_iso, valid_worker_command

logger = logging.getLogger(__name__)

LOSSY_EVENT_TYPES = frozenset({"heartbeat", "live_tick", "snapshot", "backfill_progress"})


def _emit(
    event_queue,
    terminal_id: str,
    event: str,
    data: dict[str, Any] | None = None,
) -> bool:
    payload = WorkerEvent(terminal_id=terminal_id, event=event, data=data or {}).to_dict()
    try:
        event_queue.put_nowait(payload)
        return True
    except queue.Full:
        if event in LOSSY_EVENT_TYPES:
            # Cotações, snapshots e heartbeats serão renovados; nunca bloqueiam o worker.
            return False
        try:
            # Eventos de ciclo de vida recebem uma chance limitada sem criar deadlock.
            event_queue.put(payload, timeout=0.25)
            return True
        except queue.Full:
            logger.error("Fila de eventos cheia; evento crítico %s não foi entregue.", event)
            return False
    except (EOFError, OSError, ValueError):
        logger.exception("Fila de eventos indisponível ao emitir %s.", event)
        return False


def _status_payload(status) -> dict[str, Any]:
    return status.to_dict()


def _keeps_ipc_attachment(state: str) -> bool:
    """Separa sessão de negociação indisponível de falha no canal IPC do MT5."""

    return state in IPC_ATTACHED_STATES


def _normalized_executable(value: str | Path) -> str:
    try:
        return str(Path(value).resolve()).casefold()
    except Exception:
        return str(value).casefold()


def _terminal_process_running(terminal_exe: str) -> bool:
    target = _normalized_executable(terminal_exe)
    for process in psutil.process_iter(["exe"]):
        try:
            executable = process.info.get("exe")
            if executable and _normalized_executable(executable) == target:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return False


def _emit_terminal_restart_required(
    event_queue,
    profile: TerminalProfile,
    reconnect_attempts: int,
) -> None:
    _emit(
        event_queue,
        profile.id,
        "terminal_restart_required",
        {
            "state": WorkerConnectionState.REOPENING_TERMINAL.value,
            "alive": True,
            "connected": False,
            "message": "Reabrindo MT5 de forma controlada e minimizada.",
            "pid": os.getpid(),
            "reconnect_attempts": reconnect_attempts,
        },
    )


def _emit_live_status(
    event_queue,
    profile: TerminalProfile,
    slot_id: str,
    state: str,
    message: str,
    symbol_spec: dict[str, Any],
    resolved_symbol: str | None = None,
    connected: bool = False,
) -> None:
    _emit(
        event_queue,
        profile.id,
        "live_status",
        {
            "slot_id": slot_id,
            "terminal_id": profile.id,
            "terminal_label": profile.label,
            "broker_name": profile.broker_name,
            "logical_id": symbol_spec.get("logical_id"),
            "name": symbol_spec.get("name"),
            "category": symbol_spec.get("category"),
            "symbol": resolved_symbol,
            "state": state,
            "connected": connected,
            "message": message,
            "pid": os.getpid(),
            "updated_at": now_iso(),
        },
    )


def _emit_tick_diagnostic_event(
    event_queue,
    profile: TerminalProfile,
    event_type: str,
    request_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"request_id": request_id, "pid": os.getpid()}
    if extra:
        payload.update(extra)
    _emit(event_queue, profile.id, event_type, payload)


def _emit_tick_diagnostic_failed(
    event_queue,
    profile: TerminalProfile,
    request_id: str,
    reason: str,
    message: str,
    *,
    code: int | None = None,
) -> None:
    _emit_tick_diagnostic_event(
        event_queue,
        profile,
        "tick_diagnostic_failed",
        request_id,
        {"reason": reason, "message": message, "code": code},
    )


def _start_tick_diagnostic(
    event_queue,
    profile: TerminalProfile,
    request: TickWindowRequest,
    available_symbol_states: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    # Resolução histórica/de dados (COR-DEV-002): diferente do resolvedor
    # operacional usado por snapshot/streaming, aceita um alias exato listado
    # no terminal mesmo com negociação desativada (ex.: WIN$ na Clear).
    # Aceitar essa fonte para diagnóstico não a torna negociável.
    resolved_symbol = resolve_historical_symbol_alias(
        list(request.aliases), set(available_symbol_states)
    )
    if not resolved_symbol:
        _emit_tick_diagnostic_failed(
            event_queue,
            profile,
            request.request_id,
            "symbol_not_found",
            "Nenhum alias adequado deste ativo foi encontrado/listado neste MT5.",
        )
        return None
    active: dict[str, Any] = {
        "request": request,
        "resolved_symbol": resolved_symbol,
        "window_index": 0,
        "chunk_index": 0,
        "chunk_lists": [window.chunks(request.chunk_seconds) for window in request.windows],
        "accumulator": TickWindowAccumulator(),
    }
    _emit_tick_diagnostic_event(
        event_queue,
        profile,
        "tick_diagnostic_accepted",
        request.request_id,
        {
            "resolved_symbol": resolved_symbol,
            "source_id": profile.id,
            "logical_id": request.logical_id,
        },
    )
    return active


def _emit_backfill_event(
    event_queue,
    profile: TerminalProfile,
    event_type: str,
    request_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"request_id": request_id, "pid": os.getpid()}
    if extra:
        payload.update(extra)
    _emit(event_queue, profile.id, event_type, payload)


def _emit_backfill_failed(
    event_queue,
    profile: TerminalProfile,
    request_id: str,
    reason: str,
    message: str,
    *,
    code: int | None = None,
) -> None:
    _emit_backfill_event(
        event_queue, profile, "backfill_failed", request_id, {"reason": reason, "message": message, "code": code}
    )


def _backfill_result_payload(result: BackfillSessionResult) -> dict[str, Any]:
    """Resumo pequeno de um resultado terminal — nunca inclui ticks brutos."""

    payload: dict[str, Any] = {
        "state": result.state,
        "resolved_symbol": result.resolved_symbol,
    }
    if result.summary is not None:
        payload["summary"] = result.summary.to_dict()
    if result.promoted is not None:
        payload["file_path"] = str(result.promoted.path)
        payload["file_size_bytes"] = result.promoted.size_bytes
        payload["sha256"] = result.promoted.sha256
        payload["row_count"] = result.promoted.row_count
    return payload


def _make_backfill_fetch_chunk(connector: MT5Connector, resolved_symbol: str, tick_type: str):
    """Fronteira do worker: busca um chunk e o converte em `TickRecord`.

    Nunca guarda o array bruto além da conversão do chunk corrente. Erros da
    API/constante viram `BackfillSourceError("mt5_error", ...)`; erros de
    extração/tipo dos campos viram `BackfillSourceError("malformed_response",
    ...)` — a mesma separação de causas usada pelo diagnóstico de ticks.
    """

    def _fetch(chunk: TickWindow) -> list[TickRecord]:
        try:
            raw_ticks = connector.copy_ticks_chunk(resolved_symbol, tick_type, chunk.start_utc, chunk.end_utc)
        except MT5TicksError as exc:
            raise BackfillSourceError(exc.reason, exc.message, code=exc.code) from exc
        except Exception as exc:
            raise BackfillSourceError("mt5_error", str(exc)) from exc

        chunk_start_ms = int(chunk.start_utc.timestamp() * 1000)
        chunk_end_ms = int(chunk.end_utc.timestamp() * 1000)
        try:
            records = [
                TickRecord(
                    time=int(row["time"]),
                    time_msc=int(row["time_msc"]),
                    bid=float(row["bid"]),
                    ask=float(row["ask"]),
                    last=float(row["last"]),
                    volume=float(row["volume"]),
                    volume_real=float(row["volume_real"]),
                    flags=int(row["flags"]),
                )
                for row in raw_ticks
                if chunk_start_ms <= int(row["time_msc"]) < chunk_end_ms
            ]
        except Exception as exc:
            raise BackfillSourceError(
                "malformed_response", f"Retorno malformado de copy_ticks_range: {exc}"
            ) from exc
        return records

    return _fetch


def _advance_tick_diagnostic(
    event_queue,
    profile: TerminalProfile,
    connector: MT5Connector,
    active: dict[str, Any],
) -> str:
    """Processa exatamente um chunk do diagnóstico ativo.

    Retorna ``"progress"``, ``"completed"`` ou ``"failed"``. Nunca guarda o
    array bruto de ticks: itera e descarta a cada chamada, delegando o
    resumo ao acumulador incremental de `market_analytics.tick_diagnostics`.
    """

    request: TickWindowRequest = active["request"]
    windows = request.windows
    window_index = active["window_index"]
    if window_index >= len(windows):
        return "completed"
    window = windows[window_index]
    chunk_list = active["chunk_lists"][window_index]
    chunk = chunk_list[active["chunk_index"]]
    accumulator: TickWindowAccumulator = active["accumulator"]

    try:
        raw_ticks = connector.copy_ticks_chunk(
            active["resolved_symbol"], request.tick_type, chunk.start_utc, chunk.end_utc
        )
    except MT5TicksError as exc:
        _emit_tick_diagnostic_failed(
            event_queue, profile, request.request_id, exc.reason, exc.message, code=exc.code
        )
        return "failed"
    except Exception as exc:
        # Defesa em profundidade: qualquer exceção comum que escape do
        # conector (ex.: um fake de teste, ou um caso não previsto na
        # normalização do conector) também vira falha estruturada do
        # diagnóstico, nunca um crash do worker.
        _emit_tick_diagnostic_failed(event_queue, profile, request.request_id, "mt5_error", str(exc))
        return "failed"

    # Fronteira [start, end) filtrada por time_msc: nossas próprias
    # fronteiras entre chunks não perdem nem duplicam um tick que a API já
    # tenha retornado. Isso não garante que a corretora/cache/Tester tenha
    # fornecido todos os ticks realmente existentes no intervalo.
    chunk_start_ms = int(chunk.start_utc.timestamp() * 1000)
    chunk_end_ms = int(chunk.end_utc.timestamp() * 1000)
    try:
        for row in raw_ticks:
            time_msc = int(row["time_msc"])
            if time_msc < chunk_start_ms or time_msc >= chunk_end_ms:
                continue
            accumulator.consume(
                TickRecord(
                    time=int(row["time"]),
                    time_msc=time_msc,
                    bid=float(row["bid"]),
                    ask=float(row["ask"]),
                    last=float(row["last"]),
                    volume=float(row["volume"]),
                    volume_real=float(row["volume_real"]),
                    flags=int(row["flags"]),
                )
            )
    except Exception as exc:
        # Cobre erros de extração (campo ausente/tipo errado) e de
        # validação (NaN/infinito, timestamp incoerente, flags inválidas)
        # levantados por `TickWindowAccumulator.consume`.
        _emit_tick_diagnostic_failed(
            event_queue,
            profile,
            request.request_id,
            "malformed_response",
            f"Retorno malformado de copy_ticks_range: {exc}",
        )
        return "failed"

    active["chunk_index"] += 1
    if active["chunk_index"] < len(chunk_list):
        return "progress"

    summary = accumulator.finalize(
        window=window,
        request_id=request.request_id,
        pid=os.getpid(),
        source_id=profile.id,
        logical_id=request.logical_id,
        resolved_symbol=active["resolved_symbol"],
        tick_type=request.tick_type,
    )
    _emit_tick_diagnostic_event(
        event_queue,
        profile,
        "tick_diagnostic_window_result",
        request.request_id,
        {"summary": summary.to_dict()},
    )
    active["window_index"] += 1
    active["chunk_index"] = 0
    active["accumulator"] = TickWindowAccumulator()
    if active["window_index"] >= len(windows):
        _emit_tick_diagnostic_event(event_queue, profile, "tick_diagnostic_completed", request.request_id)
        return "completed"
    return "progress"


def mt5_worker_main(
    profile_data: dict[str, Any],
    symbol_rows: list[dict[str, Any]],
    command_queue,
    event_queue,
    stop_event: EventType,
    refresh_seconds: float = 2.0,
    live_poll_seconds: float = 0.20,
    reconnect_seconds: float = 5.0,
    heartbeat_seconds: float = 2.0,
    backfill_data_root: str | Path | None = None,
) -> None:
    """Processo persistente que possui uma única conexão MetaTrader5.

    Além dos snapshots consolidados, a baseline atual aceita assinaturas de fluxo ao
    vivo. Cada assinatura consulta um ativo no processo proprietário daquele
    terminal, sem alternar ``initialize`` entre terminais.

    ``backfill_data_root`` (DEV-002) é a raiz onde o backfill grava Parquet e
    o catálogo SQLite; por padrão é `core.config.DEFAULT_MARKET_DATA_ROOT`.
    O catálogo só é aberto sob demanda, na primeira solicitação de backfill
    aceita — um worker que nunca recebe ``start_backfill`` nunca toca esse
    diretório.
    """

    profile = TerminalProfile.from_dict(profile_data)
    symbols = [SymbolDefinition.from_dict(row) for row in symbol_rows]
    connector = MT5Connector(profile)
    force_snapshot = False
    reconnect_attempts = 0
    next_connect = 0.0
    next_snapshot = 0.0
    next_heartbeat = 0.0
    next_live_poll = 0.0
    last_state = WorkerConnectionState.STARTING.value
    last_message = "Worker iniciado; conectando ao MT5..."
    connection_meta: dict[str, Any] = {}
    available_symbol_states: dict[str, dict[str, Any]] = {}
    available_symbols_updated = 0.0

    # slot_id -> configuração e estado local do fluxo.
    live_streams: dict[str, dict[str, Any]] = {}

    # Diagnóstico de cobertura de ticks ativo (no máximo um por worker).
    active_tick_diagnostic: dict[str, Any] | None = None

    # Backfill histórico ativo (no máximo um por worker). O catálogo SQLite
    # só é aberto na primeira solicitação aceita (ver docstring da função).
    active_backfill: dict[str, Any] | None = None
    backfill_data_root_path = Path(backfill_data_root) if backfill_data_root else Path(DEFAULT_MARKET_DATA_ROOT)
    backfill_catalog_conn = None
    worker_pid = os.getpid()
    worker_process_started_at = psutil.Process(worker_pid).create_time()

    _emit(
        event_queue,
        profile.id,
        "started",
        {
            "state": WorkerConnectionState.STARTING.value,
            "alive": True,
            "connected": False,
            "message": last_message,
            "pid": os.getpid(),
            "started_at": now_iso(),
        },
    )

    try:
        while not stop_event.is_set():
            now = time.monotonic()

            while True:
                try:
                    command = command_queue.get_nowait()
                except queue.Empty:
                    break

                if not valid_worker_command(command):
                    logger.warning("Comando inválido ou incompatível descartado: %r", command)
                    continue
                action = command["action"]
                if action == "stop":
                    stop_event.set()
                    break
                if action == "snapshot":
                    force_snapshot = True
                elif action == "update_symbols":
                    rows = command.get("symbols", [])
                    symbols = [SymbolDefinition.from_dict(row) for row in rows if isinstance(row, dict)]
                    force_snapshot = True
                elif action == "reconnect":
                    connector.shutdown()
                    available_symbol_states.clear()
                    for stream in live_streams.values():
                        stream["resolved_symbol"] = None
                    next_connect = 0.0
                elif action == "set_live_stream":
                    slot_id = str(command.get("slot_id", "")).strip()
                    symbol_spec = command.get("symbol") if isinstance(command.get("symbol"), dict) else {}
                    if slot_id and symbol_spec:
                        if active_backfill is not None:
                            # DEV-002 (Portão A, correção de auditoria): backfill e
                            # fluxo ao vivo não podem se sobrepor no mesmo worker,
                            # em nenhuma ordem. A recusa é um evento específico
                            # deste slot; nunca contamina WorkerState nem cria a
                            # assinatura.
                            _emit_live_status(
                                event_queue,
                                profile,
                                slot_id,
                                "rejected_backfill_active",
                                "Já existe um backfill em andamento neste worker; "
                                "pare-o antes de iniciar um fluxo ao vivo.",
                                symbol_spec,
                                connected=connector.initialized,
                            )
                        else:
                            live_streams[slot_id] = {
                                "symbol_spec": symbol_spec,
                                "resolved_symbol": None,
                                "last_signature": None,
                                "poll_sequence": 0,
                                "tick_sequence": 0,
                                "last_status": None,
                            }
                            next_live_poll = 0.0
                            _emit_live_status(
                                event_queue,
                                profile,
                                slot_id,
                                "configuring",
                                "Assinatura recebida; resolvendo símbolo no MT5.",
                                symbol_spec,
                                connected=connector.initialized,
                            )
                elif action == "clear_live_stream":
                    slot_id = str(command.get("slot_id", "")).strip()
                    stream = live_streams.pop(slot_id, None)
                    if stream:
                        _emit_live_status(
                            event_queue,
                            profile,
                            slot_id,
                            "stopped",
                            "Fluxo ao vivo encerrado.",
                            stream["symbol_spec"],
                            stream.get("resolved_symbol"),
                            connected=connector.initialized,
                        )
                elif action == "clear_all_live_streams":
                    for slot_id, stream in list(live_streams.items()):
                        _emit_live_status(
                            event_queue,
                            profile,
                            slot_id,
                            "stopped",
                            "Fluxo ao vivo encerrado.",
                            stream["symbol_spec"],
                            stream.get("resolved_symbol"),
                            connected=connector.initialized,
                        )
                    live_streams.clear()
                elif action == "diagnose_ticks":
                    payload = command.get("request")
                    payload = payload if isinstance(payload, dict) else {}
                    request_id = str(payload.get("request_id", "")).strip()
                    try:
                        request = TickWindowRequest.from_dict(payload)
                    except (ValueError, KeyError, TypeError) as exc:
                        _emit_tick_diagnostic_failed(
                            event_queue, profile, request_id, "invalid_request", str(exc)
                        )
                    else:
                        active_request = (
                            active_tick_diagnostic["request"] if active_tick_diagnostic else None
                        )
                        if active_backfill is not None:
                            # DEV-002 (Portão A, correção de auditoria): backfill e
                            # diagnóstico não podem se sobrepor no mesmo worker,
                            # em nenhuma ordem.
                            _emit_tick_diagnostic_failed(
                                event_queue,
                                profile,
                                request.request_id,
                                "backfill_active",
                                "Já existe um backfill em andamento neste worker; "
                                "pare-o antes de iniciar um diagnóstico.",
                            )
                        elif active_request and active_request.request_id == request.request_id:
                            if active_request.fingerprint() != request.fingerprint():
                                _emit_tick_diagnostic_failed(
                                    event_queue,
                                    profile,
                                    request.request_id,
                                    "request_id_conflict",
                                    "Este request_id já está em execução com parâmetros diferentes.",
                                )
                            # Mesmo request_id e mesma impressão: idempotente, nada a fazer.
                        elif active_tick_diagnostic:
                            _emit_tick_diagnostic_failed(
                                event_queue,
                                profile,
                                request.request_id,
                                "diagnostic_busy",
                                "Já existe um diagnóstico de ticks em andamento neste worker.",
                            )
                        elif not (
                            connector.initialized
                            and last_state == WorkerConnectionState.CONNECTED.value
                        ):
                            _emit_tick_diagnostic_failed(
                                event_queue,
                                profile,
                                request.request_id,
                                "terminal_disconnected",
                                "Terminal não está conectado; diagnóstico não iniciado.",
                            )
                        else:
                            if not available_symbol_states or now - available_symbols_updated >= 30.0:
                                available_symbol_states = connector.list_symbol_states()
                                available_symbols_updated = now
                            active_tick_diagnostic = _start_tick_diagnostic(
                                event_queue, profile, request, available_symbol_states
                            )
                elif action == "start_backfill":
                    payload = command.get("request")
                    payload = payload if isinstance(payload, dict) else {}
                    request_id = str(payload.get("request_id", "")).strip()
                    try:
                        backfill_request = BackfillSessionRequest.from_dict(payload)
                    except (ValueError, KeyError, TypeError) as exc:
                        _emit_backfill_failed(event_queue, profile, request_id, "invalid_request", str(exc))
                    else:
                        active_backfill_request = (
                            active_backfill["request"] if active_backfill else None
                        )
                        if active_backfill_request and active_backfill_request.request_id == backfill_request.request_id:
                            if active_backfill_request.fingerprint() != backfill_request.fingerprint():
                                _emit_backfill_failed(
                                    event_queue,
                                    profile,
                                    backfill_request.request_id,
                                    "request_id_conflict",
                                    "Este request_id já está em execução com parâmetros diferentes.",
                                )
                            # Mesmo request_id e mesma impressão: idempotente, nada a fazer.
                        elif active_backfill:
                            _emit_backfill_failed(
                                event_queue,
                                profile,
                                backfill_request.request_id,
                                "backfill_busy",
                                "Já existe um backfill em andamento neste worker.",
                            )
                        elif live_streams:
                            _emit_backfill_failed(
                                event_queue,
                                profile,
                                backfill_request.request_id,
                                "live_stream_active",
                                "Pare o(s) fluxo(s) ao vivo deste terminal antes de iniciar um backfill.",
                            )
                        elif active_tick_diagnostic:
                            # DEV-002 (Portão A, correção de auditoria): backfill e
                            # diagnóstico não podem se sobrepor no mesmo worker,
                            # em nenhuma ordem.
                            _emit_backfill_failed(
                                event_queue,
                                profile,
                                backfill_request.request_id,
                                "diagnostic_active",
                                "Já existe um diagnóstico de ticks em andamento neste worker; "
                                "aguarde a conclusão antes de iniciar um backfill.",
                            )
                        elif not (
                            connector.initialized
                            and last_state == WorkerConnectionState.CONNECTED.value
                        ):
                            _emit_backfill_failed(
                                event_queue,
                                profile,
                                backfill_request.request_id,
                                "terminal_disconnected",
                                "Terminal não está conectado; backfill não iniciado.",
                            )
                        else:
                            if not available_symbol_states or now - available_symbols_updated >= 30.0:
                                available_symbol_states = connector.list_symbol_states()
                                available_symbols_updated = now
                            resolved_symbol = resolve_historical_symbol_alias(
                                list(backfill_request.aliases), set(available_symbol_states)
                            )
                            if not resolved_symbol:
                                _emit_backfill_failed(
                                    event_queue,
                                    profile,
                                    backfill_request.request_id,
                                    "symbol_not_found",
                                    "Nenhum alias adequado deste ativo foi encontrado/listado neste MT5.",
                                )
                            else:
                                if backfill_catalog_conn is None:
                                    try:
                                        backfill_catalog_conn = open_catalog(
                                            catalog_db_path(backfill_data_root_path)
                                        )
                                    except CatalogStateError as exc:
                                        _emit_backfill_failed(
                                            event_queue,
                                            profile,
                                            backfill_request.request_id,
                                            "catalog_error",
                                            f"Falha ao abrir o catálogo de backfill: {exc}",
                                        )
                                if backfill_catalog_conn is not None:
                                    job, immediate = start_backfill_job(
                                        conn=backfill_catalog_conn,
                                        data_root=backfill_data_root_path,
                                        request=backfill_request,
                                        resolved_symbol=resolved_symbol,
                                        fetch_chunk=_make_backfill_fetch_chunk(
                                            connector, resolved_symbol, backfill_request.tick_type
                                        ),
                                        owner_pid=worker_pid,
                                        owner_process_started_at=worker_process_started_at,
                                        owner_terminal_id=profile.id,
                                    )
                                    if job is None:
                                        assert immediate is not None
                                        _emit_backfill_failed(
                                            event_queue,
                                            profile,
                                            backfill_request.request_id,
                                            immediate.error_reason or "mt5_error",
                                            immediate.error_message or "Falha ao iniciar o backfill.",
                                        )
                                    else:
                                        active_backfill = job
                                        _emit_backfill_event(
                                            event_queue,
                                            profile,
                                            "backfill_accepted",
                                            backfill_request.request_id,
                                            {
                                                "resolved_symbol": resolved_symbol,
                                                "source_id": backfill_request.source_id,
                                                "logical_id": backfill_request.logical_id,
                                                "session_date": backfill_request.session_date.isoformat(),
                                                "attempt_id": job["attempt_id"],
                                            },
                                        )
                elif action == "stop_backfill":
                    stop_request_id = str(command.get("request_id", "")).strip()
                    if active_backfill and active_backfill["request"].request_id == stop_request_id:
                        stop_result = interrupt_backfill_job(
                            active_backfill, "Interrompido a pedido do usuário entre chunks."
                        )
                        _emit_backfill_event(
                            event_queue,
                            profile,
                            "backfill_interrupted",
                            stop_request_id,
                            _backfill_result_payload(stop_result),
                        )
                        active_backfill = None

            if stop_event.is_set():
                break

            if not connector.initialized and now >= next_connect:
                if not _terminal_process_running(profile.terminal_exe):
                    reconnect_attempts += 1
                    last_state = WorkerConnectionState.REOPENING_TERMINAL.value
                    last_message = "MT5 fechado; aguardando reabertura controlada."
                    _emit_terminal_restart_required(event_queue, profile, reconnect_attempts)
                    next_connect = now + reconnect_seconds
                else:
                    status = connector.initialize()
                    connection_meta = _status_payload(status)
                    if status.ok:
                        reconnect_attempts = 0
                        last_state = WorkerConnectionState.CONNECTED.value
                        last_message = status.message
                        next_snapshot = 0.0
                        next_live_poll = 0.0
                        available_symbol_states.clear()
                        for stream in live_streams.values():
                            stream["resolved_symbol"] = None
                        _emit(
                            event_queue,
                            profile.id,
                            "status",
                            {
                                **connection_meta,
                                "state": WorkerConnectionState.CONNECTED.value,
                                "alive": True,
                                "connected": True,
                                "pid": os.getpid(),
                                "reconnect_attempts": reconnect_attempts,
                            },
                        )
                    elif connector.initialized and _keeps_ipc_attachment(status.state):
                        reconnect_attempts = 0
                        last_state = status.state
                        last_message = status.message
                        available_symbol_states.clear()
                        for stream in live_streams.values():
                            stream["resolved_symbol"] = None
                        _emit(
                            event_queue,
                            profile.id,
                            "status",
                            {
                                **connection_meta,
                                "state": last_state,
                                "alive": True,
                                "connected": False,
                                "pid": os.getpid(),
                                "reconnect_attempts": reconnect_attempts,
                            },
                        )
                    else:
                        reconnect_attempts += 1
                        connector.shutdown()
                        next_connect = now + reconnect_seconds
                        reported_state = status.state or WorkerConnectionState.RECONNECTING.value
                        last_state = state_after_reconnect_attempts(
                            reported_state,
                            reconnect_attempts,
                        )
                        last_message = status.message
                        _emit(
                            event_queue,
                            profile.id,
                            "status",
                            {
                                **connection_meta,
                                "state": last_state,
                                "alive": True,
                                "connected": False,
                                "pid": os.getpid(),
                                "reconnect_attempts": reconnect_attempts,
                            },
                        )

            elif connector.initialized:
                current_status = connector.connection_status()
                connection_meta = _status_payload(current_status)
                if not current_status.ok:
                    reported_state = (
                        current_status.state or WorkerConnectionState.RECONNECTING.value
                    )
                    if _keeps_ipc_attachment(reported_state):
                        previous_state = last_state
                        reconnect_attempts = 0
                        last_state = reported_state
                        last_message = current_status.message
                        if previous_state != last_state:
                            available_symbol_states.clear()
                            for stream in live_streams.values():
                                stream["resolved_symbol"] = None
                            _emit(
                                event_queue,
                                profile.id,
                                "status",
                                {
                                    **connection_meta,
                                    "state": last_state,
                                    "alive": True,
                                    "connected": False,
                                    "pid": os.getpid(),
                                    "reconnect_attempts": reconnect_attempts,
                                },
                            )
                    else:
                        reconnect_attempts += 1
                        last_state = state_after_reconnect_attempts(
                            reported_state,
                            reconnect_attempts,
                        )
                        last_message = current_status.message
                        connector.shutdown()
                        available_symbol_states.clear()
                        for stream in live_streams.values():
                            stream["resolved_symbol"] = None
                        next_connect = now + reconnect_seconds
                        if _terminal_process_running(profile.terminal_exe):
                            _emit(
                                event_queue,
                                profile.id,
                                "status",
                                {
                                    **connection_meta,
                                    "state": last_state,
                                    "alive": True,
                                    "connected": False,
                                    "pid": os.getpid(),
                                    "reconnect_attempts": reconnect_attempts,
                                },
                            )
                        else:
                            last_state = WorkerConnectionState.REOPENING_TERMINAL.value
                            last_message = "MT5 fechado; aguardando reabertura controlada."
                            _emit_terminal_restart_required(event_queue, profile, reconnect_attempts)
                else:
                    if last_state != WorkerConnectionState.CONNECTED.value:
                        reconnect_attempts = 0
                        last_state = WorkerConnectionState.CONNECTED.value
                        last_message = current_status.message
                        next_snapshot = 0.0
                        next_live_poll = 0.0
                        available_symbol_states.clear()
                        for stream in live_streams.values():
                            stream["resolved_symbol"] = None
                        _emit(
                            event_queue,
                            profile.id,
                            "status",
                            {
                                **connection_meta,
                                "state": last_state,
                                "alive": True,
                                "connected": True,
                                "pid": os.getpid(),
                                "reconnect_attempts": reconnect_attempts,
                            },
                        )
                    if force_snapshot or now >= next_snapshot:
                        snapshot = build_snapshot_from_connector(profile, connector, symbols)
                        _emit(
                            event_queue,
                            profile.id,
                            "snapshot",
                            {"snapshot": snapshot, "pid": os.getpid()},
                        )
                        force_snapshot = False
                        next_snapshot = now + max(0.5, refresh_seconds)

                    if live_streams and now >= next_live_poll:
                        if not available_symbol_states or now - available_symbols_updated >= 30.0:
                            available_symbol_states = connector.list_symbol_states()
                            available_symbols_updated = now

                        for slot_id, stream in list(live_streams.items()):
                            symbol_spec = stream["symbol_spec"]
                            resolved_symbol = stream.get("resolved_symbol")
                            if not resolved_symbol:
                                resolved_symbol = resolve_symbol_aliases(
                                    list(symbol_spec.get("aliases", [])),
                                    set(available_symbol_states),
                                    available_symbol_states,
                                )
                                stream["resolved_symbol"] = resolved_symbol
                                if not resolved_symbol:
                                    status_signature = ("symbol_not_found", tuple(symbol_spec.get("aliases", [])))
                                    if stream.get("last_status") != status_signature:
                                        _emit_live_status(
                                            event_queue,
                                            profile,
                                            slot_id,
                                            "symbol_not_found",
                                            "Nenhum alias ativo/tradável deste ativo foi encontrado neste MT5.",
                                            symbol_spec,
                                            connected=True,
                                        )
                                        stream["last_status"] = status_signature
                                    continue
                                _emit_live_status(
                                    event_queue,
                                    profile,
                                    slot_id,
                                    "streaming",
                                    f"Fluxo ativo em {resolved_symbol}.",
                                    symbol_spec,
                                    resolved_symbol,
                                    connected=True,
                                )
                                stream["last_status"] = ("streaming", resolved_symbol)

                            tick = connector.get_tick(resolved_symbol)
                            stream["poll_sequence"] += 1
                            signature = (tick.time_msc, tick.bid, tick.ask)
                            changed = signature != stream.get("last_signature")
                            if changed:
                                stream["tick_sequence"] += 1
                                stream["last_signature"] = signature

                            tick_payload = tick.to_dict()
                            tick_payload.update(
                                {
                                    "slot_id": slot_id,
                                    "terminal_id": profile.id,
                                    "terminal_label": profile.label,
                                    "broker_name": profile.broker_name,
                                    "logical_id": symbol_spec.get("logical_id"),
                                    "name": symbol_spec.get("name"),
                                    "category": symbol_spec.get("category"),
                                    "resolved_symbol": resolved_symbol,
                                    "pid": os.getpid(),
                                    "account_login": connection_meta.get("account_login"),
                                    "server": connection_meta.get("server"),
                                    "company": connection_meta.get("company"),
                                    "poll_sequence": stream["poll_sequence"],
                                    "tick_sequence": stream["tick_sequence"],
                                    "changed": changed,
                                }
                            )
                            _emit(event_queue, profile.id, "live_tick", {"tick": tick_payload})

                        next_live_poll = now + max(0.05, live_poll_seconds)

            if active_tick_diagnostic is not None:
                if connector.initialized and last_state == WorkerConnectionState.CONNECTED.value:
                    outcome = _advance_tick_diagnostic(
                        event_queue, profile, connector, active_tick_diagnostic
                    )
                    if outcome in ("completed", "failed"):
                        active_tick_diagnostic = None
                else:
                    _emit_tick_diagnostic_failed(
                        event_queue,
                        profile,
                        active_tick_diagnostic["request"].request_id,
                        "terminal_disconnected",
                        "Conexão perdida durante o diagnóstico de ticks.",
                    )
                    active_tick_diagnostic = None

            if active_backfill is not None:
                if connector.initialized and last_state == WorkerConnectionState.CONNECTED.value:
                    backfill_request: BackfillSessionRequest = active_backfill["request"]
                    outcome, backfill_result = advance_backfill_job(active_backfill)
                    if outcome == "progress":
                        _emit_backfill_event(
                            event_queue,
                            profile,
                            "backfill_progress",
                            backfill_request.request_id,
                            {
                                "resolved_symbol": active_backfill["resolved_symbol"],
                                "chunk_index": active_backfill["chunk_index"],
                                "chunk_count": len(active_backfill["chunks"]),
                            },
                        )
                    elif outcome == "failed":
                        assert backfill_result is not None
                        _emit_backfill_failed(
                            event_queue,
                            profile,
                            backfill_request.request_id,
                            backfill_result.error_reason or "mt5_error",
                            backfill_result.error_message or "Falha no backfill.",
                            code=backfill_result.error_code,
                        )
                        active_backfill = None
                    else:  # "completed" ou "empty"
                        assert backfill_result is not None
                        _emit_backfill_event(
                            event_queue,
                            profile,
                            "backfill_completed",
                            backfill_request.request_id,
                            _backfill_result_payload(backfill_result),
                        )
                        active_backfill = None
                else:
                    interrupted_result = interrupt_backfill_job(
                        active_backfill, "Conexão perdida durante o backfill."
                    )
                    _emit_backfill_event(
                        event_queue,
                        profile,
                        "backfill_interrupted",
                        active_backfill["request"].request_id,
                        _backfill_result_payload(interrupted_result),
                    )
                    active_backfill = None

            if now >= next_heartbeat:
                status = connector.connection_status() if connector.initialized else None
                _emit(
                    event_queue,
                    profile.id,
                    "heartbeat",
                    {
                        "state": (
                            WorkerConnectionState.CONNECTED.value
                            if status and status.ok
                            else last_state
                        ),
                        "alive": True,
                        "connected": bool(status and status.ok),
                        "message": status.message if status else last_message,
                        "pid": os.getpid(),
                        "last_heartbeat": now_iso(),
                        "reconnect_attempts": reconnect_attempts,
                    },
                )
                next_heartbeat = now + heartbeat_seconds

            time.sleep(0.04)

    except BaseException as exc:
        _emit(
            event_queue,
            profile.id,
            "error",
            {
                "state": WorkerConnectionState.WORKER_CRASHED.value,
                "alive": False,
                "connected": False,
                "message": f"Worker interrompido: {exc}",
                "traceback": traceback.format_exc(),
                "pid": os.getpid(),
            },
        )
    finally:
        if active_backfill is not None:
            # Encerramento gracioso do worker com um backfill em andamento:
            # descarta o `.partial` e marca a sessão como interrompida, em
            # vez de deixar o catálogo preso em "running" indefinidamente.
            try:
                interrupt_backfill_job(active_backfill, "Worker encerrado durante o backfill.")
            except Exception:
                logger.exception("Falha ao interromper o backfill ativo durante o encerramento do worker.")
            active_backfill = None
        if backfill_catalog_conn is not None:
            try:
                backfill_catalog_conn.close()
            except Exception:
                logger.exception("Falha ao fechar a conexão do catálogo de backfill.")
        connector.shutdown()
        _emit(
            event_queue,
            profile.id,
            "stopped",
            {
                "state": WorkerConnectionState.STOPPED.value,
                "alive": False,
                "connected": False,
                "message": "Desconectado.",
                "pid": os.getpid(),
            },
        )
