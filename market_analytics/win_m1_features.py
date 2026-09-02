"""Orquestrador do produtor histórico M1→features do WIN$ (DEV-007, entregas A+B).

Junta o coletor (`win_m1_collector.py`), o fingerprint/inventário
(`win_m1_inventory.py`) e o pipeline de features já existente
(`market_analytics.pipeline.compute_feature_rows`,
`market_analytics.quant_mvp.aggregate_bars`) numa única execução
determinística: para cada mês da janela congelada, busca e valida as barras
M1, confere contagem/fingerprint contra o inventário do Fusion Quant,
concatena os meses numa única série contínua (o warm-up de features nunca
reinicia no primeiro dia de um mês), agrega para M5 e escreve os artefatos
exigidos sob um diretório de saída fornecido explicitamente pelo chamador.

Este módulo não importa `MetaTrader5`, Qt nem toca GUI/bridge/worker/kernel.
Não reimplementa ATR, volatilidade, efficiency ratio, trend strength nem a
agregação de timeframes — reaproveita deliberadamente `Bar`, `FeatureConfig`,
`compute_feature_rows` e `aggregate_bars`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .bars import Bar
from .config import FeatureConfig
from .features import FeatureRow
from .pipeline import compute_feature_rows
from .quant_mvp import aggregate_bars
from .win_m1_collector import (
    FROZEN_WINDOW_END_EXCLUSIVE,
    FROZEN_WINDOW_START,
    MonthRejectedError,
    RatesProvider,
    build_month_bars,
    fetch_and_validate_month,
    month_windows,
)
from .win_m1_inventory import (
    INVENTORY_SYMBOL,
    InventoryMonthMismatchError,
    RawM1Row,
    WinCopy2Inventory,
    compute_month_fingerprint,
    fingerprint_human_prefix,
    load_inventory_file,
)

RUN_SUMMARY_SCHEMA = "ep_market_hub.win_m1_history_run"
RUN_SUMMARY_SCHEMA_VERSION = 1
PRODUCER_VERSION = "dev-007-win-m1-history-1"

M1_BARS_SCHEMA = "ep_market_hub.win_m1_bars"
M1_BARS_SCHEMA_VERSION = 1
LOGICAL_ID = "win"

_TIMEFRAME_MINUTES: dict[str, int] = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}
FEATURES_TIMEFRAME = "M5"

VOLUME_POLICY = (
    "volume_quality='exchange' com volume=real_volume quando real_volume for um "
    "inteiro não negativo; caso contrário volume_quality='tick_proxy' com "
    "volume=tick_volume. Decisão por barra (nunca por mês/lote inteiro); "
    "'exchange' e 'tick_proxy' nunca se misturam dentro da mesma janela de "
    "volume_relative (garantido por market_analytics.features.volume_relative)."
)
AVAILABILITY_POLICY = (
    "timestamp da barra = abertura do bucket (UTC); available_at_utc = "
    "timestamp + duração do timeframe = encerramento do bucket. O consumidor "
    "deve usar somente barras com available_at_utc <= instante da decisão."
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


class HistoryProducerError(Exception):
    """Erro estruturado do orquestrador (argumentos inválidos, promoção falhou)."""


@dataclass
class ProducerProgress:
    """Evento de progresso emitido por mês (consumido só pela CLI, nunca por GUI)."""

    month: str
    status: str
    detail: str = ""


def available_at_utc(bar: Bar) -> datetime:
    """Instante em que `bar` (já fechada) fica disponível para o consumidor.

    Igual ao encerramento temporal do bucket: `bar.timestamp` é a abertura,
    então `available_at_utc` soma a duração do timeframe. Uma barra M5 aberta
    às 09:05 só fica disponível às 09:10 — antes disso, a última M5
    disponível continua sendo a aberta às 09:00 (fecha às 09:05).
    """

    minutes = _TIMEFRAME_MINUTES.get(bar.timeframe)
    if minutes is None:
        raise ValueError(f"timeframe sem duração conhecida: {bar.timeframe!r}")
    return bar.timestamp + timedelta(minutes=minutes)


def _month_of(timestamp: datetime) -> str:
    return f"{timestamp.year:04d}-{timestamp.month:02d}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _paths_overlap(a: Path, b: Path) -> bool:
    for x, y in ((a, b), (b, a)):
        try:
            x.relative_to(y)
        except ValueError:
            continue
        return True
    return False


def _repo_root() -> Path:
    # market_analytics/win_m1_features.py -> market_analytics/ -> raiz do repo.
    return Path(__file__).resolve().parents[1]


def assert_frozen_protocol(*, symbol: str, start_date: date, end_date_exclusive: date) -> None:
    """Confere que a fonte/protocolo/janela batem com o congelado no DEV-007.

    Pura e sem efeitos colaterais — chamada pela CLI antes de abrir qualquer
    terminal MT5 (`preflight`), nunca pelo núcleo genérico
    `run_m1_history_producer`/`collect_validated_history` (que continuam
    aceitando janelas menores/arbitrárias para permanecerem testáveis com
    fixtures pequenas).
    """

    if symbol != INVENTORY_SYMBOL:
        raise HistoryProducerError(
            f"protocolo congelado exige symbol={INVENTORY_SYMBOL!r} (recebido: {symbol!r})"
        )
    if start_date != FROZEN_WINDOW_START or end_date_exclusive != FROZEN_WINDOW_END_EXCLUSIVE:
        raise HistoryProducerError(
            "protocolo congelado exige a janela "
            f"[{FROZEN_WINDOW_START.isoformat()}, {FROZEN_WINDOW_END_EXCLUSIVE.isoformat()}) "
            f"(recebido: [{start_date.isoformat()}, {end_date_exclusive.isoformat()}))"
        )


def assert_output_within_allowed_root(output_root: Path, allowed_root: Path) -> Path:
    """Recusa `output_root` fora de `allowed_root` (dados reais: `D:\\EPData\\MarketHub`).

    Pura e testável com qualquer `allowed_root` injetado — a CLI real chama
    com o caminho literal `D:\\EPData\\MarketHub`; os testes usam uma raiz
    dentro de `tmp_path`. `output_root` igual a `allowed_root` também é
    aceito (a própria raiz permitida é um destino válido). Retorna
    `output_root` resolvido quando aceito.
    """

    resolved_output = Path(output_root).expanduser().resolve(strict=False)
    resolved_allowed = Path(allowed_root).expanduser().resolve(strict=False)
    try:
        resolved_output.relative_to(resolved_allowed)
    except ValueError as exc:
        raise HistoryProducerError(
            f"output_root deve estar dentro de {resolved_allowed} (recebido: {resolved_output})"
        ) from exc
    return resolved_output


def preflight(
    *,
    output_root: Path,
    terminal_path: str,
    inventory_path: Path,
    inventory_sha256: str,
    symbol: str,
    start_date: date,
    end_date_exclusive: date,
    require_frozen_protocol: bool = True,
) -> tuple[Path, WinCopy2Inventory]:
    """Valida tudo que não depende de MT5, antes de abrir qualquer terminal.

    Ordem: protocolo congelado (símbolo/janela, quando `require_frozen_protocol`)
    -> destino (`_validate_run_arguments`: sem sobreposição com repositório/
    terminal/inventário, nunca raiz de volume) -> hash/schema do inventário
    (`load_inventory_file`, estrito) -> identidade do inventário carregado
    contra o `symbol` efetivamente solicitado. Nenhuma chamada aqui toca a
    rede ou importa `MetaTrader5`. A CLI real chama esta função e só
    constrói `Mt5RatesProvider` depois que ela retorna com sucesso
    (auditoria Codex da 1ª entrega: antes, o terminal era aberto antes de
    qualquer uma dessas checagens).
    """

    if require_frozen_protocol:
        assert_frozen_protocol(symbol=symbol, start_date=start_date, end_date_exclusive=end_date_exclusive)
    resolved_output = _validate_run_arguments(
        output_root=output_root, terminal_path=terminal_path, inventory_path=inventory_path
    )
    inventory = load_inventory_file(Path(inventory_path), expected_sha256=inventory_sha256)
    if inventory.symbol != symbol:
        # `WinCopy2Inventory.from_dict` já recusa qualquer `symbol` diferente
        # de "WIN$" no próprio arquivo; esta checagem cobre o caso do
        # ARGUMENTO `symbol` divergir do inventário carregado (ex.: alguém
        # chamando com `--symbol WDO$` contra o inventário do WIN$) — sem
        # ela, a divergência só apareceria indiretamente como um fingerprint
        # incompatível, mês a mês, em vez de uma mensagem de identidade clara
        # e imediata (auditoria Codex, item 9).
        raise HistoryProducerError(
            f"inventário carregado é para symbol={inventory.symbol!r}, mas foi solicitado symbol={symbol!r}"
        )
    return resolved_output, inventory


def _validate_run_arguments(
    *, output_root: Path, terminal_path: str, inventory_path: Path
) -> Path:
    """Recusa um destino sobreposto ao repositório, ao terminal ou ao inventário.

    Chamado antes de qualquer `mkdir`/leitura de rede — reflete a exigência
    do DEV-007 de que o destino de dados reais é sempre argumento explícito e
    nunca pode coincidir com o clone de trabalho, a instalação do terminal ou
    a raiz de um volume.
    """

    resolved_output = Path(output_root).expanduser().resolve(strict=False)

    if resolved_output.parent == resolved_output:
        raise HistoryProducerError(f"output_root não pode ser a raiz de um volume: {resolved_output}")

    repo_root = _repo_root()
    if _paths_overlap(resolved_output, repo_root):
        raise HistoryProducerError(
            f"output_root não pode coincidir com o repositório nem estar contido nele: "
            f"output_root={resolved_output}, repo_root={repo_root}"
        )

    terminal_dir = Path(terminal_path).expanduser().resolve(strict=False).parent
    if _paths_overlap(resolved_output, terminal_dir):
        raise HistoryProducerError(
            f"output_root não pode coincidir com a pasta do terminal MT5 nem estar contido nela: "
            f"output_root={resolved_output}, terminal_dir={terminal_dir}"
        )

    inventory_dir = Path(inventory_path).expanduser().resolve(strict=False).parent
    if _paths_overlap(resolved_output, inventory_dir):
        raise HistoryProducerError(
            f"output_root não pode coincidir com a pasta do inventário nem estar contido nela: "
            f"output_root={resolved_output}, inventory_dir={inventory_dir}"
        )

    return resolved_output


def _promote_output(temp_dir: Path, output_root: Path) -> None:
    """Promove `temp_dir` para `output_root` com recuperação transacional.

    Mesma técnica de duas trocas atômicas com backup intermediário usada em
    `market_analytics.quant_mvp._promote_output` (não reexportada de lá por
    ser privada): uma falha na segunda troca restaura o backup antes de
    propagar o erro, então `output_root` nunca fica ausente nem corrompido.
    """

    output_root = Path(output_root)
    temp_dir = Path(temp_dir)
    backup_path = output_root.with_name(f"{output_root.name}.win_m1_history_backup_{temp_dir.name.lstrip('.')}")

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


def _write_m1_month_parquet(
    path: Path,
    *,
    rows: Sequence[RawM1Row],
    bars: Sequence[Bar],
    source_id: str,
    symbol: str,
    month: str,
    fingerprint: str,
) -> None:
    """Grava as barras M1 já validadas de um mês, com metadados determinísticos.

    Nenhum campo de horário de execução (`generated_at_utc`/`duration`) é
    embutido aqui — só `run_summary.json` carrega esses campos voláteis
    (auditoria Codex: duas execuções sobre a mesma entrada devem produzir
    este Parquet byte-idêntico, e um `generated_at_utc` por mês quebrava
    isso).
    """

    metadata = {
        "schema": M1_BARS_SCHEMA,
        "schema_version": str(M1_BARS_SCHEMA_VERSION),
        "producer_version": PRODUCER_VERSION,
        "source_id": source_id,
        "logical_id": LOGICAL_ID,
        "resolved_symbol": symbol,
        "month": month,
        "bar_count": str(len(rows)),
        "fingerprint": fingerprint,
    }
    encoded_metadata = {key.encode("utf-8"): value.encode("utf-8") for key, value in metadata.items()}
    columns: dict[str, Any] = {
        "source_id": [bar.source_id for bar in bars],
        "symbol": [bar.symbol for bar in bars],
        "timeframe": [bar.timeframe for bar in bars],
        "timestamp_utc": pa.array([bar.timestamp for bar in bars], type=pa.timestamp("us", tz="UTC")),
        "open": [row.open for row in rows],
        "high": [row.high for row in rows],
        "low": [row.low for row in rows],
        "close": [row.close for row in rows],
        "tick_volume": [row.tick_volume for row in rows],
        "spread": [row.spread for row in rows],
        "real_volume": [row.real_volume for row in rows],
        "volume": [bar.volume for bar in bars],
        "volume_quality": [bar.volume_quality for bar in bars],
    }
    table = pa.table(columns)
    table = table.replace_schema_metadata(encoded_metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path), compression="zstd")


FEATURES_SCHEMA = "ep_market_hub.win_m1_features_m5"
FEATURES_SCHEMA_VERSION = 1


def _write_features_parquet(
    path: Path,
    *,
    bars: Sequence[Bar],
    feature_rows: Sequence[FeatureRow],
    source_id: str,
    symbol: str,
    config: FeatureConfig,
) -> None:
    """Grava as features M5 contínuas, com metadados/proveniência determinísticos.

    A metadata embutida identifica schema/versão/produtor, fonte lógica,
    timeframe, `FeatureConfig` completa (serializada como JSON) e as
    políticas de volume/disponibilidade — mas nenhum campo de horário de
    execução, pela mesma razão de `_write_m1_month_parquet`.
    """

    metadata = {
        "schema": FEATURES_SCHEMA,
        "schema_version": str(FEATURES_SCHEMA_VERSION),
        "producer_version": PRODUCER_VERSION,
        "source_id": source_id,
        "logical_id": LOGICAL_ID,
        "resolved_symbol": symbol,
        "features_timeframe": FEATURES_TIMEFRAME,
        "feature_config": json.dumps(config.to_dict(), sort_keys=True),
        "volume_policy": VOLUME_POLICY,
        "availability_policy": AVAILABILITY_POLICY,
    }
    encoded_metadata = {key.encode("utf-8"): value.encode("utf-8") for key, value in metadata.items()}

    columns: dict[str, Any] = {
        "month": [_month_of(bar.timestamp) for bar in bars],
        "source_id": [bar.source_id for bar in bars],
        "symbol": [bar.symbol for bar in bars],
        "timeframe": [bar.timeframe for bar in bars],
        "timestamp_utc": pa.array([bar.timestamp for bar in bars], type=pa.timestamp("us", tz="UTC")),
        "available_at_utc": pa.array([available_at_utc(bar) for bar in bars], type=pa.timestamp("us", tz="UTC")),
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
    table = table.replace_schema_metadata(encoded_metadata)
    pq.write_table(table, str(path), compression="zstd")


def _percentile(sorted_values: list[float], p: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (p / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[int(rank)]
    return sorted_values[int(lower)] * (upper - rank) + sorted_values[int(upper)] * (rank - lower)


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


def build_feature_summary(feature_rows: Sequence[FeatureRow], *, timeframe: str) -> list[dict[str, Any]]:
    total = len(feature_rows)
    rows: list[dict[str, Any]] = []
    for field_name in _FEATURE_NUMERIC_FIELDS:
        values = [getattr(row, field_name) for row in feature_rows]
        present = sorted(value for value in values if value is not None)
        missing = total - len(present)
        record: dict[str, Any] = {"timeframe": timeframe, "feature": field_name, "count": total, "missing": missing}
        for label, p in (("p10", 10), ("p25", 25), ("p50", 50), ("p75", 75), ("p90", 90)):
            record[label] = _percentile(present, p) if present else None
        rows.append(record)
    return rows


_COVERAGE_FIELDS: tuple[str, ...] = (
    "month",
    "bar_count",
    "fingerprint",
    "fingerprint_prefix",
    "inventory_bar_count",
    "inventory_fingerprint_prefix",
    "matched",
    "first_timestamp_utc",
    "last_timestamp_utc",
)


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


@dataclass(frozen=True)
class MonthOutcome:
    month: str
    rows: list[RawM1Row]
    bars: list[Bar]
    fingerprint: str
    inventory_bar_count: int
    inventory_fingerprint_prefix: str


def collect_validated_history(
    provider: RatesProvider,
    inventory: WinCopy2Inventory,
    *,
    symbol: str,
    source_id: str,
    start_date: date = FROZEN_WINDOW_START,
    end_date_exclusive: date = FROZEN_WINDOW_END_EXCLUSIVE,
    progress: Callable[[ProducerProgress], None] | None = None,
) -> list[MonthOutcome]:
    """Coleta, valida e confere contra o inventário cada mês da janela.

    Tudo-ou-nada por construção: a primeira `MonthRejectedError` ou
    `InventoryMonthMismatchError` interrompe a coleta inteira (propagada ao
    chamador) sem produzir nenhum artefato parcial — DEV-007 trata este lote
    fechado de 7 meses como uma unidade única de integridade, não como um
    backfill incremental tolerante a lacunas.
    """

    outcomes: list[MonthOutcome] = []
    for month, window_start, window_end, request_end in month_windows(start_date, end_date_exclusive):
        rows = fetch_and_validate_month(
            provider,
            symbol=symbol,
            month=month,
            window_start=window_start,
            window_end=window_end,
            request_end=request_end,
        )
        entry = inventory.months.get(month)
        if entry is None:
            raise InventoryMonthMismatchError(f"mês {month} ausente no inventário congelado")
        actual_fingerprint = compute_month_fingerprint(rows)
        if len(rows) != entry.bars:
            raise InventoryMonthMismatchError(
                f"{month}: contagem divergente do inventário: esperado {entry.bars}, obtido {len(rows)}"
            )
        if actual_fingerprint.upper() != entry.bars_fingerprint.upper():
            raise InventoryMonthMismatchError(
                f"{month}: fingerprint divergente do inventário "
                f"(prefixo esperado {fingerprint_human_prefix(entry.bars_fingerprint)}, "
                f"obtido {fingerprint_human_prefix(actual_fingerprint)})"
            )

        bars = build_month_bars(month, rows, source_id=source_id, symbol=symbol)
        outcomes.append(
            MonthOutcome(
                month=month,
                rows=rows,
                bars=bars,
                fingerprint=actual_fingerprint,
                inventory_bar_count=entry.bars,
                inventory_fingerprint_prefix=fingerprint_human_prefix(entry.bars_fingerprint),
            )
        )
        if progress is not None:
            progress(ProducerProgress(month=month, status="ok", detail=f"barras_m1={len(rows)}"))

    _assert_continuous(outcomes)
    return outcomes


def _assert_continuous(outcomes: Sequence[MonthOutcome]) -> None:
    """Confere que a concatenação cronológica dos meses é estritamente crescente.

    Defensivo: cada mês já é validado internamente por
    `fetch_and_validate_month`; isto só confirma que a fronteira entre um mês
    e o seguinte também é estritamente crescente, para que o warm-up
    contínuo de `compute_feature_rows` nunca veja uma regressão de tempo.
    """

    previous_timestamp = None
    for outcome in outcomes:
        for bar in outcome.bars:
            if previous_timestamp is not None and bar.timestamp <= previous_timestamp:
                raise MonthRejectedError(
                    outcome.month,
                    f"descontinuidade entre meses: {previous_timestamp.isoformat()} -> {bar.timestamp.isoformat()}",
                )
            previous_timestamp = bar.timestamp


def run_m1_history_producer(
    *,
    provider: RatesProvider,
    terminal_path: str,
    inventory_path: Path,
    inventory_sha256: str,
    output_root: Path,
    source_id: str = "clear_research",
    symbol: str = "WIN$",
    start_date: date = FROZEN_WINDOW_START,
    end_date_exclusive: date = FROZEN_WINDOW_END_EXCLUSIVE,
    feature_config: FeatureConfig | None = None,
    progress: Callable[[ProducerProgress], None] | None = None,
) -> dict[str, Any]:
    """Orquestra a entrega completa: inventário -> coleta -> features -> artefatos.

    Valida `output_root` (sem sobreposição com repositório/terminal/
    inventário, nunca raiz de volume) antes de tocar rede ou disco. Carrega e
    confere o hash do inventário. Coleta e valida cada mês (tudo-ou-nada).
    Concatena a série M1 contínua, agrega para M5 (`aggregate_bars`) e calcula
    features (`compute_feature_rows`) sobre essa série contínua — o warm-up
    nunca reinicia por mês. Escreve tudo primeiro num diretório temporário e
    só promove para `output_root` depois que todos os artefatos foram
    gravados com sucesso; qualquer falha antes da promoção nunca toca
    `output_root`.
    """

    started_at = datetime.now(UTC)
    config = feature_config if feature_config is not None else FeatureConfig()

    # `require_frozen_protocol=False`: este núcleo genérico continua aceitando
    # janelas menores/arbitrárias para permanecer testável com fixtures
    # pequenas — a CLI real (`tools/collect_win_m1_history.py`) chama
    # `preflight(..., require_frozen_protocol=True)` por conta própria, antes
    # de sequer construir `Mt5RatesProvider`.
    resolved_output, inventory = preflight(
        output_root=output_root,
        terminal_path=terminal_path,
        inventory_path=inventory_path,
        inventory_sha256=inventory_sha256,
        symbol=symbol,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        require_frozen_protocol=False,
    )

    outcomes = collect_validated_history(
        provider,
        inventory,
        symbol=symbol,
        source_id=source_id,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        progress=progress,
    )

    m1_bars_continuous: list[Bar] = [bar for outcome in outcomes for bar in outcome.bars]
    m5_bars_continuous = aggregate_bars(m1_bars_continuous, "M5")
    feature_rows_m5 = compute_feature_rows(m5_bars_continuous, config)

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".win_m1_history_tmp_", dir=str(resolved_output.parent)))
    try:
        artifact_paths: dict[str, Path] = {}

        coverage_rows: list[dict[str, Any]] = []
        for outcome in outcomes:
            m1_month_path = temp_dir / "m1" / f"year={outcome.month[:4]}" / f"month={outcome.month[5:7]}" / "bars_m1.parquet"
            _write_m1_month_parquet(
                m1_month_path,
                rows=outcome.rows,
                bars=outcome.bars,
                source_id=source_id,
                symbol=symbol,
                month=outcome.month,
                fingerprint=outcome.fingerprint,
            )
            artifact_paths[f"bars_m1_{outcome.month}"] = m1_month_path
            coverage_rows.append(
                {
                    "month": outcome.month,
                    "bar_count": len(outcome.rows),
                    "fingerprint": outcome.fingerprint,
                    "fingerprint_prefix": fingerprint_human_prefix(outcome.fingerprint),
                    "inventory_bar_count": outcome.inventory_bar_count,
                    "inventory_fingerprint_prefix": outcome.inventory_fingerprint_prefix,
                    "matched": True,
                    "first_timestamp_utc": outcome.bars[0].timestamp.isoformat(),
                    "last_timestamp_utc": outcome.bars[-1].timestamp.isoformat(),
                }
            )

        coverage_path = temp_dir / "coverage_months.csv"
        _write_csv(coverage_path, _COVERAGE_FIELDS, coverage_rows)
        artifact_paths["coverage_months"] = coverage_path

        features_path = temp_dir / "bars_features_M5.parquet"
        _write_features_parquet(
            features_path,
            bars=m5_bars_continuous,
            feature_rows=feature_rows_m5,
            source_id=source_id,
            symbol=symbol,
            config=config,
        )
        artifact_paths["bars_features_M5"] = features_path

        feature_summary_rows = build_feature_summary(feature_rows_m5, timeframe=FEATURES_TIMEFRAME)
        feature_summary_path = temp_dir / "feature_summary.csv"
        _write_csv(feature_summary_path, _FEATURE_SUMMARY_FIELDS, feature_summary_rows)
        artifact_paths["feature_summary"] = feature_summary_path

        finished_at = datetime.now(UTC)
        artifacts_manifest = {
            name: {"path": path.relative_to(temp_dir).as_posix(), "sha256": _sha256_file(path), "size_bytes": path.stat().st_size}
            for name, path in artifact_paths.items()
        }

        run_summary: dict[str, Any] = {
            "schema": RUN_SUMMARY_SCHEMA,
            "schema_version": RUN_SUMMARY_SCHEMA_VERSION,
            "producer_version": PRODUCER_VERSION,
            "generated_at_utc": finished_at.isoformat(),
            "duration_seconds": (finished_at - started_at).total_seconds(),
            "terminal_path": str(terminal_path),
            "source_id": source_id,
            "logical_id": LOGICAL_ID,
            "resolved_symbol": symbol,
            "window": {"start_date": start_date.isoformat(), "end_date_exclusive": end_date_exclusive.isoformat()},
            "inventory": {
                "path": str(Path(inventory_path)),
                "sha256_expected": inventory_sha256.strip().lower(),
                "verified": True,
            },
            "feature_config": config.to_dict(),
            "features_timeframe": FEATURES_TIMEFRAME,
            "volume_policy": VOLUME_POLICY,
            "availability_policy": AVAILABILITY_POLICY,
            "months": [
                {
                    "month": outcome.month,
                    "bar_count": len(outcome.rows),
                    "fingerprint": outcome.fingerprint,
                    "fingerprint_prefix": fingerprint_human_prefix(outcome.fingerprint),
                }
                for outcome in outcomes
            ],
            "totals": {
                "months_processed": len(outcomes),
                "bars_m1": len(m1_bars_continuous),
                "bars_m5": len(m5_bars_continuous),
            },
            "output_root": str(resolved_output),
            "artifacts": artifacts_manifest,
        }
        run_summary_path = temp_dir / "run_summary.json"
        run_summary_path.write_text(
            json.dumps(run_summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )

        _promote_output(temp_dir, resolved_output)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return run_summary
