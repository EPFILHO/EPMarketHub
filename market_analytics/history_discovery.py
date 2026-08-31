"""Descoberta conservadora da cobertura histórica de ticks.

O módulo não conhece MT5, GUI nem disco. Ele planeja probes curtos e recebe
uma função injetada que devolve apenas metadados agregados. Um vazio isolado
ou erro nunca é tratado como prova do início da história.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DISCOVERY_SCHEMA_VERSION = 1
B3_TIMEZONE = "America/Sao_Paulo"
PROBE_LOCAL_TIME = time(13, 0)
PROBE_MINUTES = 5


class DiscoveryCancelled(RuntimeError):
    """Cancelamento cooperativo entre probes."""


@dataclass(frozen=True)
class DiscoveryAsset:
    logical_id: str
    symbol: str
    benchmark_bytes_per_session: int
    benchmark_seconds_per_session: float


@dataclass(frozen=True)
class ProbeObservation:
    logical_id: str
    symbol: str
    session_date: date
    start_utc: datetime
    end_utc: datetime
    phase: str
    status: str
    tick_count: int
    elapsed_seconds: float
    error_code: int | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"available", "empty", "inconclusive"}:
            raise ValueError(f"status inválido: {self.status}")
        if self.tick_count < 0:
            raise ValueError("tick_count não pode ser negativo")
        if self.start_utc.tzinfo != UTC or self.end_utc.tzinfo != UTC:
            raise ValueError("limites do probe devem estar em UTC")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["session_date"] = self.session_date.isoformat()
        result["start_utc"] = self.start_utc.isoformat()
        result["end_utc"] = self.end_utc.isoformat()
        return result


@dataclass(frozen=True)
class DiscoveryProgress:
    probes_done: int
    phase: str
    symbol: str
    session_date: date
    status: str
    tick_count: int


ProbeFunction = Callable[[DiscoveryAsset, date, datetime, datetime, str], ProbeObservation]
ProgressFunction = Callable[[DiscoveryProgress], None]
CancelFunction = Callable[[], bool]


def latest_completed_weekday(today: date) -> date:
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def month_start(value: date) -> date:
    return value.replace(day=1)


def shift_month(value: date, delta: int) -> date:
    index = value.year * 12 + value.month - 1 + delta
    year, month_index = divmod(index, 12)
    return date(year, month_index + 1, 1)


def month_sequence(start: date, end: date) -> list[date]:
    start = month_start(start)
    end = month_start(end)
    if start > end:
        return []
    result: list[date] = []
    current = start
    while current <= end:
        result.append(current)
        current = shift_month(current, 1)
    return result


def coarse_anchor_months(reference_date: date, floor_date: date, *, step_months: int = 6) -> list[date]:
    if step_months <= 0:
        raise ValueError("step_months deve ser positivo")
    current = month_start(reference_date)
    floor = month_start(floor_date)
    if floor > current:
        raise ValueError("floor_date não pode ser posterior à referência")
    anchors: list[date] = []
    while current >= floor:
        anchors.append(current)
        next_month = shift_month(current, -step_months)
        if next_month < floor:
            break
        current = next_month
    if anchors[-1] != floor:
        anchors.append(floor)
    return anchors


def month_probe_dates(month: date, *, not_after: date | None = None) -> list[date]:
    """Escolhe até três quartas-feiras espalhadas pelo mês."""

    first_weekday, days_in_month = calendar.monthrange(month.year, month.month)
    first_wednesday = 1 + (2 - first_weekday) % 7
    wednesdays = [date(month.year, month.month, day) for day in range(first_wednesday, days_in_month + 1, 7)]
    candidates = wednesdays[1:4] if len(wednesdays) >= 4 else wednesdays[:3]
    if not_after is not None:
        candidates = [candidate for candidate in candidates if candidate <= not_after]
    return candidates


def business_days_in_month(month: date, *, not_after: date | None = None) -> list[date]:
    days_in_month = calendar.monthrange(month.year, month.month)[1]
    result = [
        date(month.year, month.month, day)
        for day in range(1, days_in_month + 1)
        if date(month.year, month.month, day).weekday() < 5
    ]
    if not_after is not None:
        result = [candidate for candidate in result if candidate <= not_after]
    return result


def probe_window(session_date: date) -> tuple[datetime, datetime]:
    local_zone = ZoneInfo(B3_TIMEZONE)
    start_local = datetime.combine(session_date, PROBE_LOCAL_TIME, tzinfo=local_zone)
    end_local = start_local + timedelta(minutes=PROBE_MINUTES)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def classify_observations(observations: list[ProbeObservation]) -> str:
    if any(item.status == "available" for item in observations):
        return "available"
    if observations and all(item.status == "empty" for item in observations):
        return "empty"
    return "inconclusive"


class HistoryDiscoveryEngine:
    def __init__(
        self,
        *,
        assets: tuple[DiscoveryAsset, ...],
        reference_date: date,
        floor_date: date,
        probe: ProbeFunction,
        progress: ProgressFunction | None = None,
        cancelled: CancelFunction = lambda: False,
    ) -> None:
        if not assets:
            raise ValueError("ao menos um ativo é necessário")
        self.assets = assets
        self.reference_date = reference_date
        self.floor_date = floor_date
        self.probe = probe
        self.progress = progress
        self.cancelled = cancelled
        self.observations: list[ProbeObservation] = []
        self._month_cache: dict[tuple[str, date], tuple[str, list[ProbeObservation]]] = {}

    def run(self) -> dict[str, Any]:
        results = [self._discover_asset(asset) for asset in self.assets]
        return {
            "schema": "ep_market_hub.history_discovery",
            "schema_version": DISCOVERY_SCHEMA_VERSION,
            "reference_date": self.reference_date.isoformat(),
            "floor_date": self.floor_date.isoformat(),
            "timezone": B3_TIMEZONE,
            "probe_local_time": PROBE_LOCAL_TIME.isoformat(),
            "probe_minutes": PROBE_MINUTES,
            "assets": results,
            "observations": [item.to_dict() for item in self.observations],
        }

    def _discover_asset(self, asset: DiscoveryAsset) -> dict[str, Any]:
        latest = self._find_latest_observed(asset)
        anchors = coarse_anchor_months(self.reference_date, self.floor_date)
        coarse: list[tuple[date, str]] = []
        for anchor in anchors:
            status, _ = self._probe_month(asset, anchor, "coarse")
            coarse.append((anchor, status))

        available_anchors = [month for month, status in coarse if status == "available"]
        if not available_anchors:
            return self._asset_result(asset, latest, None, "none_observed", coarse, [])

        oldest_available_anchor = min(available_anchors)
        older_anchors = [month for month, _status in coarse if month < oldest_available_anchor]
        refinement_start = max(older_anchors) if older_anchors else self.floor_date
        refinement: list[tuple[date, str]] = []
        for month in month_sequence(refinement_start, oldest_available_anchor):
            status, _ = self._probe_month(asset, month, "monthly_refinement")
            refinement.append((month, status))

        available_months = [month for month, status in refinement if status == "available"]
        earliest_month = min(available_months) if available_months else oldest_available_anchor
        daily: list[ProbeObservation] = []
        previous_month = shift_month(earliest_month, -1)
        daily_months = [earliest_month]
        if previous_month >= month_start(self.floor_date):
            daily_months.insert(0, previous_month)
        for daily_month in daily_months:
            for session_date in business_days_in_month(daily_month, not_after=self.reference_date):
                observation = self._run_probe(asset, session_date, "daily_refinement")
                daily.append(observation)
        available_days = [item.session_date for item in daily if item.status == "available"]
        earliest_session = min(available_days) if available_days else None
        confidence_month = month_start(earliest_session) if earliest_session else earliest_month
        confidence = self._confidence(confidence_month, refinement, daily)
        return self._asset_result(asset, latest, earliest_session, confidence, coarse, refinement)

    def _find_latest_observed(self, asset: DiscoveryAsset) -> date | None:
        candidate = self.reference_date
        checked = 0
        while checked < 5:
            if candidate.weekday() < 5:
                observation = self._run_probe(asset, candidate, "latest_confirmation")
                checked += 1
                if observation.status == "available":
                    return candidate
            candidate -= timedelta(days=1)
        return None

    def _probe_month(
        self, asset: DiscoveryAsset, month: date, phase: str
    ) -> tuple[str, list[ProbeObservation]]:
        key = (asset.logical_id, month_start(month))
        if key in self._month_cache:
            return self._month_cache[key]
        observations: list[ProbeObservation] = []
        for session_date in month_probe_dates(month, not_after=self.reference_date):
            observation = self._run_probe(asset, session_date, phase)
            observations.append(observation)
            if observation.status == "available":
                break
        result = (classify_observations(observations), observations)
        self._month_cache[key] = result
        return result

    def _run_probe(self, asset: DiscoveryAsset, session_date: date, phase: str) -> ProbeObservation:
        if self.cancelled():
            raise DiscoveryCancelled("Descoberta cancelada pelo usuário entre probes.")
        start_utc, end_utc = probe_window(session_date)
        observation = self.probe(asset, session_date, start_utc, end_utc, phase)
        self.observations.append(observation)
        if self.progress is not None:
            self.progress(
                DiscoveryProgress(
                    probes_done=len(self.observations),
                    phase=phase,
                    symbol=asset.symbol,
                    session_date=session_date,
                    status=observation.status,
                    tick_count=observation.tick_count,
                )
            )
        return observation

    @staticmethod
    def _confidence(
        earliest_month: date,
        refinement: list[tuple[date, str]],
        daily: list[ProbeObservation],
    ) -> str:
        earlier = [status for month, status in refinement if month < earliest_month]
        available_days = sum(item.status == "available" for item in daily)
        if len(earlier) >= 2 and all(status == "empty" for status in earlier[-2:]) and available_days >= 2:
            return "high"
        if any(status == "empty" for status in earlier) and available_days >= 1:
            return "medium"
        return "low"

    def _asset_result(
        self,
        asset: DiscoveryAsset,
        latest: date | None,
        earliest: date | None,
        confidence: str,
        coarse: list[tuple[date, str]],
        refinement: list[tuple[date, str]],
    ) -> dict[str, Any]:
        projection = None
        if earliest is not None and latest is not None and earliest <= latest:
            years = max(0.0, (latest - earliest).days / 365.2425)
            sessions = max(1, round(years * 252) + 1)
            projection = {
                "assumption": "252 sessões/ano entre as datas observadas",
                "estimated_sessions": sessions,
                "estimated_bytes": sessions * asset.benchmark_bytes_per_session,
                "estimated_seconds": round(sessions * asset.benchmark_seconds_per_session, 1),
            }
        asset_observations = [item for item in self.observations if item.logical_id == asset.logical_id]
        return {
            "logical_id": asset.logical_id,
            "symbol": asset.symbol,
            "latest_observed_session": latest.isoformat() if latest else None,
            "earliest_observed_session": earliest.isoformat() if earliest else None,
            "confidence": confidence,
            "probe_count": len(asset_observations),
            "available_probe_count": sum(item.status == "available" for item in asset_observations),
            "empty_probe_count": sum(item.status == "empty" for item in asset_observations),
            "inconclusive_probe_count": sum(item.status == "inconclusive" for item in asset_observations),
            "coarse_months": [{"month": month.isoformat(), "status": status} for month, status in coarse],
            "refinement_months": [
                {"month": month.isoformat(), "status": status} for month, status in refinement
            ],
            "projection": projection,
        }
