"""Cobertura do planejador e fila genéricos (DEV-004 — C3.1).

Núcleo puro: nenhum teste toca ticks reais, MT5 ou `D:\\EPData`. O catálogo é
sempre um snapshot fabricado à mão (`CatalogSnapshot`); persistência usa só
`tmp_path`.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from market_analytics.backfill_plan import (
    BackfillPlan,
    PlanBuildError,
    PlanItem,
    assert_plan_matches_manifest,
    build_plan,
    build_session_dates,
    classify_session_status,
    estimate_eta_seconds,
    latest_closed_session_date,
    load_plan,
    resolve_end_date,
    save_plan_atomic,
)
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
    start_date: date,
    end_date_policy: str = "explicit",
    end_date: date | None = None,
    execution_order: str = "oldest_first",
    authorization_state: str = "planning",
    session_close_local_time: time = time(19, 0),
    max_total_bytes: int = 1_000_000_000,
    max_total_duration_seconds: int = 3_600,
) -> BackfillManifest:
    return BackfillManifest(
        manifest_id="fake-manifest",
        display_name="Manifesto fictício de teste",
        work_order="DEV-004",
        source_id="fake_source",
        session_timezone="America/Sao_Paulo",
        session_close_local_time=session_close_local_time,
        assets=assets,
        start_date=start_date,
        end_date_policy=end_date_policy,
        end_date=end_date,
        execution_order=execution_order,
        chunk_seconds=900,
        max_attempts=3,
        concurrency=1,
        max_total_bytes=max_total_bytes,
        max_total_duration_seconds=max_total_duration_seconds,
        authorization_state=authorization_state,
    )


def _item(
    logical_id: str, session_date: date, status: str, catalog_state: str | None = None
) -> PlanItem:
    return PlanItem(
        logical_id=logical_id,
        requested_symbol=logical_id.upper(),
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


def _replace_items(plan: BackfillPlan, items: tuple[PlanItem, ...]) -> BackfillPlan:
    """Substitui `items`/`totals` de um plano já válido -- para simular
    adulteração sem precisar reconstruir todos os outros campos."""

    return replace(plan, items=items, totals=_totals(items))


def _plan(
    items: tuple[PlanItem, ...],
    *,
    manifest_id: str = "fake-manifest",
    manifest_fingerprint: str = "fake-fingerprint",
    execution_authorized: bool = False,
    estimated_bytes_remaining: int = 0,
    estimated_seconds_remaining: float = 0.0,
) -> BackfillPlan:
    return BackfillPlan(
        manifest_id=manifest_id,
        manifest_fingerprint=manifest_fingerprint,
        source_id="fake_source",
        session_timezone="America/Sao_Paulo",
        execution_order="oldest_first",
        chunk_seconds=900,
        resolved_start_date=items[0].session_date if items else date(2025, 7, 14),
        resolved_end_date=items[-1].session_date if items else date(2025, 7, 14),
        items=items,
        totals=_totals(items),
        estimated_bytes_remaining=estimated_bytes_remaining,
        estimated_seconds_remaining=estimated_seconds_remaining,
        execution_authorized=execution_authorized,
    )


# --- calendário / ordem -------------------------------------------------------


def test_build_session_dates_only_includes_weekdays_with_inclusive_bounds() -> None:
    # 2025-07-14 (segunda) .. 2025-07-20 (domingo): só seg-sex, incluindo as
    # duas pontas.
    dates = build_session_dates(date(2025, 7, 14), date(2025, 7, 20), order="oldest_first")
    assert dates == [date(2025, 7, 14), date(2025, 7, 15), date(2025, 7, 16), date(2025, 7, 17), date(2025, 7, 18)]


def test_build_session_dates_newest_first_is_the_exact_reverse() -> None:
    oldest_first = build_session_dates(date(2025, 7, 14), date(2025, 7, 18), order="oldest_first")
    newest_first = build_session_dates(date(2025, 7, 14), date(2025, 7, 18), order="newest_first")
    assert newest_first == list(reversed(oldest_first))


def test_build_session_dates_single_day_boundary_is_inclusive() -> None:
    monday = date(2025, 7, 14)
    assert build_session_dates(monday, monday, order="oldest_first") == [monday]


def test_build_session_dates_rejects_unknown_order() -> None:
    with pytest.raises(PlanBuildError):
        build_session_dates(date(2025, 7, 14), date(2025, 7, 15), order="sideways")


# --- latest_closed: consciente do horário local de fechamento -----------------


def test_latest_closed_includes_the_current_weekday_after_close() -> None:
    # Terça 2025-07-15, 20:00 America/Sao_Paulo (UTC-3) = 23:00 UTC; fecha às 19:00.
    now_utc = datetime(2025, 7, 15, 23, 0, tzinfo=UTC)
    result = latest_closed_session_date(
        now_utc=now_utc, session_timezone="America/Sao_Paulo", session_close_local_time=time(19, 0)
    )
    assert result == date(2025, 7, 15)


def test_latest_closed_at_the_exact_close_instant_counts_as_closed() -> None:
    # Quarta 2025-07-16, 19:00:00 exatos America/Sao_Paulo = 22:00 UTC.
    now_utc = datetime(2025, 7, 16, 22, 0, 0, tzinfo=UTC)
    result = latest_closed_session_date(
        now_utc=now_utc, session_timezone="America/Sao_Paulo", session_close_local_time=time(19, 0)
    )
    assert result == date(2025, 7, 16)


def test_latest_closed_uses_the_previous_weekday_before_close() -> None:
    # Terça 2025-07-15, 10:00 America/Sao_Paulo = 13:00 UTC; ainda não fechou.
    now_utc = datetime(2025, 7, 15, 13, 0, tzinfo=UTC)
    result = latest_closed_session_date(
        now_utc=now_utc, session_timezone="America/Sao_Paulo", session_close_local_time=time(19, 0)
    )
    assert result == date(2025, 7, 14)


def test_latest_closed_one_second_before_close_excludes_today() -> None:
    # Quarta 2025-07-16, 18:59:59 America/Sao_Paulo = 21:59:59 UTC.
    now_utc = datetime(2025, 7, 16, 21, 59, 59, tzinfo=UTC)
    result = latest_closed_session_date(
        now_utc=now_utc, session_timezone="America/Sao_Paulo", session_close_local_time=time(19, 0)
    )
    assert result == date(2025, 7, 15)


def test_latest_closed_on_a_saturday_goes_back_to_friday() -> None:
    # Sábado 2025-07-19, meio-dia America/Sao_Paulo = 15:00 UTC.
    now_utc = datetime(2025, 7, 19, 15, 0, tzinfo=UTC)
    result = latest_closed_session_date(
        now_utc=now_utc, session_timezone="America/Sao_Paulo", session_close_local_time=time(19, 0)
    )
    assert result == date(2025, 7, 18)


def test_latest_closed_on_a_sunday_goes_back_to_friday_too() -> None:
    # Domingo 2025-07-20, meio-dia America/Sao_Paulo = 15:00 UTC.
    now_utc = datetime(2025, 7, 20, 15, 0, tzinfo=UTC)
    result = latest_closed_session_date(
        now_utc=now_utc, session_timezone="America/Sao_Paulo", session_close_local_time=time(19, 0)
    )
    assert result == date(2025, 7, 18)


def test_latest_closed_rejects_naive_now_utc() -> None:
    with pytest.raises(PlanBuildError, match="timezone-aware"):
        latest_closed_session_date(
            now_utc=datetime(2025, 7, 15, 20, 0),
            session_timezone="America/Sao_Paulo",
            session_close_local_time=time(19, 0),
        )


def test_resolve_end_date_explicit_ignores_now_utc() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 18),
    )
    assert resolve_end_date(manifest, now_utc=datetime(2020, 1, 1, tzinfo=UTC)) == date(2025, 7, 18)


def test_resolve_end_date_latest_closed_delegates_to_latest_closed_session_date() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="latest_closed",
    )
    now_utc = datetime(2025, 7, 15, 13, 0, tzinfo=UTC)  # terça, 10:00 local, antes do fechamento
    assert resolve_end_date(manifest, now_utc=now_utc) == date(2025, 7, 14)


# --- classificação de status a partir do catálogo -----------------------------


@pytest.mark.parametrize(
    ("catalog_state", "expected"),
    [
        (None, "planned"),
        ("pending", "planned"),
        ("completed", "reusable"),
        ("empty", "reusable"),
        ("running", "blocked"),
        ("failed", "pending"),
        ("interrupted", "pending"),
    ],
)
def test_classify_session_status_maps_every_known_catalog_state(catalog_state, expected) -> None:
    assert classify_session_status(catalog_state) == expected


def test_classify_session_status_rejects_unknown_catalog_state() -> None:
    with pytest.raises(PlanBuildError):
        classify_session_status("archived")


def test_classify_session_status_never_returns_blocked_by_limit() -> None:
    for catalog_state in (None, "pending", "completed", "empty", "running", "failed", "interrupted"):
        assert classify_session_status(catalog_state) != "blocked_by_limit"


# --- fila determinística -------------------------------------------------------


def test_build_plan_is_deterministic_with_a_single_fictitious_asset() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
        execution_order="oldest_first",
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 100.0},
        benchmark_seconds_per_session={"fake_a": 2.0},
    )
    assert [item.session_date for item in plan.items] == [date(2025, 7, 14), date(2025, 7, 15), date(2025, 7, 16)]
    assert all(item.logical_id == "fake_a" for item in plan.items)
    assert all(item.status == "planned" for item in plan.items)
    assert plan.totals == {
        "sessions_total": 3,
        "planned": 3,
        "reusable": 0,
        "pending": 0,
        "blocked": 0,
        "blocked_by_limit": 0,
    }
    assert plan.estimated_bytes_remaining == 300
    assert plan.estimated_seconds_remaining == 6.0
    assert plan.execution_authorized is False
    assert plan.manifest_fingerprint == manifest.fingerprint()


def test_build_plan_interleaves_multiple_fictitious_assets_per_session_date() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"), _asset("fake_b", "FAKEB")),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 15),
        execution_order="oldest_first",
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 10.0, "fake_b": 20.0},
        benchmark_seconds_per_session={"fake_a": 1.0, "fake_b": 2.0},
    )
    # Determinístico: para cada data, os ativos aparecem na ordem declarada
    # no manifesto.
    assert [(item.session_date, item.logical_id) for item in plan.items] == [
        (date(2025, 7, 14), "fake_a"),
        (date(2025, 7, 14), "fake_b"),
        (date(2025, 7, 15), "fake_a"),
        (date(2025, 7, 15), "fake_b"),
    ]
    assert plan.totals["sessions_total"] == 4


def test_build_plan_rejects_missing_benchmark_for_a_declared_asset() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    with pytest.raises(PlanBuildError):
        build_plan(
            manifest,
            now_utc=datetime(2025, 7, 20, tzinfo=UTC),
            catalog_snapshot={},
            benchmark_bytes_per_session={},
            benchmark_seconds_per_session={},
        )


# --- catálogo: reuso sem rebaixar sessão concluída ----------------------------


def test_catalog_snapshot_marks_reuse_without_downgrading_completed_sessions() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
    )
    snapshot = {
        ("fake_a", date(2025, 7, 14)): "completed",
        ("fake_a", date(2025, 7, 15)): "failed",
        ("fake_a", date(2025, 7, 16)): "running",
    }
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot=snapshot,
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    statuses = {item.session_date: item.status for item in plan.items}
    catalog_states = {item.session_date: item.catalog_state for item in plan.items}
    assert statuses[date(2025, 7, 14)] == "reusable"
    assert catalog_states[date(2025, 7, 14)] == "completed"
    assert statuses[date(2025, 7, 15)] == "pending"
    assert statuses[date(2025, 7, 16)] == "blocked"
    # A sessão concluída nunca é recontada nas estimativas restantes.
    assert plan.totals == {
        "sessions_total": 3,
        "planned": 0,
        "reusable": 1,
        "pending": 1,
        "blocked": 1,
        "blocked_by_limit": 0,
    }
    assert plan.estimated_bytes_remaining == 1
    assert plan.estimated_seconds_remaining == 1.0


def test_catalog_snapshot_reuse_preserves_empty_distinct_from_completed() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={("fake_a", date(2025, 7, 14)): "empty"},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    assert plan.items[0].status == "reusable"
    assert plan.items[0].catalog_state == "empty"


# --- limites de bytes/duração: bloqueador explícito no plano -------------------


def test_build_plan_marks_items_beyond_max_total_bytes_as_blocked_by_limit() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),  # 3 sessões seg-qua
        max_total_bytes=100,
        max_total_duration_seconds=999_999,
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 40.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    statuses = [item.status for item in plan.items]
    # 40 + 40 = 80 <= 100 (dois primeiros cabem); 80 + 40 = 120 > 100 (terceiro bloqueado).
    assert statuses == ["planned", "planned", "blocked_by_limit"]
    assert plan.totals["blocked_by_limit"] == 1
    assert plan.totals["planned"] == 2
    assert plan.estimated_bytes_remaining == 80


def test_build_plan_marks_items_beyond_max_total_duration_seconds_as_blocked_by_limit() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
        max_total_bytes=999_999_999,
        max_total_duration_seconds=100,
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 40.0},
    )
    statuses = [item.status for item in plan.items]
    assert statuses == ["planned", "planned", "blocked_by_limit"]
    assert plan.estimated_seconds_remaining == 80.0


def test_build_plan_blocked_by_limit_applies_to_pending_items_too_in_execution_order() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
        max_total_bytes=100,
        max_total_duration_seconds=999_999,
    )
    snapshot = {("fake_a", date(2025, 7, 14)): "failed"}  # vira "pending", mas ainda entra no orçamento
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot=snapshot,
        benchmark_bytes_per_session={"fake_a": 40.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    statuses = [item.status for item in plan.items]
    assert statuses == ["pending", "planned", "blocked_by_limit"]
    assert plan.totals["blocked_by_limit"] == 1


# --- ETA sem divisão por zero e sem volume centralizado -----------------------


def test_estimate_eta_seconds_returns_zero_when_nothing_remains() -> None:
    assert estimate_eta_seconds(completed_count=5, elapsed_seconds=10.0, remaining_count=0) == 0.0


def test_estimate_eta_seconds_returns_none_without_completed_items() -> None:
    assert estimate_eta_seconds(completed_count=0, elapsed_seconds=10.0, remaining_count=3) is None


def test_estimate_eta_seconds_returns_none_without_elapsed_time() -> None:
    assert estimate_eta_seconds(completed_count=3, elapsed_seconds=0.0, remaining_count=3) is None


def test_estimate_eta_seconds_scales_linearly_from_the_observed_pace() -> None:
    eta = estimate_eta_seconds(completed_count=2, elapsed_seconds=10.0, remaining_count=4)
    assert eta == pytest.approx(20.0)


def test_build_plan_never_assumes_a_shared_benchmark_between_assets() -> None:
    """Dois ativos fictícios com benchmarks bem diferentes: a estimativa
    restante soma exatamente os dois, nunca usa um valor único/"centralizado".
    """

    manifest = _manifest(
        assets=(_asset("heavy", "HEAVY"), _asset("light", "LIGHT")),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"heavy": 1_000_000.0, "light": 10.0},
        benchmark_seconds_per_session={"heavy": 500.0, "light": 0.1},
    )
    assert plan.estimated_bytes_remaining == 1_000_010
    assert plan.estimated_seconds_remaining == pytest.approx(500.1)


# --- plano/relatório atômico e reabrível em tmp_path --------------------------


def test_save_and_load_plan_round_trips_in_tmp_path(tmp_path) -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 15),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    path = tmp_path / "reports" / "plan.json"

    save_plan_atomic(path, plan)
    reloaded = load_plan(path)

    assert reloaded == plan
    assert list(path.parent.iterdir()) == [path]


def test_load_plan_rejects_wrong_schema(tmp_path) -> None:
    path = tmp_path / "plan.json"
    path.write_text('{"schema": "something.else", "schema_version": 1}', encoding="utf-8")
    with pytest.raises(PlanBuildError):
        load_plan(path)


# --- PlanItem: vocabulário fechado e coerência status/catalog_state -----------


def test_plan_item_rejects_unknown_status() -> None:
    with pytest.raises(PlanBuildError):
        PlanItem(
            logical_id="fake_a",
            requested_symbol="FAKEA",
            session_date=date(2025, 7, 14),
            status="archived",
            catalog_state=None,
        )


@pytest.mark.parametrize(
    ("status", "catalog_state"),
    [
        ("reusable", "running"),
        ("reusable", None),
        ("planned", "completed"),
        ("pending", "empty"),
        ("blocked", "failed"),
        ("blocked_by_limit", "completed"),
    ],
)
def test_plan_item_rejects_incoherent_status_and_catalog_state(status: str, catalog_state: str | None) -> None:
    """Defesa contra um plano adulterado: status e catalog_state precisam ser
    uma combinação real (ver `_STATUS_TO_ALLOWED_CATALOG_STATES`)."""

    with pytest.raises(PlanBuildError, match="incoerente"):
        PlanItem(
            logical_id="fake_a",
            requested_symbol="FAKEA",
            session_date=date(2025, 7, 14),
            status=status,
            catalog_state=catalog_state,
        )


def test_plan_item_from_dict_rejects_unknown_field() -> None:
    with pytest.raises(PlanBuildError, match="desconhecido"):
        PlanItem.from_dict(
            {
                "logical_id": "fake_a",
                "requested_symbol": "FAKEA",
                "session_date": "2025-07-14",
                "status": "planned",
                "catalog_state": None,
                "extra": "nope",
            }
        )


def test_plan_item_from_dict_rejects_missing_field() -> None:
    with pytest.raises(PlanBuildError, match="ausente"):
        PlanItem.from_dict(
            {
                "logical_id": "fake_a",
                "requested_symbol": "FAKEA",
                "session_date": "2025-07-14",
                "status": "planned",
            }
        )


# --- BackfillPlan: esquema estrito e coerência totals/itens (defesa contra adulteração) --


def _valid_plan_dict(plan: BackfillPlan) -> dict:
    return plan.to_dict()


def test_backfill_plan_from_dict_rejects_wrong_schema_version() -> None:
    with pytest.raises(PlanBuildError):
        BackfillPlan.from_dict({"schema": "ep_market_hub.backfill_plan", "schema_version": 99})


def test_backfill_plan_from_dict_rejects_unknown_field() -> None:
    plan = _plan((_item("fake_a", date(2025, 7, 14), "planned"),))
    data = _valid_plan_dict(plan)
    data["extra_field"] = "nope"
    with pytest.raises(PlanBuildError, match="desconhecido"):
        BackfillPlan.from_dict(data)


def test_backfill_plan_from_dict_rejects_missing_field() -> None:
    plan = _plan((_item("fake_a", date(2025, 7, 14), "planned"),))
    data = _valid_plan_dict(plan)
    del data["chunk_seconds"]
    with pytest.raises(PlanBuildError, match="ausente"):
        BackfillPlan.from_dict(data)


def test_backfill_plan_from_dict_rejects_non_boolean_execution_authorized() -> None:
    """Regressão do achado da auditoria Codex: `bool("false")` é `True` em
    Python -- `from_dict` nunca pode aceitar essa coerção."""

    plan = _plan((_item("fake_a", date(2025, 7, 14), "planned"),))
    data = _valid_plan_dict(plan)
    data["execution_authorized"] = "false"
    with pytest.raises(PlanBuildError, match="booleano"):
        BackfillPlan.from_dict(data)


def test_backfill_plan_post_init_rejects_totals_incoherent_with_items() -> None:
    """Plano adulterado: `totals` diz uma coisa, `items` mostra outra."""

    items = (_item("fake_a", date(2025, 7, 14), "planned"),)
    tampered_totals = _totals(items)
    tampered_totals["planned"] = 99  # mentira -- só há 1 item "planned"
    with pytest.raises(PlanBuildError, match="incoerente"):
        BackfillPlan(
            manifest_id="fake-manifest",
            manifest_fingerprint="fake-fingerprint",
            source_id="fake_source",
            session_timezone="America/Sao_Paulo",
            execution_order="oldest_first",
            chunk_seconds=900,
            resolved_start_date=date(2025, 7, 14),
            resolved_end_date=date(2025, 7, 14),
            items=items,
            totals=tampered_totals,
            estimated_bytes_remaining=0,
            estimated_seconds_remaining=0.0,
            execution_authorized=False,
        )


def test_backfill_plan_from_dict_rejects_tampered_items_without_matching_totals() -> None:
    """Mesmo achado, mas passando pelo caminho `from_dict` (plano editado à mão em disco)."""

    plan = _plan((_item("fake_a", date(2025, 7, 14), "planned"),))
    data = _valid_plan_dict(plan)
    # Adiciona um segundo item sem atualizar `totals` -- exatamente o cenário
    # de um relatório adulterado manualmente.
    data["items"].append(
        {
            "logical_id": "fake_a",
            "requested_symbol": "FAKEA",
            "session_date": "2025-07-15",
            "status": "planned",
            "catalog_state": None,
        }
    )
    with pytest.raises(PlanBuildError, match="incoerente"):
        BackfillPlan.from_dict(data)


def test_backfill_plan_from_dict_rejects_negative_estimated_bytes() -> None:
    plan = _plan((_item("fake_a", date(2025, 7, 14), "planned"),))
    data = _valid_plan_dict(plan)
    data["estimated_bytes_remaining"] = -1
    with pytest.raises(PlanBuildError):
        BackfillPlan.from_dict(data)


def test_backfill_plan_from_dict_round_trips_a_valid_plan() -> None:
    plan = _plan(
        (_item("fake_a", date(2025, 7, 14), "planned"), _item("fake_a", date(2025, 7, 15), "reusable", "completed")),
        execution_authorized=True,
        estimated_bytes_remaining=10,
        estimated_seconds_remaining=2.5,
    )
    reloaded = BackfillPlan.from_dict(_valid_plan_dict(plan))
    assert reloaded == plan


# --- benchmarks: número finito e não negativo (3ª auditoria Codex) -----------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0, -0.001])
def test_build_plan_rejects_non_finite_or_negative_byte_benchmarks(value) -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    with pytest.raises(PlanBuildError):
        build_plan(
            manifest,
            now_utc=datetime(2025, 7, 20, tzinfo=UTC),
            catalog_snapshot={},
            benchmark_bytes_per_session={"fake_a": value},
            benchmark_seconds_per_session={"fake_a": 1.0},
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_build_plan_rejects_non_finite_or_negative_second_benchmarks(value) -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    with pytest.raises(PlanBuildError):
        build_plan(
            manifest,
            now_utc=datetime(2025, 7, 20, tzinfo=UTC),
            catalog_snapshot={},
            benchmark_bytes_per_session={"fake_a": 1.0},
            benchmark_seconds_per_session={"fake_a": value},
        )


def test_build_plan_rejects_a_non_numeric_benchmark() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    with pytest.raises(PlanBuildError):
        build_plan(
            manifest,
            now_utc=datetime(2025, 7, 20, tzinfo=UTC),
            catalog_snapshot={},
            benchmark_bytes_per_session={"fake_a": "not-a-number"},
            benchmark_seconds_per_session={"fake_a": 1.0},
        )


def test_build_plan_accepts_a_zero_benchmark() -> None:
    """Zero é finito e não negativo -- válido."""

    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 0.0},
        benchmark_seconds_per_session={"fake_a": 0.0},
    )
    assert plan.estimated_bytes_remaining == 0
    assert plan.estimated_seconds_remaining == 0.0


# --- assert_plan_matches_manifest: vínculo completo, item a item (2ª auditoria Codex) --


def test_assert_plan_matches_manifest_accepts_a_freshly_built_plan() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    assert_plan_matches_manifest(plan, manifest)  # não levanta


def test_assert_plan_matches_manifest_rejects_a_tampered_requested_symbol() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    tampered_item = PlanItem(
        logical_id="fake_a",
        requested_symbol="NOT-AUTHORIZED",
        session_date=plan.items[0].session_date,
        status="planned",
        catalog_state=None,
    )
    tampered_plan = _replace_items(plan, (tampered_item,))
    with pytest.raises(PlanBuildError, match="requested_symbol"):
        assert_plan_matches_manifest(tampered_plan, manifest)


def test_assert_plan_matches_manifest_rejects_an_unauthorized_logical_id() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    rogue_item = PlanItem(
        logical_id="rogue_asset",
        requested_symbol="ROGUE",
        session_date=plan.items[0].session_date,
        status="planned",
        catalog_state=None,
    )
    tampered_plan = _replace_items(plan, (rogue_item,))
    with pytest.raises(PlanBuildError, match="fora do escopo"):
        assert_plan_matches_manifest(tampered_plan, manifest)


def test_assert_plan_matches_manifest_rejects_a_duplicate_item() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    tampered_plan = _replace_items(plan, (plan.items[0], plan.items[0]))
    with pytest.raises(PlanBuildError, match="duplicado"):
        assert_plan_matches_manifest(tampered_plan, manifest)


def test_assert_plan_matches_manifest_rejects_a_date_outside_the_resolved_interval() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    out_of_range_item = PlanItem(
        logical_id="fake_a",
        requested_symbol="FAKEA",
        session_date=date(2025, 7, 25),  # bem além de resolved_end_date=07-16
        status="planned",
        catalog_state=None,
    )
    tampered_plan = _replace_items(plan, (out_of_range_item,))
    with pytest.raises(PlanBuildError, match="fora do intervalo resolvido"):
        assert_plan_matches_manifest(tampered_plan, manifest)


def test_assert_plan_matches_manifest_rejects_a_weekend_date_within_the_nominal_range() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 21),  # abrange o fim de semana 07-19/07-20
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 25, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    weekend_item = PlanItem(
        logical_id="fake_a",
        requested_symbol="FAKEA",
        session_date=date(2025, 7, 19),  # sábado, dentro do intervalo numérico
        status="planned",
        catalog_state=None,
    )
    tampered_plan = _replace_items(plan, (weekend_item,))
    with pytest.raises(PlanBuildError, match="dia candidato"):
        assert_plan_matches_manifest(tampered_plan, manifest)


def test_assert_plan_matches_manifest_rejects_a_tampered_execution_order() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
        execution_order="oldest_first",
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    tampered_plan = replace(plan, execution_order="newest_first")
    with pytest.raises(PlanBuildError, match="execution_order"):
        assert_plan_matches_manifest(tampered_plan, manifest)


def test_assert_plan_matches_manifest_rejects_a_tampered_resolved_start_date() -> None:
    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0},
    )
    tampered_plan = replace(plan, resolved_start_date=date(2025, 7, 7))
    with pytest.raises(PlanBuildError, match="resolved_start_date"):
        assert_plan_matches_manifest(tampered_plan, manifest)


def test_assert_plan_matches_manifest_rejects_an_extended_resolved_end_date_for_explicit_policy() -> None:
    """Reproduz o achado da 3ª auditoria Codex: manifesto `explicit` com
    `end_date=2025-07-15`; `resolved_end_date` adulterado para 2025-07-16 (com
    um item WIN$ nessa data) precisa ser recusado -- e a rejeição acontece nos
    metadados de topo, antes mesmo do item extra ser conferido."""

    manifest = _manifest(
        assets=(_asset("win", "WIN$"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 15),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"win": 1.0},
        benchmark_seconds_per_session={"win": 1.0},
    )
    extra_item = PlanItem(
        logical_id="win",
        requested_symbol="WIN$",
        session_date=date(2025, 7, 16),
        status="planned",
        catalog_state=None,
    )
    tampered_items = (*plan.items, extra_item)
    tampered_plan = replace(
        plan, resolved_end_date=date(2025, 7, 16), items=tampered_items, totals=_totals(tampered_items)
    )

    with pytest.raises(PlanBuildError, match="resolved_end_date"):
        assert_plan_matches_manifest(tampered_plan, manifest)


def test_assert_plan_matches_manifest_rejects_a_latest_closed_resolved_end_date_beyond_the_currently_closed_session() -> None:
    """`latest_closed` nunca aceita `resolved_end_date` posterior à última
    sessão fechada no momento da validação -- `now_utc` é determinístico e
    injetado explicitamente pelo teste."""

    manifest = _manifest(
        assets=(_asset("win", "WIN$"),),
        start_date=date(2025, 7, 14),
        end_date_policy="latest_closed",
    )
    # Quinta 2025-07-17, 10:00 America/Sao_Paulo (13:00 UTC) -- antes do
    # fechamento (19:00): a última sessão fechada é quarta, 2025-07-16.
    now_utc = datetime(2025, 7, 17, 13, 0, tzinfo=UTC)
    plan = build_plan(
        manifest,
        now_utc=now_utc,
        catalog_snapshot={},
        benchmark_bytes_per_session={"win": 1.0},
        benchmark_seconds_per_session={"win": 1.0},
    )
    assert plan.resolved_end_date == date(2025, 7, 16)

    # Adultera resolved_end_date para o próprio dia de "agora" -- que ainda
    # não fechou no momento da validação.
    tampered_plan = replace(plan, resolved_end_date=date(2025, 7, 17))

    with pytest.raises(PlanBuildError, match="posterior"):
        assert_plan_matches_manifest(tampered_plan, manifest, now_utc=now_utc)


def test_assert_plan_matches_manifest_accepts_latest_closed_resolved_end_date_equal_to_the_currently_closed_session() -> None:
    """Controle positivo: um plano corretamente construído (`resolved_end_date`
    igual à última sessão fechada em `now_utc`) continua aceito -- a correção
    não é mais restritiva do que o necessário."""

    manifest = _manifest(
        assets=(_asset("win", "WIN$"),),
        start_date=date(2025, 7, 14),
        end_date_policy="latest_closed",
    )
    now_utc = datetime(2025, 7, 17, 13, 0, tzinfo=UTC)
    plan = build_plan(
        manifest,
        now_utc=now_utc,
        catalog_snapshot={},
        benchmark_bytes_per_session={"win": 1.0},
        benchmark_seconds_per_session={"win": 1.0},
    )
    assert_plan_matches_manifest(plan, manifest, now_utc=now_utc)  # não levanta


def test_assert_plan_matches_manifest_rejects_reversed_items_against_execution_order() -> None:
    """Reproduz o achado da 3ª auditoria Codex: inverter `plan.items` é
    recusado mesmo com `execution_order` intacto -- a sequência real dos
    itens precisa respeitar a grade autorizada (datas na ordem declarada)."""

    manifest = _manifest(
        assets=(_asset("win", "WIN$"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
        execution_order="oldest_first",
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"win": 1.0},
        benchmark_seconds_per_session={"win": 1.0},
    )
    assert [item.session_date for item in plan.items] == [
        date(2025, 7, 14),
        date(2025, 7, 15),
        date(2025, 7, 16),
    ]
    reversed_plan = _replace_items(plan, tuple(reversed(plan.items)))

    with pytest.raises(PlanBuildError, match="ordem"):
        assert_plan_matches_manifest(reversed_plan, manifest)


def test_assert_plan_matches_manifest_rejects_assets_swapped_within_the_same_date() -> None:
    """A ordem declarada dos ativos por data também é parte da grade
    autorizada -- trocar dois ativos do mesmo dia é uma reordenação, não
    apenas um conjunto diferente."""

    manifest = _manifest(
        assets=(_asset("fake_a", "FAKEA"), _asset("fake_b", "FAKEB")),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 14),
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"fake_a": 1.0, "fake_b": 1.0},
        benchmark_seconds_per_session={"fake_a": 1.0, "fake_b": 1.0},
    )
    assert [item.logical_id for item in plan.items] == ["fake_a", "fake_b"]
    swapped_plan = _replace_items(plan, tuple(reversed(plan.items)))

    with pytest.raises(PlanBuildError, match="ordem"):
        assert_plan_matches_manifest(swapped_plan, manifest)


def test_assert_plan_matches_manifest_accepts_a_legitimate_subset_that_preserves_order() -> None:
    """Um subconjunto da grade autorizada continua aceito, desde que a ordem
    relativa dos itens restantes não seja alterada."""

    manifest = _manifest(
        assets=(_asset("win", "WIN$"),),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 16),
        execution_order="oldest_first",
    )
    plan = build_plan(
        manifest,
        now_utc=datetime(2025, 7, 20, tzinfo=UTC),
        catalog_snapshot={},
        benchmark_bytes_per_session={"win": 1.0},
        benchmark_seconds_per_session={"win": 1.0},
    )
    subset_plan = _replace_items(plan, (plan.items[0], plan.items[2]))

    assert_plan_matches_manifest(subset_plan, manifest)  # não levanta


# --- pureza de import ---------------------------------------------------------


def test_backfill_plan_module_does_not_import_mt5_or_gui() -> None:
    script = (
        "import sys\n"
        "import market_analytics.backfill_plan\n"
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
