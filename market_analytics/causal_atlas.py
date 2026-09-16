"""Núcleo causal local e autocontido do Atlas de mercado (DEV-008A).

Este módulo promove ao EP Market Hub o cálculo já auditado no Fusion Quant
(DEV-011D.1/D.2, execução real auditada no commit `424cf66`): tabela de
sessão, checkpoints relativos à abertura, eventos de primeiro cruzamento e
relatório de qualidade, todos com semântica causal explícita.

Genérico por construção: nenhum símbolo, corretora, horário de B3 ou
qualidade de volume é codificado aqui. Todo parâmetro de calendário/
checkpoint/evento chega por `AtlasManifest`. Este módulo não importa Qt,
`MetaTrader5` nem `core/` — um adaptador futuro (DEV-008B) alimentará este
núcleo com candles fechados reais.

Vocabulário temporal obrigatório (ver `docs/ATLAS_CONTRACT.md`):

- ``known_at_open``: conhecido na abertura da sessão (colunas sem prefixo
  especial na tabela de sessão).
- ``known_at_checkpoint``: conhecido só depois de N minutos da abertura
  ESPERADA da sessão (`build_checkpoint_table`).
- ``known_at_event``: conhecido somente no FECHAMENTO da barra M1 onde um
  nível foi tocado (`available_at_utc` do evento), nunca em instante
  intrabar.
- ``end_of_session_label``: rótulo de resultado, conhecido só após o
  encerramento (ou, em eventos, após a disponibilidade causal do
  cruzamento). Fisicamente isolado: toda coluna desse tipo começa com
  ``label_``.

Política causal de disponibilidade do M1 (fixa): a barra M1 registra o
timestamp de ABERTURA; só fica causalmente disponível em
``timestamp + 1 minuto`` (`M1_AVAILABILITY_OFFSET`).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from statistics import pstdev
from typing import Any

from .bars import Bar

CAUSAL_ATLAS_CORE_VERSION = 1
CLOCK_POLICY = "source_wall_clock_no_conversion"

M1_AVAILABILITY_OFFSET = timedelta(minutes=1)

ALLOWED_SERIES_KIND = frozenset({"continuous_proportional", "individual_contract"})

CHECKPOINT_MINUTES_DEFAULT: tuple[int, ...] = (15, 30, 60, 120, 240)
PCT_EVENT_THRESHOLDS_DEFAULT: tuple[float, ...] = (0.0025, 0.0050, 0.0075, 0.0100)
ATR_EVENT_MULTIPLES_DEFAULT: tuple[float, ...] = (0.5, 1.0)
OPENING_RANGE_MINUTES_DEFAULT: tuple[int, ...] = (15, 30)
ATR_PERCENTILE_WINDOWS_DEFAULT: tuple[int, ...] = (20, 60)
EVENT_OUTCOME_HORIZONS_MINUTES_DEFAULT: tuple[int, ...] = (15, 30, 60)
RANGE_EXPANSION_PERCENTILE_DEFAULT = 0.75
ATR_LOOKBACK_SESSIONS_DEFAULT = 14
DAY_TYPE_RULE_VERSION = "v1"

# Janelas de percentil do range intradiário observado no checkpoint. Fixas
# (não dirigidas por manifesto) porque descrevem a forma do relatório causal
# em si, não uma escolha de mercado/corretora.
CHECKPOINT_RANGE_PERCENTILE_WINDOWS: tuple[int, ...] = (20, 60)

_FLAT_DAY_RANGE_PCT_THRESHOLD = 0.10
_REVERSAL_EPSILON_PCT = 0.0
_MAX_PLAUSIBLE_PCT_THRESHOLD = 0.20

REASON_ATYPICAL_OPEN = "sessao_parcial_abertura_atipica"
REASON_ATYPICAL_CLOSE = "sessao_parcial_encerramento_atipico"
REASON_LOW_COVERAGE = "sessao_parcial_cobertura_insuficiente"
REASON_MISSING_BARS = "sessao_parcial_barras_m1_faltantes_ou_duplicadas"
REASON_MIXED_VOLUME_QUALITY = "volume_quality_mista"

OUTSIDE_CORE_BEFORE_OPEN = "before_core_session_open"
OUTSIDE_CORE_AFTER_CLOSE = "after_core_session_close"


class CausalAtlasError(ValueError):
    """Erro de manifesto, identidade, ordem, duplicidade ou proveniência."""


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return round(float(value), digits)


def _hhmm_to_minutes(value: str, label: str) -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise CausalAtlasError(f"{label} inválido (esperado HH:MM): {value!r}")
    hour_text, minute_text = value[:2], value[3:]
    if not (hour_text.isdigit() and minute_text.isdigit()):
        raise CausalAtlasError(f"{label} inválido (esperado HH:MM): {value!r}")
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise CausalAtlasError(f"{label} inválido (esperado HH:MM): {value!r}")
    return hour * 60 + minute


def _validate_positive_unique_sorted(
    values: tuple, label: str, *, integer_only: bool = False
) -> None:
    if not values:
        raise CausalAtlasError(f"{label} não pode ser vazio")
    for value in values:
        valid_type = isinstance(value, int) if integer_only else isinstance(value, (int, float))
        if isinstance(value, bool) or not valid_type:
            expected = "inteiros" if integer_only else "números"
            raise CausalAtlasError(f"{label} deve conter somente {expected}: {value!r}")
    if len(set(values)) != len(values):
        raise CausalAtlasError(f"{label} tem valores repetidos: {values}")
    if list(values) != sorted(values):
        raise CausalAtlasError(f"{label} precisa estar em ordem crescente (determinismo): {values}")
    for value in values:
        if isinstance(value, bool) or value <= 0:
            raise CausalAtlasError(f"{label} tem valor não positivo: {value}")


def _require_nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CausalAtlasError(f"{label} não pode ser vazio")
    return value


@dataclass(frozen=True)
class AtlasBar:
    """Uma barra M1 já fechada, com os metadados de série exigidos pelo Atlas.

    Compõe `Bar` (identidade/OHLCV/volume já validados por `bars.Bar`) com
    `contract_id`/`roll_session`, que só fazem sentido no nível do Atlas
    (séries `individual_contract` e transições de rolagem) e por isso não
    poluem o `Bar` genérico usado pelo resto de `market_analytics`.
    """

    bar: Bar
    contract_id: str | None = None
    roll_session: bool = False

    def __post_init__(self) -> None:
        if self.bar.timeframe != "M1":
            raise CausalAtlasError(
                f"AtlasBar exige timeframe M1 (recebido: {self.bar.timeframe!r})"
            )
        if self.bar.timestamp.utcoffset() != timedelta(0):
            raise CausalAtlasError(
                f"AtlasBar.timestamp deve estar em UTC (recebido offset={self.bar.timestamp.utcoffset()!r})"
            )
        if self.contract_id is not None and (
            not isinstance(self.contract_id, str) or not self.contract_id.strip()
        ):
            raise CausalAtlasError("contract_id deve ser string não vazia ou None")
        if not isinstance(self.roll_session, bool):
            raise CausalAtlasError("roll_session deve ser booleano")

    @property
    def timestamp(self) -> datetime:
        return self.bar.timestamp

    @property
    def available_at_utc(self) -> datetime:
        return self.bar.timestamp + M1_AVAILABILITY_OFFSET

    @property
    def session_date(self) -> date:
        return self.bar.timestamp.date()


def wrap_bars(bars: list[Bar]) -> list[AtlasBar]:
    """Conveniência: embrulha `Bar`s simples sem `contract_id`/rolagem."""

    return [AtlasBar(bar=bar) for bar in bars]


@dataclass(frozen=True)
class AtlasManifest:
    """Única fonte de verdade dos parâmetros de um Atlas causal.

    Nenhum default aqui é específico de um ativo/corretora: as escadas de
    checkpoint/evento são as declaradas no DEV-011D (ver
    `docs/work_orders/DEV-008.md`), não otimizadas por busca.
    """

    logical_id: str
    symbol: str
    series_kind: str
    source_symbol: str
    adjustment_method: str
    expected_session_start: str
    expected_session_end: str
    contract_id: str | None = None
    checkpoint_minutes: tuple[int, ...] = CHECKPOINT_MINUTES_DEFAULT
    pct_event_thresholds: tuple[float, ...] = PCT_EVENT_THRESHOLDS_DEFAULT
    atr_event_multiples: tuple[float, ...] = ATR_EVENT_MULTIPLES_DEFAULT
    opening_range_minutes: tuple[int, ...] = OPENING_RANGE_MINUTES_DEFAULT
    atr_percentile_windows: tuple[int, ...] = ATR_PERCENTILE_WINDOWS_DEFAULT
    event_outcome_horizons_minutes: tuple[int, ...] = EVENT_OUTCOME_HORIZONS_MINUTES_DEFAULT
    range_expansion_percentile: float = RANGE_EXPANSION_PERCENTILE_DEFAULT
    atr_lookback_sessions: int = ATR_LOOKBACK_SESSIONS_DEFAULT
    coverage_tolerance_minutes: int = 5
    min_coverage_ratio: float = 0.5

    def __post_init__(self) -> None:
        _require_nonempty_str(self.logical_id, "logical_id")
        _require_nonempty_str(self.symbol, "symbol")
        _require_nonempty_str(self.source_symbol, "source_symbol")
        _require_nonempty_str(self.adjustment_method, "adjustment_method")
        if self.series_kind not in ALLOWED_SERIES_KIND:
            raise CausalAtlasError(f"series_kind fora do vocabulário permitido: {self.series_kind!r}")
        if self.contract_id is not None and (
            not isinstance(self.contract_id, str) or not self.contract_id.strip()
        ):
            raise CausalAtlasError("contract_id deve ser string não vazia ou None")

        start_minutes = _hhmm_to_minutes(self.expected_session_start, "expected_session_start")
        end_minutes = _hhmm_to_minutes(self.expected_session_end, "expected_session_end")
        if end_minutes <= start_minutes:
            raise CausalAtlasError("expected_session_end deve ser posterior a expected_session_start")

        _validate_positive_unique_sorted(
            self.checkpoint_minutes, "checkpoint_minutes", integer_only=True
        )
        _validate_positive_unique_sorted(self.pct_event_thresholds, "pct_event_thresholds")
        _validate_positive_unique_sorted(self.atr_event_multiples, "atr_event_multiples")
        _validate_positive_unique_sorted(
            self.opening_range_minutes, "opening_range_minutes", integer_only=True
        )
        _validate_positive_unique_sorted(
            self.atr_percentile_windows, "atr_percentile_windows", integer_only=True
        )
        _validate_positive_unique_sorted(
            self.event_outcome_horizons_minutes,
            "event_outcome_horizons_minutes",
            integer_only=True,
        )
        for threshold in self.pct_event_thresholds:
            if threshold >= _MAX_PLAUSIBLE_PCT_THRESHOLD:
                raise CausalAtlasError(
                    f"pct_event_thresholds tem valor implausível para evento intradiário: {threshold}"
                )
        if (
            isinstance(self.atr_lookback_sessions, bool)
            or not isinstance(self.atr_lookback_sessions, int)
            or self.atr_lookback_sessions <= 0
        ):
            raise CausalAtlasError("atr_lookback_sessions deve ser um inteiro positivo")
        if (
            isinstance(self.coverage_tolerance_minutes, bool)
            or not isinstance(self.coverage_tolerance_minutes, int)
            or self.coverage_tolerance_minutes < 0
        ):
            raise CausalAtlasError(
                "coverage_tolerance_minutes deve ser um inteiro não negativo"
            )
        if (
            isinstance(self.min_coverage_ratio, bool)
            or not isinstance(self.min_coverage_ratio, (int, float))
            or not (0.0 < self.min_coverage_ratio <= 1.0)
        ):
            raise CausalAtlasError(f"min_coverage_ratio precisa estar em (0, 1]: {self.min_coverage_ratio}")
        if (
            isinstance(self.range_expansion_percentile, bool)
            or not isinstance(self.range_expansion_percentile, (int, float))
            or not (0.0 < self.range_expansion_percentile < 1.0)
        ):
            raise CausalAtlasError(
                f"range_expansion_percentile precisa estar em (0, 1): {self.range_expansion_percentile}"
            )

    def session_window_minutes(self) -> tuple[int, int]:
        return (
            _hhmm_to_minutes(self.expected_session_start, "expected_session_start"),
            _hhmm_to_minutes(self.expected_session_end, "expected_session_end"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "symbol": self.symbol,
            "series_kind": self.series_kind,
            "source_symbol": self.source_symbol,
            "adjustment_method": self.adjustment_method,
            "contract_id": self.contract_id,
            "expected_session_start": self.expected_session_start,
            "expected_session_end": self.expected_session_end,
            "checkpoint_minutes": list(self.checkpoint_minutes),
            "pct_event_thresholds": list(self.pct_event_thresholds),
            "atr_event_multiples": list(self.atr_event_multiples),
            "opening_range_minutes": list(self.opening_range_minutes),
            "atr_percentile_windows": list(self.atr_percentile_windows),
            "event_outcome_horizons_minutes": list(self.event_outcome_horizons_minutes),
            "range_expansion_percentile": self.range_expansion_percentile,
            "atr_lookback_sessions": self.atr_lookback_sessions,
            "coverage_tolerance_minutes": self.coverage_tolerance_minutes,
            "min_coverage_ratio": self.min_coverage_ratio,
            "clock_policy": CLOCK_POLICY,
            "core_version": CAUSAL_ATLAS_CORE_VERSION,
        }

    def params_sha256(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()


def bars_content_sha256(bars: list[AtlasBar]) -> str:
    """Hash determinístico do conteúdo das barras de entrada (sem tocar disco)."""

    payload = [
        {
            "source_id": item.bar.source_id,
            "symbol": item.bar.symbol,
            "timeframe": item.bar.timeframe,
            "timestamp_utc": item.bar.timestamp.isoformat(),
            "open": item.bar.open,
            "high": item.bar.high,
            "low": item.bar.low,
            "close": item.bar.close,
            "volume": item.bar.volume,
            "volume_quality": item.bar.volume_quality,
            "contract_id": item.contract_id,
            "roll_session": item.roll_session,
        }
        for item in bars
    ]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()


# --------------------------------------------------------------------------
# Validação da série de entrada
# --------------------------------------------------------------------------


def _validate_bar_series(bars: list[AtlasBar], manifest: AtlasManifest) -> None:
    if not bars:
        raise CausalAtlasError("nenhuma barra M1 informada")

    symbols = {item.bar.symbol for item in bars}
    if symbols != {manifest.symbol}:
        raise CausalAtlasError(
            f"barras devem cobrir exatamente o símbolo do manifesto {manifest.symbol!r}: {sorted(symbols)}"
        )

    previous_timestamp: datetime | None = None
    for item in bars:
        if previous_timestamp is not None and item.timestamp <= previous_timestamp:
            raise CausalAtlasError(
                "timestamps M1 devem ser estritamente crescentes e sem duplicatas: "
                f"{previous_timestamp!r} seguido de {item.timestamp!r}"
            )
        previous_timestamp = item.timestamp

    seen_dates: set[date] = set()
    previous_date: date | None = None
    for item in bars:
        current_date = item.session_date
        if current_date != previous_date:
            if current_date in seen_dates:
                raise CausalAtlasError(
                    f"session_date {current_date.isoformat()!r} não está contíguo (sessões intercaladas)"
                )
            seen_dates.add(current_date)
            previous_date = current_date


def _group_by_session(bars: list[AtlasBar]) -> dict[date, list[AtlasBar]]:
    grouped: dict[date, list[AtlasBar]] = {}
    for item in bars:
        grouped.setdefault(item.session_date, []).append(item)
    return grouped


# --------------------------------------------------------------------------
# Matemática pura (sem dependências externas)
# --------------------------------------------------------------------------


def _true_range(high: float, low: float, previous_close: float | None) -> float:
    if previous_close is None:
        return high - low
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    middle = n // 2
    return ordered[middle] if n % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _percentile_rank(value: float, history: list[float]) -> float | None:
    if not history:
        return None
    return sum(1 for item in history if item <= value) / len(history)


def _gap_band(gap_pct_abs: float, thresholds_pct: tuple[float, ...]) -> str:
    bands = sorted(thresholds_pct)
    labels = [f"lt_{bands[0] * 100:.2f}pct"]
    for lower, upper in pairwise(bands):
        labels.append(f"{lower * 100:.2f}_{upper * 100:.2f}pct")
    labels.append(f"gte_{bands[-1] * 100:.2f}pct")
    for band_index, upper in enumerate(bands):
        if gap_pct_abs < upper:
            return labels[band_index]
    return labels[-1]


def _day_type_v1(day_return_pct: float, range_pct: float, close_position: float) -> str:
    if range_pct < _FLAT_DAY_RANGE_PCT_THRESHOLD:
        return "range"
    if day_return_pct > 0 and close_position >= 0.60:
        return "trend_up"
    if day_return_pct < 0 and close_position <= 0.40:
        return "trend_down"
    return "reversal"


# --------------------------------------------------------------------------
# Tabela de sessão (known_at_open + label_*)
# --------------------------------------------------------------------------


def build_session_table(
    bars: list[AtlasBar], manifest: AtlasManifest
) -> tuple[list[dict[str, Any]], dict[date, list[AtlasBar]], list[dict[str, Any]]]:
    """Uma linha por sessão. Elegibilidade contra o calendário declarado no
    manifesto, nunca contra estatística agregada do próprio arquivo.

    Retorna as linhas, o mapa `session_date -> barras do núcleo` (usado por
    checkpoint/evento) e as barras fora do núcleo causal declarado."""

    _validate_bar_series(bars, manifest)
    start_minutes, end_minutes = manifest.session_window_minutes()
    expected_minutes = end_minutes - start_minutes

    grouped = _group_by_session(bars)
    session_order = sorted(grouped)

    rows: list[dict[str, Any]] = []
    outside_core_rows: list[dict[str, Any]] = []
    session_core_bars: dict[date, list[AtlasBar]] = {}

    true_range_history: list[float] = []
    atr_history: list[float] = []
    previous_close: float | None = None
    previous_true_range: float | None = None
    previous_contract_id: str | None = None
    previous_source_id: str | None = None
    previous_eligible = True
    previous_eligible_for_aggregates = True
    previous_volatility_state: str | None = None
    previous_volatility_state_streak: int | None = None
    previous_return_close_open_pct: float | None = None
    previous_close_position: float | None = None
    previous_efficiency_ratio: float | None = None
    previous_day_type: str | None = None

    for session_date in session_order:
        all_bars = grouped[session_date]
        is_first_session = previous_close is None

        session_source_ids = {item.bar.source_id for item in all_bars}
        if len(session_source_ids) != 1:
            raise CausalAtlasError(
                f"source_id não é constante dentro da sessão {session_date.isoformat()}: {sorted(session_source_ids)}"
            )
        session_source_id = next(iter(session_source_ids))
        source_transition = False if is_first_session else session_source_id != previous_source_id

        expected_open_time = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC) + timedelta(
            minutes=start_minutes
        )
        expected_close_time = expected_open_time + timedelta(minutes=expected_minutes)

        core_bars = [item for item in all_bars if expected_open_time <= item.timestamp < expected_close_time]
        outside_bars = [item for item in all_bars if item not in core_bars]
        if not core_bars:
            raise CausalAtlasError(
                f"sessão {session_date.isoformat()} não tem nenhuma barra dentro do núcleo causal declarado "
                f"[{manifest.expected_session_start}, {manifest.expected_session_end})"
            )
        for outside_bar in outside_bars:
            reason = (
                OUTSIDE_CORE_BEFORE_OPEN
                if outside_bar.timestamp < expected_open_time
                else OUTSIDE_CORE_AFTER_CLOSE
            )
            outside_core_rows.append(
                {
                    "source_id": session_source_id,
                    "symbol": manifest.symbol,
                    "session_date": session_date.isoformat(),
                    "timestamp_utc": outside_bar.timestamp.isoformat(),
                    "available_at_utc": outside_bar.available_at_utc.isoformat(),
                    "open": _round(outside_bar.bar.open, 4),
                    "high": _round(outside_bar.bar.high, 4),
                    "low": _round(outside_bar.bar.low, 4),
                    "close": _round(outside_bar.bar.close, 4),
                    "volume_quality": outside_bar.bar.volume_quality,
                    "outside_core_session_reason": reason,
                }
            )
        session_core_bars[session_date] = core_bars

        session_open = core_bars[0].bar.open
        session_close = core_bars[-1].bar.close
        session_high = max(item.bar.high for item in core_bars)
        session_low = min(item.bar.low for item in core_bars)
        bar_count = len(core_bars)
        first_time = core_bars[0].timestamp
        last_time = core_bars[-1].timestamp
        last_available_at = core_bars[-1].available_at_utc

        session_contract_id = _resolve_session_contract_id(core_bars, manifest, session_date)

        true_range_session = _true_range(session_high, session_low, previous_close)
        true_range_pct = true_range_session / session_open * 100.0 if session_open else 0.0

        if is_first_session:
            true_range_context_quality, true_range_context_reason = "unknown", "sem_sessao_anterior"
        elif not previous_eligible:
            true_range_context_quality, true_range_context_reason = "partial_context", "sessao_anterior_parcial"
        else:
            true_range_context_quality, true_range_context_reason = "full_context", None

        reasons: list[str] = []
        if abs((first_time - expected_open_time).total_seconds()) / 60.0 > manifest.coverage_tolerance_minutes:
            reasons.append(REASON_ATYPICAL_OPEN)
        if abs((last_available_at - expected_close_time).total_seconds()) / 60.0 > manifest.coverage_tolerance_minutes:
            reasons.append(REASON_ATYPICAL_CLOSE)
        if bar_count > 1:
            for previous_bar, current_bar in pairwise(core_bars):
                if current_bar.timestamp - previous_bar.timestamp != timedelta(minutes=1):
                    reasons.append(REASON_MISSING_BARS)
                    break
        if bar_count < manifest.min_coverage_ratio * expected_minutes:
            reasons.append(REASON_LOW_COVERAGE)

        volume_qualities = sorted({item.bar.volume_quality for item in core_bars})
        if len(volume_qualities) > 1:
            reasons.append(REASON_MIXED_VOLUME_QUALITY)
        session_volume_quality = volume_qualities[0] if len(volume_qualities) == 1 else "mixed"

        eligible = len(reasons) == 0

        atr_window = true_range_history[-manifest.atr_lookback_sessions :]
        if len(atr_window) >= manifest.atr_lookback_sessions:
            atr14_previous_session: float | None = _mean(atr_window)
            atr14_window_reason = None
        else:
            atr14_previous_session = None
            atr14_window_reason = "atr14_historico_insuficiente_menos_de_14_sessoes_elegiveis_anteriores"
        atr14_previous_session_pct = (
            atr14_previous_session / previous_close * 100.0
            if atr14_previous_session is not None and previous_close
            else None
        )
        atr_percentiles: dict[str, float | None] = {}
        for window in manifest.atr_percentile_windows:
            history_slice = atr_history[-window:]
            atr_percentiles[f"atr_percentile_{window}"] = (
                _round(_percentile_rank(atr14_previous_session, history_slice), 6)
                if atr14_previous_session is not None and history_slice
                else None
            )

        true_range_over_atr_previous = (
            _round(previous_true_range / atr14_previous_session, 6)
            if previous_eligible_for_aggregates and previous_true_range is not None and atr14_previous_session
            else None
        )

        if is_first_session:
            gap_points = gap_pct = gap_sign = gap_band = gap_over_previous_atr = None
            gap_quality, gap_quality_reason = "unknown", "sem_sessao_anterior"
        else:
            gap_points = session_open - previous_close
            gap_pct = gap_points / previous_close * 100.0 if previous_close else None
            gap_sign = "flat" if gap_points == 0 else ("up" if gap_points > 0 else "down")
            gap_band = _gap_band(abs(gap_pct) / 100.0, manifest.pct_event_thresholds) if gap_pct is not None else None
            gap_over_previous_atr = (
                _round(gap_points / atr14_previous_session, 6) if atr14_previous_session else None
            )
            session_has_roll = any(item.roll_session for item in core_bars)
            if not eligible or not previous_eligible:
                gap_quality = "partial_context"
                parts = []
                if not previous_eligible:
                    parts.append("sessao_anterior_parcial")
                if not eligible:
                    parts.append("sessao_atual_parcial")
                gap_quality_reason = ";".join(parts)
            elif session_has_roll:
                gap_quality, gap_quality_reason = "roll_affected", "sessao_marcada_como_rolagem"
            elif manifest.series_kind == "continuous_proportional":
                gap_quality, gap_quality_reason = "continuous_reference", None
            elif previous_contract_id is not None and session_contract_id == previous_contract_id:
                gap_quality, gap_quality_reason = "clean", None
            else:
                gap_quality, gap_quality_reason = "roll_affected", "mudanca_de_contract_id"

        eligible_for_aggregates = eligible and gap_quality != "roll_affected"

        day_return_close_open_pct = (session_close - session_open) / session_open * 100.0 if session_open else 0.0
        day_return_close_close_pct = (
            (session_close - previous_close) / previous_close * 100.0 if previous_close else None
        )
        close_position = (
            (session_close - session_low) / (session_high - session_low) if session_high > session_low else 0.5
        )
        closes = [item.bar.close for item in core_bars]
        session_path_length = sum(abs(closes[idx] - closes[idx - 1]) for idx in range(1, len(closes)))
        session_net_move = abs(session_close - session_open)
        session_efficiency_ratio = session_net_move / session_path_length if session_path_length > 0 else None
        maximum_up_excursion_pct = (session_high - session_open) / session_open * 100.0
        maximum_down_excursion_pct = (session_open - session_low) / session_open * 100.0
        high_time = max(core_bars, key=lambda item: item.bar.high).timestamp
        low_time = min(core_bars, key=lambda item: item.bar.low).timestamp
        day_type = _day_type_v1(day_return_close_open_pct, true_range_pct, close_position)

        volatility_state: str | None = None
        if previous_eligible_for_aggregates and previous_true_range is not None:
            volatility_state = (
                "expansion"
                if true_range_session > previous_true_range
                else ("contraction" if true_range_session < previous_true_range else "stable")
            )
        if eligible_for_aggregates and volatility_state is not None:
            volatility_state_streak = (
                (previous_volatility_state_streak or 0) + 1
                if volatility_state == previous_volatility_state
                else 1
            )
        else:
            volatility_state_streak = None

        row: dict[str, Any] = {
            "source_id": session_source_id,
            "source_transition": source_transition,
            "symbol": manifest.symbol,
            "series_kind": manifest.series_kind,
            "source_symbol": manifest.source_symbol,
            "adjustment_method": manifest.adjustment_method,
            "contract_id": session_contract_id,
            "session_date": session_date.isoformat(),
            "available_at_utc": last_available_at.isoformat(),
            "session_open_time_utc": first_time.isoformat(),
            "session_close_time_utc": last_time.isoformat(),
            "session_close_available_at_utc": last_available_at.isoformat(),
            "expected_session_open_time_utc": expected_open_time.isoformat(),
            "expected_session_close_time_utc": expected_close_time.isoformat(),
            "bar_count": bar_count,
            "expected_bar_count": expected_minutes,
            "is_first_session": is_first_session,
            "eligible": eligible,
            "eligible_for_aggregates": eligible_for_aggregates,
            "eligibility_reason": ";".join(reasons) if reasons else "",
            "volume_quality": session_volume_quality,
            "session_open": _round(session_open, 4),
            "session_high": _round(session_high, 4),
            "session_low": _round(session_low, 4),
            "previous_close": _round(previous_close, 4),
            "previous_true_range": _round(previous_true_range, 4),
            "previous_true_range_over_open_pct": (
                _round(previous_true_range / session_open * 100.0, 6)
                if previous_true_range is not None and session_open
                else None
            ),
            "previous_session_eligible": previous_eligible if not is_first_session else None,
            "previous_session_volatility_state": (
                previous_volatility_state if previous_eligible_for_aggregates else None
            ),
            "previous_session_volatility_state_streak": (
                previous_volatility_state_streak if previous_eligible_for_aggregates else None
            ),
            "previous_session_return_close_open_pct": (
                _round(previous_return_close_open_pct, 6) if previous_eligible_for_aggregates else None
            ),
            "previous_session_close_position_in_range": (
                _round(previous_close_position, 6) if previous_eligible_for_aggregates else None
            ),
            "previous_session_efficiency_ratio": (
                _round(previous_efficiency_ratio, 6) if previous_eligible_for_aggregates else None
            ),
            "previous_session_day_type": previous_day_type if previous_eligible_for_aggregates else None,
            "previous_session_eligible_for_aggregates": (
                previous_eligible_for_aggregates if not is_first_session else None
            ),
            "atr14_previous_session": _round(atr14_previous_session, 4),
            "atr14_previous_session_pct": _round(atr14_previous_session_pct, 6),
            "atr14_previous_session_window_reason": atr14_window_reason,
            **atr_percentiles,
            "true_range_over_atr_previous": true_range_over_atr_previous,
            "true_range_context_quality": true_range_context_quality,
            "true_range_context_quality_reason": true_range_context_reason,
            "gap_points": _round(gap_points, 4),
            "gap_pct": _round(gap_pct, 6),
            "gap_sign": gap_sign,
            "gap_band": gap_band,
            "gap_over_previous_atr": gap_over_previous_atr,
            "gap_quality": gap_quality,
            "gap_quality_reason": gap_quality_reason,
            "label_session_close": _round(session_close, 4),
            "label_return_close_open_pct": _round(day_return_close_open_pct, 6),
            "label_return_close_close_pct": _round(day_return_close_close_pct, 6),
            "label_true_range": _round(true_range_session, 4),
            "label_true_range_pct": _round(true_range_pct, 6),
            "label_high_time_utc": high_time.isoformat(),
            "label_low_time_utc": low_time.isoformat(),
            "label_close_position_in_range": _round(close_position, 6),
            "label_efficiency_ratio": _round(session_efficiency_ratio, 6),
            "label_maximum_up_excursion_pct": _round(maximum_up_excursion_pct, 6),
            "label_maximum_down_excursion_pct": _round(maximum_down_excursion_pct, 6),
            "label_day_type": day_type,
            "label_day_type_rule_version": DAY_TYPE_RULE_VERSION,
            "label_volatility_state": volatility_state,
        }
        rows.append(row)

        feeds_true_range_history = eligible_for_aggregates and true_range_context_quality != "partial_context"
        if feeds_true_range_history:
            true_range_history.append(true_range_session)
            if atr14_previous_session is not None:
                atr_history.append(atr14_previous_session)
        previous_true_range = true_range_session
        previous_close = session_close
        previous_contract_id = session_contract_id
        previous_source_id = session_source_id
        previous_eligible = eligible
        previous_eligible_for_aggregates = eligible_for_aggregates
        previous_volatility_state = volatility_state if eligible_for_aggregates else None
        previous_volatility_state_streak = volatility_state_streak if eligible_for_aggregates else None
        previous_return_close_open_pct = day_return_close_open_pct
        previous_close_position = close_position
        previous_efficiency_ratio = session_efficiency_ratio
        previous_day_type = day_type

    return rows, session_core_bars, outside_core_rows


def _resolve_session_contract_id(
    core_bars: list[AtlasBar], manifest: AtlasManifest, session_date: date
) -> str | None:
    if manifest.contract_id is not None:
        return manifest.contract_id
    if manifest.series_kind != "individual_contract":
        return None
    contract_ids = {item.contract_id for item in core_bars}
    if len(contract_ids) != 1 or None in contract_ids:
        raise CausalAtlasError(
            "series_kind=individual_contract exige AtlasManifest.contract_id ou "
            f"AtlasBar.contract_id constante por sessão; sessão {session_date.isoformat()} tem "
            f"{sorted(str(item) for item in contract_ids)}"
        )
    return next(iter(contract_ids))


# --------------------------------------------------------------------------
# Tabela de checkpoint (known_at_checkpoint)
# --------------------------------------------------------------------------


def _opening_range(bars: list[AtlasBar], anchor: datetime, minutes: int) -> tuple[float, float] | None:
    window = [item for item in bars if anchor <= item.timestamp < anchor + timedelta(minutes=minutes)]
    if not window:
        return None
    return max(item.bar.high for item in window), min(item.bar.low for item in window)


def _opening_range_state(
    high_so_far: float, low_so_far: float, close_so_far: float, opening_range: tuple[float, float] | None
) -> str | None:
    if opening_range is None:
        return None
    or_high, or_low = opening_range
    broke_up = high_so_far > or_high
    broke_down = low_so_far < or_low
    inside_now = or_low <= close_so_far <= or_high
    if broke_up and broke_down:
        if inside_now:
            return "whipsaw_returned"
        return "whipsaw_up" if close_so_far > or_high else "whipsaw_down"
    if broke_up:
        return "broke_up_returned" if inside_now else "broke_up"
    if broke_down:
        return "broke_down_returned" if inside_now else "broke_down"
    return "inside"


_NULLABLE_CHECKPOINT_FIELDS_BASE: tuple[str, ...] = (
    "high_so_far", "low_so_far", "range_so_far", "true_range_so_far",
    "range_so_far_over_atr_previous", "range_so_far_percentile_20",
    "range_so_far_percentile_60", "return_from_open_pct", "return_from_open_atr",
    "realized_volatility_so_far", "realized_volatility_so_far_percentile_60",
    "efficiency_ratio_so_far", "close_position_so_far", "volume_so_far",
    "volume_relative_so_far", "volume_relative_so_far_null_reason",
)


def build_checkpoint_table(
    session_rows: list[dict[str, Any]],
    session_core_bars: dict[date, list[AtlasBar]],
    manifest: AtlasManifest,
) -> list[dict[str, Any]]:
    """Uma linha por sessão x checkpoint, ancorada na abertura ESPERADA."""

    rows: list[dict[str, Any]] = []
    percentile_history: dict[int, list[float]] = {m: [] for m in manifest.checkpoint_minutes}
    volume_history: dict[int, list[float]] = {m: [] for m in manifest.checkpoint_minutes}
    volatility_percentile_history: dict[int, list[float]] = {m: [] for m in manifest.checkpoint_minutes}

    for session_row in session_rows:
        session_date = date.fromisoformat(session_row["session_date"])
        bars = session_core_bars[session_date]
        anchor = datetime.fromisoformat(session_row["expected_session_open_time_utc"])
        session_open = session_row["session_open"]
        atr_previous = session_row["atr14_previous_session"]
        session_eligible = session_row["eligible"]
        session_eligible_for_aggregates = session_row["eligible_for_aggregates"]
        session_last_available_at = bars[-1].available_at_utc
        opening_ranges = {m: _opening_range(bars, anchor, m) for m in manifest.opening_range_minutes}
        nullable_checkpoint_fields = _NULLABLE_CHECKPOINT_FIELDS_BASE + tuple(
            f"opening_range_{opening_minutes}_state" for opening_minutes in manifest.opening_range_minutes
        )

        for minutes in manifest.checkpoint_minutes:
            checkpoint_time = anchor + timedelta(minutes=minutes)
            match = [item for item in bars if item.available_at_utc == checkpoint_time]
            if checkpoint_time > session_last_available_at:
                available, unavailable_reason = False, "sessao_encerrou_antes_do_checkpoint"
            elif len(match) != 1:
                available, unavailable_reason = False, "barra_do_checkpoint_ausente"
            else:
                available, unavailable_reason = True, None

            window = [item for item in bars if item.timestamp < checkpoint_time]
            bars_used = len(window)
            row: dict[str, Any] = {
                "source_id": session_row["source_id"],
                "symbol": session_row["symbol"],
                "series_kind": session_row["series_kind"],
                "source_symbol": session_row["source_symbol"],
                "adjustment_method": session_row["adjustment_method"],
                "contract_id": session_row["contract_id"],
                "session_date": session_row["session_date"],
                "checkpoint_minutes": minutes,
                "checkpoint_time_utc": checkpoint_time.isoformat(),
                "available_at_utc": (
                    session_last_available_at
                    if unavailable_reason == "sessao_encerrou_antes_do_checkpoint"
                    else checkpoint_time
                ).isoformat(),
                "session_eligible": session_eligible,
                "session_eligible_for_aggregates": session_eligible_for_aggregates,
                "available": available,
                "unavailable_reason": unavailable_reason,
                "bars_used": bars_used,
            }
            if not available:
                for field_name in nullable_checkpoint_fields:
                    row.setdefault(field_name, None)
                rows.append(row)
                continue

            high_so_far = max(item.bar.high for item in window)
            low_so_far = min(item.bar.low for item in window)
            close_so_far = window[-1].bar.close
            range_so_far = high_so_far - low_so_far
            true_range_so_far = _true_range(high_so_far, low_so_far, session_row["previous_close"])
            return_from_open_pct = (close_so_far - session_open) / session_open * 100.0 if session_open else 0.0
            return_from_open_atr = (
                _round((close_so_far - session_open) / atr_previous, 6) if atr_previous else None
            )

            closes = [item.bar.close for item in window]
            one_min_returns = [
                (closes[idx] - closes[idx - 1]) / closes[idx - 1] for idx in range(1, len(closes))
            ]
            realized_volatility_so_far = pstdev(one_min_returns) if one_min_returns else 0.0
            net_move = abs(close_so_far - session_open)
            path_length = sum(abs(closes[idx] - closes[idx - 1]) for idx in range(1, len(closes))) if bars_used > 1 else 0.0
            efficiency_ratio_so_far = (net_move / path_length) if path_length > 0 else None
            close_position_so_far = (close_so_far - low_so_far) / range_so_far if range_so_far > 0 else 0.5

            range_history = percentile_history[minutes]
            volume_qualities_so_far = sorted({item.bar.volume_quality for item in window})
            checkpoint_is_exchange = volume_qualities_so_far == ["exchange"]
            volume_is_usable = volume_qualities_so_far in (["exchange"], ["tick_proxy"]) and all(
                item.bar.volume is not None for item in window
            )
            volume_so_far = sum(item.bar.volume for item in window) if volume_is_usable else None
            if checkpoint_is_exchange:
                median_previous_volume = _median(volume_history[minutes])
                if volume_so_far is not None and median_previous_volume:
                    volume_relative_so_far = _round(volume_so_far / median_previous_volume, 6)
                    volume_relative_reason = None
                else:
                    volume_relative_so_far = None
                    volume_relative_reason = "sem_historico_causal_de_volume_exchange"
            else:
                volume_relative_so_far = None
                volume_relative_reason = "volume_quality_nao_exchange"

            row.update(
                {
                    "high_so_far": _round(high_so_far, 4),
                    "low_so_far": _round(low_so_far, 4),
                    "range_so_far": _round(range_so_far, 4),
                    "true_range_so_far": _round(true_range_so_far, 4),
                    "range_so_far_over_atr_previous": (
                        _round(range_so_far / atr_previous, 6) if atr_previous else None
                    ),
                    "range_so_far_percentile_20": (
                        _round(_percentile_rank(range_so_far, range_history[-20:]), 6) if range_history else None
                    ),
                    "range_so_far_percentile_60": (
                        _round(_percentile_rank(range_so_far, range_history[-60:]), 6) if range_history else None
                    ),
                    "return_from_open_pct": _round(return_from_open_pct, 6),
                    "return_from_open_atr": return_from_open_atr,
                    "realized_volatility_so_far": _round(realized_volatility_so_far, 6),
                    "realized_volatility_so_far_percentile_60": (
                        _round(
                            _percentile_rank(
                                realized_volatility_so_far, volatility_percentile_history[minutes][-60:]
                            ),
                            6,
                        )
                        if volatility_percentile_history[minutes]
                        else None
                    ),
                    "efficiency_ratio_so_far": _round(efficiency_ratio_so_far, 6),
                    "close_position_so_far": _round(close_position_so_far, 6),
                    "volume_so_far": _round(volume_so_far, 4),
                    "volume_relative_so_far": volume_relative_so_far,
                    "volume_relative_so_far_null_reason": volume_relative_reason,
                }
            )
            for opening_minutes in manifest.opening_range_minutes:
                row[f"opening_range_{opening_minutes}_state"] = (
                    _opening_range_state(
                        high_so_far, low_so_far, close_so_far, opening_ranges.get(opening_minutes)
                    )
                    if minutes >= opening_minutes and opening_minutes in opening_ranges
                    else None
                )
            rows.append(row)

            if session_eligible_for_aggregates:
                percentile_history[minutes].append(range_so_far)
                volatility_percentile_history[minutes].append(realized_volatility_so_far)
                if checkpoint_is_exchange and volume_so_far is not None:
                    volume_history[minutes].append(volume_so_far)

    return rows


# --------------------------------------------------------------------------
# Tabela de evento (known_at_event + label_*)
# --------------------------------------------------------------------------


def _event_outcomes(
    bars: list[AtlasBar],
    event_bar_time: datetime,
    available_at: datetime,
    cross_price: float,
    direction: str,
    session_close: float,
    horizons_minutes: tuple[int, ...],
) -> dict[str, Any]:
    after = [item for item in bars if item.timestamp > event_bar_time]
    outcomes: dict[str, Any] = {}

    for horizon in horizons_minutes:
        target_time = available_at + timedelta(minutes=horizon)
        window = [item for item in bars if item.available_at_utc <= target_time]
        if not window or bars[-1].available_at_utc < target_time:
            outcomes[f"label_return_{horizon}m_pct"] = None
            outcomes[f"label_return_{horizon}m_null_reason"] = "sessao_encerrou_antes_do_horizonte"
            continue
        close_at_target = window[-1].bar.close
        outcomes[f"label_return_{horizon}m_pct"] = _round(
            (close_at_target - cross_price) / cross_price * 100.0, 6
        )
        outcomes[f"label_return_{horizon}m_null_reason"] = None

    outcomes["label_return_to_close_pct"] = _round((session_close - cross_price) / cross_price * 100.0, 6)

    if not after:
        outcomes.update({"label_mfe_pct": None, "label_mae_pct": None, "label_time_to_extreme_after_minutes": None})
    else:
        max_high_item = max(after, key=lambda item: item.bar.high)
        min_low_item = min(after, key=lambda item: item.bar.low)
        max_high = max_high_item.bar.high
        min_low = min_low_item.bar.low
        if direction == "up":
            outcomes["label_mfe_pct"] = _round(max(0.0, (max_high - cross_price) / cross_price * 100.0), 6)
            outcomes["label_mae_pct"] = _round(max(0.0, (cross_price - min_low) / cross_price * 100.0), 6)
            extreme_time = max_high_item.timestamp
        else:
            outcomes["label_mfe_pct"] = _round(max(0.0, (cross_price - min_low) / cross_price * 100.0), 6)
            outcomes["label_mae_pct"] = _round(max(0.0, (max_high - cross_price) / cross_price * 100.0), 6)
            extreme_time = min_low_item.timestamp
        outcomes["label_time_to_extreme_after_minutes"] = (extreme_time - event_bar_time).total_seconds() / 60.0

    return_to_close = outcomes["label_return_to_close_pct"]
    expected_sign = 1.0 if direction == "up" else -1.0
    outcomes["label_continued"] = bool(return_to_close * expected_sign > _REVERSAL_EPSILON_PCT)
    outcomes["label_reversed"] = bool(return_to_close * expected_sign < -_REVERSAL_EPSILON_PCT)
    return outcomes


def _scan_level_crossing(bars: list[AtlasBar], level: float, direction: str) -> datetime | None:
    hits = [item for item in bars if (item.bar.high >= level if direction == "up" else item.bar.low <= level)]
    if not hits:
        return None
    return hits[0].timestamp


def _base_event_row(session_row: dict[str, Any], event_type: str, event_id: str, direction: str) -> dict[str, Any]:
    return {
        "source_id": session_row["source_id"],
        "symbol": session_row["symbol"],
        "series_kind": session_row["series_kind"],
        "source_symbol": session_row["source_symbol"],
        "adjustment_method": session_row["adjustment_method"],
        "contract_id": session_row["contract_id"],
        "session_date": session_row["session_date"],
        "session_eligible": session_row["eligible"],
        "session_eligible_for_aggregates": session_row["eligible_for_aggregates"],
        "event_type": event_type,
        "event_id": event_id,
        "direction": direction,
    }


def _not_evaluable_row(
    session_row: dict[str, Any], event_type: str, event_id: str, direction: str, reason: str
) -> dict[str, Any]:
    row = _base_event_row(session_row, event_type, event_id, direction)
    row.update(
        {
            "anchor_price": None,
            "level": None,
            "triggered": False,
            "evaluable": False,
            "not_evaluable_reason": reason,
            "event_bar_timestamp_utc": None,
            "available_at_utc": session_row["session_close_available_at_utc"],
            "first_cross_time_utc": None,
            "bars_to_cross": None,
        }
    )
    return row


def _not_triggered_row(
    session_row: dict[str, Any], event_type: str, event_id: str, direction: str, anchor_price: float, level: float
) -> dict[str, Any]:
    row = _base_event_row(session_row, event_type, event_id, direction)
    row.update(
        {
            "anchor_price": _round(anchor_price, 4),
            "level": level,
            "triggered": False,
            "evaluable": True,
            "not_evaluable_reason": None,
            "event_bar_timestamp_utc": None,
            "available_at_utc": session_row["session_close_available_at_utc"],
            "first_cross_time_utc": None,
            "bars_to_cross": None,
        }
    )
    return row


def _resolve_crossing(
    bars: list[AtlasBar],
    session_row: dict[str, Any],
    event_type: str,
    event_id: str,
    direction: str,
    anchor_price: float,
    level: float,
    session_close: float,
    horizons_minutes: tuple[int, ...],
) -> dict[str, Any]:
    row = _base_event_row(session_row, event_type, event_id, direction)
    row["anchor_price"] = _round(anchor_price, 4)
    row["level"] = _round(level, 4)
    cross_bar_time = _scan_level_crossing(bars, level, direction)
    row["evaluable"] = True
    row["not_evaluable_reason"] = None
    if cross_bar_time is None:
        row.update(
            {
                "triggered": False,
                "event_bar_timestamp_utc": None,
                "available_at_utc": session_row["session_close_available_at_utc"],
                "first_cross_time_utc": None,
                "bars_to_cross": None,
            }
        )
        return row
    available_at = cross_bar_time + M1_AVAILABILITY_OFFSET
    bars_to_cross = sum(1 for item in bars if item.timestamp < cross_bar_time)
    row.update(
        {
            "triggered": True,
            "event_bar_timestamp_utc": cross_bar_time.isoformat(),
            "available_at_utc": available_at.isoformat(),
            "first_cross_time_utc": available_at.isoformat(),
            "bars_to_cross": bars_to_cross,
            **_event_outcomes(bars, cross_bar_time, available_at, level, direction, session_close, horizons_minutes),
        }
    )
    return row


def build_event_table(
    session_rows: list[dict[str, Any]],
    session_core_bars: dict[date, list[AtlasBar]],
    checkpoint_rows: list[dict[str, Any]],
    manifest: AtlasManifest,
) -> list[dict[str, Any]]:
    """Uma linha por sessão x definição de evento; primeiro cruzamento causal
    único, só conhecido no fechamento da barra M1 em que ocorreu."""

    checkpoints_by_session: dict[str, list[dict[str, Any]]] = {}
    for row in checkpoint_rows:
        checkpoints_by_session.setdefault(row["session_date"], []).append(row)

    horizons = manifest.event_outcome_horizons_minutes
    rows: list[dict[str, Any]] = []
    for session_row in session_rows:
        session_date = date.fromisoformat(session_row["session_date"])
        bars = session_core_bars[session_date]
        session_open = session_row["session_open"]
        atr_previous = session_row["atr14_previous_session"]
        session_close = bars[-1].bar.close
        anchor = datetime.fromisoformat(session_row["expected_session_open_time_utc"])

        if not session_row["eligible_for_aggregates"]:
            exclusion_reason = "sessao_inelegivel" if not session_row["eligible"] else "sessao_de_rolagem_excluida"
            for threshold in manifest.pct_event_thresholds:
                event_id = f"pct_{threshold * 100:.2f}"
                rows.append(_not_evaluable_row(session_row, "pct_threshold", event_id, "up", exclusion_reason))
                rows.append(_not_evaluable_row(session_row, "pct_threshold", event_id, "down", exclusion_reason))
            for multiple in manifest.atr_event_multiples:
                event_id = f"atr_{multiple:.2f}"
                rows.append(_not_evaluable_row(session_row, "atr_multiple", event_id, "up", exclusion_reason))
                rows.append(_not_evaluable_row(session_row, "atr_multiple", event_id, "down", exclusion_reason))
            for minutes in manifest.opening_range_minutes:
                event_id = f"opening_range_{minutes}"
                rows.append(_not_evaluable_row(session_row, "opening_range_breakout", event_id, "up", exclusion_reason))
                rows.append(_not_evaluable_row(session_row, "opening_range_breakout", event_id, "down", exclusion_reason))
            event_id = f"range_expansion_p{int(manifest.range_expansion_percentile * 100)}"
            rows.append(_not_evaluable_row(session_row, "range_expansion", event_id, "up", exclusion_reason))
            rows.append(_not_evaluable_row(session_row, "range_expansion", event_id, "down", exclusion_reason))
            continue

        for threshold in manifest.pct_event_thresholds:
            event_id = f"pct_{threshold * 100:.2f}"
            rows.append(
                _resolve_crossing(
                    bars, session_row, "pct_threshold", event_id, "up",
                    session_open, session_open * (1 + threshold), session_close, horizons,
                )
            )
            rows.append(
                _resolve_crossing(
                    bars, session_row, "pct_threshold", event_id, "down",
                    session_open, session_open * (1 - threshold), session_close, horizons,
                )
            )

        for multiple in manifest.atr_event_multiples:
            event_id = f"atr_{multiple:.2f}"
            if atr_previous is None:
                rows.append(
                    _not_evaluable_row(session_row, "atr_multiple", event_id, "up", "atr14_previous_session_indisponivel")
                )
                rows.append(
                    _not_evaluable_row(session_row, "atr_multiple", event_id, "down", "atr14_previous_session_indisponivel")
                )
                continue
            rows.append(
                _resolve_crossing(
                    bars, session_row, "atr_multiple", event_id, "up",
                    session_open, session_open + multiple * atr_previous, session_close, horizons,
                )
            )
            rows.append(
                _resolve_crossing(
                    bars, session_row, "atr_multiple", event_id, "down",
                    session_open, session_open - multiple * atr_previous, session_close, horizons,
                )
            )

        for minutes in manifest.opening_range_minutes:
            event_id = f"opening_range_{minutes}"
            opening_range = _opening_range(bars, anchor, minutes)
            if opening_range is None:
                rows.append(
                    _not_evaluable_row(
                        session_row, "opening_range_breakout", event_id, "up", "sessao_sem_barras_no_periodo_de_abertura"
                    )
                )
                rows.append(
                    _not_evaluable_row(
                        session_row, "opening_range_breakout", event_id, "down", "sessao_sem_barras_no_periodo_de_abertura"
                    )
                )
                continue
            or_high, or_low = opening_range
            after_open_range = [item for item in bars if item.timestamp >= anchor + timedelta(minutes=minutes)]
            if not after_open_range:
                rows.append(
                    _not_evaluable_row(
                        session_row, "opening_range_breakout", event_id, "up", "sessao_sem_barras_apos_o_periodo_de_abertura"
                    )
                )
                rows.append(
                    _not_evaluable_row(
                        session_row, "opening_range_breakout", event_id, "down", "sessao_sem_barras_apos_o_periodo_de_abertura"
                    )
                )
                continue
            rows.append(
                _resolve_crossing(
                    after_open_range, session_row, "opening_range_breakout", event_id, "up",
                    or_high, or_high, session_close, horizons,
                )
            )
            rows.append(
                _resolve_crossing(
                    after_open_range, session_row, "opening_range_breakout", event_id, "down",
                    or_low, or_low, session_close, horizons,
                )
            )

        event_id = f"range_expansion_p{int(manifest.range_expansion_percentile * 100)}"
        session_checkpoints = sorted(
            checkpoints_by_session.get(session_row["session_date"], []), key=lambda item: item["checkpoint_minutes"]
        )
        trigger_checkpoint = next(
            (
                checkpoint
                for checkpoint in session_checkpoints
                if checkpoint["available"]
                and checkpoint.get("range_so_far_percentile_60") is not None
                and checkpoint["range_so_far_percentile_60"] >= manifest.range_expansion_percentile
            ),
            None,
        )
        if trigger_checkpoint is None:
            rows.append(
                _not_triggered_row(
                    session_row, "range_expansion", event_id, "up", session_open, manifest.range_expansion_percentile
                )
            )
            rows.append(
                _not_triggered_row(
                    session_row, "range_expansion", event_id, "down", session_open, manifest.range_expansion_percentile
                )
            )
        else:
            available_at = datetime.fromisoformat(trigger_checkpoint["checkpoint_time_utc"])
            event_bar_time = available_at - M1_AVAILABILITY_OFFSET
            cross_price = [item for item in bars if item.timestamp < available_at][-1].bar.close
            observed_direction = "up" if cross_price >= session_open else "down"
            for direction in ("up", "down"):
                if direction != observed_direction:
                    rows.append(
                        _not_triggered_row(
                            session_row, "range_expansion", event_id, direction, session_open,
                            manifest.range_expansion_percentile,
                        )
                    )
                    continue
                row = _base_event_row(session_row, "range_expansion", event_id, direction)
                row.update(
                    {
                        "anchor_price": _round(session_open, 4),
                        "level": manifest.range_expansion_percentile,
                        "triggered": True,
                        "evaluable": True,
                        "not_evaluable_reason": None,
                        "event_bar_timestamp_utc": event_bar_time.isoformat(),
                        "available_at_utc": available_at.isoformat(),
                        "first_cross_time_utc": available_at.isoformat(),
                        "bars_to_cross": trigger_checkpoint["bars_used"],
                        **_event_outcomes(bars, event_bar_time, available_at, cross_price, direction, session_close, horizons),
                    }
                )
                rows.append(row)

    return rows


# --------------------------------------------------------------------------
# Problemas de qualidade (auditáveis, nunca apagam linhas)
# --------------------------------------------------------------------------


def build_quality_issue_rows(
    session_rows: list[dict[str, Any]], outside_core_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in session_rows:
        if row["eligibility_reason"]:
            for reason in row["eligibility_reason"].split(";"):
                rows.append(
                    {
                        "session_date": row["session_date"],
                        "issue_type": reason,
                        "detail": f"bar_count={row['bar_count']} expected={row['expected_bar_count']}",
                    }
                )
        if row["gap_quality"] in ("roll_affected", "partial_context"):
            rows.append(
                {
                    "session_date": row["session_date"],
                    "issue_type": f"gap_{row['gap_quality']}",
                    "detail": row["gap_quality_reason"] or "",
                }
            )
        if row.get("true_range_context_quality") == "partial_context":
            rows.append(
                {
                    "session_date": row["session_date"],
                    "issue_type": "true_range_context_partial",
                    "detail": row.get("true_range_context_quality_reason") or "",
                }
            )
        if row.get("source_transition"):
            rows.append(
                {
                    "session_date": row["session_date"],
                    "issue_type": "source_transition",
                    "detail": f"source_id={row['source_id']}",
                }
            )
        if row["is_first_session"]:
            rows.append(
                {
                    "session_date": row["session_date"],
                    "issue_type": "first_session_no_previous_context",
                    "detail": "fechamento/ATR/gap anteriores não existem para a primeira sessão da série",
                }
            )
    for row in outside_core_rows:
        rows.append(
            {
                "session_date": row["session_date"],
                "issue_type": f"outside_core_session_{row['outside_core_session_reason']}",
                "detail": f"timestamp_utc={row['timestamp_utc']} source_id={row['source_id']}",
            }
        )
    return rows


# --------------------------------------------------------------------------
# Resumo agregado (para auditoria; não é um artefato exigido pela 008A)
# --------------------------------------------------------------------------


def _wilson_interval(successes: int, total: int, z: float = 1.959963985) -> tuple[float, float] | tuple[None, None]:
    if total == 0:
        return None, None
    phat = successes / total
    denom = 1 + z * z / total
    center = phat + z * z / (2 * total)
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    return _round((center - margin) / denom, 6), _round((center + margin) / denom, 6)


def _sample_classification(count: int, distinct_months: int, max_day_share: float) -> str:
    if count < 30:
        return "insufficient_sample"
    if count < 60 or distinct_months < 3 or max_day_share > 0.5:
        return "provisional"
    return "eligible_for_validation"


def _quantile_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "count": n,
        "mean": _round(sum(ordered) / n, 6),
        "median": _round(ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2, 6),
    }


def summarize_causal_atlas(
    session_rows: list[dict[str, Any]], checkpoint_rows: list[dict[str, Any]], event_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    total_sessions = len(session_rows)
    eligible_sessions = [row for row in session_rows if row["eligible"]]
    aggregate_eligible_sessions = [row for row in session_rows if row["eligible_for_aggregates"]]
    partial_sessions = [row for row in session_rows if not row["eligible"]]
    first_sessions = [row for row in session_rows if row["is_first_session"]]
    roll_sessions = [row for row in session_rows if row["gap_quality"] == "roll_affected"]
    partial_context_gaps = [row for row in session_rows if row["gap_quality"] == "partial_context"]

    volume_quality_counts: dict[str, int] = {}
    for row in session_rows:
        volume_quality_counts[row["volume_quality"]] = volume_quality_counts.get(row["volume_quality"], 0) + 1

    event_summary = []
    by_event: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in event_rows:
        by_event.setdefault((row["event_type"], row["event_id"], row["direction"]), []).append(row)
    for (event_type, event_id, direction), rows in sorted(by_event.items()):
        triggered = [row for row in rows if row["triggered"]]
        evaluable = [row for row in rows if row["evaluable"]]
        months: dict[str, int] = {}
        days: dict[str, int] = {}
        for row in triggered:
            month = row["session_date"][:7]
            months[month] = months.get(month, 0) + 1
            days[row["session_date"]] = days.get(row["session_date"], 0) + 1
        distinct_months = len(months)
        max_day_share = (max(days.values()) / len(triggered)) if triggered else 0.0
        continued = sum(1 for row in triggered if row.get("label_continued"))
        reversed_count = sum(1 for row in triggered if row.get("label_reversed"))
        event_summary.append(
            {
                "event_type": event_type,
                "event_id": event_id,
                "direction": direction,
                "evaluable_sessions": len(evaluable),
                "triggered_count": len(triggered),
                "distinct_months": distinct_months,
                "max_single_day_share": _round(max_day_share, 6),
                "sample_classification": _sample_classification(len(triggered), distinct_months, max_day_share),
                "continuation_rate": _round(continued / len(triggered), 6) if triggered else None,
                "continuation_rate_ci95": list(_wilson_interval(continued, len(triggered))),
                "reversal_rate": _round(reversed_count / len(triggered), 6) if triggered else None,
                "reversal_rate_ci95": list(_wilson_interval(reversed_count, len(triggered))),
            }
        )

    return {
        "total_sessions": total_sessions,
        "eligible_sessions": len(eligible_sessions),
        "aggregate_eligible_sessions": len(aggregate_eligible_sessions),
        "partial_sessions": len(partial_sessions),
        "first_sessions": len(first_sessions),
        "roll_affected_sessions": len(roll_sessions),
        "partial_context_gap_sessions": len(partial_context_gaps),
        "volume_quality_counts": volume_quality_counts,
        "events": event_summary,
    }


# --------------------------------------------------------------------------
# Orquestração
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CausalAtlasResult:
    session_rows: list[dict[str, Any]]
    checkpoint_rows: list[dict[str, Any]]
    event_rows: list[dict[str, Any]]
    outside_core_rows: list[dict[str, Any]]
    quality_issue_rows: list[dict[str, Any]]
    summary: dict[str, Any]
    manifest: AtlasManifest
    params_sha256: str
    bars_sha256: str
    bar_count: int
    clock_policy: str = field(default=CLOCK_POLICY)
    m1_availability_offset_minutes: float = field(default=1.0)
    core_version: int = field(default=CAUSAL_ATLAS_CORE_VERSION)


def build_causal_atlas(bars: list[AtlasBar], manifest: AtlasManifest) -> CausalAtlasResult:
    """Ponto de entrada único do núcleo causal: barras M1 fechadas + manifesto
    -> tabelas de sessão/checkpoint/evento, barras fora do núcleo e
    problemas de qualidade. Não lê Parquet nem toca disco/rede."""

    session_rows, session_core_bars, outside_core_rows = build_session_table(bars, manifest)
    checkpoint_rows = build_checkpoint_table(session_rows, session_core_bars, manifest)
    event_rows = build_event_table(session_rows, session_core_bars, checkpoint_rows, manifest)
    quality_issue_rows = build_quality_issue_rows(session_rows, outside_core_rows)
    summary = summarize_causal_atlas(session_rows, checkpoint_rows, event_rows)

    return CausalAtlasResult(
        session_rows=session_rows,
        checkpoint_rows=checkpoint_rows,
        event_rows=event_rows,
        outside_core_rows=outside_core_rows,
        quality_issue_rows=quality_issue_rows,
        summary=summary,
        manifest=manifest,
        params_sha256=manifest.params_sha256(),
        bars_sha256=bars_content_sha256(bars),
        bar_count=len(bars),
    )
