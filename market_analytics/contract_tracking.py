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


def update_contract_tracker(
    tracker: dict[str, Any],
    active_sessions: tuple[DailyCaptureSession, ...],
    symbol_states: dict[str, dict[str, Any]],
    *,
    now_utc: datetime,
    session_date: date,
) -> tuple[tuple[DailyCaptureSession, ...], dict[str, Any]]:
    """Adiciona o mais líquido e mantém contratos anteriores até expirar.

    Retorna o plano do dia e o novo registro. Um contrato rastreado que some
    da corretora permanece no registro como ``unavailable``; nunca é trocado
    silenciosamente por outro nome.
    """

    if now_utc.tzinfo is None or now_utc.utcoffset() != timedelta(0):
        raise ValueError("now_utc deve ser timezone-aware em UTC")
    rows = {row["symbol"]: dict(row) for row in _validate_tracker(tracker)}
    observed_at = now_utc.isoformat()
    active_by_symbol = {session.selection.spec.symbol: session for session in active_sessions}

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
            row.update({"state": "unavailable", "last_observed_utc": observed_at})
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
    return ordered_plan, new_tracker
