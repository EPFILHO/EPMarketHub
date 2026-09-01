"""Cobertura das regras puras da GUI genérica de backfill (DEV-004 — C3.1).

Testa só as funções de nível de módulo de `tools/manifest_backfill_gui.py`
(mesmo padrão de `tests/test_b3_contract_capture_gui.py`): nenhuma janela Tk é
instanciada. Nenhum teste toca `D:\\EPData`, MT5/Clear/FOT nem inicia
qualquer backfill real -- o único manifesto usado (`c3-win-clear`) permanece
`planning`, e `ADAPTER_FACTORY` é sempre `None` a não ser quando um teste o
substitui explicitamente por um fake via monkeypatch.

A GUI só abre manifestos registrados/aprovados (`open_known_manifest` ->
`manifest_registry.load_known_manifest`); nenhum teste aqui exercita um
caminho de arquivo arbitrário, porque esse caminho não existe mais na GUI
(auditoria Codex do DEV-004, achado 6).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time

import pytest

import tools.manifest_backfill_gui as gui
from market_analytics.backfill_plan import PlanBuildError, PlanItem, load_plan
from market_analytics.manifest import BackfillManifest, ManifestAsset, ManifestValidationError
from market_analytics.manifest_registry import UnknownManifestError


def _manifest(
    authorization_state: str, *, logical_id: str = "win", manifest_id: str = "fake-manifest", **overrides
) -> BackfillManifest:
    base = dict(
        manifest_id=manifest_id,
        display_name="Manifesto fictício de teste",
        work_order="DEV-004",
        source_id="fake_source",
        session_timezone="America/Sao_Paulo",
        session_close_local_time=time(19, 0),
        assets=(
            ManifestAsset(
                logical_id=logical_id,
                requested_symbol=logical_id.upper(),
                provenance=(("series_kind", "continuous"),),
            ),
        ),
        start_date=date(2025, 7, 14),
        end_date_policy="explicit",
        end_date=date(2025, 7, 15),
        execution_order="oldest_first",
        chunk_seconds=900,
        max_attempts=3,
        concurrency=1,
        max_total_bytes=1_000_000_000,
        max_total_duration_seconds=3_600,
        authorization_state=authorization_state,
    )
    base.update(overrides)
    return BackfillManifest(**base)


NOW_UTC = datetime(2025, 7, 21, 12, 0, tzinfo=UTC)  # segunda, bem depois do fechamento de sexta


# --- banner de autorização ------------------------------------------------------


def test_planning_banner_text_for_each_authorization_state() -> None:
    assert gui.planning_banner_text(_manifest("planning")) == "PLANEJAMENTO — EXECUÇÃO BLOQUEADA"
    assert (
        gui.planning_banner_text(_manifest("preflight_approved"))
        == "PREFLIGHT APROVADO — EXECUÇÃO AINDA BLOQUEADA"
    )
    assert gui.planning_banner_text(_manifest("execution_approved")) == "EXECUÇÃO APROVADA"


# --- botão de execução: planning nunca habilita execução -----------------------


def test_execute_button_is_disabled_for_planning_even_with_an_adapter_factory_registered(monkeypatch) -> None:
    monkeypatch.setattr(gui, "ADAPTER_FACTORY", lambda manifest: object())
    assert gui.execute_button_state(_manifest("planning")) == "disabled"


def test_execute_button_is_disabled_for_preflight_approved_too(monkeypatch) -> None:
    monkeypatch.setattr(gui, "ADAPTER_FACTORY", lambda manifest: object())
    assert gui.execute_button_state(_manifest("preflight_approved")) == "disabled"


def test_execute_button_stays_disabled_for_execution_approved_without_a_registered_adapter() -> None:
    assert gui.ADAPTER_FACTORY is None
    assert gui.execute_button_state(_manifest("execution_approved")) == "disabled"


def test_execute_button_only_enables_for_execution_approved_with_a_registered_adapter(monkeypatch) -> None:
    monkeypatch.setattr(gui, "ADAPTER_FACTORY", lambda manifest: object())
    assert gui.execute_button_state(_manifest("execution_approved")) == "normal"


# --- catálogo abstrato/falso -----------------------------------------------------


def test_build_fake_catalog_snapshot_never_marks_anything_as_known() -> None:
    manifest = _manifest("planning")
    assert gui.build_fake_catalog_snapshot(manifest) == {}


# --- estimativas ilustrativas -----------------------------------------------------


def test_missing_benchmark_logical_ids_flags_assets_without_an_illustrative_estimate() -> None:
    manifest = _manifest("planning", logical_id="fictitious_asset")
    assert gui.missing_benchmark_logical_ids(manifest) == ["fictitious_asset"]


def test_missing_benchmark_logical_ids_is_empty_for_win() -> None:
    manifest = _manifest("planning", logical_id="win")
    assert gui.missing_benchmark_logical_ids(manifest) == []


def test_generate_plan_refuses_a_manifest_without_a_registered_benchmark() -> None:
    manifest = _manifest("planning", logical_id="fictitious_asset")
    with pytest.raises(ManifestValidationError):
        gui.generate_plan(manifest, now_utc=NOW_UTC)


# --- abertura restrita ao registro de manifestos --------------------------------


def test_open_known_manifest_returns_the_approved_c3_win_manifest() -> None:
    manifest = gui.open_known_manifest("c3-win-clear")
    assert manifest.manifest_id == "c3-win-clear"
    assert manifest.source_id == "clear"
    assert manifest.authorization_state == "planning"


def test_open_known_manifest_rejects_an_unknown_manifest_id() -> None:
    with pytest.raises(UnknownManifestError):
        gui.open_known_manifest("c3-wdo-clear")


# --- geração do plano exibido pela GUI (manifesto real C3-WIN, catálogo falso) --


def test_generate_plan_for_the_approved_c3_win_manifest_stays_in_planning() -> None:
    manifest = gui.open_known_manifest("c3-win-clear")
    plan = gui.generate_plan(manifest, now_utc=NOW_UTC)

    assert manifest.authorization_state == "planning"
    assert plan.execution_authorized is False
    assert plan.resolved_start_date == date(2025, 7, 14)
    assert all(item.status == "planned" for item in plan.items)  # catálogo falso: nada é conhecido


def test_format_summary_lines_surfaces_source_manifest_fingerprint_and_state() -> None:
    manifest = gui.open_known_manifest("c3-win-clear")
    plan = gui.generate_plan(manifest, now_utc=NOW_UTC)

    lines = gui.format_summary_lines(manifest, plan)
    joined = "\n".join(lines)
    assert "Fonte: clear" in joined
    assert plan.manifest_fingerprint in joined
    assert "planning" in joined
    assert "win (WIN$)" in joined
    assert "bloqueadas por limite" in joined


def test_format_queue_rows_reflects_every_planned_item() -> None:
    manifest = gui.open_known_manifest("c3-win-clear")
    plan = gui.generate_plan(manifest, now_utc=NOW_UTC)

    rows = gui.format_queue_rows(plan)
    assert len(rows) == len(plan.items)
    assert rows[0] == (
        plan.items[0].logical_id,
        plan.items[0].session_date.isoformat(),
        plan.items[0].status,
        "-",
    )


# --- velocidade/ETA sem divisão por zero -----------------------------------------


def test_progress_speed_eta_text_reports_unknown_speed_without_any_progress() -> None:
    text = gui.progress_speed_eta_text(
        completed_count=0, elapsed_seconds=0.0, remaining_count=5, chunk_seconds=900
    )
    assert "velocidade desconhecida" in text
    assert "desconhecido" in text


def test_progress_speed_eta_text_reports_a_computed_speed_and_eta() -> None:
    text = gui.progress_speed_eta_text(
        completed_count=2, elapsed_seconds=10.0, remaining_count=4, chunk_seconds=900
    )
    assert "sessões/s" in text
    assert "ETA: 20s" in text


# --- relatório de plano: atômico e reabrível em tmp_path -------------------------


def test_write_plan_report_persists_a_timestamped_and_a_latest_copy(tmp_path) -> None:
    manifest = gui.open_known_manifest("c3-win-clear")
    plan = gui.generate_plan(manifest, now_utc=NOW_UTC)

    report_path = gui.write_plan_report(tmp_path, plan)

    assert report_path.exists()
    latest_path = tmp_path / f"{plan.manifest_id}_latest.json"
    assert latest_path.exists()

    reloaded = load_plan(report_path)
    assert reloaded == plan
    reloaded_latest = load_plan(latest_path)
    assert reloaded_latest == plan


# --- reabertura de plano: sempre revalidada contra o manifesto atual ------------


def test_reopen_plan_accepts_a_plan_that_still_matches_the_current_manifest(tmp_path) -> None:
    manifest = _manifest("planning", logical_id="win")
    plan = gui.generate_plan(manifest, now_utc=NOW_UTC)
    report_path = gui.write_plan_report(tmp_path, plan)

    reopened = gui.reopen_plan(report_path, manifest)

    assert reopened == plan


def test_reopen_plan_rejects_a_plan_from_a_different_manifest_id(tmp_path) -> None:
    manifest_a = _manifest("planning", logical_id="win", manifest_id="fake-manifest-a")
    manifest_b = _manifest("planning", logical_id="win", manifest_id="fake-manifest-b")
    plan_from_a = gui.generate_plan(manifest_a, now_utc=NOW_UTC)
    report_path = gui.write_plan_report(tmp_path, plan_from_a)

    with pytest.raises(PlanBuildError):
        gui.reopen_plan(report_path, manifest_b)


def test_reopen_plan_rejects_a_plan_with_a_stale_fingerprint_for_the_same_manifest_id(tmp_path) -> None:
    """Mesmo `manifest_id`, mas o manifesto mudou (aqui: chunk_seconds) desde
    que o plano foi gerado -- o fingerprint diverge e o plano é recusado."""

    old_manifest = _manifest("planning", logical_id="win", manifest_id="fake-manifest", chunk_seconds=900)
    plan_from_old = gui.generate_plan(old_manifest, now_utc=NOW_UTC)
    report_path = gui.write_plan_report(tmp_path, plan_from_old)

    new_manifest = _manifest("planning", logical_id="win", manifest_id="fake-manifest", chunk_seconds=300)

    with pytest.raises(PlanBuildError):
        gui.reopen_plan(report_path, new_manifest)


def test_reopen_plan_rejects_a_plan_with_a_tampered_item_requested_symbol(tmp_path) -> None:
    """`reopen_plan` usa a MESMA validação completa item a item que `run_plan`
    aplica antes de tocar o adaptador -- um `requested_symbol` fora do
    autorizado é recusado mesmo com os metadados de topo do plano intactos
    (DEV-004, 2ª auditoria Codex)."""

    manifest = _manifest("planning", logical_id="win")
    plan = gui.generate_plan(manifest, now_utc=NOW_UTC)
    first = plan.items[0]
    tampered_first = PlanItem(
        logical_id=first.logical_id,
        requested_symbol="NOT-AUTHORIZED",
        session_date=first.session_date,
        status=first.status,
        catalog_state=first.catalog_state,
    )
    tampered_items = (tampered_first, *plan.items[1:])
    tampered_totals = dict(plan.totals)  # status inalterado -- só o símbolo do 1º item mudou
    tampered_plan = replace(plan, items=tampered_items, totals=tampered_totals)
    report_path = gui.write_plan_report(tmp_path, tampered_plan)

    with pytest.raises(PlanBuildError, match="requested_symbol"):
        gui.reopen_plan(report_path, manifest)


def test_reopen_plan_rejects_a_plan_with_an_extended_resolved_end_date(tmp_path) -> None:
    """Reproduz o achado da 3ª auditoria Codex também no caminho de
    reabertura: manifesto `explicit` com `end_date=2025-07-15` (default deste
    helper); `resolved_end_date` adulterado para 2025-07-16, com um item
    extra nessa data, precisa ser recusado."""

    manifest = _manifest("planning", logical_id="win")  # end_date padrão = 2025-07-15
    plan = gui.generate_plan(manifest, now_utc=NOW_UTC)
    extra_item = PlanItem(
        logical_id=plan.items[0].logical_id,
        requested_symbol=plan.items[0].requested_symbol,
        session_date=date(2025, 7, 16),
        status="planned",
        catalog_state=None,
    )
    tampered_items = (*plan.items, extra_item)
    tampered_totals = dict(plan.totals)
    tampered_totals["sessions_total"] += 1
    tampered_totals["planned"] += 1
    tampered_plan = replace(
        plan, resolved_end_date=date(2025, 7, 16), items=tampered_items, totals=tampered_totals
    )
    report_path = gui.write_plan_report(tmp_path, tampered_plan)

    with pytest.raises(PlanBuildError, match="resolved_end_date"):
        gui.reopen_plan(report_path, manifest)


def test_reopen_plan_rejects_a_plan_with_reversed_items(tmp_path) -> None:
    """Reproduz o achado da 3ª auditoria Codex também no caminho de
    reabertura: inverter `plan.items` é recusado mesmo com `execution_order`
    intacto."""

    manifest = _manifest("planning", logical_id="win", end_date=date(2025, 7, 16))
    plan = gui.generate_plan(manifest, now_utc=NOW_UTC)
    assert len(plan.items) == 3
    reversed_plan = replace(plan, items=tuple(reversed(plan.items)))
    report_path = gui.write_plan_report(tmp_path, reversed_plan)

    with pytest.raises(PlanBuildError, match="ordem"):
        gui.reopen_plan(report_path, manifest)


# --- pureza/ausência de campo livre de manifesto --------------------------------


def test_gui_module_has_no_free_form_manifest_file_picker() -> None:
    """Regressão do achado 6 da auditoria Codex: a GUI não pode mais oferecer
    nenhum jeito de carregar um manifesto por caminho de arquivo arbitrário."""

    assert not hasattr(gui, "open_manifest_dialog")
    assert not hasattr(gui, "load_and_validate_manifest")
