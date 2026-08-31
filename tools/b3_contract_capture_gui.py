from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import UTC, date, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.worker_manager import MT5WorkerManager  # noqa: E402
from market_analytics.backfill_catalog import (  # noqa: E402
    get_session,
    interrupt_session,
    list_running_sessions,
    open_catalog,
)
from market_analytics.backfill_runner import (  # noqa: E402
    BackfillSourceError,
    advance_backfill_job,
    interrupt_backfill_job,
    start_backfill_job,
)
from market_analytics.backfill_writer import discard_partial, inspect_final_file  # noqa: E402
from market_analytics.contract_tracking import empty_tracker, update_contract_tracker  # noqa: E402
from market_analytics.daily_capture import (  # noqa: E402
    DailyCaptureSession,
    build_current_contract_plan,
    latest_closed_b3_session,
)
from market_analytics.pilot_backfill import retryable_failure, summarize_results  # noqa: E402
from market_analytics.tick_backfill import (  # noqa: E402
    BackfillSessionRequest,
    catalog_db_path,
    raw_partition_dir,
)
from market_analytics.tick_diagnostics import TickRecord, TickWindow  # noqa: E402

DATA_ROOT = Path(r"D:\EPData\MarketHub")
REPORT_DIR = DATA_ROOT / "contract_capture" / "reports"
TRACKER_PATH = DATA_ROOT / "contract_capture" / "tracked_contracts.json"
CLEAR_EXE = Path(r"C:\Program Files\MetaTrader 5\terminal64.exe")
OWNER_ID = "dev003-c3-b3-contract-capture"
CHUNK_SECONDS = 15 * 60
MAX_ATTEMPTS = 3
B3_ZONE = ZoneInfo("America/Sao_Paulo")


class CaptureCancelled(RuntimeError):
    pass


class CaptureFailure(RuntimeError):
    pass


def determine_exit_code(kind: str, payload: Any) -> int:
    """Decide o exit code do processo a partir do evento terminal da captura.

    Extraída de ``CaptureWindow.poll`` para ser testável sem uma janela Tk
    real: 0 apenas para ``"done"`` sem issues críticas em
    ``report["issues"]``; ``"cancelled"``, ``"error"`` ou um ``"done"`` com
    issues são sempre 1 — sucesso real é o único caminho para 0.
    """

    if kind == "done":
        issues = payload["report"].get("issues") or []
        return 1 if issues else 0
    return 1


def scheduled_close_delay_ms(has_issues: bool) -> int:
    """Atraso (ms) até a janela agendada se fechar sozinha após um "done".

    Extraída de ``CaptureWindow.poll`` pela mesma razão de
    ``determine_exit_code``: um "done" com issues críticas usa o mesmo
    prazo de inspeção de 2 minutos de um ``"error"`` — nunca os 30s de um
    sucesso real, que dariam pouco tempo para notar o problema antes de a
    janela fechar sozinha.
    """

    return 120_000 if has_issues else 30_000


def atomic_write_json(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, destination)


def load_tracker() -> dict[str, Any]:
    if not TRACKER_PATH.exists():
        return empty_tracker()
    try:
        with TRACKER_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureFailure(f"Registro de contratos não pôde ser lido: {exc}") from exc
    if not isinstance(payload, dict):
        raise CaptureFailure("Registro de contratos não é um objeto JSON.")
    return payload


def _symbol_states() -> dict[str, dict[str, Any]]:
    rows = mt5.symbols_get() or []
    disabled_mode = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(getattr(row, "name", "") or "")
        if not name:
            continue
        trade_mode = getattr(row, "trade_mode", None)
        bid = getattr(row, "bid", None)
        ask = getattr(row, "ask", None)
        last = getattr(row, "last", None)
        result[name] = {
            "tradable": trade_mode is None or trade_mode != disabled_mode,
            "has_quote": any(value not in (None, 0) for value in (bid, ask, last)),
            "expiration_time": getattr(row, "expiration_time", None),
            "session_volume": getattr(row, "session_volume", None),
            "session_deals": getattr(row, "session_deals", None),
        }
    return result


class ContractCaptureRunner:
    def __init__(self, events: queue.Queue[tuple[str, Any]], cancel_event: threading.Event) -> None:
        self.events = events
        self.cancel_event = cancel_event
        self.process = psutil.Process(os.getpid())
        self.owner_started_at = self.process.create_time()
        self.peak_rss = self.process.memory_info().rss
        self.results: list[dict[str, Any]] = []
        self.plan: tuple[DailyCaptureSession, ...] = ()
        self.skipped_contracts: list[dict[str, Any]] = []
        # Issues estruturadas e não-fatais: instrumento sem candidato atual,
        # contrato rastreado que sumiu antes da sessão de vencimento, ou
        # seleção recusada pela corretora. Nunca impedem a captura dos
        # demais instrumentos, mas tornam a execução um "sucesso com
        # ressalvas" — ver CaptureWindow.exit_code.
        self.issues: list[dict[str, Any]] = []
        self.active_job: dict[str, Any] | None = None

    def emit(self, kind: str, payload: Any) -> None:
        self.events.put((kind, payload))

    @staticmethod
    def catalog_confirmed_symbols(conn, tracker: dict[str, Any], session_date: date) -> frozenset[str]:
        """Símbolos cuja sessão pedida já está completed/empty e verificada.

        Evidência vem só do catálogo (nunca inferência): consulta cada
        símbolo já conhecido pelo registro e confirma hash/contagem/tamanho
        via `verify_row`, o mesmo caminho usado para o fast path
        `already_completed`. Sem isso, reexecutar no mesmo dia depois de um
        símbolo já capturado sumir do terminal geraria um falso
        `missing_before_expiration`.
        """

        confirmed: set[str] = set()
        for row in tracker.get("contracts", []):
            symbol = row.get("symbol")
            logical_id = row.get("logical_id")
            if not symbol or not logical_id:
                continue
            catalog_row = get_session(
                conn, source_id="clear", logical_id=logical_id, session_date=session_date
            )
            if catalog_row is None or catalog_row["state"] not in {"completed", "empty"}:
                continue
            try:
                ContractCaptureRunner.verify_row(catalog_row)
            except CaptureFailure:
                continue
            confirmed.add(symbol)
        return frozenset(confirmed)

    def connect_and_plan(self, conn) -> dict[str, Any]:
        if not CLEAR_EXE.exists():
            raise CaptureFailure(f"Terminal Clear não encontrado: {CLEAR_EXE}")
        if not mt5.initialize(path=str(CLEAR_EXE), portable=False, timeout=60_000):
            raise CaptureFailure(f"Falha ao conectar à Clear: {mt5.last_error()}")
        terminal = mt5.terminal_info()
        if terminal is None or not bool(getattr(terminal, "connected", False)):
            raise CaptureFailure("Terminal Clear aberto, mas desconectado da corretora.")

        now_utc = datetime.now(UTC)
        now_local = now_utc.astimezone(B3_ZONE)
        session_date = latest_closed_b3_session(now_local)
        states = _symbol_states()
        active_plan = build_current_contract_plan(
            states,
            now_utc=now_utc,
            session_date=session_date,
        )
        # Ausência de candidato atual para UM instrumento nunca aborta o
        # outro: vira issue crítica visível no relatório, e o instrumento
        # simplesmente fica de fora do plano desta execução.
        found_roots = {item.selection.spec.instrument for item in active_plan}
        for root in sorted({"win", "wdo"} - found_roots):
            self.issues.append(
                {
                    "type": "no_current_candidate",
                    "symbol": None,
                    "instrument": root,
                    "logical_id": None,
                    "session_date": session_date.isoformat(),
                    "message": (
                        f"Nenhum contrato {root.upper()} elegível foi observado nesta execução; "
                        "instrumento pulado sem afetar os demais."
                    ),
                }
            )
            self.emit("log", self.issues[-1]["message"])

        tracker_before = load_tracker()
        confirmed_symbols = self.catalog_confirmed_symbols(conn, tracker_before, session_date)
        try:
            self.plan, tracker, tracker_issues = update_contract_tracker(
                tracker_before,
                active_plan,
                states,
                now_utc=now_utc,
                session_date=session_date,
                catalog_confirmed_symbols=confirmed_symbols,
            )
        except ValueError as exc:
            raise CaptureFailure(f"Registro de contratos incompatível: {exc}") from exc
        for issue in tracker_issues:
            self.issues.append(issue)
            self.emit("log", issue["message"])
        # Persiste a descoberta antes do download: uma queda não pode fazer o
        # sistema esquecer que um novo contrato já entrou em acompanhamento.
        atomic_write_json(TRACKER_PATH, tracker)
        selectable_plan: list[DailyCaptureSession] = []
        for item in self.plan:
            if not mt5.symbol_select(item.selection.spec.symbol, True):
                if item.selection.selection_reason == "tracked_until_expiration":
                    skipped = {
                        **item.to_dict(),
                        "reason": "broker_refused_historical_symbol_selection",
                    }
                    self.skipped_contracts.append(skipped)
                    self.emit(
                        "log",
                        f"Contrato anterior indisponível na fonte: {item.selection.spec.symbol}",
                    )
                    continue
                # A recusa em selecionar o contrato ATUAL de um instrumento
                # também não pode mais abortar o outro: vira issue crítica e
                # esse instrumento fica de fora desta execução.
                skipped = {
                    **item.to_dict(),
                    "reason": "broker_refused_current_symbol_selection",
                }
                self.skipped_contracts.append(skipped)
                self.issues.append(
                    {
                        "type": "symbol_select_refused",
                        "symbol": item.selection.spec.symbol,
                        "instrument": item.selection.spec.instrument,
                        "logical_id": item.selection.spec.logical_id,
                        "session_date": item.session_date.isoformat(),
                        "message": (
                            "A Clear recusou selecionar o contrato atual "
                            f"{item.selection.spec.symbol}; "
                            f"{item.selection.spec.instrument.upper()} pulado nesta execução."
                        ),
                    }
                )
                self.emit("log", self.issues[-1]["message"])
                continue
            selectable_plan.append(item)
            self.emit("planned", item.to_dict())
        self.plan = tuple(selectable_plan)
        return {
            "connected": True,
            "build": getattr(terminal, "build", None),
            "company": getattr(terminal, "company", None),
            "observed_at_utc": now_utc.isoformat(),
        }

    def fetch_records(self, symbol: str, window: TickWindow) -> list[TickRecord]:
        raw = mt5.copy_ticks_range(symbol, window.start_utc, window.end_utc, mt5.COPY_TICKS_ALL)
        if raw is None:
            code, message = mt5.last_error()
            raise BackfillSourceError("mt5_error", f"{symbol}: {code} - {message}", code=code)
        start_ms = int(window.start_utc.timestamp() * 1000)
        end_ms = int(window.end_utc.timestamp() * 1000)
        records: list[TickRecord] = []
        for row in raw:
            time_msc = int(row["time_msc"])
            if start_ms <= time_msc < end_ms:
                records.append(
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
        self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)
        return records

    def recover_dead_sessions(self, conn) -> None:
        for row in list_running_sessions(conn):
            if row.get("owner_terminal_id") != OWNER_ID:
                continue
            owner_alive = MT5WorkerManager._catalog_owner_alive(row)
            if owner_alive is True:
                raise CaptureFailure("Outra captura diária ainda é proprietária de uma sessão.")
            if owner_alive is None:
                raise CaptureFailure("Uma sessão running possui identidade de processo inconclusiva.")
            interrupt_session(
                conn,
                source_id=row["source_id"],
                logical_id=row["logical_id"],
                session_date=row["session_date"],
                attempt_id=row["attempt_id"],
                message="Captura diária: proprietário anterior morreu; sessão liberada.",
            )
            final_path = (
                raw_partition_dir(
                    DATA_ROOT,
                    source_id=row["source_id"],
                    logical_id=row["logical_id"],
                    session_date=row["session_date"],
                )
                / "ticks.parquet"
            )
            discard_partial(final_path.with_name(final_path.name + ".partial"))
            self.emit("log", f"Sessão interrompida recuperada: {row['logical_id']}")

    @staticmethod
    def verify_row(row: dict[str, Any]) -> None:
        path_value = row.get("file_path")
        if not path_value:
            raise CaptureFailure("Sessão terminal sem file_path no catálogo.")
        path = Path(str(path_value))
        inspected = inspect_final_file(path)
        if inspected is None:
            raise CaptureFailure(f"Parquet não encontrado: {path}")
        if inspected.sha256 != row.get("sha256"):
            raise CaptureFailure(f"Hash divergente: {path}")
        if inspected.row_count != int(row.get("tick_count") or 0):
            raise CaptureFailure(f"Contagem divergente: {path}")
        if inspected.size_bytes != int(row.get("file_size_bytes") or 0):
            raise CaptureFailure(f"Tamanho divergente: {path}")

    def result_from_row(
        self,
        row: dict[str, Any],
        session: DailyCaptureSession,
        *,
        reused: bool,
        attempts_used: int,
        elapsed_seconds: float,
    ) -> dict[str, Any]:
        return {
            **session.to_dict(),
            "source_id": "clear",
            "state": row["state"],
            "tick_count": int(row.get("tick_count") or 0),
            "file_path": row.get("file_path"),
            "file_size_bytes": int(row.get("file_size_bytes") or 0),
            "sha256": row.get("sha256"),
            "reused": reused,
            "attempts_used_this_run": attempts_used,
            "elapsed_seconds": round(elapsed_seconds, 3),
        }

    @staticmethod
    def result_from_issue(issue: dict[str, Any]) -> dict[str, Any]:
        """Resultado operacional não-sucedido para uma issue com contrato identificado.

        Garante que uma issue crítica como ``missing_before_expiration``
        apareça em ``results``/``summary`` como uma sessão que falhou — nunca
        apenas como uma linha isolada em ``issues`` fácil de não perceber.
        """

        return {
            "logical_id": issue["logical_id"],
            "symbol": issue["symbol"],
            "session_date": issue["session_date"],
            "provenance": {},
            "selection_evidence": {},
            "source_id": "clear",
            "state": issue["type"],
            "tick_count": 0,
            "file_path": None,
            "file_size_bytes": 0,
            "sha256": None,
            "reused": False,
            "attempts_used_this_run": 0,
            "elapsed_seconds": 0.0,
        }

    def run_session(self, conn, session: DailyCaptureSession, session_index: int) -> dict[str, Any]:
        started = time.perf_counter()
        selection = session.selection
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.cancel_event.is_set():
                raise CaptureCancelled("Captura cancelada antes da próxima tentativa.")
            request = BackfillSessionRequest(
                request_id=(
                    f"b3-contract-{selection.spec.symbol.lower()}-{session.session_date.isoformat()}"
                ),
                source_id="clear",
                logical_id=selection.spec.logical_id,
                aliases=(selection.spec.symbol,),
                tick_type="all",
                session_date=session.session_date,
                chunk_seconds=CHUNK_SECONDS,
                series_metadata=tuple(selection.provenance().items()),
            )
            job, immediate = start_backfill_job(
                conn=conn,
                data_root=DATA_ROOT,
                request=request,
                resolved_symbol=selection.spec.symbol,
                fetch_chunk=lambda window: self.fetch_records(selection.spec.symbol, window),
                owner_pid=os.getpid(),
                owner_process_started_at=self.owner_started_at,
                owner_terminal_id=OWNER_ID,
            )
            if job is None:
                if immediate is not None and immediate.error_reason == "already_completed":
                    row = get_session(
                        conn,
                        source_id="clear",
                        logical_id=selection.spec.logical_id,
                        session_date=session.session_date,
                    )
                    if row is None or row["state"] not in {"completed", "empty"}:
                        raise CaptureFailure("already_completed sem catálogo terminal coerente")
                    self.verify_row(row)
                    return self.result_from_row(
                        row,
                        session,
                        reused=True,
                        attempts_used=attempt,
                        elapsed_seconds=time.perf_counter() - started,
                    )
                reason = immediate.error_reason if immediate else "unknown"
                message = immediate.error_message if immediate else "Falha desconhecida."
                if retryable_failure(reason) and attempt < MAX_ATTEMPTS:
                    self.emit("log", f"Retentativa {selection.spec.symbol}: {message}")
                    if self.cancel_event.wait(1 if attempt == 1 else 3):
                        raise CaptureCancelled("Cancelada durante a espera de retentativa.")
                    continue
                raise CaptureFailure(f"{selection.spec.symbol}: {reason} - {message}")

            self.active_job = job
            chunk_total = len(job["chunks"])
            terminal_result = None
            while True:
                if self.cancel_event.is_set():
                    interrupt_backfill_job(job, "Captura diária cancelada entre chunks.")
                    self.active_job = None
                    raise CaptureCancelled("Sessão corrente interrompida com segurança.")
                outcome, terminal_result = advance_backfill_job(job)
                self.emit(
                    "progress",
                    {
                        "value": session_index * chunk_total + int(job["chunk_index"]),
                        "maximum": len(self.plan) * chunk_total,
                        "symbol": selection.spec.symbol,
                        "chunk": int(job["chunk_index"]),
                        "chunks": chunk_total,
                    },
                )
                if outcome != "progress":
                    break
            self.active_job = None
            if terminal_result is not None and terminal_result.state in {"completed", "empty"}:
                row = get_session(
                    conn,
                    source_id="clear",
                    logical_id=selection.spec.logical_id,
                    session_date=session.session_date,
                )
                if row is None:
                    raise CaptureFailure("Sessão concluída sem catálogo.")
                self.verify_row(row)
                return self.result_from_row(
                    row,
                    session,
                    reused=False,
                    attempts_used=attempt,
                    elapsed_seconds=time.perf_counter() - started,
                )
            reason = terminal_result.error_reason if terminal_result else "unknown"
            message = terminal_result.error_message if terminal_result else "Resultado ausente."
            if retryable_failure(reason) and attempt < MAX_ATTEMPTS:
                self.emit("log", f"Retentativa {selection.spec.symbol}: {message}")
                continue
            raise CaptureFailure(f"{selection.spec.symbol}: {reason} - {message}")
        raise AssertionError("laço de tentativas terminou sem resultado")

    def run(self) -> None:
        started_at = datetime.now(UTC)
        conn = None
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            conn = open_catalog(catalog_db_path(DATA_ROOT))
            self.recover_dead_sessions(conn)
            self.emit("status", "Conectando à Clear e selecionando contratos...")
            terminal = self.connect_and_plan(conn)
            # Uma issue "missing_before_expiration" identifica um contrato e
            # uma sessão específicos: vira também um resultado operacional
            # não-sucedido, visível em results/summary, não só em issues.
            for issue in self.issues:
                if issue["type"] == "missing_before_expiration":
                    self.results.append(self.result_from_issue(issue))
            for index, session in enumerate(self.plan):
                self.emit(
                    "status",
                    f"Coletando {session.selection.spec.symbol} — {session.session_date.isoformat()}",
                )
                result = self.run_session(conn, session, index)
                self.results.append(result)
                self.emit("result", result)

            ended_at = datetime.now(UTC)
            summary = summarize_results(self.results)
            report = {
                "schema": "ep_market_hub.b3_contract_capture",
                "schema_version": 1,
                "work_order": "DEV-003/C3-contract-foundation",
                "source_id": "clear",
                "started_at_utc": started_at.isoformat(),
                "ended_at_utc": ended_at.isoformat(),
                "elapsed_seconds": round((ended_at - started_at).total_seconds(), 3),
                "peak_process_rss_mb": round(self.peak_rss / (1024 * 1024), 2),
                "terminal": terminal,
                "plan": [item.to_dict() for item in self.plan],
                "skipped_contracts": self.skipped_contracts,
                "issues": self.issues,
                "tracker_path": str(TRACKER_PATH),
                "results": self.results,
                "summary": summary,
                "policy": {
                    "raw_truth": "individual_contract",
                    "continuous_series": "historical_reference_or_derived",
                    "same_day_after": "19:00 America/Sao_Paulo",
                    "roll_selection": "broker_expiration_then_observed_session_liquidity",
                },
            }
            timestamp = ended_at.strftime("%Y%m%dT%H%M%SZ")
            report_path = REPORT_DIR / f"b3_contract_capture_{timestamp}.json"
            atomic_write_json(report_path, report)
            atomic_write_json(REPORT_DIR / "b3_contract_capture_latest.json", report)
            self.emit("done", {"report_path": str(report_path), "report": report})
        except CaptureCancelled as exc:
            self.emit("cancelled", str(exc))
        except Exception as exc:
            if self.active_job is not None:
                try:
                    interrupt_backfill_job(
                        self.active_job,
                        "Captura interrompida após falha inesperada do controlador.",
                    )
                except Exception:
                    pass
                self.active_job = None
            self.emit("error", {"message": str(exc), "traceback": traceback.format_exc()})
        finally:
            mt5.shutdown()
            if conn is not None:
                conn.close()


class CaptureWindow:
    def __init__(self, *, scheduled: bool = False) -> None:
        self.scheduled = scheduled
        self.root = tk.Tk()
        self.root.title("EP Market Hub — Captura diária dos contratos B3")
        self.root.geometry("820x540")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        # Pessimista por padrão: só vira 0 diante de um "done" sem issues.
        # Cancelamento, erro e uma janela nunca iniciada nunca são 0 — o
        # Agendador do Windows só deve ver sucesso quando a captura de fato
        # terminou sem ressalvas.
        self.exit_code = 1

        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Contratos reais B3", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="WIN e WDO • preço bruto • sem ajuste • arquivo separado por contrato",
        ).pack(anchor="w", pady=(2, 14))

        self.status = ttk.Label(frame, text="Pronto para selecionar os contratos atuais.")
        self.status.pack(anchor="w")
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.pack(fill="x", pady=(8, 12))

        self.table = ttk.Treeview(
            frame,
            columns=("contract", "session", "state", "ticks"),
            show="headings",
            height=5,
        )
        for key, label, width in (
            ("contract", "Contrato", 140),
            ("session", "Sessão", 130),
            ("state", "Estado", 150),
            ("ticks", "Ticks", 150),
        ):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor="center")
        self.table.pack(fill="x")

        self.log = tk.Text(frame, height=12, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(12, 10))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="Iniciar captura", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Cancelar", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Fechar", command=self.close).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)
        if self.scheduled:
            self.root.after(750, self.start)

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.cancel_event.clear()
        self.table.delete(*self.table.get_children())
        self.progress.configure(value=0, maximum=1)
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        runner = ContractCaptureRunner(self.events, self.cancel_event)
        self.thread = threading.Thread(target=runner.run, daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.status.configure(text="Cancelamento solicitado; aguardando o fim do chunk...")

    def finish_ui(self) -> None:
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")

    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status.configure(text=str(payload))
                    self.append_log(str(payload))
                elif kind == "planned":
                    self.table.insert(
                        "",
                        "end",
                        iid=payload["logical_id"],
                        values=(payload["symbol"], payload["session_date"], "planejado", "—"),
                    )
                    evidence = payload["selection_evidence"]
                    self.append_log(
                        f"Selecionado {payload['symbol']}: {evidence['selection_reason']} "
                        f"(volume={evidence['session_volume']}, negócios={evidence['session_deals']})"
                    )
                elif kind == "progress":
                    self.progress.configure(maximum=payload["maximum"], value=payload["value"])
                    self.status.configure(
                        text=f"{payload['symbol']} — chunk {payload['chunk']}/{payload['chunks']}"
                    )
                elif kind == "result":
                    iid = payload["logical_id"]
                    self.table.item(
                        iid,
                        values=(
                            payload["symbol"],
                            payload["session_date"],
                            "reutilizado" if payload["reused"] else payload["state"],
                            f"{payload['tick_count']:,}".replace(",", "."),
                        ),
                    )
                    self.append_log(
                        f"{payload['symbol']}: {payload['state']} — {payload['tick_count']:,} ticks"
                    )
                elif kind == "log":
                    self.append_log(str(payload))
                elif kind == "done":
                    self.finish_ui()
                    issues = payload["report"].get("issues") or []
                    # Sucesso real (exit code 0) exige zero issues críticas;
                    # "concluído com ressalvas" ainda é uma falha do ponto de
                    # vista do Agendador, mesmo sem exceção.
                    self.exit_code = determine_exit_code(kind, payload)
                    if issues:
                        self.status.configure(
                            text=f"Captura concluída com {len(issues)} issue(s) — revisar relatório."
                        )
                    else:
                        self.status.configure(text="Captura concluída e auditada.")
                    self.progress.configure(value=self.progress["maximum"])
                    self.root.bell()
                    if self.scheduled:
                        delay_ms = scheduled_close_delay_ms(bool(issues))
                        if issues:
                            self.append_log(
                                "A janela permanecerá aberta por 2 minutos para inspeção das issues."
                            )
                        else:
                            self.append_log("Execução automática encerrará esta janela em 30 segundos.")
                        self.root.after(delay_ms, self.root.destroy)
                    elif issues:
                        messagebox.showwarning(
                            "Captura concluída com issues",
                            "Contratos B3 processados, mas com issues que exigem atenção.\n\n"
                            f"Relatório: {payload['report_path']}",
                        )
                    else:
                        messagebox.showinfo(
                            "Captura concluída",
                            "Contratos B3 arquivados com sucesso.\n\n"
                            f"Relatório: {payload['report_path']}",
                        )
                elif kind == "cancelled":
                    self.exit_code = determine_exit_code(kind, payload)
                    self.finish_ui()
                    self.status.configure(text=str(payload))
                    self.root.bell()
                    if self.scheduled:
                        self.append_log("Execução automática cancelada; esta janela encerrará em 30 segundos.")
                        self.root.after(30_000, self.root.destroy)
                elif kind == "error":
                    self.exit_code = determine_exit_code(kind, payload)
                    self.finish_ui()
                    self.status.configure(text="Falha na captura.")
                    self.append_log(payload["traceback"])
                    self.root.bell()
                    if self.scheduled:
                        self.append_log("A janela permanecerá aberta por 2 minutos para inspeção.")
                        self.root.after(120_000, self.root.destroy)
                    else:
                        messagebox.showerror("Falha na captura", payload["message"])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            if not messagebox.askyesno(
                "Captura em andamento",
                "Solicitar cancelamento e fechar quando o chunk atual terminar?",
            ):
                return
            self.cancel_event.set()
            self.root.after(250, self._close_when_done)
            return
        self.root.destroy()

    def _close_when_done(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.root.after(250, self._close_when_done)
        else:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    window = CaptureWindow(scheduled="--scheduled" in sys.argv[1:])
    window.run()
    # O lançador PowerShell aguarda este processo e repassa o exit code ao
    # Agendador do Windows: 0 só em sucesso real, sem issues críticas.
    sys.exit(window.exit_code)
