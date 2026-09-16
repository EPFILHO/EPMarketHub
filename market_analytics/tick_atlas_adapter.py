"""Adaptador genérico e offline de sessões de ticks Parquet encerradas para
segmentos M1 consumíveis por `market_analytics.atlas_incremental` (DEV-008B.1B).

Este módulo não importa `MetaTrader5`, Qt nem rede. Reaproveita
deliberadamente:

- `market_analytics.quant_mvp.discover_sessions` (descoberta
  `year=*/month=*/session_date=*/ticks.parquet`) e
  `market_analytics.quant_mvp.read_session_ticks_to_m1` (leitura por
  batches, política de preço/volume e deduplicação exata adjacente já
  provadas pelo MVP WIN) — nunca reimplementa essa matemática;
- `market_analytics.atlas_incremental.SegmentSpec`/`DatasetSpec`/
  `MaterializationManifest` para validar a identidade causal declarada e
  gerar um manifesto `ep_market_hub.atlas.materialization_manifest.v1`
  aceito por `tools/update_market_atlas.py` sem reimplementar aquela
  validação.

Este módulo cuida só de: um manifesto estrito e versionado de adaptador
(identidade individual de um contrato + raízes de entrada/saída), inventário
incremental por sessão (SHA-256/tamanho do `ticks.parquet` de origem),
construção de um Parquet M1 determinístico por sessão e geração atômica do
manifesto de materialização do Atlas para essas sessões. Nunca executa o
materializador Atlas (`atlas_incremental.materialize_dataset`/`run_update`)
— essa é uma fase seguinte, com seu próprio portão de aprovação.
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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from . import atlas_incremental, quant_mvp
from .bars import Bar
from .causal_atlas import CausalAtlasError

MANIFEST_SCHEMA = "ep_market_hub.atlas.tick_m1_adapter_manifest.v1"
STATE_SCHEMA = "ep_market_hub.atlas.tick_m1_adapter_state.v1"
CURRENT_SCHEMA = "ep_market_hub.atlas.tick_m1_adapter_current.v1"
RUN_REPORT_SCHEMA = "ep_market_hub.atlas.tick_m1_adapter_report.v1"
M1_SEGMENT_SCHEMA = "ep_market_hub.atlas.tick_m1_segment.v1"
M1_SEGMENT_SCHEMA_VERSION = 1
PRODUCER_VERSION = "dev-008b1b-tick-atlas-adapter-1"

# DEV-008B.1B trabalha somente com contratos individuais, sem ajuste — uma
# série contínua/ajustada pertence a outro produtor (DEV-008B.1A). Estes
# valores são exigidos literalmente no manifesto, nunca inferidos.
FIXED_SERIES_KIND = "individual_contract"
FIXED_ADJUSTMENT_METHOD = "none"

_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "schema", "manifest_id", "input_root", "derived_m1_root", "atlas_output_root",
        "logical_id", "source_id", "resolved_symbol", "contract_id", "series_kind",
        "source_symbol", "adjustment_method", "session_timezone",
        "expected_session_start", "expected_session_end",
    }
)
_CAUSAL_OPTIONAL_FIELDS: frozenset[str] = frozenset(
    {
        "checkpoint_minutes", "pct_event_thresholds", "atr_event_multiples",
        "opening_range_minutes", "atr_percentile_windows", "event_outcome_horizons_minutes",
        "range_expansion_percentile", "atr_lookback_sessions", "coverage_tolerance_minutes",
        "min_coverage_ratio",
    }
)
_CAUSAL_LIST_FIELDS: frozenset[str] = frozenset(
    {
        "checkpoint_minutes", "pct_event_thresholds", "atr_event_multiples",
        "opening_range_minutes", "atr_percentile_windows", "event_outcome_horizons_minutes",
    }
)
_OPTIONAL_FIELDS: frozenset[str] = _CAUSAL_OPTIONAL_FIELDS | {"batch_size"}
_ALL_FIELDS: frozenset[str] = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_STATE_SESSION_FIELDS = frozenset(
    {"tick_path", "tick_sha256", "tick_size_bytes", "m1_path", "m1_sha256", "m1_size_bytes"}
)

# Metadados opcionais de proveniência que um `ticks.parquet` de contrato pode
# (ou não) carregar (ver `market_analytics.tick_backfill.SERIES_METADATA_KEYS`).
# Nenhum caminho existente hoje os grava; quando ausentes, a checagem
# correspondente é simplesmente pulada ("quando disponível") — nunca exigida.
_OPTIONAL_IDENTITY_METADATA_KEYS: tuple[tuple[str, str], ...] = (
    ("series_series_kind", "series_kind"),
    ("series_source_symbol", "source_symbol"),
    ("series_adjustment_method", "adjustment_method"),
    ("series_contract_id", "contract_id"),
)


class TickAdapterError(Exception):
    """Erro geral do adaptador de ticks (a base falha sempre fechada)."""


class TickManifestError(TickAdapterError):
    """Manifesto do adaptador inválido: campo desconhecido/ausente ou identidade causal inválida."""


class TickRegressionError(TickAdapterError):
    """Remoção/regressão de sessão previamente publicada — nunca tratada como append."""


def _require_nonempty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TickManifestError(f"{field_name} não pode ser vazio")
    return value


def _strict_json_loads(text: str, *, label: str) -> Any:
    def _reject_constant(value: str) -> None:
        raise ValueError(f"constante não JSON {value!r}")

    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise TickAdapterError(f"JSON inválido em {label}: {exc}") from exc


def _paths_overlap(a: Path, b: Path) -> bool:
    for x, y in ((a, b), (b, a)):
        try:
            x.relative_to(y)
        except ValueError:
            continue
        return True
    return False


def _placeholder_segment(source_id: str) -> atlas_incremental.SegmentSpec:
    """Segmento sintético só para validar a identidade causal sem tocar disco.

    `SegmentSpec`/`DatasetSpec.__post_init__` são puros (nunca abrem o
    arquivo declarado) — só `atlas_incremental._stat_and_hash_segment`, que
    não é chamado aqui, exige que `path` exista de verdade. Isso permite
    reaproveitar toda a validação causal de `DatasetSpec` (vocabulário de
    `series_kind`, formato HH:MM, faixas dos parâmetros causais) no momento
    em que o manifesto do adaptador é carregado, antes de descobrir qualquer
    sessão.
    """

    return atlas_incremental.SegmentSpec(
        segment_id="preflight",
        path="__preflight_placeholder__",
        source_id=source_id,
        allowed_start_date=date(1970, 1, 1),
        allowed_end_date=date(1970, 1, 1),
    )


@dataclass(frozen=True)
class TickAdapterManifest:
    """Manifesto estrito, genérico e versionado do adaptador de ticks.

    Declara as três raízes fora do repositório (`input_root`/
    `derived_m1_root`/`atlas_output_root`) e a identidade individual de um
    único contrato: `logical_id`/`source_id`/`resolved_symbol`/`contract_id`
    (identidade do `ticks.parquet` bruto e do M1 derivado) e
    `series_kind`/`source_symbol`/`adjustment_method`/`session_timezone`/
    `expected_session_start`/`expected_session_end` (identidade causal do
    Atlas — `series_kind` e `adjustment_method` são fixos para este
    adaptador). Parâmetros causais opcionais não declarados usam os defaults
    versionados de `causal_atlas` via `DatasetSpec`, nunca redeclarados aqui.
    """

    schema: str
    manifest_id: str
    input_root: str
    derived_m1_root: str
    atlas_output_root: str
    logical_id: str
    source_id: str
    resolved_symbol: str
    contract_id: str
    series_kind: str
    source_symbol: str
    adjustment_method: str
    session_timezone: str
    expected_session_start: str
    expected_session_end: str
    batch_size: int = 200_000
    causal_overrides: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != MANIFEST_SCHEMA:
            raise TickManifestError(f"schema inesperado: {self.schema!r}")
        for field_name in (
            "manifest_id", "input_root", "derived_m1_root", "atlas_output_root",
            "logical_id", "source_id", "resolved_symbol", "contract_id", "source_symbol",
            "session_timezone", "expected_session_start", "expected_session_end",
        ):
            _require_nonempty_str(getattr(self, field_name), field_name)
        if not _SLUG_RE.fullmatch(self.manifest_id):
            raise TickManifestError(f"manifest_id inválido: {self.manifest_id!r}")
        if self.series_kind != FIXED_SERIES_KIND:
            raise TickManifestError(
                f"series_kind deve ser {FIXED_SERIES_KIND!r} neste adaptador (recebido: {self.series_kind!r})"
            )
        if self.adjustment_method != FIXED_ADJUSTMENT_METHOD:
            raise TickManifestError(
                "adjustment_method deve ser "
                f"{FIXED_ADJUSTMENT_METHOD!r} neste adaptador (recebido: {self.adjustment_method!r})"
            )
        if isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int) or self.batch_size <= 0:
            raise TickManifestError(f"batch_size deve ser um inteiro positivo (recebido: {self.batch_size!r})")
        try:
            ZoneInfo(self.session_timezone)
        except Exception as exc:
            raise TickManifestError(f"session_timezone inválida: {self.session_timezone!r}") from exc

        # Preflight: reaproveita toda a validação causal de DatasetSpec (sem
        # tocar disco) para recusar cedo uma identidade/parâmetro inválido.
        try:
            self.to_dataset_spec(segments=(_placeholder_segment(self.source_id),))
        except (atlas_incremental.ManifestError, CausalAtlasError) as exc:
            raise TickManifestError(f"identidade causal inválida no manifesto do adaptador: {exc}") from exc

    def to_dataset_spec(self, *, segments: tuple[atlas_incremental.SegmentSpec, ...]) -> atlas_incremental.DatasetSpec:
        return atlas_incremental.DatasetSpec(
            logical_id=self.logical_id,
            symbol=self.resolved_symbol,
            series_kind=self.series_kind,
            source_symbol=self.source_symbol,
            adjustment_method=self.adjustment_method,
            expected_session_start=self.expected_session_start,
            expected_session_end=self.expected_session_end,
            input_kind="m1_segments",
            segments=segments,
            contract_id=self.contract_id,
            **self.causal_overrides,
        )

    def fingerprint(self) -> str:
        payload = {
            "schema": self.schema,
            "manifest_id": self.manifest_id,
            "input_root": str(Path(self.input_root).expanduser().resolve(strict=False)),
            "derived_m1_root": str(Path(self.derived_m1_root).expanduser().resolve(strict=False)),
            "atlas_output_root": str(Path(self.atlas_output_root).expanduser().resolve(strict=False)),
            "logical_id": self.logical_id,
            "source_id": self.source_id,
            "resolved_symbol": self.resolved_symbol,
            "contract_id": self.contract_id,
            "series_kind": self.series_kind,
            "source_symbol": self.source_symbol,
            "adjustment_method": self.adjustment_method,
            "session_timezone": self.session_timezone,
            "expected_session_start": self.expected_session_start,
            "expected_session_end": self.expected_session_end,
            "batch_size": self.batch_size,
            "causal_overrides": self.causal_overrides,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, data: Any) -> TickAdapterManifest:
        if not isinstance(data, dict):
            raise TickManifestError(f"manifesto deve ser um objeto JSON (recebido: {data!r})")
        extra = set(data) - _ALL_FIELDS
        if extra:
            raise TickManifestError(f"campo(s) desconhecido(s) no manifesto: {sorted(extra)}")
        missing = _REQUIRED_FIELDS - set(data)
        if missing:
            raise TickManifestError(f"campo(s) ausente(s) no manifesto: {sorted(missing)}")

        causal_overrides: dict[str, Any] = {}
        for key in _CAUSAL_OPTIONAL_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if key in _CAUSAL_LIST_FIELDS:
                if not isinstance(value, list):
                    raise TickManifestError(f"{key} deve ser uma lista")
                value = tuple(value)
            causal_overrides[key] = value

        kwargs: dict[str, Any] = {name: data[name] for name in _REQUIRED_FIELDS}
        if "batch_size" in data:
            kwargs["batch_size"] = data["batch_size"]
        return cls(**kwargs, causal_overrides=causal_overrides)


def load_tick_adapter_manifest_file(path: Path) -> TickAdapterManifest:
    """Lê e valida estritamente o manifesto do adaptador de ticks (JSON) do disco."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TickManifestError(f"não foi possível ler o manifesto: {path}: {exc}") from exc
    data = _strict_json_loads(text, label=str(path))
    return TickAdapterManifest.from_dict(data)


# --------------------------------------------------------------------------
# Validação de identidade dos metadados brutos
# --------------------------------------------------------------------------


def _validate_tick_metadata(
    metadata: dict[str, str], *, path: Path, session_date: date, manifest: TickAdapterManifest
) -> None:
    """Valida os metadados de um `ticks.parquet` contra a identidade declarada.

    Campos sempre exigidos (falham fechado se ausentes/divergentes):
    `schema`/`schema_version`/`source_id`/`logical_id`/`resolved_symbol`/
    `session_date` — via `quant_mvp.validate_raw_tick_metadata`, a mesma
    disciplina do MVP WIN. `series_kind`/`source_symbol`/`adjustment_method`/
    `contract_id` são checados só "quando disponíveis" nos metadados
    (`series_series_kind`/`series_source_symbol`/`series_adjustment_method`/
    `series_contract_id`): nenhum produtor real grava esses campos hoje (ver
    `market_analytics.tick_backfill`), então exigi-los sempre rejeitaria toda
    sessão real; quando um produtor futuro os gravar, uma divergência ainda
    é recusada.
    """

    quant_mvp.validate_raw_tick_metadata(
        metadata,
        path=path,
        session_date=session_date,
        expected_source_id=manifest.source_id,
        expected_logical_id=manifest.logical_id,
        expected_resolved_symbol=manifest.resolved_symbol,
    )
    for metadata_key, manifest_field in _OPTIONAL_IDENTITY_METADATA_KEYS:
        if metadata_key not in metadata:
            continue
        expected = getattr(manifest, manifest_field)
        found = metadata[metadata_key]
        if found != expected:
            raise quant_mvp.SessionRejectedError(
                path,
                f"{metadata_key} divergente da identidade declarada: "
                f"esperado {expected!r}, encontrado {found!r}",
            )


# --------------------------------------------------------------------------
# Inventário/estado persistido por sessão
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _FileInfo:
    size_bytes: int
    sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _stat_and_hash_file(path: Path) -> _FileInfo:
    size_bytes = path.stat().st_size
    return _FileInfo(size_bytes=size_bytes, sha256=_sha256_file(path))


@dataclass(frozen=True)
class _PublishedState:
    sessions: dict[str, dict[str, Any]]
    generation_id: str | None
    state_path: Path | None
    atlas_manifest_path: Path | None
    manifest_sha256: str | None


def _load_state(
    derived_root: Path,
    *,
    manifest: TickAdapterManifest,
    input_root: Path,
) -> _PublishedState:
    current_path = derived_root / "current.json"
    if not current_path.is_file():
        return _PublishedState({}, None, None, None, None)
    try:
        current = _strict_json_loads(current_path.read_text(encoding="utf-8"), label=str(current_path))
    except OSError as exc:
        raise TickAdapterError(f"current.json ilegível em {current_path}: {exc}") from exc
    if not isinstance(current, dict) or current.get("schema") != CURRENT_SCHEMA:
        raise TickAdapterError(f"current.json inválido em {current_path}: schema inesperado")
    generation_id = current.get("current_generation")
    if (
        not isinstance(generation_id, str)
        or not generation_id
        or Path(generation_id).name != generation_id
        or not re.fullmatch(r"[A-Za-z0-9_-]+", generation_id)
    ):
        raise TickAdapterError(f"current.json inválido em {current_path}: geração inválida")
    generation_root = derived_root / "generations" / generation_id
    state_path = generation_root / "state.json"
    atlas_manifest_path = generation_root / "atlas_materialization_manifest.json"
    try:
        data = _strict_json_loads(state_path.read_text(encoding="utf-8"), label=str(state_path))
    except OSError as exc:
        raise TickAdapterError(f"state.json ilegível em {state_path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        raise TickAdapterError(f"state.json inválido em {state_path}: schema inesperado")
    if data.get("generation_id") != generation_id:
        raise TickAdapterError(f"state.json inválido em {state_path}: generation_id divergente")
    manifest_sha256 = data.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or not _HEX64_RE.fullmatch(manifest_sha256):
        raise TickAdapterError(f"state.json inválido em {state_path}: manifest_sha256 inválido")
    if data.get("logical_id") != manifest.logical_id:
        raise TickAdapterError(f"state.json inválido em {state_path}: logical_id divergente")
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        raise TickAdapterError(f"state.json inválido em {state_path}: sessions ausente")
    previous_date: date | None = None
    objects_root = (derived_root / "objects").resolve(strict=False)
    for key, item in sessions.items():
        try:
            parsed_date = date.fromisoformat(key)
        except (TypeError, ValueError) as exc:
            raise TickAdapterError(f"state.json inválido: session_date {key!r}") from exc
        if previous_date is not None and parsed_date <= previous_date:
            raise TickAdapterError("state.json inválido: sessões fora de ordem")
        previous_date = parsed_date
        if not isinstance(item, dict) or set(item) != _STATE_SESSION_FIELDS:
            raise TickAdapterError(f"state.json inválido: registro da sessão {key} fora do schema")
        for hash_field in ("tick_sha256", "m1_sha256"):
            value = item.get(hash_field)
            if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
                raise TickAdapterError(f"state.json inválido: {hash_field} da sessão {key}")
        for size_field in ("tick_size_bytes", "m1_size_bytes"):
            value = item.get(size_field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise TickAdapterError(f"state.json inválido: {size_field} da sessão {key}")
        tick_path = Path(item.get("tick_path", "")).resolve(strict=False)
        m1_path = Path(item.get("m1_path", "")).resolve(strict=False)
        try:
            tick_path.relative_to(input_root)
            m1_path.relative_to(objects_root)
        except ValueError as exc:
            raise TickAdapterError(f"state.json inválido: caminho fora das raízes na sessão {key}") from exc
        if m1_path.name != f"{item['m1_sha256']}.parquet":
            raise TickAdapterError(f"state.json inválido: nome do objeto M1 divergente na sessão {key}")
    if not atlas_manifest_path.is_file():
        raise TickAdapterError(f"manifesto Atlas ausente na geração {generation_id}: {atlas_manifest_path}")
    return _PublishedState(
        sessions, generation_id, state_path, atlas_manifest_path, manifest_sha256
    )


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


def _new_generation_id(now: Callable[[], datetime]) -> str:
    return f"{now().strftime('%Y%m%dT%H%M%S%f')}_{uuid.uuid4().hex[:8]}"


def _promote_generation(
    derived_root: Path,
    *,
    generation_id: str,
    state_payload: dict[str, Any],
    atlas_payload: dict[str, Any],
    now: Callable[[], datetime],
) -> tuple[Path, Path]:
    generations_root = derived_root / "generations"
    generations_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=".gen_", dir=str(generations_root)))
    final_root = generations_root / generation_id
    try:
        state_path = temp_root / "state.json"
        atlas_manifest_path = temp_root / "atlas_materialization_manifest.json"
        _atomic_write_json(state_path, state_payload)
        _atomic_write_json(atlas_manifest_path, atlas_payload)
        os.replace(str(temp_root), str(final_root))
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise

    # Único ponto de commit: até esta troca, consumidores continuam vendo a
    # geração anterior integralmente. Uma falha deixa apenas uma geração
    # órfã, nunca estado/manifesto misturados.
    _atomic_write_json(
        derived_root / "current.json",
        {
            "schema": CURRENT_SCHEMA,
            "current_generation": generation_id,
            "promoted_at_utc": now().isoformat(),
        },
    )
    return final_root / "state.json", final_root / "atlas_materialization_manifest.json"


def _write_session_m1_parquet(
    objects_dir: Path,
    *,
    bars: Sequence[Bar],
    manifest: TickAdapterManifest,
    session_date: date,
    source: quant_mvp.InspectedFile,
) -> tuple[Path, str, int]:
    """Grava o M1 determinístico de uma sessão: escrita em temporário no
    mesmo diretório, `fsync` e só então `os.replace` — nunca substitui um M1
    já publicado por um arquivo parcialmente escrito. Nenhum campo de
    horário de execução é embutido: duas execuções sobre o mesmo tick de
    origem produzem o mesmo Parquet, byte a byte.
    """

    metadata = {
        "schema": M1_SEGMENT_SCHEMA,
        "schema_version": str(M1_SEGMENT_SCHEMA_VERSION),
        "producer_version": PRODUCER_VERSION,
        "source_id": manifest.source_id,
        "logical_id": manifest.logical_id,
        "resolved_symbol": manifest.resolved_symbol,
        "contract_id": manifest.contract_id,
        "series_kind": manifest.series_kind,
        "source_symbol": manifest.source_symbol,
        "adjustment_method": manifest.adjustment_method,
        "session_date": session_date.isoformat(),
        "session_timezone": manifest.session_timezone,
        "timestamp_policy": "session_wall_clock_relabelled_utc",
        "source_ticks_path": str(source.path),
        "source_ticks_sha256": source.sha256,
        "source_ticks_size_bytes": str(source.size_bytes),
        "source_ticks_row_count": str(source.row_count),
    }
    encoded_metadata = {key.encode("utf-8"): value.encode("utf-8") for key, value in metadata.items()}
    session_zone = ZoneInfo(manifest.session_timezone)
    wall_clock_timestamps = [
        bar.timestamp.astimezone(session_zone).replace(tzinfo=UTC) for bar in bars
    ]
    if any(timestamp.date() != session_date for timestamp in wall_clock_timestamps):
        raise TickAdapterError(
            f"sessão {session_date.isoformat()} contém barra fora da data local declarada"
        )
    columns: dict[str, Any] = {
        "source_id": [bar.source_id for bar in bars],
        "symbol": [bar.symbol for bar in bars],
        "timeframe": [bar.timeframe for bar in bars],
        # O Atlas trabalha com o relógio de sessão sem conversão. Ticks
        # brutos carregam instante UTC real; convertemos para o relógio da
        # sessão e recolocamos UTC apenas como marcador tipado, exatamente
        # como a série contínua vinda do MT5.
        "timestamp_utc": pa.array(wall_clock_timestamps, type=pa.timestamp("us", tz="UTC")),
        "open": [bar.open for bar in bars],
        "high": [bar.high for bar in bars],
        "low": [bar.low for bar in bars],
        "close": [bar.close for bar in bars],
        "volume": [bar.volume for bar in bars],
        "volume_quality": [bar.volume_quality for bar in bars],
        "contract_id": [manifest.contract_id] * len(bars),
        "roll_session": [False] * len(bars),
    }
    table = pa.table(columns).replace_schema_metadata(encoded_metadata)

    objects_dir.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".m1_", suffix=".parquet.tmp", dir=str(objects_dir))
        os.close(fd)
        temp_path = Path(temp_name)
        pq.write_table(table, str(temp_path), compression="zstd")
        with open(temp_path, "r+b") as handle:
            os.fsync(handle.fileno())
        digest = _sha256_file(temp_path)
        target = objects_dir / f"{digest}.parquet"
        if target.exists():
            if _sha256_file(target) != digest:
                raise TickAdapterError(f"objeto M1 existente corrompido: {target}")
            temp_path.unlink()
        else:
            os.replace(str(temp_path), str(target))
        return target, digest, target.stat().st_size
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _validate_existing_m1(
    path: Path,
    *,
    session_date: str,
    manifest: TickAdapterManifest,
    expected_tick_sha256: str,
) -> None:
    try:
        parquet_file = pq.ParquetFile(path)
    except (OSError, pa.ArrowException) as exc:
        raise TickAdapterError(f"objeto M1 ilegível para {session_date}: {path}: {exc}") from exc
    raw_metadata = parquet_file.schema_arrow.metadata or {}
    metadata = {key.decode("utf-8"): value.decode("utf-8") for key, value in raw_metadata.items()}
    expected = {
        "schema": M1_SEGMENT_SCHEMA,
        "schema_version": str(M1_SEGMENT_SCHEMA_VERSION),
        "source_id": manifest.source_id,
        "logical_id": manifest.logical_id,
        "resolved_symbol": manifest.resolved_symbol,
        "contract_id": manifest.contract_id,
        "series_kind": manifest.series_kind,
        "source_symbol": manifest.source_symbol,
        "adjustment_method": manifest.adjustment_method,
        "session_date": session_date,
        "session_timezone": manifest.session_timezone,
        "timestamp_policy": "session_wall_clock_relabelled_utc",
        "source_ticks_sha256": expected_tick_sha256,
    }
    divergent = {
        key: (value, metadata.get(key))
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if divergent:
        raise TickAdapterError(f"objeto M1 com metadados divergentes para {session_date}: {divergent}")
    if parquet_file.metadata.num_rows <= 0:
        raise TickAdapterError(f"objeto M1 vazio para {session_date}: {path}")


def _dataset_to_manifest_dict(dataset: atlas_incremental.DatasetSpec) -> dict[str, Any]:
    return {
        "logical_id": dataset.logical_id,
        "symbol": dataset.symbol,
        "series_kind": dataset.series_kind,
        "source_symbol": dataset.source_symbol,
        "adjustment_method": dataset.adjustment_method,
        "expected_session_start": dataset.expected_session_start,
        "expected_session_end": dataset.expected_session_end,
        "input_kind": dataset.input_kind,
        "contract_id": dataset.contract_id,
        "checkpoint_minutes": list(dataset.checkpoint_minutes),
        "pct_event_thresholds": list(dataset.pct_event_thresholds),
        "atr_event_multiples": list(dataset.atr_event_multiples),
        "opening_range_minutes": list(dataset.opening_range_minutes),
        "atr_percentile_windows": list(dataset.atr_percentile_windows),
        "event_outcome_horizons_minutes": list(dataset.event_outcome_horizons_minutes),
        "range_expansion_percentile": dataset.range_expansion_percentile,
        "atr_lookback_sessions": dataset.atr_lookback_sessions,
        "coverage_tolerance_minutes": dataset.coverage_tolerance_minutes,
        "min_coverage_ratio": dataset.min_coverage_ratio,
        "segments": [segment.to_dict() for segment in dataset.segments],
    }


def _build_atlas_payload(
    manifest: TickAdapterManifest,
    sessions: dict[str, dict[str, Any]],
    *,
    atlas_output_root: Path,
) -> dict[str, Any]:
    segments = tuple(
        atlas_incremental.SegmentSpec(
            segment_id=f"session_{key.replace('-', '')}",
            path=sessions[key]["m1_path"],
            source_id=manifest.source_id,
            allowed_start_date=date.fromisoformat(key),
            allowed_end_date=date.fromisoformat(key),
            expected_sha256=sessions[key]["m1_sha256"],
        )
        for key in sorted(sessions)
    )
    dataset = manifest.to_dataset_spec(segments=segments)
    payload = {
        "schema": atlas_incremental.MATERIALIZATION_MANIFEST_SCHEMA,
        "manifest_id": f"{manifest.logical_id}_tick_adapter",
        "output_root": str(atlas_output_root),
        "datasets": [_dataset_to_manifest_dict(dataset)],
    }
    atlas_incremental.MaterializationManifest.from_dict(payload)
    return payload


def _handle_session_failure(
    key: str, previous_sessions: dict[str, Any], sessions_rejected: list[dict[str, str]], reason: str
) -> None:
    if key in previous_sessions:
        raise TickRegressionError(
            f"sessão {key} já publicada anteriormente passou a ser rejeitada agora: {reason}"
        )
    sessions_rejected.append({"session_date": key, "reason": reason})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def assert_output_outside_repo(output_root: Path) -> Path:
    """Recusa uma raiz de saída que coincida com o repositório ou o contenha."""

    resolved = Path(output_root).expanduser().resolve(strict=False)
    if resolved.parent == resolved:
        raise TickAdapterError(f"caminho não pode ser a raiz de um volume: {resolved}")
    repo_root = _repo_root()
    if _paths_overlap(resolved, repo_root):
        raise TickAdapterError(
            f"caminho não pode coincidir com o repositório nem estar contido nele: {resolved}"
        )
    return resolved


def _resolve_roots(manifest: TickAdapterManifest) -> tuple[Path, Path, Path]:
    input_root = Path(manifest.input_root).expanduser().resolve(strict=False)
    derived_parent = assert_output_outside_repo(Path(manifest.derived_m1_root))
    atlas_output_root = assert_output_outside_repo(Path(manifest.atlas_output_root))
    if _paths_overlap(input_root, derived_parent) or _paths_overlap(input_root, atlas_output_root):
        raise TickAdapterError("input_root não pode se sobrepor a derived_m1_root nem a atlas_output_root")
    if _paths_overlap(derived_parent, atlas_output_root):
        raise TickAdapterError("derived_m1_root não pode se sobrepor a atlas_output_root")
    if not input_root.is_dir():
        raise TickAdapterError(f"input_root não existe ou não é diretório: {input_root}")
    return input_root, derived_parent, atlas_output_root


def _run_tick_atlas_adapter_unlocked(
    *,
    manifest: TickAdapterManifest,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Ponto de entrada único: descobre, inventaria, constrói M1 por sessão e
    gera o manifesto de materialização do Atlas.

    Incremental por sessão (não por série causal inteira, ao contrário de
    `atlas_incremental`): cada sessão é hasheada/reconstruída de forma
    independente, então uma correção histórica numa sessão nunca exige
    reler as demais. `no_change` (nenhuma sessão nova/alterada) nunca
    reescreve `state.json` nem o manifesto do Atlas — ambos só são
    regravados quando pelo menos uma sessão muda, e sempre nesta ordem:
    primeiro os M1 tocados, depois o manifesto do Atlas, e só por último
    `state.json` (o commit atômico do run) — uma interrupção antes dele
    nunca deixa `state.json`/manifesto anterior inconsistentes; a próxima
    execução simplesmente refaz o trabalho pendente.
    """

    input_root, derived_parent, atlas_output_root = _resolve_roots(manifest)

    derived_root = derived_parent / manifest.logical_id
    discovered = quant_mvp.discover_sessions(input_root)
    cutoff = now().date()
    eligible = [(session_date, path) for session_date, path in discovered if session_date < cutoff]
    excluded_current = [session_date.isoformat() for session_date, _path in discovered if session_date >= cutoff]

    published_state = _load_state(
        derived_root,
        manifest=manifest,
        input_root=input_root,
    )
    previous_sessions = published_state.sessions
    manifest_sha256 = manifest.fingerprint()
    manifest_changed = (
        published_state.manifest_sha256 is not None
        and published_state.manifest_sha256 != manifest_sha256
    )

    discovered_dates = {session_date.isoformat() for session_date, _path in discovered}
    missing = sorted(set(previous_sessions) - discovered_dates)
    if missing:
        raise TickRegressionError(
            f"sessão(ões) previamente publicada(s) desapareceu(ram) da origem: {missing}"
        )

    new_sessions: dict[str, Any] = dict(previous_sessions)
    sessions_added: list[str] = []
    sessions_recalculated: list[str] = []
    sessions_rejected: list[dict[str, str]] = []

    def _validator(metadata: dict[str, str], *, path: Path, session_date: date) -> None:
        _validate_tick_metadata(metadata, path=path, session_date=session_date, manifest=manifest)

    for session_date, path in eligible:
        key = session_date.isoformat()
        try:
            file_info = _stat_and_hash_file(path)
        except OSError as exc:
            _handle_session_failure(key, previous_sessions, sessions_rejected, f"falha ao ler arquivo: {exc}")
            continue

        prior = previous_sessions.get(key)
        if (
            prior is not None
            and not manifest_changed
            and prior.get("tick_sha256") == file_info.sha256
            and prior.get("tick_size_bytes") == file_info.size_bytes
            and prior.get("tick_path") == str(path)
        ):
            m1_path = Path(prior["m1_path"])
            if m1_path.is_file() and _sha256_file(m1_path) == prior.get("m1_sha256"):
                _validate_existing_m1(
                    m1_path,
                    session_date=key,
                    manifest=manifest,
                    expected_tick_sha256=file_info.sha256,
                )
                continue  # no_change verdadeiro: tick e M1 publicado íntegros e inalterados.
            # M1 publicado ausente/corrompido apesar do tick inalterado: reconstrói abaixo.

        try:
            result = quant_mvp.read_session_ticks_to_m1(
                path,
                session_date=session_date,
                source_id=manifest.source_id,
                symbol=manifest.resolved_symbol,
                batch_size=manifest.batch_size,
                validate_metadata=_validator,
            )
        except quant_mvp.SessionRejectedError as exc:
            _handle_session_failure(key, previous_sessions, sessions_rejected, exc.reason)
            continue

        if not result.m1_bars:
            _handle_session_failure(
                key, previous_sessions, sessions_rejected, "sessão sem nenhum tick operacional válido"
            )
            continue

        if dry_run:
            m1_path: Path | None = None
            m1_sha256: str | None = None
            m1_size_bytes: int | None = None
        else:
            m1_path, m1_sha256, m1_size_bytes = _write_session_m1_parquet(
                derived_root / "objects",
                bars=result.m1_bars,
                manifest=manifest,
                session_date=session_date,
                source=result.source,
            )

        is_new = key not in previous_sessions
        (sessions_added if is_new else sessions_recalculated).append(key)
        new_sessions[key] = {
            "tick_path": str(path),
            "tick_sha256": file_info.sha256,
            "tick_size_bytes": file_info.size_bytes,
            "m1_path": str(m1_path) if m1_path is not None else None,
            "m1_sha256": m1_sha256,
            "m1_size_bytes": m1_size_bytes,
        }

    if sessions_recalculated:
        status = "historical_correction"
    elif sessions_added:
        status = "append"
    else:
        status = "no_change"

    report: dict[str, Any] = {
        "schema": RUN_REPORT_SCHEMA,
        "manifest_id": manifest.manifest_id,
        "logical_id": manifest.logical_id,
        "status": status,
        "dry_run": dry_run,
        "sessions_added": sessions_added,
        "sessions_recalculated": sessions_recalculated,
        "sessions_rejected": sessions_rejected,
        "sessions_excluded_current": excluded_current,
        "total_sessions": len(new_sessions),
        "derived_m1_root": str(derived_root),
        "state_path": str(published_state.state_path) if published_state.state_path else None,
        "atlas_manifest_path": (
            str(published_state.atlas_manifest_path) if published_state.atlas_manifest_path else None
        ),
        "previous_generation": published_state.generation_id,
        "new_generation": published_state.generation_id,
        "manifest_sha256": manifest_sha256,
    }

    if dry_run:
        return report

    if not new_sessions:
        return report
    atlas_payload = _build_atlas_payload(
        manifest, new_sessions, atlas_output_root=atlas_output_root
    )
    if status == "no_change":
        atlas_manifest_path = published_state.atlas_manifest_path
        if atlas_manifest_path is None:
            raise TickAdapterError("estado publicado sem manifesto Atlas")
        try:
            published = _strict_json_loads(
                atlas_manifest_path.read_text(encoding="utf-8"),
                label=str(atlas_manifest_path),
            )
        except OSError as exc:
            raise TickAdapterError(f"manifesto Atlas ilegível: {atlas_manifest_path}: {exc}") from exc
        if published != atlas_payload:
            raise TickAdapterError(
                "manifesto Atlas publicado diverge do estado vigente; execução bloqueada por segurança"
            )
        return report

    generation_id = _new_generation_id(now)
    state_payload = {
        "schema": STATE_SCHEMA,
        "generation_id": generation_id,
        "logical_id": manifest.logical_id,
        "manifest_sha256": manifest_sha256,
        "updated_at_utc": now().isoformat(),
        "sessions": new_sessions,
    }
    state_path, atlas_manifest_path = _promote_generation(
        derived_root,
        generation_id=generation_id,
        state_payload=state_payload,
        atlas_payload=atlas_payload,
        now=now,
    )
    report["state_path"] = str(state_path)
    report["atlas_manifest_path"] = str(atlas_manifest_path)
    report["new_generation"] = generation_id

    return report


def run_tick_atlas_adapter(
    *,
    manifest: TickAdapterManifest,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Executa o adaptador com exclusão mútua por série em runs reais.

    `dry_run` permanece rigorosamente sem escrita e por isso não cria lock.
    A implementação interna repete o preflight sob o lock antes de ler o
    estado, evitando dois escritores concorrentes sobre a mesma série.
    """

    _input_root, derived_parent, _atlas_output_root = _resolve_roots(manifest)
    if dry_run:
        return _run_tick_atlas_adapter_unlocked(manifest=manifest, dry_run=True, now=now)
    try:
        with atlas_incremental.DatasetLock(derived_parent / manifest.logical_id):
            return _run_tick_atlas_adapter_unlocked(manifest=manifest, dry_run=False, now=now)
    except atlas_incremental.MaterializationError as exc:
        raise TickAdapterError(str(exc)) from exc


__all__ = [
    "FIXED_ADJUSTMENT_METHOD",
    "FIXED_SERIES_KIND",
    "MANIFEST_SCHEMA",
    "M1_SEGMENT_SCHEMA",
    "M1_SEGMENT_SCHEMA_VERSION",
    "RUN_REPORT_SCHEMA",
    "STATE_SCHEMA",
    "TickAdapterError",
    "TickAdapterManifest",
    "TickManifestError",
    "TickRegressionError",
    "assert_output_outside_repo",
    "load_tick_adapter_manifest_file",
    "run_tick_atlas_adapter",
]
