"""Fingerprint canônico de barras M1 e verificação contra o inventário congelado
do Fusion Quant (DEV-007 — Fusion Quant DEV-009C.1).

Este módulo é puro: não importa `MetaTrader5`, `pyarrow` nem toca a rede.
Define somente:

- `RawM1Row`: a barra M1 bruta exatamente como o MT5 a devolve, preservando
  os oito campos canônicos citados no inventário (`time`, `open`, `high`,
  `low`, `close`, `tick_volume`, `spread`, `real_volume`) sem qualquer
  transformação de qualidade de volume — essa decisão é do coletor
  (`win_m1_collector.py`), não deste módulo.
- `compute_month_fingerprint`: o hash SHA-256 determinístico de um mês de
  barras, no MESMO algoritmo usado pelo Fusion Quant para congelar
  `inventory_m1.json` (ver abaixo) — usado tanto para comparar contra o
  inventário quanto para registrar proveniência no `run_summary.json`.
- `WinCopy2Inventory`/`load_inventory_file`: leitura estrita do inventário
  independente já congelado pelo Fusion Quant
  (`runtime/win_copy2_monthly_retro/inventory_m1.json`, fora deste
  repositório), com verificação de hash do arquivo inteiro antes de qualquer
  leitura estrutural.

## Algoritmo do fingerprint (exato, igual ao Fusion Quant — corrigido na
## reprovação da auditoria Codex da 1ª entrega)

Para cada barra, na ordem em que aparece em `rows` (já ordenada por `time`
por `win_m1_collector.fetch_and_validate_month`), monta uma linha canônica
com os oito campos, nesta ordem exata, unidos por ``"|"``:

    time          str(int(value))
    open          format(float(value), ".10f")
    high          format(float(value), ".10f")
    low           format(float(value), ".10f")
    close         format(float(value), ".10f")
    tick_volume   str(int(value))
    spread        str(int(value))
    real_volume   str(int(value))

Para cada barra: ``digest.update(linha.encode("utf-8"))`` seguido de
``digest.update(b"\\n")``. O fingerprint final é ``digest.hexdigest()`` em
**hexadecimal maiúsculo** (`str.upper()`), nunca minúsculo — ao contrário da
1ª entrega, que usava um empacotamento binário incompatível com o Fusion
Quant.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Identidade estrita do inventário congelado pelo Fusion Quant (DEV-007,
# "Fonte e protocolo congelados"). Qualquer divergência é recusada — nenhuma
# tolerância especulativa de schema.
INVENTORY_SCHEMA = "fusion-quant-mt5-history-inventory-v1"
INVENTORY_SYMBOL = "WIN$"
INVENTORY_TIMEFRAME = "M1"
INVENTORY_START_MONTH = "2026-01"
INVENTORY_END_MONTH = "2026-07"
EXPECTED_INVENTORY_MONTHS: tuple[str, ...] = (
    "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema", "symbol", "timeframe", "start_month", "end_month", "months",
        # Proveniência factual da captura do inventário (schema real
        # observado — auditoria Codex, 3ª rodada): quem gerou o arquivo,
        # nunca dados de negociação.
        "terminal_path", "terminal_connected", "terminal_build", "mt5_version", "notes",
    }
)
# Status esperado de um mês com barras (bars>0) no protocolo congelado
# jan..jul — a única faixa que este inventário aceita (`EXPECTED_INVENTORY_MONTHS`).
INVENTORY_MONTH_STATUS_WITH_BARS = "available_full_span_candidate"

_MONTH_RECORD_FIELDS = frozenset(
    {
        "requested_month", "status", "bars", "first_timestamp_utc", "last_timestamp_utc",
        "outside_range_count", "bars_fingerprint", "query_attempts", "stable", "mt5_last_error",
    }
)


class InventoryValidationError(ValueError):
    """Inventário ausente, ilegível, com hash divergente ou schema incompatível."""


class InventoryHashMismatchError(InventoryValidationError):
    """O SHA-256 do arquivo de inventário inteiro não bate com o esperado."""


class InventoryMonthMismatchError(InventoryValidationError):
    """Um mês coletado diverge do inventário: mês ausente, contagem ou fingerprint."""


@dataclass(frozen=True)
class RawM1Row:
    """Uma barra M1 bruta, exatamente como devolvida por `copy_rates_range`.

    Somente `time` e OHLC são validados como numericamente sãos aqui — o
    tempo deve ser um inteiro e OHLC deve ser finito, porque um valor
    inválido nesses campos torna a própria barra inutilizável (rejeição do
    mês inteiro, ver `win_m1_collector.fetch_and_validate_month`).
    `tick_volume`/`spread`/`real_volume` são preservados como campos
    factuais (exigência do DEV-007): nenhuma faixa é imposta aqui, porque a
    decisão de qualidade de volume (`exchange` vs `tick_proxy`) é do
    coletor, não deste módulo, e precisa poder ver um `real_volume`
    "inválido" sem que a própria construção da linha já rejeite o dado.
    """

    time: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int
    real_volume: int

    def __post_init__(self) -> None:
        if isinstance(self.time, bool) or not isinstance(self.time, int):
            raise ValueError(f"time deve ser um inteiro (segundos Unix UTC): {self.time!r}")
        for field_name in ("open", "high", "low", "close"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise ValueError(f"{field_name} deve ser finito (recebido: {value!r})")
        for field_name in ("tick_volume", "spread", "real_volume"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{field_name} deve ser um inteiro (recebido: {value!r})")


def _canonical_line(row: RawM1Row) -> str:
    """Linha canônica de uma barra, no formato exato exigido pelo algoritmo."""

    fields = (
        str(int(row.time)),
        format(float(row.open), ".10f"),
        format(float(row.high), ".10f"),
        format(float(row.low), ".10f"),
        format(float(row.close), ".10f"),
        str(int(row.tick_volume)),
        str(int(row.spread)),
        str(int(row.real_volume)),
    )
    return "|".join(fields)


def compute_month_fingerprint(rows: Sequence[RawM1Row]) -> str:
    """SHA-256 hexadecimal MAIÚSCULO (64 caracteres) sobre `rows`, na ordem dada.

    Algoritmo exato do Fusion Quant (ver docstring do módulo): uma linha
    canônica ``"|"``-separada por barra, cada uma seguida de um byte `\\n`
    isolado. O chamador é responsável por já ter ordenado/validado `rows`
    (ver `win_m1_collector.fetch_and_validate_month`) — esta função não
    reordena nada, para que o fingerprint reflita exatamente a sequência
    auditada.
    """

    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical_line(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest().upper()


def fingerprint_human_prefix(fingerprint: str) -> str:
    """Primeiros 16 caracteres do fingerprint (maiúsculos) — só evidência humana.

    Nunca é o critério de comparação executável (ver módulo/DEV-007); use
    `fingerprint` (integral) para qualquer decisão de aceitar/rejeitar.
    """

    return fingerprint[:16].upper()


def _parse_month_timestamp_field(value: Any, field_name: str, requested_month: str) -> datetime:
    """Confere que `value` é ISO-8601 timezone-aware e cai no mês `requested_month`.

    A comparação de mês é feita sobre o instante normalizado em UTC (não
    sobre o texto bruto), então um deslocamento não-UTC que ainda caia no
    mesmo mês em UTC é aceito normalmente.
    """

    if not isinstance(value, str) or not value.strip():
        raise InventoryValidationError(f"{field_name} inválido para {requested_month}: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InventoryValidationError(
            f"{field_name} não é ISO-8601 válido para {requested_month}: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise InventoryValidationError(
            f"{field_name} deve ser timezone-aware para {requested_month}: {value!r}"
        )
    normalized = parsed.astimezone(UTC)
    actual_month = f"{normalized.year:04d}-{normalized.month:02d}"
    if actual_month != requested_month:
        raise InventoryValidationError(
            f"{field_name} ({value!r}) não é coerente com requested_month={requested_month!r} "
            f"(mês real: {actual_month!r})"
        )
    return normalized


@dataclass(frozen=True)
class MonthlyInventoryEntry:
    """Um registro de mês do inventário, no schema real do Fusion Quant.

    DEV-007 exige, para cada um dos sete meses aceitos (todos com `bars>0`
    no protocolo congelado): `stable is True`, `outside_range_count == 0`,
    `status == "available_full_span_candidate"`,
    `first_timestamp_utc`/`last_timestamp_utc` não nulos, ISO-8601
    timezone-aware, coerentes com `requested_month` e com
    `first <= last` — tudo verificado aqui, na construção, para que um
    registro instável/incompleto nunca chegue a ser comparado como se fosse
    confiável.
    """

    requested_month: str
    status: str
    bars: int
    first_timestamp_utc: str | None
    last_timestamp_utc: str | None
    outside_range_count: int
    bars_fingerprint: str
    query_attempts: int
    stable: bool
    mt5_last_error: list[Any]

    def __post_init__(self) -> None:
        if not isinstance(self.requested_month, str) or not _MONTH_RE.match(self.requested_month):
            raise InventoryValidationError(
                f"requested_month inválido no inventário: {self.requested_month!r} (esperado YYYY-MM)"
            )
        if isinstance(self.bars, bool) or not isinstance(self.bars, int) or self.bars < 0:
            raise InventoryValidationError(f"bars inválido para {self.requested_month}: {self.bars!r}")
        if not isinstance(self.bars_fingerprint, str) or not _HEX64_RE.match(self.bars_fingerprint):
            raise InventoryValidationError(
                f"bars_fingerprint inválido para {self.requested_month}: deve ser hex SHA-256 de 64 caracteres"
            )
        if not isinstance(self.status, str) or not self.status.strip():
            raise InventoryValidationError(f"status inválido para {self.requested_month}: {self.status!r}")
        if (
            isinstance(self.outside_range_count, bool)
            or not isinstance(self.outside_range_count, int)
            or self.outside_range_count < 0
        ):
            raise InventoryValidationError(
                f"outside_range_count inválido para {self.requested_month}: {self.outside_range_count!r}"
            )
        if not isinstance(self.stable, bool):
            raise InventoryValidationError(f"stable deve ser booleano para {self.requested_month}: {self.stable!r}")
        if self.outside_range_count != 0:
            raise InventoryValidationError(
                f"{self.requested_month}: outside_range_count deve ser 0 (recebido {self.outside_range_count})"
            )
        if self.stable is not True:
            raise InventoryValidationError(f"{self.requested_month}: stable deve ser true (recebido {self.stable})")

        # O protocolo congelado (jan..jul) só produz meses com bars>0 —
        # `first`/`last_timestamp_utc` só podem ser null quando bars==0.
        if self.bars == 0:
            if self.first_timestamp_utc is not None or self.last_timestamp_utc is not None:
                raise InventoryValidationError(
                    f"{self.requested_month}: first/last_timestamp_utc devem ser null quando bars==0"
                )
        else:
            if self.status != INVENTORY_MONTH_STATUS_WITH_BARS:
                raise InventoryValidationError(
                    f"{self.requested_month}: status deve ser {INVENTORY_MONTH_STATUS_WITH_BARS!r} "
                    f"quando bars>0 (recebido: {self.status!r})"
                )
            if self.first_timestamp_utc is None or self.last_timestamp_utc is None:
                raise InventoryValidationError(
                    f"{self.requested_month}: first/last_timestamp_utc não podem ser null quando bars>0"
                )
            first_dt = _parse_month_timestamp_field(
                self.first_timestamp_utc, "first_timestamp_utc", self.requested_month
            )
            last_dt = _parse_month_timestamp_field(
                self.last_timestamp_utc, "last_timestamp_utc", self.requested_month
            )
            if first_dt > last_dt:
                raise InventoryValidationError(
                    f"{self.requested_month}: first_timestamp_utc ({self.first_timestamp_utc!r}) posterior a "
                    f"last_timestamp_utc ({self.last_timestamp_utc!r})"
                )

        if (
            isinstance(self.query_attempts, bool)
            or not isinstance(self.query_attempts, int)
            or self.query_attempts <= 0
        ):
            raise InventoryValidationError(
                f"{self.requested_month}: query_attempts deve ser um inteiro positivo "
                f"(recebido: {self.query_attempts!r})"
            )

        if (
            not isinstance(self.mt5_last_error, list)
            or len(self.mt5_last_error) != 2
            or isinstance(self.mt5_last_error[0], bool)
            or not isinstance(self.mt5_last_error[0], int)
            or not isinstance(self.mt5_last_error[1], str)
        ):
            raise InventoryValidationError(
                f"{self.requested_month}: mt5_last_error deve ser [código_int, mensagem_str] "
                f"(recebido: {self.mt5_last_error!r})"
            )


def _entry_from_dict(value: Any) -> MonthlyInventoryEntry:
    if not isinstance(value, dict):
        raise InventoryValidationError(f"registro de mês do inventário deve ser um objeto (recebido: {value!r})")
    extra = set(value) - _MONTH_RECORD_FIELDS
    if extra:
        raise InventoryValidationError(f"campo(s) desconhecido(s) em registro de mês: {sorted(extra)}")
    missing = _MONTH_RECORD_FIELDS - set(value)
    if missing:
        raise InventoryValidationError(f"campo(s) ausente(s) em registro de mês: {sorted(missing)}")
    return MonthlyInventoryEntry(
        requested_month=value["requested_month"],
        status=value["status"],
        bars=value["bars"],
        first_timestamp_utc=value["first_timestamp_utc"],
        last_timestamp_utc=value["last_timestamp_utc"],
        outside_range_count=value["outside_range_count"],
        bars_fingerprint=value["bars_fingerprint"],
        query_attempts=value["query_attempts"],
        stable=value["stable"],
        mt5_last_error=value["mt5_last_error"],
    )


def _require_nonempty_str_field(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryValidationError(f"{field_name} inválido no inventário: {value!r}")
    return value


def _require_true_field(value: Any, field_name: str) -> bool:
    if value is not True:
        raise InventoryValidationError(f"{field_name} deve ser true (recebido: {value!r})")
    return value


def _require_positive_int_field(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InventoryValidationError(f"{field_name} deve ser um inteiro positivo (recebido: {value!r})")
    return value


def _require_mt5_version_field(value: Any) -> list[Any] | None:
    """`mt5_version`: `null` (schema observado) ou uma lista factual não vazia
    de componentes de versão (`int`/`str` — MT5 devolve uma tupla mista de
    versão/build/data)."""

    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise InventoryValidationError(f"mt5_version deve ser uma lista não vazia ou null (recebido: {value!r})")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | str):
            raise InventoryValidationError(f"mt5_version contém elemento inválido: {item!r}")
    return value


def _require_notes_field(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise InventoryValidationError(f"notes deve ser uma lista (recebido: {value!r})")
    for item in value:
        if not isinstance(item, str):
            raise InventoryValidationError(f"notes deve conter só strings (recebido: {item!r})")
    return value


@dataclass(frozen=True)
class WinCopy2Inventory:
    """Inventário independente congelado pelo Fusion Quant, já validado estritamente.

    Exige exatamente `schema`/`symbol`/`timeframe`/`start_month`/`end_month`
    iguais às constantes congeladas do DEV-007, `months` como lista, e
    exatamente um registro por mês de `EXPECTED_INVENTORY_MONTHS` (jan..jul
    de 2026), nem a mais nem a menos. Também exige os campos factuais de
    proveniência da captura observados no schema real —
    `terminal_path`/`terminal_connected`/`terminal_build`/`mt5_version`/
    `notes` — mas nenhum outro campo além destes onze é aceito.
    """

    schema: str
    symbol: str
    timeframe: str
    start_month: str
    end_month: str
    months: dict[str, MonthlyInventoryEntry]
    terminal_path: str
    terminal_connected: bool
    terminal_build: int
    mt5_version: list[Any] | None
    notes: list[str]

    @classmethod
    def from_dict(cls, data: Any) -> WinCopy2Inventory:
        if not isinstance(data, dict):
            raise InventoryValidationError(f"inventário deve ser um objeto JSON (recebido: {data!r})")
        extra = set(data) - _TOP_LEVEL_FIELDS
        if extra:
            raise InventoryValidationError(f"campo(s) desconhecido(s) no inventário: {sorted(extra)}")
        missing = _TOP_LEVEL_FIELDS - set(data)
        if missing:
            raise InventoryValidationError(f"campo(s) ausente(s) no inventário: {sorted(missing)}")

        schema = data["schema"]
        if schema != INVENTORY_SCHEMA:
            raise InventoryValidationError(f"schema do inventário inesperado: esperado {INVENTORY_SCHEMA!r}, recebido {schema!r}")
        symbol = data["symbol"]
        if symbol != INVENTORY_SYMBOL:
            raise InventoryValidationError(f"symbol do inventário inesperado: esperado {INVENTORY_SYMBOL!r}, recebido {symbol!r}")
        timeframe = data["timeframe"]
        if timeframe != INVENTORY_TIMEFRAME:
            raise InventoryValidationError(
                f"timeframe do inventário inesperado: esperado {INVENTORY_TIMEFRAME!r}, recebido {timeframe!r}"
            )
        start_month = data["start_month"]
        if start_month != INVENTORY_START_MONTH:
            raise InventoryValidationError(
                f"start_month do inventário inesperado: esperado {INVENTORY_START_MONTH!r}, recebido {start_month!r}"
            )
        end_month = data["end_month"]
        if end_month != INVENTORY_END_MONTH:
            raise InventoryValidationError(
                f"end_month do inventário inesperado: esperado {INVENTORY_END_MONTH!r}, recebido {end_month!r}"
            )

        # Proveniência factual da captura — validada, mas nunca usada para
        # decidir aceitar/rejeitar barras (isso é sempre contagem +
        # fingerprint integral, por mês).
        terminal_path = _require_nonempty_str_field(data["terminal_path"], "terminal_path")
        terminal_connected = _require_true_field(data["terminal_connected"], "terminal_connected")
        terminal_build = _require_positive_int_field(data["terminal_build"], "terminal_build")
        mt5_version = _require_mt5_version_field(data["mt5_version"])
        notes = _require_notes_field(data["notes"])

        months_raw = data["months"]
        if not isinstance(months_raw, list):
            raise InventoryValidationError("'months' deve ser uma lista")

        entries: dict[str, MonthlyInventoryEntry] = {}
        for value in months_raw:
            entry = _entry_from_dict(value)
            if entry.requested_month in entries:
                raise InventoryValidationError(f"requested_month duplicado no inventário: {entry.requested_month!r}")
            entries[entry.requested_month] = entry

        seen_months = set(entries)
        expected_months = set(EXPECTED_INVENTORY_MONTHS)
        missing_months = sorted(expected_months - seen_months)
        extra_months = sorted(seen_months - expected_months)
        if missing_months or extra_months:
            raise InventoryValidationError(
                "meses do inventário divergem de jan..jul/2026 (exatamente uma vez cada): "
                f"ausentes={missing_months}, inesperados={extra_months}"
            )

        return cls(
            schema=schema, symbol=symbol, timeframe=timeframe,
            start_month=start_month, end_month=end_month, months=entries,
            terminal_path=terminal_path, terminal_connected=terminal_connected,
            terminal_build=terminal_build, mt5_version=mt5_version, notes=notes,
        )


def load_inventory_file(path: Path, *, expected_sha256: str) -> WinCopy2Inventory:
    """Lê `path`, confere o SHA-256 do arquivo inteiro e só então faz o parse.

    O hash é conferido sobre os bytes crus do arquivo — antes de qualquer
    tentativa de decodificar JSON — para que um arquivo adulterado nunca
    chegue a ser interpretado como um inventário válido, mesmo que o JSON em
    si continue bem formado.
    """

    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise InventoryValidationError(f"não foi possível ler o inventário: {path}: {exc}") from exc

    if not isinstance(expected_sha256, str) or not _HEX64_RE.match(expected_sha256.strip()):
        raise InventoryValidationError(
            f"expected_sha256 deve ser um hex SHA-256 de 64 caracteres (recebido: {expected_sha256!r})"
        )
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256.lower() != expected_sha256.strip().lower():
        raise InventoryHashMismatchError(
            f"SHA-256 do inventário divergente: esperado {expected_sha256.strip().lower()}, "
            f"calculado {actual_sha256} ({path})"
        )

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryValidationError(f"inventário não é um JSON UTF-8 válido: {path}: {exc}") from exc
    return WinCopy2Inventory.from_dict(data)


def assert_month_matches_inventory(
    inventory: WinCopy2Inventory, month: str, rows: Sequence[RawM1Row]
) -> MonthlyInventoryEntry:
    """Confere `rows` (já ordenadas/validadas) contra o inventário para `month`.

    Compara sempre pelo fingerprint **integral** (nunca pelo prefixo, que é
    só evidência humana). Levanta `InventoryMonthMismatchError` com a
    contagem e os prefixos (para leitura humana no log) em qualquer
    divergência — mês ausente, contagem errada ou fingerprint diferente.
    """

    entry = inventory.months.get(month)
    if entry is None:
        raise InventoryMonthMismatchError(f"mês {month} ausente no inventário congelado")

    actual_count = len(rows)
    actual_fingerprint = compute_month_fingerprint(rows)

    if actual_count != entry.bars:
        raise InventoryMonthMismatchError(
            f"{month}: contagem divergente do inventário: esperado {entry.bars}, obtido {actual_count}"
        )
    if actual_fingerprint.upper() != entry.bars_fingerprint.upper():
        raise InventoryMonthMismatchError(
            f"{month}: fingerprint divergente do inventário "
            f"(prefixo esperado {fingerprint_human_prefix(entry.bars_fingerprint)}, "
            f"obtido {fingerprint_human_prefix(actual_fingerprint)})"
        )
    return entry
