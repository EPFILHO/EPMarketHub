"""Catálogo operacional SQLite do backfill histórico (DEV-002 — Portão A).

Usa somente `sqlite3` da biblioteca padrão. Guarda estado, tentativas,
contagens, hash e retomada por sessão (`source_id`/`logical_id`/
`session_date`) — nunca os ticks brutos. Cada transição é uma transação
isolada (`BEGIN IMMEDIATE` ... `COMMIT`/`ROLLBACK`) sobre uma conexão com
`busy_timeout` configurado, para que duas conexões disputando a mesma sessão
se serializem em vez de corromper o estado.

Toda função pública deste módulo normaliza `sqlite3.Error` (lock, corrupção,
I/O do próprio SQLite) em `CatalogStateError` — nenhuma chamadora precisa
tratar exceções cruas do driver separadamente.

Propriedade por tentativa (`attempt_id`, terceira correção de auditoria):
`begin_attempt` gera um identificador opaco novo toda vez que uma sessão
entra em `running` e o grava na linha. Toda transição terminal
(`complete_session`/`fail_session`/`interrupt_session`) exige esse
`attempt_id` no `WHERE`, além de `state='running'` — um evento tardio da
tentativa A nunca pode atingir a tentativa B que a sucedeu, mesmo que B
esteja com o mesmo `state='running'`. `reconcile_session` nunca toca uma
linha `running`: reconciliar é, por definição, uma decisão sobre uma sessão
sem dono ativo — ver `docs/work_orders/DEV-002.md`.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .tick_diagnostics import TickWindowSummary

CATALOG_SCHEMA_VERSION = 1

# Tempo que uma conexão espera pelo lock de escrita de outra antes de
# desistir. SQLite então levanta `sqlite3.OperationalError` ("database is
# locked"), normalizado abaixo em `CatalogStateError` — nunca um travamento
# indefinido nem uma exceção crua propagada ao worker.
_BUSY_TIMEOUT_MS = 5000

# A primeira transição de modo de journal para WAL num arquivo novo é uma
# operação exclusiva: se duas conexões tentam essa mesma transição ao mesmo
# tempo, o SQLite pode devolver "database is locked" mesmo com busy_timeout
# configurado (esse caso específico nem sempre passa pelo busy handler
# normal). Tentativas curtas e limitadas resolvem sem bloquear indefinidamente.
_WAL_SWITCH_RETRIES = 20
_WAL_SWITCH_RETRY_DELAY_SECONDS = 0.025

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS backfill_sessions (
    source_id TEXT NOT NULL,
    logical_id TEXT NOT NULL,
    session_date TEXT NOT NULL,
    attempt_id TEXT,
    resolved_symbol TEXT,
    session_timezone TEXT NOT NULL,
    tick_type TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    requested_start_utc TEXT,
    requested_end_utc TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds REAL,
    first_tick_utc TEXT,
    last_tick_utc TEXT,
    tick_count INTEGER,
    non_zero_counts TEXT,
    flags_histogram TEXT,
    out_of_order_count INTEGER,
    exact_duplicate_count INTEGER,
    time_msc_tie_count INTEGER,
    largest_gaps_seconds TEXT,
    empty_reason TEXT,
    file_path TEXT,
    file_size_bytes INTEGER,
    sha256 TEXT,
    schema_version INTEGER,
    collector_version TEXT,
    error_reason TEXT,
    error_message TEXT,
    error_code INTEGER,
    reconciled INTEGER NOT NULL DEFAULT 0,
    catalog_schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, logical_id, session_date)
);
"""

_JSON_COLUMNS = ("non_zero_counts", "flags_histogram", "largest_gaps_seconds")


class CatalogStateError(Exception):
    """Transição de catálogo recusada (sessão já concluída, corrida, lock, etc.)."""


def open_catalog(db_path: Path) -> sqlite3.Connection:
    """Abre (criando se necessário) o catálogo SQLite e garante o schema.

    Se a conexão for aberta com sucesso mas uma etapa de inicialização
    seguinte falhar (pragma, criação de schema), a conexão parcialmente
    aberta é fechada explicitamente antes de propagar `CatalogStateError` —
    nunca vaza um handle de conexão não utilizável.
    """

    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CatalogStateError(f"disk_error: não foi possível criar a pasta do catálogo: {exc}") from exc

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        _set_wal_journal_mode(conn)
        conn.execute(_SCHEMA_SQL)
    except sqlite3.Error as exc:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        raise CatalogStateError(f"sqlite_error: falha ao abrir o catálogo: {exc}") from exc
    return conn


def _set_wal_journal_mode(conn: sqlite3.Connection) -> None:
    """Liga o modo WAL, tolerando a corrida conhecida de duas conexões
    tentando a mesma transição de journal simultaneamente num arquivo novo
    (ver `_WAL_SWITCH_RETRIES`). Tentativas limitadas — nunca bloqueia
    indefinidamente; se todas falharem, a última `sqlite3.OperationalError`
    propaga e é normalizada em `CatalogStateError` pelo chamador.
    """

    last_exc: sqlite3.OperationalError | None = None
    for _ in range(_WAL_SWITCH_RETRIES):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            last_exc = exc
            time.sleep(_WAL_SWITCH_RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc


def new_attempt_id() -> str:
    """Identificador opaco e único de uma tentativa (`uuid4`, sem dados pessoais)."""

    return uuid.uuid4().hex


def get_session(
    conn: sqlite3.Connection, *, source_id: str, logical_id: str, session_date: date
) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT * FROM backfill_sessions WHERE source_id=? AND logical_id=? AND session_date=?",
            (source_id, logical_id, session_date.isoformat()),
        ).fetchone()
    except sqlite3.Error as exc:
        raise CatalogStateError(f"sqlite_error: falha ao ler a sessão: {exc}") from exc
    return _row_to_dict(row) if row is not None else None


def begin_attempt(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    logical_id: str,
    session_date: date,
    session_timezone: str,
    tick_type: str,
    requested_start_utc: datetime,
    requested_end_utc: datetime,
    rebuild: bool,
) -> dict[str, Any]:
    """Registra o início de uma tentativa para a sessão.

    Recusa com `CatalogStateError` uma sessão já `completed` sem
    `rebuild=True`, ou já `running` (corrida entre duas tentativas). A
    leitura do estado atual e a gravação da nova tentativa acontecem na
    mesma transação SQLite (`BEGIN IMMEDIATE`), serializando com qualquer
    outra conexão que tente a mesma transição ao mesmo tempo — a segunda
    conexão a chegar sempre vê a primeira já `running` e recebe
    `backfill_busy` antes de tocar qualquer arquivo.

    Gera um `attempt_id` novo e opaco (`new_attempt_id`) a cada chamada
    bem-sucedida, mesmo ao reaproveitar uma sessão existente: essa é a
    identidade que toda transição terminal subsequente precisa apresentar.
    """

    now = _now_iso()
    attempt_id = new_attempt_id()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise CatalogStateError(f"catalog_locked: não foi possível reservar a sessão: {exc}") from exc
    try:
        existing = conn.execute(
            "SELECT * FROM backfill_sessions WHERE source_id=? AND logical_id=? AND session_date=?",
            (source_id, logical_id, session_date.isoformat()),
        ).fetchone()
        if existing is not None:
            state = existing["state"]
            if state == "completed" and not rebuild:
                raise CatalogStateError(
                    "already_completed: sessão já concluída; use rebuild=True para refazer."
                )
            if state == "running":
                raise CatalogStateError(
                    "backfill_busy: já existe uma tentativa em andamento para esta sessão."
                )
            attempts = int(existing["attempts"]) + 1
            # Limpa explicitamente TODOS os campos terminais/métricas da
            # tentativa anterior ao entrar em `running` (correção de
            # auditoria: uma reconstrução vazia não pode herdar
            # first_tick_utc/non_zero_counts/flags_histogram/hash/etc. de um
            # resultado anterior não vazio, nem o inverso). Não há, nesta
            # fase, um modelo separado para "último artefato bom": a única
            # fonte de verdade após `begin_attempt` é o resultado desta
            # tentativa (ou a reconciliação, que grava tudo de uma vez).
            conn.execute(
                """
                UPDATE backfill_sessions SET
                    state='running', attempt_id=?, attempts=?, resolved_symbol=NULL,
                    session_timezone=?, tick_type=?, requested_start_utc=?, requested_end_utc=?,
                    started_at=?, ended_at=NULL, duration_seconds=NULL,
                    first_tick_utc=NULL, last_tick_utc=NULL, tick_count=NULL,
                    non_zero_counts=NULL, flags_histogram=NULL, out_of_order_count=NULL,
                    exact_duplicate_count=NULL, time_msc_tie_count=NULL, largest_gaps_seconds=NULL,
                    empty_reason=NULL, file_path=NULL, file_size_bytes=NULL, sha256=NULL,
                    schema_version=NULL, collector_version=NULL, reconciled=0,
                    error_reason=NULL, error_message=NULL, error_code=NULL,
                    updated_at=?
                WHERE source_id=? AND logical_id=? AND session_date=?
                """,
                (
                    attempt_id,
                    attempts,
                    session_timezone,
                    tick_type,
                    requested_start_utc.isoformat(),
                    requested_end_utc.isoformat(),
                    now,
                    now,
                    source_id,
                    logical_id,
                    session_date.isoformat(),
                ),
            )
        else:
            attempts = 1
            conn.execute(
                """
                INSERT INTO backfill_sessions (
                    source_id, logical_id, session_date, attempt_id, resolved_symbol, session_timezone,
                    tick_type, state, attempts, requested_start_utc, requested_end_utc,
                    started_at, catalog_schema_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    logical_id,
                    session_date.isoformat(),
                    attempt_id,
                    session_timezone,
                    tick_type,
                    attempts,
                    requested_start_utc.isoformat(),
                    requested_end_utc.isoformat(),
                    now,
                    CATALOG_SCHEMA_VERSION,
                    now,
                    now,
                ),
            )
        conn.execute("COMMIT")
    except CatalogStateError:
        _rollback(conn)
        raise
    except sqlite3.Error as exc:
        _rollback(conn)
        raise CatalogStateError(f"sqlite_error: falha ao reservar a sessão: {exc}") from exc
    result = get_session(conn, source_id=source_id, logical_id=logical_id, session_date=session_date)
    assert result is not None
    return result


def complete_session(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    logical_id: str,
    session_date: date,
    attempt_id: str,
    resolved_symbol: str,
    summary: TickWindowSummary,
    file_path: Path,
    file_size_bytes: int,
    sha256: str,
    schema_version: int,
    collector_version: str,
) -> dict[str, Any]:
    """Marca a sessão como `completed` (ou `empty` se `total_count == 0`).

    Só deve ser chamado depois que o arquivo final já foi promovido e o
    hash calculado. A transição só é aplicada se o estado atual ainda for
    `running` **e** pertencer a `attempt_id` — recusa (com
    `CatalogStateError`) concluir uma sessão que já não está mais em
    andamento, ou que já foi sucedida por outra tentativa, para nunca
    rebaixar um resultado terminal já registrado por outra transição nem
    aplicar um evento tardio da tentativa errada.
    """

    state = "empty" if summary.total_count == 0 else "completed"
    now = _now_iso()

    def _update(cur: sqlite3.Cursor, started_at: str | None) -> None:
        _write_completion_row(
            cur,
            state=state,
            attempt_id=attempt_id,
            resolved_symbol=resolved_symbol,
            now=now,
            duration=_duration_seconds(started_at, now),
            summary=summary,
            file_path=file_path,
            file_size_bytes=file_size_bytes,
            sha256=sha256,
            schema_version=schema_version,
            collector_version=collector_version,
            reconciled=0,
            source_id=source_id,
            logical_id=logical_id,
            session_date=session_date,
            require_running_attempt_id=attempt_id,
        )

    return _conditional_transition(
        conn,
        source_id=source_id,
        logical_id=logical_id,
        session_date=session_date,
        attempt_id=attempt_id,
        apply_update=_update,
        transition_name="complete_session",
    )


def fail_session(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    logical_id: str,
    session_date: date,
    attempt_id: str,
    reason: str,
    message: str,
    code: int | None = None,
) -> dict[str, Any]:
    now = _now_iso()

    def _update(cur: sqlite3.Cursor, started_at: str | None) -> None:
        duration = _duration_seconds(started_at, now)
        cur.execute(
            """
            UPDATE backfill_sessions SET
                state='failed', ended_at=?, duration_seconds=?,
                error_reason=?, error_message=?, error_code=?, updated_at=?
            WHERE source_id=? AND logical_id=? AND session_date=? AND state='running' AND attempt_id=?
            """,
            (now, duration, reason, message, code, now, source_id, logical_id, session_date.isoformat(), attempt_id),
        )

    return _conditional_transition(
        conn,
        source_id=source_id,
        logical_id=logical_id,
        session_date=session_date,
        attempt_id=attempt_id,
        apply_update=_update,
        transition_name="fail_session",
    )


def interrupt_session(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    logical_id: str,
    session_date: date,
    attempt_id: str,
    message: str,
) -> dict[str, Any]:
    """Marca a sessão como `interrupted`. Dias já concluídos, e tentativas
    diferentes da informada, não são afetados."""

    now = _now_iso()

    def _update(cur: sqlite3.Cursor, started_at: str | None) -> None:
        duration = _duration_seconds(started_at, now)
        cur.execute(
            """
            UPDATE backfill_sessions SET
                state='interrupted', ended_at=?, duration_seconds=?,
                error_reason='interrupted', error_message=?, error_code=NULL, updated_at=?
            WHERE source_id=? AND logical_id=? AND session_date=? AND state='running' AND attempt_id=?
            """,
            (now, duration, message, now, source_id, logical_id, session_date.isoformat(), attempt_id),
        )

    return _conditional_transition(
        conn,
        source_id=source_id,
        logical_id=logical_id,
        session_date=session_date,
        attempt_id=attempt_id,
        apply_update=_update,
        transition_name="interrupt_session",
    )


def reconcile_session(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    logical_id: str,
    session_date: date,
    file_present: bool,
    attempt_id: str | None = None,
    resolved_symbol: str | None = None,
    summary: TickWindowSummary | None = None,
    file_path: Path | None = None,
    file_size_bytes: int | None = None,
    file_sha256: str | None = None,
    schema_version: int | None = None,
    collector_version: str | None = None,
    allow_running_attempt_id: str | None = None,
) -> dict[str, Any] | None:
    """Concilia o catálogo com a verdade física do arquivo final.

    Só atua sobre uma linha já existente (criada por `begin_attempt`); não
    fabrica uma linha nova a partir de um arquivo totalmente órfão sem
    nenhum registro prévio — esse caso extremo fica fora do escopo do
    Portão A. Retorna `None` (sem tocar nada) se a sessão não existir no
    catálogo.

    **Nunca toca uma linha `state='running'`**, com uma única exceção
    explícita (correção da terceira auditoria, itens 1–3; e da quarta
    auditoria, item 1): `running` significa, por padrão, que existe uma
    tentativa ativa dona da sessão — reconciliar por cima dela corromperia
    essa tentativa e poderia levar a remoção do `.partial` que ela possui.
    A única exceção é `allow_running_attempt_id`: quando informado E igual
    ao `attempt_id` já gravado na linha `running`, o chamador está provando
    posse da própria tentativa órfã (ex.: o arquivo final promovido tem essa
    identidade embutida) — só então a linha é liberada da proteção de
    `running`. Qualquer outro caso (`allow_running_attempt_id=None`, ou
    divergente do `attempt_id` atual — ex.: uma nova tentativa já assumiu a
    sessão entre a leitura e esta chamada) continua um no-op silencioso;
    quem quer que tenha chamado `reconcile_session` deve, em seguida, tentar
    `begin_attempt`, que recusará corretamente com `backfill_busy`.

    Quando o arquivo está presente e a linha não está `running`, a
    reconciliação **sempre** regrava a linha inteira a partir do resumo
    recém-recalculado pelo chamador — nunca usa um atalho de "hash igual,
    nada a fazer": `state+sha256` sozinhos não provam que as demais métricas
    do catálogo estão corretas (ex.: um campo adulterado por fora do fluxo
    normal). `resolved_symbol`/`summary`/`file_path`/`file_size_bytes`/
    `file_sha256`/`schema_version`/`collector_version`/`attempt_id` (o do
    arquivo, não o de uma tentativa qualquer) são obrigatórios nesse caso.

    Exceção: se a linha já estava `completed`/`empty` e tem um `sha256`
    previamente auditado que diverge do `file_sha256` recém-calculado, isso
    nunca é tratado como uma nova verdade a adotar — o arquivo foi reescrito
    (adulteração ou corrupção estrutural que preserva schema/metadados) por
    fora do fluxo normal. Levanta `CatalogStateError` com prefixo
    `integrity_error` e não grava nada; a linha permanece exatamente como
    estava (correção da rejeição focal da quarta auditoria, item 2).

    Arquivo ausente e catálogo `completed`/`empty`: rebaixa para `failed`
    com `error_reason="reconciliation_missing_file"`, nunca deixa o
    catálogo apontar para um artefato inexistente.

    Nunca apaga nem sobrescreve o arquivo final: só ajusta o catálogo para
    refletir a realidade do disco.
    """

    if file_present:
        required = {
            "attempt_id": attempt_id,
            "resolved_symbol": resolved_symbol,
            "summary": summary,
            "file_path": file_path,
            "file_size_bytes": file_size_bytes,
            "file_sha256": file_sha256,
            "schema_version": schema_version,
            "collector_version": collector_version,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(f"reconcile_session com file_present=True exige: {', '.join(missing)}")

    now = _now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise CatalogStateError(f"catalog_locked: não foi possível iniciar a reconciliação: {exc}") from exc
    try:
        row = conn.execute(
            "SELECT * FROM backfill_sessions WHERE source_id=? AND logical_id=? AND session_date=?",
            (source_id, logical_id, session_date.isoformat()),
        ).fetchone()
        running_but_unproven_orphan = row is not None and row["state"] == "running" and (
            allow_running_attempt_id is None or row["attempt_id"] != allow_running_attempt_id
        )
        if row is None or running_but_unproven_orphan:
            # Nada a conciliar: sessão inexistente, ou dona ativa presente
            # sem prova de posse órfã da tentativa exata — em nenhum dos
            # casos a reconciliação decide algo aqui.
            conn.execute("COMMIT")
            return _row_to_dict(row) if row is not None else None

        if file_present:
            assert attempt_id is not None and resolved_symbol is not None and summary is not None
            assert file_path is not None and file_size_bytes is not None and file_sha256 is not None
            assert schema_version is not None and collector_version is not None
            if row["state"] in ("completed", "empty") and row["sha256"] is not None and row["sha256"] != file_sha256:
                # Sessão já auditada: um hash diferente prova que o arquivo
                # mudou por fora do fluxo normal, nunca que o resumo
                # recém-recalculado é a nova verdade. Falha estruturada, sem
                # tocar a linha (rejeição focal da quarta auditoria, item 2)
                # — o `except Exception` abaixo desfaz a transação (nada foi
                # escrito) e repropaga.
                raise CatalogStateError(
                    "integrity_error: hash divergente para sessão já "
                    f"{row['state']} ({source_id}/{logical_id}/{session_date.isoformat()}): "
                    f"catálogo={row['sha256']}, arquivo={file_sha256}"
                )
            state = "empty" if summary.total_count == 0 else "completed"
            cur = conn.cursor()
            _write_completion_row(
                cur,
                state=state,
                attempt_id=attempt_id,
                resolved_symbol=resolved_symbol,
                now=now,
                duration=_duration_seconds(row["started_at"], now),
                summary=summary,
                file_path=file_path,
                file_size_bytes=file_size_bytes,
                sha256=file_sha256,
                schema_version=schema_version,
                collector_version=collector_version,
                reconciled=1,
                source_id=source_id,
                logical_id=logical_id,
                session_date=session_date,
                require_running_attempt_id=None,
            )
        else:
            if row["state"] in ("completed", "empty"):
                conn.execute(
                    """
                    UPDATE backfill_sessions SET
                        state='failed', error_reason='reconciliation_missing_file',
                        error_message=?, reconciled=1, updated_at=?
                    WHERE source_id=? AND logical_id=? AND session_date=?
                    """,
                    (
                        "Arquivo final registrado como concluído não foi encontrado no disco.",
                        now,
                        source_id,
                        logical_id,
                        session_date.isoformat(),
                    ),
                )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        _rollback(conn)
        raise CatalogStateError(f"sqlite_error: falha ao conciliar a sessão: {exc}") from exc
    except Exception:
        _rollback(conn)
        raise
    return get_session(conn, source_id=source_id, logical_id=logical_id, session_date=session_date)


def _write_completion_row(
    cur: sqlite3.Cursor,
    *,
    state: str,
    attempt_id: str,
    resolved_symbol: str,
    now: str,
    duration: float | None,
    summary: TickWindowSummary,
    file_path: Path,
    file_size_bytes: int,
    sha256: str,
    schema_version: int,
    collector_version: str,
    reconciled: int,
    source_id: str,
    logical_id: str,
    session_date: date,
    require_running_attempt_id: str | None,
) -> int:
    """Grava uma linha `completed`/`empty` como unidade coerente.

    Único ponto que escreve esses campos: usado por `complete_session`
    (fluxo normal, exige `state='running' AND attempt_id=?`) e por
    `reconcile_session` (recuperação a partir do arquivo, sem essa
    exigência — a chamadora já garantiu que a linha não está `running`).
    Sempre grava **todos** os campos de resumo/identidade juntos — nunca
    deixa um campo de uma escrita anterior sobreviver a uma nova transição
    terminal, e sempre atualiza `attempt_id` para refletir a tentativa dona
    deste resultado.
    """

    where = "WHERE source_id=? AND logical_id=? AND session_date=?"
    params: list[Any] = [
        state,
        attempt_id,
        resolved_symbol,
        now,
        duration,
        summary.first_time_utc.isoformat() if summary.first_time_utc else None,
        summary.last_time_utc.isoformat() if summary.last_time_utc else None,
        summary.total_count,
        json.dumps(summary.non_zero_counts),
        json.dumps(summary.flags_histogram),
        summary.out_of_order_count,
        summary.exact_duplicate_count,
        summary.time_msc_tie_count,
        json.dumps(summary.largest_gaps_seconds),
        summary.empty_reason,
        str(file_path),
        file_size_bytes,
        sha256,
        schema_version,
        collector_version,
        reconciled,
        now,
        source_id,
        logical_id,
        session_date.isoformat(),
    ]
    if require_running_attempt_id is not None:
        where += " AND state='running' AND attempt_id=?"
        params.append(require_running_attempt_id)
    cur.execute(
        f"""
        UPDATE backfill_sessions SET
            state=?, attempt_id=?, resolved_symbol=?, ended_at=?, duration_seconds=?,
            first_tick_utc=?, last_tick_utc=?, tick_count=?,
            non_zero_counts=?, flags_histogram=?, out_of_order_count=?, exact_duplicate_count=?,
            time_msc_tie_count=?, largest_gaps_seconds=?, empty_reason=?,
            file_path=?, file_size_bytes=?, sha256=?, schema_version=?, collector_version=?,
            error_reason=NULL, error_message=NULL, error_code=NULL, reconciled=?, updated_at=?
        {where}
        """,
        params,
    )
    return cur.rowcount


def _conditional_transition(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    logical_id: str,
    session_date: date,
    attempt_id: str,
    apply_update,
    transition_name: str,
) -> dict[str, Any]:
    """Aplica uma transição terminal só se o estado atual for `running` E
    pertencer a `attempt_id`.

    Compartilhada por `complete_session`/`fail_session`/`interrupt_session`:
    lê o estado/tentativa atual, recusa com `CatalogStateError` se não for
    `running` ou se pertencer a outra tentativa (evento tardio de uma
    tentativa já superada), e confere o `rowcount` do `UPDATE` como defesa
    em profundidade extra (o `BEGIN IMMEDIATE` já deveria impedir qualquer
    corrida real).
    """

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise CatalogStateError(f"catalog_locked: não foi possível iniciar {transition_name}: {exc}") from exc
    try:
        row = conn.execute(
            "SELECT started_at, state, attempt_id FROM backfill_sessions "
            "WHERE source_id=? AND logical_id=? AND session_date=?",
            (source_id, logical_id, session_date.isoformat()),
        ).fetchone()
        if row is None:
            raise CatalogStateError(
                f"no_such_session: sessão não encontrada para {transition_name}; "
                "begin_attempt deve ser chamado antes."
            )
        if row["state"] != "running":
            raise CatalogStateError(
                f"invalid_transition: {transition_name} recusado; estado atual é {row['state']!r}, "
                "não 'running' — uma transição terminal tardia nunca rebaixa completed/empty."
            )
        if row["attempt_id"] != attempt_id:
            raise CatalogStateError(
                f"stale_attempt: {transition_name} recusado; a tentativa em execução não é mais "
                "a que gerou este evento — um evento tardio de uma tentativa superada nunca "
                "altera a tentativa atual."
            )
        rowcount = _apply_and_count(conn, apply_update, row["started_at"])
        if rowcount != 1:
            raise CatalogStateError(
                f"concurrent_transition: o estado mudou durante {transition_name}; tente novamente."
            )
        conn.execute("COMMIT")
    except CatalogStateError:
        _rollback(conn)
        raise
    except sqlite3.Error as exc:
        _rollback(conn)
        raise CatalogStateError(f"sqlite_error: falha durante {transition_name}: {exc}") from exc
    result = get_session(conn, source_id=source_id, logical_id=logical_id, session_date=session_date)
    assert result is not None
    return result


def _apply_and_count(conn: sqlite3.Connection, apply_update, started_at: str | None) -> int:
    cursor = conn.cursor()
    apply_update(cursor, started_at)
    return cursor.rowcount


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _duration_seconds(started_at: str | None, ended_at_iso: str) -> float | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        ended = datetime.fromisoformat(ended_at_iso)
    except ValueError:
        return None
    return (ended - started).total_seconds()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Converte uma linha crua em `dict`, normalizando dados inválidos.

    JSON corrompido num campo de métricas ou uma `session_date` inválida
    nunca propagam como exceção crua de parsing: viram `CatalogStateError`,
    igual a qualquer outra falha estruturada deste módulo.
    """

    data = dict(row)
    for key in _JSON_COLUMNS:
        raw = data.get(key)
        if raw:
            try:
                data[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                raise CatalogStateError(f"catalog_corrupt: campo {key!r} com JSON inválido: {exc}") from exc
    try:
        data["session_date"] = date.fromisoformat(data["session_date"])
    except (ValueError, TypeError) as exc:
        raise CatalogStateError(f"catalog_corrupt: session_date inválida no catálogo: {exc}") from exc
    return data


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
