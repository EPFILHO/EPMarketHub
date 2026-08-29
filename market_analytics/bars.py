"""Modelo de barra fechada, neutro para qualquer mercado ou corretora."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from numbers import Real
from typing import Literal

VolumeQuality = Literal["exchange", "tick_proxy", "missing"]

VOLUME_QUALITIES: frozenset[str] = frozenset({"exchange", "tick_proxy", "missing"})


@dataclass(frozen=True)
class Bar:
    """Uma barra (candle) já fechada de um símbolo em um timeframe.

    `source_id` identifica a origem (terminal/corretora/feed) que produziu a
    barra. É obrigatório e não vazio porque o mesmo `symbol` pode existir em
    fontes distintas (ex.: o mesmo ativo cotado por duas corretoras) com
    preços e horários de fechamento diferentes; sem `source_id`, essas séries
    colidiriam silenciosamente.

    O volume nunca é presumido. `volume_quality` declara explicitamente a
    origem do dado:

    - ``"exchange"``: volume real reportado pela fonte (ex.: B3, bolsas).
    - ``"tick_proxy"``: contagem de ticks usada como aproximação de volume
      (comum em Forex/CFDs no MT5, onde não há volume real de mercado).
    - ``"missing"``: a fonte não fornece nenhuma medida de volume.

    Quando `volume_quality` é ``"missing"``, `volume` deve ser `None` — nunca
    zero. Um zero imputado seria indistinguível de um período real sem
    negociação e contaminaria qualquer feature derivada.
    """

    source_id: str
    symbol: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    volume_quality: VolumeQuality

    def __post_init__(self) -> None:
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
                f"Bar.timestamp deve ser timezone-aware (recebido: {self.timestamp!r})"
            )

        for field_name in ("open", "high", "low", "close"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
                raise ValueError(f"{field_name} deve ser finito (recebido: {value!r})")

        if not isinstance(self.volume_quality, str) or self.volume_quality not in VOLUME_QUALITIES:
            raise ValueError(
                f"volume_quality inválido: {self.volume_quality!r}. "
                f"Use um de {sorted(VOLUME_QUALITIES)}."
            )
        if self.volume_quality == "missing" and self.volume is not None:
            raise ValueError("volume deve ser None quando volume_quality == 'missing'")
        if self.volume_quality != "missing" and self.volume is None:
            raise ValueError(
                "volume não pode ser None quando volume_quality declara uma origem "
                "('exchange' ou 'tick_proxy')"
            )
        if self.volume is not None:
            if (
                isinstance(self.volume, bool)
                or not isinstance(self.volume, Real)
                or not math.isfinite(self.volume)
            ):
                raise ValueError(f"volume deve ser finito (recebido: {self.volume!r})")
            if self.volume < 0:
                raise ValueError(f"volume não pode ser negativo (recebido: {self.volume!r})")

        if self.high < self.low:
            raise ValueError(f"high ({self.high}) não pode ser menor que low ({self.low})")
        if self.high < max(self.open, self.close):
            raise ValueError(
                f"high ({self.high}) deve ser >= max(open, close) ({max(self.open, self.close)})"
            )
        if self.low > min(self.open, self.close):
            raise ValueError(
                f"low ({self.low}) deve ser <= min(open, close) ({min(self.open, self.close)})"
            )
