from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow

from core.models import TerminalProfile
from core.symbol_registry import SymbolRegistry
from core.terminal_manager import TerminalManager
from core.terminal_registry import TerminalRegistry
from core.worker_manager import MT5WorkerManager

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

        self._last_worker_state_emit = 0.0
        self._normalize_registered_instance_names()
        for profile in self.terminal_registry.list():
            self.terminal_manager.remember(profile)

    @property
    def max_active_mt5(self) -> int:
        return self.worker_manager.max_workers

    def _normalize_registered_instance_names(self) -> None:
        """Migra pastas antigas em minúsculas para o padrão CORRETORA-CONTA."""

        for profile in self.terminal_registry.list():
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
        return self.terminal_manager.running_count(self.terminal_registry.list())

    def _activation_limit_message(self) -> str:
        return (
            f"O EP Market Hub permite até {self.max_active_mt5} MT5 abertos/conectados "
            "ao mesmo tempo. Os demais podem permanecer cadastrados."
        )

    def _terminals_payload(self) -> list[dict]:
        rows: list[dict] = []
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
            item["running"] = self.terminal_manager.is_running(terminal.id, terminal)
            item["worker"] = self.worker_manager.state(terminal.id).to_dict()
            rows.append(item)
        return rows

    def _emit_terminals(self) -> None:
        self.terminalsChanged.emit(json.dumps(self._terminals_payload(), ensure_ascii=False))

    def _emit_live_streams(self) -> None:
        self.liveStreamStatusChanged.emit(
            json.dumps(self.worker_manager.live_streams_payload(), ensure_ascii=False)
        )

    def poll_worker_events(self) -> None:
        events = self.worker_manager.poll_events()
        if not events:
            return

        should_emit_state = False
        force_state_emit = False
        should_emit_live = False
        for event in events:
            event_type = event.get("event")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event_type == "snapshot" and isinstance(data.get("snapshot"), dict):
                self.snapshotChanged.emit(json.dumps(data["snapshot"], ensure_ascii=False))
            elif event_type == "live_tick" and isinstance(data.get("tick"), dict):
                self.liveTickChanged.emit(json.dumps(data["tick"], ensure_ascii=False))
            elif event_type == "live_status":
                should_emit_live = True

            if event_type == "error":
                logger.error(
                    "Worker %s: %s\n%s",
                    event.get("terminal_id"),
                    data.get("message", "erro não especificado"),
                    data.get("traceback", ""),
                )
            if event_type in {"started", "status", "heartbeat", "stopped", "error", "snapshot"}:
                should_emit_state = True
            if event_type in {"started", "status", "stopped", "error"}:
                force_state_emit = True

        if should_emit_state:
            now = time.monotonic()
            if force_state_emit or now - self._last_worker_state_emit >= 0.35:
                states = self.worker_manager.states_payload([t.id for t in self.terminal_registry.list()])
                self.workerStatesChanged.emit(json.dumps(states, ensure_ascii=False))
                self._last_worker_state_emit = now
        if should_emit_live:
            self._emit_live_streams()

    @Slot(result=str)
    def getTerminals(self) -> str:
        self.worker_manager.poll_events()
        return ok(self._terminals_payload())

    @Slot(result=str)
    def getWorkerStates(self) -> str:
        self.worker_manager.poll_events()
        return ok(self.worker_manager.states_payload([t.id for t in self.terminal_registry.list()]))

    @Slot(result=str)
    def getSnapshots(self) -> str:
        self.worker_manager.poll_events()
        return ok(self.worker_manager.snapshots_payload())

    @Slot(result=str)
    def getLiveStreams(self) -> str:
        self.worker_manager.poll_events()
        return ok(self.worker_manager.live_streams_payload())

    @Slot(result=str)
    def getSymbols(self) -> str:
        return ok([s.to_dict() for s in self.symbol_registry.list()])

    @Slot(result=str)
    def getBaseMt5Status(self) -> str:
        return ok(self.terminal_manager.base_status())

    @Slot(result=str)
    def getRuntimeLimits(self) -> str:
        return ok({
            "max_active_mt5": self.max_active_mt5,
            "registered": len(self.terminal_registry.list()),
            "open_mt5": self._running_mt5_count(),
            "active_workers": self.worker_manager.active_count(),
        })

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
            terminal_exe = self.terminal_manager.create_instance_from_base(instance_slug)
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

    @Slot(str, str, str, str, result=str)
    def updateTerminal(
        self,
        terminal_id: str,
        label: str,
        broker_name: str,
        account_login: str,
    ) -> str:
        profile = self.terminal_registry.get(terminal_id)
        if not profile:
            return fail("Terminal não encontrado.")

        if self.terminal_manager.is_running(terminal_id, profile) or self.worker_manager.is_running(
            terminal_id
        ):
            return fail("Feche o MT5 e pare a leitura antes de editar este terminal.")

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

        try:
            profile = self.terminal_registry.get(terminal_id)
            if not profile:
                return fail("Terminal não encontrado.")

            terminal_was_open = self.terminal_manager.is_running(profile.id, profile)
            if not terminal_was_open and self._running_mt5_count() >= self.max_active_mt5:
                return fail(self._activation_limit_message())

            self.terminal_manager.launch(profile)
            reading_ok, reading_message = self._start_reading_for_profile(profile)
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
        try:
            self.worker_manager.stop_worker(terminal_id)
            stopped = self.terminal_manager.stop(terminal_id)
            self._emit_terminals()
            self._emit_live_streams()
            return ok({"stopped": stopped}, "Leitura encerrada e comando para fechar o MT5 executado.")
        except Exception as exc:
            logger.exception("Erro ao parar terminal")
            return fail(str(exc))

    @Slot(str, str, result=str)
    def deleteTerminal(self, terminal_id: str, confirmation: str) -> str:
        profile = self.terminal_registry.get(terminal_id)
        if not profile:
            return fail("Terminal não encontrado.")
        if confirmation.strip().upper() != "EXCLUIR":
            return fail('Digite EXCLUIR para confirmar a remoção da instância local.')
        if self.terminal_manager.is_running(profile.id, profile):
            return fail("Feche o MT5 antes de excluir a instância local.")

        original_dir: Path | None = None
        staged_dir: Path | None = None
        try:
            self.worker_manager.clear_live_streams_for_terminal(terminal_id)
            self.worker_manager.stop_worker(terminal_id)
            self.terminal_manager.stop(terminal_id, profile=profile)
            original_dir, staged_dir = self.terminal_manager.stage_delete_instance(profile)

            if not self.terminal_registry.remove(terminal_id):
                self.terminal_manager.restore_staged_instance(original_dir, staged_dir)
                return fail("Não foi possível remover o cadastro do terminal.")

            self.worker_manager.forget_terminal(terminal_id)
            self.terminal_manager.forget(terminal_id)
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
    def startWorker(self, terminal_id: str) -> str:
        try:
            profile = self.terminal_registry.get(terminal_id)
            if not profile:
                return fail("Terminal não encontrado.")
            if not self.terminal_manager.is_running(profile.id, profile):
                return fail("Abra o MT5 antes de iniciar a leitura.")

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
        try:
            _, message = self.worker_manager.stop_worker(terminal_id)
            self._emit_terminals()
            self._emit_live_streams()
            return ok(self.worker_manager.state(terminal_id).to_dict(), message)
        except Exception as exc:
            logger.exception("Erro ao parar worker")
            return fail(str(exc))

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
                        self.terminal_manager.launch(profile)
                        predicted_open += 1
                    started, message = self.worker_manager.start_worker(profile, symbol_list)
                    result[terminal_id] = message
                    if started:
                        predicted_workers += 1
                except Exception as exc:
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
        try:
            terminal_ids, error = self._parse_terminal_ids(terminal_ids_json)
            if error:
                return fail(error)
            profiles_by_id = {profile.id: profile for profile in self.terminal_registry.list()}
            result: dict[str, str] = {}
            for terminal_id in terminal_ids:
                profile = profiles_by_id.get(terminal_id)
                if not profile:
                    result[terminal_id] = "Terminal não encontrado."
                    continue
                self.worker_manager.clear_live_streams_for_terminal(terminal_id)
                self.worker_manager.stop_worker(terminal_id)
                stopped = self.terminal_manager.stop(profile.id, profile=profile)
                result[terminal_id] = "MT5 fechado." if stopped else "O MT5 já estava fechado."
            self._emit_terminals()
            self._emit_live_streams()
            return ok(result, "Terminais selecionados fechados.")
        except Exception as exc:
            logger.exception("Erro ao fechar terminais selecionados")
            return fail(str(exc))

    @Slot(result=str)
    def startAllWorkers(self) -> str:
        """Compatibilidade temporária; a GUI atual usa startSelectedWorkers."""
        terminal_ids = [profile.id for profile in self.terminal_registry.list()[: self.max_active_mt5]]
        return self.startSelectedWorkers(json.dumps(terminal_ids))

    @Slot(result=str)
    def stopAllWorkers(self) -> str:
        try:
            for terminal in self.terminal_registry.list():
                self.worker_manager.stop_worker(terminal.id)
            self._emit_terminals()
            self._emit_live_streams()
            return ok(message="Todas as leituras foram encerradas.")
        except Exception as exc:
            logger.exception("Erro ao parar todos os workers")
            return fail(str(exc))

    @Slot(str, result=str)
    def testConnection(self, terminal_id: str) -> str:
        return self.startWorker(terminal_id)

    @Slot(str, result=str)
    def reconnectWorker(self, terminal_id: str) -> str:
        try:
            sent, message = self.worker_manager.request_reconnect(terminal_id)
            return ok(message=message) if sent else fail(message)
        except Exception as exc:
            logger.exception("Erro ao solicitar reconexão")
            return fail(str(exc))

    @Slot(str, result=str)
    def refreshSnapshot(self, terminal_id: str) -> str:
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
        try:
            profile = self.terminal_registry.get(terminal_id)
            if not profile:
                return fail("Terminal do fluxo não encontrado.")
            symbol = self.symbol_registry.get(symbol_id)
            if not symbol or not symbol.enabled:
                return fail("Ativo do fluxo não encontrado ou inativo.")

            if not self.worker_manager.is_running(terminal_id):
                if (
                    not self.terminal_manager.is_running(profile.id, profile)
                    and self._running_mt5_count() >= self.max_active_mt5
                ):
                    return fail(self._activation_limit_message())
                started, start_message = self.worker_manager.start_worker(
                    profile,
                    self.symbol_registry.list(enabled_only=True),
                )
                if not started and not self.worker_manager.is_running(terminal_id):
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
        try:
            _, message = self.worker_manager.clear_live_stream(slot_id)
            self._emit_live_streams()
            return ok(message=message)
        except Exception as exc:
            logger.exception("Erro ao parar fluxo ao vivo")
            return fail(str(exc))

    @Slot(result=str)
    def clearAllLiveStreams(self) -> str:
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
        self.setWindowTitle("EP Market Hub — Base 0.4.9 Clean Handoff")
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
        self._shutdown_done = True
        logger.info("Encerrando EP Market Hub, workers e MT5 controlados...")
        self.worker_poll_timer.stop()
        try:
            self.worker_manager.clear_all_live_streams()
        except Exception:
            logger.exception("Falha ao limpar fluxos durante encerramento")
        try:
            self.worker_manager.stop_all()
        except Exception:
            logger.exception("Falha ao encerrar workers durante encerramento")
        try:
            stopped = self.terminal_manager.stop_all(self.terminal_registry.list())
            logger.info("MT5 controlados encerrados: %s", stopped)
        except Exception:
            logger.exception("Falha ao encerrar MT5 controlados")

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        event.accept()
