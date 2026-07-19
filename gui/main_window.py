from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow

from core.models import TerminalProfile
from core.symbol_registry import SymbolRegistry
from core.terminal_manager import TerminalManager
from core.terminal_registry import TerminalRegistry
from core.terminal_states import (
    InstanceIntegrityState,
    ProcessState,
    TerminalProcessStateMachine,
    WorkerConnectionState,
)
from core.worker_manager import MT5WorkerManager
from core.worker_protocol import (
    WORKER_IMMEDIATE_STATE_EVENT_TYPES,
    WORKER_STATE_EVENT_TYPES,
)
from gui.lifecycle_executor import SerializedLifecycleExecutor

logger = logging.getLogger(__name__)


def ok(data: dict | list | str | None = None, message: str = "OK") -> str:
    return json.dumps({"ok": True, "message": message, "data": data}, ensure_ascii=False)


def fail(message: str, data: dict | None = None) -> str:
    return json.dumps({"ok": False, "message": message, "data": data or {}}, ensure_ascii=False)


class MarketHubBridge(QObject):
    """Ponte QWebChannel e supervisor dos workers MT5."""

    terminalsChanged = Signal(str)
    workerStatesChanged = Signal(str)
    snapshotChanged = Signal(str)
    liveTickChanged = Signal(str)
    liveStreamStatusChanged = Signal(str)
    lifecycleProgress = Signal(str)
    lifecycleFinished = Signal(str)

    def __init__(
        self,
        terminal_registry: TerminalRegistry,
        symbol_registry: SymbolRegistry,
        terminal_manager: TerminalManager,
        worker_manager: MT5WorkerManager,
    ):
        super().__init__()
        self.terminal_registry = terminal_registry
        self.symbol_registry = symbol_registry
        self.terminal_manager = terminal_manager
        self.worker_manager = worker_manager
        self.process_states = TerminalProcessStateMachine()
        self._lifecycle_executor = SerializedLifecycleExecutor(self)
        self._lifecycle_executor.progress.connect(
            self._forward_lifecycle_progress,
            Qt.ConnectionType.QueuedConnection,
        )
        self._lifecycle_executor.finished.connect(
            self._finish_lifecycle_operation,
            Qt.ConnectionType.QueuedConnection,
        )
        self._lifecycle_operations: dict[str, dict] = {}
        self._terminal_lifecycle_operations: dict[str, str] = {}
        self._terminal_lifecycle_runtime: dict[str, dict[str, int | bool]] = {}
        self._shutdown_operation_id: str | None = None
        self._completed_shutdown: dict | None = None
        self._cached_terminals: list[dict] = []
        self._cached_worker_states: list[dict] = []
        self._cached_snapshots: dict = {}
        self._cached_live_streams: dict = {}
        self._cached_runtime_limits: dict = {}

        self._last_worker_state_emit = 0.0
        self._normalize_registered_instance_names()
        for profile in self.terminal_registry.list():
            self.terminal_manager.remember(profile)
        self._refresh_lifecycle_cache()

    @property
    def max_active_mt5(self) -> int:
        return self.worker_manager.max_workers

    def _normalize_registered_instance_names(self) -> None:
        """Migra pastas antigas em minúsculas para o padrão CORRETORA-CONTA."""

        for profile in self.terminal_registry.list():
            instance_status = self.terminal_manager.instance_status(profile)
            if not instance_status["ready"]:
                logger.warning(
                    "Normalização da instância %s adiada: %s",
                    profile.id,
                    instance_status["message"],
                )
                continue
            desired_slug = self.terminal_manager.build_instance_slug(
                profile.broker_name, profile.account_login
            )
            current_name = Path(profile.instance_dir).name if profile.instance_dir else ""
            if not current_name or current_name == desired_slug:
                continue
            try:
                if self.terminal_manager.is_running(profile.id, profile):
                    logger.info(
                        "Migração de caixa adiada para %s porque o MT5 está aberto.",
                        profile.id,
                    )
                    continue
                new_dir, terminal_exe = self.terminal_manager.rename_instance(profile, desired_slug)
                profile.instance_slug = desired_slug
                profile.instance_dir = str(new_dir)
                profile.terminal_exe = str(terminal_exe)
                self.terminal_registry.upsert(profile)
            except Exception:
                logger.exception("Não foi possível normalizar a pasta da instância %s", profile.id)

    def _running_mt5_count(self) -> int:
        busy_terminal_ids = set(self._terminal_lifecycle_operations)
        if not busy_terminal_ids:
            return self.terminal_manager.running_count(self.terminal_registry.list())
        cached_by_id = {
            str(row.get("id", "")): row for row in self._cached_terminals
        }
        count = 0
        for profile in self.terminal_registry.list():
            runtime = self._terminal_lifecycle_runtime.get(profile.id)
            cached = cached_by_id.get(profile.id)
            if profile.id in busy_terminal_ids and runtime is not None:
                count += int(bool(runtime.get("running")))
            elif profile.id in busy_terminal_ids and cached is not None:
                count += int(bool(cached.get("running")))
            else:
                count += int(self.terminal_manager.is_running(profile.id, profile))
        return count

    def _activation_limit_message(self) -> str:
        return (
            f"O EP Market Hub permite até {self.max_active_mt5} MT5 abertos/conectados "
            "ao mesmo tempo. Os demais podem permanecer cadastrados."
        )

    def _instance_unavailable(self, profile: TerminalProfile) -> tuple[dict, str | None]:
        status = self.terminal_manager.instance_status(profile)
        if status["ready"]:
            return status, None
        return status, (
            f"{status['message']} Clique no botão Resolver para ver as opções."
        )

    def _duplicate_process_error(self, profile: TerminalProfile) -> str | None:
        process_count = self.terminal_manager.process_count(profile)
        if process_count <= 1:
            return None
        return (
            f"Foram encontrados {process_count} processos para esta instância. "
            "Feche o MT5 antes de reiniciar a leitura."
        )

    def _remove_registration_only(self, profile: TerminalProfile, instance_status: dict) -> str:
        self.worker_manager.clear_live_streams_for_terminal(profile.id)
        if not self.terminal_registry.remove(profile.id):
            return fail("Não foi possível remover o cadastro do terminal.")
        self.worker_manager.forget_terminal(profile.id)
        self.terminal_manager.forget(profile.id)
        self.process_states.forget(profile.id)
        self._emit_terminals()
        self._emit_live_streams()
        return ok(
            {"terminal_id": profile.id, "instance_status": instance_status},
            "Cadastro local removido. Nenhuma pasta ou conta na corretora foi alterada.",
        )

    @staticmethod
    def _orphan_instance_failure(
        label: str,
        broker_name: str,
        account_login: str,
        instance_slug: str,
        instance_status: dict,
    ) -> str:
        return fail(
            "Já existe uma pasta local com esse nome, mas ela não está cadastrada. "
            "Escolha Usar pasta existente para recuperar o cadastro.",
            {
                "reason": "orphan_instance",
                "instance_status": instance_status,
                "candidate": {
                    "label": label.strip(),
                    "broker_name": broker_name.strip(),
                    "account_login": account_login.strip(),
                    "instance_slug": instance_slug,
                },
            },
        )

    def _terminals_payload(self) -> list[dict]:
        rows: list[dict] = []
        cached_by_id = {
            str(row.get("id", "")): row for row in self._cached_terminals
        }
        terminals = sorted(
            self.terminal_registry.list(),
            key=lambda terminal: (
                (terminal.label or "").casefold(),
                (terminal.broker_name or "").casefold(),
                str(terminal.account_login or "").casefold(),
            ),
        )
        for terminal in terminals:
            item = terminal.to_dict()
            cached = cached_by_id.get(terminal.id)
            if terminal.id in self._terminal_lifecycle_operations and cached is not None:
                item["instance_status"] = cached.get("instance_status", {})
                runtime = self._terminal_lifecycle_runtime.get(terminal.id, cached)
                item["running"] = bool(runtime.get("running"))
                item["process_count"] = int(runtime.get("process_count", item["running"]))
                item["process_state"] = self.process_states.resolve(
                    terminal.id,
                    running=item["running"],
                    process_count=item["process_count"],
                )
                item["worker"] = self.worker_manager.state(terminal.id).to_dict()
                rows.append(item)
                continue
            item["instance_status"] = self.terminal_manager.instance_status(terminal)
            process_count = self.terminal_manager.process_count(terminal)
            item["running"] = process_count > 0
            item["process_count"] = process_count
            item["process_state"] = self.process_states.resolve(
                terminal.id,
                running=item["running"],
                process_count=process_count,
            )
            item["worker"] = self.worker_manager.state(terminal.id).to_dict()
            rows.append(item)
        return rows

    def _emit_terminals(self) -> None:
        payload = self._terminals_payload()
        self._cached_terminals = payload
        self.terminalsChanged.emit(json.dumps(payload, ensure_ascii=False))

    def _worker_states_payload(self) -> list[dict]:
        return self.worker_manager.states_payload(
            [terminal.id for terminal in self.terminal_registry.list()]
        )

    def _emit_worker_states(self) -> None:
        payload = self._worker_states_payload()
        self._cached_worker_states = payload
        self.workerStatesChanged.emit(json.dumps(payload, ensure_ascii=False))

    def _refresh_lifecycle_cache(self) -> None:
        self._cached_terminals = self._terminals_payload()
        self._cached_worker_states = self._worker_states_payload()
        self._cached_snapshots = self.worker_manager.snapshots_payload()
        self._cached_live_streams = self.worker_manager.live_streams_payload()
        self._cached_runtime_limits = self._runtime_limits_payload()

    def _runtime_limits_payload(self) -> dict:
        return {
            "max_active_mt5": self.max_active_mt5,
            "registered": len(self.terminal_registry.list()),
            "open_mt5": self._running_mt5_count(),
            "active_workers": self.worker_manager.active_count(),
        }

    def _publish_terminal_transition(self) -> None:
        """Entrega a transição ao QWebEngine antes de uma operação bloqueante."""

        self._emit_worker_states()
        self._emit_terminals()
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def publish_shutdown_transitions(self) -> None:
        """Publica Fechando/Desconectando para todos os recursos ainda ativos."""

        changed = False
        for profile in self.terminal_registry.list():
            if self.terminal_manager.is_running(profile.id, profile):
                self.process_states.set(profile.id, ProcessState.CLOSING)
                changed = True
            if self.worker_manager.mark_stopping(profile.id):
                changed = True
        if changed:
            self._publish_terminal_transition()

    @property
    def lifecycle_busy(self) -> bool:
        return bool(self._lifecycle_operations)

    def terminal_lifecycle_busy(self, terminal_id: str) -> bool:
        return bool(
            self._shutdown_operation_id
            or terminal_id in self._terminal_lifecycle_operations
        )

    def _lifecycle_conflict_response(self, terminal_ids: list[str] | None = None) -> str:
        terminal_ids = terminal_ids or []
        conflicting = {
            terminal_id: self._terminal_lifecycle_operations[terminal_id]
            for terminal_id in terminal_ids
            if terminal_id in self._terminal_lifecycle_operations
        }
        return fail(
            "Aguarde o encerramento do terminal envolvido terminar.",
            {
                "shutdown_operation_id": self._shutdown_operation_id,
                "terminal_operations": conflicting,
            },
        )

    def _has_lifecycle_conflict(self, terminal_ids: list[str]) -> bool:
        return bool(
            self._shutdown_operation_id
            or any(
                terminal_id in self._terminal_lifecycle_operations
                for terminal_id in terminal_ids
            )
        )

    def _stop_profile_blocking(
        self,
        profile: TerminalProfile,
        *,
        close_mt5: bool,
    ) -> dict:
        terminal_id = profile.id
        worker_message = "A leitura deste terminal já está parada."
        worker_error = ""
        if close_mt5:
            try:
                self.worker_manager.clear_live_streams_for_terminal(terminal_id)
            except Exception as exc:
                worker_error = f"Falha ao limpar fluxos: {exc}"
                logger.exception("Falha ao limpar fluxos do terminal %s", terminal_id)
        try:
            _, worker_message = self.worker_manager.stop_worker(terminal_id)
        except Exception as exc:
            worker_error = f"{worker_error}; {exc}" if worker_error else str(exc)
            worker_message = f"Falha ao encerrar worker: {exc}"
            logger.exception("Falha ao encerrar worker %s", terminal_id)

        try:
            worker_running = self.worker_manager.is_running(terminal_id)
        except Exception as exc:
            worker_running = True
            worker_error = f"{worker_error}; {exc}" if worker_error else str(exc)
            logger.exception("Falha ao confirmar worker %s", terminal_id)
        mt5_stopped = False
        terminal_error = ""
        if close_mt5 and not worker_running:
            try:
                mt5_stopped = self.terminal_manager.stop(
                    terminal_id,
                    profile=profile,
                )
            except Exception as exc:
                terminal_error = str(exc)
                logger.exception("Falha ao encerrar MT5 %s", terminal_id)

        try:
            mt5_running = self.terminal_manager.is_running(terminal_id, profile)
        except Exception as exc:
            mt5_running = True
            terminal_error = f"{terminal_error}; {exc}" if terminal_error else str(exc)
            logger.exception("Falha ao confirmar MT5 %s", terminal_id)
        operation_ok = (
            not worker_running
            and not worker_error
            and (not close_mt5 or (not mt5_running and not terminal_error))
        )
        if close_mt5:
            if operation_ok:
                message = "MT5 fechado." if mt5_stopped else "O MT5 já estava fechado."
            else:
                message = (
                    f"MT5 {'ainda aberto' if mt5_running else 'fechado'}, "
                    f"worker {'ainda vivo' if worker_running else 'encerrado'}: "
                    f"{worker_message}"
                )
        else:
            message = worker_message

        return {
            "terminal_id": terminal_id,
            "ok": operation_ok,
            "message": message,
            "worker_message": worker_message,
            "worker_error": worker_error,
            "worker_running": worker_running,
            "mt5_stopped": mt5_stopped,
            "mt5_running": mt5_running,
            "terminal_error": terminal_error,
        }

    def _build_lifecycle_task(
        self,
        kind: str,
        profiles: list[TerminalProfile],
        *,
        close_mt5: bool,
        application_shutdown: bool,
    ):
        def task(report_progress) -> dict:
            results: dict[str, dict] = {}
            setup_errors: list[str] = []
            if application_shutdown:
                try:
                    self.worker_manager.clear_all_live_streams()
                except Exception as exc:
                    setup_errors.append(f"Falha ao limpar fluxos: {exc}")
                    logger.exception("Falha ao limpar fluxos durante encerramento")

            total = len(profiles)
            for index, profile in enumerate(profiles, start=1):
                try:
                    result = self._stop_profile_blocking(profile, close_mt5=close_mt5)
                except Exception as exc:
                    logger.exception("Falha inesperada ao processar terminal %s", profile.id)
                    result = {
                        "terminal_id": profile.id,
                        "ok": False,
                        "message": f"Falha inesperada ao encerrar terminal: {exc}",
                        "worker_message": str(exc),
                        "worker_error": str(exc),
                        "worker_running": True,
                        "mt5_stopped": False,
                        "mt5_running": True,
                        "terminal_error": str(exc),
                    }
                results[profile.id] = result
                report_progress(
                    {
                        "kind": kind,
                        "close_mt5": close_mt5,
                        "index": index,
                        "total": total,
                        "terminal": result,
                    }
                )

            supervisor_finished = True
            if application_shutdown:
                finish_shutdown = getattr(self.worker_manager, "finish_shutdown", None)
                if finish_shutdown is not None:
                    try:
                        supervisor_finished = bool(finish_shutdown())
                    except Exception as exc:
                        supervisor_finished = False
                        setup_errors.append(f"Falha ao finalizar supervisor: {exc}")
                        logger.exception("Falha ao finalizar supervisor de workers")

            failures = [result for result in results.values() if not result["ok"]]
            operation_ok = not failures and not setup_errors and supervisor_finished
            if operation_ok:
                if application_shutdown:
                    message = "Workers e MT5 controlados encerrados."
                elif close_mt5:
                    message = "Terminal encerrado." if total == 1 else "Terminais selecionados fechados."
                else:
                    message = "Leitura encerrada." if total == 1 else "Todas as leituras foram encerradas."
            else:
                message = f"Encerramento concluído com {len(failures)} falha(s)."
                if setup_errors or not supervisor_finished:
                    message = "O encerramento produziu uma falha explícita no supervisor."

            return {
                "kind": kind,
                "close_mt5": close_mt5,
                "ok": operation_ok,
                "message": message,
                "data": {
                    "results": results,
                    "errors": setup_errors,
                    "supervisor_finished": supervisor_finished,
                },
            }

        return task

    def _start_lifecycle_operation(
        self,
        kind: str,
        profiles: list[TerminalProfile],
        *,
        close_mt5: bool,
        application_shutdown: bool = False,
    ) -> str:
        if application_shutdown and self._completed_shutdown is not None:
            return ok(self._completed_shutdown, "O encerramento da aplicação já foi concluído.")
        if application_shutdown and self._shutdown_operation_id:
            return ok(
                {
                    "operation_id": self._shutdown_operation_id,
                    "kind": "application_shutdown",
                },
                "O encerramento da aplicação já está em andamento.",
            )

        unique_profiles = list({profile.id: profile for profile in profiles}.values())
        terminal_ids = [profile.id for profile in unique_profiles]
        if not application_shutdown and self._has_lifecycle_conflict(terminal_ids):
            return self._lifecycle_conflict_response(terminal_ids)

        terminal_runtime = {}
        for profile in unique_profiles:
            process_count = self.terminal_manager.process_count(profile)
            terminal_runtime[profile.id] = {
                "running": process_count > 0,
                "process_count": process_count,
            }

        operation_id = uuid4().hex
        self._lifecycle_operations[operation_id] = {
            "kind": kind,
            "close_mt5": close_mt5,
            "terminal_ids": terminal_ids,
            "application_shutdown": application_shutdown,
        }
        if application_shutdown:
            self._shutdown_operation_id = operation_id
        else:
            for terminal_id in terminal_ids:
                self._terminal_lifecycle_operations[terminal_id] = operation_id
                self._terminal_lifecycle_runtime[terminal_id] = terminal_runtime[terminal_id]

        changed = False
        for profile in unique_profiles:
            if close_mt5 and self.terminal_manager.is_running(profile.id, profile):
                self.process_states.set(profile.id, ProcessState.CLOSING)
                changed = True
            if self.worker_manager.mark_stopping(profile.id):
                changed = True
        if changed:
            self._publish_terminal_transition()
        else:
            self._refresh_lifecycle_cache()

        task = self._build_lifecycle_task(
            kind,
            unique_profiles,
            close_mt5=close_mt5,
            application_shutdown=application_shutdown,
        )
        if not self._lifecycle_executor.start(operation_id, task):
            self._lifecycle_operations.pop(operation_id, None)
            if self._shutdown_operation_id == operation_id:
                self._shutdown_operation_id = None
            for terminal_id in terminal_ids:
                if self._terminal_lifecycle_operations.get(terminal_id) == operation_id:
                    self._terminal_lifecycle_operations.pop(terminal_id, None)
                    self._terminal_lifecycle_runtime.pop(terminal_id, None)
            return fail("Não foi possível iniciar a operação de encerramento.")
        return ok(
            {"operation_id": operation_id, "kind": kind},
            "Operação de encerramento iniciada.",
        )

    @Slot(str)
    def _forward_lifecycle_progress(self, payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            logger.exception("Progresso inválido recebido do executor de encerramento")
            return
        if payload.get("operation_id") not in self._lifecycle_operations:
            logger.warning("Progresso tardio de encerramento descartado: %s", payload_json)
            return
        self.lifecycleProgress.emit(payload_json)

    @Slot(str)
    def _finish_lifecycle_operation(self, payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            logger.exception("Resultado inválido recebido do executor de encerramento")
            return
        operation_id = str(payload.get("operation_id") or "")
        operation = self._lifecycle_operations.get(operation_id)
        if operation is None:
            logger.warning("Conclusão tardia de encerramento descartada: %s", payload_json)
            return

        kind = str(payload.get("kind") or operation.get("kind") or "")
        results = payload.get("data", {}).get("results", {})
        close_mt5 = bool(operation.get("close_mt5"))
        for terminal_id, result in results.items():
            if close_mt5:
                if result.get("worker_running") or result.get("mt5_running"):
                    self.process_states.set(terminal_id, ProcessState.CLOSE_FAILED)
                else:
                    self.process_states.clear(terminal_id)
            else:
                self.process_states.complete_startup(terminal_id)

        self._lifecycle_operations.pop(operation_id, None)
        if self._shutdown_operation_id == operation_id:
            self._shutdown_operation_id = None
        for terminal_id in operation.get("terminal_ids", []):
            if self._terminal_lifecycle_operations.get(terminal_id) == operation_id:
                self._terminal_lifecycle_operations.pop(terminal_id, None)
                self._terminal_lifecycle_runtime.pop(terminal_id, None)
        if kind == "application_shutdown":
            self._completed_shutdown = payload
        self._emit_worker_states()
        self._emit_terminals()
        self._emit_live_streams()
        self._cached_snapshots = self.worker_manager.snapshots_payload()
        self.lifecycleFinished.emit(json.dumps(payload, ensure_ascii=False))

    @Slot(result=str)
    def shutdownApplication(self) -> str:
        return self._start_lifecycle_operation(
            "application_shutdown",
            self.terminal_registry.list(),
            close_mt5=True,
            application_shutdown=True,
        )

    def _emit_live_streams(self) -> None:
        payload = self.worker_manager.live_streams_payload()
        self._cached_live_streams = payload
        self.liveStreamStatusChanged.emit(json.dumps(payload, ensure_ascii=False))

    def poll_worker_events(self) -> None:
        if self._shutdown_operation_id is not None:
            return
        events = self.worker_manager.poll_events()
        if not events:
            return

        should_emit_state = False
        force_state_emit = False
        should_emit_live = False
        should_emit_terminals = False
        for event in events:
            event_type = event.get("event")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            terminal_id = str(event.get("terminal_id", ""))
            worker_state = str(data.get("state", ""))
            if event_type == "snapshot":
                self.process_states.complete_startup(terminal_id)
                should_emit_terminals = True
            elif event_type in {"status", "heartbeat", "error", "stopped"}:
                if worker_state == WorkerConnectionState.REOPENING_TERMINAL.value:
                    self.process_states.set(terminal_id, ProcessState.REOPENING)
                elif worker_state != WorkerConnectionState.STARTING.value:
                    self.process_states.complete_startup(terminal_id)
                if event_type in {"status", "error", "stopped"}:
                    should_emit_terminals = True
            if event_type == "snapshot" and isinstance(data.get("snapshot"), dict):
                self.snapshotChanged.emit(json.dumps(data["snapshot"], ensure_ascii=False))
            elif event_type == "live_tick" and isinstance(data.get("tick"), dict):
                self.liveTickChanged.emit(json.dumps(data["tick"], ensure_ascii=False))
            elif event_type == "live_status":
                should_emit_live = True
            elif event_type == "terminal_restart_required":
                self.process_states.set(terminal_id, ProcessState.REOPENING)
                profile = self.terminal_registry.get(terminal_id)
                if (
                    profile
                    and self.worker_manager.is_running(terminal_id)
                    and not self.terminal_manager.is_running(terminal_id, profile)
                ):
                    instance_status = self.terminal_manager.instance_status(profile)
                    if not instance_status["ready"]:
                        QTimer.singleShot(
                            0,
                            lambda profile=profile: self._start_lifecycle_operation(
                                "reconcile_worker_stop",
                                [profile],
                                close_mt5=False,
                            ),
                        )
                        self.process_states.clear(terminal_id)
                        should_emit_terminals = True
                        should_emit_live = True
                        logger.error(
                            "Reabertura do MT5 %s cancelada: %s",
                            terminal_id,
                            instance_status["message"],
                        )
                    else:
                        try:
                            self.terminal_manager.launch(profile, minimized=True)
                            should_emit_terminals = True
                            logger.info(
                                "MT5 %s reaberto minimizado após fechamento externo.",
                                terminal_id,
                            )
                        except Exception:
                            self.process_states.set(terminal_id, ProcessState.LAUNCH_FAILED)
                            logger.exception(
                                "Falha ao reabrir minimizado o MT5 %s solicitado pelo worker",
                                terminal_id,
                            )

            if event_type == "error":
                logger.error(
                    "Worker %s: %s\n%s",
                    event.get("terminal_id"),
                    data.get("message", "erro não especificado"),
                    data.get("traceback", ""),
                )
            if event_type in WORKER_STATE_EVENT_TYPES:
                should_emit_state = True
            if event_type in WORKER_IMMEDIATE_STATE_EVENT_TYPES:
                force_state_emit = True

        if should_emit_state:
            now = time.monotonic()
            if force_state_emit or now - self._last_worker_state_emit >= 0.35:
                self._emit_worker_states()
                self._last_worker_state_emit = now
        if should_emit_live:
            self._emit_live_streams()
        if should_emit_terminals:
            self._emit_terminals()

    @Slot(result=str)
    def getTerminals(self) -> str:
        if self._shutdown_operation_id is not None:
            return ok(self._cached_terminals)
        self.poll_worker_events()
        return ok(self._terminals_payload())

    @Slot(result=str)
    def getWorkerStates(self) -> str:
        if self._shutdown_operation_id is not None:
            return ok(self._cached_worker_states)
        self.poll_worker_events()
        return ok(self._worker_states_payload())

    @Slot(result=str)
    def getSnapshots(self) -> str:
        if self._shutdown_operation_id is not None:
            return ok(self._cached_snapshots)
        self.poll_worker_events()
        return ok(self.worker_manager.snapshots_payload())

    @Slot(result=str)
    def getLiveStreams(self) -> str:
        if self._shutdown_operation_id is not None:
            return ok(self._cached_live_streams)
        self.poll_worker_events()
        return ok(self.worker_manager.live_streams_payload())

    @Slot(result=str)
    def getSymbols(self) -> str:
        return ok([s.to_dict() for s in self.symbol_registry.list()])

    @Slot(result=str)
    def getBaseMt5Status(self) -> str:
        return ok(self.terminal_manager.base_status())

    @Slot(result=str)
    def getRuntimeLimits(self) -> str:
        if self._shutdown_operation_id is not None:
            return ok(self._cached_runtime_limits)
        return ok(self._runtime_limits_payload())

    @staticmethod
    def _validate_terminal_fields(label: str, broker_name: str, account_login: str) -> str | None:
        if not label.strip():
            return "Informe um apelido para o terminal."
        if not broker_name.strip():
            return "Informe o nome da corretora."
        if not account_login.strip():
            return "Informe o número da conta. Ele diferencia instâncias da mesma corretora."
        return None

    @Slot(str, str, str, result=str)
    def createTerminal(self, label: str, broker_name: str, account_login: str) -> str:
        if self._shutdown_operation_id:
            return self._lifecycle_conflict_response()
        try:
            validation = self._validate_terminal_fields(label, broker_name, account_login)
            if validation:
                return fail(validation)

            duplicate = self.terminal_registry.find_by_identity(broker_name, account_login)
            if duplicate:
                return fail(
                    f"Já existe uma instância para {duplicate.broker_name} — conta {duplicate.account_login}."
                )

            instance_slug = self.terminal_manager.build_instance_slug(broker_name, account_login)
            candidate_status = self.terminal_manager.instance_status_for_slug(instance_slug)
            if candidate_status["state"] != InstanceIntegrityState.DIRECTORY_MISSING.value:
                return self._orphan_instance_failure(
                    label,
                    broker_name,
                    account_login,
                    instance_slug,
                    candidate_status,
                )
            try:
                terminal_exe = self.terminal_manager.create_instance_from_base(instance_slug)
            except FileExistsError:
                candidate_status = self.terminal_manager.instance_status_for_slug(instance_slug)
                if (
                    candidate_status["state"]
                    != InstanceIntegrityState.DIRECTORY_MISSING.value
                ):
                    return self._orphan_instance_failure(
                        label,
                        broker_name,
                        account_login,
                        instance_slug,
                        candidate_status,
                    )
                raise
            profile = TerminalProfile(
                id=uuid4().hex[:12],
                label=label.strip(),
                broker_name=broker_name.strip(),
                account_login=account_login.strip(),
                instance_slug=instance_slug,
                instance_dir=str(terminal_exe.parent),
                terminal_exe=str(terminal_exe),
                portable=True,
            )
            try:
                self.terminal_registry.upsert(profile)
            except Exception as save_error:
                try:
                    self.terminal_manager.rollback_created_instance(terminal_exe.parent)
                except Exception as rollback_error:
                    logger.exception(
                        "Falha ao remover a pasta recém-criada após erro no cadastro"
                    )
                    raise RuntimeError(
                        f"{save_error} A pasta recém-criada também não pôde ser removida: "
                        f"{rollback_error}"
                    ) from save_error
                raise
            self.terminal_manager.remember(profile)
            self._emit_terminals()
            return ok(profile.to_dict(), "Terminal criado. Abra o MT5 e faça login manualmente na primeira vez.")
        except Exception as exc:
            logger.exception("Erro ao criar terminal")
            return fail(str(exc))

    @Slot(str, str, str, result=str)
    def adoptTerminalInstance(self, label: str, broker_name: str, account_login: str) -> str:
        if self._shutdown_operation_id:
            return self._lifecycle_conflict_response()
        validation = self._validate_terminal_fields(label, broker_name, account_login)
        if validation:
            return fail(validation)

        duplicate = self.terminal_registry.find_by_identity(broker_name, account_login)
        if duplicate:
            return fail(
                f"Já existe uma instância para {duplicate.broker_name} — conta {duplicate.account_login}."
            )

        instance_slug = self.terminal_manager.build_instance_slug(broker_name, account_login)
        instance_status = self.terminal_manager.instance_status_for_slug(instance_slug)
        if instance_status["state"] == InstanceIntegrityState.DIRECTORY_MISSING.value:
            return fail("A pasta local não existe mais. Tente criar a instância novamente.")
        if instance_status["state"] == InstanceIntegrityState.INVALID_PATH.value:
            return fail(instance_status["message"])

        target_dir = Path(instance_status["path"]).resolve()
        for existing in self.terminal_registry.list():
            if Path(existing.instance_dir).resolve() == target_dir:
                return fail(
                    f"A pasta local já pertence ao cadastro {existing.broker_name} — "
                    f"conta {existing.account_login}."
                )
        if self.terminal_manager.is_executable_running(instance_status["terminal_exe"]):
            return fail("Feche o MT5 desta pasta antes de recuperar o cadastro.")

        profile = TerminalProfile(
            id=uuid4().hex[:12],
            label=label.strip(),
            broker_name=broker_name.strip(),
            account_login=account_login.strip(),
            instance_slug=instance_slug,
            instance_dir=instance_status["path"],
            terminal_exe=instance_status["terminal_exe"],
            portable=True,
        )
        try:
            repaired = (
                instance_status["state"] == InstanceIntegrityState.EXECUTABLE_MISSING.value
            )
            if repaired:
                terminal_exe = self.terminal_manager.repair_instance_from_base(profile)
                profile.instance_dir = str(terminal_exe.parent)
                profile.terminal_exe = str(terminal_exe)
            self.terminal_registry.upsert(profile)
            self.terminal_manager.remember(profile)
            self._emit_terminals()
            message = (
                "Cadastro recuperado e terminal64.exe reparado. Abra o MT5 e confirme o login manual."
                if repaired
                else "Cadastro recuperado usando a pasta existente. Abra o MT5 para validar a sessão."
            )
            return ok(profile.to_dict(), message)
        except Exception as exc:
            logger.exception("Erro ao recuperar cadastro a partir de pasta existente")
            return fail(str(exc))

    @Slot(str, str, str, str, result=str)
    def updateTerminal(
        self,
        terminal_id: str,
        label: str,
        broker_name: str,
        account_login: str,
    ) -> str:
        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        profile = self.terminal_registry.get(terminal_id)
        if not profile:
            return fail("Terminal não encontrado.")

        if self.terminal_manager.is_running(terminal_id, profile) or self.worker_manager.is_running(
            terminal_id
        ):
            return fail("Feche o MT5 e pare a leitura antes de editar este terminal.")

        instance_status, instance_error = self._instance_unavailable(profile)
        if instance_error:
            return fail(
                instance_error,
                {"reason": "instance_unavailable", "instance_status": instance_status},
            )

        validation = self._validate_terminal_fields(label, broker_name, account_login)
        if validation:
            return fail(validation)

        duplicate = self.terminal_registry.find_by_identity(
            broker_name, account_login, exclude_id=terminal_id
        )
        if duplicate:
            return fail(
                f"Já existe uma instância para {duplicate.broker_name} — conta {duplicate.account_login}."
            )

        old_profile = replace(profile)
        old_dir = Path(profile.instance_dir).resolve()
        new_slug = self.terminal_manager.build_instance_slug(broker_name, account_login)
        renamed_dir: Path | None = None

        try:
            if new_slug != (profile.instance_slug or old_dir.name):
                renamed_dir, terminal_exe = self.terminal_manager.rename_instance(profile, new_slug)
                profile.instance_slug = new_slug
                profile.instance_dir = str(renamed_dir)
                profile.terminal_exe = str(terminal_exe)

            profile.instance_slug = new_slug
            profile.label = label.strip()
            profile.broker_name = broker_name.strip()
            profile.account_login = account_login.strip()
            self.terminal_registry.upsert(profile)
            self.terminal_manager.remember(profile)

            self._emit_terminals()
            self._emit_live_streams()
            return ok(profile.to_dict(), "Dados atualizados e pasta da instância ajustada automaticamente.")
        except Exception as exc:
            logger.exception("Erro ao editar terminal")
            rollback_message = ""
            try:
                if renamed_dir is not None:
                    self.terminal_manager.rollback_rename(renamed_dir, old_dir)
                self.terminal_registry.upsert(old_profile)
                self.terminal_manager.remember(old_profile)
            except Exception as rollback_error:
                logger.exception("Falha ao desfazer edição do terminal")
                rollback_message = (
                    " Também houve falha ao desfazer completamente a edição: "
                    f"{rollback_error}"
                )
            return fail(f"{exc}{rollback_message}")

    def _start_reading_for_profile(self, profile: TerminalProfile) -> tuple[bool, str]:
        """Inicia o worker de um terminal já aberto, sem alternar conexões."""

        if not profile.enabled:
            return False, "Este terminal está desativado."
        started, message = self.worker_manager.start_worker(
            profile,
            self.symbol_registry.list(enabled_only=True),
        )
        if started or self.worker_manager.is_running(profile.id):
            return True, message
        return False, message

    @Slot(str, result=str)
    def launchTerminal(self, terminal_id: str) -> str:
        """Abre a instância controlada e inicia sua leitura persistente."""

        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        try:
            profile = self.terminal_registry.get(terminal_id)
            if not profile:
                return fail("Terminal não encontrado.")
            if not profile.enabled:
                return fail("Este terminal está desativado.")

            instance_status, instance_error = self._instance_unavailable(profile)
            if instance_error:
                return fail(
                    instance_error,
                    {"reason": "instance_unavailable", "instance_status": instance_status},
                )
            duplicate_error = self._duplicate_process_error(profile)
            if duplicate_error:
                return fail(duplicate_error)

            terminal_was_open = self.terminal_manager.is_running(profile.id, profile)
            if not terminal_was_open and (
                self._running_mt5_count() >= self.max_active_mt5
                or self.worker_manager.active_count() >= self.max_active_mt5
            ):
                return fail(self._activation_limit_message())

            if not terminal_was_open:
                self.process_states.set(profile.id, ProcessState.OPENING)
            try:
                self.terminal_manager.launch(profile)
            except Exception:
                self.process_states.set(profile.id, ProcessState.LAUNCH_FAILED)
                self._emit_terminals()
                raise
            reading_ok, reading_message = self._start_reading_for_profile(profile)
            if not reading_ok:
                self.process_states.clear(profile.id)
            self._emit_terminals()

            if not reading_ok:
                return fail(
                    "O MT5 foi aberto, mas a leitura não pôde ser iniciada: "
                    f"{reading_message}"
                )

            message = (
                "MT5 já estava aberto; leitura iniciada."
                if terminal_was_open
                else "MT5 aberto e leitura iniciada. Faça login manual se for o primeiro acesso."
            )
            return ok(profile.to_dict(), message)
        except Exception as exc:
            logger.exception("Erro ao abrir terminal e iniciar leitura")
            return fail(str(exc))

    @Slot(str, result=str)
    def stopTerminal(self, terminal_id: str) -> str:
        profile = self.terminal_registry.get(terminal_id)
        if not profile:
            return fail("Terminal não encontrado.")
        return self._start_lifecycle_operation(
            "close_terminal",
            [profile],
            close_mt5=True,
        )

    @Slot(str, str, result=str)
    def deleteTerminal(self, terminal_id: str, confirmation: str) -> str:
        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        profile = self.terminal_registry.get(terminal_id)
        if not profile:
            return fail("Terminal não encontrado.")
        if confirmation.strip().upper() != "EXCLUIR":
            return fail('Digite EXCLUIR para confirmar a remoção da instância local.')
        if self.terminal_manager.is_running(profile.id, profile) or self.worker_manager.is_running(
            terminal_id
        ):
            return fail("Feche o MT5 e pare a leitura antes de excluir a instância local.")

        instance_status = self.terminal_manager.instance_status(profile)
        if instance_status["state"] in {
            InstanceIntegrityState.DIRECTORY_MISSING.value,
            InstanceIntegrityState.INVALID_PATH.value,
        }:
            try:
                return self._remove_registration_only(profile, instance_status)
            except Exception as exc:
                logger.exception("Erro ao remover cadastro sem pasta controlada")
                return fail(str(exc))

        original_dir: Path | None = None
        staged_dir: Path | None = None
        try:
            self.worker_manager.clear_live_streams_for_terminal(terminal_id)
            original_dir, staged_dir = self.terminal_manager.stage_delete_instance(profile)

            if not self.terminal_registry.remove(terminal_id):
                self.terminal_manager.restore_staged_instance(original_dir, staged_dir)
                return fail("Não foi possível remover o cadastro do terminal.")

            self.worker_manager.forget_terminal(terminal_id)
            self.terminal_manager.forget(terminal_id)
            self.process_states.forget(terminal_id)
            try:
                self.terminal_manager.finalize_staged_delete(staged_dir)
            except Exception:
                logger.exception("Cadastro removido, mas a pasta temporária não pôde ser apagada")
                self._emit_terminals()
                self._emit_live_streams()
                return ok(
                    {"terminal_id": terminal_id, "staged_path": str(staged_dir)},
                    "Terminal removido. Uma pasta temporária ficou pendente para limpeza manual.",
                )

            self._emit_terminals()
            self._emit_live_streams()
            return ok(
                {"terminal_id": terminal_id},
                "Terminal, cadastro e instância local excluídos. A conta na corretora não foi alterada.",
            )
        except Exception as exc:
            logger.exception("Erro ao excluir terminal")
            if original_dir is not None and staged_dir is not None:
                try:
                    self.terminal_manager.restore_staged_instance(original_dir, staged_dir)
                except Exception:
                    logger.exception("Falha ao restaurar a pasta após erro de exclusão")
            return fail(str(exc))

    @Slot(str, result=str)
    def recreateTerminalInstance(self, terminal_id: str) -> str:
        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        profile = self.terminal_registry.get(terminal_id)
        if not profile:
            return fail("Terminal não encontrado.")
        if self.terminal_manager.is_running(profile.id, profile) or self.worker_manager.is_running(
            terminal_id
        ):
            return fail("Feche o MT5 e pare a leitura antes de recriar a instância local.")

        try:
            previous_status = self.terminal_manager.instance_status(profile)
            terminal_exe = self.terminal_manager.repair_instance_from_base(profile)
            profile.instance_dir = str(terminal_exe.parent)
            profile.terminal_exe = str(terminal_exe)
            self.terminal_manager.remember(profile)
            self._emit_terminals()
            return ok(
                {
                    "terminal": profile.to_dict(),
                    "previous_instance_status": previous_status,
                    "instance_status": self.terminal_manager.instance_status(profile),
                },
                "Instância local recriada. Abra o MT5 e faça login manualmente novamente.",
            )
        except Exception as exc:
            logger.exception("Erro ao recriar instância local do terminal %s", terminal_id)
            return fail(str(exc))

    @Slot(str, result=str)
    def removeMissingTerminal(self, terminal_id: str) -> str:
        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        profile = self.terminal_registry.get(terminal_id)
        if not profile:
            return fail("Terminal não encontrado.")
        if self.terminal_manager.is_running(profile.id, profile) or self.worker_manager.is_running(
            terminal_id
        ):
            return fail("Feche o MT5 e pare a leitura antes de remover este cadastro.")

        instance_status = self.terminal_manager.instance_status(profile)
        if instance_status["ready"]:
            return fail(
                "A instância local existe. Use a exclusão normal para remover cadastro e pasta."
            )

        try:
            return self._remove_registration_only(profile, instance_status)
        except Exception as exc:
            logger.exception("Erro ao remover cadastro com instância ausente")
            return fail(str(exc))

    @Slot(str, result=str)
    def startWorker(self, terminal_id: str) -> str:
        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        try:
            profile = self.terminal_registry.get(terminal_id)
            if not profile:
                return fail("Terminal não encontrado.")
            if not profile.enabled:
                return fail("Este terminal está desativado.")
            instance_status, instance_error = self._instance_unavailable(profile)
            if instance_error:
                return fail(
                    instance_error,
                    {"reason": "instance_unavailable", "instance_status": instance_status},
                )
            if not self.terminal_manager.is_running(profile.id, profile):
                return fail("Abra o MT5 antes de iniciar a leitura.")

            duplicate_error = self._duplicate_process_error(profile)
            if duplicate_error:
                return fail(duplicate_error)

            self.process_states.clear(profile.id)
            started, message = self.worker_manager.start_worker(
                profile,
                self.symbol_registry.list(enabled_only=True),
            )
            self._emit_terminals()
            state = self.worker_manager.state(terminal_id).to_dict()
            if not started and not self.worker_manager.is_running(terminal_id):
                return fail(message)
            return ok(state, message)
        except Exception as exc:
            logger.exception("Erro ao iniciar worker")
            return fail(str(exc))

    @Slot(str, result=str)
    def toggleWorker(self, terminal_id: str) -> str:
        """Alterna a leitura sem abrir ou fechar a instância MT5."""

        if self.worker_manager.is_running(terminal_id):
            return self.stopWorker(terminal_id)
        return self.startWorker(terminal_id)

    @Slot(str, result=str)
    def stopWorker(self, terminal_id: str) -> str:
        profile = self.terminal_registry.get(terminal_id)
        if not profile:
            return fail("Terminal não encontrado.")
        return self._start_lifecycle_operation(
            "stop_worker",
            [profile],
            close_mt5=False,
        )

    def _parse_terminal_ids(self, terminal_ids_json: str) -> tuple[list[str], str | None]:
        try:
            raw = json.loads(terminal_ids_json)
        except json.JSONDecodeError:
            return [], "Seleção de terminais inválida."
        if not isinstance(raw, list):
            return [], "Seleção de terminais inválida."
        terminal_ids = []
        seen = set()
        for value in raw:
            terminal_id = str(value or "").strip()
            if terminal_id and terminal_id not in seen:
                terminal_ids.append(terminal_id)
                seen.add(terminal_id)
        if not terminal_ids:
            return [], "Selecione pelo menos um terminal."
        if len(terminal_ids) > self.max_active_mt5:
            return [], f"Selecione no máximo {self.max_active_mt5} terminais."
        return terminal_ids, None

    @Slot(str, result=str)
    def startSelectedWorkers(self, terminal_ids_json: str) -> str:
        try:
            terminal_ids, error = self._parse_terminal_ids(terminal_ids_json)
            if error:
                return fail(error)
            if self._has_lifecycle_conflict(terminal_ids):
                return self._lifecycle_conflict_response(terminal_ids)

            profiles_by_id = {profile.id: profile for profile in self.terminal_registry.list()}
            missing = [terminal_id for terminal_id in terminal_ids if terminal_id not in profiles_by_id]
            if missing:
                return fail("Um ou mais terminais selecionados não existem mais.")

            symbol_list = self.symbol_registry.list(enabled_only=True)
            result: dict[str, str] = {}
            predicted_open = self._running_mt5_count()
            predicted_workers = self.worker_manager.active_count()

            for terminal_id in terminal_ids:
                profile = profiles_by_id[terminal_id]
                if not profile.enabled:
                    result[terminal_id] = "Terminal desativado."
                    continue
                _, instance_error = self._instance_unavailable(profile)
                if instance_error:
                    result[terminal_id] = instance_error
                    continue
                duplicate_error = self._duplicate_process_error(profile)
                if duplicate_error:
                    result[terminal_id] = duplicate_error
                    continue
                if self.worker_manager.is_running(profile.id):
                    result[terminal_id] = "A leitura deste terminal já está ativa."
                    continue

                terminal_is_open = self.terminal_manager.is_running(profile.id, profile)
                if predicted_workers >= self.max_active_mt5:
                    result[terminal_id] = self._activation_limit_message()
                    continue
                if not terminal_is_open and predicted_open >= self.max_active_mt5:
                    result[terminal_id] = self._activation_limit_message()
                    continue

                try:
                    if not terminal_is_open:
                        self.process_states.set(profile.id, ProcessState.OPENING)
                        self.terminal_manager.launch(profile)
                        predicted_open += 1
                    started, message = self.worker_manager.start_worker(profile, symbol_list)
                    result[terminal_id] = message
                    if started:
                        predicted_workers += 1
                    elif not terminal_is_open:
                        self.process_states.clear(profile.id)
                except Exception as exc:
                    if self.terminal_manager.is_running(profile.id, profile):
                        self.process_states.clear(profile.id)
                    else:
                        self.process_states.set(profile.id, ProcessState.LAUNCH_FAILED)
                    logger.exception("Erro ao abrir o terminal selecionado %s", terminal_id)
                    result[terminal_id] = f"Falha ao abrir/iniciar: {exc}"

            self._emit_terminals()
            started_count = sum(
                1 for terminal_id in terminal_ids if self.worker_manager.is_running(terminal_id)
            )
            return ok(
                result,
                f"Seleção processada: {started_count} MT5 com leitura ativa, limite de {self.max_active_mt5}.",
            )
        except Exception as exc:
            logger.exception("Erro ao abrir terminais selecionados")
            return fail(str(exc))

    @Slot(str, result=str)
    def closeSelectedTerminals(self, terminal_ids_json: str) -> str:
        terminal_ids, error = self._parse_terminal_ids(terminal_ids_json)
        if error:
            return fail(error)
        profiles_by_id = {profile.id: profile for profile in self.terminal_registry.list()}
        if any(terminal_id not in profiles_by_id for terminal_id in terminal_ids):
            return fail("Um ou mais terminais selecionados não existem mais.")
        return self._start_lifecycle_operation(
            "close_selected",
            [profiles_by_id[terminal_id] for terminal_id in terminal_ids],
            close_mt5=True,
        )

    @Slot(result=str)
    def startAllWorkers(self) -> str:
        """Compatibilidade temporária; a GUI atual usa startSelectedWorkers."""
        terminal_ids = [profile.id for profile in self.terminal_registry.list()[: self.max_active_mt5]]
        return self.startSelectedWorkers(json.dumps(terminal_ids))

    @Slot(result=str)
    def stopAllWorkers(self) -> str:
        profiles = [
            profile
            for profile in self.terminal_registry.list()
            if self.worker_manager.is_running(profile.id)
        ]
        return self._start_lifecycle_operation(
            "stop_all_workers",
            profiles,
            close_mt5=False,
        )

    @Slot(str, result=str)
    def testConnection(self, terminal_id: str) -> str:
        return self.startWorker(terminal_id)

    @Slot(str, result=str)
    def reconnectWorker(self, terminal_id: str) -> str:
        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        try:
            sent, message = self.worker_manager.request_reconnect(terminal_id)
            return ok(message=message) if sent else fail(message)
        except Exception as exc:
            logger.exception("Erro ao solicitar reconexão")
            return fail(str(exc))

    @Slot(str, result=str)
    def refreshSnapshot(self, terminal_id: str) -> str:
        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        try:
            profile = self.terminal_registry.get(terminal_id)
            if not profile:
                return fail("Terminal não encontrado.")
            if not self.worker_manager.is_running(terminal_id):
                return fail("Inicie a leitura deste terminal antes de solicitar um snapshot.")
            sent, message = self.worker_manager.request_snapshot(terminal_id)
            self._emit_terminals()
            if not sent:
                return fail(message)
            cached = self.worker_manager.snapshots_payload().get(terminal_id)
            return ok(cached, message)
        except Exception as exc:
            logger.exception("Erro ao solicitar snapshot")
            return fail(str(exc))

    @Slot(str, str, str, result=str)
    def configureLiveStream(self, slot_id: str, terminal_id: str, symbol_id: str) -> str:
        if self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        try:
            profile = self.terminal_registry.get(terminal_id)
            if not profile:
                return fail("Terminal do fluxo não encontrado.")
            if not profile.enabled:
                return fail("Este terminal está desativado.")
            instance_status, instance_error = self._instance_unavailable(profile)
            if instance_error:
                return fail(
                    instance_error,
                    {"reason": "instance_unavailable", "instance_status": instance_status},
                )
            duplicate_error = self._duplicate_process_error(profile)
            if duplicate_error:
                return fail(duplicate_error)
            symbol = self.symbol_registry.get(symbol_id)
            if not symbol or not symbol.enabled:
                return fail("Ativo do fluxo não encontrado ou inativo.")

            if not self.worker_manager.is_running(terminal_id):
                terminal_is_open = self.terminal_manager.is_running(profile.id, profile)
                if not terminal_is_open:
                    if self._running_mt5_count() >= self.max_active_mt5:
                        return fail(self._activation_limit_message())
                    self.process_states.set(profile.id, ProcessState.OPENING)
                    try:
                        self.terminal_manager.launch(profile, minimized=True)
                    except Exception:
                        self.process_states.set(profile.id, ProcessState.LAUNCH_FAILED)
                        self._emit_terminals()
                        raise
                started, start_message = self.worker_manager.start_worker(
                    profile,
                    self.symbol_registry.list(enabled_only=True),
                )
                if not started and not self.worker_manager.is_running(terminal_id):
                    self.process_states.clear(profile.id)
                    return fail(start_message)
            sent, message = self.worker_manager.configure_live_stream(slot_id, profile, symbol)
            self._emit_terminals()
            self._emit_live_streams()
            return ok(self.worker_manager.live_streams_payload().get(slot_id), message) if sent else fail(message)
        except Exception as exc:
            logger.exception("Erro ao configurar fluxo ao vivo")
            return fail(str(exc))

    @Slot(str, result=str)
    def clearLiveStream(self, slot_id: str) -> str:
        terminal_id = self.worker_manager.live_stream_terminal_id(slot_id)
        if terminal_id and self.terminal_lifecycle_busy(terminal_id):
            return self._lifecycle_conflict_response([terminal_id])
        try:
            _, message = self.worker_manager.clear_live_stream(slot_id)
            self._emit_live_streams()
            return ok(message=message)
        except Exception as exc:
            logger.exception("Erro ao parar fluxo ao vivo")
            return fail(str(exc))

    @Slot(result=str)
    def clearAllLiveStreams(self) -> str:
        if self.lifecycle_busy:
            return self._lifecycle_conflict_response(
                list(self._terminal_lifecycle_operations)
            )
        try:
            self.worker_manager.clear_all_live_streams()
            self._emit_live_streams()
            return ok(message="Todos os fluxos ao vivo foram encerrados.")
        except Exception as exc:
            logger.exception("Erro ao parar fluxos ao vivo")
            return fail(str(exc))


class MainWindow(QMainWindow):
    def __init__(
        self,
        terminal_registry: TerminalRegistry,
        symbol_registry: SymbolRegistry,
        terminal_manager: TerminalManager,
        worker_manager: MT5WorkerManager,
        web_dir: Path,
    ):
        super().__init__()
        self.terminal_registry = terminal_registry
        self.terminal_manager = terminal_manager
        self.worker_manager = worker_manager
        self._shutdown_done = False
        self._close_requested = False
        self._shutdown_started = False
        self.setWindowTitle("EP Market Hub — Kernel 0.4.11")
        self.resize(1440, 860)

        self.web_view = QWebEngineView(self)
        self.web_view.setStyleSheet("background: #0b1020;")
        self.web_view.page().setBackgroundColor(QColor("#0b1020"))
        self.setCentralWidget(self.web_view)

        self._repaint_timer = QTimer(self)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.setInterval(90)
        self._repaint_timer.timeout.connect(self._force_web_repaint)

        self.channel = QWebChannel(self.web_view.page())
        self.bridge = MarketHubBridge(
            terminal_registry=terminal_registry,
            symbol_registry=symbol_registry,
            terminal_manager=terminal_manager,
            worker_manager=worker_manager,
        )
        self.channel.registerObject("marketHub", self.bridge)
        self.web_view.page().setWebChannel(self.channel)
        self.bridge.lifecycleFinished.connect(self._on_lifecycle_finished)

        self.worker_poll_timer = QTimer(self)
        self.worker_poll_timer.setInterval(150)
        self.worker_poll_timer.timeout.connect(self.bridge.poll_worker_events)
        self.worker_poll_timer.start()

        index_file = web_dir / "index.html"
        self.web_view.load(QUrl.fromLocalFile(str(index_file.resolve())))

    def _schedule_web_repaint(self) -> None:
        if not self._shutdown_done:
            self._repaint_timer.start()

    def _force_web_repaint(self) -> None:
        if self._shutdown_done:
            return
        self.web_view.update()
        self.web_view.repaint()
        self.web_view.page().runJavaScript(
            "window.dispatchEvent(new Event('resize')); void document.body.offsetHeight;"
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_web_repaint()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._schedule_web_repaint()
            QTimer.singleShot(220, self._force_web_repaint)

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        if not self._close_requested:
            self._close_requested = True
            self.worker_poll_timer.stop()
        self._begin_application_shutdown()

    def _begin_application_shutdown(self) -> None:
        if self._shutdown_done or self._shutdown_started:
            return
        if self.bridge.lifecycle_busy:
            return
        logger.info("Encerrando EP Market Hub, workers e MT5 controlados...")
        response = json.loads(self.bridge.shutdownApplication())
        if not response.get("ok"):
            logger.error("Falha explícita ao iniciar shutdown: %s", response.get("message"))
            QTimer.singleShot(0, self._begin_application_shutdown)
            return
        operation_id = response.get("data", {}).get("operation_id")
        if operation_id:
            self._shutdown_started = True
            return
        self._shutdown_done = True
        self.close()

    @Slot(str)
    def _on_lifecycle_finished(self, payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            logger.exception("Resultado de encerramento inválido recebido pela janela")
            return
        if payload.get("kind") != "application_shutdown":
            if self._close_requested:
                QTimer.singleShot(0, self._begin_application_shutdown)
            return

        self._shutdown_started = False
        self._shutdown_done = True
        if payload.get("ok"):
            logger.info("Shutdown concluído com confirmação dos workers e MT5.")
        else:
            logger.error(
                "Shutdown produziu falha explícita: %s | %s",
                payload.get("message"),
                payload.get("data"),
            )
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._shutdown_done:
            event.accept()
            return
        event.ignore()
        if self._close_requested:
            return
        self._close_requested = True
        self.worker_poll_timer.stop()
        try:
            if not self.bridge.lifecycle_busy:
                self.bridge.publish_shutdown_transitions()
            self.web_view.page().runJavaScript(
                "showShutdownTransitions(); void document.body.offsetHeight;"
            )
            self.web_view.update()
            self.web_view.repaint()
            QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
            )
        except Exception:
            logger.exception("Falha ao preparar feedback visual do encerramento")
        QTimer.singleShot(150, self._begin_application_shutdown)
