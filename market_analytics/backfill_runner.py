"""Orquestra o backfill de uma sessão (dia civil) entre catálogo e escritor.

Este módulo não conhece MT5 nem o worker: `fetch_chunk` é injetado pelo
chamador e devolve os `TickRecord` já extraídos de uma janela
`[start_utc, end_utc)`, ou levanta `BackfillSourceError`. Isso permite testar
toda a lógica de atomicidade, retomada, catálogo e concorrência com fontes
falsas em diretórios temporários, sem qualquer worker ou processo MT5 real.

A API é dividida em passos (`start_backfill_job` / `advance_backfill_job` /
`interrupt_backfill_job`) para que o worker processe exatamente **um chunk**
por iteração do laço principal, sem bloquear heartbeat/comandos/parada pela
sessão inteira — a mesma estratégia já usada por
`market_analytics.tick_diagnostics` no worker (uma chamada MT5 é síncrona).
`run_session_backfill` apenas encadeia esses passos até um estado terminal;
é o modo mais simples de testar a lógica completa e serve de base para uma
futura orquestração fora do worker.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import backfill_catalog as catalog
from .backfill_writer import (
    BackfillWriteError,
    PromotedFile,
    SessionTickWriter,
    build_schema,
    ensure_rebuild_allowed,
    inspect_final_file,
    recompute_summary_from_file,
)
from .tick_backfill import (
    BACKFILL_COLLECTOR_VERSION,
    RAW_SCHEMA_VERSION,
    ArtifactIdentityError,
    BackfillSessionRequest,
    build_raw_metadata,
    raw_partition_dir,
    validate_artifact_identity,
)
from .tick_diagnostics import TickRecord, TickWindow, TickWindowAccumulator, TickWindowSummary

logger = logging.getLogger(__name__)

# Estados terminais possíveis retornados por `advance_backfill_job`/`run_session_backfill`.
TERMINAL_STATES: frozenset[str] = frozenset({"completed", "empty", "failed", "interrupted"})


class BackfillSourceError(Exception):
    """Falha estruturada ao buscar um chunk (mt5_error, malformed_response, ...)."""

    def __init__(self, reason: str, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.code = code


@dataclass(frozen=True)
class BackfillSessionResult:
    """Resultado terminal de uma sessão: completa, vazia, falha ou interrompida."""

    state: str
    resolved_symbol: str | None
    summary: TickWindowSummary | None
    promoted: PromotedFile | None
    error_reason: str | None = None
    error_message: str | None = None
    error_code: int | None = None


def _fast_path_still_valid(
    final_path: Path, existing_row: dict[str, Any], request: BackfillSessionRequest
) -> bool:
    """Checagem barata (footer/schema/hash do Parquet, nunca tick a tick)
    para decidir se uma sessão já `completed`/`empty` pode pular a
    reconciliação completa.

    Correção da quarta auditoria: tamanho de arquivo sozinho não prova
    integridade — um rodapé Parquet corrompido pode preservar o tamanho
    exato do arquivo íntegro. Este atalho sempre reabre o arquivo com
    `inspect_final_file` (lê footer/schema/metadados e recalcula o hash
    SHA-256 do conteúdo — nunca decodifica tick a tick) e só aceita pular a
    reconciliação se: o arquivo continuar estruturalmente válido, o hash e a
    contagem de linhas do footer baterem exatamente com o que já está
    gravado no catálogo, e a identidade embutida (`validate_artifact_identity`)
    continuar batendo com esta sessão. Qualquer divergência devolve `False` —
    o chamador cai na reconciliação completa, que sempre falha como
    estruturada (nunca `already_completed`) diante de um arquivo inválido ou
    de hash divergente. Uma retomada de muitos dias já concluídos e intactos
    continua evitando reconstruir/descompactar o resumo de ticks de cada um
    deles: isso continua reservado a `recompute_summary_from_file`, usado só
    quando este atalho não se aplica (sessão não terminal, `rebuild=True`,
    arquivo ausente, corrompido ou divergente)."""

    expected_size = existing_row.get("file_size_bytes")
    expected_sha256 = existing_row.get("sha256")
    if expected_size is None or not expected_sha256:
        return False
    try:
        inspected = inspect_final_file(final_path)
    except BackfillWriteError:
        return False
    if inspected is None:
        return False
    if inspected.size_bytes != expected_size or inspected.sha256 != expected_sha256:
        return False
    if inspected.row_count != existing_row.get("tick_count"):
        return False
    try:
        validate_artifact_identity(inspected.metadata, request=request)
    except ArtifactIdentityError:
        return False
    return True


def start_backfill_job(
    *,
    conn: sqlite3.Connection,
    data_root: Path,
    request: BackfillSessionRequest,
    resolved_symbol: str,
    fetch_chunk: Callable[[TickWindow], Sequence[TickRecord]],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, Any] | None, BackfillSessionResult | None]:
    """Reserva a sessão no catálogo e abre o escritor parcial.

    Antes de decidir se esta tentativa pode prosseguir, concilia o catálogo
    com a verdade física do arquivo final (`reconcile_session`) quando isso
    é necessário: corrige uma divergência deixada por uma promoção sem
    `complete_session` confirmado, ou por uma reconstrução interrompida que
    nunca chegou a substituir o artefato anterior — sem isso, o artefato
    concluído mais recente poderia ficar invisível ou ser perdido por
    engano. Uma sessão `running` nunca é tocada pela reconciliação: isso
    corromperia a tentativa ativa dona dela (ver `CatalogStateError`
    `backfill_busy`).

    Retorna `(job, None)` pronto para `advance_backfill_job`, ou
    `(None, result)` com um resultado terminal `"failed"` se a sessão for
    recusada de imediato: arquivo final já existe sem `rebuild`, sessão já
    `completed`/`running` no catálogo, identidade do arquivo divergente, ou
    falha ao preparar o disco/catálogo.
    """

    window = request.session_window()
    final_dir = raw_partition_dir(
        data_root,
        source_id=request.source_id,
        logical_id=request.logical_id,
        session_date=request.session_date,
    )
    final_path = final_dir / "ticks.parquet"

    def _failed(reason: str, message: str) -> tuple[None, BackfillSessionResult]:
        return None, BackfillSessionResult(
            state="failed",
            resolved_symbol=resolved_symbol,
            summary=None,
            promoted=None,
            error_reason=reason,
            error_message=message,
        )

    try:
        existing_row = catalog.get_session(
            conn, source_id=request.source_id, logical_id=request.logical_id, session_date=request.session_date
        )
    except catalog.CatalogStateError as exc:
        return _failed("catalog_error", str(exc))

    session_is_running = existing_row is not None and existing_row["state"] == "running"
    if session_is_running:
        # Igualdade de `attempt_id` entre um arquivo já promovido e a linha
        # `running` prova apenas autoria do arquivo, nunca que o dono
        # morreu (rejeição focal da quarta auditoria, item 1): um chamador
        # comum de `start_backfill_job` nunca tenta recuperar essa linha
        # sozinho. Não reconcilia nada — begin_attempt, adiante, recusa
        # corretamente com backfill_busy antes de qualquer arquivo ser
        # tocado. A única liberação legítima de uma linha `running` com
        # dono morto é a do manager, com a morte do worker/instância já
        # confirmada (ver `core/worker_manager.py`), que libera via
        # `interrupt_session` — nunca por esta função.
        skip_reconciliation = True
    elif (
        existing_row is not None
        and existing_row["state"] in ("completed", "empty")
        and not request.rebuild
        and final_path.exists()
        and _fast_path_still_valid(final_path, existing_row, request)
    ):
        # Caminho rápido de retomada: o catálogo já tem um resultado
        # terminal para esta sessão e o arquivo no disco não mudou de
        # tamanho. ensure_rebuild_allowed recusa como already_completed
        # adiante, sem reler/recontar ticks de um dia já concluído.
        skip_reconciliation = True
    else:
        skip_reconciliation = False

    if not skip_reconciliation:
        try:
            inspected = inspect_final_file(final_path)
        except BackfillWriteError as exc:
            return _failed("disk_error", str(exc))

        if inspected is None:
            try:
                catalog.reconcile_session(
                    conn,
                    source_id=request.source_id,
                    logical_id=request.logical_id,
                    session_date=request.session_date,
                    file_present=False,
                )
            except catalog.CatalogStateError as exc:
                return _failed("catalog_error", str(exc))
        else:
            # A identidade semântica do arquivo é conferida ANTES de
            # qualquer gravação no catálogo: um arquivo estruturalmente
            # válido, mas cujos metadados embutidos apontam para outra
            # fonte/ativo/data/fuso/versão de coletor, nunca é adotado
            # silenciosamente para esta sessão.
            try:
                identity = validate_artifact_identity(inspected.metadata, request=request)
            except ArtifactIdentityError as exc:
                return _failed("identity_mismatch", str(exc))

            # O resumo de qualidade é reconstruído inteiramente a partir do
            # arquivo, em batches limitados — nunca misturado com métricas
            # de uma tentativa anterior do catálogo.
            try:
                recomputed_summary = recompute_summary_from_file(
                    inspected.path,
                    request_id=request.request_id,
                    source_id=request.source_id,
                    logical_id=request.logical_id,
                    resolved_symbol=identity.resolved_symbol,
                    tick_type=request.tick_type,
                    window=window,
                )
            except BackfillWriteError as exc:
                return _failed("disk_error", str(exc))

            if recomputed_summary.total_count != inspected.row_count:
                return _failed(
                    "disk_error",
                    "Contagem do footer Parquet diverge do resumo reconstruído: "
                    f"footer={inspected.row_count}, resumo={recomputed_summary.total_count}",
                )

            try:
                catalog.reconcile_session(
                    conn,
                    source_id=request.source_id,
                    logical_id=request.logical_id,
                    session_date=request.session_date,
                    file_present=True,
                    attempt_id=identity.attempt_id,
                    resolved_symbol=identity.resolved_symbol,
                    summary=recomputed_summary,
                    file_path=inspected.path,
                    file_size_bytes=inspected.size_bytes,
                    file_sha256=inspected.sha256,
                    schema_version=RAW_SCHEMA_VERSION,
                    collector_version=identity.collector_version,
                )
            except catalog.CatalogStateError as exc:
                # Classificação fiel (mesmo padrão usado adiante para
                # `begin_attempt`): só o prefixo conhecido de falha de
                # integridade vira esse motivo estruturado — qualquer outra
                # falha de catálogo continua `catalog_error` genérico.
                message = str(exc)
                reason = "integrity_error" if message.startswith("integrity_error") else "catalog_error"
                return _failed(reason, message)

    if not session_is_running:
        # Só checa a existência do arquivo quando não há uma tentativa
        # ativa: com a sessão `running`, o motivo correto de recusa é
        # `backfill_busy` (de `begin_attempt`, a seguir) — nunca
        # `already_completed` por causa de um arquivo que por acaso já
        # existia antes dessa tentativa ativa começar (correção da
        # terceira auditoria, item 3).
        try:
            ensure_rebuild_allowed(final_path, rebuild=request.rebuild)
        except BackfillWriteError as exc:
            return _failed("already_completed", str(exc))

    try:
        begun_row = catalog.begin_attempt(
            conn,
            source_id=request.source_id,
            logical_id=request.logical_id,
            session_date=request.session_date,
            session_timezone=request.session_timezone,
            tick_type=request.tick_type,
            requested_start_utc=window.start_utc,
            requested_end_utc=window.end_utc,
            rebuild=request.rebuild,
        )
    except catalog.CatalogStateError as exc:
        # Classificação fiel (correção de auditoria): só os dois prefixos
        # conhecidos de recusa legítima do domínio viram esses motivos.
        # Qualquer outra falha do catálogo (lock, erro de SQLite, transição
        # inválida, corrupção) vira `catalog_error` — nunca é mascarada como
        # "ocupado".
        message = str(exc)
        if message.startswith("already_completed"):
            reason = "already_completed"
        elif message.startswith("backfill_busy"):
            reason = "backfill_busy"
        else:
            reason = "catalog_error"
        return _failed(reason, message)

    attempt_id = begun_row["attempt_id"]
    metadata = build_raw_metadata(
        request=request, resolved_symbol=resolved_symbol, collected_at=now(), attempt_id=attempt_id
    )
    schema = build_schema(metadata=metadata)
    writer = SessionTickWriter(final_path, schema=schema)
    try:
        writer.open()
    except BackfillWriteError as exc:
        _best_effort_fail_session(conn, request, attempt_id, "disk_error", str(exc))
        return _failed("disk_error", str(exc))

    job: dict[str, Any] = {
        "conn": conn,
        "request": request,
        "attempt_id": attempt_id,
        "resolved_symbol": resolved_symbol,
        "window": window,
        "chunks": window.chunks(request.chunk_seconds),
        "chunk_index": 0,
        "accumulator": TickWindowAccumulator(),
        "writer": writer,
        "fetch_chunk": fetch_chunk,
    }
    return job, None


def advance_backfill_job(job: dict[str, Any]) -> tuple[str, BackfillSessionResult | None]:
    """Processa exatamente um chunk: busca, valida e escreve.

    Retorna `("progress", None)` se restam chunks, ou `(estado_terminal,
    resultado)` — `"completed"`, `"empty"` ou `"failed"` — quando a sessão
    termina. O array retornado pela fonte para o chunk é descartado ao fim
    desta chamada; nada além de um chunk fica em memória de uma vez.
    """

    chunks: list[TickWindow] = job["chunks"]
    index: int = job["chunk_index"]
    writer: SessionTickWriter = job["writer"]
    accumulator: TickWindowAccumulator = job["accumulator"]

    if index >= len(chunks):
        return _finalize_safely(job)

    chunk = chunks[index]
    try:
        records = list(job["fetch_chunk"](chunk))
    except BackfillSourceError as exc:
        return "failed", _fail(job, exc.reason, exc.message, exc.code)
    except Exception as exc:  # defesa em profundidade, como no diagnóstico de ticks
        return "failed", _fail(job, "mt5_error", str(exc), None)

    try:
        for record in records:
            accumulator.consume(record)
        writer.write_chunk(records)
    except (BackfillWriteError, OSError) as exc:
        return "failed", _fail(job, "disk_error", str(exc), None)
    except Exception as exc:
        return "failed", _fail(job, "malformed_response", f"Retorno malformado do chunk: {exc}", None)

    job["chunk_index"] = index + 1
    if job["chunk_index"] >= len(chunks):
        return _finalize_safely(job)
    return "progress", None


def _finalize_safely(job: dict[str, Any]) -> tuple[str, BackfillSessionResult]:
    """Chama `_finalize` sob uma última rede de segurança.

    `_finalize` já normaliza as falhas esperadas de Arrow/disco/catálogo
    internamente; esta camada extra garante que nenhuma exceção — esperada
    ou não — escape até `advance_backfill_job` e, dali, até o laço global do
    worker (correção da terceira auditoria, item 4).
    """

    try:
        return _finalize(job)
    except Exception as exc:
        logger.exception("Falha inesperada ao finalizar um backfill.")
        return "failed", _fail(job, "disk_error", f"Falha inesperada ao finalizar: {exc}", None)


def interrupt_backfill_job(job: dict[str, Any], message: str) -> BackfillSessionResult:
    """Aborta o escritor e marca a sessão como `interrupted` no catálogo.

    Descarta apenas o `.partial` desta tentativa; sessões (dias) já
    concluídas anteriormente por outras chamadas não são tocadas. Uma falha
    ao descartar o parcial ou ao gravar a interrupção no catálogo é
    registrada em log, nunca propagada — o worker sempre recebe um
    resultado `"interrupted"` estruturado, e uma divergência real de
    arquivo↔catálogo deixada para trás é corrigida pela reconciliação da
    próxima tentativa.
    """

    request: BackfillSessionRequest = job["request"]
    _safe_abort(job["writer"])
    _best_effort_terminal_transition(
        lambda: catalog.interrupt_session(
            job["conn"],
            source_id=request.source_id,
            logical_id=request.logical_id,
            session_date=request.session_date,
            attempt_id=job["attempt_id"],
            message=message,
        )
    )
    return BackfillSessionResult(
        state="interrupted",
        resolved_symbol=job["resolved_symbol"],
        summary=None,
        promoted=None,
        error_reason="interrupted",
        error_message=message,
    )


def run_session_backfill(
    *,
    conn: sqlite3.Connection,
    data_root: Path,
    request: BackfillSessionRequest,
    resolved_symbol: str,
    fetch_chunk: Callable[[TickWindow], Sequence[TickRecord]],
    should_stop: Callable[[], bool] = lambda: False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BackfillSessionResult:
    """Encadeia os passos até um estado terminal — uso direto em testes/CLI.

    O worker real não usa esta função: ele chama `start_backfill_job` uma
    vez e `advance_backfill_job` um chunk por iteração do laço principal,
    para nunca bloquear heartbeat/comandos pela sessão inteira.
    """

    job, result = start_backfill_job(
        conn=conn,
        data_root=data_root,
        request=request,
        resolved_symbol=resolved_symbol,
        fetch_chunk=fetch_chunk,
        now=now,
    )
    if job is None:
        assert result is not None
        return result

    while True:
        if should_stop():
            return interrupt_backfill_job(job, "Interrompido a pedido entre chunks.")
        outcome, result = advance_backfill_job(job)
        if outcome in TERMINAL_STATES:
            assert result is not None
            return result


def _fail(job: dict[str, Any], reason: str, message: str, code: int | None) -> BackfillSessionResult:
    request: BackfillSessionRequest = job["request"]
    _safe_abort(job["writer"])
    _best_effort_terminal_transition(
        lambda: catalog.fail_session(
            job["conn"],
            source_id=request.source_id,
            logical_id=request.logical_id,
            session_date=request.session_date,
            attempt_id=job["attempt_id"],
            reason=reason,
            message=message,
            code=code,
        )
    )
    return BackfillSessionResult(
        state="failed",
        resolved_symbol=job["resolved_symbol"],
        summary=None,
        promoted=None,
        error_reason=reason,
        error_message=message,
        error_code=code,
    )


def _finalize(job: dict[str, Any]) -> tuple[str, BackfillSessionResult]:
    request: BackfillSessionRequest = job["request"]
    writer: SessionTickWriter = job["writer"]
    accumulator: TickWindowAccumulator = job["accumulator"]
    try:
        promoted = writer.close_and_promote()
    except (BackfillWriteError, OSError) as exc:
        return "failed", _fail(job, "disk_error", str(exc), None)

    summary = accumulator.finalize(
        window=job["window"],
        request_id=request.request_id,
        pid=None,
        source_id=request.source_id,
        logical_id=request.logical_id,
        resolved_symbol=job["resolved_symbol"],
        tick_type=request.tick_type,
    )
    try:
        catalog.complete_session(
            job["conn"],
            source_id=request.source_id,
            logical_id=request.logical_id,
            session_date=request.session_date,
            attempt_id=job["attempt_id"],
            resolved_symbol=job["resolved_symbol"],
            summary=summary,
            file_path=promoted.path,
            file_size_bytes=promoted.size_bytes,
            sha256=promoted.sha256,
            schema_version=RAW_SCHEMA_VERSION,
            collector_version=BACKFILL_COLLECTOR_VERSION,
        )
    except catalog.CatalogStateError as exc:
        # O arquivo já foi promovido, validado e tem hash calculado — nunca
        # o apagamos nem o sobrescrevemos por causa de uma falha do
        # catálogo neste ponto. Ainda somos a tentativa dona da sessão (o
        # `attempt_id` não mudou), então tentamos, como melhor esforço,
        # liberar a propriedade chamando fail_session com a mesma
        # tentativa — isso não é um evento "tardio" de outra tentativa, é a
        # própria tentativa atual se autodeclarando falha. Se isso também
        # falhar (o catálogo pode estar genuinamente inacessível), a linha
        # fica "running" até uma liberação externa e explícita de
        # propriedade (na produção, o manager detectando o worker morto) —
        # reconciliação nunca toca uma linha "running" por conta própria
        # (correção da terceira auditoria, itens 1–3).
        complete_session_error_message = str(exc)
        logger.error(
            "Arquivo final promovido, mas complete_session falhou (%s/%s/%s): %s",
            request.source_id,
            request.logical_id,
            request.session_date,
            complete_session_error_message,
        )
        # Captura a mensagem numa variável comum (não no nome do `except ...
        # as exc`, que Python desvincula ao sair do bloco) antes de fechar
        # sobre ela na lambda — evita depender do tempo de vida exato do
        # nome da exceção através de um closure.
        _best_effort_terminal_transition(
            lambda: catalog.fail_session(
                job["conn"],
                source_id=request.source_id,
                logical_id=request.logical_id,
                session_date=request.session_date,
                attempt_id=job["attempt_id"],
                reason="catalog_error",
                message=complete_session_error_message,
            )
        )
        return "failed", BackfillSessionResult(
            state="failed",
            resolved_symbol=job["resolved_symbol"],
            summary=summary,
            promoted=promoted,
            error_reason="catalog_error",
            error_message=str(exc),
        )
    state = "empty" if summary.total_count == 0 else "completed"
    return state, BackfillSessionResult(
        state=state,
        resolved_symbol=job["resolved_symbol"],
        summary=summary,
        promoted=promoted,
    )


def _safe_abort(writer: SessionTickWriter) -> None:
    """Descarta o parcial sem propagar: uma falha aqui já está em log.

    Chamado sempre a partir de um caminho que já vai reportar uma falha
    "principal" ao chamador; uma falha secundária de limpeza é registrada,
    não escondida — mas também não pode substituir/mascarar o motivo
    original do backfill ter parado.
    """

    try:
        writer.abort()
    except Exception:
        # `writer.abort()` já normaliza falhas esperadas de Arrow/disco em
        # `BackfillWriteError`; o `except Exception` aqui é só a última rede
        # de segurança (correção da terceira auditoria, item 4) — nunca deve
        # mascarar o motivo original do backfill ter parado.
        logger.exception("Falha ao descartar o parcial ao encerrar um backfill.")


def _best_effort_fail_session(
    conn: sqlite3.Connection, request: BackfillSessionRequest, attempt_id: str, reason: str, message: str
) -> None:
    _best_effort_terminal_transition(
        lambda: catalog.fail_session(
            conn,
            source_id=request.source_id,
            logical_id=request.logical_id,
            session_date=request.session_date,
            attempt_id=attempt_id,
            reason=reason,
            message=message,
        )
    )


def _best_effort_terminal_transition(action: Callable[[], Any]) -> None:
    try:
        action()
    except Exception:
        logger.exception("Falha ao gravar transição terminal do backfill no catálogo.")
