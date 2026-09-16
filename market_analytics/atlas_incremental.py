"""Materializador incremental do Atlas causal a partir de segmentos M1 Parquet
(DEV-008B.1A).

Transforma o núcleo puro da DEV-008A (`causal_atlas.build_causal_atlas`,
`atlas_contract.to_hub_contract`) num produtor persistente, genérico para
qualquer dataset formado por segmentos M1 Parquet declarados num manifesto.
Este módulo não importa `MetaTrader5`, Qt nem `core/` — não sabe nada sobre
WIN, Clear ou qualquer corretora específica.

Não duplica matemática causal: todo o cálculo de sessão/checkpoint/evento
passa exclusivamente por `causal_atlas.build_causal_atlas` e
`atlas_contract.to_hub_contract`. Este módulo cuida só de: manifesto,
inventário determinístico, planejamento incremental (`no_change`/`append`/
`historical_correction`, falhando fechado em remoção/regressão), objetos
imutáveis endereçados por SHA-256, geração/promoção atômica de `current.json`
e lock de execução.

Layout gravado sob `<output_root>/<logical_id>/` (sempre fora do
repositório):

    state.json          resumo denormalizado, só para auditoria humana —
                         nunca lido para decisões (a verdade é sempre
                         current.json -> generations/<run_id>/manifest.json).
    current.json         ponteiro atômico para a geração vigente.
    lock.json             lock de execução (PID + instante de criação).
    objects/<sha256>.json objetos imutáveis, um por sessão, endereçados por
                          conteúdo — nunca reescritos quando o hash já existe.
    generations/<run_id>/manifest.json
                          geração imutável: lista ordenada de sessões
                          (session_date -> object_sha256) e o "fingerprint"
                          de entrada (arquivos/manifesto/parâmetros) usado
                          pelo atalho `no_change` da próxima execução.
    reports/<run_id>.json relatório compacto da execução que produziu a
                          geração.

Algoritmo incremental (ver `docs/work_orders/DEV-008B1.md`): toda execução
que não bate o atalho `no_change` recalcula a série causal inteira via
`build_causal_atlas` (o núcleo é causal por construção — a linha de uma
sessão nunca depende de sessão futura — então o conteúdo recomputado de uma
sessão anterior a uma correção é sempre byte-idêntico ao publicado
anteriormente). Os objetos são então gravados por conteúdo: uma sessão cujo
hash já existe em `objects/` nunca é reescrita, o que automaticamente
preserva o prefixo em `append` e em `historical_correction` sem precisar de
um caminho de código separado para "recompute parcial".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import psutil
import pyarrow as pa
import pyarrow.parquet as pq

from .atlas_contract import HUB_ATLAS_CONTRACT_VERSION, to_hub_contract
from .bars import Bar
from .causal_atlas import (
    ATR_EVENT_MULTIPLES_DEFAULT,
    ATR_LOOKBACK_SESSIONS_DEFAULT,
    ATR_PERCENTILE_WINDOWS_DEFAULT,
    CHECKPOINT_MINUTES_DEFAULT,
    EVENT_OUTCOME_HORIZONS_MINUTES_DEFAULT,
    OPENING_RANGE_MINUTES_DEFAULT,
    PCT_EVENT_THRESHOLDS_DEFAULT,
    RANGE_EXPANSION_PERCENTILE_DEFAULT,
    AtlasBar,
    AtlasManifest,
    CausalAtlasError,
    bars_content_sha256,
    build_causal_atlas,
)

MATERIALIZATION_MANIFEST_SCHEMA = "ep_market_hub.atlas.materialization_manifest.v1"
GENERATION_MANIFEST_SCHEMA = "ep_market_hub.atlas.materialization_generation.v1"
GENERATION_MANIFEST_SCHEMA_VERSION = 1
CURRENT_POINTER_SCHEMA = "ep_market_hub.atlas.materialization_current.v1"
STATE_SUMMARY_SCHEMA = "ep_market_hub.atlas.materialization_state.v1"
SESSION_OBJECT_SCHEMA = "ep_market_hub.atlas.materialization_session_object.v1"
SESSION_OBJECT_SCHEMA_VERSION = 1
REPORT_SCHEMA = "ep_market_hub.atlas.materialization_report.v1"
RUN_REPORT_SCHEMA = "ep_market_hub.atlas.materialization_run_report.v1"
LOCK_SCHEMA = "ep_market_hub.atlas.materialization_lock.v1"

ALLOWED_INPUT_KINDS: frozenset[str] = frozenset({"m1_segments"})
ALLOWED_STATUSES: frozenset[str] = frozenset({"no_change", "append", "historical_correction"})

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_REQUIRED_PARQUET_COLUMNS: frozenset[str] = frozenset(
    {"timestamp_utc", "open", "high", "low", "close", "volume", "volume_quality", "symbol"}
)

_DATASET_REQUIRED_FIELDS = frozenset(
    {
        "logical_id", "symbol", "series_kind", "source_symbol", "adjustment_method",
        "expected_session_start", "expected_session_end", "input_kind", "segments",
    }
)
_DATASET_OPTIONAL_CAUSAL_FIELDS = frozenset(
    {
        "contract_id", "checkpoint_minutes", "pct_event_thresholds", "atr_event_multiples",
        "opening_range_minutes", "atr_percentile_windows", "event_outcome_horizons_minutes",
        "range_expansion_percentile", "atr_lookback_sessions", "coverage_tolerance_minutes",
        "min_coverage_ratio",
    }
)
_DATASET_FIELDS = _DATASET_REQUIRED_FIELDS | _DATASET_OPTIONAL_CAUSAL_FIELDS

_SEGMENT_FIELDS = frozenset(
    {"segment_id", "path", "source_id", "allowed_start_date", "allowed_end_date", "expected_sha256"}
)
_SEGMENT_REQUIRED_FIELDS = _SEGMENT_FIELDS - {"expected_sha256"}

_MANIFEST_FIELDS = frozenset({"schema", "manifest_id", "output_root", "datasets"})


class MaterializationError(Exception):
    """Erro geral do materializador incremental (a base falha sempre fechada)."""


class ManifestError(MaterializationError):
    """Manifesto de materialização inválido: campo desconhecido/ausente ou fora do contrato."""


class SegmentValidationError(MaterializationError):
    """Segmento Parquet inválido: schema, hash, fronteira ou identidade divergente."""


class RegressionError(MaterializationError):
    """Remoção ou regressão detectada — nunca tratada como append/correção."""


class LockError(MaterializationError):
    """Lock vivo ou verificação ambígua impede uma nova execução."""


# --------------------------------------------------------------------------
# Helpers puros de validação (mesma disciplina de market_analytics.manifest)
# --------------------------------------------------------------------------


def _require_slug(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        raise ManifestError(
            f"{field_name} inválido: deve começar com letra minúscula e conter só letras, dígitos, "
            f"'_'/'-' (recebido: {value!r})"
        )
    return value


def _require_source_id(value: Any) -> str:
    if not isinstance(value, str) or not _SOURCE_ID_RE.match(value):
        raise ManifestError(
            "source_id inválido: deve começar com letra e conter só letras, dígitos, '_'/'-' "
            f"(recebido: {value!r})"
        )
    return value


def _require_nonempty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field_name} não pode ser vazio")
    return value


def _require_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise ManifestError(f"{field_name} deve ser uma string ISO (recebido: {value!r})")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ManifestError(f"{field_name} inválida: {value!r}") from exc


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _strict_json_loads(text: str, *, label: str) -> Any:
    def _reject_constant(value: str) -> None:
        raise ValueError(f"constante não JSON {value!r}")

    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MaterializationError(f"JSON inválido em {label}: {exc}") from exc


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Manifesto de materialização
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentSpec:
    """Um segmento Parquet M1 declarado: identidade, fonte e intervalo permitido.

    `allowed_start_date`/`allowed_end_date` (inclusivos) são a fronteira
    declarada do segmento: toda barra lida do arquivo cujo `session_date` cai
    fora desse intervalo é recusada — a fronteira entre segmentos nunca é
    inferida do conteúdo do arquivo.
    """

    segment_id: str
    path: str
    source_id: str
    allowed_start_date: date
    allowed_end_date: date
    expected_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "segment_id", _require_slug(self.segment_id, "segment_id"))
        object.__setattr__(self, "path", _require_nonempty_str(self.path, "path"))
        object.__setattr__(self, "source_id", _require_source_id(self.source_id))
        if not isinstance(self.allowed_start_date, date) or isinstance(self.allowed_start_date, datetime):
            raise ManifestError(f"allowed_start_date deve ser uma data: {self.allowed_start_date!r}")
        if not isinstance(self.allowed_end_date, date) or isinstance(self.allowed_end_date, datetime):
            raise ManifestError(f"allowed_end_date deve ser uma data: {self.allowed_end_date!r}")
        if self.allowed_end_date < self.allowed_start_date:
            raise ManifestError(
                f"segmento {self.segment_id!r}: allowed_end_date anterior a allowed_start_date"
            )
        if self.expected_sha256 is not None:
            if not isinstance(self.expected_sha256, str) or not _HEX64_RE.match(self.expected_sha256.strip()):
                raise ManifestError(f"segmento {self.segment_id!r}: expected_sha256 inválido")
            object.__setattr__(self, "expected_sha256", self.expected_sha256.strip().lower())

    @classmethod
    def from_dict(cls, data: Any) -> SegmentSpec:
        if not isinstance(data, dict):
            raise ManifestError(f"segmento deve ser um objeto (recebido: {data!r})")
        extra = set(data) - _SEGMENT_FIELDS
        if extra:
            raise ManifestError(f"campo(s) desconhecido(s) em segmento: {sorted(extra)}")
        missing = _SEGMENT_REQUIRED_FIELDS - set(data)
        if missing:
            raise ManifestError(f"campo(s) ausente(s) em segmento: {sorted(missing)}")
        return cls(
            segment_id=data["segment_id"],
            path=data["path"],
            source_id=data["source_id"],
            allowed_start_date=_require_date(data["allowed_start_date"], "allowed_start_date"),
            allowed_end_date=_require_date(data["allowed_end_date"], "allowed_end_date"),
            expected_sha256=data.get("expected_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "path": self.path,
            "source_id": self.source_id,
            "allowed_start_date": self.allowed_start_date.isoformat(),
            "allowed_end_date": self.allowed_end_date.isoformat(),
            "expected_sha256": self.expected_sha256,
        }


@dataclass(frozen=True)
class DatasetSpec:
    """Uma série lógica do manifesto: identidade causal + segmentos M1 declarados.

    Parâmetros causais não declarados usam os defaults versionados da 008A
    (`causal_atlas.py`); `atlas_manifest()` constrói o `AtlasManifest`
    correspondente e reutiliza toda a validação já existente — este módulo
    nunca reimplementa aquelas regras.
    """

    logical_id: str
    symbol: str
    series_kind: str
    source_symbol: str
    adjustment_method: str
    expected_session_start: str
    expected_session_end: str
    input_kind: str
    segments: tuple[SegmentSpec, ...]
    contract_id: str | None = None
    checkpoint_minutes: tuple[int, ...] = CHECKPOINT_MINUTES_DEFAULT
    pct_event_thresholds: tuple[float, ...] = PCT_EVENT_THRESHOLDS_DEFAULT
    atr_event_multiples: tuple[float, ...] = ATR_EVENT_MULTIPLES_DEFAULT
    opening_range_minutes: tuple[int, ...] = OPENING_RANGE_MINUTES_DEFAULT
    atr_percentile_windows: tuple[int, ...] = ATR_PERCENTILE_WINDOWS_DEFAULT
    event_outcome_horizons_minutes: tuple[int, ...] = EVENT_OUTCOME_HORIZONS_MINUTES_DEFAULT
    range_expansion_percentile: float = RANGE_EXPANSION_PERCENTILE_DEFAULT
    atr_lookback_sessions: int = ATR_LOOKBACK_SESSIONS_DEFAULT
    coverage_tolerance_minutes: int = 5
    min_coverage_ratio: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_id", _require_slug(self.logical_id, "logical_id"))
        if self.input_kind not in ALLOWED_INPUT_KINDS:
            raise ManifestError(f"input_kind não suportado: {self.input_kind!r}")
        if not isinstance(self.segments, tuple) or not self.segments:
            raise ManifestError(f"dataset {self.logical_id!r}: segments não pode ser vazio")
        if not all(isinstance(segment, SegmentSpec) for segment in self.segments):
            raise ManifestError(f"dataset {self.logical_id!r}: segments deve conter apenas SegmentSpec")

        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        for segment in self.segments:
            if segment.segment_id in seen_ids:
                raise ManifestError(f"dataset {self.logical_id!r}: segment_id duplicado: {segment.segment_id!r}")
            seen_ids.add(segment.segment_id)
            normalized_path = str(Path(segment.path))
            if normalized_path in seen_paths:
                raise ManifestError(f"dataset {self.logical_id!r}: path de segmento duplicado: {segment.path!r}")
            seen_paths.add(normalized_path)

        ordered = tuple(sorted(self.segments, key=lambda item: item.allowed_start_date))
        object.__setattr__(self, "segments", ordered)
        for previous, current in pairwise(ordered):
            if current.allowed_start_date <= previous.allowed_end_date:
                raise ManifestError(
                    f"dataset {self.logical_id!r}: segmentos {previous.segment_id!r} e {current.segment_id!r} "
                    "têm intervalos de data sobrepostos"
                )

        try:
            self.atlas_manifest()
        except CausalAtlasError as exc:
            raise ManifestError(f"dataset {self.logical_id!r}: {exc}") from exc

    def atlas_manifest(self) -> AtlasManifest:
        return AtlasManifest(
            logical_id=self.logical_id,
            symbol=self.symbol,
            series_kind=self.series_kind,
            source_symbol=self.source_symbol,
            adjustment_method=self.adjustment_method,
            expected_session_start=self.expected_session_start,
            expected_session_end=self.expected_session_end,
            contract_id=self.contract_id,
            checkpoint_minutes=self.checkpoint_minutes,
            pct_event_thresholds=self.pct_event_thresholds,
            atr_event_multiples=self.atr_event_multiples,
            opening_range_minutes=self.opening_range_minutes,
            atr_percentile_windows=self.atr_percentile_windows,
            event_outcome_horizons_minutes=self.event_outcome_horizons_minutes,
            range_expansion_percentile=self.range_expansion_percentile,
            atr_lookback_sessions=self.atr_lookback_sessions,
            coverage_tolerance_minutes=self.coverage_tolerance_minutes,
            min_coverage_ratio=self.min_coverage_ratio,
        )

    def manifest_fingerprint(self) -> str:
        """Hash determinístico da identidade/fronteiras declaradas (sem tocar disco).

        Cobre `input_kind`, os segmentos declarados (identidade, fonte,
        intervalo, hash esperado) e o `params_sha256` causal — a outra
        metade do "fingerprint de entrada" (`_input_fingerprint`) vem do
        hash real dos arquivos, calculado à parte.
        """

        payload = {
            "input_kind": self.input_kind,
            "segments": [segment.to_dict() for segment in self.segments],
            "atlas_params_sha256": self.atlas_manifest().params_sha256(),
        }
        return _hash_payload(payload)

    @classmethod
    def from_dict(cls, data: Any) -> DatasetSpec:
        if not isinstance(data, dict):
            raise ManifestError(f"dataset deve ser um objeto (recebido: {data!r})")
        extra = set(data) - _DATASET_FIELDS
        if extra:
            raise ManifestError(f"campo(s) desconhecido(s) em dataset: {sorted(extra)}")
        missing = _DATASET_REQUIRED_FIELDS - set(data)
        if missing:
            raise ManifestError(f"campo(s) ausente(s) em dataset: {sorted(missing)}")

        segments_raw = data["segments"]
        if not isinstance(segments_raw, list) or not segments_raw:
            raise ManifestError("segments deve ser uma lista não vazia")

        kwargs: dict[str, Any] = {
            "logical_id": data["logical_id"],
            "symbol": data["symbol"],
            "series_kind": data["series_kind"],
            "source_symbol": data["source_symbol"],
            "adjustment_method": data["adjustment_method"],
            "expected_session_start": data["expected_session_start"],
            "expected_session_end": data["expected_session_end"],
            "input_kind": data["input_kind"],
            "segments": tuple(SegmentSpec.from_dict(item) for item in segments_raw),
        }
        if "contract_id" in data:
            kwargs["contract_id"] = data["contract_id"]
        for field_name in (
            "checkpoint_minutes", "pct_event_thresholds", "atr_event_multiples",
            "opening_range_minutes", "atr_percentile_windows", "event_outcome_horizons_minutes",
        ):
            if field_name in data:
                value = data[field_name]
                if not isinstance(value, list):
                    raise ManifestError(f"{field_name} deve ser uma lista")
                kwargs[field_name] = tuple(value)
        for field_name in (
            "range_expansion_percentile", "atr_lookback_sessions",
            "coverage_tolerance_minutes", "min_coverage_ratio",
        ):
            if field_name in data:
                kwargs[field_name] = data[field_name]
        return cls(**kwargs)


@dataclass(frozen=True)
class MaterializationManifest:
    """Manifesto genérico e estrito: identidade do lote + datasets declarados."""

    schema: str
    manifest_id: str
    output_root: str
    datasets: tuple[DatasetSpec, ...]

    def __post_init__(self) -> None:
        if self.schema != MATERIALIZATION_MANIFEST_SCHEMA:
            raise ManifestError(f"schema inesperado: {self.schema!r}")
        object.__setattr__(self, "manifest_id", _require_slug(self.manifest_id, "manifest_id"))
        object.__setattr__(self, "output_root", _require_nonempty_str(self.output_root, "output_root"))
        if not isinstance(self.datasets, tuple) or not self.datasets:
            raise ManifestError("datasets não pode ser vazio")
        seen: set[str] = set()
        for dataset in self.datasets:
            if dataset.logical_id in seen:
                raise ManifestError(f"logical_id duplicado no manifesto: {dataset.logical_id!r}")
            seen.add(dataset.logical_id)

    def dataset(self, logical_id: str) -> DatasetSpec:
        for dataset in self.datasets:
            if dataset.logical_id == logical_id:
                return dataset
        raise ManifestError(f"dataset {logical_id!r} não declarado no manifesto {self.manifest_id!r}")

    @classmethod
    def from_dict(cls, data: Any) -> MaterializationManifest:
        if not isinstance(data, dict):
            raise ManifestError(f"manifesto deve ser um objeto JSON (recebido: {data!r})")
        extra = set(data) - _MANIFEST_FIELDS
        if extra:
            raise ManifestError(f"campo(s) desconhecido(s) no manifesto: {sorted(extra)}")
        missing = _MANIFEST_FIELDS - set(data)
        if missing:
            raise ManifestError(f"campo(s) ausente(s) no manifesto: {sorted(missing)}")
        datasets_raw = data["datasets"]
        if not isinstance(datasets_raw, list) or not datasets_raw:
            raise ManifestError("datasets deve ser uma lista não vazia")
        return cls(
            schema=data["schema"],
            manifest_id=data["manifest_id"],
            output_root=data["output_root"],
            datasets=tuple(DatasetSpec.from_dict(item) for item in datasets_raw),
        )


def load_materialization_manifest_file(path: Path) -> MaterializationManifest:
    """Lê e valida estritamente um manifesto de materialização JSON do disco."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"não foi possível ler o manifesto: {path}: {exc}") from exc
    try:
        data = _strict_json_loads(text, label=str(path))
    except MaterializationError as exc:
        raise ManifestError(str(exc)) from exc
    return MaterializationManifest.from_dict(data)


# --------------------------------------------------------------------------
# Inventário determinístico: arquivo (caminho/tamanho/SHA-256) e barras
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentFileInfo:
    segment_id: str
    path: str
    source_id: str
    size_bytes: int
    sha256: str


def _stat_and_hash_segment(segment: SegmentSpec) -> SegmentFileInfo:
    path = Path(segment.path)
    if not path.is_file():
        raise SegmentValidationError(f"segmento {segment.segment_id!r}: arquivo não encontrado: {path}")
    size_bytes = path.stat().st_size
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    sha256_hex = digest.hexdigest()
    if segment.expected_sha256 is not None and sha256_hex.lower() != segment.expected_sha256:
        raise SegmentValidationError(
            f"segmento {segment.segment_id!r}: sha256 divergente do declarado no manifesto "
            f"(esperado {segment.expected_sha256}, obtido {sha256_hex})"
        )
    return SegmentFileInfo(
        segment_id=segment.segment_id, path=str(path), source_id=segment.source_id,
        size_bytes=size_bytes, sha256=sha256_hex,
    )


def _input_fingerprint(dataset: DatasetSpec, files: Sequence[SegmentFileInfo]) -> dict[str, Any]:
    return {
        "manifest_fingerprint": dataset.manifest_fingerprint(),
        "params_sha256": dataset.atlas_manifest().params_sha256(),
        "segments": [
            {
                "segment_id": info.segment_id, "path": info.path, "source_id": info.source_id,
                "size_bytes": info.size_bytes, "sha256": info.sha256,
            }
            for info in sorted(files, key=lambda item: item.segment_id)
        ],
    }


def _read_segment_bars(segment: SegmentSpec, dataset: DatasetSpec) -> list[AtlasBar]:
    """Lê, valida schema/tipos/fronteira e converte um segmento em `AtlasBar`.

    Só exige a presença das colunas causais mínimas (`_REQUIRED_PARQUET_COLUMNS`)
    mais `source_id`/`contract_id`/`roll_session` quando presentes — colunas
    extras factuais de proveniência (ex.: `tick_volume`, `spread` de um
    produtor M1 real) são ignoradas, nunca recusadas, para que este módulo
    permaneça genérico entre produtores distintos.
    """

    try:
        table = pq.read_table(segment.path)
    except (OSError, pa.ArrowException) as exc:
        raise SegmentValidationError(f"segmento {segment.segment_id!r} ilegível ({segment.path}): {exc}") from exc

    columns = set(table.column_names)
    missing = _REQUIRED_PARQUET_COLUMNS - columns
    if missing:
        raise SegmentValidationError(
            f"segmento {segment.segment_id!r} sem coluna(s) obrigatória(s): {sorted(missing)}"
        )

    ts_field = table.schema.field("timestamp_utc")
    if not (pa.types.is_timestamp(ts_field.type) and ts_field.type.tz is not None):
        raise SegmentValidationError(
            f"segmento {segment.segment_id!r}: timestamp_utc deve ser timestamp timezone-aware"
        )

    row_count = table.num_rows
    if row_count == 0:
        raise SegmentValidationError(f"segmento {segment.segment_id!r} não tem nenhuma barra")

    timestamps = table.column("timestamp_utc").to_pylist()
    opens = table.column("open").to_pylist()
    highs = table.column("high").to_pylist()
    lows = table.column("low").to_pylist()
    closes = table.column("close").to_pylist()
    volumes = table.column("volume").to_pylist()
    volume_qualities = table.column("volume_quality").to_pylist()
    symbols = table.column("symbol").to_pylist()
    source_ids = table.column("source_id").to_pylist() if "source_id" in columns else None
    timeframes = table.column("timeframe").to_pylist() if "timeframe" in columns else None
    contract_ids = table.column("contract_id").to_pylist() if "contract_id" in columns else None
    roll_sessions = table.column("roll_session").to_pylist() if "roll_session" in columns else None

    bars: list[AtlasBar] = []
    for index in range(row_count):
        raw_timestamp = timestamps[index]
        if raw_timestamp is None or raw_timestamp.tzinfo is None:
            raise SegmentValidationError(
                f"segmento {segment.segment_id!r}: timestamp_utc nulo ou sem timezone na linha {index}"
            )
        timestamp = raw_timestamp.astimezone(UTC)

        symbol = symbols[index]
        if symbol != dataset.symbol:
            raise SegmentValidationError(
                f"segmento {segment.segment_id!r}: symbol {symbol!r} diverge do dataset {dataset.symbol!r}"
            )
        if source_ids is not None and source_ids[index] != segment.source_id:
            raise SegmentValidationError(
                f"segmento {segment.segment_id!r}: source_id {source_ids[index]!r} diverge do declarado "
                f"no manifesto ({segment.source_id!r})"
            )
        if timeframes is not None and timeframes[index] != "M1":
            raise SegmentValidationError(
                f"segmento {segment.segment_id!r}: timeframe {timeframes[index]!r} não é M1 na linha {index}"
            )

        session_date = timestamp.date()
        if not (segment.allowed_start_date <= session_date <= segment.allowed_end_date):
            raise SegmentValidationError(
                f"segmento {segment.segment_id!r}: barra em {session_date.isoformat()} fora do intervalo "
                f"permitido [{segment.allowed_start_date.isoformat()}, {segment.allowed_end_date.isoformat()}]"
            )

        contract_id = contract_ids[index] if contract_ids is not None else None
        roll_session = bool(roll_sessions[index]) if roll_sessions is not None and roll_sessions[index] else False

        try:
            bar = Bar(
                source_id=segment.source_id, symbol=symbol, timeframe="M1", timestamp=timestamp,
                open=float(opens[index]), high=float(highs[index]), low=float(lows[index]),
                close=float(closes[index]),
                volume=(float(volumes[index]) if volumes[index] is not None else None),
                volume_quality=volume_qualities[index],
            )
            atlas_bar = AtlasBar(bar=bar, contract_id=contract_id, roll_session=roll_session)
        except ValueError as exc:
            raise SegmentValidationError(f"segmento {segment.segment_id!r}: linha {index} inválida: {exc}") from exc
        bars.append(atlas_bar)

    return bars


def _load_dataset_bars(dataset: DatasetSpec) -> list[AtlasBar]:
    all_bars: list[AtlasBar] = []
    for segment in dataset.segments:
        all_bars.extend(_read_segment_bars(segment, dataset))
    all_bars.sort(key=lambda item: item.timestamp)
    return all_bars


# --------------------------------------------------------------------------
# Objetos imutáveis por sessão (endereçados por conteúdo)
# --------------------------------------------------------------------------


def _group_by_session_date(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["session_date"], []).append(row)
    return grouped


def _group_bars_by_session(bars: Sequence[AtlasBar]) -> dict[str, list[AtlasBar]]:
    grouped: dict[str, list[AtlasBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.session_date.isoformat(), []).append(bar)
    return grouped


def _session_object_payload(
    *,
    logical_id: str,
    session_date: str,
    session_row: dict[str, Any],
    checkpoint_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    outside_core_rows: list[dict[str, Any]],
    quality_issue_rows: list[dict[str, Any]],
    bars_sha256: str,
    core_version: int,
    params_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": SESSION_OBJECT_SCHEMA,
        "schema_version": SESSION_OBJECT_SCHEMA_VERSION,
        "hub_contract_version": HUB_ATLAS_CONTRACT_VERSION,
        "core_version": core_version,
        "logical_id": logical_id,
        "session_date": session_date,
        "available_at_utc": session_row["available_at_utc"],
        "params_sha256": params_sha256,
        "bars_sha256": bars_sha256,
        "session_row": session_row,
        "checkpoint_rows": checkpoint_rows,
        "event_rows": event_rows,
        "outside_core_rows": outside_core_rows,
        "quality_issue_rows": quality_issue_rows,
    }


def _object_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _write_object_if_absent(objects_dir: Path, payload: dict[str, Any]) -> str:
    """Grava o objeto endereçado por conteúdo se ainda não existir.

    Um hash já presente em disco nunca é reescrito — é assim que o prefixo
    comprovadamente idêntico sobrevive intacto (bytes e mtime) entre
    gerações, sem precisar de um caminho de código separado para "copiar o
    prefixo".
    """

    content = _canonical_json(payload)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    objects_dir.mkdir(parents=True, exist_ok=True)
    target = objects_dir / f"{digest}.json"
    if target.exists():
        try:
            if target.read_text(encoding="utf-8") != content:
                raise MaterializationError(
                    f"objeto existente {target} não corresponde ao conteúdo endereçado por seu SHA-256"
                )
        except OSError as exc:
            raise MaterializationError(f"não foi possível validar objeto existente {target}: {exc}") from exc
        return digest
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=str(objects_dir), suffix=".tmp"
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(str(temp_path), str(target))
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return digest


def _issue_counts(quality_issue_rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in quality_issue_rows:
        counts[row["issue_type"]] = counts.get(row["issue_type"], 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------
# Lock por logical_id (PID + instante de criação)
# --------------------------------------------------------------------------


def _process_is_alive(pid: int, started_at: Any) -> bool | None:
    """`True`/`False` só quando a identidade do processo pode ser confirmada;
    `None` (ambíguo) em qualquer outro caso — o chamador trata isso como
    "vivo" por segurança (falha fechado)."""

    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return None
    if not isinstance(started_at, int | float) or isinstance(started_at, bool):
        return None
    try:
        actual_started_at = process.create_time()
    except Exception:
        return None
    return abs(actual_started_at - started_at) < 1.0


class DatasetLock:
    """Lock exclusivo de execução por `logical_id`.

    Identificado por PID + instante de criação do processo (mesma disciplina
    de `market_analytics.backfill_catalog`/`owner_pid`/`owner_process_started_at`):
    um lock cujo dono real não pode ser confirmado como vivo OU morto bloqueia
    a nova execução (`LockError`); só um lock comprovadamente órfão (processo
    confirmadamente morto, ou PID reaproveitado por outro processo) é
    recuperado automaticamente.
    """

    def __init__(self, dataset_root: Path):
        self.path = Path(dataset_root) / "lock.json"

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            self._reclaim_if_orphan()
        pid = os.getpid()
        try:
            started_at: float | None = psutil.Process(pid).create_time()
        except Exception:
            started_at = None
        payload = {
            "schema": LOCK_SCHEMA,
            "pid": pid,
            "process_started_at": started_at,
            "acquired_at_utc": datetime.now(UTC).isoformat(),
        }
        content = _canonical_json(payload)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise LockError(f"lock concorrente detectado ao adquirir {self.path}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.path.unlink(missing_ok=True)
            raise

    def _reclaim_if_orphan(self) -> None:
        try:
            data = _strict_json_loads(self.path.read_text(encoding="utf-8"), label=str(self.path))
        except (OSError, MaterializationError) as exc:
            raise LockError(f"lock existente ilegível em {self.path} — bloqueado por segurança") from exc
        pid = data.get("pid")
        started_at = data.get("process_started_at")
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise LockError(f"lock existente sem pid válido em {self.path} — bloqueado por segurança")
        alive = _process_is_alive(pid, started_at)
        if alive is None:
            raise LockError(
                f"não foi possível verificar o processo dono do lock (PID {pid}) — bloqueado por segurança"
            )
        if alive:
            raise LockError(f"lock vivo (PID {pid}) para {self.path.parent} — execução já em andamento")
        self.path.unlink(missing_ok=True)

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> DatasetLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


# --------------------------------------------------------------------------
# Leitura da geração vigente e escrita atômica
# --------------------------------------------------------------------------


def _read_current_generation(
    dataset_root: Path, *, expected_logical_id: str | None = None
) -> dict[str, Any] | None:
    current_path = dataset_root / "current.json"
    if not current_path.is_file():
        return None
    try:
        current = _strict_json_loads(current_path.read_text(encoding="utf-8"), label=str(current_path))
    except (OSError, MaterializationError) as exc:
        raise MaterializationError(f"current.json ilegível em {dataset_root}: {exc}") from exc
    if not isinstance(current, dict) or current.get("schema") != CURRENT_POINTER_SCHEMA:
        raise MaterializationError(f"current.json inválido em {dataset_root}: schema inesperado")
    run_id = current.get("current_generation")
    if (
        not isinstance(run_id, str)
        or not run_id
        or Path(run_id).name != run_id
        or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id)
    ):
        raise MaterializationError(f"current.json inválido em {dataset_root}: current_generation ausente")
    generation_path = dataset_root / "generations" / run_id / "manifest.json"
    try:
        generation = _strict_json_loads(
            generation_path.read_text(encoding="utf-8"), label=str(generation_path)
        )
    except (OSError, MaterializationError) as exc:
        raise MaterializationError(f"geração {run_id!r} referenciada por current.json ilegível: {exc}") from exc
    if not isinstance(generation, dict):
        raise MaterializationError(f"geração {run_id!r} inválida: raiz não é objeto")
    if generation.get("schema") != GENERATION_MANIFEST_SCHEMA:
        raise MaterializationError(f"geração {run_id!r} inválida: schema inesperado")
    if generation.get("schema_version") != GENERATION_MANIFEST_SCHEMA_VERSION:
        raise MaterializationError(f"geração {run_id!r} inválida: versão inesperada")
    if generation.get("run_id") != run_id:
        raise MaterializationError(f"geração {run_id!r} inválida: run_id divergente")
    logical_id = generation.get("logical_id")
    if expected_logical_id is not None and logical_id != expected_logical_id:
        raise MaterializationError(
            f"geração {run_id!r} pertence a {logical_id!r}, não a {expected_logical_id!r}"
        )
    if not isinstance(generation.get("input_inventory"), dict):
        raise MaterializationError(f"geração {run_id!r} inválida: input_inventory ausente")
    params_sha256 = generation.get("params_sha256")
    if not isinstance(params_sha256, str) or not _HEX64_RE.fullmatch(params_sha256):
        raise MaterializationError(f"geração {run_id!r} inválida: params_sha256 inválido")
    sessions = generation.get("sessions")
    if not isinstance(sessions, list):
        raise MaterializationError(f"geração {run_id!r} inválida: sessions ausente")
    previous_date: date | None = None
    for index, item in enumerate(sessions):
        if not isinstance(item, dict):
            raise MaterializationError(f"geração {run_id!r}: sessão {index} inválida")
        try:
            session_date = date.fromisoformat(item["session_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MaterializationError(
                f"geração {run_id!r}: session_date inválida no item {index}"
            ) from exc
        if previous_date is not None and session_date <= previous_date:
            raise MaterializationError(f"geração {run_id!r}: sessões fora de ordem ou duplicadas")
        previous_date = session_date
        object_sha256 = item.get("object_sha256")
        if not isinstance(object_sha256, str) or not _HEX64_RE.fullmatch(object_sha256):
            raise MaterializationError(f"geração {run_id!r}: object_sha256 inválido no item {index}")
        object_path = dataset_root / "objects" / f"{object_sha256}.json"
        try:
            object_bytes = object_path.read_bytes()
        except OSError as exc:
            raise MaterializationError(
                f"geração {run_id!r}: objeto ausente ou ilegível: {object_path}"
            ) from exc
        if hashlib.sha256(object_bytes).hexdigest() != object_sha256:
            raise MaterializationError(f"geração {run_id!r}: objeto corrompido: {object_path}")
    return generation


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp"
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(str(temp_path), str(path))
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _new_run_id(now: Callable[[], datetime]) -> str:
    stamp = now().strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def _promote(
    dataset_root: Path,
    run_id: str,
    generation_manifest: dict[str, Any],
    report_payload: dict[str, Any],
    *,
    now: Callable[[], datetime],
) -> None:
    """Publica a geração nova e só então troca `current.json` (última etapa).

    A geração inteira é montada num diretório temporário no mesmo volume e
    promovida por um único `os.replace` de diretório — uma falha antes dessa
    troca nunca deixa uma geração parcial visível em `generations/`, e uma
    falha depois dela (ao gravar `reports/`/`state.json`) nunca impede a
    troca final de `current.json`, que é sempre a última escrita.
    """

    dataset_root.mkdir(parents=True, exist_ok=True)
    generations_dir = dataset_root / "generations"
    generations_dir.mkdir(parents=True, exist_ok=True)
    tmp_gen_dir = Path(tempfile.mkdtemp(prefix=".gen_tmp_", dir=str(generations_dir)))
    try:
        manifest_path = tmp_gen_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                generation_manifest,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        # No Windows, FlushFileBuffers (usado por os.fsync) exige um handle
        # gravável; abrir como somente leitura produz EBADF.
        with open(manifest_path, "r+b") as handle:
            os.fsync(handle.fileno())
        final_gen_dir = generations_dir / run_id
        os.replace(str(tmp_gen_dir), str(final_gen_dir))
    except Exception:
        shutil.rmtree(tmp_gen_dir, ignore_errors=True)
        raise

    _atomic_write_json(dataset_root / "reports" / f"{run_id}.json", report_payload)

    sessions = generation_manifest["sessions"]
    state_summary = {
        "schema": STATE_SUMMARY_SCHEMA,
        "logical_id": generation_manifest["logical_id"],
        "current_generation": run_id,
        "status": generation_manifest["status"],
        "total_sessions": len(sessions),
        "last_session_date": sessions[-1]["session_date"] if sessions else None,
        "updated_at_utc": now().isoformat(),
    }
    _atomic_write_json(dataset_root / "state.json", state_summary)

    current_payload = {
        "schema": CURRENT_POINTER_SCHEMA,
        "current_generation": run_id,
        "promoted_at_utc": now().isoformat(),
    }
    _atomic_write_json(dataset_root / "current.json", current_payload)


# --------------------------------------------------------------------------
# Planejamento incremental
# --------------------------------------------------------------------------


def _classify(
    old_sessions: list[dict[str, Any]], new_sessions: list[dict[str, Any]]
) -> tuple[str, str | None]:
    """Classifica a transição `old_sessions -> new_sessions` (ambas ordenadas
    por `session_date`, cada item com `session_date`/`object_sha256`).

    Levanta `RegressionError` fechado diante de qualquer remoção ou
    reordenação — nunca interpreta isso como `append`."""

    if old_sessions == new_sessions:
        return "no_change", None

    old_by_date = {item["session_date"]: item for item in old_sessions}
    new_by_date = {item["session_date"]: item for item in new_sessions}
    old_dates = set(old_by_date)
    new_dates = set(new_by_date)
    missing = sorted(old_dates - new_dates)
    if missing or len(new_sessions) < len(old_sessions):
        raise RegressionError(
            "remoção ou regressão detectada: a nova série não contém todas as sessões anteriores "
            f"(ausentes: {missing or 'contagem total menor que a geração anterior'})"
        )

    changed_dates = {
        session_date
        for session_date in old_dates
        if old_by_date[session_date]["object_sha256"]
        != new_by_date[session_date]["object_sha256"]
    }
    added_dates = new_dates - old_dates

    if not old_sessions:
        return "append", new_sessions[0]["session_date"]

    last_old_date = old_sessions[-1]["session_date"]
    historical_additions = {session_date for session_date in added_dates if session_date <= last_old_date}
    if not changed_dates and not historical_additions:
        return "append", min(added_dates)

    return "historical_correction", min(changed_dates | historical_additions)


# --------------------------------------------------------------------------
# Relatório e orquestração pública
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterializationReport:
    logical_id: str
    status: str
    previous_generation: str | None
    new_generation: str | None
    sessions_added: list[str]
    sessions_recalculated: list[str]
    first_changed_session_date: str | None
    total_sessions: int
    issues: dict[str, int]
    input_inventory_sha256: str
    params_sha256: str
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "logical_id": self.logical_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "previous_generation": self.previous_generation,
            "new_generation": self.new_generation,
            "sessions_added": list(self.sessions_added),
            "sessions_recalculated": list(self.sessions_recalculated),
            "first_changed_session_date": self.first_changed_session_date,
            "total_sessions": self.total_sessions,
            "issues": dict(self.issues),
            "input_inventory_sha256": self.input_inventory_sha256,
            "params_sha256": self.params_sha256,
        }


def materialize_dataset(
    dataset: DatasetSpec,
    dataset_root: Path,
    *,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MaterializationReport:
    """Ponto de entrada único por dataset: inventaria, planeja e (fora de
    `dry_run`) publica a nova geração sob `dataset_root`.

    Reexecuta `build_causal_atlas` sobre a série inteira sempre que o atalho
    `no_change` (hashes de arquivo/manifesto/parâmetros idênticos aos da
    última geração) não se aplica — nunca tenta "continuar de onde parou"
    dentro do núcleo causal, que permanece o único dono da matemática.
    """

    dataset_root = Path(dataset_root)

    files = [_stat_and_hash_segment(segment) for segment in dataset.segments]
    new_input_inventory = _input_fingerprint(dataset, files)
    input_inventory_sha256 = _hash_payload(new_input_inventory)

    previous_generation = _read_current_generation(
        dataset_root, expected_logical_id=dataset.logical_id
    )
    previous_run_id = previous_generation["run_id"] if previous_generation else None

    if previous_generation is not None and previous_generation["input_inventory"] == new_input_inventory:
        old_sessions = previous_generation["sessions"]
        return MaterializationReport(
            logical_id=dataset.logical_id,
            status="no_change",
            previous_generation=previous_run_id,
            new_generation=previous_run_id,
            sessions_added=[],
            sessions_recalculated=[],
            first_changed_session_date=None,
            total_sessions=len(old_sessions),
            issues={},
            input_inventory_sha256=input_inventory_sha256,
            params_sha256=previous_generation["params_sha256"],
            dry_run=dry_run,
        )

    atlas_manifest = dataset.atlas_manifest()
    bars = _load_dataset_bars(dataset)
    cutoff_date = now().date()
    open_session_dates = sorted(
        {bar.session_date for bar in bars if bar.session_date >= cutoff_date}
    )
    if open_session_dates:
        raise SegmentValidationError(
            "DEV-008B.1A aceita somente sessões anteriores à data de execução; "
            f"sessão ainda não elegível: {open_session_dates[0].isoformat()}"
        )
    result = build_causal_atlas(bars, atlas_manifest)
    hub_payload = to_hub_contract(result)

    session_rows_by_date = _group_by_session_date(hub_payload["session_rows"])
    checkpoint_rows_by_date = _group_by_session_date(hub_payload["checkpoint_rows"])
    event_rows_by_date = _group_by_session_date(hub_payload["event_rows"])
    outside_rows_by_date = _group_by_session_date(hub_payload["outside_core_rows"])
    quality_rows_by_date = _group_by_session_date(hub_payload["quality_issue_rows"])
    bars_by_date = _group_bars_by_session(bars)

    session_dates_sorted = sorted(session_rows_by_date)
    payloads_by_date: dict[str, dict[str, Any]] = {}
    new_sessions: list[dict[str, Any]] = []
    for session_date in session_dates_sorted:
        session_row = session_rows_by_date[session_date][0]
        payload = _session_object_payload(
            logical_id=dataset.logical_id,
            session_date=session_date,
            session_row=session_row,
            checkpoint_rows=checkpoint_rows_by_date.get(session_date, []),
            event_rows=event_rows_by_date.get(session_date, []),
            outside_core_rows=outside_rows_by_date.get(session_date, []),
            quality_issue_rows=quality_rows_by_date.get(session_date, []),
            bars_sha256=bars_content_sha256(bars_by_date.get(session_date, [])),
            core_version=result.core_version,
            params_sha256=result.params_sha256,
        )
        payloads_by_date[session_date] = payload
        new_sessions.append(
            {
                "session_date": session_date,
                "object_sha256": _object_hash(payload),
                "available_at_utc": session_row["available_at_utc"],
            }
        )

    old_sessions = previous_generation["sessions"] if previous_generation is not None else []
    status, first_changed = _classify(old_sessions, new_sessions)
    issues = _issue_counts(hub_payload["quality_issue_rows"])

    if status == "no_change":
        # O conteúdo causal pode permanecer idêntico apesar de uma mudança
        # auditável de manifesto/caminho/hash de arquivo. Como o atalho por
        # inventário já falhou, publique a nova proveniência uma única vez;
        # caso contrário toda execução futura reconstruiria a série de novo.
        status = "historical_correction"
        first_changed = new_sessions[0]["session_date"]

    old_dates = {item["session_date"] for item in old_sessions}
    if status == "append":
        sessions_added = [
            item["session_date"] for item in new_sessions if item["session_date"] not in old_dates
        ]
        sessions_recalculated: list[str] = []
    else:
        sessions_recalculated = [
            item["session_date"]
            for item in new_sessions
            if item["session_date"] in old_dates and item["session_date"] >= first_changed
        ]
        sessions_added = [
            item["session_date"] for item in new_sessions if item["session_date"] not in old_dates
        ]

    if dry_run:
        return MaterializationReport(
            logical_id=dataset.logical_id,
            status=status,
            previous_generation=previous_run_id,
            new_generation=None,
            sessions_added=sessions_added,
            sessions_recalculated=sessions_recalculated,
            first_changed_session_date=first_changed,
            total_sessions=len(new_sessions),
            issues=issues,
            input_inventory_sha256=input_inventory_sha256,
            params_sha256=result.params_sha256,
            dry_run=True,
        )

    with DatasetLock(dataset_root):
        current_check = _read_current_generation(
            dataset_root, expected_logical_id=dataset.logical_id
        )
        current_run_id_now = current_check["run_id"] if current_check is not None else None
        if current_run_id_now != previous_run_id:
            raise MaterializationError(
                f"geração atual de {dataset.logical_id!r} mudou durante a execução "
                f"(esperado {previous_run_id!r}, encontrado {current_run_id_now!r}); execute novamente"
            )

        objects_dir = dataset_root / "objects"
        for session_date in session_dates_sorted:
            _write_object_if_absent(objects_dir, payloads_by_date[session_date])

        run_id = _new_run_id(now)
        generation_manifest = {
            "schema": GENERATION_MANIFEST_SCHEMA,
            "schema_version": GENERATION_MANIFEST_SCHEMA_VERSION,
            "run_id": run_id,
            "logical_id": dataset.logical_id,
            "previous_generation": previous_run_id,
            "status": status,
            "first_changed_session_date": first_changed,
            "created_at_utc": now().isoformat(),
            "core_version": result.core_version,
            "params_sha256": result.params_sha256,
            "input_inventory": new_input_inventory,
            "sessions": new_sessions,
            "summary": result.summary,
        }
        report = MaterializationReport(
            logical_id=dataset.logical_id,
            status=status,
            previous_generation=previous_run_id,
            new_generation=run_id,
            sessions_added=sessions_added,
            sessions_recalculated=sessions_recalculated,
            first_changed_session_date=first_changed,
            total_sessions=len(new_sessions),
            issues=issues,
            input_inventory_sha256=input_inventory_sha256,
            params_sha256=result.params_sha256,
            dry_run=False,
        )
        _promote(dataset_root, run_id, generation_manifest, report.to_dict(), now=now)

    return report


# --------------------------------------------------------------------------
# Fronteira de destino (nunca dentro do repositório) e CLI
# --------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _paths_overlap(a: Path, b: Path) -> bool:
    for x, y in ((a, b), (b, a)):
        try:
            x.relative_to(y)
        except ValueError:
            continue
        return True
    return False


def assert_output_outside_repo(output_root: Path) -> Path:
    """Recusa um `output_root` que coincida com o repositório ou o contenha.

    Mesma disciplina defensiva de `market_analytics.win_m1_features`: os
    artefatos reais deste materializador nunca podem ser versionados por
    acidente."""

    resolved = Path(output_root).expanduser().resolve(strict=False)
    if resolved.parent == resolved:
        raise MaterializationError(f"output_root não pode ser a raiz de um volume: {resolved}")
    repo_root = _repo_root()
    if _paths_overlap(resolved, repo_root):
        raise MaterializationError(
            f"output_root não pode coincidir com o repositório nem estar contido nele: {resolved}"
        )
    return resolved


def run_update(
    *,
    manifest_path: Path,
    output_root: Path,
    dataset_id: str | None = None,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Orquestra a CLI: carrega o manifesto e materializa um ou todos os datasets."""

    manifest = load_materialization_manifest_file(Path(manifest_path))
    resolved_output_root = assert_output_outside_repo(output_root)
    declared_output_root = assert_output_outside_repo(Path(manifest.output_root))
    if declared_output_root != resolved_output_root:
        raise ManifestError(
            "output_root da CLI diverge do declarado no manifesto "
            f"({resolved_output_root} != {declared_output_root})"
        )

    datasets = (manifest.dataset(dataset_id),) if dataset_id is not None else manifest.datasets

    reports = []
    for dataset in datasets:
        dataset_root = resolved_output_root / dataset.logical_id
        report = materialize_dataset(dataset, dataset_root, dry_run=dry_run, now=now)
        reports.append(report.to_dict())

    return {
        "schema": RUN_REPORT_SCHEMA,
        "manifest_id": manifest.manifest_id,
        "output_root": str(resolved_output_root),
        "dry_run": dry_run,
        "datasets": reports,
    }


__all__ = [
    "ALLOWED_INPUT_KINDS",
    "ALLOWED_STATUSES",
    "CURRENT_POINTER_SCHEMA",
    "GENERATION_MANIFEST_SCHEMA",
    "GENERATION_MANIFEST_SCHEMA_VERSION",
    "LOCK_SCHEMA",
    "MATERIALIZATION_MANIFEST_SCHEMA",
    "REPORT_SCHEMA",
    "RUN_REPORT_SCHEMA",
    "SESSION_OBJECT_SCHEMA",
    "SESSION_OBJECT_SCHEMA_VERSION",
    "STATE_SUMMARY_SCHEMA",
    "DatasetLock",
    "DatasetSpec",
    "LockError",
    "ManifestError",
    "MaterializationError",
    "MaterializationManifest",
    "MaterializationReport",
    "RegressionError",
    "SegmentSpec",
    "SegmentValidationError",
    "assert_output_outside_repo",
    "load_materialization_manifest_file",
    "materialize_dataset",
    "run_update",
]
