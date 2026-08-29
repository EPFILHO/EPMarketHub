"""Configuração reproduzível do cálculo de features.

`FeatureConfig` é a única fonte de verdade para os parâmetros de janela. Ela
é persistida integralmente no artefato (`storage.py`) para que o consumidor
(Fusion Quant) saiba exatamente com quais parâmetros cada linha foi
calculada, sem precisar adivinhar ou assumir os defaults do produtor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

_POSITIVE_INT_FIELDS = ("atr_period", "volatility_window", "trend_window", "volume_window")


@dataclass(frozen=True)
class FeatureConfig:
    """Janelas usadas no cálculo de features. Todas inteiros positivos.

    - `atr_period`: número de true ranges para a média móvel simples do ATR.
    - `volatility_window`: número de retornos log válidos exigidos para a
      volatilidade realizada.
    - `trend_window`: número de variações de fechamento exigidas (ou seja,
      `trend_window + 1` fechamentos) para efficiency ratio e trend strength.
    - `volume_window`: número exato de barras imediatamente anteriores usado
      na média de volume relativo.
    """

    atr_period: int = 14
    volatility_window: int = 14
    trend_window: int = 14
    volume_window: int = 20

    def __post_init__(self) -> None:
        for field_name in _POSITIVE_INT_FIELDS:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} deve ser um inteiro positivo (recebido: {value!r})")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureConfig:
        if not isinstance(data, dict):
            raise ValueError(f"config deve ser um objeto JSON (recebido: {data!r})")
        expected = set(_POSITIVE_INT_FIELDS)
        actual = set(data)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                "config incompatível com o schema: "
                f"campos ausentes={missing}, campos extras={extra}"
            )
        return cls(**data)
