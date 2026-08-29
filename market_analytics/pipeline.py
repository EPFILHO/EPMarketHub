"""Monta a série de FeatureRow a partir de uma sequência de barras fechadas.

Invariante central: o cálculo da linha `i` usa exclusivamente `bars[0:i+1]`.
Nenhuma função aqui olha para `bars[i+1:]`. Isso é verificado por teste de
invariância de prefixo (ver `tests/test_market_analytics.py`).
"""

from __future__ import annotations

from collections.abc import Sequence

from .bars import Bar
from .config import FeatureConfig
from .features import (
    FEATURE_SCHEMA_VERSION,
    FeatureRow,
    atr_normalized,
    average_true_range,
    close_position,
    efficiency_ratio,
    log_return,
    normalized_range,
    realized_volatility,
    trend_strength,
    true_range,
    volume_relative,
)


def compute_feature_rows(
    bars: Sequence[Bar],
    config: FeatureConfig | None = None,
) -> list[FeatureRow]:
    """Calcula features determinísticas para cada barra, usando só o prefixo.

    `bars` deve estar em ordem cronológica estritamente crescente (sem
    timestamps duplicados) e conter uma única `source_id`/`symbol`/
    `timeframe`; qualquer mistura é rejeitada (ver `_validate_bar_series`).

    `config` é a única fonte de verdade para os parâmetros de janela; se
    omitido, usa `FeatureConfig()` (defaults documentados em `config.py`).
    """

    cfg = config if config is not None else FeatureConfig()
    _validate_bar_series(bars)

    rows: list[FeatureRow] = []
    true_ranges: list[float] = []
    closes: list[float] = []
    log_returns: list[float | None] = []
    volumes: list[float | None] = []
    qualities: list[str] = []

    for index, bar in enumerate(bars):
        previous = bars[index - 1] if index > 0 else None

        tr = true_range(bar, previous)
        true_ranges.append(tr)
        atr = average_true_range(true_ranges, cfg.atr_period)

        ret = log_return(bar.close, previous.close if previous else None)
        closes.append(bar.close)

        rel_vol = volume_relative(
            bar.volume,
            bar.volume_quality,
            volumes,
            qualities,
            cfg.volume_window,
        )

        log_returns.append(ret)

        row = FeatureRow(
            schema_version=FEATURE_SCHEMA_VERSION,
            source_id=bar.source_id,
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            timestamp=bar.timestamp,
            true_range=tr,
            atr=atr,
            atr_normalized=atr_normalized(atr, bar.close),
            log_return=ret,
            realized_volatility=realized_volatility(log_returns, cfg.volatility_window),
            normalized_range=normalized_range(bar),
            efficiency_ratio=efficiency_ratio(closes, cfg.trend_window),
            trend_strength=trend_strength(closes, cfg.trend_window),
            close_position=close_position(bar),
            volume_relative=rel_vol,
            volume_quality=bar.volume_quality,
        )
        rows.append(row)

        volumes.append(bar.volume)
        qualities.append(bar.volume_quality)

    return rows


def _validate_bar_series(bars: Sequence[Bar]) -> None:
    """Garante uma única fonte/símbolo/timeframe e timestamps crescentes."""

    if not bars:
        return

    first = bars[0]
    for bar in bars:
        if (bar.source_id, bar.symbol, bar.timeframe) != (
            first.source_id,
            first.symbol,
            first.timeframe,
        ):
            raise ValueError(
                "compute_feature_rows exige uma única combinação de "
                "source_id/symbol/timeframe por chamada; encontrado "
                f"{(first.source_id, first.symbol, first.timeframe)!r} e "
                f"{(bar.source_id, bar.symbol, bar.timeframe)!r}"
            )

    for previous, current in zip(bars, bars[1:], strict=False):
        if current.timestamp <= previous.timestamp:
            raise ValueError(
                "timestamps devem ser estritamente crescentes e sem duplicatas: "
                f"{previous.timestamp!r} seguido de {current.timestamp!r}"
            )
