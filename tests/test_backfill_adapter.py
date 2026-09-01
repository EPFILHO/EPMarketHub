"""Cobertura da fronteira de execução futura (DEV-004 — C3.1).

Nenhum adaptador real existe nesta ordem: todo teste usa um `FakeAdapter`
definido neste arquivo, nunca conecta MT5/Clear/FOT nem toca disco. `run_plan`
NUNCA confia em `plan.execution_authorized`/vínculo desserializado sozinho:
vários testes aqui constroem deliberadamente um plano "mentiroso" (adulterado
ou vindo de outro manifesto) para provar que `run_plan` sempre revalida contra
um `BackfillManifest` fresco (auditoria Codex do DEV-004).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from market_analytics.backfill_adapter import (
    REUSED_FROM_CATALOG_DETAIL,
    ManifestNotExecutionAuthorizedError,
    PlanManifestMismatchError,
    QueueItemResult,
    SessionOutcome,
    is_executed_result,
    item_key,
    remaining_item_count,
    run_plan,
)
from market_analytics.backfill_plan import BackfillPlan, PlanItem, build_plan
from market_analytics.manifest import BackfillManifest, ManifestAsset


def _asset(logical_id: str, symbol: str) -> ManifestAsset:
    return ManifestAsset(
        logical_id=logical_id,
        requested_symbol=symbol,
        provenance=(("series_kind", "continuous"), ("instrument", logical_id)),
    )


def _manifest(
    *,
    assets: tuple[ManifestAsset, ...],
    source_id: str = "fake_source",
    authorization_state: str = "execution_approved",
    max_attempts: int = 3,
    max_total_bytes: int = 1_000_000_000,
    max_total_duration_seconds: int = 3_600,
    end_date: date = date(2025, 7, 18),
) -> BackfillManifest:
    return BackfillManifest(
        manifest_id="fake-manifest",
        display_name="Manifesto fictício de teste",
        work_order="DEV-004",
        source_id=source_id,
        session_timezone="America/Sao_Paulo",
        session_close_local_time=time(19, 0),
        assets=assets,
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=end_date,
        execution_order="oldest_first",
        chunk_seconds=900,
        max_attempts=max_attempts,
        concurrency=1,
        max_total_bytes=max_total_bytes,
        max_total_duration_seconds=max_total_duration_seconds,
        authorization_state=authorization_state,
    )


def _item(logical_id: str, session_date: date, status: str, catalog_state: str | None = None) -> PlanItem:
    """Item legítimo: `requested_symbol` precisa bater exatamente com o que
    `_asset` declarou para o mesmo `logical_id` (ex.: `_asset("fake_a", "FAKEA")`)
    -- `assert_plan_matches_manifest` agora confere isso item a item, então o
    helper nunca pode inventar um símbolo diferente do autorizado. Cenários de
    adulteração continuam construindo `PlanItem(...)` diretamente com um
    `requested_symbol` divergente, nunca por aqui."""

    return PlanItem(
        logical_id=logical_id,
        requested_symbol=logical_id.upper().replace("_", ""),
        session_date=session_date,
        status=status,
        catalog_state=catalog_state,
    )


def _totals(items: tuple[PlanItem, ...]) -> dict[str, int]:
    return {
        "sessions_total": len(items),
        "planned": sum(1 for item in items if item.status == "planned"),
        "reusable": sum(1 for item in items if item.status == "reusable"),
        "pending": sum(1 for item in items if item.status == "pending"),
        "blocked": sum(1 for item in items if item.status == "blocked"),
        "blocked_by_limit": sum(1 for item in items if item.status == "blocked_by_limit"),
    }


def _bound_plan(manifest: BackfillManifest, items: tuple[PlanItem, ...]) -> BackfillPlan:
    """Plano corretamente vinculado a `manifest` (fingerprint/id/fonte/tz/chunk),
    mas com `items`/`totals` substituídos pelos itens fictícios do teste."""

    base = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={asset.logical_id: 1.0 for asset in manifest.assets},
        benchmark_seconds_per_session={asset.logical_id: 1.0 for asset in manifest.assets},
    )
    return replace(base, items=items, totals=_totals(items))


@dataclass
class FakeAdapter:
    """Adaptador falso: devolve resultados pré-programados por (logical_id, data)
    e registra todos os kwargs recebidos, para provar que `run_plan` repassa
    proveniência/max_attempts/cancelled corretamente."""

    outcomes: dict[tuple[str, date], SessionOutcome]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def fetch_session(
        self,
        *,
        logical_id,
        requested_symbol,
        provenance,
        session_date,
        session_timezone,
        chunk_seconds,
        max_attempts,
        cancelled,
    ) -> SessionOutcome:
        self.calls.append(
            {
                "logical_id": logical_id,
                "requested_symbol": requested_symbol,
                "provenance": dict(provenance),
                "session_date": session_date,
                "session_timezone": session_timezone,
                "chunk_seconds": chunk_seconds,
                "max_attempts": max_attempts,
                "cancelled": cancelled,
            }
        )
        return self.outcomes[(logical_id, session_date)]


def _step_clock(values: list[float]):
    """Clock injetável e determinístico: devolve os valores em sequência."""

    iterator = iter(values)
    return lambda: next(iterator)


# --- run_plan nunca confia em plan.execution_authorized/vínculo sozinho -------


def test_run_plan_refuses_when_manifest_is_not_execution_approved_even_if_plan_lies() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),), authorization_state="planning")
    plan = _bound_plan(manifest, (_item("fake_a", date(2025, 7, 14), "planned"),))
    lying_plan = replace(plan, execution_authorized=True)  # plano "mentiroso"
    adapter = FakeAdapter(outcomes={})

    with pytest.raises(ManifestNotExecutionAuthorizedError):
        run_plan(lying_plan, manifest, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_bound_to_a_different_manifest() -> None:
    manifest_a = _manifest(assets=(_asset("fake_a", "FAKEA"),), source_id="source_a")
    manifest_b = _manifest(assets=(_asset("fake_a", "FAKEA"),), source_id="source_b")
    plan_from_a = _bound_plan(manifest_a, (_item("fake_a", date(2025, 7, 14), "planned"),))
    adapter = FakeAdapter(outcomes={})

    with pytest.raises(PlanManifestMismatchError):
        run_plan(plan_from_a, manifest_b, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_with_tampered_chunk_seconds() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    plan = _bound_plan(manifest, (_item("fake_a", date(2025, 7, 14), "planned"),))
    tampered_plan = replace(plan, chunk_seconds=plan.chunk_seconds + 1)
    adapter = FakeAdapter(outcomes={})

    with pytest.raises(PlanManifestMismatchError, match="chunk_seconds"):
        run_plan(tampered_plan, manifest, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_with_a_tampered_requested_symbol_and_never_calls_the_adapter() -> None:
    """Regressão da 2ª auditoria Codex: um item com `requested_symbol` que não
    corresponde ao ativo autorizado para o `logical_id` precisa ser recusado
    ANTES de qualquer chamada ao adaptador -- vinculação de metadados de topo
    (fingerprint/id/fonte/etc.) sozinha não é suficiente."""

    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    plan = _bound_plan(manifest, (_item("fake_a", date(2025, 7, 14), "planned"),))
    tampered_item = PlanItem(
        logical_id="fake_a",
        requested_symbol="NOT-AUTHORIZED",
        session_date=date(2025, 7, 14),
        status="planned",
        catalog_state=None,
    )
    tampered_plan = replace(plan, items=(tampered_item,), totals=_totals((tampered_item,)))
    adapter = FakeAdapter(outcomes={("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed")})

    with pytest.raises(PlanManifestMismatchError, match="requested_symbol"):
        run_plan(tampered_plan, manifest, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_with_a_session_date_outside_the_resolved_interval_and_never_calls_the_adapter() -> None:
    """Regressão da 2ª auditoria Codex: uma `session_date` fora do intervalo
    resolvido precisa ser recusada ANTES de qualquer chamada ao adaptador."""

    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))  # end_date=2025-07-18
    plan = _bound_plan(manifest, (_item("fake_a", date(2025, 7, 14), "planned"),))
    tampered_item = PlanItem(
        logical_id="fake_a",
        requested_symbol="FAKEA",
        session_date=date(2025, 8, 1),  # bem além do intervalo resolvido (até 2025-07-18)
        status="planned",
        catalog_state=None,
    )
    tampered_plan = replace(plan, items=(tampered_item,), totals=_totals((tampered_item,)))
    adapter = FakeAdapter(outcomes={("fake_a", date(2025, 8, 1)): SessionOutcome(state="completed")})

    with pytest.raises(PlanManifestMismatchError, match="fora do intervalo resolvido"):
        run_plan(tampered_plan, manifest, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_with_a_weekend_session_date_and_never_calls_the_adapter() -> None:
    """Regressão da 2ª auditoria Codex: uma `session_date` de fim de semana
    (nunca um dia candidato) precisa ser recusada ANTES de qualquer chamada
    ao adaptador, mesmo caindo dentro do intervalo numérico resolvido."""

    # end_date=07-21 (segunda seguinte) para que o intervalo numérico
    # resolvido abranja o fim de semana 07-19/07-20, isolando a checagem de
    # "dia candidato" da checagem de "fora do intervalo".
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),), end_date=date(2025, 7, 21))
    plan = _bound_plan(manifest, (_item("fake_a", date(2025, 7, 14), "planned"),))
    weekend_item = PlanItem(
        logical_id="fake_a",
        requested_symbol="FAKEA",
        session_date=date(2025, 7, 19),  # sábado, dentro de [07-14, 07-21]
        status="planned",
        catalog_state=None,
    )
    tampered_plan = replace(plan, items=(weekend_item,), totals=_totals((weekend_item,)))
    adapter = FakeAdapter(outcomes={("fake_a", date(2025, 7, 19)): SessionOutcome(state="completed")})

    with pytest.raises(PlanManifestMismatchError):
        run_plan(tampered_plan, manifest, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_with_a_duplicate_item_and_never_calls_the_adapter() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    plan = _bound_plan(manifest, (_item("fake_a", date(2025, 7, 14), "planned"),))
    duplicated = (plan.items[0], plan.items[0])
    tampered_plan = replace(plan, items=duplicated, totals=_totals(duplicated))
    adapter = FakeAdapter(outcomes={("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed")})

    with pytest.raises(PlanManifestMismatchError, match="duplicado"):
        run_plan(tampered_plan, manifest, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_with_an_item_for_an_unauthorized_logical_id() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    plan = _bound_plan(manifest, (_item("fake_a", date(2025, 7, 14), "planned"),))
    rogue_item = PlanItem(
        logical_id="rogue_asset",
        requested_symbol="ROGUE",
        session_date=date(2025, 7, 14),
        status="planned",
        catalog_state=None,
    )
    tampered_plan = replace(plan, items=(rogue_item,), totals=_totals((rogue_item,)))
    adapter = FakeAdapter(outcomes={("rogue_asset", date(2025, 7, 14)): SessionOutcome(state="completed")})

    with pytest.raises(PlanManifestMismatchError, match="fora do escopo"):
        run_plan(tampered_plan, manifest, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_with_an_extended_resolved_end_date_and_never_calls_the_adapter() -> None:
    """Reproduz o achado da 3ª auditoria Codex: manifesto `explicit` com
    `end_date=2025-07-15`; `resolved_end_date` adulterado para 2025-07-16 (com
    um item extra nessa data) precisa ser recusado ANTES de qualquer chamada
    ao adaptador."""

    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),), end_date=date(2025, 7, 15))
    plan = _bound_plan(manifest, (_item("fake_a", date(2025, 7, 14), "planned"),))
    extra_item = PlanItem(
        logical_id="fake_a",
        requested_symbol="FAKEA",
        session_date=date(2025, 7, 16),
        status="planned",
        catalog_state=None,
    )
    tampered_items = (*plan.items, extra_item)
    tampered_plan = replace(
        plan, resolved_end_date=date(2025, 7, 16), items=tampered_items, totals=_totals(tampered_items)
    )
    adapter = FakeAdapter(
        outcomes={
            ("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed"),
            ("fake_a", date(2025, 7, 16)): SessionOutcome(state="completed"),
        }
    )

    with pytest.raises(PlanManifestMismatchError, match="resolved_end_date"):
        run_plan(tampered_plan, manifest, adapter)

    assert adapter.calls == []


def test_run_plan_refuses_a_plan_with_reversed_items_and_never_calls_the_adapter() -> None:
    """Reproduz o achado da 3ª auditoria Codex: inverter `plan.items` é
    recusado mesmo com `execution_order` intacto -- ANTES de qualquer chamada
    ao adaptador."""

    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),), end_date=date(2025, 7, 16))
    plan = _bound_plan(
        manifest,
        (
            _item("fake_a", date(2025, 7, 14), "planned"),
            _item("fake_a", date(2025, 7, 15), "planned"),
            _item("fake_a", date(2025, 7, 16), "planned"),
        ),
    )
    reversed_items = tuple(reversed(plan.items))
    tampered_plan = replace(plan, items=reversed_items, totals=_totals(reversed_items))
    adapter = FakeAdapter(
        outcomes={
            ("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed"),
            ("fake_a", date(2025, 7, 15)): SessionOutcome(state="completed"),
            ("fake_a", date(2025, 7, 16)): SessionOutcome(state="completed"),
        }
    )

    with pytest.raises(PlanManifestMismatchError, match="ordem"):
        run_plan(tampered_plan, manifest, adapter)

    assert adapter.calls == []


# --- reuso sem chamar o adaptador, preservando completed vs. empty ------------


def test_run_plan_reuses_reusable_items_without_calling_the_adapter_and_preserves_catalog_state() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    items = (
        _item("fake_a", date(2025, 7, 14), "reusable", catalog_state="completed"),
        _item("fake_a", date(2025, 7, 15), "reusable", catalog_state="empty"),
    )
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(outcomes={})

    results = run_plan(plan, manifest, adapter)

    assert adapter.calls == []
    assert [result.outcome.state for result in results] == ["completed", "empty"]
    assert all(result.outcome.detail == REUSED_FROM_CATALOG_DETAIL for result in results)


def test_run_plan_skips_blocked_and_blocked_by_limit_items_without_calling_the_adapter() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    items = (
        _item("fake_a", date(2025, 7, 14), "blocked", catalog_state="running"),
        _item("fake_a", date(2025, 7, 15), "blocked_by_limit", catalog_state=None),
        _item("fake_a", date(2025, 7, 16), "planned"),
    )
    plan = _bound_plan(manifest, items)
    outcome = SessionOutcome(state="completed", tick_count=10, file_size_bytes=100)
    adapter = FakeAdapter(outcomes={("fake_a", date(2025, 7, 16)): outcome})

    results = run_plan(plan, manifest, adapter)

    assert adapter.calls[0]["session_date"] == date(2025, 7, 16)
    assert len(adapter.calls) == 1
    assert len(results) == 1
    assert results[0].session_date == date(2025, 7, 16)


# --- falha/cancelamento preservam estado explícito -----------------------------


def test_run_plan_stops_after_a_failed_item_but_preserves_prior_results() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    items = (
        _item("fake_a", date(2025, 7, 14), "planned"),
        _item("fake_a", date(2025, 7, 15), "planned"),
        _item("fake_a", date(2025, 7, 16), "planned"),
    )
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(
        outcomes={
            ("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed"),
            ("fake_a", date(2025, 7, 15)): SessionOutcome(state="failed", detail="mt5_error"),
            ("fake_a", date(2025, 7, 16)): SessionOutcome(state="completed"),
        }
    )

    results = run_plan(plan, manifest, adapter)

    assert [result.session_date for result in results] == [date(2025, 7, 14), date(2025, 7, 15)]
    assert results[0].outcome.state == "completed"
    assert results[1].outcome.state == "failed"
    assert len(adapter.calls) == 2


def test_run_plan_stops_after_an_interrupted_item_too() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    items = (
        _item("fake_a", date(2025, 7, 14), "planned"),
        _item("fake_a", date(2025, 7, 15), "planned"),
    )
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(
        outcomes={
            ("fake_a", date(2025, 7, 14)): SessionOutcome(state="interrupted", detail="cancelled_mid_chunk"),
            ("fake_a", date(2025, 7, 15)): SessionOutcome(state="completed"),
        }
    )

    results = run_plan(plan, manifest, adapter)

    assert len(results) == 1
    assert results[0].outcome.state == "interrupted"
    assert len(adapter.calls) == 1


def test_run_plan_checks_cancellation_only_between_items_never_mid_item() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    items = (
        _item("fake_a", date(2025, 7, 14), "planned"),
        _item("fake_a", date(2025, 7, 15), "planned"),
    )
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(
        outcomes={
            ("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed"),
            ("fake_a", date(2025, 7, 15)): SessionOutcome(state="completed"),
        }
    )

    def cancelled() -> bool:
        # Sinaliza cancelamento só depois que o primeiro item já foi
        # processado por completo -- nunca no meio dele.
        return len(adapter.calls) >= 1

    results = run_plan(plan, manifest, adapter, cancelled=cancelled)

    assert len(results) == 1
    assert results[0].outcome.state == "completed"
    assert len(adapter.calls) == 1


def test_run_plan_reports_progress_for_every_processed_item_including_reused() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    items = (
        _item("fake_a", date(2025, 7, 14), "reusable", catalog_state="completed"),
        _item("fake_a", date(2025, 7, 15), "planned"),
    )
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(outcomes={("fake_a", date(2025, 7, 15)): SessionOutcome(state="empty")})
    seen: list[QueueItemResult] = []

    run_plan(plan, manifest, adapter, progress=seen.append)

    assert [result.session_date for result in seen] == [date(2025, 7, 14), date(2025, 7, 15)]


# --- fronteira de execução: proveniência, max_attempts e cancelamento entre chunks --


def test_run_plan_passes_provenance_max_attempts_and_the_cancelled_function_to_the_adapter() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),), max_attempts=7)
    items = (_item("fake_a", date(2025, 7, 14), "planned"),)
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(outcomes={("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed")})

    def cancelled() -> bool:
        return False

    run_plan(plan, manifest, adapter, cancelled=cancelled)

    call = adapter.calls[0]
    assert call["provenance"] == manifest.assets[0].provenance_dict()
    assert call["max_attempts"] == 7
    # A MESMA função é repassada -- o adaptador pode checá-la entre chunks.
    assert call["cancelled"] is cancelled


class _ChunkedAdapter:
    """Adaptador falso que simula 3 chunks internos e verifica `cancelled`
    entre cada um -- demonstra que a função repassada por `run_plan` permite
    cancelamento ENTRE CHUNKS, não só entre sessões inteiras."""

    def __init__(self) -> None:
        self.chunks_processed = 0

    def fetch_session(
        self,
        *,
        logical_id,
        requested_symbol,
        provenance,
        session_date,
        session_timezone,
        chunk_seconds,
        max_attempts,
        cancelled,
    ) -> SessionOutcome:
        for _chunk_index in range(3):
            if cancelled():
                return SessionOutcome(
                    state="interrupted",
                    tick_count=self.chunks_processed * 100,
                    detail="cancelled_between_chunks",
                )
            self.chunks_processed += 1
        return SessionOutcome(state="completed", tick_count=self.chunks_processed * 100)


def test_fake_adapter_honors_cancellation_between_chunks_within_a_single_session() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    items = (_item("fake_a", date(2025, 7, 14), "planned"),)
    plan = _bound_plan(manifest, items)
    adapter = _ChunkedAdapter()

    # Cancela depois de exatamente 2 dos 3 chunks internos.
    cancel_after = 2

    def cancelled() -> bool:
        return adapter.chunks_processed >= cancel_after

    results = run_plan(plan, manifest, adapter, cancelled=cancelled)

    assert len(results) == 1
    assert results[0].outcome.state == "interrupted"
    assert results[0].outcome.detail == "cancelled_between_chunks"
    assert adapter.chunks_processed == cancel_after


# --- limites do manifesto: run_plan recusa executar acima do limite -----------


def test_run_plan_refuses_to_start_another_item_once_live_bytes_reach_the_limit() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),), max_total_bytes=100)
    items = (
        _item("fake_a", date(2025, 7, 14), "planned"),
        _item("fake_a", date(2025, 7, 15), "planned"),
        _item("fake_a", date(2025, 7, 16), "planned"),
    )
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(
        outcomes={
            ("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed", file_size_bytes=60),
            ("fake_a", date(2025, 7, 15)): SessionOutcome(state="completed", file_size_bytes=60),
            ("fake_a", date(2025, 7, 16)): SessionOutcome(state="completed", file_size_bytes=60),
        }
    )

    results = run_plan(plan, manifest, adapter)

    # 60, depois 60+60=120 >= 100: o terceiro item nunca é iniciado.
    assert len(adapter.calls) == 2
    assert len(results) == 2


def test_run_plan_refuses_to_start_another_item_once_live_duration_reaches_the_limit() -> None:
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),), max_total_duration_seconds=10)
    items = (
        _item("fake_a", date(2025, 7, 14), "planned"),
        _item("fake_a", date(2025, 7, 15), "planned"),
        _item("fake_a", date(2025, 7, 16), "planned"),
    )
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(
        outcomes={
            ("fake_a", date(2025, 7, 14)): SessionOutcome(state="completed"),
            ("fake_a", date(2025, 7, 15)): SessionOutcome(state="completed"),
            ("fake_a", date(2025, 7, 16)): SessionOutcome(state="completed"),
        }
    )
    # clock(): 1 chamada inicial + 1 checagem por item antes de iniciá-lo.
    clock = _step_clock([0.0, 0.0, 5.0, 15.0])

    results = run_plan(plan, manifest, adapter, clock=clock)

    assert len(adapter.calls) == 2
    assert len(results) == 2


# --- utilitários de fila --------------------------------------------------------


def test_remaining_item_count_only_counts_planned_and_pending_not_yet_processed() -> None:
    items = (
        _item("fake_a", date(2025, 7, 14), "planned"),
        _item("fake_a", date(2025, 7, 15), "pending", catalog_state="failed"),
        _item("fake_a", date(2025, 7, 16), "reusable", catalog_state="completed"),
        _item("fake_a", date(2025, 7, 17), "blocked", catalog_state="running"),
        _item("fake_a", date(2025, 7, 18), "blocked_by_limit", catalog_state=None),
    )
    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    plan = _bound_plan(manifest, items)
    processed = [
        QueueItemResult("fake_a", "FAKEA", date(2025, 7, 14), SessionOutcome(state="completed")),
    ]

    assert remaining_item_count(plan, processed) == 1  # só a "pending" de 07-15 falta


def test_item_key_pairs_logical_id_with_session_date() -> None:
    item = _item("fake_a", date(2025, 7, 14), "planned")
    assert item_key(item) == ("fake_a", date(2025, 7, 14))


# --- is_executed_result: velocidade/ETA nunca inflados por reuso -------------


def test_is_executed_result_is_false_for_a_reused_result() -> None:
    reused = QueueItemResult(
        "fake_a",
        "FAKEA",
        date(2025, 7, 14),
        SessionOutcome(state="completed", detail=REUSED_FROM_CATALOG_DETAIL),
    )
    assert is_executed_result(reused) is False


def test_is_executed_result_is_true_for_a_result_from_a_real_adapter_call() -> None:
    executed = QueueItemResult("fake_a", "FAKEA", date(2025, 7, 14), SessionOutcome(state="completed"))
    assert is_executed_result(executed) is True


def test_run_plan_marks_reused_results_so_they_can_be_excluded_from_speed_and_eta() -> None:
    """Ponta a ponta: os resultados que `run_plan` devolve para itens
    `reusable` sempre carregam o `detail` que `is_executed_result` reconhece
    -- a GUI filtra por ele antes de contar `completed_count` (DEV-004, 3ª
    auditoria Codex)."""

    manifest = _manifest(assets=(_asset("fake_a", "FAKEA"),))
    items = (
        _item("fake_a", date(2025, 7, 14), "reusable", catalog_state="completed"),
        _item("fake_a", date(2025, 7, 15), "planned"),
    )
    plan = _bound_plan(manifest, items)
    adapter = FakeAdapter(outcomes={("fake_a", date(2025, 7, 15)): SessionOutcome(state="completed")})

    results = run_plan(plan, manifest, adapter)

    executed = [result for result in results if is_executed_result(result)]
    assert len(results) == 2
    assert len(executed) == 1
    assert executed[0].session_date == date(2025, 7, 15)


# --- SessionOutcome: vocabulário fechado ---------------------------------------


def test_session_outcome_rejects_unknown_state() -> None:
    with pytest.raises(ValueError):
        SessionOutcome(state="archived")


def test_session_outcome_rejects_negative_tick_count() -> None:
    with pytest.raises(ValueError):
        SessionOutcome(state="completed", tick_count=-1)


def test_session_outcome_rejects_negative_file_size() -> None:
    with pytest.raises(ValueError):
        SessionOutcome(state="completed", file_size_bytes=-1)


# --- pureza de import -----------------------------------------------------------


def test_backfill_adapter_module_does_not_import_mt5_or_gui() -> None:
    script = (
        "import sys\n"
        "import market_analytics.backfill_adapter\n"
        "forbidden = {'MetaTrader5', 'tkinter', 'PySide6', 'PyQt5', 'PyQt6'}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, f'módulos proibidos carregados: {loaded}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
