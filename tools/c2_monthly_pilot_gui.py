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
from market_analytics.pilot_backfill import (  # noqa: E402
    PilotAsset,
    PilotSession,
    build_pilot_queue,
    retryable_failure,
    summarize_results,
)
from market_analytics.tick_backfill import (  # noqa: E402
    BackfillSessionRequest,
    catalog_db_path,
    raw_partition_dir,
)
from market_analytics.tick_diagnostics import TickRecord, TickWindow  # noqa: E402

DATA_ROOT = Path(r"D:\EPData\MarketHub")
REPORT_DIR = DATA_ROOT / "pilot" / "reports"
CLEAR_EXE = Path(r"C:\Program Files\MetaTrader 5\terminal64.exe")
PILOT_START = date(2026, 8, 1)
PILOT_END = date(2026, 8, 28)
PILOT_OWNER_ID = "dev003-c2-clear-pilot"
CHUNK_SECONDS = 15 * 60
CHUNKS_PER_DAY = 96
MAX_ATTEMPTS = 3
ASSETS = (PilotAsset("win", "WIN$"), PilotAsset("wdo", "WDO$"))
PILOT_QUEUE = tuple(build_pilot_queue(ASSETS, PILOT_START, PILOT_END))


class PilotCancelled(RuntimeError):
    pass


class PilotFailure(RuntimeError):
    pass


def atomic_write_json(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, destination)


class C2PilotRunner:
    def __init__(self, events: queue.Queue[tuple[str, Any]], cancel_event: threading.Event) -> None:
        self.events = events
        self.cancel_event = cancel_event
        self.process = psutil.Process(os.getpid())
        self.owner_started_at = self.process.create_time()
        self.peak_rss = self.process.memory_info().rss
        self.results: list[dict[str, Any]] = []
        self.sessions_finished = 0
        self.active_job: dict[str, Any] | None = None

    def emit(self, kind: str, payload: Any) -> None:
        self.events.put((kind, payload))

    def update_peak(self) -> None:
        self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)

    @staticmethod
    def connect_clear() -> dict[str, Any]:
        if not CLEAR_EXE.exists():
            raise PilotFailure(f"Terminal Clear não encontrado: {CLEAR_EXE}")
        if not mt5.initialize(path=str(CLEAR_EXE), portable=False, timeout=60_000):
            raise PilotFailure(f"Falha ao conectar à Clear: {mt5.last_error()}")
        terminal = mt5.terminal_info()
        if terminal is None or not bool(getattr(terminal, "connected", False)):
            mt5.shutdown()
            raise PilotFailure("Terminal Clear aberto, mas desconectado da corretora.")
        for asset in ASSETS:
            if mt5.symbol_info(asset.symbol) is None and not mt5.symbol_select(asset.symbol, True):
                mt5.shutdown()
                raise PilotFailure(f"Símbolo não listado pela Clear: {asset.symbol}")
        return {
            "connected": True,
            "build": getattr(terminal, "build", None),
            "company": getattr(terminal, "company", None),
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
        self.update_peak()
        return records

    def recover_dead_pilot_sessions(self, conn) -> None:
        for row in list_running_sessions(conn):
            if row.get("owner_terminal_id") != PILOT_OWNER_ID:
                continue
            owner_alive = MT5WorkerManager._catalog_owner_alive(row)
            if owner_alive is True:
                raise PilotFailure(
                    "Já existe outra instância viva do piloto C2 proprietária de uma sessão."
                )
            if owner_alive is None:
                raise PilotFailure(
                    "Existe uma sessão C2 running cuja identidade de processo não pôde ser verificada."
                )
            interrupt_session(
                conn,
                source_id=row["source_id"],
                logical_id=row["logical_id"],
                session_date=row["session_date"],
                attempt_id=row["attempt_id"],
                message="Retomada C2: processo proprietário anterior morreu.",
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
            self.emit(
                "log",
                f"Sessão órfã recuperada: {row['logical_id']} {row['session_date']}",
            )

    @staticmethod
    def verify_catalog_artifact(row: dict[str, Any]) -> None:
        path_value = row.get("file_path")
        if not path_value:
            raise PilotFailure("Sessão terminal sem file_path no catálogo.")
        inspected = inspect_final_file(Path(path_value))
        if inspected is None:
            raise PilotFailure(f"Parquet não encontrado: {path_value}")
        if inspected.sha256 != row.get("sha256"):
            raise PilotFailure(f"Hash divergente: {path_value}")
        if inspected.row_count != int(row.get("tick_count") or 0):
            raise PilotFailure(f"Contagem divergente: {path_value}")
        if inspected.size_bytes != int(row.get("file_size_bytes") or 0):
            raise PilotFailure(f"Tamanho divergente: {path_value}")

    @staticmethod
    def result_from_row(
        row: dict[str, Any], session: PilotSession, *, reused: bool, elapsed: float, attempts_used: int
    ) -> dict[str, Any]:
        return {
            "source_id": row["source_id"],
            "logical_id": row["logical_id"],
            "symbol": session.asset.symbol,
            "session_date": session.session_date.isoformat(),
            "state": row["state"],
            "tick_count": int(row.get("tick_count") or 0),
            "file_path": row.get("file_path"),
            "file_size_bytes": int(row.get("file_size_bytes") or 0),
            "sha256": row.get("sha256"),
            "catalog_attempts": int(row.get("attempts") or 0),
            "attempts_used_this_run": attempts_used,
            "elapsed_seconds": round(elapsed, 3),
            "reused": reused,
        }

    def session_progress(self, session: PilotSession, chunk_index: int, attempt: int) -> None:
        overall = self.sessions_finished * CHUNKS_PER_DAY + min(chunk_index, CHUNKS_PER_DAY)
        self.emit(
            "progress",
            {
                "value": overall,
                "maximum": len(PILOT_QUEUE) * CHUNKS_PER_DAY,
                "sessions_finished": self.sessions_finished,
                "sessions_total": len(PILOT_QUEUE),
                "symbol": session.asset.symbol,
                "session_date": session.session_date.isoformat(),
                "chunk_index": chunk_index,
                "chunk_count": CHUNKS_PER_DAY,
                "attempt": attempt,
            },
        )

    def finish_session_progress(self, session: PilotSession) -> None:
        self.sessions_finished += 1
        self.session_progress(session, 0, 1)

    def run_session(self, conn, session: PilotSession) -> dict[str, Any]:
        started = time.perf_counter()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.cancel_event.is_set():
                raise PilotCancelled("Piloto cancelado antes da próxima sessão.")
            request = BackfillSessionRequest(
                request_id=f"dev003-c2-clear-{session.asset.logical_id}-{session.session_date.isoformat()}",
                source_id="clear",
                logical_id=session.asset.logical_id,
                aliases=(session.asset.symbol,),
                tick_type="all",
                session_date=session.session_date,
                session_timezone="America/Sao_Paulo",
                chunk_seconds=CHUNK_SECONDS,
            )
            job, immediate = start_backfill_job(
                conn=conn,
                data_root=DATA_ROOT,
                request=request,
                resolved_symbol=session.asset.symbol,
                fetch_chunk=lambda window, symbol=session.asset.symbol: self.fetch_records(symbol, window),
                owner_pid=os.getpid(),
                owner_process_started_at=self.owner_started_at,
                owner_terminal_id=PILOT_OWNER_ID,
            )
            if job is None:
                if immediate is not None and immediate.error_reason == "already_completed":
                    row = get_session(
                        conn,
                        source_id="clear",
                        logical_id=session.asset.logical_id,
                        session_date=session.session_date,
                    )
                    if row is None or row["state"] not in {"completed", "empty"}:
                        raise PilotFailure("already_completed sem linha terminal coerente no catálogo")
                    self.verify_catalog_artifact(row)
                    self.finish_session_progress(session)
                    return self.result_from_row(
                        row,
                        session,
                        reused=True,
                        elapsed=time.perf_counter() - started,
                        attempts_used=attempt,
                    )
                reason = immediate.error_reason if immediate else "unknown"
                message = immediate.error_message if immediate else "Falha desconhecida ao iniciar sessão."
                if retryable_failure(reason) and attempt < MAX_ATTEMPTS:
                    self.emit("retry", f"{session.asset.symbol} {session.session_date}: {message}")
                    if self.cancel_event.wait(1 if attempt == 1 else 3):
                        raise PilotCancelled("Piloto cancelado durante espera de retentativa.")
                    continue
                raise PilotFailure(f"{session.asset.symbol} {session.session_date}: {reason} - {message}")

            self.active_job = job
            terminal_result = None
            while True:
                if self.cancel_event.is_set():
                    interrupt_backfill_job(job, "Piloto C2 cancelado pelo usuário entre chunks.")
                    self.active_job = None
                    raise PilotCancelled("Piloto cancelado; a sessão corrente foi marcada como interrupted.")
                outcome, terminal_result = advance_backfill_job(job)
                self.session_progress(session, int(job["chunk_index"]), attempt)
                if outcome != "progress":
                    break
            self.active_job = None
            if terminal_result is not None and terminal_result.state in {"completed", "empty"}:
                row = get_session(
                    conn,
                    source_id="clear",
                    logical_id=session.asset.logical_id,
                    session_date=session.session_date,
                )
                if row is None:
                    raise PilotFailure("Sessão concluída sem linha no catálogo.")
                self.verify_catalog_artifact(row)
                self.finish_session_progress(session)
                return self.result_from_row(
                    row,
                    session,
                    reused=False,
                    elapsed=time.perf_counter() - started,
                    attempts_used=attempt,
                )

            reason = terminal_result.error_reason if terminal_result else "unknown"
            message = terminal_result.error_message if terminal_result else "Resultado terminal ausente."
            if retryable_failure(reason) and attempt < MAX_ATTEMPTS:
                self.emit("retry", f"{session.asset.symbol} {session.session_date}: {message}")
                if self.cancel_event.wait(1 if attempt == 1 else 3):
                    raise PilotCancelled("Piloto cancelado durante espera de retentativa.")
                continue
            raise PilotFailure(f"{session.asset.symbol} {session.session_date}: {reason} - {message}")
        raise AssertionError("laço de tentativas terminou sem resultado")

    def build_report(self, terminal_metadata: dict[str, Any], started_at: datetime) -> dict[str, Any]:
        summary = summarize_results(self.results)
        ended_at = datetime.now(UTC)
        return {
            "schema": "ep_market_hub.monthly_backfill_pilot",
            "schema_version": 1,
            "work_order": "DEV-003/C2",
            "source_id": "clear",
            "pilot_start": PILOT_START.isoformat(),
            "pilot_end": PILOT_END.isoformat(),
            "started_at_utc": started_at.isoformat(),
            "ended_at_utc": ended_at.isoformat(),
            "elapsed_seconds": round((ended_at - started_at).total_seconds(), 3),
            "peak_process_rss_mb": round(self.peak_rss / (1024 * 1024), 2),
            "terminal": terminal_metadata,
            "queue": [session.to_dict() for session in PILOT_QUEUE],
            "results": self.results,
            "summary": summary,
            "ready_for_c3_review": summary["totals"]["failed"] == 0
            and summary["totals"]["sessions"] == len(PILOT_QUEUE),
            "notes": [
                "Dias candidatos são segunda–sexta; feriado factual pode resultar em sessão empty.",
                "Nenhum tick bruto atravessou GUI, fila ou relatório.",
                "O dia 2026-08-28 previamente concluído é validado e reutilizado.",
                "Este piloto não inicia nem autoriza o C3.",
            ],
        }

    def run(self) -> None:
        started_at = datetime.now(UTC)
        conn = None
        terminal_metadata: dict[str, Any] = {}
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            conn = open_catalog(catalog_db_path(DATA_ROOT))
            self.recover_dead_pilot_sessions(conn)
            self.emit("status", "Conectando ao terminal Clear...")
            terminal_metadata = self.connect_clear()
            for index, session in enumerate(PILOT_QUEUE, start=1):
                self.emit(
                    "session_start",
                    {
                        "index": index,
                        "total": len(PILOT_QUEUE),
                        "symbol": session.asset.symbol,
                        "session_date": session.session_date.isoformat(),
                    },
                )
                result = self.run_session(conn, session)
                self.results.append(result)
                self.emit("session_done", result)

            report = self.build_report(terminal_metadata, started_at)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            report_path = REPORT_DIR / f"c2_monthly_pilot_{timestamp}.json"
            atomic_write_json(report_path, report)
            atomic_write_json(REPORT_DIR / "c2_monthly_pilot_latest.json", report)
            print(str(report_path), flush=True)
            self.emit("done", {"report_path": str(report_path), "report": report})
        except PilotCancelled as exc:
            self.emit("cancelled", str(exc))
        except Exception as exc:
            if self.active_job is not None:
                try:
                    interrupt_backfill_job(
                        self.active_job,
                        "Piloto C2 interrompido após falha inesperada do controlador.",
                    )
                except Exception:
                    pass
                self.active_job = None
            failure = {
                "schema": "ep_market_hub.monthly_backfill_pilot.failure",
                "schema_version": 1,
                "work_order": "DEV-003/C2",
                "state": "failed",
                "created_at_utc": datetime.now(UTC).isoformat(),
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "completed_results": self.results,
                "summary": summarize_results(self.results),
            }
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            failure_path = REPORT_DIR / f"c2_monthly_pilot_error_{timestamp}.json"
            try:
                atomic_write_json(failure_path, failure)
            except Exception:
                failure_path = None
            self.emit(
                "error",
                {
                    "message": str(exc),
                    "traceback": failure["traceback"],
                    "failure_path": str(failure_path) if failure_path else None,
                },
            )
        finally:
            mt5.shutdown()
            if conn is not None:
                conn.close()


class PilotWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("EP Market Hub — Portão C / Piloto C2")
        self.root.geometry("860x590")
        self.root.minsize(760, 500)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = True

        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="Piloto mensal B3 — C2", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            container,
            text="WIN$ e WDO$ • 03/08/2026 a 28/08/2026 • 40 sessões • C3 bloqueado",
        ).pack(anchor="w", pady=(2, 14))
        self.status = ttk.Label(container, text="Preparando...", font=("Segoe UI", 10, "bold"))
        self.status.pack(anchor="w")
        self.detail = ttk.Label(container, text="0/40 sessões")
        self.detail.pack(anchor="w", pady=(4, 8))
        self.progress = ttk.Progressbar(
            container,
            mode="determinate",
            maximum=len(PILOT_QUEUE) * CHUNKS_PER_DAY,
        )
        self.progress.pack(fill="x")

        self.log = tk.Text(container, height=20, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, pady=(12, 10))
        self.log.tag_configure("ok", foreground="#176b2c")
        self.log.tag_configure("retry", foreground="#a35a00")
        self.log.tag_configure("error", foreground="#a00000")

        actions = ttk.Frame(container)
        actions.pack(fill="x")
        self.cancel_button = ttk.Button(actions, text="Cancelar entre chunks", command=self.cancel)
        self.cancel_button.pack(side="right")
        self.close_button = ttk.Button(actions, text="Fechar", command=self.root.destroy, state="disabled")
        self.close_button.pack(side="right", padx=(0, 8))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        worker = threading.Thread(
            target=C2PilotRunner(self.events, self.cancel_event).run,
            name="c2-monthly-pilot",
            daemon=True,
        )
        worker.start()
        self.root.after(100, self.poll)

    def append_log(self, message: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def cancel(self) -> None:
        if self.running:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.configure(text="Cancelamento solicitado; aguardando o chunk atual...")

    def on_close(self) -> None:
        if not self.running:
            self.root.destroy()
            return
        if messagebox.askyesno(
            "Cancelar piloto?",
            "A sessão atual será interrompida após o chunk em andamento. Deseja cancelar?",
        ):
            self.cancel()

    def finish(self, message: str) -> None:
        self.running = False
        self.cancel_button.configure(state="disabled")
        self.close_button.configure(state="normal")
        self.status.configure(text=message)

    def beep(self, error: bool = False) -> None:
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND if error else winsound.MB_ICONASTERISK)
        except Exception:
            self.root.bell()

    def poll(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.configure(text=str(payload))
                self.append_log(str(payload))
            elif kind == "log":
                self.append_log(str(payload))
            elif kind == "session_start":
                self.status.configure(text=f"{payload['symbol']} — {payload['session_date']}")
                self.append_log(
                    f"[{payload['index']:02d}/{payload['total']:02d}] "
                    f"{payload['symbol']} {payload['session_date']}"
                )
            elif kind == "progress":
                self.progress["value"] = payload["value"]
                self.detail.configure(
                    text=f"{payload['sessions_finished']}/{payload['sessions_total']} sessões • "
                    f"chunk {payload['chunk_index']}/{payload['chunk_count']} • "
                    f"tentativa {payload['attempt']}"
                )
            elif kind == "retry":
                self.append_log(f"Retentativa: {payload}", "retry")
            elif kind == "session_done":
                size_mb = payload["file_size_bytes"] / (1024 * 1024)
                marker = "reutilizada" if payload["reused"] else payload["state"]
                self.append_log(
                    f"  OK — {marker}; {payload['tick_count']:,} ticks; {size_mb:.2f} MB",
                    "ok",
                )
            elif kind == "done":
                report = payload["report"]
                totals = report["summary"]["totals"]
                self.progress["value"] = self.progress["maximum"]
                self.finish("C2 CONCLUÍDO — aguardando auditoria; C3 não foi iniciado")
                self.append_log("")
                self.append_log(
                    f"Concluídas: {totals['completed']} • vazias: {totals['empty']} • "
                    f"reutilizadas: {totals['reused']} • falhas: {totals['failed']}",
                    "ok",
                )
                self.append_log(f"Relatório: {payload['report_path']}")
                self.beep()
            elif kind == "cancelled":
                self.finish("C2 CANCELADO — sessões concluídas foram preservadas")
                self.append_log(str(payload), "retry")
                self.beep(error=True)
            elif kind == "error":
                self.finish("FALHA NO C2 — a mensagem permanecerá nesta janela")
                self.append_log(payload["message"], "error")
                if payload.get("failure_path"):
                    self.append_log(f"Diagnóstico: {payload['failure_path']}", "error")
                self.append_log(payload["traceback"], "error")
                self.beep(error=True)
        self.root.after(100, self.poll)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    PilotWindow().run()
