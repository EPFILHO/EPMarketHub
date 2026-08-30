"""Contratos puros do backfill histórico de ticks (DEV-002 — Portão A).

Este módulo não importa `MetaTrader5`, `pyarrow` nem `sqlite3`: define o
contrato de uma solicitação de backfill de **uma sessão** (um dia civil de
uma fonte/ativo lógico) e a identidade de caminho/catálogo dela. Ele
reaproveita deliberadamente `market_analytics.tick_diagnostics`
(`TickWindow`, `TickWindowAccumulator`, `TickRecord`, limites de
`chunk_seconds`) em vez de duplicar essas regras já validadas.

`market_analytics.backfill_writer` (Parquet/`pyarrow`) e
`market_analytics.backfill_catalog` (`sqlite3`) consomem estes contratos sem
depender do worker nem de MT5.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .tick_diagnostics import (
    CANONICAL_TICK_TYPES,
    DEFAULT_CHUNK_SECONDS,
    MAX_CHUNK_SECONDS,
    MIN_CHUNK_SECONDS,
    TickWindow,
)

# Versão do schema bruto persistido em Parquet (metadados do arquivo, não o
# protocolo do worker). Mudar o schema bruto exige incrementar este valor.
RAW_SCHEMA_VERSION = 1

# Identifica a versão do código coletor nos metadados do arquivo e no
# catálogo, para auditoria — não é o protocolo do worker.
BACKFILL_COLLECTOR_VERSION = "dev-002-gate-a"

# Uma sessão é o dia civil completo nesta timezone, convertido para limites
# UTC timezone-aware. A ingestão bruta não fixa horário de pregão.
SESSION_TIMEZONE = "America/Sao_Paulo"

# Estados possíveis de uma sessão no catálogo (ver docs/work_orders/DEV-002.md).
BACKFILL_SESSION_STATES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "empty", "failed", "interrupted"}
)

# Vocabulário de falha estruturada do backfill. Reaproveita razões já
# existentes em `tick_diagnostics` sempre que o significado é o mesmo, e
# acrescenta somente as específicas de escrita/catálogo/concorrência.
BACKFILL_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "invalid_request",
        "symbol_not_found",
        "terminal_disconnected",
        "malformed_response",
        "mt5_error",
        "disk_error",
        "catalog_error",
        "request_id_conflict",
        "backfill_busy",
        "worker_unavailable",
        "live_stream_active",
        "diagnostic_active",
        "already_completed",
        "interrupted",
        # Correção de auditoria (segunda entrega): um artefato existente no
        # caminho esperado, mas cujos metadados embutidos apontam para outra
        # fonte/ativo/data/fuso, nunca é adotado silenciosamente.
        "identity_mismatch",
    }
)

EMPTY_REASON_NO_TICKS = "no_ticks_returned"

# Começa por letra (nunca por dígito, para não aceitar um identificador
# puramente numérico como um número de conta) e tem um teto de tamanho
# razoável para um segmento de caminho.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _require_slug(value: Any, field_name: str) -> str:
    """Valida um identificador seguro para nome de pasta.

    `source_id`/`logical_id` viram segmentos de caminho no disco. Exigir um
    formato restrito (minúsculo, iniciado por letra, sem espaços/separadores,
    com teto de tamanho) reduz a chance de colisão de identidade entre
    fontes/ativos e de um valor livre (ex.: número de conta, que é só
    dígitos) vazar para o caminho por engano. Isso reduz o risco — não é uma
    prova de ausência de dado pessoal; o chamador continua responsável por
    nunca passar conta/credenciais aqui.
    """

    text = str(value).strip()
    if not text or not _SLUG_RE.match(text):
        raise ValueError(
            f"{field_name} deve começar com uma letra minúscula e conter só letras, "
            f"dígitos, '_' ou '-' (máx. 64 caracteres); recebido: {value!r}"
        )
    return text


@dataclass(frozen=True)
class SessionWindow:
    """Janela UTC `[start_utc, end_utc)` de uma sessão (dia civil).

    Ao contrário de `TickWindow` (que limita deliberadamente 24h como
    salvaguarda genérica de uma janela de diagnóstico), uma sessão de
    backfill é um dia civil real, que pode durar 23h ou 25h numa transição
    de horário de verão. `SessionWindow` aceita essa faixa e sempre
    subdivide em `TickWindow` de até `chunk_seconds` cada, contíguos e sem
    sobreposição — os próprios `TickWindow` de chunk continuam limitados a
    900s pelo contrato de `tick_diagnostics`.
    """

    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        for value, name in ((self.start_utc, "start_utc"), (self.end_utc, "end_utc")):
            if not isinstance(value, datetime):
                raise ValueError(f"{name} deve ser datetime (recebido: {value!r})")
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(
                    f"{name} deve ser timezone-aware em UTC (utcoffset()==0); recebido {value!r}"
                )
        if self.end_utc <= self.start_utc:
            raise ValueError(f"end_utc ({self.end_utc}) deve ser maior que start_utc ({self.start_utc})")
        duration = (self.end_utc - self.start_utc).total_seconds()
        # Faixa de sanidade generosa em torno de 24h: cobre qualquer
        # transição real de horário de verão (tipicamente ±1h) sem aceitar
        # uma janela absurda por erro de outro lugar do código.
        if not (20 * 3600 <= duration <= 28 * 3600):
            raise ValueError(
                f"duração de sessão fora da faixa esperada de 20h–28h: {duration:.0f}s"
            )

    def chunks(self, chunk_seconds: int) -> list[TickWindow]:
        """Subdivide a sessão inteira em `TickWindow` de até `chunk_seconds`.

        Puramente aritmético sobre os instantes UTC já resolvidos — não
        precisa saber se a sessão teve 23h, 24h ou 25h locais para gerar
        chunks contíguos e sem gap/overlap. `chunk_seconds` é validado
        diretamente aqui (inteiro real, não `bool`, entre `MIN_CHUNK_SECONDS`
        e `MAX_CHUNK_SECONDS`) antes de qualquer aritmética — não depende de
        `TickWindow` para recusar um valor degenerado como `0` ou negativo.
        """

        if isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, int):
            raise ValueError(f"chunk_seconds deve ser um inteiro (recebido: {chunk_seconds!r})")
        if not (MIN_CHUNK_SECONDS <= chunk_seconds <= MAX_CHUNK_SECONDS):
            raise ValueError(
                f"chunk_seconds deve estar entre {MIN_CHUNK_SECONDS} e {MAX_CHUNK_SECONDS} "
                f"(recebido: {chunk_seconds!r})"
            )
        step = timedelta(seconds=chunk_seconds)
        result: list[TickWindow] = []
        cursor = self.start_utc
        while cursor < self.end_utc:
            chunk_end = min(cursor + step, self.end_utc)
            result.append(TickWindow(start_utc=cursor, end_utc=chunk_end))
            cursor = chunk_end
        return result


def session_window_utc(session_date: date, tz_name: str = SESSION_TIMEZONE) -> SessionWindow:
    """Converte o dia civil `session_date` em `tz_name` numa `SessionWindow` UTC.

    A janela é `[meia-noite local, meia-noite local seguinte)`, convertida
    para UTC timezone-aware. Não recorta horário de pregão: a ingestão
    bruta solicita o dia civil inteiro e preserva o que a fonte retornar.
    A duração real pode ser 23h/24h/25h numa transição de horário de verão;
    ver `SessionWindow`.
    """

    zone = ZoneInfo(tz_name)
    local_start = datetime(session_date.year, session_date.month, session_date.day, tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    return SessionWindow(start_utc=local_start.astimezone(UTC), end_utc=local_end.astimezone(UTC))


@dataclass(frozen=True)
class BackfillSessionRequest:
    """Contrato de uma solicitação de backfill de uma única sessão (dia civil).

    A identidade de catálogo/caminho é `(source_id, logical_id,
    session_date)` — nunca a conta ou credenciais do terminal. `source_id`
    é escolhido pelo chamador (ex.: ``"clear"``, ``"fot"``), desacoplado do
    `id` interno do cadastro de terminal.
    """

    request_id: str
    source_id: str
    logical_id: str
    aliases: tuple[str, ...]
    tick_type: str
    session_date: date
    session_timezone: str = SESSION_TIMEZONE
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS
    rebuild: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id não pode ser vazio")
        object.__setattr__(self, "source_id", _require_slug(self.source_id, "source_id"))
        object.__setattr__(self, "logical_id", _require_slug(self.logical_id, "logical_id"))
        if not self.aliases or not all(isinstance(a, str) and a.strip() for a in self.aliases):
            raise ValueError("aliases deve conter ao menos um símbolo não vazio")
        if self.tick_type not in CANONICAL_TICK_TYPES:
            raise ValueError(
                f"tick_type inválido: {self.tick_type!r}. Use um de {sorted(CANONICAL_TICK_TYPES)}."
            )
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise ValueError(f"session_date deve ser um date, não datetime (recebido: {self.session_date!r})")
        if not isinstance(self.session_timezone, str) or not self.session_timezone.strip():
            raise ValueError("session_timezone não pode ser vazio")
        try:
            ZoneInfo(self.session_timezone)
        except Exception as exc:
            raise ValueError(f"session_timezone inválida: {self.session_timezone!r}") from exc
        if (
            isinstance(self.chunk_seconds, bool)
            or not isinstance(self.chunk_seconds, int)
            or not (MIN_CHUNK_SECONDS <= self.chunk_seconds <= MAX_CHUNK_SECONDS)
        ):
            raise ValueError(
                f"chunk_seconds deve ser um inteiro entre {MIN_CHUNK_SECONDS} e "
                f"{MAX_CHUNK_SECONDS} (recebido: {self.chunk_seconds!r})"
            )
        if not isinstance(self.rebuild, bool):
            raise ValueError(f"rebuild deve ser booleano (recebido: {self.rebuild!r})")
        # Valida a conversão de fuso e a duração da sessão agora (aceita
        # 20h–28h, cobrindo transições reais de horário de verão), para
        # recusar cedo uma combinação data/fuso absurda.
        session_window_utc(self.session_date, self.session_timezone)

    def session_window(self) -> SessionWindow:
        return session_window_utc(self.session_date, self.session_timezone)

    def chunks(self) -> list[TickWindow]:
        return self.session_window().chunks(self.chunk_seconds)

    def fingerprint(self) -> str:
        """Impressão digital canônica e determinística da solicitação.

        Mesma convenção de `TickWindowRequest.fingerprint`: identidade
        imutável usada para detectar `request_id` reaproveitado com
        parâmetros diferentes.
        """

        payload = {
            "source_id": self.source_id,
            "logical_id": self.logical_id,
            "aliases": list(self.aliases),
            "tick_type": self.tick_type,
            "session_date": self.session_date.isoformat(),
            "session_timezone": self.session_timezone,
            "chunk_seconds": self.chunk_seconds,
            "rebuild": self.rebuild,
        }
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_id": self.source_id,
            "logical_id": self.logical_id,
            "aliases": list(self.aliases),
            "tick_type": self.tick_type,
            "session_date": self.session_date.isoformat(),
            "session_timezone": self.session_timezone,
            "chunk_seconds": self.chunk_seconds,
            "rebuild": self.rebuild,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackfillSessionRequest:
        """Desserialização estrita: nunca converte tipo por truthiness.

        `rebuild` e `chunk_seconds` precisam chegar com o tipo Python real
        (`bool`/`int`); uma string como `"false"` ou `"900"` é rejeitada como
        `invalid_request` em vez de virar `True`/900 por coerção silenciosa.
        Campos textuais também exigem `str` real — `None`/número não são
        implicitamente convertidos em texto.
        """

        if not isinstance(data, dict):
            raise ValueError(f"request deve ser um objeto (recebido: {data!r})")

        def _str_field(key: str, default: str | None) -> str:
            value = data.get(key, default)
            if not isinstance(value, str):
                raise ValueError(f"{key} deve ser uma string (recebido: {value!r})")
            return value.strip()

        aliases_raw = data.get("aliases", [])
        if not isinstance(aliases_raw, list | tuple):
            raise ValueError(f"aliases deve ser uma lista (recebido: {aliases_raw!r})")
        if not all(isinstance(alias, str) for alias in aliases_raw):
            raise ValueError(f"aliases deve conter apenas strings (recebido: {aliases_raw!r})")

        session_date_text = _str_field("session_date", "")
        try:
            session_date = date.fromisoformat(session_date_text)
        except ValueError as exc:
            raise ValueError(f"session_date inválida: {session_date_text!r}") from exc

        chunk_seconds_raw = data.get("chunk_seconds", DEFAULT_CHUNK_SECONDS)
        if isinstance(chunk_seconds_raw, bool) or not isinstance(chunk_seconds_raw, int):
            raise ValueError(f"chunk_seconds deve ser um inteiro (recebido: {chunk_seconds_raw!r})")

        rebuild_raw = data.get("rebuild", False)
        if not isinstance(rebuild_raw, bool):
            raise ValueError(f"rebuild deve ser booleano (recebido: {rebuild_raw!r})")

        return cls(
            request_id=_str_field("request_id", ""),
            source_id=_str_field("source_id", ""),
            logical_id=_str_field("logical_id", ""),
            aliases=tuple(alias.strip() for alias in aliases_raw if alias.strip()),
            tick_type=_str_field("tick_type", ""),
            session_date=session_date,
            session_timezone=_str_field("session_timezone", SESSION_TIMEZONE) or SESSION_TIMEZONE,
            chunk_seconds=chunk_seconds_raw,
            rebuild=rebuild_raw,
        )


def raw_partition_dir(
    data_root: Path,
    *,
    source_id: str,
    logical_id: str,
    session_date: date,
) -> Path:
    """Pasta de destino do Parquet final, sem tocar o disco.

    Layout fixado por `docs/work_orders/DEV-002.md`:
    ``raw/<source_id>/<logical_id>/year=YYYY/month=MM/session_date=YYYY-MM-DD/``.
    Esta função pública revalida `source_id`/`logical_id` por conta própria —
    nunca confia apenas na validação já feita pelo chamador (ex.:
    `BackfillSessionRequest.__post_init__`).
    """

    source_id = _require_slug(source_id, "source_id")
    logical_id = _require_slug(logical_id, "logical_id")
    return (
        Path(data_root)
        / "raw"
        / source_id
        / logical_id
        / f"year={session_date.year:04d}"
        / f"month={session_date.month:02d}"
        / f"session_date={session_date.isoformat()}"
    )


def catalog_db_path(data_root: Path) -> Path:
    return Path(data_root) / "catalog" / "collection.sqlite3"


def logs_dir(data_root: Path) -> Path:
    return Path(data_root) / "logs"


def build_raw_metadata(
    *,
    request: BackfillSessionRequest,
    resolved_symbol: str,
    collected_at: datetime,
    attempt_id: str,
) -> dict[str, str]:
    """Metadados versionados embutidos no schema do arquivo Parquet.

    Nunca inclui conta, credenciais nem dados pessoais — apenas identidade
    de fonte/ativo/símbolo, timezone da sessão, intervalo solicitado, versão
    do coletor e `attempt_id` (identificador opaco da tentativa dona desta
    escrita — ver `docs/work_orders/DEV-002.md`, correção da terceira
    auditoria, item 1). `collected_at` precisa ser timezone-aware em UTC e
    `resolved_symbol`/`attempt_id` não podem ser vazios: esses metadados são
    a base da identidade do artefato usada pela reconciliação
    (`validate_artifact_identity`).
    """

    if not isinstance(collected_at, datetime):
        raise ValueError(f"collected_at deve ser datetime (recebido: {collected_at!r})")
    if collected_at.tzinfo is None or collected_at.utcoffset() != timedelta(0):
        raise ValueError(
            f"collected_at deve ser timezone-aware em UTC (utcoffset()==0); recebido {collected_at!r}"
        )
    if not isinstance(resolved_symbol, str) or not resolved_symbol.strip():
        raise ValueError(f"resolved_symbol não pode ser vazio (recebido: {resolved_symbol!r})")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError(f"attempt_id não pode ser vazio (recebido: {attempt_id!r})")

    window = request.session_window()
    return {
        "schema": "ep_market_hub.raw_ticks",
        "schema_version": str(RAW_SCHEMA_VERSION),
        "source_id": request.source_id,
        "logical_id": request.logical_id,
        "resolved_symbol": resolved_symbol.strip(),
        "session_date": request.session_date.isoformat(),
        "session_timezone": request.session_timezone,
        "requested_start_utc": window.start_utc.isoformat(),
        "requested_end_utc": window.end_utc.isoformat(),
        "tick_type": request.tick_type,
        "collected_at_utc": collected_at.isoformat(),
        "collector_version": BACKFILL_COLLECTOR_VERSION,
        "attempt_id": attempt_id.strip(),
    }


class ArtifactIdentityError(ValueError):
    """Metadados embutidos de um Parquet final divergem da sessão esperada."""


@dataclass(frozen=True)
class ArtifactIdentity:
    """Fatos do artefato recuperado, lidos do próprio arquivo — nunca
    presumidos nem substituídos silenciosamente pelos valores da chamada
    atual (correção da terceira auditoria, item 6).
    """

    resolved_symbol: str
    collector_version: str
    attempt_id: str
    collected_at_utc: datetime


def validate_artifact_identity(metadata: dict[str, Any], *, request: BackfillSessionRequest) -> ArtifactIdentity:
    """Confere os metadados embutidos de um Parquet final contra a sessão.

    Usado só pela reconciliação (`market_analytics.backfill_runner`): antes
    de adotar um arquivo já presente no caminho esperado como pertencente à
    sessão atual, valida que `schema`, `schema_version`, `source_id`,
    `logical_id`, `session_date`, `session_timezone`, `tick_type` e os
    limites UTC solicitados batem exatamente com a `request`. Um metadado
    ausente, malformado ou divergente nunca é tolerado — evita atribuir
    silenciosamente um arquivo de outra fonte/ativo/data/fuso ao caminho
    atual.

    `collector_version` precisa ser EXATAMENTE `BACKFILL_COLLECTOR_VERSION`:
    este Portão A só reconhece uma única versão compatível de coletor, nunca
    adivinha compatibilidade com uma versão futura/estranha desconhecida.
    `collected_at_utc` precisa ser um `datetime` válido, timezone-aware em
    UTC. `attempt_id` precisa estar presente.

    Retorna um `ArtifactIdentity` com os fatos lidos do próprio arquivo —
    `resolved_symbol`, `collector_version`, `attempt_id` e `collected_at_utc`
    — para que o chamador nunca substitua silenciosamente esses valores
    pelos da tentativa/constantes atuais.
    """

    def _require(key: str) -> str:
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ArtifactIdentityError(f"metadado obrigatório ausente ou vazio no arquivo: {key!r}")
        return value

    schema = _require("schema")
    if schema != "ep_market_hub.raw_ticks":
        raise ArtifactIdentityError(f"schema do arquivo inesperado: {schema!r}")

    schema_version = _require("schema_version")
    if schema_version != str(RAW_SCHEMA_VERSION):
        raise ArtifactIdentityError(
            f"schema_version do arquivo incompatível: esperado {RAW_SCHEMA_VERSION!r}, encontrado {schema_version!r}"
        )

    for key, expected in (
        ("source_id", request.source_id),
        ("logical_id", request.logical_id),
        ("session_date", request.session_date.isoformat()),
        ("session_timezone", request.session_timezone),
        ("tick_type", request.tick_type),
    ):
        found = _require(key)
        if found != expected:
            raise ArtifactIdentityError(
                f"{key} do arquivo divergente da sessão esperada: esperado {expected!r}, encontrado {found!r}"
            )

    window = request.session_window()
    for key, expected in (
        ("requested_start_utc", window.start_utc.isoformat()),
        ("requested_end_utc", window.end_utc.isoformat()),
    ):
        found = _require(key)
        if found != expected:
            raise ArtifactIdentityError(
                f"{key} do arquivo divergente da sessão esperada: esperado {expected!r}, encontrado {found!r}"
            )

    collector_version = _require("collector_version")
    if collector_version != BACKFILL_COLLECTOR_VERSION:
        raise ArtifactIdentityError(
            "collector_version do arquivo não é compatível com este Portão A: esperado "
            f"{BACKFILL_COLLECTOR_VERSION!r}, encontrado {collector_version!r}"
        )

    collected_at_text = _require("collected_at_utc")
    try:
        collected_at = datetime.fromisoformat(collected_at_text)
    except ValueError as exc:
        raise ArtifactIdentityError(f"collected_at_utc do arquivo inválido: {collected_at_text!r}") from exc
    if collected_at.tzinfo is None or collected_at.utcoffset() != timedelta(0):
        raise ArtifactIdentityError(
            f"collected_at_utc do arquivo não é timezone-aware em UTC: {collected_at_text!r}"
        )

    attempt_id = _require("attempt_id")
    resolved_symbol = _require("resolved_symbol")

    return ArtifactIdentity(
        resolved_symbol=resolved_symbol,
        collector_version=collector_version,
        attempt_id=attempt_id,
        collected_at_utc=collected_at,
    )
