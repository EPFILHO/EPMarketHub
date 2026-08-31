from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from market_analytics.backfill_catalog import get_session as catalog_get_session
from market_analytics.backfill_catalog import interrupt_session as catalog_interrupt_session
from market_analytics.backfill_catalog import list_running_sessions, open_catalog
from market_analytics.backfill_writer import discard_partial
from market_analytics.tick_backfill import (
    BackfillSessionRequest,
    catalog_db_path,
    raw_partition_dir,
)
from market_analytics.tick_diagnostics import TickWindowRequest

from .config import DEFAULT_MARKET_DATA_ROOT, MAX_ACTIVE_TERMINALS
from .models import SymbolDefinition, TerminalProfile
from .mt5_worker import mt5_worker_main
from .terminal_states import (
    MT5_COMMUNICATION_GUIDANCE,
    WORKER_UNRESPONSIVE_SECONDS,
    WorkerConnectionState,
)
from .worker_protocol import WorkerEvent, WorkerState, now_iso, valid_worker_event, worker_command

logger = logging.getLogger(__name__)

_BACKFILL_RECOVERY_SCAN_SECONDS = 5.0


@dataclass
class WorkerHandle:
    process: Any
    command_queue: Any
    stop_event: Any
    terminal_exe: str


class MT5WorkerManager:
    """Supervisor dos processos persistentes, um por terminal MT5."""

    def __init__(
        self,
        refresh_seconds: float = 2.0,
        live_poll_seconds: float = 0.20,
        max_workers: int = MAX_ACTIVE_TERMINALS,
        unresponsive_seconds: float = WORKER_UNRESPONSIVE_SECONDS,
        backfill_data_root: str | Path | None = None,
    ):
        self.context = mp.get_context("spawn")
        self.refresh_seconds = refresh_seconds
        self.live_poll_seconds = live_poll_seconds
        self.max_workers = max(1, int(max_workers))
        self.unresponsive_seconds = max(1.0, float(unresponsive_seconds))
        self.backfill_data_root = Path(backfill_data_root) if backfill_data_root else Path(DEFAULT_MARKET_DATA_ROOT)
        self.event_queue = self.context.Queue(maxsize=2048)
        self._handles: dict[str, WorkerHandle] = {}
        self._states: dict[str, WorkerState] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._live_slots: dict[str, dict[str, Any]] = {}
        self._live_ticks: dict[str, dict[str, Any]] = {}
        self._live_statuses: dict[str, dict[str, Any]] = {}
        self._last_activity: dict[str, float] = {}
        self._tick_diagnostics: dict[str, dict[str, Any]] = {}
        self._backfills: dict[str, dict[str, Any]] = {}
        self._backfill_catalog_conn = None
        self._last_backfill_recovery_scan = 0.0
        self._unowned_running_warned: set[tuple[str, str, object]] = set()
        self._shutdown = False
        self._lock = threading.RLock()
        self._stopping_terminal_ids: set[str] = set()

    @staticmethod
    def _normalize_path(value: str) -> str:
        try:
            return str(Path(value).resolve()).lower()
        except Exception:
            return value.lower()

    def start_worker(
        self,
        profile: TerminalProfile,
        symbols: Iterable[SymbolDefinition],
    ) -> tuple[bool, str]:
        with self._lock:
            if self._shutdown:
                return False, "O supervisor de workers já foi encerrado."
            existing = self._handles.get(profile.id)
            handles = list(self._handles.items())
        if existing and existing.process.is_alive():
            return False, "A leitura deste terminal já está ativa."

        if self.active_count() >= self.max_workers:
            return False, (
                f"Limite de {self.max_workers} conexões MT5 simultâneas atingido. "
                "Pare uma leitura antes de iniciar outra."
            )

        target_path = self._normalize_path(profile.terminal_exe)
        for other_id, handle in handles:
            if other_id != profile.id and handle.process.is_alive() and self._normalize_path(handle.terminal_exe) == target_path:
                return False, f"Este terminal64.exe já pertence ao worker {other_id}."

        if existing:
            self._cleanup_handle(profile.id, existing)

        command_queue = self.context.Queue(maxsize=128)
        stop_event = self.context.Event()
        process = self.context.Process(
            target=mt5_worker_main,
            name=f"EP-MarketHub-MT5-{profile.id}",
            args=(
                profile.to_dict(),
                [symbol.to_dict() for symbol in symbols],
                command_queue,
                self.event_queue,
                stop_event,
                self.refresh_seconds,
                self.live_poll_seconds,
            ),
            kwargs={"backfill_data_root": str(self.backfill_data_root)},
            daemon=True,
        )
        try:
            process.start()
        except Exception as exc:
            self._close_queue(command_queue)
            with self._lock:
                self._states[profile.id] = WorkerState(
                    terminal_id=profile.id,
                    state=WorkerConnectionState.WORKER_START_FAILED.value,
                    connected=False,
                    alive=False,
                    message=f"Não foi possível criar o processo worker: {exc}",
                )
            logger.exception("Falha ao criar worker para o terminal %s", profile.id)
            return False, f"Não foi possível iniciar a leitura: {exc}"
        handle = WorkerHandle(process, command_queue, stop_event, profile.terminal_exe)
        with self._lock:
            self._handles[profile.id] = handle
            self._states[profile.id] = WorkerState(
                terminal_id=profile.id,
                state=WorkerConnectionState.STARTING.value,
                connected=False,
                alive=True,
                message="Worker iniciado; conectando ao MT5...",
                pid=process.pid,
                started_at=now_iso(),
            )
            self._last_activity[profile.id] = time.monotonic()
            self._stopping_terminal_ids.discard(profile.id)
        self._restore_live_streams(profile.id)
        return True, "Leitura persistente iniciada."

    def start_all(
        self,
        profiles: Iterable[TerminalProfile],
        symbols: Iterable[SymbolDefinition],
    ) -> dict[str, str]:
        symbol_list = list(symbols)
        result: dict[str, str] = {}
        for profile in profiles:
            if not profile.enabled:
                continue
            _, message = self.start_worker(profile, symbol_list)
            result[profile.id] = message
        return result

    def stop_worker(self, terminal_id: str, timeout: float = 5.0) -> tuple[bool, str]:
        with self._lock:
            handle = self._handles.get(terminal_id)
            if not handle:
                self._states[terminal_id] = WorkerState(terminal_id=terminal_id)
                self._mark_live_terminal_stopped(terminal_id)
                return False, "A leitura deste terminal já está parada."
            self._stopping_terminal_ids.add(terminal_id)
            self.mark_stopping(terminal_id)

        try:
            handle.command_queue.put_nowait(worker_command("stop"))
        except (queue.Full, OSError, ValueError, EOFError):
            logger.warning(
                "Fila de comandos indisponível ao parar %s; usando o evento de parada.",
                terminal_id,
                exc_info=True,
            )
        try:
            handle.stop_event.set()
        except Exception:
            logger.exception("Falha ao sinalizar parada graciosa do worker %s", terminal_id)
        try:
            handle.process.join(timeout=timeout)
        except Exception:
            logger.exception("Falha ao aguardar encerramento gracioso do worker %s", terminal_id)
        if handle.process.is_alive():
            try:
                handle.process.terminate()
                handle.process.join(timeout=2.0)
            except Exception:
                logger.exception("Falha ao terminar worker %s", terminal_id)
        if handle.process.is_alive() and hasattr(handle.process, "kill"):
            try:
                handle.process.kill()
                handle.process.join(timeout=2.0)
            except Exception:
                logger.exception("Falha ao forçar encerramento do worker %s", terminal_id)
        if handle.process.is_alive():
            with self._lock:
                self._states[terminal_id] = WorkerState(
                    terminal_id=terminal_id,
                    state=WorkerConnectionState.STOP_FAILED.value,
                    connected=False,
                    alive=True,
                    message="Worker não respondeu ao encerramento forçado.",
                    pid=getattr(handle.process, "pid", None),
                )
                self._mark_live_terminal_stopped(terminal_id)
            return False, "Não foi possível confirmar o encerramento do worker."
        self._cleanup_handle(terminal_id, handle)
        with self._lock:
            self._states[terminal_id] = WorkerState(
                terminal_id=terminal_id,
                state=WorkerConnectionState.STOPPED.value,
                connected=False,
                alive=False,
                message="Desconectado.",
            )
            self._stopping_terminal_ids.discard(terminal_id)
            self._mark_live_terminal_stopped(terminal_id)
        return True, "Leitura persistente encerrada."

    def mark_stopping(self, terminal_id: str) -> bool:
        """Publica a intenção de parada sem executar a operação bloqueante."""

        with self._lock:
            if terminal_id not in self._handles:
                return False
            self._stopping_terminal_ids.add(terminal_id)
            current = self._states.setdefault(terminal_id, WorkerState(terminal_id=terminal_id))
            current.update(
                {
                    "state": WorkerConnectionState.STOPPING.value,
                    "connected": False,
                    "alive": True,
                    "message": "Encerrando leitura persistente...",
                }
            )
            return True

    def stop_all(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            terminal_ids = list(self._handles)
        for terminal_id in terminal_ids:
            try:
                self.stop_worker(terminal_id)
            except Exception:
                logger.exception("Falha inesperada ao encerrar worker %s", terminal_id)
        self.finish_shutdown()

    def finish_shutdown(self) -> bool:
        """Sela o supervisor depois que todos os workers tiveram a morte confirmada."""

        with self._lock:
            if self._shutdown:
                return True
        if self.active_count():
            logger.error(
                "O encerramento terminou com %s worker(s) ainda vivo(s).",
                self.active_count(),
            )
            return False
        with self._lock:
            self._shutdown = True
        try:
            self.event_queue.close()
            self.event_queue.join_thread()
        except Exception:
            logger.exception("Falha ao fechar a fila de eventos dos workers")
        if self._backfill_catalog_conn is not None:
            try:
                self._backfill_catalog_conn.close()
            except Exception:
                logger.exception("Falha ao fechar a conexão do catálogo de backfill do manager")
            self._backfill_catalog_conn = None
        return True

    def active_count(self) -> int:
        with self._lock:
            handles = list(self._handles.values())
        return sum(1 for handle in handles if handle.process.is_alive())

    def clear_live_streams_for_terminal(self, terminal_id: str) -> int:
        with self._lock:
            slot_ids = [
                slot_id
                for slot_id, slot in self._live_slots.items()
                if str(slot.get("terminal_id", "")) == terminal_id
            ]
        for slot_id in slot_ids:
            self.clear_live_stream(slot_id)
        return len(slot_ids)

    def forget_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._states.pop(terminal_id, None)
            self._snapshots.pop(terminal_id, None)
            self._last_activity.pop(terminal_id, None)
            self._stopping_terminal_ids.discard(terminal_id)
            stale_requests = [
                request_id
                for request_id, entry in self._tick_diagnostics.items()
                if entry.get("terminal_id") == terminal_id
            ]
            for request_id in stale_requests:
                self._tick_diagnostics.pop(request_id, None)
            stale_backfills = [
                request_id
                for request_id, entry in self._backfills.items()
                if entry.get("terminal_id") == terminal_id
            ]
            for request_id in stale_backfills:
                self._backfills.pop(request_id, None)
        self.clear_live_streams_for_terminal(terminal_id)

    def request_snapshot(self, terminal_id: str) -> tuple[bool, str]:
        return self._send_command(terminal_id, worker_command("snapshot"), "Snapshot solicitado.")

    def request_reconnect(self, terminal_id: str) -> tuple[bool, str]:
        return self._send_command(terminal_id, worker_command("reconnect"), "Reconexão solicitada.")

    def update_symbols(self, symbols: Iterable[SymbolDefinition]) -> None:
        payload = worker_command("update_symbols", symbols=[s.to_dict() for s in symbols])
        with self._lock:
            terminal_ids = list(self._handles)
        for terminal_id in terminal_ids:
            self._send_command(terminal_id, payload, "")

    def configure_live_stream(
        self,
        slot_id: str,
        profile: TerminalProfile,
        symbol: SymbolDefinition,
    ) -> tuple[bool, str]:
        slot_id = str(slot_id).strip()
        if not slot_id:
            return False, "Identificador do painel ao vivo inválido."
        if not self.is_running(profile.id):
            return False, "Inicie a leitura persistente deste terminal antes do fluxo ao vivo."

        with self._lock:
            previous = self._live_slots.get(slot_id)
        if previous and previous.get("terminal_id") != profile.id:
            self._send_command(
                str(previous.get("terminal_id")),
                worker_command("clear_live_stream", slot_id=slot_id),
                "",
            )

        row = {
            "slot_id": slot_id,
            "terminal_id": profile.id,
            "terminal_label": profile.label,
            "broker_name": profile.broker_name,
            "symbol": symbol.to_dict(),
            "configured_at": now_iso(),
        }
        with self._lock:
            self._live_slots[slot_id] = row
            self._live_ticks.pop(slot_id, None)
            self._live_statuses[slot_id] = {
                **row,
                "state": "configuring",
                "connected": False,
                "message": "Enviando assinatura ao worker...",
                "updated_at": now_iso(),
            }
        sent, message = self._send_command(
            profile.id,
            worker_command("set_live_stream", slot_id=slot_id, symbol=symbol.to_dict()),
            "Fluxo ao vivo configurado.",
        )
        return sent, message

    def clear_live_stream(self, slot_id: str) -> tuple[bool, str]:
        with self._lock:
            slot = self._live_slots.pop(slot_id, None)
            self._live_ticks.pop(slot_id, None)
            self._live_statuses.pop(slot_id, None)
        if not slot:
            return False, "Este fluxo já está parado."
        terminal_id = str(slot.get("terminal_id", ""))
        self._send_command(
            terminal_id,
            worker_command("clear_live_stream", slot_id=slot_id),
            "",
        )
        return True, "Fluxo ao vivo encerrado."

    def clear_all_live_streams(self) -> None:
        with self._lock:
            slot_ids = list(self._live_slots)
        for slot_id in slot_ids:
            self.clear_live_stream(slot_id)

    def request_tick_diagnostic(
        self, terminal_id: str, request: TickWindowRequest
    ) -> tuple[bool, str]:
        """Encaminha um diagnóstico de ticks ao worker dono da conexão.

        A identidade imutável da solicitação é o par (`terminal_id`,
        impressão digital canônica do payload), guardado indefinidamente
        durante a vida deste supervisor — não só enquanto está em execução.
        O mesmo `request_id` reutilizado em outro `terminal_id`, mesmo com
        payload idêntico, é um `request_id_conflict`: nenhum comando é
        enviado a um segundo worker. A checagem e a reserva do `request_id`
        acontecem em uma única seção crítica, sem soltar o lock entre elas,
        para que duas chamadas concorrentes não possam reservar o mesmo
        `request_id` em terminais diferentes.
        """

        fingerprint = request.fingerprint()
        with self._lock:
            existing = self._tick_diagnostics.get(request.request_id)
            if existing is not None:
                if existing["terminal_id"] != terminal_id or existing["fingerprint"] != fingerprint:
                    return (
                        False,
                        "request_id já usado em outro terminal ou com parâmetros diferentes "
                        "(request_id_conflict).",
                    )
                return True, "Diagnóstico já registrado; reaproveitando o estado existente."

            if not self.is_running(terminal_id):
                return False, "Inicie a leitura persistente deste terminal antes do diagnóstico."

            # Reserva o request_id antes de soltar o lock, para que uma
            # segunda chamada concorrente (mesmo request_id, outro terminal)
            # sempre encontre esta reserva em vez de também passar pelo
            # `existing is None` acima.
            self._tick_diagnostics[request.request_id] = {
                "terminal_id": terminal_id,
                "fingerprint": fingerprint,
                "state": "pending",
                "pid": None,
                "windows": [],
                "error": None,
                "updated_at": now_iso(),
            }

        sent, message = self._send_command(
            terminal_id,
            worker_command("diagnose_ticks", request=request.to_dict()),
            "Diagnóstico de ticks solicitado.",
        )
        if not sent:
            with self._lock:
                current = self._tick_diagnostics.get(request.request_id)
                if (
                    current is not None
                    and current["terminal_id"] == terminal_id
                    and current["fingerprint"] == fingerprint
                ):
                    self._tick_diagnostics.pop(request.request_id, None)
        return sent, message

    def tick_diagnostic_status(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._tick_diagnostics.get(request_id)
            return dict(entry) if entry is not None else None

    def tick_diagnostics_payload(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {request_id: dict(entry) for request_id, entry in self._tick_diagnostics.items()}

    def request_backfill(self, terminal_id: str, request: BackfillSessionRequest) -> tuple[bool, str]:
        """Encaminha um backfill de sessão ao worker dono da conexão.

        Mesma identidade imutável de `request_tick_diagnostic`: o par
        (`terminal_id`, impressão digital canônica) é reservado numa única
        seção crítica, e o mesmo `request_id` reaproveitado em outro
        terminal (mesmo com payload idêntico) é `request_id_conflict`.
        """

        fingerprint = request.fingerprint()
        with self._lock:
            existing = self._backfills.get(request.request_id)
            if existing is not None:
                if existing["terminal_id"] != terminal_id or existing["fingerprint"] != fingerprint:
                    return (
                        False,
                        "request_id já usado em outro terminal ou com parâmetros diferentes "
                        "(request_id_conflict).",
                    )
                return True, "Backfill já registrado; reaproveitando o estado existente."

            if not self.is_running(terminal_id):
                return False, "Inicie a leitura persistente deste terminal antes do backfill."

            self._backfills[request.request_id] = {
                "terminal_id": terminal_id,
                "fingerprint": fingerprint,
                "source_id": request.source_id,
                "logical_id": request.logical_id,
                "session_date": request.session_date,
                "state": "pending",
                "pid": None,
                "attempt_id": None,
                "resolved_symbol": None,
                "result": None,
                "error": None,
                "updated_at": now_iso(),
            }

        sent, message = self._send_command(
            terminal_id,
            worker_command("start_backfill", request=request.to_dict()),
            "Backfill de sessão solicitado.",
        )
        if not sent:
            with self._lock:
                current = self._backfills.get(request.request_id)
                if (
                    current is not None
                    and current["terminal_id"] == terminal_id
                    and current["fingerprint"] == fingerprint
                ):
                    self._backfills.pop(request.request_id, None)
        return sent, message

    def stop_backfill(self, request_id: str) -> tuple[bool, str]:
        with self._lock:
            entry = self._backfills.get(request_id)
        if entry is None:
            return False, "Este backfill não está registrado."
        if entry["state"] not in {"pending", "running"}:
            return False, "Este backfill já terminou."
        return self._send_command(
            str(entry["terminal_id"]),
            worker_command("stop_backfill", request_id=request_id),
            "Parada do backfill solicitada.",
        )

    def backfill_status(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._backfills.get(request_id)
            return dict(entry) if entry is not None else None

    def backfills_payload(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {request_id: dict(entry) for request_id, entry in self._backfills.items()}

    def _restore_live_streams(self, terminal_id: str) -> None:
        with self._lock:
            slots = list(self._live_slots.items())
        for slot_id, slot in slots:
            if slot.get("terminal_id") != terminal_id:
                continue
            self._send_command(
                terminal_id,
                worker_command(
                    "set_live_stream",
                    slot_id=slot_id,
                    symbol=slot.get("symbol", {}),
                ),
                "",
            )

    def _mark_live_terminal_stopped(self, terminal_id: str) -> None:
        with self._lock:
            for slot_id, slot in self._live_slots.items():
                if slot.get("terminal_id") != terminal_id:
                    continue
                current = self._live_statuses.get(slot_id, {})
                self._live_statuses[slot_id] = {
                    **slot,
                    **current,
                    "state": "worker_stopped",
                    "connected": False,
                    "message": "Worker deste terminal está parado.",
                    "updated_at": now_iso(),
                }

    def live_stream_terminal_id(self, slot_id: str) -> str:
        with self._lock:
            slot = self._live_slots.get(slot_id) or {}
            return str(slot.get("terminal_id", ""))

    def _send_command(self, terminal_id: str, command: dict[str, Any], message: str) -> tuple[bool, str]:
        with self._lock:
            handle = self._handles.get(terminal_id)
        if not handle or not handle.process.is_alive():
            return False, "A leitura não está ativa para este terminal."
        try:
            handle.command_queue.put_nowait(command)
            return True, message
        except queue.Full:
            return False, "Fila do worker ocupada; tente novamente."
        except (OSError, ValueError, EOFError):
            logger.exception("Fila de comandos indisponível para o worker %s", terminal_id)
            return False, "A comunicação com o worker foi encerrada; reinicie a leitura."

    def poll_events(self, limit: int = 500) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for _ in range(limit):
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            except (OSError, ValueError, EOFError):
                logger.exception("Fila de eventos dos workers foi encerrada ou ficou indisponível")
                break
            if not valid_worker_event(event):
                logger.warning("Evento de worker inválido ou incompatível foi descartado: %r", event)
                continue
            if self._apply_event(event):
                events.append(event)

        self._detect_dead_workers(events)
        self._recover_orphaned_catalog_sessions()
        self._detect_unresponsive_workers(events)
        return events

    def _apply_event(self, event: dict[str, Any]) -> bool:
        with self._lock:
            return self._apply_event_locked(event)

    def _apply_event_locked(self, event: dict[str, Any]) -> bool:
        terminal_id = str(event.get("terminal_id", ""))
        event_type = str(event.get("event", ""))
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if not terminal_id:
            return False
        if terminal_id in self._stopping_terminal_ids:
            logger.debug("Ignorando evento de %s durante seu encerramento.", terminal_id)
            return False
        if terminal_id not in self._handles:
            logger.debug("Ignorando evento tardio de worker já removido: %s", terminal_id)
            return False

        state = self._states.setdefault(terminal_id, WorkerState(terminal_id=terminal_id))
        event_pid = self._event_pid(event_type, data)
        if state.pid is not None and event_pid is not None and str(state.pid) != str(event_pid):
            logger.debug(
                "Ignorando evento residual de %s: PID %s, worker atual PID %s.",
                terminal_id,
                event_pid,
                state.pid,
            )
            return False
        self._last_activity[terminal_id] = time.monotonic()
        if event_type == "snapshot":
            snapshot = data.get("snapshot")
            if isinstance(snapshot, dict):
                self._snapshots[terminal_id] = snapshot
                state.last_snapshot = snapshot.get("timestamp") or event.get("timestamp")
                status = snapshot.get("status") if isinstance(snapshot.get("status"), dict) else {}
                state.update(
                    {
                        "connected": bool(status.get("ok")),
                        "state": status.get("state") or (
                            WorkerConnectionState.CONNECTED.value
                            if status.get("ok")
                            else WorkerConnectionState.RECONNECTING.value
                        ),
                        "message": status.get("message", state.message),
                        "account_login": status.get("account_login"),
                        "server": status.get("server"),
                        "company": status.get("company"),
                        "balance": status.get("balance"),
                        "currency": status.get("currency"),
                        "terminal_path": status.get("terminal_path"),
                    }
                )
        elif event_type == "live_tick":
            tick = data.get("tick")
            if isinstance(tick, dict) and tick.get("slot_id"):
                slot_id = str(tick["slot_id"])
                configured = self._live_slots.get(slot_id)
                # Ignora ticks atrasados de um fluxo já encerrado ou de um worker anterior.
                if not configured or str(configured.get("terminal_id", "")) != terminal_id:
                    return False
                self._live_ticks[slot_id] = tick
                current = self._live_statuses.get(slot_id, {})
                self._live_statuses[slot_id] = {
                    **configured,
                    **current,
                    "state": "streaming" if tick.get("ok", True) else "error",
                    "connected": True,
                    "message": tick.get("message") or f"Recebendo {tick.get('resolved_symbol') or tick.get('symbol')}",
                    "symbol": tick.get("resolved_symbol") or tick.get("symbol"),
                    "pid": tick.get("pid"),
                    "updated_at": tick.get("received_at") or now_iso(),
                }
        elif event_type == "live_status":
            slot_id = str(data.get("slot_id", ""))
            if slot_id:
                configured = self._live_slots.get(slot_id)
                # O worker pode confirmar o encerramento depois que a GUI já limpou o painel.
                if not configured or str(configured.get("terminal_id", "")) != terminal_id:
                    return False
                self._live_statuses[slot_id] = {
                    **configured,
                    **data,
                }
        elif event_type in {
            "tick_diagnostic_accepted",
            "tick_diagnostic_window_result",
            "tick_diagnostic_completed",
            "tick_diagnostic_failed",
        }:
            # Estado de job de diagnóstico, nunca misturado ao WorkerState de
            # conexão do terminal.
            request_id = str(data.get("request_id", ""))
            entry = self._tick_diagnostics.get(request_id)
            if not entry or entry.get("terminal_id") != terminal_id:
                return False
            entry["pid"] = data.get("pid", entry.get("pid"))
            entry["updated_at"] = now_iso()
            if event_type == "tick_diagnostic_accepted":
                entry["state"] = "running"
                entry["resolved_symbol"] = data.get("resolved_symbol")
            elif event_type == "tick_diagnostic_window_result":
                summary = data.get("summary")
                if isinstance(summary, dict):
                    entry["windows"].append(summary)
            elif event_type == "tick_diagnostic_completed":
                entry["state"] = "completed"
            else:
                entry["state"] = "failed"
                entry["error"] = {
                    "reason": data.get("reason"),
                    "message": data.get("message"),
                    "code": data.get("code"),
                }
        elif event_type in {
            "backfill_accepted",
            "backfill_progress",
            "backfill_completed",
            "backfill_failed",
            "backfill_interrupted",
        }:
            # Estado de job de backfill, nunca misturado ao WorkerState de
            # conexão do terminal — mesmo princípio do diagnóstico de ticks.
            request_id = str(data.get("request_id", ""))
            entry = self._backfills.get(request_id)
            if not entry or entry.get("terminal_id") != terminal_id:
                return False
            entry["pid"] = data.get("pid", entry.get("pid"))
            entry["updated_at"] = now_iso()
            if event_type == "backfill_accepted":
                entry["state"] = "running"
                entry["resolved_symbol"] = data.get("resolved_symbol")
                entry["attempt_id"] = data.get("attempt_id")
            elif event_type == "backfill_progress":
                entry["resolved_symbol"] = data.get("resolved_symbol", entry.get("resolved_symbol"))
                entry["chunk_index"] = data.get("chunk_index")
                entry["chunk_count"] = data.get("chunk_count")
            elif event_type == "backfill_completed":
                entry["state"] = data.get("state", "completed")
                entry["result"] = data
            elif event_type == "backfill_interrupted":
                entry["state"] = "interrupted"
                entry["result"] = data
            else:
                entry["state"] = "failed"
                entry["error"] = {
                    "reason": data.get("reason"),
                    "message": data.get("message"),
                    "code": data.get("code"),
                }
        else:
            state.update(data)

        state.last_heartbeat = data.get("last_heartbeat") or state.last_heartbeat
        if event_type in {"stopped", "error"}:
            state.alive = False
            state.connected = False
            self._mark_live_terminal_stopped(terminal_id)
        return True

    def _interrupt_tick_diagnostics_for_terminal(self, terminal_id: str, message: str) -> None:
        """Marca diagnósticos pendentes/em execução como interrompidos.

        A falha de um diagnóstico nunca contamina `WorkerState`, mas a morte
        ou falta de resposta real do worker precisa ser refletida também no
        próprio diagnóstico, sem deixá-lo preso em "running" para sempre.
        """

        with self._lock:
            for entry in self._tick_diagnostics.values():
                if entry.get("terminal_id") == terminal_id and entry.get("state") in {
                    "pending",
                    "running",
                }:
                    entry["state"] = "interrupted"
                    entry["error"] = {"reason": "worker_unavailable", "message": message}
                    entry["updated_at"] = now_iso()

    def _backfill_catalog_conn_for_recovery(self):
        """Conexão do manager ao mesmo catálogo SQLite dos workers, sob demanda.

        Só é aberta quando um worker morre/fica sem resposta com um backfill
        pendente/em execução — nunca no caminho comum de leitura ao vivo, e
        nunca antes de qualquer backfill ter sido solicitado.
        """

        if self._backfill_catalog_conn is None:
            self._backfill_catalog_conn = open_catalog(catalog_db_path(self.backfill_data_root))
        return self._backfill_catalog_conn

    @staticmethod
    def _catalog_owner_alive(row: dict[str, Any]) -> bool | None:
        """Confere PID e instante real de criação do processo proprietário.

        ``False`` é prova de que o dono morreu (PID ausente ou reutilizado),
        ``True`` preserva sua propriedade e ``None`` significa que a prova
        não pôde ser obtida. Falta de heartbeat nunca participa da decisão.
        """

        pid = row.get("owner_pid")
        expected_started_at = row.get("owner_process_started_at")
        if pid is None or expected_started_at is None:
            return None
        try:
            process = psutil.Process(int(pid))
            if not process.is_running():
                return False
            actual_started_at = process.create_time()
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, OSError, ValueError, TypeError):
            return None
        return abs(float(actual_started_at) - float(expected_started_at)) < 0.001

    def _recover_orphaned_catalog_sessions(self, *, force: bool = False) -> None:
        """Libera tentativas órfãs mesmo após reinício completo do aplicativo.

        A varredura usa somente a identidade persistida do processo. Uma
        sessão sem identidade (catálogo legado) ou cujo processo não pôde ser
        inspecionado permanece ``running`` por segurança. A transição ainda é
        condicionada ao mesmo ``attempt_id`` dentro do SQLite, protegendo uma
        nova tentativa que tenha vencido uma corrida entre a leitura e a
        atualização.
        """

        now = time.monotonic()
        if not force and now - self._last_backfill_recovery_scan < _BACKFILL_RECOVERY_SCAN_SECONDS:
            return
        self._last_backfill_recovery_scan = now
        if not catalog_db_path(self.backfill_data_root).exists():
            return
        try:
            conn = self._backfill_catalog_conn_for_recovery()
            rows = list_running_sessions(conn)
        except Exception:
            logger.exception("Falha ao consultar sessões órfãs no catálogo de backfill")
            return

        for row in rows:
            owner_alive = self._catalog_owner_alive(row)
            key = (row["source_id"], row["logical_id"], row["session_date"])
            if owner_alive is True:
                continue
            if owner_alive is None:
                if key not in self._unowned_running_warned:
                    logger.warning(
                        "Sessão running sem identidade de processo verificável foi preservada: %s/%s/%s",
                        *key,
                    )
                    self._unowned_running_warned.add(key)
                continue

            attempt_id = row.get("attempt_id")
            if not attempt_id:
                continue
            try:
                catalog_interrupt_session(
                    conn,
                    source_id=row["source_id"],
                    logical_id=row["logical_id"],
                    session_date=row["session_date"],
                    attempt_id=attempt_id,
                    message="Recuperação automática: o processo proprietário não está mais ativo.",
                )
                final_path = (
                    raw_partition_dir(
                        self.backfill_data_root,
                        source_id=row["source_id"],
                        logical_id=row["logical_id"],
                        session_date=row["session_date"],
                    )
                    / "ticks.parquet"
                )
                discard_partial(final_path.with_name(final_path.name + ".partial"))
                self._unowned_running_warned.discard(key)
                logger.info("Sessão órfã recuperada automaticamente: %s/%s/%s", *key)
            except Exception:
                logger.exception("Falha ao recuperar sessão órfã de backfill: %s/%s/%s", *key)

    def _interrupt_backfills_for_terminal(self, terminal_id: str, message: str) -> None:
        """Marca backfills pendentes/em execução como interrompidos e libera
        a propriedade no catálogo — **só deve ser chamado com a morte do
        worker já confirmada** (`handle.process.is_alive()` falso), nunca
        por ausência temporária de heartbeat com o processo ainda vivo (ver
        `_detect_unresponsive_workers`, correção da quarta auditoria, item
        2): liberar a propriedade durante uma chamada MT5 síncrona longa
        deixaria duas tentativas escrevendo a mesma sessão ao mesmo tempo.

        Além do estado em memória (bookkeeping do manager), libera a sessão
        no catálogo SQLite persistente: o processo do worker morreu com o
        `SessionTickWriter` e a conexão SQLite dele, então só o manager pode
        tirar a linha de `"running"` — sem isso, uma nova tentativa ficaria
        presa para sempre em `backfill_busy`.
        """

        with self._lock:
            stuck = [
                entry
                for entry in self._backfills.values()
                if entry.get("terminal_id") == terminal_id and entry.get("state") in {"pending", "running"}
            ]
            for entry in stuck:
                entry["state"] = "interrupted"
                entry["error"] = {"reason": "worker_unavailable", "message": message}
                entry["updated_at"] = now_iso()
        for entry in stuck:
            source_id = entry.get("source_id")
            logical_id = entry.get("logical_id")
            session_date = entry.get("session_date")
            attempt_id = entry.get("attempt_id")
            if not source_id or not logical_id or session_date is None:
                continue
            if not attempt_id:
                # Evento de aceite perdido (correção da quarta auditoria,
                # item 3): o worker morreu depois de begin_attempt, mas
                # antes do evento `backfill_accepted` chegar a este
                # bookkeeping em memória — o manager nunca soube o
                # attempt_id. Como a morte já está confirmada, consulta a
                # sessão correspondente diretamente no catálogo e recupera o
                # attempt_id dali: só ele sabe se uma tentativa está mesmo
                # `running` e qual é. `interrupt_session` ainda exige esse
                # mesmo attempt_id casar com o gravado na linha antes de
                # aplicar a transição — nunca toca a tentativa de outro
                # worker que porventura já tenha assumido a sessão.
                #
                # Antes disso, uma checagem de ambiguidade: se OUTRO pedido
                # rastreado por este mesmo manager (outro terminal_id) ainda
                # reivindica ativamente a mesma sessão (pending/running), não
                # há como provar com segurança que a linha "running" do
                # catálogo pertence a ESTE worker morto, e não ao outro —
                # "não toque tentativa de outro worker" se aplica também
                # quando a identidade não pode ser desambiguada, não só
                # quando ela é conhecida e diferente. Melhor deixar a linha
                # como está (recuperável depois) do que arriscar interromper
                # a tentativa real de outro worker.
                with self._lock:
                    ambiguous_owner = any(
                        other is not entry
                        and other.get("source_id") == source_id
                        and other.get("logical_id") == logical_id
                        and other.get("session_date") == session_date
                        and other.get("state") in {"pending", "running"}
                        for other in self._backfills.values()
                    )
                if ambiguous_owner:
                    continue
                try:
                    conn = self._backfill_catalog_conn_for_recovery()
                    catalog_row = catalog_get_session(
                        conn, source_id=source_id, logical_id=logical_id, session_date=session_date
                    )
                except Exception:
                    logger.exception(
                        "Falha ao consultar no catálogo a sessão de um evento de aceite perdido: %s/%s/%s",
                        source_id,
                        logical_id,
                        session_date,
                    )
                    continue
                if catalog_row is None or catalog_row.get("state") != "running":
                    continue
                attempt_id = catalog_row.get("attempt_id")
                if not attempt_id:
                    continue
            try:
                conn = self._backfill_catalog_conn_for_recovery()
                catalog_interrupt_session(
                    conn,
                    source_id=source_id,
                    logical_id=logical_id,
                    session_date=session_date,
                    attempt_id=attempt_id,
                    message=message,
                )
            except Exception:
                logger.exception(
                    "Falha ao liberar no catálogo a sessão de backfill de um worker morto: %s/%s/%s",
                    source_id,
                    logical_id,
                    session_date,
                )

    def _detect_dead_workers(self, events: list[dict[str, Any]]) -> None:
        with self._lock:
            handles = list(self._handles.items())
        for terminal_id, handle in handles:
            if handle.process.is_alive():
                continue
            self._interrupt_tick_diagnostics_for_terminal(
                terminal_id, "Worker terminou antes da conclusão do diagnóstico."
            )
            self._interrupt_backfills_for_terminal(
                terminal_id, "Worker terminou antes da conclusão do backfill."
            )
            with self._lock:
                state = self._states.setdefault(terminal_id, WorkerState(terminal_id=terminal_id))
                if (
                    terminal_id in self._stopping_terminal_ids
                    and state.state == WorkerConnectionState.STOPPING.value
                ):
                    # A thread de lifecycle ainda possui este handle e fará a
                    # confirmação/limpeza após join, terminate e kill.
                    continue
                if state.state not in {
                    WorkerConnectionState.STOPPED.value,
                    WorkerConnectionState.ERROR.value,
                    WorkerConnectionState.WORKER_CRASHED.value,
                    WorkerConnectionState.STOP_FAILED.value,
                }:
                    state.update(
                        {
                            "state": WorkerConnectionState.WORKER_CRASHED.value,
                            "alive": False,
                            "connected": False,
                            "message": f"Worker terminou inesperadamente (código {handle.process.exitcode}).",
                        }
                    )
                    events.append(
                        WorkerEvent(
                            terminal_id=terminal_id,
                            event="error",
                            data=state.to_dict(),
                        ).to_dict()
                    )
                else:
                    state.alive = False
                    state.connected = False
                self._mark_live_terminal_stopped(terminal_id)
            self._cleanup_handle(terminal_id, handle)

    def _detect_unresponsive_workers(self, events: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        with self._lock:
            handles = list(self._handles.items())
        for terminal_id, handle in handles:
            if not handle.process.is_alive():
                continue
            with self._lock:
                last_activity = self._last_activity.get(terminal_id, now)
            if now - last_activity < self.unresponsive_seconds:
                continue
            with self._lock:
                state = self._states.setdefault(terminal_id, WorkerState(terminal_id=terminal_id))
                if state.state in {
                    WorkerConnectionState.STOPPED.value,
                    WorkerConnectionState.STOPPING.value,
                    WorkerConnectionState.BROKER_DISCONNECTED.value,
                    WorkerConnectionState.UNRESPONSIVE.value,
                    WorkerConnectionState.ERROR.value,
                    WorkerConnectionState.WORKER_CRASHED.value,
                    WorkerConnectionState.STOP_FAILED.value,
                }:
                    continue
                state.update(
                    {
                        "state": WorkerConnectionState.UNRESPONSIVE.value,
                        "alive": True,
                        "connected": False,
                        "message": MT5_COMMUNICATION_GUIDANCE,
                    }
                )
                events.append(
                    WorkerEvent(
                        terminal_id=terminal_id,
                        event="status",
                        data=state.to_dict(),
                    ).to_dict()
                )
            self._interrupt_tick_diagnostics_for_terminal(
                terminal_id, "Worker sem resposta durante o diagnóstico."
            )
            # Correção da quarta auditoria, item 2: ausência temporária de
            # heartbeat com o processo ainda vivo NUNCA libera a propriedade
            # do catálogo de backfill — isso corromperia a tentativa ativa
            # se ela só estiver presa numa chamada MT5 síncrona longa. A UI
            # já foi marcada `UNRESPONSIVE` acima; a propriedade do catálogo
            # só é liberada em `_detect_dead_workers`, com a morte do
            # processo comprovada por `handle.process.is_alive()`.

    def _cleanup_handle(self, terminal_id: str, handle: WorkerHandle) -> None:
        with self._lock:
            if self._handles.get(terminal_id) is handle:
                self._handles.pop(terminal_id, None)
            self._last_activity.pop(terminal_id, None)
            self._stopping_terminal_ids.discard(terminal_id)
        self._close_queue(handle.command_queue)

    @staticmethod
    def _close_queue(command_queue) -> None:
        try:
            command_queue.close()
            command_queue.join_thread()
        except Exception:
            logger.exception("Falha ao fechar fila de comandos de worker")

    @staticmethod
    def _event_pid(event_type: str, data: dict[str, Any]):
        if event_type == "live_tick":
            tick = data.get("tick") if isinstance(data.get("tick"), dict) else {}
            return tick.get("pid")
        if event_type == "snapshot":
            return data.get("pid")
        return data.get("pid")

    def is_running(self, terminal_id: str) -> bool:
        with self._lock:
            handle = self._handles.get(terminal_id)
        return bool(handle and handle.process.is_alive())

    def state(self, terminal_id: str) -> WorkerState:
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                state = WorkerState(terminal_id=terminal_id)
                self._states[terminal_id] = state
            if self.is_running(terminal_id):
                state.alive = True
            return state

    def states_payload(self, terminal_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            ids = (
                list(terminal_ids)
                if terminal_ids is not None
                else sorted(set(self._states) | set(self._handles))
            )
            return [self.state(terminal_id).to_dict() for terminal_id in ids]

    def snapshots_payload(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._snapshots)

    def live_streams_payload(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            slot_ids = sorted(
                set(self._live_slots) | set(self._live_statuses) | set(self._live_ticks)
            )
            return {
                slot_id: {
                    "config": self._live_slots.get(slot_id),
                    "status": self._live_statuses.get(slot_id),
                    "tick": self._live_ticks.get(slot_id),
                }
                for slot_id in slot_ids
            }
