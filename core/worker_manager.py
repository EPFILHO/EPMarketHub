from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import MAX_ACTIVE_TERMINALS
from .models import SymbolDefinition, TerminalProfile
from .mt5_worker import mt5_worker_main
from .terminal_states import (
    MT5_COMMUNICATION_GUIDANCE,
    WORKER_UNRESPONSIVE_SECONDS,
    WorkerConnectionState,
)
from .worker_protocol import WorkerEvent, WorkerState, now_iso, valid_worker_event, worker_command

logger = logging.getLogger(__name__)


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
    ):
        self.context = mp.get_context("spawn")
        self.refresh_seconds = refresh_seconds
        self.live_poll_seconds = live_poll_seconds
        self.max_workers = max(1, int(max_workers))
        self.unresponsive_seconds = max(1.0, float(unresponsive_seconds))
        self.event_queue = self.context.Queue(maxsize=2048)
        self._handles: dict[str, WorkerHandle] = {}
        self._states: dict[str, WorkerState] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._live_slots: dict[str, dict[str, Any]] = {}
        self._live_ticks: dict[str, dict[str, Any]] = {}
        self._live_statuses: dict[str, dict[str, Any]] = {}
        self._last_activity: dict[str, float] = {}
        self._shutdown = False

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
        if self._shutdown:
            return False, "O supervisor de workers já foi encerrado."

        existing = self._handles.get(profile.id)
        if existing and existing.process.is_alive():
            return False, "A leitura deste terminal já está ativa."

        if self.active_count() >= self.max_workers:
            return False, (
                f"Limite de {self.max_workers} conexões MT5 simultâneas atingido. "
                "Pare uma leitura antes de iniciar outra."
            )

        target_path = self._normalize_path(profile.terminal_exe)
        for other_id, handle in self._handles.items():
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
            daemon=True,
        )
        try:
            process.start()
        except Exception as exc:
            self._close_queue(command_queue)
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
        handle = self._handles.get(terminal_id)
        if not handle:
            self._states[terminal_id] = WorkerState(terminal_id=terminal_id)
            self._mark_live_terminal_stopped(terminal_id)
            return False, "A leitura deste terminal já está parada."

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
        self._states[terminal_id] = WorkerState(
            terminal_id=terminal_id,
            state=WorkerConnectionState.STOPPED.value,
            connected=False,
            alive=False,
            message="Desconectado.",
        )
        self._mark_live_terminal_stopped(terminal_id)
        return True, "Leitura persistente encerrada."

    def mark_stopping(self, terminal_id: str) -> bool:
        """Publica a intenção de parada sem executar a operação bloqueante."""

        if terminal_id not in self._handles:
            return False
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
        if self._shutdown:
            return
        for terminal_id in list(self._handles):
            try:
                self.stop_worker(terminal_id)
            except Exception:
                logger.exception("Falha inesperada ao encerrar worker %s", terminal_id)
        if self.active_count():
            logger.error(
                "O encerramento terminou com %s worker(s) ainda vivo(s).",
                self.active_count(),
            )
            return
        self._shutdown = True
        try:
            self.event_queue.close()
            self.event_queue.join_thread()
        except Exception:
            logger.exception("Falha ao fechar a fila de eventos dos workers")

    def active_count(self) -> int:
        return sum(1 for handle in self._handles.values() if handle.process.is_alive())

    def clear_live_streams_for_terminal(self, terminal_id: str) -> int:
        slot_ids = [
            slot_id for slot_id, slot in self._live_slots.items()
            if str(slot.get("terminal_id", "")) == terminal_id
        ]
        for slot_id in slot_ids:
            self.clear_live_stream(slot_id)
        return len(slot_ids)

    def forget_terminal(self, terminal_id: str) -> None:
        self._states.pop(terminal_id, None)
        self._snapshots.pop(terminal_id, None)
        self._last_activity.pop(terminal_id, None)
        self.clear_live_streams_for_terminal(terminal_id)

    def request_snapshot(self, terminal_id: str) -> tuple[bool, str]:
        return self._send_command(terminal_id, worker_command("snapshot"), "Snapshot solicitado.")

    def request_reconnect(self, terminal_id: str) -> tuple[bool, str]:
        return self._send_command(terminal_id, worker_command("reconnect"), "Reconexão solicitada.")

    def update_symbols(self, symbols: Iterable[SymbolDefinition]) -> None:
        payload = worker_command("update_symbols", symbols=[s.to_dict() for s in symbols])
        for terminal_id in list(self._handles):
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
        for slot_id in list(self._live_slots):
            self.clear_live_stream(slot_id)

    def _restore_live_streams(self, terminal_id: str) -> None:
        for slot_id, slot in self._live_slots.items():
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

    def _send_command(self, terminal_id: str, command: dict[str, Any], message: str) -> tuple[bool, str]:
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
        self._detect_unresponsive_workers(events)
        return events

    def _apply_event(self, event: dict[str, Any]) -> bool:
        terminal_id = str(event.get("terminal_id", ""))
        event_type = str(event.get("event", ""))
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if not terminal_id:
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
        else:
            state.update(data)

        state.last_heartbeat = data.get("last_heartbeat") or state.last_heartbeat
        if event_type in {"stopped", "error"}:
            state.alive = False
            state.connected = False
            self._mark_live_terminal_stopped(terminal_id)
        return True

    def _detect_dead_workers(self, events: list[dict[str, Any]]) -> None:
        for terminal_id, handle in list(self._handles.items()):
            if handle.process.is_alive():
                continue
            state = self._states.setdefault(terminal_id, WorkerState(terminal_id=terminal_id))
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
        for terminal_id, handle in self._handles.items():
            if not handle.process.is_alive():
                continue
            last_activity = self._last_activity.get(terminal_id, now)
            if now - last_activity < self.unresponsive_seconds:
                continue
            state = self._states.setdefault(terminal_id, WorkerState(terminal_id=terminal_id))
            if state.state in {
                WorkerConnectionState.STOPPED.value,
                WorkerConnectionState.STOPPING.value,
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

    def _cleanup_handle(self, terminal_id: str, handle: WorkerHandle) -> None:
        self._handles.pop(terminal_id, None)
        self._last_activity.pop(terminal_id, None)
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
        handle = self._handles.get(terminal_id)
        return bool(handle and handle.process.is_alive())

    def state(self, terminal_id: str) -> WorkerState:
        state = self._states.get(terminal_id)
        if state is None:
            state = WorkerState(terminal_id=terminal_id)
            self._states[terminal_id] = state
        if self.is_running(terminal_id):
            state.alive = True
        return state

    def states_payload(self, terminal_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        ids = list(terminal_ids) if terminal_ids is not None else sorted(set(self._states) | set(self._handles))
        return [self.state(terminal_id).to_dict() for terminal_id in ids]

    def snapshots_payload(self) -> dict[str, dict[str, Any]]:
        return dict(self._snapshots)

    def live_streams_payload(self) -> dict[str, dict[str, Any]]:
        slot_ids = sorted(set(self._live_slots) | set(self._live_statuses) | set(self._live_ticks))
        return {
            slot_id: {
                "config": self._live_slots.get(slot_id),
                "status": self._live_statuses.get(slot_id),
                "tick": self._live_ticks.get(slot_id),
            }
            for slot_id in slot_ids
        }
