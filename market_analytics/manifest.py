"""Manifesto puro e estrito de backfill genérico por fonte/ativo (DEV-004 — C3.1).

Este módulo não importa `MetaTrader5`, `tkinter`, `PySide6`/Qt nem toca disco
fora de `load_manifest_file`/`save_manifest_file` (JSON puro). Ele define o
único contrato que autoriza um lote de backfill: fonte, ativos, proveniência,
intervalo, ordem, limites e — acima de tudo — o estado de autorização. Nenhum
outro módulo deste pacote (`backfill_plan.py`, `backfill_adapter.py`) aceita
símbolo, fonte ou data livre; todos partilham este contrato.

A validação é estrita por design: campo desconhecido, `logical_id` duplicado,
timezone inválida, intervalo invertido, `chunk_seconds` fora da faixa já
auditada em `tick_diagnostics`, `concurrency != 1` ou limite não positivo são
sempre recusados com `ManifestValidationError`, nunca silenciosamente
corrigidos. O esquema fechado (`_MANIFEST_FIELDS`/`_ASSET_FIELDS`) é também a
defesa contra credenciais/conta/login/dados pessoais: esses campos
simplesmente não existem no contrato, então `from_dict` os rejeita como
"campo desconhecido" antes de qualquer outra validação.

`BackfillManifest.fingerprint()` gera um SHA-256 determinístico do JSON
canônico (`sort_keys=True`) do manifesto — a mesma convenção usada por
`BackfillSessionRequest.fingerprint()` em `tick_backfill.py`. Planos e
relatórios subsequentes (`backfill_plan.py`) carregam esse fingerprint para
provar exatamente qual manifesto autorizou (ou não) cada item da fila.

`session_close_local_time` (HH:MM ou HH:MM:SS, 24h) é o horário local -- na
`session_timezone` do próprio manifesto -- em que a sessão do dia fecha. É
esse campo, e não uma constante UTC fixa, que `backfill_plan.
latest_closed_session_date` usa para decidir se o próprio dia útil corrente
já pode entrar na política `latest_closed` (auditoria Codex do DEV-004).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .tick_diagnostics import MAX_CHUNK_SECONDS, MIN_CHUNK_SECONDS

MANIFEST_SCHEMA = "ep_market_hub.backfill_manifest"
MANIFEST_SCHEMA_VERSION = 1

# Único vocabulário de autorização. `planning` e `preflight_approved` nunca
# habilitam execução real — só `execution_approved` faz
# `BackfillManifest.is_execution_authorized` devolver `True` (ver
# `docs/work_orders/DEV-004.md`, fronteira 1 e 4).
AUTHORIZATION_STATES: frozenset[str] = frozenset({"planning", "preflight_approved", "execution_approved"})
EXECUTION_ORDERS: frozenset[str] = frozenset({"newest_first", "oldest_first"})
END_DATE_POLICIES: frozenset[str] = frozenset({"explicit", "latest_closed"})

# Mesma disciplina de `market_analytics.tick_backfill._require_slug`: começa
# por letra minúscula (nunca um identificador puramente numérico, reduzindo o
# risco de um número de conta vazar para um identificador), com teto de
# tamanho. `manifest_id` aceita também `.` para nomes versionáveis
# (`c3-win-clear`), mas continua com o mesmo espírito restrito.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MANIFEST_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")

# HH:MM ou HH:MM:SS, 24h. Único formato aceito para `session_close_local_time`
# -- ver `_require_close_time`.
_CLOSE_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")

_ASSET_FIELDS = frozenset({"logical_id", "requested_symbol", "provenance"})
_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "manifest_id",
        "display_name",
        "work_order",
        "source_id",
        "session_timezone",
        "session_close_local_time",
        "assets",
        "start_date",
        "end_date_policy",
        "end_date",
        "execution_order",
        "chunk_seconds",
        "max_attempts",
        "concurrency",
        "max_total_bytes",
        "max_total_duration_seconds",
        "authorization_state",
    }
)
# `end_date` só é exigido quando `end_date_policy == "explicit"` — ver
# `BackfillManifest.from_dict`.
_MANIFEST_REQUIRED_FIELDS = _MANIFEST_FIELDS - {"end_date"}


class ManifestValidationError(ValueError):
    """Manifesto recusado: campo desconhecido/ausente, duplicado ou fora do contrato."""


def _require_slug(value: Any, field_name: str, *, pattern: re.Pattern[str] = _SLUG_RE) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field_name} deve ser uma string (recebido: {value!r})")
    text = value.strip()
    if not text or not pattern.match(text):
        raise ManifestValidationError(
            f"{field_name} inválido: deve começar com letra minúscula e conter só letras, "
            f"dígitos, '_'/'-' (recebido: {value!r})"
        )
    return text


def _require_nonempty_str(value: Any, field_name: str, *, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{field_name} não pode ser vazio")
    text = value.strip()
    if len(text) > max_length:
        raise ManifestValidationError(f"{field_name} excede o tamanho máximo de {max_length} caracteres")
    return text


def _require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"{field_name} deve ser um inteiro (recebido: {value!r})")
    return value


def _require_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field_name} deve ser uma string ISO (recebido: {value!r})")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ManifestValidationError(f"{field_name} inválida: {value!r}") from exc


def _require_close_time(value: Any, field_name: str) -> time:
    """Horário local de fechamento da sessão: só `HH:MM` ou `HH:MM:SS`, 24h.

    Usado por `latest_closed` (`market_analytics.backfill_plan`) para saber a
    partir de que instante local o próprio dia útil corrente já conta como
    fechado -- nunca inferido de `history_discovery` nem de uma constante UTC
    fixa (DEV-004, auditoria Codex).
    """

    if not isinstance(value, str) or not _CLOSE_TIME_RE.match(value):
        raise ManifestValidationError(
            f"{field_name} deve ser um horário HH:MM ou HH:MM:SS, 24h (recebido: {value!r})"
        )
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ManifestValidationError(f"{field_name} inválido: {value!r}") from exc


@dataclass(frozen=True)
class ManifestAsset:
    """Um ativo autorizado: identidade lógica, símbolo solicitado e proveniência.

    `provenance` é sempre explícita (nunca vazia) — normalmente as mesmas
    chaves de `FuturesSeriesSpec.provenance()` (`market_analytics.futures_series`),
    mas este módulo não importa `futures_series`: permanece neutro por
    mercado e só exige que a proveniência exista e seja texto não vazio.
    """

    logical_id: str
    requested_symbol: str
    provenance: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_id", _require_slug(self.logical_id, "logical_id"))
        object.__setattr__(
            self,
            "requested_symbol",
            _require_nonempty_str(self.requested_symbol, "requested_symbol", max_length=32),
        )
        if not isinstance(self.provenance, tuple) or not self.provenance:
            raise ManifestValidationError("provenance do ativo não pode ser vazia")
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in self.provenance:
            if not isinstance(item, tuple | list) or len(item) != 2:
                raise ManifestValidationError("provenance deve conter pares (chave, valor)")
            key, value = item
            if not isinstance(key, str) or not key.strip():
                raise ManifestValidationError(f"chave de provenance inválida: {key!r}")
            key = key.strip()
            if key in seen:
                raise ManifestValidationError(f"chave de provenance duplicada: {key!r}")
            if not isinstance(value, str) or not value.strip():
                raise ManifestValidationError(f"valor de provenance inválido para {key!r}: {value!r}")
            seen.add(key)
            normalized.append((key, value.strip()))
        object.__setattr__(self, "provenance", tuple(sorted(normalized)))

    def provenance_dict(self) -> dict[str, str]:
        return dict(self.provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "requested_symbol": self.requested_symbol,
            "provenance": self.provenance_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ManifestAsset:
        if not isinstance(data, dict):
            raise ManifestValidationError(f"ativo do manifesto deve ser um objeto (recebido: {data!r})")
        extra = set(data) - _ASSET_FIELDS
        if extra:
            raise ManifestValidationError(f"campo(s) desconhecido(s) em ativo do manifesto: {sorted(extra)}")
        missing = _ASSET_FIELDS - set(data)
        if missing:
            raise ManifestValidationError(f"campo(s) ausente(s) em ativo do manifesto: {sorted(missing)}")
        provenance_raw = data["provenance"]
        if not isinstance(provenance_raw, dict):
            raise ManifestValidationError("provenance deve ser um objeto")
        return cls(
            logical_id=data["logical_id"],
            requested_symbol=data["requested_symbol"],
            provenance=tuple(provenance_raw.items()),
        )


@dataclass(frozen=True)
class BackfillManifest:
    """Contrato versionado que autoriza (ou não) um lote de backfill genérico.

    Um único `source_id`/terminal por manifesto e `concurrency` sempre igual
    a 1 — uma execução futura pode conter vários ativos, mas nunca mistura
    fontes nem paraleliza jobs dentro do mesmo manifesto (DEV-004, fronteira
    3). Manifestos de fontes distintas são sempre executados separadamente.
    """

    manifest_id: str
    display_name: str
    work_order: str
    source_id: str
    session_timezone: str
    session_close_local_time: time
    assets: tuple[ManifestAsset, ...]
    start_date: date
    end_date_policy: str
    execution_order: str
    chunk_seconds: int
    max_attempts: int
    concurrency: int
    max_total_bytes: int
    max_total_duration_seconds: int
    authorization_state: str
    end_date: date | None = None
    schema: str = MANIFEST_SCHEMA
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != MANIFEST_SCHEMA:
            raise ManifestValidationError(f"schema inesperado: {self.schema!r}")
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestValidationError(f"schema_version não suportada: {self.schema_version!r}")
        object.__setattr__(
            self, "manifest_id", _require_slug(self.manifest_id, "manifest_id", pattern=_MANIFEST_ID_RE)
        )
        object.__setattr__(self, "display_name", _require_nonempty_str(self.display_name, "display_name"))
        object.__setattr__(self, "work_order", _require_nonempty_str(self.work_order, "work_order", max_length=64))
        object.__setattr__(self, "source_id", _require_slug(self.source_id, "source_id"))

        if not isinstance(self.session_timezone, str) or not self.session_timezone.strip():
            raise ManifestValidationError("session_timezone não pode ser vazio")
        try:
            ZoneInfo(self.session_timezone)
        except Exception as exc:
            raise ManifestValidationError(f"session_timezone inválida: {self.session_timezone!r}") from exc

        if not isinstance(self.session_close_local_time, time) or isinstance(
            self.session_close_local_time, datetime
        ):
            raise ManifestValidationError(
                f"session_close_local_time deve ser um datetime.time (recebido: {self.session_close_local_time!r})"
            )
        if self.session_close_local_time.tzinfo is not None:
            raise ManifestValidationError(
                "session_close_local_time deve ser um horário local sem tzinfo "
                "(a timezone vem de session_timezone)"
            )

        if not isinstance(self.assets, tuple) or not self.assets:
            raise ManifestValidationError("assets deve conter ao menos um ativo (lista não vazia)")
        if not all(isinstance(asset, ManifestAsset) for asset in self.assets):
            raise ManifestValidationError("assets deve conter apenas instâncias de ManifestAsset")
        seen_logical_ids: set[str] = set()
        for asset in self.assets:
            if asset.logical_id in seen_logical_ids:
                raise ManifestValidationError(f"logical_id duplicado no manifesto: {asset.logical_id!r}")
            seen_logical_ids.add(asset.logical_id)

        if not isinstance(self.start_date, date) or isinstance(self.start_date, datetime):
            raise ManifestValidationError(f"start_date deve ser um date (recebido: {self.start_date!r})")

        if self.end_date_policy not in END_DATE_POLICIES:
            raise ManifestValidationError(f"end_date_policy inválida: {self.end_date_policy!r}")
        if self.end_date_policy == "explicit":
            if not isinstance(self.end_date, date) or isinstance(self.end_date, datetime):
                raise ManifestValidationError("end_date_policy='explicit' exige end_date (date)")
            if self.end_date < self.start_date:
                raise ManifestValidationError(
                    f"intervalo invertido: end_date ({self.end_date}) anterior a start_date ({self.start_date})"
                )
        elif self.end_date is not None:
            raise ManifestValidationError("end_date_policy='latest_closed' não deve declarar end_date")

        if self.execution_order not in EXECUTION_ORDERS:
            raise ManifestValidationError(f"execution_order inválida: {self.execution_order!r}")

        chunk_seconds = _require_int(self.chunk_seconds, "chunk_seconds")
        if not (MIN_CHUNK_SECONDS <= chunk_seconds <= MAX_CHUNK_SECONDS):
            raise ManifestValidationError(
                f"chunk_seconds deve estar entre {MIN_CHUNK_SECONDS} e {MAX_CHUNK_SECONDS} "
                f"(recebido: {chunk_seconds!r})"
            )

        max_attempts = _require_int(self.max_attempts, "max_attempts")
        if not (1 <= max_attempts <= 10):
            raise ManifestValidationError(f"max_attempts deve estar entre 1 e 10 (recebido: {max_attempts!r})")

        concurrency = _require_int(self.concurrency, "concurrency")
        if concurrency != 1:
            raise ManifestValidationError(
                f"concurrency deve ser exatamente 1 nesta ordem (recebido: {concurrency!r})"
            )

        max_total_bytes = _require_int(self.max_total_bytes, "max_total_bytes")
        if max_total_bytes <= 0:
            raise ManifestValidationError(f"max_total_bytes deve ser um inteiro positivo (recebido: {max_total_bytes!r})")

        max_total_duration_seconds = _require_int(self.max_total_duration_seconds, "max_total_duration_seconds")
        if max_total_duration_seconds <= 0:
            raise ManifestValidationError(
                f"max_total_duration_seconds deve ser um inteiro positivo (recebido: {max_total_duration_seconds!r})"
            )

        if self.authorization_state not in AUTHORIZATION_STATES:
            raise ManifestValidationError(f"authorization_state inválido: {self.authorization_state!r}")

    @property
    def is_execution_authorized(self) -> bool:
        """Único ponto de verdade sobre autorização de execução real.

        `planning` e `preflight_approved` sempre devolvem `False`. Esta
        propriedade pura é a base do bloqueio da GUI (`tools/manifest_backfill_gui.py`)
        e da fronteira de execução futura (`market_analytics/backfill_adapter.py`):
        nenhum dos dois reimplementa a regra, ambos só consultam este valor.
        """

        return self.authorization_state == "execution_approved"

    def fingerprint(self) -> str:
        """SHA-256 determinístico do JSON canônico deste manifesto."""

        text = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "display_name": self.display_name,
            "work_order": self.work_order,
            "source_id": self.source_id,
            "session_timezone": self.session_timezone,
            "session_close_local_time": self.session_close_local_time.isoformat(),
            "assets": [asset.to_dict() for asset in self.assets],
            "start_date": self.start_date.isoformat(),
            "end_date_policy": self.end_date_policy,
            "end_date": self.end_date.isoformat() if self.end_date is not None else None,
            "execution_order": self.execution_order,
            "chunk_seconds": self.chunk_seconds,
            "max_attempts": self.max_attempts,
            "concurrency": self.concurrency,
            "max_total_bytes": self.max_total_bytes,
            "max_total_duration_seconds": self.max_total_duration_seconds,
            "authorization_state": self.authorization_state,
        }

    @classmethod
    def from_dict(cls, data: Any) -> BackfillManifest:
        """Desserialização estrita: campo desconhecido ou ausente é sempre recusado.

        Não há coerção por truthiness em nenhum campo — mesma disciplina de
        `BackfillSessionRequest.from_dict` (`tick_backfill.py`).
        """

        if not isinstance(data, dict):
            raise ManifestValidationError(f"manifesto deve ser um objeto JSON (recebido: {data!r})")
        extra = set(data) - _MANIFEST_FIELDS
        if extra:
            raise ManifestValidationError(f"campo(s) desconhecido(s) no manifesto: {sorted(extra)}")
        missing = _MANIFEST_REQUIRED_FIELDS - set(data)
        if missing:
            raise ManifestValidationError(f"campo(s) ausente(s) no manifesto: {sorted(missing)}")

        def _str(key: str) -> str:
            value = data[key]
            if not isinstance(value, str):
                raise ManifestValidationError(f"{key} deve ser uma string (recebido: {value!r})")
            return value

        assets_raw = data["assets"]
        if not isinstance(assets_raw, list) or not assets_raw:
            raise ManifestValidationError("assets deve ser uma lista não vazia")

        end_date_policy = _str("end_date_policy")
        if end_date_policy == "explicit":
            if "end_date" not in data or data["end_date"] is None:
                raise ManifestValidationError("end_date_policy='explicit' exige end_date")
            end_date = _require_date(data["end_date"], "end_date")
        else:
            if "end_date" in data and data["end_date"] is not None:
                raise ManifestValidationError("end_date_policy='latest_closed' não deve declarar end_date")
            end_date = None

        return cls(
            schema=_str("schema"),
            schema_version=_require_int(data["schema_version"], "schema_version"),
            manifest_id=_str("manifest_id"),
            display_name=_str("display_name"),
            work_order=_str("work_order"),
            source_id=_str("source_id"),
            session_timezone=_str("session_timezone"),
            session_close_local_time=_require_close_time(
                data["session_close_local_time"], "session_close_local_time"
            ),
            assets=tuple(ManifestAsset.from_dict(item) for item in assets_raw),
            start_date=_require_date(data["start_date"], "start_date"),
            end_date_policy=end_date_policy,
            end_date=end_date,
            execution_order=_str("execution_order"),
            chunk_seconds=_require_int(data["chunk_seconds"], "chunk_seconds"),
            max_attempts=_require_int(data["max_attempts"], "max_attempts"),
            concurrency=_require_int(data["concurrency"], "concurrency"),
            max_total_bytes=_require_int(data["max_total_bytes"], "max_total_bytes"),
            max_total_duration_seconds=_require_int(
                data["max_total_duration_seconds"], "max_total_duration_seconds"
            ),
            authorization_state=_str("authorization_state"),
        )


def load_manifest_file(path: Path) -> BackfillManifest:
    """Lê e valida estritamente um manifesto JSON do disco."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestValidationError(f"não foi possível ler o manifesto: {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"manifesto não é um JSON válido: {path}: {exc}") from exc
    return BackfillManifest.from_dict(data)


def save_manifest_file(path: Path, manifest: BackfillManifest) -> None:
    """Grava o manifesto por substituição atômica (nunca deixa um arquivo parcial)."""

    import os
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n"
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
