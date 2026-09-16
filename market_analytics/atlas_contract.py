"""Contrato versionado do Atlas causal do EP Market Hub (DEV-008A).

Declara os nomes de schema do Hub e a compatibilidade explícita com o
contrato v1 estabilizado no Fusion Quant (`fusion_quant.market_atlas`,
DEV-011D.1/D.2, execução real auditada no commit `424cf66`). Este módulo não
recalcula nada de `causal_atlas.py`; ele só rotula/adapta os dicionários já
produzidos. Nenhum dado real do Fusion Quant é lido, importado ou versionado
aqui — a equivalência é provada por fixture sintética golden (ver
`tests/test_causal_atlas.py`) e auditada localmente pelo proprietário/Codex
contra a execução real, fora deste repositório.

A adaptação só é garantida quando o `AtlasManifest` usa as escadas padrão do
DEV-011D (checkpoints 15/30/60/120/240, thresholds percentuais
0,25/0,50/0,75/1,00%, múltiplos de ATR 0,5/1,0, faixa inicial 15/30 min,
janelas de percentil de ATR 20/60 e horizontes de evento 15/30/60 min). Um
manifesto que se desvie dessas escadas produz colunas com nomes diferentes
(ex.: `opening_range_45_state`) e a adaptação falha fechado em vez de
inventar um mapeamento.
"""
from __future__ import annotations

from typing import Any

from .causal_atlas import (
    ATR_EVENT_MULTIPLES_DEFAULT,
    ATR_PERCENTILE_WINDOWS_DEFAULT,
    CAUSAL_ATLAS_CORE_VERSION,
    CHECKPOINT_MINUTES_DEFAULT,
    EVENT_OUTCOME_HORIZONS_MINUTES_DEFAULT,
    OPENING_RANGE_MINUTES_DEFAULT,
    PCT_EVENT_THRESHOLDS_DEFAULT,
    AtlasManifest,
    CausalAtlasError,
    CausalAtlasResult,
)

HUB_ATLAS_CONTRACT_VERSION = 1

SCHEMA_RESULT = "ep_market_hub.atlas.result.v1"
SCHEMA_SESSION_ROWS = "ep_market_hub.atlas.session_rows.v1"
SCHEMA_CHECKPOINT_ROWS = "ep_market_hub.atlas.checkpoint_rows.v1"
SCHEMA_EVENT_ROWS = "ep_market_hub.atlas.event_rows.v1"
SCHEMA_OUTSIDE_CORE_ROWS = "ep_market_hub.atlas.outside_core_rows.v1"
SCHEMA_QUALITY_ISSUE_ROWS = "ep_market_hub.atlas.quality_issue_rows.v1"

# Schemas espelhados do contrato v1 estabilizado no Fusion Quant
# (`fusion_quant.market_atlas`). Usados somente como rótulo de saída do
# adaptador de compatibilidade abaixo — nunca lidos de um pacote externo.
FUSION_QUANT_V1_SESSION_ROWS = "fusion-quant-market-session-rows-v1"
FUSION_QUANT_V1_CHECKPOINT_ROWS = "fusion-quant-market-checkpoint-rows-v1"
FUSION_QUANT_V1_EVENT_ROWS = "fusion-quant-market-event-rows-v1"
FUSION_QUANT_V1_OUTSIDE_CORE_ROWS = "fusion-quant-market-outside-core-rows-v1"

# Campos do contrato v1 do Fusion Quant para as escadas padrão do DEV-011D.
# Extraídos de `fusion_quant/market_atlas.py` (build_session_table/
# build_checkpoint_table/build_event_table). A adaptação recusa produzir uma
# linha cujo conjunto de campos (menos `schema`) não bata exatamente com um
# destes conjuntos — divergência de escada vira erro, não campo ausente
# silencioso.
FUSION_QUANT_V1_SESSION_FIELDS: frozenset[str] = frozenset(
    {
        "source_id", "source_transition", "symbol", "series_kind", "source_symbol",
        "adjustment_method", "contract_id", "session_date", "available_at_utc",
        "session_open_time_utc", "session_close_time_utc", "session_close_available_at_utc",
        "expected_session_open_time_utc", "expected_session_close_time_utc", "bar_count",
        "expected_bar_count", "is_first_session", "eligible", "eligible_for_aggregates",
        "eligibility_reason", "volume_quality", "session_open", "session_high", "session_low",
        "previous_close", "previous_true_range", "previous_true_range_over_open_pct",
        "previous_session_eligible", "previous_session_volatility_state",
        "previous_session_volatility_state_streak", "previous_session_return_close_open_pct",
        "previous_session_close_position_in_range", "previous_session_efficiency_ratio",
        "previous_session_day_type", "previous_session_eligible_for_aggregates",
        "atr14_previous_session", "atr14_previous_session_pct",
        "atr14_previous_session_window_reason",
        *(f"atr_percentile_{window}" for window in ATR_PERCENTILE_WINDOWS_DEFAULT),
        "true_range_over_atr_previous", "true_range_context_quality",
        "true_range_context_quality_reason", "gap_points", "gap_pct", "gap_sign", "gap_band",
        "gap_over_previous_atr", "gap_quality", "gap_quality_reason", "label_session_close",
        "label_return_close_open_pct", "label_return_close_close_pct", "label_true_range",
        "label_true_range_pct", "label_high_time_utc", "label_low_time_utc",
        "label_close_position_in_range", "label_efficiency_ratio",
        "label_maximum_up_excursion_pct", "label_maximum_down_excursion_pct", "label_day_type",
        "label_day_type_rule_version", "label_volatility_state",
    }
)

FUSION_QUANT_V1_CHECKPOINT_FIELDS: frozenset[str] = frozenset(
    {
        "source_id", "symbol", "series_kind", "source_symbol", "adjustment_method",
        "contract_id", "session_date", "checkpoint_minutes", "checkpoint_time_utc",
        "available_at_utc", "session_eligible", "session_eligible_for_aggregates", "available",
        "unavailable_reason", "bars_used", "high_so_far", "low_so_far", "range_so_far",
        "true_range_so_far", "range_so_far_over_atr_previous", "range_so_far_percentile_20",
        "range_so_far_percentile_60", "return_from_open_pct", "return_from_open_atr",
        "realized_volatility_so_far", "realized_volatility_so_far_percentile_60",
        "efficiency_ratio_so_far", "close_position_so_far", "volume_so_far",
        "volume_relative_so_far", "volume_relative_so_far_null_reason",
        *(f"opening_range_{minutes}_state" for minutes in OPENING_RANGE_MINUTES_DEFAULT),
    }
)

_EVENT_OUTCOME_FIELDS: frozenset[str] = frozenset(
    {
        *(f"label_return_{horizon}m_pct" for horizon in EVENT_OUTCOME_HORIZONS_MINUTES_DEFAULT),
        *(f"label_return_{horizon}m_null_reason" for horizon in EVENT_OUTCOME_HORIZONS_MINUTES_DEFAULT),
        "label_return_to_close_pct", "label_mfe_pct", "label_mae_pct",
        "label_time_to_extreme_after_minutes", "label_continued", "label_reversed",
    }
)

FUSION_QUANT_V1_EVENT_BASE_FIELDS: frozenset[str] = frozenset(
    {
        "source_id", "symbol", "series_kind", "source_symbol", "adjustment_method",
        "contract_id", "session_date", "session_eligible", "session_eligible_for_aggregates",
        "event_type", "event_id", "direction", "anchor_price", "level", "triggered",
        "evaluable", "not_evaluable_reason", "event_bar_timestamp_utc", "available_at_utc",
        "first_cross_time_utc", "bars_to_cross",
    }
)
FUSION_QUANT_V1_EVENT_FIELDS_TRIGGERED: frozenset[str] = (
    FUSION_QUANT_V1_EVENT_BASE_FIELDS | _EVENT_OUTCOME_FIELDS
)


def _uses_default_ladders(manifest: AtlasManifest) -> bool:
    return (
        tuple(manifest.checkpoint_minutes) == CHECKPOINT_MINUTES_DEFAULT
        and tuple(manifest.pct_event_thresholds) == PCT_EVENT_THRESHOLDS_DEFAULT
        and tuple(manifest.atr_event_multiples) == ATR_EVENT_MULTIPLES_DEFAULT
        and tuple(manifest.opening_range_minutes) == OPENING_RANGE_MINUTES_DEFAULT
        and tuple(manifest.atr_percentile_windows) == ATR_PERCENTILE_WINDOWS_DEFAULT
        and tuple(manifest.event_outcome_horizons_minutes) == EVENT_OUTCOME_HORIZONS_MINUTES_DEFAULT
    )


def _adapt_row(row: dict[str, Any], expected_fields: frozenset[str], schema: str, label: str) -> dict[str, Any]:
    actual_fields = frozenset(row)
    if actual_fields != expected_fields:
        missing = sorted(expected_fields - actual_fields)
        extra = sorted(actual_fields - expected_fields)
        raise CausalAtlasError(
            f"{label}: linha incompatível com o contrato v1 do Fusion Quant "
            f"(campos ausentes={missing}, campos extras={extra})"
        )
    return {"schema": schema, **row}


def _hub_session_fields(manifest: AtlasManifest) -> frozenset[str]:
    default_dynamic = {
        f"atr_percentile_{window}" for window in ATR_PERCENTILE_WINDOWS_DEFAULT
    }
    return (FUSION_QUANT_V1_SESSION_FIELDS - default_dynamic) | {
        f"atr_percentile_{window}" for window in manifest.atr_percentile_windows
    }


def _hub_checkpoint_fields(manifest: AtlasManifest) -> frozenset[str]:
    default_dynamic = {
        f"opening_range_{minutes}_state" for minutes in OPENING_RANGE_MINUTES_DEFAULT
    }
    return (FUSION_QUANT_V1_CHECKPOINT_FIELDS - default_dynamic) | {
        f"opening_range_{minutes}_state" for minutes in manifest.opening_range_minutes
    }


def _hub_triggered_event_fields(manifest: AtlasManifest) -> frozenset[str]:
    return FUSION_QUANT_V1_EVENT_BASE_FIELDS | {
        *(f"label_return_{horizon}m_pct" for horizon in manifest.event_outcome_horizons_minutes),
        *(
            f"label_return_{horizon}m_null_reason"
            for horizon in manifest.event_outcome_horizons_minutes
        ),
        "label_return_to_close_pct",
        "label_mfe_pct",
        "label_mae_pct",
        "label_time_to_extreme_after_minutes",
        "label_continued",
        "label_reversed",
    }


def to_hub_contract(result: CausalAtlasResult) -> dict[str, Any]:
    """Serializa o resultado puro no contrato público v1 do Hub.

    As tabelas mantêm exatamente os campos estabilizados no Fusion Quant,
    acrescentando somente o schema próprio do Hub. Problemas de qualidade
    possuem contrato menor e explícito. Metadados/hashes ficam no envelope,
    nunca repetidos em milhões de linhas.
    """

    quality_fields = frozenset({"session_date", "issue_type", "detail"})
    payload = {
        "schema": SCHEMA_RESULT,
        "contract_version": HUB_ATLAS_CONTRACT_VERSION,
        "core_version": result.core_version,
        "clock_policy": result.clock_policy,
        "m1_availability_offset_minutes": result.m1_availability_offset_minutes,
        "params_sha256": result.params_sha256,
        "bars_sha256": result.bars_sha256,
        "bar_count": result.bar_count,
        "manifest": result.manifest.to_dict(),
        "summary": result.summary,
        "session_rows": [
            _adapt_row(
                row, _hub_session_fields(result.manifest), SCHEMA_SESSION_ROWS, "session_row"
            )
            for row in result.session_rows
        ],
        "checkpoint_rows": [
            _adapt_row(
                row,
                _hub_checkpoint_fields(result.manifest),
                SCHEMA_CHECKPOINT_ROWS,
                "checkpoint_row",
            )
            for row in result.checkpoint_rows
        ],
        "event_rows": [],
        "outside_core_rows": [],
        "quality_issue_rows": [
            _adapt_row(row, quality_fields, SCHEMA_QUALITY_ISSUE_ROWS, "quality_issue_row")
            for row in result.quality_issue_rows
        ],
    }
    for row in result.event_rows:
        expected = (
            _hub_triggered_event_fields(result.manifest)
            if row["triggered"]
            else FUSION_QUANT_V1_EVENT_BASE_FIELDS
        )
        payload["event_rows"].append(
            _adapt_row(row, expected, SCHEMA_EVENT_ROWS, "event_row")
        )
    outside_fields = frozenset(
        {
            "source_id", "symbol", "session_date", "timestamp_utc", "available_at_utc",
            "open", "high", "low", "close", "volume_quality",
            "outside_core_session_reason",
        }
    )
    payload["outside_core_rows"] = [
        _adapt_row(row, outside_fields, SCHEMA_OUTSIDE_CORE_ROWS, "outside_core_row")
        for row in result.outside_core_rows
    ]
    return payload


def to_fusion_quant_v1_session_rows(result: CausalAtlasResult) -> list[dict[str, Any]]:
    """Adapta `result.session_rows` para o contrato v1 do Fusion Quant.

    Só aceito quando `result.manifest` usa as escadas padrão do DEV-011D
    (ver docstring do módulo); caso contrário falha fechado.
    """

    if not _uses_default_ladders(result.manifest):
        raise CausalAtlasError(
            "adaptação para o contrato v1 do Fusion Quant exige as escadas padrão do "
            "DEV-011D no AtlasManifest (checkpoints/thresholds/opening_range/atr_percentile/"
            "horizontes); manifesto usa uma configuração diferente"
        )
    return [
        _adapt_row(row, FUSION_QUANT_V1_SESSION_FIELDS, FUSION_QUANT_V1_SESSION_ROWS, "session_row")
        for row in result.session_rows
    ]


def to_fusion_quant_v1_checkpoint_rows(result: CausalAtlasResult) -> list[dict[str, Any]]:
    if not _uses_default_ladders(result.manifest):
        raise CausalAtlasError(
            "adaptação para o contrato v1 do Fusion Quant exige as escadas padrão do "
            "DEV-011D no AtlasManifest; manifesto usa uma configuração diferente"
        )
    return [
        _adapt_row(row, FUSION_QUANT_V1_CHECKPOINT_FIELDS, FUSION_QUANT_V1_CHECKPOINT_ROWS, "checkpoint_row")
        for row in result.checkpoint_rows
    ]


def to_fusion_quant_v1_event_rows(result: CausalAtlasResult) -> list[dict[str, Any]]:
    if not _uses_default_ladders(result.manifest):
        raise CausalAtlasError(
            "adaptação para o contrato v1 do Fusion Quant exige as escadas padrão do "
            "DEV-011D no AtlasManifest; manifesto usa uma configuração diferente"
        )
    adapted = []
    for row in result.event_rows:
        expected = (
            FUSION_QUANT_V1_EVENT_FIELDS_TRIGGERED
            if row["triggered"]
            else FUSION_QUANT_V1_EVENT_BASE_FIELDS
        )
        adapted.append(_adapt_row(row, expected, FUSION_QUANT_V1_EVENT_ROWS, "event_row"))
    return adapted


def to_fusion_quant_v1_outside_core_rows(result: CausalAtlasResult) -> list[dict[str, Any]]:
    expected = frozenset(
        {
            "source_id", "symbol", "session_date", "timestamp_utc", "available_at_utc",
            "open", "high", "low", "close", "volume_quality", "outside_core_session_reason",
        }
    )
    return [
        _adapt_row(row, expected, FUSION_QUANT_V1_OUTSIDE_CORE_ROWS, "outside_core_row")
        for row in result.outside_core_rows
    ]


def to_fusion_quant_v1(result: CausalAtlasResult) -> dict[str, list[dict[str, Any]]]:
    """Adapta as quatro tabelas do núcleo causal para o contrato v1 do Fusion
    Quant de uma vez. Equivalente campo a campo, provado por fixture
    sintética golden em `tests/test_causal_atlas.py`."""

    return {
        "session_rows": to_fusion_quant_v1_session_rows(result),
        "checkpoint_rows": to_fusion_quant_v1_checkpoint_rows(result),
        "event_rows": to_fusion_quant_v1_event_rows(result),
        "outside_core_rows": to_fusion_quant_v1_outside_core_rows(result),
    }


__all__ = [
    "HUB_ATLAS_CONTRACT_VERSION",
    "CAUSAL_ATLAS_CORE_VERSION",
    "SCHEMA_RESULT",
    "SCHEMA_SESSION_ROWS",
    "SCHEMA_CHECKPOINT_ROWS",
    "SCHEMA_EVENT_ROWS",
    "SCHEMA_OUTSIDE_CORE_ROWS",
    "SCHEMA_QUALITY_ISSUE_ROWS",
    "FUSION_QUANT_V1_SESSION_ROWS",
    "FUSION_QUANT_V1_CHECKPOINT_ROWS",
    "FUSION_QUANT_V1_EVENT_ROWS",
    "FUSION_QUANT_V1_OUTSIDE_CORE_ROWS",
    "FUSION_QUANT_V1_SESSION_FIELDS",
    "FUSION_QUANT_V1_CHECKPOINT_FIELDS",
    "FUSION_QUANT_V1_EVENT_BASE_FIELDS",
    "FUSION_QUANT_V1_EVENT_FIELDS_TRIGGERED",
    "to_hub_contract",
    "to_fusion_quant_v1",
    "to_fusion_quant_v1_session_rows",
    "to_fusion_quant_v1_checkpoint_rows",
    "to_fusion_quant_v1_event_rows",
    "to_fusion_quant_v1_outside_core_rows",
]
