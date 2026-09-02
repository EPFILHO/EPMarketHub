"""Núcleo do MVP quantitativo WIN (DEV-006): ticks brutos → barras/features/relatório.

Este módulo é o produtor de uma etapa de pesquisa local e determinística.
Não importa `MetaTrader5` nem Qt. Depende de `pyarrow` (leitura dos Parquets
brutos e escrita dos artefatos analíticos) e reaproveita deliberadamente:

- `market_analytics.bars.Bar` / `market_analytics.config.FeatureConfig` /
  `market_analytics.pipeline.compute_feature_rows` para não reimplementar
  ATR, volatilidade realizada, efficiency ratio ou volume relativo;
- `market_analytics.backfill_writer.inspect_final_file` para identidade
  verificável (hash/contagem) de cada Parquet bruto de origem, sem duplicar
  a leitura segura de metadados Parquet já validada no Portão A.

Escopo travado (ver `docs/work_orders/DEV-006.md`): somente
`raw/clear/win/year=*/month=*/session_date=*/ticks.parquet`. Nenhum outro
`source_id`/`logical_id`/símbolo é aceito.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .backfill_writer import BackfillWriteError, InspectedFile, inspect_final_file
from .bars import Bar
from .config import FeatureConfig
from .features import FeatureRow
from .pipeline import compute_feature_rows

# Identidade obrigatória da fonte (escopo travado do DEV-006).
EXPECTED_SCHEMA = "ep_market_hub.raw_ticks"
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1"})
EXPECTED_SOURCE_ID = "clear"
EXPECTED_LOGICAL_ID = "win"
EXPECTED_RESOLVED_SYMBOL = "WIN$"

# Versão do próprio relatório MVP (não confundir com o schema bruto v1).
RUN_SUMMARY_SCHEMA = "ep_market_hub.quant_mvp_run"
RUN_SUMMARY_SCHEMA_VERSION = 1
PRODUCER_VERSION = "dev-006-mvp-1"

TIMEFRAMES: tuple[str, ...] = ("M1", "M5", "M15", "M30", "H1")
_TIMEFRAME_MINUTES: dict[str, int] = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}

_SESSION_DIR_RE = re.compile(r"^session_date=(\d{4}-\d{2}-\d{2})$")

PRICE_POLICY = (
    "preço operacional = last (somente valores finitos e > 0); "
    "não depende de bit isolado de flags (valores compostos 1080/1336 são "
    "aceitos quando last/volume_real são válidos)."
)
VOLUME_POLICY = (
    "volume = soma de volume_real finito e não negativo das ticks válidas "
    "(last > 0); volume_real inválido contribui como 0 sem invalidar a tick."
)
DEDUP_POLICY = (
    "remove somente duplicatas exatas adjacentes (todos os campos brutos "
    "iguais ao registro imediatamente anterior), inclusive na fronteira "
    "entre batches; empates de time_msc com conteúdo diferente são válidos."
)

_FEATURE_NUMERIC_FIELDS: tuple[str, ...] = (
    "true_range",
    "atr",
    "atr_normalized",
    "log_return",
    "realized_volatility",
    "normalized_range",
    "efficiency_ratio",
    "trend_strength",
    "close_position",
    "volume_relative",
)


class QuantMvpError(Exception):
    """Erro estruturado do pipeline quant MVP."""


class SessionRejectedError(QuantMvpError):
    """Uma sessão descoberta foi rejeitada (metadado inválido, ordem quebrada, arquivo ilegível).

    Sempre carrega `path`/`reason` para que o chamador registre um alerta e
    siga para a próxima sessão em vez de abortar o lote inteiro.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class SessionTickStats:
    """Contagens de ticks de uma sessão, reconciliáveis com o Parquet de origem."""

    ticks_read: int
    ticks_valid: int
    ticks_duplicated: int


@dataclass(frozen=True)
class SessionOutcome:
    """Resultado do processamento de uma sessão: barras por timeframe + contagens.

    `first_tick_utc`/`last_tick_utc` são os timestamps exatos (com
    segundo/milissegundo, não truncados ao minuto) do primeiro e do último
    tick válido da sessão — distintos do `timestamp` (início do minuto) da
    primeira/última barra M1. `None` quando `stats.ticks_valid == 0`.
    """

    session_date: date
    path: Path
    stats: SessionTickStats
    bars_by_timeframe: dict[str, list[Bar]]
    source: InspectedFile
    first_tick_utc: datetime | None
    last_tick_utc: datetime | None


def discover_sessions(input_root: Path) -> list[tuple[date, Path]]:
    """Descobre somente `year=*/month=*/session_date=*/ticks.parquet` sob `input_root`.

    Retorna pares `(session_date, path)` ordenados por `session_date`. Nunca
    segue outros layouts (`win_contract_*`, WDO, benchmarks): o chamador é
    responsável por apontar `input_root` diretamente para a pasta do
    `logical_id` esperado (ex.: `raw/clear/win`), como no exemplo de CLI do
    work order.
    """

    input_root = Path(input_root)
    found: list[tuple[date, Path]] = []
    for path in input_root.glob("year=*/month=*/session_date=*/ticks.parquet"):
        match = _SESSION_DIR_RE.match(path.parent.name)
        if not match:
            continue
        found.append((date.fromisoformat(match.group(1)), path))
    found.sort(key=lambda item: (item[0], str(item[1])))
    return found


def _open_parquet_file(path: Path) -> tuple[pq.ParquetFile, pa.OSFile]:
    try:
        handle = pa.OSFile(str(path), mode="r")
    except (pa.ArrowException, OSError) as exc:
        raise SessionRejectedError(path, f"falha ao abrir o arquivo: {exc}") from exc
    try:
        parquet_file = pq.ParquetFile(handle)
    except (pa.ArrowException, OSError) as exc:
        try:
            handle.close()
        except (pa.ArrowException, OSError):
            pass
        raise SessionRejectedError(path, f"Parquet corrompido ou inválido: {exc}") from exc
    return parquet_file, handle


def _decode_metadata(schema: pa.Schema) -> dict[str, str]:
    raw = schema.metadata or {}
    return {
        key.decode("utf-8", errors="replace"): value.decode("utf-8", errors="replace")
        for key, value in raw.items()
    }


def validate_session_metadata(metadata: dict[str, str], *, path: Path, session_date: date) -> None:
    """Valida os metadados essenciais antes de qualquer processamento.

    Recusa (via `SessionRejectedError`) schema/versão/`source_id`/
    `logical_id`/`resolved_symbol` fora do esperado, ou `session_date`
    embutido divergente da partição no caminho — nunca assume silenciosamente
    que um arquivo pertence à série WIN$/Clear só porque está no caminho
    certo.
    """

    def require(key: str, expected: str | None = None) -> str:
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SessionRejectedError(path, f"metadado obrigatório ausente ou vazio: {key!r}")
        if expected is not None and value != expected:
            raise SessionRejectedError(
                path, f"{key} inesperado: esperado {expected!r}, encontrado {value!r}"
            )
        return value

    require("schema", EXPECTED_SCHEMA)
    schema_version = require("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SessionRejectedError(
            path,
            f"schema_version não suportada: {schema_version!r} "
            f"(suportadas: {sorted(SUPPORTED_SCHEMA_VERSIONS)})",
        )
    require("source_id", EXPECTED_SOURCE_ID)
    require("logical_id", EXPECTED_LOGICAL_ID)
    require("resolved_symbol", EXPECTED_RESOLVED_SYMBOL)
    embedded_session_date = require("session_date")
    if embedded_session_date != session_date.isoformat():
        raise SessionRejectedError(
            path,
            "session_date do metadado divergente da partição: "
            f"metadado={embedded_session_date!r}, partição={session_date.isoformat()!r}",
        )


class _M1Builder:
    """Constrói barras M1 fechadas a partir de um fluxo ordenado de ticks válidos.

    Ancorado no relógio UTC: cada tick cai no minuto `timestamp` truncado a
    segundo/microsegundo zero. Nunca preenche minutos sem negócio.
    """

    def __init__(self, *, source_id: str, symbol: str) -> None:
        self._source_id = source_id
        self._symbol = symbol
        self._bucket_start: datetime | None = None
        self._open = 0.0
        self._high = 0.0
        self._low = 0.0
        self._close = 0.0
        self._volume = 0.0
        self._bars: list[Bar] = []

    def add(self, timestamp: datetime, price: float, volume: float) -> None:
        bucket = timestamp.replace(second=0, microsecond=0)
        if self._bucket_start is None or bucket != self._bucket_start:
            self._flush()
            self._bucket_start = bucket
            self._open = self._high = self._low = price
            self._volume = 0.0
        else:
            self._high = max(self._high, price)
            self._low = min(self._low, price)
        self._close = price
        self._volume += volume

    def _flush(self) -> None:
        if self._bucket_start is None:
            return
        self._bars.append(
            Bar(
                source_id=self._source_id,
                symbol=self._symbol,
                timeframe="M1",
                timestamp=self._bucket_start,
                open=self._open,
                high=self._high,
                low=self._low,
                close=self._close,
                volume=self._volume,
                volume_quality="exchange",
            )
        )

    def finish(self) -> list[Bar]:
        self._flush()
        self._bucket_start = None
        return self._bars


def aggregate_bars(bars: Sequence[Bar], timeframe: str) -> list[Bar]:
    """Deriva `timeframe` (M5/M15/M30/H1) a partir de uma sequência de barras M1.

    Puramente aritmético sobre o relógio UTC de cada barra M1: nunca cria um
    bucket vazio, nunca olha para fora de `bars`. `bars` deve já estar
    ordenada por `timestamp` (uma única sessão ou a concatenação
    cronológica de várias).

    A qualidade de volume do bucket agregado nunca é presumida: se todas as
    barras M1 do bucket compartilham a mesma `volume_quality` (`"exchange"`
    ou `"tick_proxy"`), o volume é somado e essa qualidade é preservada. Se o
    bucket mistura qualidades diferentes ou contém qualquer barra
    `"missing"`, somar os volumes seria combinar grandezas incompatíveis
    (ex.: contagem de ticks com volume real) — o agregado sai com
    `volume=None`/`volume_quality="missing"`, nunca com uma soma inventada
    (auditoria Codex do DEV-007: a versão anterior sempre gravava
    `"exchange"`, mesmo agregando barras `"tick_proxy"`).
    """

    if timeframe not in _TIMEFRAME_MINUTES:
        raise ValueError(f"timeframe desconhecido: {timeframe!r}")
    minutes = _TIMEFRAME_MINUTES[timeframe]
    if minutes == 1:
        return list(bars)

    result: list[Bar] = []
    bucket_start: datetime | None = None
    open_ = high = low = close = 0.0
    volume = 0.0
    qualities: set[str] = set()

    def bucket_of(ts: datetime) -> datetime:
        total_minutes = ts.hour * 60 + ts.minute
        floored = (total_minutes // minutes) * minutes
        return ts.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)

    def flush(source_id: str, symbol: str) -> None:
        if len(qualities) == 1 and "missing" not in qualities:
            bucket_volume: float | None = volume
            bucket_quality = next(iter(qualities))
        else:
            bucket_volume = None
            bucket_quality = "missing"
        result.append(
            Bar(
                source_id=source_id,
                symbol=symbol,
                timeframe=timeframe,
                timestamp=bucket_start,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=bucket_volume,
                volume_quality=bucket_quality,
            )
        )

    for bar in bars:
        bucket = bucket_of(bar.timestamp)
        if bucket_start is None:
            bucket_start, open_, high, low = bucket, bar.open, bar.high, bar.low
            volume = 0.0
            qualities = set()
        elif bucket != bucket_start:
            flush(bar.source_id, bar.symbol)
            bucket_start, open_, high, low = bucket, bar.open, bar.high, bar.low
            volume = 0.0
            qualities = set()
        else:
            high = max(high, bar.high)
            low = min(low, bar.low)
        close = bar.close
        volume += bar.volume or 0.0
        qualities.add(bar.volume_quality)

    if bucket_start is not None:
        flush(bars[-1].source_id, bars[-1].symbol)
    return result


def process_session(path: Path, *, session_date: date, batch_size: int = 200_000) -> SessionOutcome:
    """Lê uma sessão por row group/batches e produz barras M1..H1.

    Nunca materializa os ticks inteiros da sessão em memória: processa cada
    batch e descarta seu conteúdo bruto assim que consumido. Levanta
    `SessionRejectedError` para qualquer metadado inválido, arquivo
    corrompido ou timestamps fora de ordem — o chamador decide se isso
    interrompe só esta sessão ou o lote inteiro.
    """

    path = Path(path)
    parquet_file, handle = _open_parquet_file(path)
    try:
        metadata = _decode_metadata(parquet_file.schema_arrow)
        validate_session_metadata(metadata, path=path, session_date=session_date)

        builder = _M1Builder(source_id=EXPECTED_SOURCE_ID, symbol=EXPECTED_RESOLVED_SYMBOL)
        ticks_read = 0
        ticks_valid = 0
        ticks_duplicated = 0
        first_tick_utc: datetime | None = None
        last_tick_utc: datetime | None = None
        previous_row: tuple[Any, ...] | None = None
        previous_time_msc: int | None = None

        try:
            for batch in parquet_file.iter_batches(batch_size=batch_size):
                columns = batch.to_pydict()
                length = batch.num_rows
                for index in range(length):
                    row = (
                        columns["time"][index],
                        columns["time_msc"][index],
                        columns["bid"][index],
                        columns["ask"][index],
                        columns["last"][index],
                        columns["volume"][index],
                        columns["volume_real"][index],
                        columns["flags"][index],
                    )
                    ticks_read += 1
                    time_msc = row[1]
                    if previous_time_msc is not None and time_msc < previous_time_msc:
                        raise SessionRejectedError(
                            path,
                            "timestamps fora de ordem: "
                            f"{previous_time_msc} seguido de {time_msc}",
                        )
                    if previous_row is not None and row == previous_row:
                        ticks_duplicated += 1
                        previous_row = row
                        previous_time_msc = time_msc
                        continue
                    previous_row = row
                    previous_time_msc = time_msc

                    last_price = row[4]
                    if (
                        isinstance(last_price, bool)
                        or not isinstance(last_price, int | float)
                        or not math.isfinite(last_price)
                        or last_price <= 0
                    ):
                        continue
                    ticks_valid += 1

                    volume_real = row[6]
                    if (
                        isinstance(volume_real, bool)
                        or not isinstance(volume_real, int | float)
                        or not math.isfinite(volume_real)
                        or volume_real < 0
                    ):
                        volume_contribution = 0.0
                    else:
                        volume_contribution = float(volume_real)

                    timestamp = datetime.fromtimestamp(time_msc / 1000.0, tz=UTC)
                    if first_tick_utc is None:
                        first_tick_utc = timestamp
                    last_tick_utc = timestamp
                    builder.add(timestamp, float(last_price), volume_contribution)
        except (pa.ArrowException, OSError, ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, SessionRejectedError):
                raise
            raise SessionRejectedError(path, f"falha ao ler ticks: {exc}") from exc

        m1_bars = builder.finish()
        bars_by_timeframe = {"M1": m1_bars}
        for timeframe in TIMEFRAMES[1:]:
            bars_by_timeframe[timeframe] = aggregate_bars(m1_bars, timeframe)

        try:
            source = inspect_final_file(path)
        except BackfillWriteError as exc:
            raise SessionRejectedError(path, f"falha ao inspecionar arquivo de origem: {exc}") from exc
        if source is None:
            raise SessionRejectedError(path, "arquivo desapareceu durante o processamento")
        if source.row_count != ticks_read:
            raise SessionRejectedError(
                path,
                f"contagem de linhas divergente: lidas {ticks_read}, arquivo reporta {source.row_count}",
            )

        stats = SessionTickStats(ticks_read=ticks_read, ticks_valid=ticks_valid, ticks_duplicated=ticks_duplicated)
        return SessionOutcome(
            session_date=session_date,
            path=path,
            stats=stats,
            bars_by_timeframe=bars_by_timeframe,
            source=source,
            first_tick_utc=first_tick_utc,
            last_tick_utc=last_tick_utc,
        )
    finally:
        try:
            parquet_file.close()
        except (pa.ArrowException, OSError):
            pass
        try:
            handle.close()
        except (pa.ArrowException, OSError):
            pass


def _percentile(sorted_values: list[float], p: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (p / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[int(rank)]
    lower_value = sorted_values[int(lower)] * (upper - rank)
    upper_value = sorted_values[int(upper)] * (rank - lower)
    return lower_value + upper_value


def build_feature_summary(feature_rows_by_timeframe: dict[str, list[FeatureRow]]) -> list[dict[str, Any]]:
    """Contagem, ausentes e percentis p10/p25/p50/p75/p90 por timeframe/feature."""

    rows: list[dict[str, Any]] = []
    for timeframe in TIMEFRAMES:
        feature_rows = feature_rows_by_timeframe.get(timeframe, [])
        total = len(feature_rows)
        for field_name in _FEATURE_NUMERIC_FIELDS:
            values = [getattr(row, field_name) for row in feature_rows]
            present = sorted(value for value in values if value is not None)
            missing = total - len(present)
            record: dict[str, Any] = {
                "timeframe": timeframe,
                "feature": field_name,
                "count": total,
                "missing": missing,
            }
            for label, p in (("p10", 10), ("p25", 25), ("p50", 50), ("p75", 75), ("p90", 90)):
                record[label] = _percentile(present, p) if present else None
            rows.append(record)
    return rows


def build_session_summary(outcomes: Sequence[SessionOutcome]) -> list[dict[str, Any]]:
    """Uma linha por sessão: OHLC/retorno/amplitude, contagens de ticks, volume,
    primeira/última observação e quantidade de barras por timeframe.

    `first_observation_utc`/`last_observation_utc` são os timestamps exatos
    do primeiro/último tick válido (`SessionOutcome.first_tick_utc`/
    `last_tick_utc`) — nunca o início do minuto da barra M1, que já perde a
    precisão de segundo/milissegundo do tick original.
    """

    rows: list[dict[str, Any]] = []
    for outcome in outcomes:
        m1_bars = outcome.bars_by_timeframe.get("M1", [])
        if m1_bars:
            open_price = m1_bars[0].open
            close_price = m1_bars[-1].close
            high_price = max(bar.high for bar in m1_bars)
            low_price = min(bar.low for bar in m1_bars)
            volume = sum(bar.volume or 0.0 for bar in m1_bars)
            return_value = (close_price - open_price) / open_price if open_price else None
            range_value = high_price - low_price
        else:
            open_price = close_price = high_price = low_price = None
            volume = 0.0
            return_value = None
            range_value = None
        first_observation = outcome.first_tick_utc
        last_observation = outcome.last_tick_utc

        row: dict[str, Any] = {
            "session_date": outcome.session_date.isoformat(),
            "source_id": EXPECTED_SOURCE_ID,
            "symbol": EXPECTED_RESOLVED_SYMBOL,
            "ticks_read": outcome.stats.ticks_read,
            "ticks_valid": outcome.stats.ticks_valid,
            "ticks_duplicated": outcome.stats.ticks_duplicated,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "return": return_value,
            "range": range_value,
            "volume": volume,
            "first_observation_utc": first_observation.isoformat() if first_observation else None,
            "last_observation_utc": last_observation.isoformat() if last_observation else None,
        }
        for timeframe in TIMEFRAMES:
            row[f"bars_{timeframe.lower()}"] = len(outcome.bars_by_timeframe.get(timeframe, []))
        rows.append(row)
    return rows


_SESSION_SUMMARY_FIELDS: tuple[str, ...] = (
    "session_date",
    "source_id",
    "symbol",
    "ticks_read",
    "ticks_valid",
    "ticks_duplicated",
    "open",
    "high",
    "low",
    "close",
    "return",
    "range",
    "volume",
    "first_observation_utc",
    "last_observation_utc",
    "bars_m1",
    "bars_m5",
    "bars_m15",
    "bars_m30",
    "bars_h1",
)

_FEATURE_SUMMARY_FIELDS: tuple[str, ...] = (
    "timeframe",
    "feature",
    "count",
    "missing",
    "p10",
    "p25",
    "p50",
    "p75",
    "p90",
)


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    import csv

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_bars_features_parquet(
    path: Path,
    *,
    timeframe: str,
    bars: list[Bar],
    feature_rows: list[FeatureRow],
    session_dates: list[str],
) -> None:
    """Grava barras+features num Parquet, com `timestamp_utc` como coluna
    Arrow `timestamp(us, tz=UTC)` timezone-aware — não uma string ISO —
    para que o consumidor (Fusion Quant) não precise reanalisar texto para
    obter um tipo de tempo nativo comparável/ordenável."""

    columns: dict[str, Any] = {
        "session_date": session_dates,
        "source_id": [bar.source_id for bar in bars],
        "symbol": [bar.symbol for bar in bars],
        "timeframe": [bar.timeframe for bar in bars],
        "timestamp_utc": pa.array([bar.timestamp for bar in bars], type=pa.timestamp("us", tz="UTC")),
        "open": [bar.open for bar in bars],
        "high": [bar.high for bar in bars],
        "low": [bar.low for bar in bars],
        "close": [bar.close for bar in bars],
        "volume": [bar.volume for bar in bars],
        "volume_quality": [bar.volume_quality for bar in bars],
    }
    for field_name in _FEATURE_NUMERIC_FIELDS:
        columns[field_name] = [getattr(row, field_name) for row in feature_rows]
    table = pa.table(columns)
    pq.write_table(table, str(path), compression="zstd")


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _paths_overlap(a: Path, b: Path) -> bool:
    """`True` se `a` e `b` são o mesmo caminho ou um está contido no outro.

    Comparação puramente lexical sobre caminhos já resolvidos
    (`Path.resolve`) — suficiente para recusar sobreposição óbvia entre
    entrada e saída em qualquer direção, sem exigir que os caminhos
    existam no disco.
    """

    for x, y in ((a, b), (b, a)):
        try:
            x.relative_to(y)
        except ValueError:
            continue
        return True
    return False


def _validate_run_arguments(*, input_root: Path, output_root: Path, batch_size: int) -> tuple[Path, Path]:
    """Normaliza `input_root`/`output_root` e recusa combinações destrutivas.

    Chamado antes de qualquer `mkdir` ou leitura: um `batch_size` inválido,
    uma saída igual/sobreposta à entrada (em qualquer direção) ou uma saída
    igual à raiz de um volume nunca chegam a tocar o disco. Retorna os
    caminhos já resolvidos (`Path.resolve`), para que o restante da função
    opere sobre a mesma identidade validada aqui — nunca revalida com um
    caminho relativo que possa ter mudado de significado entre a validação
    e o uso.
    """

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise QuantMvpError(f"batch_size deve ser um inteiro positivo (recebido: {batch_size!r})")

    resolved_input = Path(input_root).expanduser().resolve(strict=False)
    resolved_output = Path(output_root).expanduser().resolve(strict=False)

    if resolved_output.parent == resolved_output:
        raise QuantMvpError(
            f"output_root não pode ser a raiz de um volume: {resolved_output}"
        )
    if _paths_overlap(resolved_input, resolved_output):
        raise QuantMvpError(
            "output_root não pode ser igual a input_root nem estar contido/conter "
            f"input_root: input_root={resolved_input}, output_root={resolved_output}"
        )

    return resolved_input, resolved_output


def _promote_output(temp_dir: Path, output_root: Path) -> None:
    """Promove `temp_dir` para `output_root` com recuperação transacional.

    Um diretório não pode ser substituído por outro como uma única operação
    atômica no Windows (não há um equivalente de `renameat2`/
    `RENAME_EXCHANGE` exposto pela biblioteca padrão para diretórios em
    NTFS) — por isso a promoção usa duas trocas, cada uma atômica
    individualmente (`os.rename` dentro do mesmo volume), com um backup
    intermediário que garante recuperação total se a segunda troca falhar:

    1. Se `output_root` já existir, ele é renomeado (atômico) para um
       backup único no mesmo diretório pai.
    2. `temp_dir` é renomeado (atômico) para `output_root`.
    3. Se o passo 2 falhar, o backup do passo 1 é restaurado para
       `output_root` antes de propagar o erro — a saída anterior nunca é
       perdida nem fica ausente.
    4. O backup só é removido depois que o passo 2 tiver sucesso.

    Isto NÃO é atomicidade de diretório de ponta a ponta: existe uma janela
    real entre os passos 1 e 2 em que nada existe sob o nome final de
    `output_root`. A garantia real é de recuperabilidade — qualquer falha
    deixa o sistema num dos dois estados válidos e completos conhecidos (a
    saída antiga restaurada, ou a nova already promovida), nunca num estado
    parcial ou corrompido.
    """

    output_root = Path(output_root)
    temp_dir = Path(temp_dir)
    backup_path = output_root.with_name(f"{output_root.name}.quant_mvp_backup_{temp_dir.name.lstrip('.')}")

    had_previous = output_root.exists()
    if had_previous:
        os.rename(output_root, backup_path)

    try:
        os.rename(temp_dir, output_root)
    except OSError:
        if had_previous:
            os.rename(backup_path, output_root)
        raise

    if had_previous:
        shutil.rmtree(backup_path, ignore_errors=True)


@dataclass
class RunProgress:
    """Evento de progresso emitido por sessão (consumido pela CLI, nunca por uma GUI)."""

    session_date: date
    path: Path
    status: str
    detail: str = ""


def run_quant_mvp(
    *,
    input_root: Path,
    output_root: Path,
    feature_config: FeatureConfig | None = None,
    batch_size: int = 200_000,
    progress: Callable[[RunProgress], None] | None = None,
) -> dict[str, Any]:
    """Orquestra o MVP completo: descoberta → barras → features → artefatos.

    Antes de tocar o disco, normaliza e valida `input_root`/`output_root`/
    `batch_size` (ver `_validate_run_arguments`): recusa `batch_size<=0`,
    uma saída igual/sobreposta à entrada em qualquer direção, e uma saída
    igual à raiz de um volume. Grava tudo primeiro num diretório temporário
    e só promove para `output_root` depois que todos os artefatos foram
    escritos com sucesso, com recuperação transacional (`_promote_output`)
    — uma falha na promoção restaura a saída anterior, nunca a perde. Uma
    falha em qualquer etapa anterior remove o temporário e nunca toca
    `output_root`. Os Parquets brutos sob `input_root` nunca são escritos.
    """

    started_at = datetime.now(UTC)
    config = feature_config if feature_config is not None else FeatureConfig()
    input_root, output_root = _validate_run_arguments(
        input_root=input_root, output_root=output_root, batch_size=batch_size
    )

    sessions = discover_sessions(input_root)
    if not sessions:
        raise QuantMvpError(f"nenhuma sessão encontrada sob {input_root}")

    outcomes: list[SessionOutcome] = []
    alerts: list[str] = []
    for session_date, path in sessions:
        try:
            outcome = process_session(path, session_date=session_date, batch_size=batch_size)
        except SessionRejectedError as exc:
            alerts.append(f"sessão rejeitada: {exc.path}: {exc.reason}")
            if progress is not None:
                progress(RunProgress(session_date=session_date, path=path, status="rejected", detail=exc.reason))
            continue
        outcomes.append(outcome)
        if progress is not None:
            progress(
                RunProgress(
                    session_date=session_date,
                    path=path,
                    status="ok",
                    detail=f"ticks_validos={outcome.stats.ticks_valid} barras_m1={len(outcome.bars_by_timeframe['M1'])}",
                )
            )

    if not outcomes:
        raise QuantMvpError("todas as sessões descobertas foram rejeitadas; nada para processar")

    bars_by_timeframe: dict[str, list[Bar]] = {timeframe: [] for timeframe in TIMEFRAMES}
    session_date_by_timeframe: dict[str, list[str]] = {timeframe: [] for timeframe in TIMEFRAMES}
    for outcome in outcomes:
        for timeframe in TIMEFRAMES:
            bars = outcome.bars_by_timeframe.get(timeframe, [])
            bars_by_timeframe[timeframe].extend(bars)
            session_date_by_timeframe[timeframe].extend([outcome.session_date.isoformat()] * len(bars))

    feature_rows_by_timeframe: dict[str, list[FeatureRow]] = {}
    for timeframe in TIMEFRAMES:
        feature_rows_by_timeframe[timeframe] = compute_feature_rows(bars_by_timeframe[timeframe], config)

    output_root.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".quant_mvp_tmp_", dir=str(output_root.parent)))
    try:
        artifact_paths: dict[str, Path] = {}
        for timeframe in TIMEFRAMES:
            artifact_path = temp_dir / f"bars_features_{timeframe}.parquet"
            _write_bars_features_parquet(
                artifact_path,
                timeframe=timeframe,
                bars=bars_by_timeframe[timeframe],
                feature_rows=feature_rows_by_timeframe[timeframe],
                session_dates=session_date_by_timeframe[timeframe],
            )
            artifact_paths[f"bars_features_{timeframe}"] = artifact_path

        session_summary_rows = build_session_summary(outcomes)
        session_summary_path = temp_dir / "session_summary.csv"
        _write_csv(session_summary_path, _SESSION_SUMMARY_FIELDS, session_summary_rows)
        artifact_paths["session_summary"] = session_summary_path

        feature_summary_rows = build_feature_summary(feature_rows_by_timeframe)
        feature_summary_path = temp_dir / "feature_summary.csv"
        _write_csv(feature_summary_path, _FEATURE_SUMMARY_FIELDS, feature_summary_rows)
        artifact_paths["feature_summary"] = feature_summary_path

        finished_at = datetime.now(UTC)
        artifacts_manifest = {
            name: {
                "path": path.name,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for name, path in artifact_paths.items()
        }

        totals = {
            "sessions_discovered": len(sessions),
            "sessions_processed": len(outcomes),
            "sessions_rejected": len(sessions) - len(outcomes),
            "ticks_read": sum(outcome.stats.ticks_read for outcome in outcomes),
            "ticks_valid": sum(outcome.stats.ticks_valid for outcome in outcomes),
            "ticks_duplicated": sum(outcome.stats.ticks_duplicated for outcome in outcomes),
            "bars": {timeframe: len(bars_by_timeframe[timeframe]) for timeframe in TIMEFRAMES},
        }

        inputs_manifest = [
            {
                "session_date": outcome.session_date.isoformat(),
                "path": str(outcome.path),
                "size_bytes": outcome.source.size_bytes,
                "sha256": outcome.source.sha256,
                "row_count": outcome.source.row_count,
                "ticks_valid": outcome.stats.ticks_valid,
                "ticks_duplicated": outcome.stats.ticks_duplicated,
            }
            for outcome in outcomes
        ]

        run_summary: dict[str, Any] = {
            "schema": RUN_SUMMARY_SCHEMA,
            "schema_version": RUN_SUMMARY_SCHEMA_VERSION,
            "producer_version": PRODUCER_VERSION,
            "generated_at_utc": finished_at.isoformat(),
            "duration_seconds": (finished_at - started_at).total_seconds(),
            "input_root": str(input_root),
            "output_root": str(output_root),
            "feature_config": config.to_dict(),
            "price_policy": PRICE_POLICY,
            "volume_policy": VOLUME_POLICY,
            "dedup_policy": DEDUP_POLICY,
            "totals": totals,
            "alerts": alerts,
            "inputs": inputs_manifest,
            "artifacts": artifacts_manifest,
        }
        run_summary_path = temp_dir / "run_summary.json"
        run_summary_path.write_text(
            json.dumps(run_summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )

        _promote_output(temp_dir, output_root)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return run_summary
