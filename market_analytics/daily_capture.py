"""Planejamento puro da captura incremental dos contratos atuais B3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from .futures_series import ContractSelection, select_current_b3_contract


@dataclass(frozen=True)
class DailyCaptureSession:
    selection: ContractSelection
    session_date: date

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.selection.spec.logical_id,
            "symbol": self.selection.spec.symbol,
            "session_date": self.session_date.isoformat(),
            "provenance": self.selection.provenance(),
            "selection_evidence": self.selection.selection_evidence(),
        }


def latest_closed_weekday(as_of_date: date) -> date:
    """Último dia candidato já encerrado; feriados são confirmados pela fonte."""

    candidate = as_of_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def latest_closed_b3_session(
    now_local: datetime,
    *,
    conservative_close: time = time(19, 0),
) -> date:
    """Sessão candidata encerrada, adequada à captura diária pós-mercado.

    Depois das 19h de um dia útil, o próprio dia já pode ser arquivado. Antes
    desse horário (ou no fim de semana), usa o dia útil anterior. O horário é
    deliberadamente conservador; a fonte continua sendo a autoridade sobre
    feriados e pode devolver uma sessão vazia.
    """

    if now_local.tzinfo is None:
        raise ValueError("now_local deve ser timezone-aware")
    if now_local.weekday() < 5 and now_local.timetz().replace(tzinfo=None) >= conservative_close:
        return now_local.date()
    return latest_closed_weekday(now_local.date())


def build_current_contract_plan(
    symbol_states: dict[str, dict[str, Any]],
    *,
    now_utc,
    session_date: date,
) -> tuple[DailyCaptureSession, ...]:
    selections = [
        select_current_b3_contract(root, symbol_states, now_utc=now_utc)
        for root in ("WIN", "WDO")
    ]
    return tuple(
        DailyCaptureSession(selection=selection, session_date=session_date)
        for selection in selections
        if selection is not None
    )
