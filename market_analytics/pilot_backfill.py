"""Planejamento puro do piloto mensal de backfill (DEV-003/C2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any


@dataclass(frozen=True)
class PilotAsset:
    logical_id: str
    symbol: str

    def __post_init__(self) -> None:
        if not self.logical_id.strip() or not self.symbol.strip():
            raise ValueError("ativo do piloto exige logical_id e symbol")


@dataclass(frozen=True)
class PilotSession:
    asset: PilotAsset
    session_date: date

    def to_dict(self) -> dict[str, Any]:
        return {"asset": asdict(self.asset), "session_date": self.session_date.isoformat()}


def weekday_sessions(start_date: date, end_date: date) -> list[date]:
    """Dias candidatos segunda–sexta; feriados são confirmados pela fonte."""

    if end_date < start_date:
        raise ValueError("end_date não pode ser anterior a start_date")
    result: list[date] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def build_pilot_queue(
    assets: tuple[PilotAsset, ...],
    start_date: date,
    end_date: date,
    *,
    newest_first: bool = True,
) -> list[PilotSession]:
    if not assets:
        raise ValueError("ao menos um ativo é necessário")
    dates = weekday_sessions(start_date, end_date)
    if newest_first:
        dates.reverse()
    return [PilotSession(asset=asset, session_date=session_date) for session_date in dates for asset in assets]


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_asset: dict[str, dict[str, Any]] = {}
    for result in results:
        logical_id = str(result["logical_id"])
        entry = by_asset.setdefault(
            logical_id,
            {
                "logical_id": logical_id,
                "symbol": result["symbol"],
                "sessions": 0,
                "completed": 0,
                "empty": 0,
                "reused": 0,
                "failed": 0,
                "tick_count": 0,
                "file_size_bytes": 0,
            },
        )
        entry["sessions"] += 1
        state = str(result["state"])
        if state in {"completed", "empty"}:
            entry[state] += 1
        else:
            entry["failed"] += 1
        entry["reused"] += int(bool(result.get("reused")))
        entry["tick_count"] += int(result.get("tick_count") or 0)
        entry["file_size_bytes"] += int(result.get("file_size_bytes") or 0)
    totals = {
        "sessions": sum(item["sessions"] for item in by_asset.values()),
        "completed": sum(item["completed"] for item in by_asset.values()),
        "empty": sum(item["empty"] for item in by_asset.values()),
        "reused": sum(item["reused"] for item in by_asset.values()),
        "failed": sum(item["failed"] for item in by_asset.values()),
        "tick_count": sum(item["tick_count"] for item in by_asset.values()),
        "file_size_bytes": sum(item["file_size_bytes"] for item in by_asset.values()),
    }
    return {"by_asset": list(by_asset.values()), "totals": totals}


def retryable_failure(reason: str | None) -> bool:
    return reason == "mt5_error"
