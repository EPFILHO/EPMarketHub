"""Fronteira de execução futura do backfill genérico (DEV-004 — C3.1).

Este módulo define a interface pequena que o C3.3 vai preencher depois com
um adaptador real (worker Clear/MT5 + `backfill_catalog`/`backfill_runner`
já existentes). Nesta ordem, **nenhum** adaptador real é implementado aqui:
só o contrato (`SourceAdapter`) e o laço puro que consome um `BackfillPlan`
já construído (`run_plan`). Testes usam adaptadores falsos definidos no
próprio arquivo de teste.

Continua puro: não importa `MetaTrader5`, `tkinter` nem Qt, e não toca disco.

`run_plan` NUNCA confia em `plan.execution_authorized` (um campo apenas
informativo, desserializado de um arquivo em disco que poderia ter sido
editado à mão). Em vez disso recebe de volta um `BackfillManifest` já
validado, exige `manifest.is_execution_authorized` e confere o vínculo
plano/manifesto (fingerprint, id, fonte, timezone e chunk) antes de tocar o
adaptador -- auditoria Codex do DEV-004, mesma disciplina de "nunca confiar
em estado desserializado" já aplicada em `BackfillPlan.from_dict`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from .backfill_plan import BackfillPlan, PlanBuildError, PlanItem, assert_plan_matches_manifest
from .manifest import BackfillManifest

# Marca um `QueueItemResult` reaproveitado do catálogo (nunca chamou o
# adaptador). Usado por `run_plan` e por `is_executed_result` -- nenhum dos
# dois duplica a string por conta própria.
REUSED_FROM_CATALOG_DETAIL = "reused_from_catalog_snapshot"

# Vocabulário fechado do resultado de uma sessão processada pelo adaptador.
# "completed"/"empty" também são usados para itens "reusable": `run_plan`
# devolve diretamente o `catalog_state` original do item (nunca força
# "completed" para uma sessão que era "empty" no catálogo -- DEV-004,
# auditoria Codex).
SESSION_OUTCOME_STATES: frozenset[str] = frozenset({"completed", "empty", "failed", "interrupted"})


class ManifestNotExecutionAuthorizedError(RuntimeError):
    """`run_plan` recusa executar quando o manifesto revalidado não está `execution_approved`."""


class PlanManifestMismatchError(RuntimeError):
    """`run_plan` recusa um plano cujo vínculo com o manifesto revalidado não bate.

    Cobre fingerprint, `manifest_id`, `source_id`, `session_timezone` e
    `chunk_seconds` divergentes -- sinal de um plano adulterado ou gerado a
    partir de outro manifesto.
    """


@dataclass(frozen=True)
class SessionOutcome:
    """Resultado estruturado de uma sessão, devolvido pelo adaptador real (C3.3)."""

    state: str
    tick_count: int = 0
    file_size_bytes: int = 0
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.state not in SESSION_OUTCOME_STATES:
            raise ValueError(f"state inválido: {self.state!r}")
        if self.tick_count < 0:
            raise ValueError("tick_count não pode ser negativo")
        if self.file_size_bytes < 0:
            raise ValueError("file_size_bytes não pode ser negativo")


CancelFunction = Callable[[], bool]


class SourceAdapter(Protocol):
    """Interface mínima que o C3.3 implementa contra o worker/catálogo reais.

    Uma única sessão (um `logical_id`/dia) por chamada — o mesmo grão de
    `BackfillSessionRequest` (`tick_backfill.py`). O adaptador é responsável
    por decidir chunking e persistência; `run_plan` só orquestra a fila,
    cancelamento e progresso -- mas fornece tudo que o adaptador precisa para
    isso: a proveniência do ativo (identidade explícita, nunca um símbolo
    livre), `max_attempts` do manifesto, e a MESMA função `cancelled` que
    `run_plan` usa entre itens, para que o adaptador possa checá-la também
    ENTRE CHUNKS dentro de uma única sessão (cancelamento não fica preso
    esperando uma sessão inteira terminar).
    """

    def fetch_session(
        self,
        *,
        logical_id: str,
        requested_symbol: str,
        provenance: Mapping[str, str],
        session_date: date,
        session_timezone: str,
        chunk_seconds: int,
        max_attempts: int,
        cancelled: CancelFunction,
    ) -> SessionOutcome: ...


@dataclass(frozen=True)
class QueueItemResult:
    logical_id: str
    requested_symbol: str
    session_date: date
    outcome: SessionOutcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "requested_symbol": self.requested_symbol,
            "session_date": self.session_date.isoformat(),
            "state": self.outcome.state,
            "tick_count": self.outcome.tick_count,
            "file_size_bytes": self.outcome.file_size_bytes,
            "detail": self.outcome.detail,
        }


ProgressCallback = Callable[[QueueItemResult], None]
ClockFunction = Callable[[], float]


def _assert_plan_matches_manifest(
    plan: BackfillPlan, manifest: BackfillManifest, *, now_utc: datetime | None = None
) -> None:
    """Confere o vínculo *completo* plano/manifesto antes de tocar o adaptador.

    Delega para `backfill_plan.assert_plan_matches_manifest` -- o mesmo
    núcleo puro usado pela GUI ao reabrir um plano (`reopen_plan`), para que
    as duas fronteiras apliquem exatamente a mesma regra (DEV-004, 3ª
    auditoria Codex). Aqui a falha é sempre reembalada como
    `PlanManifestMismatchError` para preservar o contrato público de
    `run_plan`.
    """

    try:
        assert_plan_matches_manifest(plan, manifest, now_utc=now_utc)
    except PlanBuildError as exc:
        raise PlanManifestMismatchError(str(exc)) from exc


def run_plan(
    plan: BackfillPlan,
    manifest: BackfillManifest,
    adapter: SourceAdapter,
    *,
    cancelled: CancelFunction = lambda: False,
    progress: ProgressCallback | None = None,
    clock: ClockFunction = time.monotonic,
    now_utc: datetime | None = None,
) -> list[QueueItemResult]:
    """Consome a fila de `plan` sequencialmente, um item por vez (concurrency=1).

    - `manifest` é revalidado aqui: precisa estar `execution_approved` E bater
      com o vínculo completo do plano (metadados, `resolved_end_date` conforme
      a política de data final, e a sequência dos itens contra a grade
      autorizada) -- nunca se confia em `plan.execution_authorized`
      desserializado sozinho. `now_utc` (injetável/determinístico; default
      `datetime.now(UTC)`) só importa para manifestos `latest_closed`;
    - itens `reusable` nunca chamam o adaptador: são registrados reaproveitados
      preservando o `catalog_state` original (`completed` continua
      `completed`, `empty` continua `empty` -- nunca rebaixado nem promovido);
    - itens `blocked` (outra tentativa dona da sessão agora) e
      `blocked_by_limit` (excederia `max_total_bytes`/`max_total_duration_seconds`)
      são pulados sem alterar nada e sem interromper o restante da fila;
    - além do bloqueio estático do plano, o consumo real (bytes de cada
      `SessionOutcome` e tempo decorrido por `clock`) é acompanhado em tempo de
      execução: antes de iniciar qualquer novo item, `run_plan` recusa
      continuar se o consumido já alcançou os limites do manifesto -- os
      limites nunca são apenas exibidos, também são impostos aqui;
    - cancelamento é checado entre itens (nunca no meio de uma sessão pelo
      próprio `run_plan`), mas a MESMA função `cancelled` é repassada ao
      adaptador em cada chamada, para que ele possa parar entre chunks dentro
      de uma sessão;
    - uma falha (`outcome.state == "failed"`) interrompe o lote de forma
      conservadora, preservando os resultados já obtidos (mesma política do
      C2/C3 documentada em DEV-003).
    """

    if not manifest.is_execution_authorized:
        raise ManifestNotExecutionAuthorizedError(
            "O manifesto revalidado não está execution_approved; nenhuma execução real é permitida (DEV-004)."
        )
    _assert_plan_matches_manifest(plan, manifest, now_utc=now_utc)

    provenance_by_logical_id = {asset.logical_id: asset.provenance_dict() for asset in manifest.assets}

    results: list[QueueItemResult] = []
    started = clock()
    consumed_bytes = 0
    for item in plan.items:
        if item.status == "reusable":
            # `PlanItem.__post_init__` já garante catalog_state em
            # {"completed", "empty"} para status "reusable" -- nunca None.
            assert item.catalog_state is not None
            outcome = SessionOutcome(state=item.catalog_state, detail=REUSED_FROM_CATALOG_DETAIL)
            result = QueueItemResult(item.logical_id, item.requested_symbol, item.session_date, outcome)
            results.append(result)
            if progress is not None:
                progress(result)
            continue
        if item.status in ("blocked", "blocked_by_limit"):
            continue
        if cancelled():
            break
        elapsed = clock() - started
        if consumed_bytes >= manifest.max_total_bytes or elapsed >= manifest.max_total_duration_seconds:
            # Recusa iniciar mais um item: o consumo real já alcançou o teto
            # do manifesto (mesmo que o plano não tivesse previsto isso).
            break

        outcome = adapter.fetch_session(
            logical_id=item.logical_id,
            requested_symbol=item.requested_symbol,
            provenance=provenance_by_logical_id[item.logical_id],
            session_date=item.session_date,
            session_timezone=plan.session_timezone,
            chunk_seconds=plan.chunk_seconds,
            max_attempts=manifest.max_attempts,
            cancelled=cancelled,
        )
        consumed_bytes += outcome.file_size_bytes
        result = QueueItemResult(item.logical_id, item.requested_symbol, item.session_date, outcome)
        results.append(result)
        if progress is not None:
            progress(result)
        if outcome.state in ("failed", "interrupted"):
            break

    return results


def is_executed_result(result: QueueItemResult) -> bool:
    """`True` só se `result` veio de uma chamada real ao adaptador.

    Itens `reusable` nunca chamam o adaptador e "concluem" instantaneamente;
    contá-los como trabalho executado infla velocidade/ETA (DEV-004, 3ª
    auditoria Codex). Quem calcula progresso/velocidade (a GUI) deve filtrar
    por esta função antes de contar `completed_count`.
    """

    return result.outcome.detail != REUSED_FROM_CATALOG_DETAIL


def remaining_item_count(plan: BackfillPlan, results: list[QueueItemResult]) -> int:
    """Itens da fila ainda não representados em `results` (para ETA/progresso).

    Só conta `planned`/`pending`: `blocked` e `blocked_by_limit` nunca são
    "restantes" no sentido de progresso -- são bloqueados, não pendentes.
    """

    processed_keys = {(result.logical_id, result.session_date) for result in results}
    return sum(
        1
        for item in plan.items
        if item.status in ("planned", "pending") and (item.logical_id, item.session_date) not in processed_keys
    )


def item_key(item: PlanItem) -> tuple[str, date]:
    return (item.logical_id, item.session_date)
