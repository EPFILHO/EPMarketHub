"""GUI Tkinter genérica de backfill orientado por manifesto (DEV-004 — C3.1).

Abre **somente** um manifesto já registrado/aprovado em
`market_analytics.manifest_registry` (nunca um arquivo arbitrário escolhido
pelo usuário) — e nunca há campo livre de símbolo, fonte ou data. Para um
manifesto em `planning` (o único aprovado nesta ordem, `c3-win-clear`), a
janela mostra com destaque **PLANEJAMENTO — EXECUÇÃO BLOQUEADA** e só permite
gerar/reabrir o plano com fonte e catálogo abstratos/falsos; o botão de
execução real fica desabilitado por uma regra pura (`execute_button_state`),
testável sem instanciar nenhuma janela.

Reabrir um plano salvo (`reopen_plan`) sempre revalida contra o manifesto
atualmente carregado com `assert_plan_matches_manifest` -- a mesma validação
completa (metadados, cada item contra os ativos autorizados, intervalo
resolvido e dias candidatos) que `run_plan` aplica antes de tocar o
adaptador -- antes de exibir ou permitir execução; um relatório de outro
manifesto, de uma versão antiga do mesmo manifesto, ou adulterado item a
item, é sempre recusado (DEV-004, 2ª auditoria Codex).

Esta ordem não conecta nem abre MT5/Clear/FOT, não lê/grava `D:\\EPData` e não
inicia nenhum backfill real — `run_plan` (`market_analytics.backfill_adapter`)
só é chamado se algum dia existir um manifesto `execution_approved` E um
adaptador real registrado; nenhum dos dois existe nesta entrega (ver
`ADAPTER_FACTORY` abaixo). A lógica pura deste módulo é extraída em funções de
nível de módulo (sem `tk.Tk()`) para ser testável num ambiente sem display —
mesmo padrão de `tools/b3_contract_capture_gui.py`.
"""

from __future__ import annotations

import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_analytics.backfill_adapter import (  # noqa: E402
    QueueItemResult,
    SourceAdapter,
    is_executed_result,
    remaining_item_count,
    run_plan,
)
from market_analytics.backfill_plan import (  # noqa: E402
    BackfillPlan,
    CatalogSnapshot,
    PlanBuildError,
    assert_plan_matches_manifest,
    build_plan,
    estimate_eta_seconds,
    load_plan,
    save_plan_atomic,
)
from market_analytics.manifest import BackfillManifest, ManifestValidationError  # noqa: E402
from market_analytics.manifest_registry import (  # noqa: E402
    ManifestRegistryIntegrityError,
    known_manifest_ids,
    load_known_manifest,
)

REPORT_DIR = Path(tempfile.gettempdir()) / "ep_market_hub_manifest_backfill_reports"

# Nenhum adaptador real existe nesta ordem (DEV-004): C3.3 registrará aqui a
# fábrica que constrói um `SourceAdapter` contra o worker/catálogo reais.
# Enquanto `None`, o botão de execução nunca chama `run_plan`, mesmo que um
# manifesto futuro chegue `execution_approved`.
ADAPTER_FACTORY: Callable[[BackfillManifest], SourceAdapter] | None = None

# Estimativas ilustrativas por sessão, só para exibição do plano em
# `planning` (nunca uma medição real desta ordem). Derivadas da projeção já
# auditada do C1 para `WIN$` em `docs/work_orders/DEV-003.md`
# (~2.064.313.924 bytes / 7.733s para 284 sessões): 7.269.415 bytes e
# 27,23s por sessão. Um `logical_id` sem entrada aqui é reportado como
# ausente pela GUI em vez de usar um valor inventado.
ILLUSTRATIVE_BENCHMARKS: dict[str, tuple[float, float]] = {
    "win": (7_269_415.0, 27.229),
}


def open_known_manifest(manifest_id: str) -> BackfillManifest:
    """Único ponto de carga: só um `manifest_id` já registrado/aprovado em
    `manifest_registry` -- nunca um caminho de arquivo arbitrário escolhido
    pelo usuário (DEV-004, auditoria Codex)."""

    return load_known_manifest(manifest_id)


def planning_banner_text(manifest: BackfillManifest) -> str:
    """Texto de destaque do estado de autorização — regra pura e testável."""

    if manifest.authorization_state == "planning":
        return "PLANEJAMENTO — EXECUÇÃO BLOQUEADA"
    if manifest.authorization_state == "preflight_approved":
        return "PREFLIGHT APROVADO — EXECUÇÃO AINDA BLOQUEADA"
    return "EXECUÇÃO APROVADA"


def execute_button_state(manifest: BackfillManifest) -> str:
    """Único ponto que decide se o botão de execução pode ficar habilitado.

    Consulta apenas `BackfillManifest.is_execution_authorized` — nunca
    reimplementa a regra de autorização. Além disso, sem `ADAPTER_FACTORY`
    registrado (o caso desta ordem), o botão permanece desabilitado mesmo
    para um manifesto hipotético `execution_approved`: não há para onde
    executar.
    """

    if not manifest.is_execution_authorized:
        return "disabled"
    return "normal" if ADAPTER_FACTORY is not None else "disabled"


def build_fake_catalog_snapshot(_manifest: BackfillManifest) -> CatalogSnapshot:
    """Catálogo abstrato/falso: nenhuma sessão é considerada conhecida.

    Esta ordem proíbe ler o catálogo real (`D:\\EPData`); todo item do plano
    em `planning` aparece como `planned`. C3.3 substituirá esta função por
    uma leitura real de `market_analytics.backfill_catalog`.
    """

    return {}


def missing_benchmark_logical_ids(manifest: BackfillManifest) -> list[str]:
    """`logical_id`s do manifesto sem estimativa ilustrativa cadastrada."""

    return [asset.logical_id for asset in manifest.assets if asset.logical_id not in ILLUSTRATIVE_BENCHMARKS]


def generate_plan(manifest: BackfillManifest, *, now_utc: datetime | None = None) -> BackfillPlan:
    """Gera o plano exibido pela GUI, sempre com catálogo abstrato/falso.

    Levanta `ManifestValidationError` se algum ativo não tiver estimativa
    ilustrativa cadastrada — a GUI nunca inventa um número.
    """

    missing = missing_benchmark_logical_ids(manifest)
    if missing:
        raise ManifestValidationError(f"sem estimativa ilustrativa cadastrada para: {sorted(missing)}")
    bytes_map = {logical_id: value[0] for logical_id, value in ILLUSTRATIVE_BENCHMARKS.items()}
    seconds_map = {logical_id: value[1] for logical_id, value in ILLUSTRATIVE_BENCHMARKS.items()}
    return build_plan(
        manifest,
        now_utc=now_utc or datetime.now(UTC),
        catalog_snapshot=build_fake_catalog_snapshot(manifest),
        benchmark_bytes_per_session=bytes_map,
        benchmark_seconds_per_session=seconds_map,
    )


def reopen_plan(path: Path, manifest: BackfillManifest, *, now_utc: datetime | None = None) -> BackfillPlan:
    """Carrega um relatório de plano gravado e o revalida contra o manifesto
    atualmente carregado antes de reexibi-lo ou permitir execução.

    Usa `assert_plan_matches_manifest` -- exatamente a mesma validação
    completa que `run_plan` aplica antes de tocar o adaptador (metadados de
    nível superior, `resolved_end_date` conforme a política de data final,
    cada item contra os ativos autorizados, intervalo resolvido, dias
    candidatos e a ordem da grade autorizada) -- para que a GUI nunca reexiba
    nem permita executar um plano fora do escopo atual do manifesto (DEV-004,
    3ª auditoria Codex). `now_utc` é injetável/determinístico para testes; o
    default (`None`) usa a hora real no momento da chamada.
    """

    plan = load_plan(path)
    assert_plan_matches_manifest(plan, manifest, now_utc=now_utc)
    return plan


def format_summary_lines(manifest: BackfillManifest, plan: BackfillPlan) -> list[str]:
    """Linhas de resumo mostradas na janela — fonte, manifesto, fingerprint, estado."""

    assets_text = ", ".join(f"{asset.logical_id} ({asset.requested_symbol})" for asset in manifest.assets)
    return [
        f"Fonte: {manifest.source_id}",
        f"Manifesto: {manifest.display_name} [{manifest.manifest_id}]",
        f"Work order: {manifest.work_order}",
        f"Fingerprint: {plan.manifest_fingerprint}",
        f"Estado de autorização: {manifest.authorization_state}",
        f"Ativos autorizados: {assets_text}",
        f"Intervalo resolvido: {plan.resolved_start_date.isoformat()} → {plan.resolved_end_date.isoformat()}",
        f"Ordem: {plan.execution_order} • chunk: {plan.chunk_seconds}s",
        (
            f"Sessões: {plan.totals['sessions_total']} total • "
            f"{plan.totals['reusable']} reutilizáveis • "
            f"{plan.totals['pending']} pendentes • "
            f"{plan.totals['blocked']} bloqueadas • "
            f"{plan.totals['blocked_by_limit']} bloqueadas por limite • "
            f"{plan.totals['planned']} planejadas"
        ),
        (
            f"Estimativa restante: {plan.estimated_bytes_remaining / (1024 * 1024):.1f} MB • "
            f"{plan.estimated_seconds_remaining:.0f}s"
        ),
    ]


def format_queue_rows(plan: BackfillPlan) -> list[tuple[str, str, str, str]]:
    """Linhas `(logical_id, sessão, status, estado_catálogo)` para a fila da GUI."""

    return [
        (item.logical_id, item.session_date.isoformat(), item.status, item.catalog_state or "-")
        for item in plan.items
    ]


def progress_speed_eta_text(
    *, completed_count: int, elapsed_seconds: float, remaining_count: int, chunk_seconds: float
) -> str:
    """Texto de velocidade/ETA — nunca divide por zero (usa `estimate_eta_seconds`)."""

    eta = estimate_eta_seconds(
        completed_count=completed_count, elapsed_seconds=elapsed_seconds, remaining_count=remaining_count
    )
    if completed_count <= 0 or elapsed_seconds <= 0:
        speed_text = "velocidade desconhecida"
    else:
        speed_text = f"{completed_count / elapsed_seconds:.3f} sessões/s"
    eta_text = "desconhecido" if eta is None else f"{eta:.0f}s"
    return f"{speed_text} • ETA: {eta_text}"


def write_plan_report(report_dir: Path, plan: BackfillPlan) -> Path:
    """Grava o plano atômico e devolve o caminho — mesmo relatório que a GUI expõe."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(report_dir) / f"{plan.manifest_id}_{timestamp}.json"
    save_plan_atomic(path, plan)
    latest_path = Path(report_dir) / f"{plan.manifest_id}_latest.json"
    save_plan_atomic(latest_path, plan)
    return path


class ManifestBackfillWindow:
    """Janela única: escolher manifesto registrado, gerar/reabrir plano, fila e
    (se autorizado) execução.

    Não instanciada em teste automatizado (exige display Tk real); toda
    lógica de decisão vive nas funções puras acima, cobertas diretamente.
    """

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("EP Market Hub — Backfill genérico por manifesto")
        self.root.geometry("880x600")
        self.root.minsize(760, 500)

        self.manifest: BackfillManifest | None = None
        self.plan: BackfillPlan | None = None
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.execution_results: list[QueueItemResult] = []

        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)

        self.banner = ttk.Label(container, text="Nenhum manifesto aberto.", font=("Segoe UI", 13, "bold"))
        self.banner.pack(anchor="w")

        picker = ttk.Frame(container)
        picker.pack(fill="x", pady=(4, 8))
        ttk.Label(picker, text="Manifesto registrado:").pack(side="left")
        self.manifest_choice = tk.StringVar()
        self.manifest_picker = ttk.Combobox(
            picker, textvariable=self.manifest_choice, values=known_manifest_ids(), state="readonly"
        )
        self.manifest_picker.pack(side="left", padx=(8, 0))
        if known_manifest_ids():
            self.manifest_picker.current(0)

        self.summary = tk.Text(container, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        self.summary.pack(fill="x", pady=(0, 8))

        self.queue_list = tk.Listbox(container, font=("Consolas", 9))
        self.queue_list.pack(fill="both", expand=True, pady=(0, 8))

        self.status = ttk.Label(container, text="")
        self.status.pack(anchor="w")

        self.log = tk.Text(container, height=6, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="x", pady=(8, 8))

        actions = ttk.Frame(container)
        actions.pack(fill="x")
        ttk.Button(actions, text="Abrir manifesto", command=self.on_open_manifest).pack(side="left")
        self.plan_button = ttk.Button(
            actions, text="Gerar plano", command=self.on_generate_plan, state="disabled"
        )
        self.plan_button.pack(side="left", padx=(8, 0))
        self.reopen_plan_button = ttk.Button(
            actions, text="Reabrir plano...", command=self.on_reopen_plan, state="disabled"
        )
        self.reopen_plan_button.pack(side="left", padx=(8, 0))
        self.execute_button = ttk.Button(
            actions, text="Executar (bloqueado)", command=self.on_execute, state="disabled"
        )
        self.execute_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(actions, text="Cancelar", command=self.on_cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))

        self.root.after(100, self.poll)

    def append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_open_manifest(self) -> None:
        manifest_id = self.manifest_choice.get().strip()
        if not manifest_id:
            return
        try:
            manifest = open_known_manifest(manifest_id)
        except (ManifestValidationError, KeyError, ManifestRegistryIntegrityError) as exc:
            messagebox.showerror("Manifesto inválido", str(exc))
            return
        self.manifest = manifest
        self.plan = None
        self.banner.configure(text=planning_banner_text(manifest))
        self.plan_button.configure(state="normal")
        self.reopen_plan_button.configure(state="normal")
        self.execute_button.configure(state="disabled", text="Executar (bloqueado)")
        self.queue_list.delete(0, "end")
        self._set_summary_text([f"Manifesto carregado: {manifest.manifest_id}"])
        self.append_log(f"Manifesto aberto: {manifest.manifest_id} ({manifest.authorization_state})")

    def _show_plan(self, plan: BackfillPlan) -> None:
        assert self.manifest is not None
        self.plan = plan
        self._set_summary_text(format_summary_lines(self.manifest, plan))
        self.queue_list.delete(0, "end")
        for logical_id, session_date, status, catalog_state in format_queue_rows(plan):
            self.queue_list.insert("end", f"{session_date}  {logical_id:<8} {status:<17} catálogo={catalog_state}")
        state = execute_button_state(self.manifest)
        self.execute_button.configure(
            state=state, text="Executar" if state == "normal" else "Executar (bloqueado)"
        )

    def on_generate_plan(self) -> None:
        if self.manifest is None:
            return
        try:
            plan = generate_plan(self.manifest)
        except (ManifestValidationError, PlanBuildError) as exc:
            messagebox.showerror("Não foi possível gerar o plano", str(exc))
            return
        self._show_plan(plan)
        report_path = write_plan_report(REPORT_DIR, plan)
        self.append_log(f"Plano gerado com catálogo abstrato/falso. Relatório: {report_path}")

    def on_reopen_plan(self) -> None:
        if self.manifest is None:
            return
        selected = filedialog.askopenfilename(
            title="Reabrir plano salvo",
            initialdir=str(REPORT_DIR) if REPORT_DIR.exists() else str(ROOT),
            filetypes=[("Relatório de plano (JSON)", "*.json")],
        )
        if not selected:
            return
        try:
            plan = reopen_plan(Path(selected), self.manifest)
        except (PlanBuildError, ManifestValidationError) as exc:
            messagebox.showerror("Plano incompatível com o manifesto atual", str(exc))
            return
        self._show_plan(plan)
        self.append_log(f"Plano reaberto e revalidado contra o manifesto atual: {selected}")

    def on_execute(self) -> None:
        if self.manifest is None or self.plan is None:
            return
        if execute_button_state(self.manifest) != "normal":
            return
        assert ADAPTER_FACTORY is not None
        adapter = ADAPTER_FACTORY(self.manifest)
        self.cancel_event.clear()
        self.running = True
        self.execution_results = []
        self.cancel_button.configure(state="normal")
        worker = threading.Thread(target=self._run_execution, args=(adapter,), daemon=True)
        worker.start()

    def _run_execution(self, adapter: SourceAdapter) -> None:
        assert self.plan is not None
        assert self.manifest is not None
        started = time.perf_counter()

        def _progress(result: QueueItemResult) -> None:
            self.events.put(("progress", (result, time.perf_counter() - started)))

        try:
            results = run_plan(self.plan, self.manifest, adapter, cancelled=self.cancel_event.is_set, progress=_progress)
            self.events.put(("done", results))
        except Exception as exc:
            # Inclui `ManifestNotExecutionAuthorizedError`/`PlanManifestMismatchError`
            # e qualquer falha inesperada do adaptador: nunca um crash silencioso da
            # thread, sempre um evento "error" explícito para a janela.
            self.events.put(("error", {"message": str(exc), "traceback": traceback.format_exc()}))

    def on_cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status.configure(text="Cancelamento solicitado; aguardando a sessão atual...")

    def _set_summary_text(self, lines: list[str]) -> None:
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "\n".join(lines))
        self.summary.configure(state="disabled")

    def poll(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                result, elapsed = payload
                self.execution_results.append(result)
                assert self.plan is not None
                remaining = remaining_item_count(self.plan, self.execution_results)
                # Só trabalho de fato executado conta para velocidade/ETA --
                # um item "reusable" nunca chamou o adaptador e não pode
                # inflar o ritmo observado (DEV-004, 3ª auditoria Codex).
                executed_count = sum(1 for item in self.execution_results if is_executed_result(item))
                speed_eta = progress_speed_eta_text(
                    completed_count=executed_count,
                    elapsed_seconds=elapsed,
                    remaining_count=remaining,
                    chunk_seconds=self.plan.chunk_seconds,
                )
                self.append_log(f"{result.session_date} {result.logical_id}: {result.outcome.state}")
                self.status.configure(
                    text=f"{result.session_date} {result.logical_id}: {result.outcome.state} • {speed_eta}"
                )
            elif kind == "done":
                self.running = False
                self.cancel_button.configure(state="disabled")
                self.status.configure(text="Execução concluída.")
            elif kind == "error":
                self.running = False
                self.cancel_button.configure(state="disabled")
                self.status.configure(text="Execução interrompida por erro.")
                self.append_log(f"ERRO: {payload['message']}")
                messagebox.showerror("Falha na execução", payload["message"])
        self.root.after(100, self.poll)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    ManifestBackfillWindow().run()
