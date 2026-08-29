"""Linha de features derivada de um prefixo de barras e funções de cálculo.

Todas as funções de cálculo neste módulo são puras, determinísticas e usam
somente a biblioteca padrão. Cada uma recebe explicitamente o prefixo de
dados disponível até a barra corrente (índice `i`) — nunca a série completa —
para tornar a ausência de vazamento (leakage) de dados futuros óbvia na
própria assinatura.

## Semântica de warm-up (alinhada ao consumidor/Fusion Quant)

Cada métrica de janela retorna `None` até que a janela completa e definida
por `FeatureConfig` esteja disponível — nunca uma média parcial encolhida.
Isso torna o warm-up comparável entre produtor e consumidor: um valor
não-`None` sempre representa exatamente `window` (ou `window + 1`)
observações, nunca uma amostra menor calculada silenciosamente.

- `atr`: `None` até acumular `atr_period` true ranges; depois, média móvel
  simples exata dessa janela.
- `realized_volatility`: olha exatamente as últimas `volatility_window`
  posições da sequência de retornos (incluindo `None`s, sem costurar
  lacunas). Se a janela não estiver completa **ou** qualquer posição dentro
  dela for `None` (preço não positivo, primeira barra etc.), o resultado é
  `None`. Uma lacuna no meio da série empurra o próximo valor válido para
  depois de uma nova janela contígua completa de retornos.
- `efficiency_ratio` / `trend_strength`: `None` até existirem
  `trend_window + 1` fechamentos; depois, calculados sobre exatamente essa
  janela.
- `volume_relative`: exige exatamente as `volume_window` barras imediatamente
  anteriores, todas com a mesma `volume_quality` da barra atual e volume
  não-`None`. Qualquer barra `missing` ou de qualidade diferente na janela
  invalida o resultado inteiro (`None`) — não há mistura parcial.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from numbers import Real
from statistics import pstdev
from typing import Any

from .bars import Bar, VolumeQuality

FEATURE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FeatureRow:
    """Uma linha de features alinhada 1:1 com uma barra fechada.

    Todos os campos numéricos são `float | None`. `None` significa
    "não computável com o histórico disponível" (ex.: warm-up incompleto) ou
    "qualidade de volume insuficiente" — nunca um valor imputado.
    """

    schema_version: int
    source_id: str
    symbol: str
    timeframe: str
    timestamp: datetime
    true_range: float | None
    atr: float | None
    atr_normalized: float | None
    log_return: float | None
    realized_volatility: float | None
    normalized_range: float | None
    efficiency_ratio: float | None
    trend_strength: float | None
    close_position: float | None
    volume_relative: float | None
    volume_quality: VolumeQuality

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != FEATURE_SCHEMA_VERSION
        ):
            raise ValueError(
                f"schema_version incompatível: esperado {FEATURE_SCHEMA_VERSION}, "
                f"recebido {self.schema_version!r}"
            )
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("source_id não pode ser vazio")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol não pode ser vazio")
        if not isinstance(self.timeframe, str) or not self.timeframe.strip():
            raise ValueError("timeframe não pode ser vazio")
        if (
            not isinstance(self.timestamp, datetime)
            or self.timestamp.tzinfo is None
            or self.timestamp.tzinfo.utcoffset(self.timestamp) is None
        ):
            raise ValueError(
                f"FeatureRow.timestamp deve ser timezone-aware (recebido: {self.timestamp!r})"
            )
        if not isinstance(self.volume_quality, str) or self.volume_quality not in (
            "exchange",
            "tick_proxy",
            "missing",
        ):
            raise ValueError(f"volume_quality inválido: {self.volume_quality!r}")

        self._check_finite("true_range", self.true_range, minimum=0.0)
        self._check_finite("atr", self.atr, minimum=0.0)
        self._check_finite("atr_normalized", self.atr_normalized, minimum=0.0)
        self._check_finite("log_return", self.log_return)
        self._check_finite("realized_volatility", self.realized_volatility, minimum=0.0)
        self._check_finite("normalized_range", self.normalized_range, minimum=0.0)
        self._check_finite("efficiency_ratio", self.efficiency_ratio, minimum=0.0, maximum=1.0)
        self._check_finite("trend_strength", self.trend_strength, minimum=-1.0, maximum=1.0)
        self._check_finite("close_position", self.close_position, minimum=0.0, maximum=1.0)
        self._check_finite("volume_relative", self.volume_relative, minimum=0.0)

    def _check_finite(
        self,
        name: str,
        value: float | None,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f"{name} deve ser finito (recebido: {value!r})")
        if minimum is not None and value < minimum - 1e-9:
            raise ValueError(f"{name} abaixo do mínimo esperado {minimum} (recebido: {value!r})")
        if maximum is not None and value > maximum + 1e-9:
            raise ValueError(f"{name} acima do máximo esperado {maximum} (recebido: {value!r})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp.isoformat(),
            "true_range": self.true_range,
            "atr": self.atr,
            "atr_normalized": self.atr_normalized,
            "log_return": self.log_return,
            "realized_volatility": self.realized_volatility,
            "normalized_range": self.normalized_range,
            "efficiency_ratio": self.efficiency_ratio,
            "trend_strength": self.trend_strength,
            "close_position": self.close_position,
            "volume_relative": self.volume_relative,
            "volume_quality": self.volume_quality,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureRow:
        return cls(
            schema_version=data["schema_version"],
            source_id=data["source_id"],
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            true_range=data["true_range"],
            atr=data["atr"],
            atr_normalized=data["atr_normalized"],
            log_return=data["log_return"],
            realized_volatility=data["realized_volatility"],
            normalized_range=data["normalized_range"],
            efficiency_ratio=data["efficiency_ratio"],
            trend_strength=data["trend_strength"],
            close_position=data["close_position"],
            volume_relative=data["volume_relative"],
            volume_quality=data["volume_quality"],
        )


def true_range(current: Bar, previous: Bar | None) -> float:
    """True range clássico. Sem barra anterior, degenera para high-low."""

    if previous is None:
        return current.high - current.low
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def average_true_range(true_ranges: Sequence[float], period: int) -> float | None:
    """Média móvel simples exata de `period` true ranges.

    Retorna `None` até que `period` valores estejam disponíveis — sem janela
    parcial encolhida, para permanecer comparável ao lado do consumidor.
    """

    if len(true_ranges) < period:
        return None
    window = true_ranges[-period:]
    return sum(window) / period


def log_return(current_close: float, previous_close: float | None) -> float | None:
    if previous_close is None or previous_close <= 0 or current_close <= 0:
        return None
    return math.log(current_close / previous_close)


def realized_volatility(log_returns: Sequence[float | None], window: int) -> float | None:
    """Desvio padrão populacional das últimas `window` posições, sem costura.

    `log_returns` é a sequência completa de retornos na ordem em que
    ocorreram, incluindo `None` onde não houve retorno válido (primeira
    barra, preço não positivo). Esta função nunca pula ou filtra `None`
    globalmente: olha exatamente `log_returns[-window:]` e retorna `None` se
    a janela não estiver completa ou se qualquer posição dentro dela for
    `None`. Uma lacuna no meio da série não é "costurada" — o resultado só
    volta depois que uma nova janela contígua e completamente válida se
    forma à frente da lacuna.
    """

    if len(log_returns) < window:
        return None
    segment = log_returns[-window:]
    if any(value is None for value in segment):
        return None
    values = [float(value) for value in segment if value is not None]
    return pstdev(values)


def normalized_range(bar: Bar) -> float | None:
    if bar.close <= 0:
        return None
    return (bar.high - bar.low) / bar.close


def atr_normalized(atr: float | None, close: float) -> float | None:
    if atr is None or close <= 0:
        return None
    return atr / close


def efficiency_ratio(closes: Sequence[float], window: int) -> float | None:
    """Kaufman efficiency ratio (0..1) sobre exatamente `window + 1` closes.

    Direção líquida dividida pela soma dos deslocamentos absolutos. Retorna
    `None` até que `window + 1` fechamentos estejam disponíveis.
    """

    return _signed_efficiency(closes, window, absolute=True)


def trend_strength(closes: Sequence[float], window: int) -> float | None:
    """Versão assinada do efficiency ratio, em [-1, 1].

    Positivo indica tendência de alta líquida dentro da janela; negativo,
    tendência de baixa. Zero ou próximo de zero indica mercado sem direção
    clara (lateralização), independentemente da volatilidade bruta.
    """

    return _signed_efficiency(closes, window, absolute=False)


def _signed_efficiency(closes: Sequence[float], window: int, *, absolute: bool) -> float | None:
    if len(closes) < window + 1:
        return None
    segment = closes[-(window + 1) :]
    direction = segment[-1] - segment[0]
    volatility = sum(abs(segment[idx] - segment[idx - 1]) for idx in range(1, len(segment)))
    if volatility == 0:
        return None
    value = direction / volatility
    return abs(value) if absolute else value


def close_position(bar: Bar) -> float | None:
    span = bar.high - bar.low
    if span <= 0:
        return None
    return (bar.close - bar.low) / span


def volume_relative(
    current_volume: float | None,
    current_quality: VolumeQuality,
    history_volumes: Sequence[float | None],
    history_qualities: Sequence[VolumeQuality],
    window: int,
) -> float | None:
    """Volume atual sobre a média das exatas `window` barras anteriores.

    Exige que as `window` barras imediatamente anteriores (não uma amostra
    parcial) tenham todas a mesma `volume_quality` da barra atual e volume
    não-`None`. `exchange` nunca é misturado com `tick_proxy`, e qualquer
    `missing` na janela invalida o resultado inteiro.
    """

    if current_quality == "missing" or current_volume is None:
        return None
    if len(history_volumes) < window:
        return None
    window_volumes = history_volumes[-window:]
    window_qualities = history_qualities[-window:]
    if any(quality != current_quality for quality in window_qualities):
        return None
    if any(volume is None for volume in window_volumes):
        return None
    average = sum(window_volumes) / window
    if average <= 0:
        return None
    return current_volume / average
