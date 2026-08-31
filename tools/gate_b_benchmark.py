from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tkinter import ttk
from typing import Any

import MetaTrader5 as mt5
import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_analytics.backfill_catalog import get_session, open_catalog  # noqa: E402
from market_analytics.backfill_runner import (  # noqa: E402
    BackfillSourceError,
    advance_backfill_job,
    start_backfill_job,
)
from market_analytics.backfill_writer import (  # noqa: E402
    SessionTickWriter,
    build_schema,
    inspect_final_file,
)
from market_analytics.tick_backfill import (  # noqa: E402
    BACKFILL_COLLECTOR_VERSION,
    BackfillSessionRequest,
    catalog_db_path,
)
from market_analytics.tick_diagnostics import (  # noqa: E402
    TickRecord,
    TickWindow,
    TickWindowAccumulator,
)

DATA_ROOT = Path(r"D:\EPData\MarketHub")
SESSION_DATE = date(2026, 8, 28)
SAMPLE_START_UTC = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)
SAMPLE_END_UTC = SAMPLE_START_UTC + timedelta(hours=1)
CHUNK_SECONDS = 15 * 60

CLEAR_EXE = Path(r"C:\Program Files\MetaTrader 5\terminal64.exe")
FOT_EXE = Path(r"C:\Program Files\Fot Limited MT5 Terminal\terminal64.exe")

B3_ASSETS = (("win", "WIN$"), ("wdo", "WDO$"))
FOT_ASSETS = (("nasdaq100", "US100Cash"), ("gold", "GOLD"), ("eurusd", "EURUSD"))


class BenchmarkError(RuntimeError):
    pass


class GateBBenchmark:
    def __init__(self, events: queue.Queue[tuple[str, Any]]) -> None:
        self.events = events
        self.process = psutil.Process(os.getpid())
        self.peak_rss = self.process.memory_info().rss
        self.done_chunks = 0
        self.total_chunks = 2 * 96 + 3 * 4
        self.results: list[dict[str, Any]] = []

    def emit(self, kind: str, payload: Any) -> None:
        self.events.put((kind, payload))

    def log(self, message: str) -> None:
        self.emit("log", message)

    def progress(self, message: str) -> None:
        self.done_chunks += 1
        self.emit(
            "progress",
            {
                "value": self.done_chunks,
                "maximum": self.total_chunks,
                "message": message,
            },
        )

    def update_peak(self) -> None:
        self.peak_rss = max(self.peak_rss, self.process.memory_info().rss)

    @staticmethod
    def initialize_terminal(label: str, executable: Path) -> None:
        if not executable.exists():
            raise BenchmarkError(f"{label}: terminal não encontrado em {executable}")
        if not mt5.initialize(path=str(executable), portable=False, timeout=60_000):
            raise BenchmarkError(f"{label}: falha ao conectar ao MT5: {mt5.last_error()}")
        terminal = mt5.terminal_info()
        if terminal is None or not bool(getattr(terminal, "connected", False)):
            mt5.shutdown()
            raise BenchmarkError(f"{label}: terminal aberto, mas desconectado da corretora")

    def fetch_records(self, symbol: str, window: TickWindow) -> list[TickRecord]:
        started = time.perf_counter()
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
        self.log(f"{symbol}: {len(records):,} ticks em {time.perf_counter() - started:.2f}s")
        return records

    def run_b3_asset(self, conn, logical_id: str, symbol: str) -> None:
        request = BackfillSessionRequest(
            request_id=f"gate-b-clear-{logical_id}-{SESSION_DATE.isoformat()}",
            source_id="clear",
            logical_id=logical_id,
            aliases=(symbol,),
            tick_type="all",
            session_date=SESSION_DATE,
            session_timezone="America/Sao_Paulo",
            chunk_seconds=CHUNK_SECONDS,
        )
        self.log(f"Iniciando sessão completa Clear/{symbol} ({SESSION_DATE})")
        start_rss = self.process.memory_info().rss
        started = time.perf_counter()
        job, immediate = start_backfill_job(
            conn=conn,
            data_root=DATA_ROOT,
            request=request,
            resolved_symbol=symbol,
            fetch_chunk=lambda window: self.fetch_records(symbol, window),
        )
        if job is None:
            if immediate is None or immediate.error_reason != "already_completed":
                raise BenchmarkError(
                    f"Clear/{symbol}: {getattr(immediate, 'error_reason', 'falha')} - "
                    f"{getattr(immediate, 'error_message', '')}"
                )
            row = get_session(
                conn,
                source_id="clear",
                logical_id=logical_id,
                session_date=SESSION_DATE,
            )
            if row is None:
                raise BenchmarkError(f"Clear/{symbol}: catálogo não encontrado após already_completed")
            self.results.append(self._result_from_catalog(row, symbol=symbol, market="B3"))
            self.done_chunks += 96
            self.emit("progress", {"value": self.done_chunks, "maximum": self.total_chunks, "message": symbol})
            return

        terminal_result = None
        while True:
            outcome, terminal_result = advance_backfill_job(job)
            self.progress(f"Clear/{symbol}: chunk {job['chunk_index']}/{len(job['chunks'])}")
            if outcome != "progress":
                break
        if terminal_result is None or terminal_result.state not in {"completed", "empty"}:
            raise BenchmarkError(
                f"Clear/{symbol}: {getattr(terminal_result, 'error_reason', 'falha')} - "
                f"{getattr(terminal_result, 'error_message', '')}"
            )
        if terminal_result.promoted is None or terminal_result.summary is None:
            raise BenchmarkError(f"Clear/{symbol}: resultado terminal incompleto")
        inspected = inspect_final_file(terminal_result.promoted.path)
        if inspected is None or inspected.sha256 != terminal_result.promoted.sha256:
            raise BenchmarkError(f"Clear/{symbol}: verificação do Parquet/hash falhou")
        if inspected.row_count != terminal_result.summary.total_count:
            raise BenchmarkError(f"Clear/{symbol}: contagem do Parquet diverge do resumo")

        elapsed = time.perf_counter() - started
        self.results.append(
            self._result_from_summary(
                source_id="clear",
                logical_id=logical_id,
                symbol=symbol,
                market="B3",
                interval_start=request.session_window().start_utc,
                interval_end=request.session_window().end_utc,
                summary=terminal_result.summary.to_dict(),
                path=terminal_result.promoted.path,
                size_bytes=terminal_result.promoted.size_bytes,
                sha256=terminal_result.promoted.sha256,
                elapsed_seconds=elapsed,
                peak_delta_bytes=max(0, self.peak_rss - start_rss),
            )
        )

    def run_fot_sample(self, logical_id: str, symbol: str) -> None:
        self.log(f"Iniciando amostra FOT/{symbol} (1 hora)")
        destination = (
            DATA_ROOT
            / "benchmark"
            / "fot"
            / logical_id
            / f"session_date={SESSION_DATE.isoformat()}"
            / "sample_1600_1700_utc"
            / "ticks.parquet"
        )
        metadata = {
            "schema": "ep_market_hub.raw_ticks.sample",
            "schema_version": "1",
            "source_id": "fot",
            "logical_id": logical_id,
            "resolved_symbol": symbol,
            "sample_start_utc": SAMPLE_START_UTC.isoformat(),
            "sample_end_utc": SAMPLE_END_UTC.isoformat(),
            "tick_type": "all",
            "collected_at_utc": datetime.now(UTC).isoformat(),
            "collector_version": f"{BACKFILL_COLLECTOR_VERSION}-gate-b-sample",
        }
        schema = build_schema(metadata=metadata)
        window = TickWindow(SAMPLE_START_UTC, SAMPLE_END_UTC)
        chunks = window.chunks(CHUNK_SECONDS)
        accumulator = TickWindowAccumulator()
        start_rss = self.process.memory_info().rss
        started = time.perf_counter()

        if destination.exists():
            inspected = inspect_final_file(destination)
            if inspected is None:
                raise BenchmarkError(f"FOT/{symbol}: arquivo existente não pôde ser inspecionado")
            self.done_chunks += len(chunks)
            self.emit("progress", {"value": self.done_chunks, "maximum": self.total_chunks, "message": symbol})
            self.results.append(
                {
                    "source_id": "fot",
                    "logical_id": logical_id,
                    "symbol": symbol,
                    "market": "external",
                    "interval_start_utc": SAMPLE_START_UTC.isoformat(),
                    "interval_end_utc": SAMPLE_END_UTC.isoformat(),
                    "tick_count": inspected.row_count,
                    "file_path": str(destination),
                    "file_size_bytes": inspected.size_bytes,
                    "sha256": inspected.sha256,
                    "reused": True,
                }
            )
            return

        writer = SessionTickWriter(destination, schema=schema)
        writer.open()
        try:
            for index, chunk in enumerate(chunks, start=1):
                records = self.fetch_records(symbol, chunk)
                for record in records:
                    accumulator.consume(record)
                writer.write_chunk(records)
                self.progress(f"FOT/{symbol}: chunk {index}/{len(chunks)}")
            promoted = writer.close_and_promote()
        except Exception:
            writer.abort()
            raise

        summary = accumulator.finalize(
            window=window,
            request_id=f"gate-b-fot-{logical_id}",
            pid=os.getpid(),
            source_id="fot",
            logical_id=logical_id,
            resolved_symbol=symbol,
            tick_type="all",
        )
        inspected = inspect_final_file(promoted.path)
        if inspected is None or inspected.sha256 != promoted.sha256 or inspected.row_count != summary.total_count:
            raise BenchmarkError(f"FOT/{symbol}: verificação final do Parquet falhou")

        self.results.append(
            self._result_from_summary(
                source_id="fot",
                logical_id=logical_id,
                symbol=symbol,
                market="external",
                interval_start=SAMPLE_START_UTC,
                interval_end=SAMPLE_END_UTC,
                summary=summary.to_dict(),
                path=promoted.path,
                size_bytes=promoted.size_bytes,
                sha256=promoted.sha256,
                elapsed_seconds=time.perf_counter() - started,
                peak_delta_bytes=max(0, self.peak_rss - start_rss),
            )
        )

    @staticmethod
    def _result_from_summary(
        *,
        source_id: str,
        logical_id: str,
        symbol: str,
        market: str,
        interval_start: datetime,
        interval_end: datetime,
        summary: dict[str, Any],
        path: Path,
        size_bytes: int,
        sha256: str,
        elapsed_seconds: float,
        peak_delta_bytes: int,
    ) -> dict[str, Any]:
        count = int(summary["total_count"])
        return {
            "source_id": source_id,
            "logical_id": logical_id,
            "symbol": symbol,
            "market": market,
            "interval_start_utc": interval_start.isoformat(),
            "interval_end_utc": interval_end.isoformat(),
            "tick_count": count,
            "non_zero_counts": summary["non_zero_counts"],
            "flags_histogram": summary["flags_histogram"],
            "out_of_order_count": summary["out_of_order_count"],
            "exact_duplicate_count": summary["exact_duplicate_count"],
            "time_msc_tie_count": summary["time_msc_tie_count"],
            "file_path": str(path),
            "file_size_bytes": size_bytes,
            "bytes_per_tick": round(size_bytes / count, 3) if count else None,
            "sha256": sha256,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "peak_rss_delta_mb": round(peak_delta_bytes / (1024 * 1024), 2),
            "reused": False,
        }

    @staticmethod
    def _result_from_catalog(row: dict[str, Any], *, symbol: str, market: str) -> dict[str, Any]:
        count = int(row.get("tick_count") or 0)
        size = int(row.get("file_size_bytes") or 0)
        return {
            "source_id": row["source_id"],
            "logical_id": row["logical_id"],
            "symbol": symbol,
            "market": market,
            "interval_start_utc": row.get("requested_start_utc"),
            "interval_end_utc": row.get("requested_end_utc"),
            "tick_count": count,
            "non_zero_counts": row.get("non_zero_counts"),
            "flags_histogram": row.get("flags_histogram"),
            "file_path": row.get("file_path"),
            "file_size_bytes": size,
            "bytes_per_tick": round(size / count, 3) if count else None,
            "sha256": row.get("sha256"),
            "reused": True,
        }

    @staticmethod
    def _projection(result: dict[str, Any]) -> dict[str, Any]:
        size = int(result.get("file_size_bytes") or 0)
        if result["market"] == "B3":
            annual = size * 252
            assumption = "252 sessões/ano a partir de uma sessão completa"
        else:
            hours_per_session = 24 if result["logical_id"] == "eurusd" else 23
            annual = size * hours_per_session * 260
            assumption = (
                f"{hours_per_session}h x 260 sessões/ano extrapoladas de uma hora líquida; "
                "estimativa conservadora"
            )
        return {
            "assumption": assumption,
            "one_year_bytes": annual,
            "three_years_bytes": annual * 3,
            "five_years_bytes": annual * 5,
        }

    def write_report(self) -> Path:
        for result in self.results:
            result["projection"] = self._projection(result)
        report = {
            "benchmark": "DEV-002 Gate B",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "session_date": SESSION_DATE.isoformat(),
            "sample_window_utc": [SAMPLE_START_UTC.isoformat(), SAMPLE_END_UTC.isoformat()],
            "peak_process_rss_mb": round(self.peak_rss / (1024 * 1024), 2),
            "results": self.results,
            "notes": [
                "B3 usa uma sessão civil completa; FOT usa somente uma hora líquida aprovada.",
                "Projeções FOT extrapolam uma hora líquida e tendem a superestimar horas menos ativas.",
                "Volume externo ausente não é interpretado como volume centralizado.",
            ],
        }
        report_dir = DATA_ROOT / "benchmark" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"gate_b_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        self._atomic_write_text(report_path, payload + "\n")
        latest = report_dir / "gate_b_latest.json"
        self._atomic_write_text(latest, payload + "\n")
        return report_path

    @staticmethod
    def _atomic_write_text(destination: Path, payload: str) -> None:
        partial = destination.with_suffix(destination.suffix + ".partial")
        with partial.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, destination)

    def run(self) -> None:
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            conn = open_catalog(catalog_db_path(DATA_ROOT))
            try:
                self.initialize_terminal("Clear", CLEAR_EXE)
                try:
                    for logical_id, symbol in B3_ASSETS:
                        self.run_b3_asset(conn, logical_id, symbol)
                finally:
                    mt5.shutdown()
            finally:
                conn.close()

            self.initialize_terminal("FOT", FOT_EXE)
            try:
                for logical_id, symbol in FOT_ASSETS:
                    self.run_fot_sample(logical_id, symbol)
            finally:
                mt5.shutdown()

            report_path = self.write_report()
            self.emit("done", {"report": str(report_path), "results": len(self.results)})
        except Exception as exc:
            try:
                mt5.shutdown()
            except Exception:
                pass
            self.emit("error", {"message": str(exc), "traceback": traceback.format_exc()})


class ProgressWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("EP Market Hub — Benchmark Gate B")
        self.root.geometry("720x440")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = True

        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Benchmark pequeno — WIN/WDO + FOT", font=("Segoe UI", 15, "bold")).pack(
            anchor="w"
        )
        self.status = ttk.Label(frame, text="Preparando...")
        self.status.pack(anchor="w", pady=(8, 8))
        self.bar = ttk.Progressbar(frame, maximum=204, mode="determinate")
        self.bar.pack(fill="x")
        self.counter = ttk.Label(frame, text="0 / 204 chunks")
        self.counter.pack(anchor="e", pady=(3, 8))
        self.log_widget = tk.Text(frame, height=17, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_widget.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll)

    def append_log(self, message: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", message + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def on_close(self) -> None:
        if self.running:
            self.append_log("A coleta ainda está em execução; aguarde o aviso final.")
            return
        self.root.destroy()

    def poll(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(str(payload))
            elif kind == "progress":
                self.bar["maximum"] = payload["maximum"]
                self.bar["value"] = payload["value"]
                self.status.configure(text=payload["message"])
                self.counter.configure(text=f"{payload['value']} / {payload['maximum']} chunks")
            elif kind == "done":
                self.running = False
                self.bar["value"] = self.bar["maximum"]
                self.status.configure(text="CONCLUÍDO — relatório verificado")
                self.append_log(f"Concluído. Relatório: {payload['report']}")
                try:
                    import winsound

                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                except Exception:
                    self.root.bell()
                self.root.after(30_000, self.root.destroy)
            elif kind == "error":
                self.running = False
                self.status.configure(text="FALHA — veja a última mensagem")
                self.append_log(payload["message"])
                self.append_log(payload["traceback"])
                try:
                    import winsound

                    winsound.MessageBeep(winsound.MB_ICONHAND)
                except Exception:
                    self.root.bell()
                self.root.after(60_000, self.root.destroy)
        self.root.after(100, self.poll)

    def run(self) -> None:
        worker = threading.Thread(target=GateBBenchmark(self.events).run, daemon=True)
        worker.start()
        self.root.mainloop()


if __name__ == "__main__":
    ProgressWindow().run()
