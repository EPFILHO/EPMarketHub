from __future__ import annotations

from statistics import mean
from typing import Iterable


USD_BASE_PAIRS = {"USDJPY", "USDCHF", "USDCAD"}
USD_QUOTE_PAIRS = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"}


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / previous * 100


def simple_usd_strength(pair_changes: dict[str, float]) -> float | None:
    """Calcula força aproximada do dólar com base em variações percentuais.

    Pares em que USD é base entram positivos; pares em que USD é cotado entram
    invertidos. Ex.: EURUSD subindo = dólar fraco, então sinal negativo.
    """
    values = []
    for pair, change in pair_changes.items():
        pair = pair.upper()
        if pair in USD_BASE_PAIRS:
            values.append(change)
        elif pair in USD_QUOTE_PAIRS:
            values.append(-change)
    return mean(values) if values else None


def correlation(xs: Iterable[float], ys: Iterable[float]) -> float | None:
    x = list(xs)
    y = list(ys)
    if len(x) != len(y) or len(x) < 2:
        return None
    mx, my = mean(x), mean(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    den_x = sum((a - mx) ** 2 for a in x) ** 0.5
    den_y = sum((b - my) ** 2 for b in y) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)
