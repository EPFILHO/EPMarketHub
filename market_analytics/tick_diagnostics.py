"""Diagnóstico somente leitura de cobertura de ticks históricos do MT5.

Este módulo é puro: não importa `core` nem a biblioteca `MetaTrader5`. Ele
define o contrato da solicitação (`TickWindowRequest`), o acumulador
incremental que resume um chunk de ticks sem nunca materializar o array
inteiro em memória (`TickWindowAccumulator`) e o resumo estruturado
(`TickWindowSummary`) que é tudo que atravessa a fila de eventos do worker.

Este diagnóstico mede apenas o que a API oficial ``copy_ticks_range``
retorna. Ele não distingue cache local, cache do Strategy Tester ou download
do servidor da corretora — qualquer uma dessas fontes pode responder à mesma
chamada, e a API não expõe qual delas foi usada.

As métricas de cobertura aqui são deliberadamente factuais (duração
observada, afastamento das bordas solicitadas). Elas não comprovam ausência
de ticks perdidos dentro do intervalo — não existe forma de provar isso só
com o que a própria API retorna, então nenhuma métrica aqui deve ser lida
como prova de completude.

A chamada `copy_ticks_range` é síncrona: enquanto o worker aguarda a
corretora (ou o cache/Tester) responder, o loop principal não avança para o
próximo lote, snapshot ou heartbeat. Limitar `chunk_seconds` reduz o tamanho
típico dessa espera, mas não elimina o risco de uma chamada demorada — a
corretora pode buscar o intervalo pedido no próprio servidor mesmo para um
chunk pequeno.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NamedTuple

CANONICAL_TICK_TYPES: frozenset[str] = frozenset({"all", "info", "trade"})
_MT5_TICK_TYPE_ATTRS: dict[str, str] = {
    "all": "COPY_TICKS_ALL",
    "info": "COPY_TICKS_INFO",
    "trade": "COPY_TICKS_TRADE",
}

TickDiagnosticFailureReason = Literal[
    "invalid_request",
    "symbol_not_found",
    "terminal_disconnected",
    "malformed_response",
    "mt5_error",
    "request_id_conflict",
    "diagnostic_busy",
    "worker_unavailable",
]

FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "invalid_request",
        "symbol_not_found",
        "terminal_disconnected",
        "malformed_response",
        "mt5_error",
        "request_id_conflict",
        "diagnostic_busy",
        "worker_unavailable",
    }
)

EMPTY_REASON_NO_TICKS = "no_ticks_returned"

COVERAGE_DISCLAIMER = (
    "Duração observada e afastamento das bordas são apenas indícios; a "
    "ausência de lacunas internas não é comprovada por estas métricas."
)

# Limites rígidos do contrato (correção obrigatória da auditoria do plano).
MAX_WINDOWS_PER_REQUEST = 3
MAX_WINDOW_SECONDS = 24 * 60 * 60
# Menor que MAX_WINDOWS_PER_REQUEST * MAX_WINDOW_SECONDS de propósito: o
# limite total é uma restrição independente do limite por janela, não uma
# consequência automática dele (3 janelas de 1 sessão cada cabem folgadas
# em 2 dias; 3 janelas de 1 dia inteiro cada, não).
MAX_TOTAL_SECONDS = 2 * MAX_WINDOW_SECONDS
MIN_CHUNK_SECONDS = 60
# Reduzido de 1 hora para 900s (correção COR-DEV-001 item 7): uma chamada
# copy_ticks_range grande demais aumenta o tempo síncrono que o loop do
# worker fica sem atender heartbeat/comandos/parada.
MAX_CHUNK_SECONDS = 15 * 60
DEFAULT_CHUNK_SECONDS = 15 * 60

# Bits do campo `flags` retornado pela API (TICK_FLAG_*). Constantes puras
# usadas só para decodificar o histograma, sem importar a biblioteca MT5.
TICK_FLAG_BID = 2
TICK_FLAG_ASK = 4
TICK_FLAG_LAST = 8
TICK_FLAG_VOLUME = 16
TICK_FLAG_BUY = 32
TICK_FLAG_SELL = 64
_TICK_FLAG_BITS: tuple[tuple[int, str], ...] = (
    (TICK_FLAG_BID, "bid"),
    (TICK_FLAG_ASK, "ask"),
    (TICK_FLAG_LAST, "last"),
    (TICK_FLAG_VOLUME, "volume"),
    (TICK_FLAG_BUY, "buy"),
    (TICK_FLAG_SELL, "sell"),
)


def mt5_tick_type_attr(tick_type: str) -> str:
    """Nome do atributo MT5 (ex.: ``COPY_TICKS_ALL``) para um tipo canônico."""

    if tick_type not in CANONICAL_TICK_TYPES:
        raise ValueError(
            f"tick_type inválido: {tick_type!r}. Use um de {sorted(CANONICAL_TICK_TYPES)}."
        )
    return _MT5_TICK_TYPE_ATTRS[tick_type]


def _require_utc_aware(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} deve ser datetime (recebido: {value!r})")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(
            f"{field_name} deve ser timezone-aware em UTC (utcoffset()==0); recebido {value!r}"
        )
    return value


@dataclass(frozen=True)
class TickWindow:
    """Uma janela `[start_utc, end_utc)` já validada como UTC-aware."""

    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        _require_utc_aware(self.start_utc, "start_utc")
        _require_utc_aware(self.end_utc, "end_utc")
        if self.end_utc <= self.start_utc:
            raise ValueError(
                f"end_utc ({self.end_utc}) deve ser maior que start_utc ({self.start_utc})"
            )
        duration = (self.end_utc - self.start_utc).total_seconds()
        if duration > MAX_WINDOW_SECONDS:
            raise ValueError(
                f"janela de {duration:.0f}s excede o máximo permitido de {MAX_WINDOW_SECONDS}s"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"start_utc": self.start_utc.isoformat(), "end_utc": self.end_utc.isoformat()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TickWindow:
        return cls(
            start_utc=datetime.fromisoformat(str(data["start_utc"])),
            end_utc=datetime.fromisoformat(str(data["end_utc"])),
        )

    def chunks(self, chunk_seconds: int) -> list[TickWindow]:
        """Subdivide em chunks contíguos e não sobrepostos cobrindo a janela inteira.

        A fronteira entre chunks é exata: o fim de um chunk é o início do
        próximo, sem gap nem overlap. Combinado com o filtro por `time_msc`
        aplicado na consulta (`>= start`, `< end`), nossas próprias fronteiras
        não perdem nem duplicam um tick que a API já tenha retornado. Isso não
        garante que a fonte (corretora/cache/Tester) tenha fornecido todos os
        ticks realmente existentes no intervalo.
        """

        step = timedelta(seconds=chunk_seconds)
        result: list[TickWindow] = []
        cursor = self.start_utc
        while cursor < self.end_utc:
            chunk_end = min(cursor + step, self.end_utc)
            result.append(TickWindow(start_utc=cursor, end_utc=chunk_end))
            cursor = chunk_end
        return result


@dataclass(frozen=True)
class TickWindowRequest:
    """Contrato de uma solicitação de diagnóstico, com limites rígidos e pequenos."""

    request_id: str
    logical_id: str
    aliases: tuple[str, ...]
    tick_type: str
    windows: tuple[TickWindow, ...]
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id não pode ser vazio")
        if not isinstance(self.logical_id, str) or not self.logical_id.strip():
            raise ValueError("logical_id não pode ser vazio")
        if not self.aliases or not all(isinstance(a, str) and a.strip() for a in self.aliases):
            raise ValueError("aliases deve conter ao menos um símbolo não vazio")
        if self.tick_type not in CANONICAL_TICK_TYPES:
            raise ValueError(
                f"tick_type inválido: {self.tick_type!r}. Use um de {sorted(CANONICAL_TICK_TYPES)}."
            )
        if not self.windows or len(self.windows) > MAX_WINDOWS_PER_REQUEST:
            raise ValueError(
                f"quantidade de janelas deve ser 1..{MAX_WINDOWS_PER_REQUEST} "
                f"(recebido: {len(self.windows)})"
            )
        total_seconds = sum(
            (window.end_utc - window.start_utc).total_seconds() for window in self.windows
        )
        if total_seconds > MAX_TOTAL_SECONDS:
            raise ValueError(
                f"duração total de {total_seconds:.0f}s excede o máximo permitido de "
                f"{MAX_TOTAL_SECONDS}s"
            )
        if (
            isinstance(self.chunk_seconds, bool)
            or not isinstance(self.chunk_seconds, int)
            or not (MIN_CHUNK_SECONDS <= self.chunk_seconds <= MAX_CHUNK_SECONDS)
        ):
            raise ValueError(
                f"chunk_seconds deve ser um inteiro entre {MIN_CHUNK_SECONDS} e "
                f"{MAX_CHUNK_SECONDS} (recebido: {self.chunk_seconds!r})"
            )

    def fingerprint(self) -> str:
        """Impressão digital canônica e determinística da solicitação.

        `request_id` é imutável durante a vida do supervisor: duas
        solicitações com o mesmo `request_id` só são a mesma execução se
        esta impressão coincidir; caso contrário, é um `request_id_conflict`.
        """

        payload = {
            "logical_id": self.logical_id,
            "aliases": list(self.aliases),
            "tick_type": self.tick_type,
            "windows": [w.to_dict() for w in self.windows],
            "chunk_seconds": self.chunk_seconds,
        }
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "logical_id": self.logical_id,
            "aliases": list(self.aliases),
            "tick_type": self.tick_type,
            "windows": [w.to_dict() for w in self.windows],
            "chunk_seconds": self.chunk_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TickWindowRequest:
        if not isinstance(data, dict):
            raise ValueError(f"request deve ser um objeto (recebido: {data!r})")
        windows_payload = data.get("windows")
        if not isinstance(windows_payload, list):
            raise ValueError("windows deve ser uma lista")
        return cls(
            request_id=str(data.get("request_id", "")).strip(),
            logical_id=str(data.get("logical_id", "")).strip(),
            aliases=tuple(str(a).strip() for a in data.get("aliases", []) if str(a).strip()),
            tick_type=str(data.get("tick_type", "")).strip(),
            windows=tuple(TickWindow.from_dict(w) for w in windows_payload),
            chunk_seconds=int(data.get("chunk_seconds", DEFAULT_CHUNK_SECONDS)),
        )


class TickRecord(NamedTuple):
    """Um tick já extraído do array bruto retornado pela API.

    Preserva os dois campos brutos de tempo do MT5 — `time` (segundos) e
    `time_msc` (milissegundos) — em vez de descartar um deles.
    """

    time: int
    time_msc: int
    bid: float
    ask: float
    last: float
    volume: float
    volume_real: float
    flags: int


_SCALAR_FIELDS: tuple[str, ...] = ("bid", "ask", "last", "volume", "volume_real")


def validate_tick_record(record: TickRecord) -> None:
    """Rejeita um `TickRecord` com dados brutos inconsistentes.

    Preços/volumes não finitos (`NaN` ou infinito), timestamps inválidos ou
    incoerentes entre si e flags inválidas são rejeitados. Valores
    exatamente zero continuam válidos e informativos conforme o tipo do
    tick (ex.: um tick `info` sem `last`).
    """

    for name in _SCALAR_FIELDS:
        value = getattr(record, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise ValueError(f"{name} não finito ou inválido: {value!r}")
    if (
        isinstance(record.time_msc, bool)
        or not isinstance(record.time_msc, int)
        or record.time_msc < 0
    ):
        raise ValueError(f"time_msc inválido: {record.time_msc!r}")
    if isinstance(record.time, bool) or not isinstance(record.time, int) or record.time < 0:
        raise ValueError(f"time inválido: {record.time!r}")
    if not (0 <= record.time_msc - record.time * 1000 < 1000):
        raise ValueError(
            f"time ({record.time}) e time_msc ({record.time_msc}) incoerentes entre si"
        )
    if isinstance(record.flags, bool) or not isinstance(record.flags, int) or record.flags < 0:
        raise ValueError(f"flags inválidas: {record.flags!r}")


@dataclass
class TickWindowSummary:
    """Resumo pequeno de uma janela — o único formato que atravessa a fila de eventos."""

    request_id: str
    pid: int | None
    source_id: str
    logical_id: str
    resolved_symbol: str
    tick_type: str
    requested_start_utc: datetime
    requested_end_utc: datetime
    total_count: int
    first_time: int | None
    first_time_msc: int | None
    first_time_utc: datetime | None
    last_time: int | None
    last_time_msc: int | None
    last_time_utc: datetime | None
    observed_duration_seconds: float | None
    leading_gap_seconds: float | None
    trailing_gap_seconds: float | None
    empty_reason: str | None
    non_zero_counts: dict[str, dict[str, int]]
    flags_histogram: dict[str, int]
    out_of_order_count: int
    exact_duplicate_count: int
    time_msc_tie_count: int
    largest_gaps_seconds: list[float]
    coverage_disclaimer: str = COVERAGE_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "pid": self.pid,
            "source_id": self.source_id,
            "logical_id": self.logical_id,
            "resolved_symbol": self.resolved_symbol,
            "tick_type": self.tick_type,
            "requested_start_utc": self.requested_start_utc.isoformat(),
            "requested_end_utc": self.requested_end_utc.isoformat(),
            "total_count": self.total_count,
            "first_time": self.first_time,
            "first_time_msc": self.first_time_msc,
            "first_time_utc": self.first_time_utc.isoformat() if self.first_time_utc else None,
            "last_time": self.last_time,
            "last_time_msc": self.last_time_msc,
            "last_time_utc": self.last_time_utc.isoformat() if self.last_time_utc else None,
            "observed_duration_seconds": self.observed_duration_seconds,
            "leading_gap_seconds": self.leading_gap_seconds,
            "trailing_gap_seconds": self.trailing_gap_seconds,
            "empty_reason": self.empty_reason,
            "non_zero_counts": self.non_zero_counts,
            "flags_histogram": self.flags_histogram,
            "out_of_order_count": self.out_of_order_count,
            "exact_duplicate_count": self.exact_duplicate_count,
            "time_msc_tie_count": self.time_msc_tie_count,
            "largest_gaps_seconds": list(self.largest_gaps_seconds),
            "coverage_disclaimer": self.coverage_disclaimer,
        }


class TickWindowAccumulator:
    """Consome `TickRecord` incrementalmente; nunca guarda o array inteiro.

    Duas noções de "registro" são mantidas separadamente e não devem ser
    confundidas:

    - `_previous`: o último registro **consumido** (ordem de chegada), usado
      só para contar desordem, empates de `time_msc` e duplicatas exatas.
    - `_min_record`/`_max_record`: os registros com o menor e o maior
      `time_msc` já vistos, usados para o "primeiro"/"último" tick e a
      duração observada no resumo. Um retorno fora de ordem nunca pode
      produzir duração ou afastamentos negativos, porque min/max nunca
      dependem da ordem de chegada.

    Mantém também o histograma de flags e um heap limitado com as maiores
    lacunas observadas — memória O(1) por chunk, nunca O(n).
    """

    def __init__(self, *, top_gaps: int = 5) -> None:
        self._top_gaps = top_gaps
        self.total_count = 0
        self._previous: TickRecord | None = None
        self._min_record: TickRecord | None = None
        self._max_record: TickRecord | None = None
        self.non_zero_counts: dict[str, int] = dict.fromkeys(_SCALAR_FIELDS, 0)
        self.flags_histogram: dict[str, int] = {name: 0 for _, name in _TICK_FLAG_BITS}
        self.out_of_order_count = 0
        self.exact_duplicate_count = 0
        self.time_msc_tie_count = 0
        self._gap_heap: list[float] = []

    def consume(self, record: TickRecord) -> None:
        validate_tick_record(record)

        self.total_count += 1
        if self._min_record is None or record.time_msc < self._min_record.time_msc:
            self._min_record = record
        if self._max_record is None or record.time_msc > self._max_record.time_msc:
            self._max_record = record
        for field_name in _SCALAR_FIELDS:
            if getattr(record, field_name) != 0:
                self.non_zero_counts[field_name] += 1
        for bit, name in _TICK_FLAG_BITS:
            if record.flags & bit:
                self.flags_histogram[name] += 1

        previous = self._previous
        if previous is not None:
            if record.time_msc < previous.time_msc:
                self.out_of_order_count += 1
            elif record.time_msc == previous.time_msc:
                self.time_msc_tie_count += 1
                if record == previous:
                    self.exact_duplicate_count += 1
            else:
                gap_seconds = (record.time_msc - previous.time_msc) / 1000.0
                if len(self._gap_heap) < self._top_gaps:
                    heapq.heappush(self._gap_heap, gap_seconds)
                elif gap_seconds > self._gap_heap[0]:
                    heapq.heapreplace(self._gap_heap, gap_seconds)
        self._previous = record

    def finalize(
        self,
        *,
        window: TickWindow,
        request_id: str,
        pid: int | None,
        source_id: str,
        logical_id: str,
        resolved_symbol: str,
        tick_type: str,
    ) -> TickWindowSummary:
        first_dt = (
            datetime.fromtimestamp(self._min_record.time_msc / 1000.0, tz=UTC)
            if self._min_record
            else None
        )
        last_dt = (
            datetime.fromtimestamp(self._max_record.time_msc / 1000.0, tz=UTC)
            if self._max_record
            else None
        )
        # first_dt <= last_dt sempre, porque ambos vêm de min/max de
        # time_msc — nunca da ordem de chegada — então nenhuma destas
        # métricas pode ficar negativa por causa de um retorno fora de ordem.
        observed_duration = (last_dt - first_dt).total_seconds() if first_dt and last_dt else None
        leading_gap = (first_dt - window.start_utc).total_seconds() if first_dt else None
        trailing_gap = (window.end_utc - last_dt).total_seconds() if last_dt else None
        empty_reason = EMPTY_REASON_NO_TICKS if self.total_count == 0 else None
        non_zero_counts = {
            name: {"non_zero": count, "total": self.total_count}
            for name, count in self.non_zero_counts.items()
        }
        return TickWindowSummary(
            request_id=request_id,
            pid=pid,
            source_id=source_id,
            logical_id=logical_id,
            resolved_symbol=resolved_symbol,
            tick_type=tick_type,
            requested_start_utc=window.start_utc,
            requested_end_utc=window.end_utc,
            total_count=self.total_count,
            first_time=self._min_record.time if self._min_record else None,
            first_time_msc=self._min_record.time_msc if self._min_record else None,
            first_time_utc=first_dt,
            last_time=self._max_record.time if self._max_record else None,
            last_time_msc=self._max_record.time_msc if self._max_record else None,
            last_time_utc=last_dt,
            observed_duration_seconds=observed_duration,
            leading_gap_seconds=leading_gap,
            trailing_gap_seconds=trailing_gap,
            empty_reason=empty_reason,
            non_zero_counts=non_zero_counts,
            flags_histogram=dict(self.flags_histogram),
            out_of_order_count=self.out_of_order_count,
            exact_duplicate_count=self.exact_duplicate_count,
            time_msc_tie_count=self.time_msc_tie_count,
            largest_gaps_seconds=sorted(self._gap_heap, reverse=True),
        )
