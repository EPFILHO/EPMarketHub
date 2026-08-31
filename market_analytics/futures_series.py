"""Identidade e seleção de séries de futuros B3.

Separa três fatos que não podem ser misturados silenciosamente:

* o contrato individual realmente negociado (preço bruto, sem ajuste);
* a regra usada para trocar de contrato numa série contínua;
* o ajuste aplicado ao histórico depois da troca.

O módulo é puro: não importa MT5 nem toca o disco. Os metadados produzidos
acompanham cada solicitação de backfill e, portanto, cada Parquet.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

SERIES_KINDS = frozenset({"continuous", "individual_contract"})
ROLL_RULES = frozenset({"liquidity", "expiration", "none"})
ADJUSTMENT_METHODS = frozenset({"proportional", "difference", "none"})

_MONTH_CODES = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}
_CONTRACT_RE = re.compile(r"^(WIN|WDO)([FGHJKMNQUVXZ])(\d{2})$", re.IGNORECASE)


@dataclass(frozen=True)
class FuturesSeriesSpec:
    logical_id: str
    instrument: str
    symbol: str
    series_kind: str
    roll_rule: str
    adjustment_method: str
    analytics_roles: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.series_kind not in SERIES_KINDS:
            raise ValueError(f"series_kind inválido: {self.series_kind!r}")
        if self.roll_rule not in ROLL_RULES:
            raise ValueError(f"roll_rule inválido: {self.roll_rule!r}")
        if self.adjustment_method not in ADJUSTMENT_METHODS:
            raise ValueError(f"adjustment_method inválido: {self.adjustment_method!r}")
        if self.series_kind == "individual_contract" and (
            self.roll_rule != "none" or self.adjustment_method != "none"
        ):
            raise ValueError("contrato individual não pode declarar rolagem ou ajuste")
        for value, name in (
            (self.logical_id, "logical_id"),
            (self.instrument, "instrument"),
            (self.symbol, "symbol"),
        ):
            if not value.strip():
                raise ValueError(f"{name} não pode ser vazio")
        if not self.analytics_roles or not all(role.strip() for role in self.analytics_roles):
            raise ValueError("analytics_roles deve conter ao menos uma finalidade")

    def provenance(self) -> dict[str, str]:
        return {
            "series_kind": self.series_kind,
            "instrument": self.instrument,
            "source_symbol": self.symbol,
            "roll_rule": self.roll_rule,
            "adjustment_method": self.adjustment_method,
            "analytics_roles": ",".join(self.analytics_roles),
        }

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["analytics_roles"] = list(self.analytics_roles)
        return result


def _continuous_specs(root: str) -> tuple[FuturesSeriesSpec, ...]:
    lower = root.lower()
    return (
        FuturesSeriesSpec(
            f"{lower}_cont_liq_ratio",
            lower,
            f"{root}$",
            "continuous",
            "liquidity",
            "proportional",
            ("regime", "relative_returns", "cross_asset"),
        ),
        FuturesSeriesSpec(
            f"{lower}_cont_liq_diff",
            lower,
            f"{root}$D",
            "continuous",
            "liquidity",
            "difference",
            ("point_calibration", "absolute_atr"),
        ),
        FuturesSeriesSpec(
            f"{lower}_cont_exp_ratio",
            lower,
            f"{root}@",
            "continuous",
            "expiration",
            "proportional",
            ("regime_fallback", "relative_returns"),
        ),
        FuturesSeriesSpec(
            f"{lower}_cont_exp_diff",
            lower,
            f"{root}@D",
            "continuous",
            "expiration",
            "difference",
            ("point_control",),
        ),
    )


B3_CONTINUOUS_SERIES = _continuous_specs("WIN") + _continuous_specs("WDO")


@dataclass(frozen=True)
class ParsedContract:
    symbol: str
    root: str
    month_code: str
    year: int
    month: int

    @property
    def contract_month(self) -> date:
        return date(self.year, self.month, 1)


def parse_b3_contract_symbol(symbol: str) -> ParsedContract | None:
    match = _CONTRACT_RE.fullmatch(str(symbol).strip())
    if match is None:
        return None
    root, month_code, short_year = match.groups()
    month_code = month_code.upper()
    return ParsedContract(
        symbol=str(symbol).strip(),
        root=root.upper(),
        month_code=month_code,
        year=2000 + int(short_year),
        month=_MONTH_CODES[month_code],
    )


@dataclass(frozen=True)
class ContractSelection:
    spec: FuturesSeriesSpec
    contract_month: date
    expiration_utc: datetime | None
    selection_reason: str
    session_volume: float
    session_deals: int

    def provenance(self) -> dict[str, str]:
        result = self.spec.provenance()
        result["contract_month"] = self.contract_month.isoformat()
        if self.expiration_utc is not None:
            result["contract_expiration_utc"] = self.expiration_utc.isoformat()
        return result

    def selection_evidence(self) -> dict[str, Any]:
        return {
            "selection_reason": self.selection_reason,
            "session_volume": self.session_volume,
            "session_deals": self.session_deals,
            "observed_expiration_utc": (
                self.expiration_utc.isoformat() if self.expiration_utc is not None else None
            ),
        }


def _as_utc_timestamp(value: Any) -> datetime | None:
    if value in (None, 0, ""):
        return None
    try:
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return parsed


def _non_negative_number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number)


def select_current_b3_contract(
    root: str,
    symbol_states: dict[str, dict[str, Any]],
    *,
    now_utc: datetime,
) -> ContractSelection | None:
    """Seleciona o contrato negociável mais líquido observável no terminal.

    A expiração informada pela corretora é a fronteira principal. Entre os
    elegíveis, volume da sessão e negócios vencem; na ausência dessas
    medidas, cotação válida e o vencimento mais próximo são o fallback
    explícito. Nunca usa um curinga para escolher silenciosamente um nome.
    """

    if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
        raise ValueError("now_utc deve ser timezone-aware em UTC")
    root = root.upper().strip()
    if root not in {"WIN", "WDO"}:
        raise ValueError(f"raiz B3 não suportada: {root!r}")

    candidates: list[tuple[ParsedContract, dict[str, Any], datetime | None, float, int]] = []
    for symbol, state in symbol_states.items():
        parsed = parse_b3_contract_symbol(symbol)
        if parsed is None or parsed.root != root or not bool(state.get("tradable", True)):
            continue
        expiration = _as_utc_timestamp(state.get("expiration_time"))
        if expiration is not None and expiration <= now_utc:
            continue
        volume = _non_negative_number(state.get("session_volume"))
        deals = int(_non_negative_number(state.get("session_deals")))
        candidates.append((parsed, state, expiration, volume, deals))
    if not candidates:
        return None

    has_liquidity = any(volume > 0 or deals > 0 for _, _, _, volume, deals in candidates)

    def rank(item: tuple[ParsedContract, dict[str, Any], datetime | None, float, int]) -> tuple[Any, ...]:
        parsed, state, _expiration, volume, deals = item
        return (
            -volume if has_liquidity else 0,
            -deals if has_liquidity else 0,
            0 if bool(state.get("has_quote", False)) else 1,
            parsed.contract_month,
            parsed.symbol.casefold(),
        )

    parsed, _state, expiration, volume, deals = min(candidates, key=rank)
    reason = "highest_session_liquidity" if has_liquidity else "nearest_tradable_with_quote"
    return describe_b3_contract(
        parsed.symbol,
        _state,
        now_utc=now_utc,
        selection_reason=reason,
    )


def describe_b3_contract(
    symbol: str,
    state: dict[str, Any],
    *,
    now_utc: datetime,
    selection_reason: str,
    allow_expired: bool = False,
    allow_non_tradable: bool = False,
) -> ContractSelection | None:
    """Constrói a identidade auditável de um contrato exato elegível."""

    if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
        raise ValueError("now_utc deve ser timezone-aware em UTC")
    parsed = parse_b3_contract_symbol(symbol)
    if parsed is None or (not allow_non_tradable and not bool(state.get("tradable", True))):
        return None
    expiration = _as_utc_timestamp(state.get("expiration_time"))
    if not allow_expired and expiration is not None and expiration <= now_utc:
        return None
    volume = _non_negative_number(state.get("session_volume"))
    deals = int(_non_negative_number(state.get("session_deals")))
    spec = FuturesSeriesSpec(
        logical_id=f"{parsed.root.lower()}_contract_{parsed.symbol.lower()}",
        instrument=parsed.root.lower(),
        symbol=parsed.symbol,
        series_kind="individual_contract",
        roll_rule="none",
        adjustment_method="none",
        analytics_roles=("execution_truth", "point_calibration", "regime_source"),
    )
    return ContractSelection(
        spec=spec,
        contract_month=parsed.contract_month,
        expiration_utc=expiration,
        selection_reason=selection_reason,
        session_volume=volume,
        session_deals=deals,
    )


def get_continuous_spec(symbol: str) -> FuturesSeriesSpec | None:
    folded = str(symbol).strip().casefold()
    return next((spec for spec in B3_CONTINUOUS_SERIES if spec.symbol.casefold() == folded), None)
