"""Registro puro dos contratos B3 acompanhados até a expiração."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .daily_capture import DailyCaptureSession
from .futures_series import describe_b3_contract

TRACKER_SCHEMA = "ep_market_hub.b3_contract_tracker"
TRACKER_SCHEMA_VERSION = 1
B3_ZONE = ZoneInfo("America/Sao_Paulo")


def empty_tracker() -> dict[str, Any]:
    return {
        "schema": TRACKER_SCHEMA,
        "schema_version": TRACKER_SCHEMA_VERSION,
        "updated_at_utc": None,
        "contracts": [],
    }


def _validate_tracker(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("schema") != TRACKER_SCHEMA or payload.get("schema_version") != TRACKER_SCHEMA_VERSION:
        raise ValueError("registro de contratos possui schema incompatível")
    rows = payload.get("contracts")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("registro de contratos inválido")
    symbols: set[str] = set()
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip() or symbol in symbols:
            raise ValueError("registro contém símbolo vazio ou duplicado")
        symbols.add(symbol)
    return rows


def _row_covers_session(row: dict[str, Any], session_date: date) -> bool:
    """True se a expiração já registrada no ``row`` ainda cobre ``session_date``.

    Usa apenas o que já está persistido no registro (não o estado ao vivo do
    terminal), para decidir se um símbolo que sumiu de ``symbol_states``
    ainda tinha uma sessão pendente de captura.
    """

    expiration_text = row.get("expiration_utc")
    if not isinstance(expiration_text, str) or not expiration_text:
        return False
    try:
        expiration_dt = datetime.fromisoformat(expiration_text)
    except (TypeError, ValueError):
        return False
    if expiration_dt.tzinfo is None:
        return False
    return session_date <= expiration_dt.astimezone(B3_ZONE).date()


def _row_is_final_session(row: dict[str, Any], session_date: date) -> bool:
    """True apenas se ``session_date`` for exatamente o dia de vencimento
    (em fuso B3) já registrado no ``row`` — a última sessão possível dele.

    Diferente de `_row_covers_session` (que aceita qualquer dia até o
    vencimento, inclusive), esta função exige igualdade exata. É o único
    dia em que a confirmação do catálogo pode legitimamente encerrar o
    acompanhamento: em qualquer dia anterior ainda restam sessões futuras
    obrigatórias, mesmo que a sessão de hoje já esteja arquivada.
    """

    expiration_text = row.get("expiration_utc")
    if not isinstance(expiration_text, str) or not expiration_text:
        return False
    try:
        expiration_dt = datetime.fromisoformat(expiration_text)
    except (TypeError, ValueError):
        return False
    if expiration_dt.tzinfo is None:
        return False
    return session_date == expiration_dt.astimezone(B3_ZONE).date()


def update_contract_tracker(
    tracker: dict[str, Any],
    active_sessions: tuple[DailyCaptureSession, ...],
    symbol_states: dict[str, dict[str, Any]],
    *,
    now_utc: datetime,
    session_date: date,
    catalog_confirmed_symbols: frozenset[str] = frozenset(),
) -> tuple[tuple[DailyCaptureSession, ...], dict[str, Any], tuple[dict[str, Any], ...]]:
    """Adiciona o mais líquido e mantém contratos anteriores até expirar.

    Retorna o plano do dia, o novo registro e uma tupla de *issues*
    estruturadas. Um contrato rastreado que some da corretora permanece no
    registro como ``unavailable``; nunca é trocado silenciosamente por outro
    nome. Se o registro indica que a sessão pedida ainda estava dentro da
    janela de vencimento conhecida daquele contrato (ou seja, a última
    captura esperada ainda não tinha acontecido), o estado vira
    ``missing_before_expiration`` e uma issue é emitida — essa distinção
    nunca deve ficar em silêncio, pois representa uma sessão obrigatória que
    não pôde ser capturada.

    ``catalog_confirmed_symbols`` é a única exceção a essa regra, e só vale
    no dia exato de vencimento: se ``session_date`` for a última sessão
    conhecida do contrato (`_row_is_final_session`) E o CHAMADOR já
    confirmou (fora deste módulo, que continua puro e não toca
    catálogo/MT5) que essa sessão está ``completed``/``empty`` e verificada
    no catálogo, sumir do terminal é o término normal do ciclo — vira
    ``expired``, nunca ``missing_before_expiration``. Em qualquer sessão
    ANTES do vencimento, mesmo já arquivada no catálogo, o desaparecimento
    continua ``missing_before_expiration``: ainda restam sessões futuras
    obrigatórias até o vencimento real, e a confirmação de hoje não prova
    nada sobre elas.
    """

    if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
        raise ValueError("now_utc deve ser timezone-aware em UTC")
    rows = {row["symbol"]: dict(row) for row in _validate_tracker(tracker)}
    observed_at = now_utc.isoformat()
    active_by_symbol = {session.selection.spec.symbol: session for session in active_sessions}
    issues: list[dict[str, Any]] = []

    for symbol, session in active_by_symbol.items():
        row = rows.setdefault(
            symbol,
            {
                "symbol": symbol,
                "instrument": session.selection.spec.instrument,
                "logical_id": session.selection.spec.logical_id,
                "contract_month": session.selection.contract_month.isoformat(),
                "first_observed_utc": observed_at,
            },
        )
        row.update(
            {
                "last_observed_utc": observed_at,
                "expiration_utc": (
                    session.selection.expiration_utc.isoformat()
                    if session.selection.expiration_utc is not None
                    else row.get("expiration_utc")
                ),
                "state": "active",
            }
        )

    plan_by_symbol = dict(active_by_symbol)
    for symbol, row in rows.items():
        if symbol in active_by_symbol:
            continue
        state = symbol_states.get(symbol)
        if state is None:
            if not _row_covers_session(row, session_date):
                row.update({"state": "unavailable", "last_observed_utc": observed_at})
            elif _row_is_final_session(row, session_date) and symbol in catalog_confirmed_symbols:
                # Só no dia exato de vencimento a confirmação do catálogo
                # encerra o acompanhamento: sumir agora é o término normal
                # do ciclo, não uma perda. Antes disso, mesmo confirmada,
                # ainda restam sessões futuras obrigatórias.
                row.update({"state": "expired", "last_observed_utc": observed_at})
            else:
                row.update({"state": "missing_before_expiration", "last_observed_utc": observed_at})
                issues.append(
                    {
                        "type": "missing_before_expiration",
                        "symbol": symbol,
                        "instrument": row.get("instrument"),
                        "logical_id": row.get("logical_id"),
                        "session_date": session_date.isoformat(),
                        "expiration_utc": row.get("expiration_utc"),
                        "message": (
                            f"{symbol} desapareceu do terminal antes de capturar a sessão de "
                            f"vencimento esperada ({session_date.isoformat()})."
                        ),
                    }
                )
            continue
        expiration_value = state.get("expiration_time")
        expiration_utc = None
        if expiration_value not in (None, 0, ""):
            try:
                expiration_utc = datetime.fromtimestamp(float(expiration_value), tz=ZoneInfo("UTC"))
            except (TypeError, ValueError, OSError, OverflowError):
                expiration_utc = None
        covers_requested_session = (
            expiration_utc is not None
            and session_date <= expiration_utc.astimezone(B3_ZONE).date()
        )
        selection = describe_b3_contract(
            symbol,
            state,
            now_utc=now_utc,
            selection_reason="tracked_until_expiration",
            allow_expired=covers_requested_session,
            allow_non_tradable=covers_requested_session,
        )
        if selection is None:
            expiration_text = row.get("expiration_utc")
            expired = False
            if isinstance(expiration_text, str) and expiration_text:
                try:
                    expired = datetime.fromisoformat(expiration_text) <= now_utc
                except (TypeError, ValueError):
                    expired = False
            row.update(
                {
                    "state": "expired" if expired else "unavailable",
                    "last_observed_utc": observed_at,
                }
            )
            continue
        row.update(
            {
                "state": "tracking_until_expiration",
                "last_observed_utc": observed_at,
                "expiration_utc": (
                    selection.expiration_utc.isoformat()
                    if selection.expiration_utc is not None
                    else row.get("expiration_utc")
                ),
            }
        )
        plan_by_symbol[symbol] = DailyCaptureSession(selection=selection, session_date=session_date)

    ordered_plan = tuple(
        sorted(
            plan_by_symbol.values(),
            key=lambda item: (item.selection.spec.instrument, item.selection.contract_month),
        )
    )
    new_tracker = {
        "schema": TRACKER_SCHEMA,
        "schema_version": TRACKER_SCHEMA_VERSION,
        "updated_at_utc": observed_at,
        "contracts": sorted(rows.values(), key=lambda row: (row["instrument"], row["contract_month"])),
    }
    return ordered_plan, new_tracker, tuple(issues)
