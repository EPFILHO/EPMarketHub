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

from .market_snapshot import build_snapshot_from_connector, resolve_symbol_aliases
from .models import SymbolDefinition, TerminalProfile
from .mt5_connector import MT5Connector
from .worker_protocol import WorkerEvent, now_iso, valid_worker_command

logger = logging.getLogger(__name__)

LOSSY_EVENT_TYPES = frozenset({"heartbeat", "live_tick", "snapshot"})


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
            "state": "reopening_terminal",
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
) -> None:
    """Processo persistente que possui uma única conexão MetaTrader5.

    Além dos snapshots consolidados, a baseline atual aceita assinaturas de fluxo ao
    vivo. Cada assinatura consulta um ativo no processo proprietário daquele
    terminal, sem alternar ``initialize`` entre terminais.
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
    last_state = "starting"
    last_message = "Worker iniciado; conectando ao MT5..."
    connection_meta: dict[str, Any] = {}
    available_symbol_states: dict[str, dict[str, Any]] = {}
    available_symbols_updated = 0.0

    # slot_id -> configuração e estado local do fluxo.
    live_streams: dict[str, dict[str, Any]] = {}

    _emit(
        event_queue,
        profile.id,
        "started",
        {
            "state": "starting",
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

            if stop_event.is_set():
                break

            if not connector.initialized and now >= next_connect:
                if not _terminal_process_running(profile.terminal_exe):
                    reconnect_attempts += 1
                    last_state = "reopening_terminal"
                    last_message = "MT5 fechado; aguardando reabertura controlada."
                    _emit_terminal_restart_required(event_queue, profile, reconnect_attempts)
                    next_connect = now + reconnect_seconds
                else:
                    status = connector.initialize()
                    connection_meta = _status_payload(status)
                    if status.ok:
                        reconnect_attempts = 0
                        last_state = "connected"
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
                                "state": "connected",
                                "alive": True,
                                "connected": True,
                                "pid": os.getpid(),
                                "reconnect_attempts": reconnect_attempts,
                            },
                        )
                    else:
                        reconnect_attempts += 1
                        connector.shutdown()
                        next_connect = now + reconnect_seconds
                        waiting_login = "sem conta logada" in status.message.lower()
                        last_state = "waiting_login" if waiting_login else "reconnecting"
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

            if connector.initialized:
                current_status = connector.connection_status()
                connection_meta = _status_payload(current_status)
                if not current_status.ok:
                    reconnect_attempts += 1
                    last_state = "reconnecting"
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
                                "state": "reconnecting",
                                "alive": True,
                                "connected": False,
                                "pid": os.getpid(),
                                "reconnect_attempts": reconnect_attempts,
                            },
                        )
                    else:
                        last_state = "reopening_terminal"
                        last_message = "MT5 fechado; aguardando reabertura controlada."
                        _emit_terminal_restart_required(event_queue, profile, reconnect_attempts)
                else:
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

            if now >= next_heartbeat:
                status = connector.connection_status() if connector.initialized else None
                _emit(
                    event_queue,
                    profile.id,
                    "heartbeat",
                    {
                        "state": "connected" if status and status.ok else last_state,
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
                "state": "error",
                "alive": False,
                "connected": False,
                "message": f"Worker interrompido: {exc}",
                "traceback": traceback.format_exc(),
                "pid": os.getpid(),
            },
        )
    finally:
        connector.shutdown()
        _emit(
            event_queue,
            profile.id,
            "stopped",
            {
                "state": "stopped",
                "alive": False,
                "connected": False,
                "message": "Desconectado.",
                "pid": os.getpid(),
            },
        )
