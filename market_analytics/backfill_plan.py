"""Planejador e fila genéricos de backfill (DEV-004 — C3.1).

Núcleo puro: não importa `MetaTrader5`, `tkinter` nem Qt, e não toca ticks —
só datas, contagens e um snapshot abstrato do catálogo. Reaproveita
deliberadamente regras já testadas em vez de duplicá-las:

- `market_analytics.pilot_backfill.weekday_sessions` para o calendário
  factual de dias candidatos (segunda–sexta; feriado é sempre confirmado pela
  fonte, nunca inferido aqui — ver DEV-003).

A política `latest_closed` resolve o próprio "dia fechado" localmente: recebe
sempre um `now_utc` timezone-aware e usa `session_timezone` +
`session_close_local_time` do manifesto (nunca `history_discovery.
latest_completed_weekday`, que assume UTC "ontem" e não conhece horário de
fechamento por sessão — auditoria Codex do DEV-004).

Uma execução futura pode conter vários ativos, mas continua com um único
`source_id`/terminal por manifesto e `concurrency=1` (`BackfillManifest`
já recusa qualquer outro valor). Manifestos de fontes distintas são sempre
planejados/executados separadamente.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .manifest import AUTHORIZATION_STATES, BackfillManifest, ManifestValidationError
from .pilot_backfill import weekday_sessions

PLAN_SCHEMA = "ep_market_hub.backfill_plan"
PLAN_SCHEMA_VERSION = 1

# Vocabulário fechado do status de um item da fila, resultado de cruzar o
# manifesto com um snapshot abstrato do catálogo (DEV-004, fronteira 3) e,
# para "planned"/"pending", com os limites de bytes/duração do manifesto:
# - "planned": nada registrado ainda (ou catálogo "pending") — primeira vez.
# - "reusable": catálogo já tem "completed"/"empty" — reaproveitável de forma
#   idempotente, nunca rebaixado por um evento tardio, e o resultado
#   reaproveitado preserva qual dos dois era (nunca vira "completed" à força).
# - "pending": catálogo tem "failed"/"interrupted" — precisa de nova
#   tentativa, mas já tem histórico (distinto de "planned").
# - "blocked": catálogo tem "running" — outra tentativa é dona agora; este
#   controlador nunca toca uma sessão alheia em andamento.
# - "blocked_by_limit": seria "planned"/"pending", mas incluí-lo excederia
#   `max_total_bytes`/`max_total_duration_seconds` do manifesto na ordem de
#   execução declarada — bloqueador explícito e visível no plano, nunca só
#   uma observação de exibição (`run_plan` também o recusa em tempo real).
ITEM_STATUSES: frozenset[str] = frozenset({"planned", "reusable", "pending", "blocked", "blocked_by_limit"})

_CATALOG_STATE_TO_STATUS: dict[str | None, str] = {
    None: "planned",
    "pending": "planned",
    "completed": "reusable",
    "empty": "reusable",
    "running": "blocked",
    "failed": "pending",
    "interrupted": "pending",
}

# Inverso estrutural do mapa acima, mais a entrada de "blocked_by_limit" (que
# nunca vem do catálogo). Usado por `PlanItem.__post_init__` para recusar
# qualquer combinação status/catalog_state incoerente -- inclusive um plano
# carregado de disco e adulterado à mão.
_STATUS_TO_ALLOWED_CATALOG_STATES: dict[str, frozenset[str | None]] = {
    "planned": frozenset({None, "pending"}),
    "reusable": frozenset({"completed", "empty"}),
    "pending": frozenset({"failed", "interrupted"}),
    "blocked": frozenset({"running"}),
    "blocked_by_limit": frozenset({None, "pending", "failed", "interrupted"}),
}

# `(logical_id, session_date) -> estado do catálogo (ou ausente)`. O
# planejador nunca lê o catálogo real: recebe este snapshot já resolvido pelo
# chamador (produção: `market_analytics.backfill_catalog`; testes: um dict
# fabricado à mão).
CatalogSnapshot = Mapping[tuple[str, date], str]


class PlanBuildError(ValueError):
    """Falha ao construir/reabrir o plano (intervalo inválido, benchmark ausente,
    esquema estrito violado ou totais/itens incoerentes -- plano possivelmente
    adulterado)."""


def classify_session_status(catalog_state: str | None) -> str:
    """Traduz um estado do catálogo (ou ausência de registro) em status de fila.

    Nunca devolve "blocked_by_limit": essa reclassificação só acontece dentro
    de `build_plan`, depois de cruzar com os limites do manifesto.
    """

    if catalog_state not in _CATALOG_STATE_TO_STATUS:
        raise PlanBuildError(f"estado de catálogo desconhecido: {catalog_state!r}")
    return _CATALOG_STATE_TO_STATUS[catalog_state]


def latest_closed_session_date(
    *, now_utc: datetime, session_timezone: str, session_close_local_time: time
) -> date:
    """Resolve a política `latest_closed` de forma consciente do fechamento local.

    Converte `now_utc` (obrigatoriamente timezone-aware) para `session_timezone`
    e inclui o próprio dia útil corrente somente se ele já é dia útil (segunda–
    sexta) E o horário local já alcançou `session_close_local_time`. Caso
    contrário (antes do fechamento, ou fim de semana), recua para o último dia
    útil anterior. Nunca assume "ontem" às cegas nem usa apenas a data UTC
    (auditoria Codex do DEV-004: `history_discovery.latest_completed_weekday`
    fazia exatamente isso e por isso nunca é reutilizado aqui).
    """

    if now_utc.tzinfo is None:
        raise PlanBuildError("now_utc deve ser timezone-aware")
    local_now = now_utc.astimezone(ZoneInfo(session_timezone))
    already_closed_today = local_now.weekday() < 5 and local_now.time() >= session_close_local_time
    candidate = local_now.date()
    if not already_closed_today:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def resolve_end_date(manifest: BackfillManifest, *, now_utc: datetime) -> date:
    """Resolve a política de data final do manifesto numa data concreta.

    `explicit` devolve `manifest.end_date` diretamente (`now_utc` é ignorado).
    `latest_closed` usa `latest_closed_session_date` com o horário de
    fechamento e a timezone do próprio manifesto.
    """

    if manifest.end_date_policy == "explicit":
        assert manifest.end_date is not None
        return manifest.end_date
    return latest_closed_session_date(
        now_utc=now_utc,
        session_timezone=manifest.session_timezone,
        session_close_local_time=manifest.session_close_local_time,
    )


def _validate_benchmark(value: Any, *, logical_id: str, kind: str) -> float:
    """Benchmark de bytes/segundos por sessão: só número finito e não negativo.

    `NaN`/`inf`/negativo corromperia silenciosamente a matemática de
    `blocked_by_limit` (comparações com `NaN` são sempre `False`, então um
    item jamais seria bloqueado) -- por isso `float()` sozinho nunca basta
    (DEV-004, 3ª auditoria Codex)."""

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanBuildError(
            f"benchmark {kind} inválido para logical_id {logical_id!r} (recebido: {value!r})"
        ) from exc
    if isinstance(value, bool) or not math.isfinite(number) or number < 0:
        raise PlanBuildError(
            f"benchmark {kind} deve ser um número finito e não negativo para logical_id {logical_id!r} "
            f"(recebido: {value!r})"
        )
    return number


def build_session_dates(start_date: date, end_date: date, *, order: str) -> list[date]:
    """Dias candidatos segunda–sexta entre `start_date` e `end_date`, na ordem pedida."""

    dates = weekday_sessions(start_date, end_date)
    if order == "newest_first":
        dates.reverse()
    elif order != "oldest_first":
        raise PlanBuildError(f"execution_order inválida: {order!r}")
    return dates


_PLAN_ITEM_FIELDS = frozenset({"logical_id", "requested_symbol", "session_date", "status", "catalog_state"})


@dataclass(frozen=True)
class PlanItem:
    logical_id: str
    requested_symbol: str
    session_date: date
    status: str
    catalog_state: str | None

    def __post_init__(self) -> None:
        if self.status not in ITEM_STATUSES:
            raise PlanBuildError(f"status de item inválido: {self.status!r}")
        allowed_catalog_states = _STATUS_TO_ALLOWED_CATALOG_STATES[self.status]
        if self.catalog_state not in allowed_catalog_states:
            raise PlanBuildError(
                f"catalog_state {self.catalog_state!r} incoerente com status {self.status!r} "
                f"(permitido: {sorted(str(state) for state in allowed_catalog_states)}) -- "
                "plano possivelmente adulterado"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "requested_symbol": self.requested_symbol,
            "session_date": self.session_date.isoformat(),
            "status": self.status,
            "catalog_state": self.catalog_state,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PlanItem:
        if not isinstance(data, dict):
            raise PlanBuildError(f"item do plano deve ser um objeto (recebido: {data!r})")
        extra = set(data) - _PLAN_ITEM_FIELDS
        if extra:
            raise PlanBuildError(f"campo(s) desconhecido(s) em item do plano: {sorted(extra)}")
        missing = _PLAN_ITEM_FIELDS - set(data)
        if missing:
            raise PlanBuildError(f"campo(s) ausente(s) em item do plano: {sorted(missing)}")
        catalog_state = data["catalog_state"]
        if catalog_state is not None and not isinstance(catalog_state, str):
            raise PlanBuildError(f"catalog_state deve ser string ou null (recebido: {catalog_state!r})")
        session_date_raw = data["session_date"]
        if not isinstance(session_date_raw, str):
            raise PlanBuildError(f"session_date deve ser uma string ISO (recebido: {session_date_raw!r})")
        try:
            session_date = date.fromisoformat(session_date_raw)
        except ValueError as exc:
            raise PlanBuildError(f"session_date inválida: {session_date_raw!r}") from exc
        logical_id = data["logical_id"]
        requested_symbol = data["requested_symbol"]
        status = data["status"]
        if not isinstance(logical_id, str) or not isinstance(requested_symbol, str) or not isinstance(status, str):
            raise PlanBuildError("logical_id/requested_symbol/status devem ser strings")
        return cls(
            logical_id=logical_id,
            requested_symbol=requested_symbol,
            session_date=session_date,
            status=status,
            catalog_state=catalog_state,
        )


def _compute_totals(items: tuple[PlanItem, ...]) -> dict[str, int]:
    return {
        "sessions_total": len(items),
        "planned": sum(1 for item in items if item.status == "planned"),
        "reusable": sum(1 for item in items if item.status == "reusable"),
        "pending": sum(1 for item in items if item.status == "pending"),
        "blocked": sum(1 for item in items if item.status == "blocked"),
        "blocked_by_limit": sum(1 for item in items if item.status == "blocked_by_limit"),
    }


_TOTAL_KEYS = frozenset({"sessions_total", "planned", "reusable", "pending", "blocked", "blocked_by_limit"})


def _require_totals(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise PlanBuildError(f"totals deve ser um objeto (recebido: {value!r})")
    extra = set(value) - _TOTAL_KEYS
    if extra:
        raise PlanBuildError(f"chave(s) desconhecida(s) em totals: {sorted(extra)}")
    missing = _TOTAL_KEYS - set(value)
    if missing:
        raise PlanBuildError(f"chave(s) ausente(s) em totals: {sorted(missing)}")
    result: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise PlanBuildError(f"totals[{key!r}] deve ser um inteiro não negativo (recebido: {raw!r})")
        result[key] = raw
    return result


def _require_plan_str(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise PlanBuildError(f"{key} deve ser uma string (recebido: {value!r})")
    return value


def _require_plan_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanBuildError(f"{field_name} deve ser um inteiro (recebido: {value!r})")
    return value


def _require_plan_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlanBuildError(f"{field_name} deve ser um número (recebido: {value!r})")
    if value < 0:
        raise PlanBuildError(f"{field_name} não pode ser negativo (recebido: {value!r})")
    return float(value)


def _require_plan_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise PlanBuildError(f"{field_name} deve ser um booleano (recebido: {value!r})")
    return value


def _require_plan_date(data: dict[str, Any], key: str) -> date:
    value = data[key]
    if not isinstance(value, str):
        raise PlanBuildError(f"{key} deve ser uma string ISO (recebido: {value!r})")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PlanBuildError(f"{key} inválida: {value!r}") from exc


_PLAN_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "manifest_id",
        "manifest_fingerprint",
        "source_id",
        "session_timezone",
        "execution_order",
        "chunk_seconds",
        "resolved_start_date",
        "resolved_end_date",
        "items",
        "totals",
        "estimated_bytes_remaining",
        "estimated_seconds_remaining",
        "execution_authorized",
    }
)


@dataclass(frozen=True)
class BackfillPlan:
    """Fila determinística de uma execução — reabrível e amarrada ao manifesto por fingerprint.

    `execution_authorized` é só informativo/para exibição: `run_plan`
    (`market_analytics.backfill_adapter`) NUNCA confia neste campo
    desserializado -- ele sempre revalida um `BackfillManifest` fresco e
    confere o vínculo (fingerprint/id/source/timezone/chunk) antes de
    executar (DEV-004, auditoria Codex).
    """

    manifest_id: str
    manifest_fingerprint: str
    source_id: str
    session_timezone: str
    execution_order: str
    chunk_seconds: int
    resolved_start_date: date
    resolved_end_date: date
    items: tuple[PlanItem, ...]
    totals: dict[str, int]
    estimated_bytes_remaining: int
    estimated_seconds_remaining: float
    execution_authorized: bool
    schema: str = PLAN_SCHEMA
    schema_version: int = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != PLAN_SCHEMA:
            raise PlanBuildError(f"schema de plano inesperado: {self.schema!r}")
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise PlanBuildError(f"schema_version de plano não suportada: {self.schema_version!r}")
        if not isinstance(self.items, tuple) or not all(isinstance(item, PlanItem) for item in self.items):
            raise PlanBuildError("items deve ser uma tupla de PlanItem")
        if self.resolved_end_date < self.resolved_start_date:
            raise PlanBuildError(
                f"intervalo resolvido invertido: end={self.resolved_end_date} "
                f"anterior a start={self.resolved_start_date}"
            )
        expected_totals = _compute_totals(self.items)
        actual_totals = _require_totals(self.totals)
        if actual_totals != expected_totals:
            raise PlanBuildError(
                "totals incoerente com os itens do plano (plano possivelmente adulterado): "
                f"esperado {expected_totals}, recebido {actual_totals}"
            )
        object.__setattr__(self, "totals", actual_totals)
        if not isinstance(self.execution_authorized, bool):
            raise PlanBuildError(f"execution_authorized deve ser um booleano (recebido: {self.execution_authorized!r})")
        if isinstance(self.estimated_bytes_remaining, bool) or not isinstance(self.estimated_bytes_remaining, int):
            raise PlanBuildError(
                f"estimated_bytes_remaining deve ser um inteiro (recebido: {self.estimated_bytes_remaining!r})"
            )
        if self.estimated_bytes_remaining < 0:
            raise PlanBuildError("estimated_bytes_remaining não pode ser negativo")
        if isinstance(self.estimated_seconds_remaining, bool) or not isinstance(
            self.estimated_seconds_remaining, int | float
        ):
            raise PlanBuildError(
                "estimated_seconds_remaining deve ser um número "
                f"(recebido: {self.estimated_seconds_remaining!r})"
            )
        if self.estimated_seconds_remaining < 0:
            raise PlanBuildError("estimated_seconds_remaining não pode ser negativo")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "manifest_fingerprint": self.manifest_fingerprint,
            "source_id": self.source_id,
            "session_timezone": self.session_timezone,
            "execution_order": self.execution_order,
            "chunk_seconds": self.chunk_seconds,
            "resolved_start_date": self.resolved_start_date.isoformat(),
            "resolved_end_date": self.resolved_end_date.isoformat(),
            "items": [item.to_dict() for item in self.items],
            "totals": dict(self.totals),
            "estimated_bytes_remaining": self.estimated_bytes_remaining,
            "estimated_seconds_remaining": self.estimated_seconds_remaining,
            "execution_authorized": self.execution_authorized,
        }

    @classmethod
    def from_dict(cls, data: Any) -> BackfillPlan:
        """Desserialização estrita: nunca `KeyError` bruto, nunca `bool("false") == True`.

        Campo desconhecido/ausente é sempre recusado com `PlanBuildError`, e a
        coerência totals/itens é reconferida por `__post_init__` -- um plano
        adulterado (itens editados sem atualizar totals, ou vice-versa) é
        sempre recusado aqui, nunca aceito silenciosamente.
        """

        if not isinstance(data, dict):
            raise PlanBuildError(f"plano deve ser um objeto JSON (recebido: {data!r})")
        if data.get("schema") != PLAN_SCHEMA:
            raise PlanBuildError(f"schema de plano inesperado: {data.get('schema')!r}")
        if data.get("schema_version") != PLAN_SCHEMA_VERSION:
            raise PlanBuildError(f"schema_version de plano não suportada: {data.get('schema_version')!r}")
        extra = set(data) - _PLAN_FIELDS
        if extra:
            raise PlanBuildError(f"campo(s) desconhecido(s) no plano: {sorted(extra)}")
        missing = _PLAN_FIELDS - set(data)
        if missing:
            raise PlanBuildError(f"campo(s) ausente(s) no plano: {sorted(missing)}")
        items_raw = data["items"]
        if not isinstance(items_raw, list):
            raise PlanBuildError(f"items deve ser uma lista (recebido: {items_raw!r})")
        return cls(
            manifest_id=_require_plan_str(data, "manifest_id"),
            manifest_fingerprint=_require_plan_str(data, "manifest_fingerprint"),
            source_id=_require_plan_str(data, "source_id"),
            session_timezone=_require_plan_str(data, "session_timezone"),
            execution_order=_require_plan_str(data, "execution_order"),
            chunk_seconds=_require_plan_int(data["chunk_seconds"], "chunk_seconds"),
            resolved_start_date=_require_plan_date(data, "resolved_start_date"),
            resolved_end_date=_require_plan_date(data, "resolved_end_date"),
            items=tuple(PlanItem.from_dict(item) for item in items_raw),
            totals=_require_totals(data["totals"]),
            estimated_bytes_remaining=_require_plan_int(
                data["estimated_bytes_remaining"], "estimated_bytes_remaining"
            ),
            estimated_seconds_remaining=_require_plan_number(
                data["estimated_seconds_remaining"], "estimated_seconds_remaining"
            ),
            execution_authorized=_require_plan_bool(data["execution_authorized"], "execution_authorized"),
        )


def build_plan(
    manifest: BackfillManifest,
    *,
    now_utc: datetime,
    catalog_snapshot: CatalogSnapshot,
    benchmark_bytes_per_session: Mapping[str, float],
    benchmark_seconds_per_session: Mapping[str, float],
) -> BackfillPlan:
    """Expande o manifesto numa fila determinística e calcula totais/projeções.

    Não carrega nenhum tick: os benchmarks por `logical_id` (bytes/segundos
    por sessão) são fornecidos explicitamente pelo chamador — nunca uma
    constante universal — para nunca presumir que o volume de um ativo
    representa o de outro ("nem assumem volume centralizado", DEV-004).

    Os itens que seriam "planned"/"pending" são cruzados, na própria ordem de
    execução declarada pelo manifesto, com `max_total_bytes`/
    `max_total_duration_seconds`: assim que a soma acumulada excederia um dos
    dois limites, esse item (e todo item seguinte que também seria
    "planned"/"pending") vira "blocked_by_limit" -- um bloqueador explícito no
    plano, não apenas um número de exibição (DEV-004, auditoria Codex).
    """

    if manifest.authorization_state not in AUTHORIZATION_STATES:
        raise ManifestValidationError(f"authorization_state inválido: {manifest.authorization_state!r}")

    resolved_end = resolve_end_date(manifest, now_utc=now_utc)
    if resolved_end < manifest.start_date:
        raise PlanBuildError(
            f"intervalo resolvido invertido: end={resolved_end} anterior a start={manifest.start_date}"
        )
    session_dates = build_session_dates(manifest.start_date, resolved_end, order=manifest.execution_order)

    items: list[PlanItem] = []
    running_bytes = 0.0
    running_seconds = 0.0
    estimated_bytes = 0.0
    estimated_seconds = 0.0
    for session_date in session_dates:
        for asset in manifest.assets:
            catalog_state = catalog_snapshot.get((asset.logical_id, session_date))
            status = classify_session_status(catalog_state)
            if status in ("planned", "pending"):
                try:
                    raw_bytes = benchmark_bytes_per_session[asset.logical_id]
                    raw_seconds = benchmark_seconds_per_session[asset.logical_id]
                except KeyError as exc:
                    raise PlanBuildError(f"benchmark ausente para logical_id {asset.logical_id!r}") from exc
                item_bytes = _validate_benchmark(raw_bytes, logical_id=asset.logical_id, kind="de bytes")
                item_seconds = _validate_benchmark(raw_seconds, logical_id=asset.logical_id, kind="de segundos")
                over_bytes = running_bytes + item_bytes > manifest.max_total_bytes
                over_seconds = running_seconds + item_seconds > manifest.max_total_duration_seconds
                if over_bytes or over_seconds:
                    status = "blocked_by_limit"
                else:
                    running_bytes += item_bytes
                    running_seconds += item_seconds
                    estimated_bytes += item_bytes
                    estimated_seconds += item_seconds
            items.append(
                PlanItem(
                    logical_id=asset.logical_id,
                    requested_symbol=asset.requested_symbol,
                    session_date=session_date,
                    status=status,
                    catalog_state=catalog_state,
                )
            )

    return BackfillPlan(
        manifest_id=manifest.manifest_id,
        manifest_fingerprint=manifest.fingerprint(),
        source_id=manifest.source_id,
        session_timezone=manifest.session_timezone,
        execution_order=manifest.execution_order,
        chunk_seconds=manifest.chunk_seconds,
        resolved_start_date=manifest.start_date,
        resolved_end_date=resolved_end,
        items=tuple(items),
        totals=_compute_totals(tuple(items)),
        estimated_bytes_remaining=round(estimated_bytes),
        estimated_seconds_remaining=round(estimated_seconds, 3),
        execution_authorized=manifest.is_execution_authorized,
    )


def assert_plan_matches_manifest(
    plan: BackfillPlan, manifest: BackfillManifest, *, now_utc: datetime | None = None
) -> None:
    """Vínculo *completo* entre `plan` e `manifest` -- núcleo puro compartilhado
    por `backfill_adapter.run_plan` e pela reabertura de plano da GUI
    (`tools/manifest_backfill_gui.reopen_plan`), para que as duas fronteiras
    apliquem exatamente a mesma regra (DEV-004, 3ª auditoria Codex).

    `now_utc` é injetável e determinístico para testes; se omitido, usa
    `datetime.now(UTC)` -- um default seguro para quem chama de verdade.

    Metadados de nível superior (fingerprint/id/fonte/timezone/chunk/ordem/
    início resolvido) precisam bater exatamente, e `resolved_end_date` é
    conferido conforme a política do manifesto:

    - `explicit`: precisa ser exatamente `manifest.end_date` -- nunca uma
      data posterior adulterada;
    - `latest_closed`: nunca pode ser posterior à última sessão fechada no
      momento da validação (`latest_closed_session_date` com `now_utc`) --
      mesmo que tivesse sido válida no passado, não pode "esticar" o
      intervalo autorizado além do que está fechado agora.

    Além disso, cada item da fila é conferido individualmente contra o
    manifesto:

    - `logical_id` precisa ser um ativo autorizado, e `requested_symbol`
      precisa ser exatamente o símbolo declarado para esse `logical_id` --
      nunca "parecido", sempre igual;
    - `session_date` precisa cair dentro de `[resolved_start_date,
      resolved_end_date]` (o próprio intervalo declarado pelo plano) E ser um
      dia candidato segunda-sexta -- nunca fim de semana, nunca fora do
      intervalo;
    - não pode haver `(logical_id, session_date)` duplicado;
    - a SEQUÊNCIA dos itens precisa respeitar a grade autorizada (datas na
      ordem de `manifest.execution_order`, ativos na ordem declarada em
      `manifest.assets` para cada data) -- um subconjunto da grade é
      permitido, mas nunca regressão nem reordenação.

    Qualquer divergência é tratada como plano fora do escopo autorizado e
    recusada com `PlanBuildError` -- nunca silenciosamente aceita.
    """

    mismatched: list[str] = []
    if plan.manifest_fingerprint != manifest.fingerprint():
        mismatched.append("manifest_fingerprint")
    if plan.manifest_id != manifest.manifest_id:
        mismatched.append("manifest_id")
    if plan.source_id != manifest.source_id:
        mismatched.append("source_id")
    if plan.session_timezone != manifest.session_timezone:
        mismatched.append("session_timezone")
    if plan.chunk_seconds != manifest.chunk_seconds:
        mismatched.append("chunk_seconds")
    if plan.execution_order != manifest.execution_order:
        mismatched.append("execution_order")
    if plan.resolved_start_date != manifest.start_date:
        mismatched.append("resolved_start_date")
    if manifest.end_date_policy == "explicit" and plan.resolved_end_date != manifest.end_date:
        mismatched.append("resolved_end_date")
    if mismatched:
        raise PlanBuildError(
            "plano não corresponde ao manifesto revalidado "
            f"(campo(s) divergente(s): {mismatched}); execução recusada"
        )

    if manifest.end_date_policy == "latest_closed":
        current_latest_closed = latest_closed_session_date(
            now_utc=now_utc if now_utc is not None else datetime.now(UTC),
            session_timezone=manifest.session_timezone,
            session_close_local_time=manifest.session_close_local_time,
        )
        if plan.resolved_end_date > current_latest_closed:
            raise PlanBuildError(
                f"resolved_end_date do plano ({plan.resolved_end_date}) é posterior à última sessão "
                f"fechada no momento da validação ({current_latest_closed}); execução recusada"
            )

    expected_dates = build_session_dates(plan.resolved_start_date, plan.resolved_end_date, order=manifest.execution_order)
    expected_keys = [(asset.logical_id, session_date) for session_date in expected_dates for asset in manifest.assets]
    grid_index = {key: index for index, key in enumerate(expected_keys)}

    authorized_symbol_by_logical_id = {asset.logical_id: asset.requested_symbol for asset in manifest.assets}
    seen_keys: set[tuple[str, date]] = set()
    last_index = -1
    for item in plan.items:
        key = (item.logical_id, item.session_date)
        if key in seen_keys:
            raise PlanBuildError(
                f"item duplicado no plano: logical_id={item.logical_id!r} session_date={item.session_date}"
            )
        seen_keys.add(key)

        authorized_symbol = authorized_symbol_by_logical_id.get(item.logical_id)
        if authorized_symbol is None:
            raise PlanBuildError(
                f"item fora do escopo autorizado: logical_id {item.logical_id!r} não está no manifesto"
            )
        if item.requested_symbol != authorized_symbol:
            raise PlanBuildError(
                f"requested_symbol divergente para logical_id {item.logical_id!r}: "
                f"esperado {authorized_symbol!r}, recebido {item.requested_symbol!r}"
            )
        if not (plan.resolved_start_date <= item.session_date <= plan.resolved_end_date):
            raise PlanBuildError(
                f"session_date fora do intervalo resolvido: {item.session_date} não está entre "
                f"{plan.resolved_start_date} e {plan.resolved_end_date}"
            )
        if item.session_date.weekday() >= 5:
            raise PlanBuildError(f"session_date não é um dia candidato (fim de semana): {item.session_date}")

        # `key` está garantidamente em `grid_index`: os quatro checks acima já
        # provaram logical_id autorizado + data num dia candidato dentro do
        # intervalo resolvido -- exatamente o critério usado para montar
        # `expected_keys`.
        index = grid_index[key]
        if index <= last_index:
            raise PlanBuildError(
                "ordem dos itens do plano diverge da grade autorizada (regressão ou reordenação) em "
                f"logical_id={item.logical_id!r} session_date={item.session_date}"
            )
        last_index = index


def estimate_eta_seconds(*, completed_count: int, elapsed_seconds: float, remaining_count: int) -> float | None:
    """ETA linear a partir do ritmo observado (itens concluídos / tempo decorrido).

    Nunca divide por zero: sem item concluído ou sem tempo decorrido o ritmo é
    desconhecido e a função devolve `None` em vez de assumir uma velocidade
    (nunca presume volume/tempo "centralizado" por sessão). Sem itens
    restantes, o ETA é `0.0`.
    """

    if remaining_count <= 0:
        return 0.0
    if completed_count <= 0 or elapsed_seconds <= 0:
        return None
    seconds_per_item = elapsed_seconds / completed_count
    return remaining_count * seconds_per_item


def save_plan_atomic(path: Path, plan: BackfillPlan) -> None:
    """Grava o plano por substituição atômica — nunca deixa um arquivo parcial."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp"
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def load_plan(path: Path) -> BackfillPlan:
    """Lê e reconstrói um plano gravado por `save_plan_atomic`."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanBuildError(f"não foi possível ler o plano: {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanBuildError(f"plano não é um JSON válido: {path}: {exc}") from exc
    return BackfillPlan.from_dict(data)


ProgressCallback = Callable[[PlanItem], None]
