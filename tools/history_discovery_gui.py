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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_analytics.history_discovery import (  # noqa: E402
    DiscoveryAsset,
    DiscoveryCancelled,
    DiscoveryProgress,
    HistoryDiscoveryEngine,
    ProbeObservation,
    latest_completed_weekday,
)

DATA_ROOT = Path(r"D:\EPData\MarketHub")
REPORT_DIR = DATA_ROOT / "discovery" / "reports"
CLEAR_EXE = Path(r"C:\Program Files\MetaTrader 5\terminal64.exe")
FLOOR_DATE = date(2000, 1, 1)

ASSETS = (
    DiscoveryAsset(
        logical_id="win",
        symbol="WIN$",
        benchmark_bytes_per_session=7_268_711,
        benchmark_seconds_per_session=27.229,
    ),
    DiscoveryAsset(
        logical_id="wdo",
        symbol="WDO$",
        benchmark_bytes_per_session=869_513,
        benchmark_seconds_per_session=1.416,
    ),
)

STORAGE_LIMIT_BYTES = 50 * 1_000_000_000
DURATION_LIMIT_SECONDS = 48 * 60 * 60


def atomic_write_json(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with partial.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, destination)


class MT5ProbeSource:
    def __init__(self) -> None:
        self.terminal_metadata: dict[str, Any] = {}

    def connect(self) -> None:
        if not CLEAR_EXE.exists():
            raise RuntimeError(f"Terminal Clear não encontrado: {CLEAR_EXE}")
        if not mt5.initialize(path=str(CLEAR_EXE), portable=False, timeout=60_000):
            raise RuntimeError(f"Falha ao conectar ao terminal Clear: {mt5.last_error()}")
        terminal = mt5.terminal_info()
        if terminal is None or not bool(getattr(terminal, "connected", False)):
            mt5.shutdown()
            raise RuntimeError("O terminal Clear está aberto, mas não está conectado à corretora.")
        self.terminal_metadata = {
            "connected": True,
            "build": getattr(terminal, "build", None),
            "company": getattr(terminal, "company", None),
        }
        for asset in ASSETS:
            if mt5.symbol_info(asset.symbol) is None and not mt5.symbol_select(asset.symbol, True):
                mt5.shutdown()
                raise RuntimeError(f"Símbolo histórico não listado pela Clear: {asset.symbol}")

    @staticmethod
    def close() -> None:
        mt5.shutdown()

    def probe(
        self,
        asset: DiscoveryAsset,
        session_date: date,
        start_utc: datetime,
        end_utc: datetime,
        phase: str,
    ) -> ProbeObservation:
        started = time.perf_counter()
        raw = mt5.copy_ticks_range(asset.symbol, start_utc, end_utc, mt5.COPY_TICKS_ALL)
        if raw is None:
            first_error = mt5.last_error()
            mt5.symbol_select(asset.symbol, True)
            time.sleep(0.25)
            raw = mt5.copy_ticks_range(asset.symbol, start_utc, end_utc, mt5.COPY_TICKS_ALL)
            if raw is None:
                code, message = mt5.last_error()
                return ProbeObservation(
                    logical_id=asset.logical_id,
                    symbol=asset.symbol,
                    session_date=session_date,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    phase=phase,
                    status="inconclusive",
                    tick_count=0,
                    elapsed_seconds=round(time.perf_counter() - started, 3),
                    error_code=int(code),
                    error_message=f"{message}; primeira tentativa: {first_error}",
                )

        start_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)
        if len(raw):
            times = raw["time_msc"]
            count = int(((times >= start_ms) & (times < end_ms)).sum())
        else:
            count = 0
        return ProbeObservation(
            logical_id=asset.logical_id,
            symbol=asset.symbol,
            session_date=session_date,
            start_utc=start_utc,
            end_utc=end_utc,
            phase=phase,
            status="available" if count else "empty",
            tick_count=count,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )


class DiscoveryRunner:
    def __init__(self, events: queue.Queue[tuple[str, Any]], cancel_event: threading.Event) -> None:
        self.events = events
        self.cancel_event = cancel_event
        self.source = MT5ProbeSource()

    def emit(self, kind: str, payload: Any) -> None:
        self.events.put((kind, payload))

    def on_progress(self, progress: DiscoveryProgress) -> None:
        self.emit("progress", progress)

    @staticmethod
    def _complete_report(report: dict[str, Any], terminal_metadata: dict[str, Any]) -> dict[str, Any]:
        projections = [asset.get("projection") for asset in report["assets"]]
        complete = all(projection is not None for projection in projections)
        total_bytes = sum(int(projection["estimated_bytes"]) for projection in projections if projection)
        total_seconds = sum(float(projection["estimated_seconds"]) for projection in projections if projection)
        report.update(
            {
                "work_order": "DEV-003/C1",
                "created_at_utc": datetime.now(UTC).isoformat(),
                "source_id": "clear",
                "terminal": terminal_metadata,
                "combined_projection": {
                    "complete": complete,
                    "estimated_bytes": total_bytes if complete else None,
                    "estimated_seconds": round(total_seconds, 1) if complete else None,
                    "storage_limit_bytes": STORAGE_LIMIT_BYTES,
                    "duration_limit_seconds": DURATION_LIMIT_SECONDS,
                    "within_storage_limit": complete and total_bytes <= STORAGE_LIMIT_BYTES,
                    "within_duration_limit": complete and total_seconds <= DURATION_LIMIT_SECONDS,
                    "ready_for_c2_review": complete
                    and total_bytes <= STORAGE_LIMIT_BYTES
                    and total_seconds <= DURATION_LIMIT_SECONDS,
                },
                "notes": [
                    "earliest_observed_session é evidência observada, não início absoluto garantido.",
                    "Probe vazio não identifica sozinho feriado nem indisponibilidade histórica.",
                    "Nenhum tick bruto foi persistido; o relatório contém apenas contagens e tempos.",
                    "Esta execução não autoriza nem inicia o piloto mensal C2.",
                ],
            }
        )
        return report

    def run(self) -> None:
        reference_date = latest_completed_weekday(date.today())
        try:
            self.emit("status", "Conectando ao terminal Clear...")
            self.source.connect()
            self.emit("status", f"Clear conectada. Referência: {reference_date.isoformat()}")
            engine = HistoryDiscoveryEngine(
                assets=ASSETS,
                reference_date=reference_date,
                floor_date=FLOOR_DATE,
                probe=self.source.probe,
                progress=self.on_progress,
                cancelled=self.cancel_event.is_set,
            )
            report = self._complete_report(engine.run(), self.source.terminal_metadata)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            report_path = REPORT_DIR / f"history_discovery_{timestamp}.json"
            atomic_write_json(report_path, report)
            atomic_write_json(REPORT_DIR / "history_discovery_latest.json", report)
            print(str(report_path), flush=True)
            self.emit("done", {"report_path": str(report_path), "report": report})
        except DiscoveryCancelled as exc:
            self.emit("cancelled", str(exc))
        except Exception as exc:
            failure = {
                "work_order": "DEV-003/C1",
                "created_at_utc": datetime.now(UTC).isoformat(),
                "state": "failed",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            failure_path = REPORT_DIR / f"history_discovery_error_{timestamp}.json"
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
            self.source.close()


class DiscoveryWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("EP Market Hub — Portão C / Descoberta C1")
        self.root.geometry("820x560")
        self.root.minsize(720, 480)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = True
        self.probes_done = 0

        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="Descoberta histórica B3", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            container,
            text="WIN$ e WDO$ • probes de 5 minutos • nenhum tick bruto será salvo",
        ).pack(anchor="w", pady=(2, 14))

        self.status = ttk.Label(container, text="Preparando...", font=("Segoe UI", 10, "bold"))
        self.status.pack(anchor="w")
        self.detail = ttk.Label(container, text="Probes concluídos: 0")
        self.detail.pack(anchor="w", pady=(4, 8))
        self.progress = ttk.Progressbar(container, mode="indeterminate")
        self.progress.pack(fill="x")
        self.progress.start(12)

        self.log = tk.Text(container, height=19, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, pady=(12, 10))
        self.log.tag_configure("available", foreground="#176b2c")
        self.log.tag_configure("empty", foreground="#6b6b6b")
        self.log.tag_configure("inconclusive", foreground="#a33a00")
        self.log.tag_configure("error", foreground="#a00000")

        actions = ttk.Frame(container)
        actions.pack(fill="x")
        self.cancel_button = ttk.Button(actions, text="Cancelar entre probes", command=self.cancel)
        self.cancel_button.pack(side="right")
        self.close_button = ttk.Button(actions, text="Fechar", command=self.root.destroy, state="disabled")
        self.close_button.pack(side="right", padx=(0, 8))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        worker = threading.Thread(
            target=DiscoveryRunner(self.events, self.cancel_event).run,
            name="history-discovery-c1",
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
            self.status.configure(text="Cancelamento solicitado; aguardando o probe atual...")
            self.cancel_button.configure(state="disabled")

    def on_close(self) -> None:
        if not self.running:
            self.root.destroy()
            return
        if messagebox.askyesno(
            "Cancelar descoberta?",
            "A janela permanecerá aberta até o probe atual terminar. Deseja cancelar?",
        ):
            self.cancel()

    def finish(self, status: str) -> None:
        self.running = False
        self.progress.stop()
        self.cancel_button.configure(state="disabled")
        self.close_button.configure(state="normal")
        self.status.configure(text=status)

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
            elif kind == "progress":
                progress: DiscoveryProgress = payload
                self.probes_done = progress.probes_done
                self.status.configure(text=f"{progress.symbol} — {progress.phase}")
                self.detail.configure(
                    text=f"Probes concluídos: {progress.probes_done} • última data: {progress.session_date}"
                )
                self.append_log(
                    f"{progress.symbol}  {progress.session_date}  {progress.status:<12}  "
                    f"{progress.tick_count:,} ticks",
                    progress.status,
                )
            elif kind == "done":
                report = payload["report"]
                self.finish("C1 CONCLUÍDO — aguardando auditoria; C2 não foi iniciado")
                self.append_log("")
                for asset in report["assets"]:
                    self.append_log(
                        f"{asset['symbol']}: {asset['earliest_observed_session']} até "
                        f"{asset['latest_observed_session']} (confiança {asset['confidence']})",
                        "available",
                    )
                self.append_log(f"Relatório: {payload['report_path']}")
                self.beep()
            elif kind == "cancelled":
                self.finish("C1 CANCELADO — nenhum backfill foi iniciado")
                self.append_log(str(payload), "inconclusive")
                self.beep(error=True)
            elif kind == "error":
                self.finish("FALHA NO C1 — a mensagem permanecerá nesta janela")
                self.append_log(payload["message"], "error")
                if payload.get("failure_path"):
                    self.append_log(f"Diagnóstico salvo em: {payload['failure_path']}", "error")
                self.append_log(payload["traceback"], "error")
                self.beep(error=True)
        self.root.after(100, self.poll)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    DiscoveryWindow().run()
