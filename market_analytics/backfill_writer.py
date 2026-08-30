"""Escritor Parquet de ticks brutos (DEV-002 — Portão A).

Único módulo deste pacote que depende de `pyarrow`. Escreve os ticks de uma
sessão por chunk — um row group por chunk, nunca o dia inteiro em memória —
em `<final>.partial`. O arquivo final só é criado por `close_and_promote`,
depois de fechar o parcial e validar contagem/schema; a promoção usa
`os.replace`, atômico dentro do mesmo volume. Qualquer falha antes da
promoção nunca altera um arquivo final já existente.

Preserva sem transformação `time`/`time_msc`, `bid`/`ask`/`last`,
`volume`/`volume_real` e `flags` (schema bruto v1 — ver
`market_analytics.tick_backfill`). Não deduplica nem descarta registros.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .tick_diagnostics import (
    TickRecord,
    TickWindowAccumulator,
    TickWindowSummary,
    validate_tick_record,
)

# `pa.ArrowIOError` já é um `OSError` (alias da biblioteca); `pa.ArrowException`
# é a base comum de `ArrowInvalid`/`ArrowNotImplementedError`/etc. Juntas,
# cobrem qualquer falha esperada do PyArrow ao ler OU escrever um arquivo —
# corrompido, truncado, schema incompatível, inacessível, disco cheio — sem
# deixar uma exceção crua do driver escapar do módulo (correção da terceira
# auditoria, item 4: a mesma disciplina vale para `open`/`write_chunk`/
# `close_and_promote`/`abort`, não só para a leitura de reconciliação).
_ARROW_ERRORS: tuple[type[BaseException], ...] = (pa.ArrowException, OSError)

# Ordem e tipos fixos do schema bruto v1. `flags` cabe em int32 (a API MT5
# retorna um pequeno campo de bits), mas usamos int64 uniformemente para não
# arriscar overflow silencioso em uma variação futura de fonte.
RAW_ARROW_FIELDS: tuple[tuple[str, pa.DataType], ...] = (
    ("time", pa.int64()),
    ("time_msc", pa.int64()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("last", pa.float64()),
    ("volume", pa.float64()),
    ("volume_real", pa.float64()),
    ("flags", pa.int64()),
)


class BackfillWriteError(Exception):
    """Falha estruturada de escrita/promoção do Parquet (schema, contagem, disco)."""


@dataclass(frozen=True)
class PromotedFile:
    """Resultado da promoção atômica: caminho final, tamanho, hash e contagem."""

    path: Path
    size_bytes: int
    sha256: str
    row_count: int


@dataclass(frozen=True)
class InspectedFile:
    """Resultado de inspecionar um `ticks.parquet` já existente para reconciliação.

    Ao contrário de `PromotedFile` (resultado de uma escrita concluída
    agora), carrega também os metadados embutidos decodificados — a
    reconciliação precisa deles para confirmar a identidade do artefato
    (`market_analytics.tick_backfill.validate_artifact_identity`) antes de
    confiar em qualquer contagem ou hash.
    """

    path: Path
    size_bytes: int
    sha256: str
    row_count: int
    metadata: dict[str, str]


def build_schema(*, metadata: dict[str, str]) -> pa.Schema:
    """Schema Arrow do schema bruto v1, com os metadados versionados embutidos."""

    fields = [pa.field(name, dtype, nullable=False) for name, dtype in RAW_ARROW_FIELDS]
    encoded_metadata = {str(key).encode("utf-8"): str(value).encode("utf-8") for key, value in metadata.items()}
    return pa.schema(fields, metadata=encoded_metadata)


def records_to_table(records: Sequence[TickRecord], schema: pa.Schema) -> pa.Table:
    """Converte um chunk de `TickRecord` já validados numa `pa.Table`.

    Valida cada registro (NaN/infinito, timestamps incoerentes, flags
    inválidas) antes de montar as colunas: um registro malformado nunca
    chega a ser escrito.
    """

    for record in records:
        validate_tick_record(record)
    columns = {
        "time": [record.time for record in records],
        "time_msc": [record.time_msc for record in records],
        "bid": [record.bid for record in records],
        "ask": [record.ask for record in records],
        "last": [record.last for record in records],
        "volume": [record.volume for record in records],
        "volume_real": [record.volume_real for record in records],
        "flags": [record.flags for record in records],
    }
    return pa.table(columns, schema=schema)


def discard_partial(partial_path: Path) -> None:
    """Remove um `.partial` órfão, se existir. Usado na retomada e no abort.

    Uma falha real ao remover (ex.: arquivo travado por outro processo)
    nunca é engolida: propaga como `BackfillWriteError`, em vez de deixar o
    chamador seguir como se a retomada estivesse limpa quando não está.
    `missing_ok=True` já cobre o caso comum de "arquivo não existe" sem
    levantar nada.
    """

    try:
        Path(partial_path).unlink(missing_ok=True)
    except OSError as exc:
        raise BackfillWriteError(f"Falha ao descartar o parcial órfão {partial_path}: {exc}") from exc


def _fsync_path(path: Path) -> None:
    """Força o SO a persistir o conteúdo de `path` em disco estável.

    O `close()` do `ParquetWriter` libera os buffers do processo, mas não
    garante por si só que o sistema operacional já escreveu os dados no
    armazenamento físico — só `fsync` faz essa garantia.
    """

    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError as exc:
        raise BackfillWriteError(f"Falha ao abrir o parcial para sincronizar: {exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise BackfillWriteError(f"Falha ao sincronizar o parcial em disco: {exc}") from exc
    finally:
        os.close(fd)


def _safe_close(closeable: Any) -> None:
    """Fecha um handle/`ParquetFile` sem deixar uma falha secundária de
    fechamento mascarar a exceção primária que já pode estar propagando —
    usado só em limpeza de leitura, onde o dado relevante já foi extraído
    (ou a leitura já falhou por outro motivo estruturado)."""

    try:
        closeable.close()
    except _ARROW_ERRORS:
        pass


def _file_size(path: Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError as exc:
        raise BackfillWriteError(f"Falha ao obter o tamanho de {path}: {exc}") from exc


def _base_raw_schema() -> pa.Schema:
    """Schema estrutural do bruto v1, sem os metadados de uma solicitação
    específica — usado só para checar compatibilidade na reconciliação."""

    return pa.schema([pa.field(name, dtype, nullable=False) for name, dtype in RAW_ARROW_FIELDS])


def _decode_metadata(raw: dict[bytes, bytes] | None) -> dict[str, str]:
    if not raw:
        return {}
    return {
        key.decode("utf-8", errors="replace"): value.decode("utf-8", errors="replace")
        for key, value in raw.items()
    }


def _open_parquet_file_safely(path: Path) -> tuple[pq.ParquetFile, pa.OSFile]:
    """Abre `path` com posse explícita do handle do SO.

    Ao contrário de `pq.ParquetFile(caminho_como_string)` — que deixa o
    PyArrow abrir e possuir seu próprio handle internamente —, aqui o
    chamador sempre recebe (e é responsável por fechar) o `pa.OSFile`
    subjacente, mesmo que a construção do `ParquetFile` falhe no meio (ex.:
    footer corrompido). Isso evita um handle preso no Windows durante a
    vida do processo quando a leitura falha.
    """

    try:
        handle = pa.OSFile(str(path), mode="r")
    except _ARROW_ERRORS as exc:
        raise BackfillWriteError(f"Falha ao abrir {path} para leitura: {exc}") from exc
    try:
        parquet_file = pq.ParquetFile(handle)
    except _ARROW_ERRORS as exc:
        _safe_close(handle)
        raise BackfillWriteError(f"Arquivo Parquet corrompido ou inválido: {path}: {exc}") from exc
    return parquet_file, handle


def _read_parquet_metadata_safely(path: Path) -> tuple[int, pa.Schema, dict[str, str]]:
    """Lê `(num_rows, schema, metadados_decodificados)` de `path` com segurança.

    Nunca deixa uma exceção crua do PyArrow/SO escapar (sempre normalizada
    em `BackfillWriteError`) nem um handle preso: tanto o `ParquetFile`
    quanto o `pa.OSFile` que o sustenta são fechados explicitamente em
    qualquer caminho, inclusive quando a própria construção falha.
    """

    parquet_file, handle = _open_parquet_file_safely(path)
    try:
        try:
            num_rows = parquet_file.metadata.num_rows
            schema = parquet_file.schema_arrow
        except _ARROW_ERRORS as exc:
            raise BackfillWriteError(f"Falha ao ler metadados do Parquet: {path}: {exc}") from exc
        finally:
            _safe_close(parquet_file)
    finally:
        _safe_close(handle)
    return num_rows, schema, _decode_metadata(schema.metadata)


def inspect_final_file(final_path: Path) -> InspectedFile | None:
    """Lê um `ticks.parquet` já promovido e recalcula hash/contagem/schema/metadados.

    Usado só pela reconciliação arquivo↔catálogo (nunca no caminho comum de
    escrita): confirma que o arquivo é estruturalmente compatível com o
    schema bruto v1 antes de qualquer decisão de catálogo confiar nele, e
    devolve os metadados embutidos para que o chamador confira a identidade
    do artefato (`validate_artifact_identity`) antes de adotá-lo.
    Retorna `None` se o arquivo não existir. Levanta `BackfillWriteError` se
    existir mas estiver corrompido, truncado ou com schema incompatível —
    nesse caso o chamador não deve promover o catálogo a partir dele.
    """

    final_path = Path(final_path)
    if not final_path.exists():
        return None

    num_rows, schema, metadata = _read_parquet_metadata_safely(final_path)
    # check_metadata=False: metadados como `collected_at_utc` variam
    # legitimamente entre coletas; só a compatibilidade estrutural das
    # colunas importa para decidir se o arquivo é estruturalmente confiável.
    # A identidade semântica (source_id/logical_id/session_date/...) é
    # responsabilidade de `validate_artifact_identity`, não desta função.
    if not schema.equals(_base_raw_schema(), check_metadata=False):
        raise BackfillWriteError(f"Schema do arquivo final incompatível com o schema bruto v1: {final_path}")

    digest = _sha256_file(final_path)
    size_bytes = _file_size(final_path)
    return InspectedFile(path=final_path, size_bytes=size_bytes, sha256=digest, row_count=num_rows, metadata=metadata)


def recompute_summary_from_file(
    final_path: Path,
    *,
    request_id: str,
    source_id: str,
    logical_id: str,
    resolved_symbol: str,
    tick_type: str,
    window: Any,
    batch_size: int = 100_000,
) -> TickWindowSummary:
    """Reconstrói o resumo de qualidade inteiramente a partir do arquivo.

    Usado só pela reconciliação: nunca mistura hash/contagem do arquivo com
    um resumo antigo do catálogo — o resumo devolvido é sempre derivado
    exclusivamente do conteúdo atual do arquivo. Lê em batches limitados
    (nunca o arquivo inteiro em memória de uma vez) e alimenta o mesmo
    acumulador incremental usado na escrita original (`TickWindowAccumulator`).

    `window` só precisa expor `start_utc`/`end_utc` (aceita tanto
    `TickWindow` quanto `SessionWindow`).
    """

    accumulator = TickWindowAccumulator()
    parquet_file, handle = _open_parquet_file_safely(final_path)
    try:
        try:
            for batch in parquet_file.iter_batches(batch_size=batch_size):
                columns = batch.to_pydict()
                for index in range(batch.num_rows):
                    record = TickRecord(
                        time=columns["time"][index],
                        time_msc=columns["time_msc"][index],
                        bid=columns["bid"][index],
                        ask=columns["ask"][index],
                        last=columns["last"][index],
                        volume=columns["volume"][index],
                        volume_real=columns["volume_real"][index],
                        flags=columns["flags"][index],
                    )
                    accumulator.consume(record)
        except (*_ARROW_ERRORS, ValueError, TypeError, KeyError) as exc:
            raise BackfillWriteError(
                f"Falha ao reconstruir o resumo a partir do arquivo: {final_path}: {exc}"
            ) from exc
        finally:
            _safe_close(parquet_file)
    finally:
        _safe_close(handle)

    return accumulator.finalize(
        window=window,
        request_id=request_id,
        pid=None,
        source_id=source_id,
        logical_id=logical_id,
        resolved_symbol=resolved_symbol,
        tick_type=tick_type,
    )


def ensure_rebuild_allowed(final_path: Path, *, rebuild: bool) -> None:
    """Recusa sobrescrever silenciosamente uma sessão já concluída.

    O padrão é idempotente e conservador: um arquivo final existente só
    pode ser substituído por opção explícita de reconstrução.
    """

    if Path(final_path).exists() and not rebuild:
        raise BackfillWriteError(
            f"Arquivo final já existe e rebuild=False; nada foi sobrescrito: {final_path}"
        )


class SessionTickWriter:
    """Escreve os chunks de uma sessão em `<final>.partial`, um row group por chunk.

    Não é um gerenciador de contexto: sua vida útil atravessa várias
    chamadas (uma por chunk) no laço do worker, então o chamador controla
    explicitamente `open()`, `write_chunk()` e, ao final,
    `close_and_promote()` (sucesso) ou `abort()` (falha/interrupção).
    """

    def __init__(self, final_path: Path, *, schema: pa.Schema, compression: str = "zstd") -> None:
        self.final_path = Path(final_path)
        self.partial_path = self.final_path.with_name(self.final_path.name + ".partial")
        self.schema = schema
        self.compression = compression
        self._writer: pq.ParquetWriter | None = None
        self.row_count = 0

    def open(self) -> None:
        try:
            self.final_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackfillWriteError(f"Falha ao criar a pasta do arquivo final: {exc}") from exc
        # A retomada descarta qualquer parcial órfão de uma tentativa
        # anterior interrompida: o dia é sempre refeito do zero, nunca
        # continuado a partir de um `.partial` de outra tentativa.
        discard_partial(self.partial_path)
        try:
            self._writer = pq.ParquetWriter(
                str(self.partial_path), self.schema, compression=self.compression
            )
        except _ARROW_ERRORS as exc:
            raise BackfillWriteError(f"Falha ao abrir o parcial para escrita: {exc}") from exc

    def write_chunk(self, records: Sequence[TickRecord]) -> None:
        if self._writer is None:
            raise BackfillWriteError("write_chunk chamado antes de open().")
        if not records:
            return
        try:
            table = records_to_table(records, self.schema)
            self._writer.write_table(table)
        except _ARROW_ERRORS as exc:
            raise BackfillWriteError(f"Falha ao escrever chunk: {exc}") from exc
        self.row_count += len(records)

    def abort(self) -> None:
        """Fecha o parcial (se aberto) sem promover e descarta o arquivo incompleto.

        Propaga `BackfillWriteError` se fechar ou descartar o parcial falhar
        de verdade — nunca finge que a limpeza funcionou.
        """

        if self._writer is not None:
            writer, self._writer = self._writer, None
            try:
                writer.close()
            except _ARROW_ERRORS as exc:
                raise BackfillWriteError(f"Falha ao fechar o parcial durante abort: {exc}") from exc
        discard_partial(self.partial_path)

    def close_and_promote(self) -> PromotedFile:
        """Fecha, sincroniza, valida e promove o parcial para o arquivo final.

        Sincroniza o parcial em disco (`fsync`) depois de fechar o
        `ParquetWriter`, reabre para conferir que a contagem de linhas e o
        schema batem com o que foi escrito, fecha esse handle de leitura
        explicitamente (não depende do refcount do CPython) e só então
        calcula o hash e promove — um arquivo final nunca reflete uma
        escrita inacabada, não sincronizada ou corrompida.
        """

        if self._writer is None:
            raise BackfillWriteError("close_and_promote chamado antes de open().")
        try:
            self._writer.close()
        except _ARROW_ERRORS as exc:
            self._writer = None
            discard_partial(self.partial_path)
            raise BackfillWriteError(f"Falha ao fechar o parcial: {exc}") from exc
        self._writer = None

        _fsync_path(self.partial_path)

        try:
            num_rows, read_schema, _metadata = _read_parquet_metadata_safely(self.partial_path)
        except BackfillWriteError:
            discard_partial(self.partial_path)
            raise
        schema_matches = read_schema.equals(self.schema)

        if num_rows != self.row_count:
            discard_partial(self.partial_path)
            raise BackfillWriteError(
                f"Contagem divergente ao validar o parcial: esperado {self.row_count}, "
                f"encontrado {num_rows}"
            )
        if not schema_matches:
            discard_partial(self.partial_path)
            raise BackfillWriteError("Schema do parcial divergente do schema esperado.")

        try:
            digest = _sha256_file(self.partial_path)
            size_bytes = _file_size(self.partial_path)
        except BackfillWriteError:
            discard_partial(self.partial_path)
            raise
        try:
            os.replace(self.partial_path, self.final_path)
        except OSError as exc:
            raise BackfillWriteError(f"Falha ao promover o arquivo final: {exc}") from exc
        return PromotedFile(path=self.final_path, size_bytes=size_bytes, sha256=digest, row_count=self.row_count)


def _sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(block_size)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()
    except OSError as exc:
        raise BackfillWriteError(f"Falha ao calcular hash de {path}: {exc}") from exc
