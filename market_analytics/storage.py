"""Artefato JSON versionado para persistir FeatureRow (pesquisa inicial).

Esta persistência é deliberadamente simples: um arquivo JSON por
fonte/símbolo/timeframe, gravado atomicamente. Ela serve para pesquisa e para
o consumidor externo (Fusion Quant) validar o formato antes de qualquer
investimento em armazenamento escalável (banco, parquet, etc.), que fica para
uma fatia futura — ver `docs/MARKET_ANALYTICS.md`.

Não depende de `core/` de propósito: este pacote deve poder ser extraído ou
consumido isoladamente.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import FeatureConfig
from .features import FEATURE_SCHEMA_VERSION, FeatureRow

ARTIFACT_KIND = "market_analytics.feature_rows"


def save_feature_artifact(
    path: Path,
    *,
    source_id: str,
    symbol: str,
    timeframe: str,
    config: FeatureConfig,
    rows: list[FeatureRow],
) -> None:
    """Grava as linhas de features em JSON por substituição atômica.

    Valida, antes de gravar, que todas as linhas pertencem ao mesmo
    `source_id`/`symbol`/`timeframe` declarados no cabeçalho, com o mesmo
    `schema_version`, e que os timestamps estão em ordem estritamente
    crescente — um artefato inconsistente nunca chega a tocar o disco.
    """

    _validate_header_identity(source_id=source_id, symbol=symbol, timeframe=timeframe)

    _validate_rows_against_header(rows, source_id=source_id, symbol=symbol, timeframe=timeframe)

    payload: dict[str, Any] = {
        "kind": ARTIFACT_KIND,
        "schema_version": FEATURE_SCHEMA_VERSION,
        "source_id": source_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "config": config.to_dict(),
        "row_count": len(rows),
        "rows": [row.to_dict() for row in rows],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            suffix=".tmp",
        ) as file_handle:
            temporary_path = Path(file_handle.name)
            file_handle.write(text)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def load_feature_artifact(path: Path) -> dict[str, Any]:
    """Lê o artefato e reconstrói as `FeatureRow`.

    Retorna um dicionário com `source_id`, `symbol`, `timeframe`, `config`
    (`FeatureConfig`) e `rows` (lista de `FeatureRow`). Recusa artefatos de
    `kind`/`schema_version` desconhecidos, `row_count` divergente do número
    real de linhas, cabeçalho inconsistente com as linhas, ou timestamps fora
    de ordem estritamente crescente.
    """

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Artefato deve ser um objeto JSON")
    if raw.get("kind") != ARTIFACT_KIND:
        raise ValueError(f"Artefato não reconhecido: kind={raw.get('kind')!r}")
    if raw.get("schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError(
            f"Versão de schema incompatível: esperado {FEATURE_SCHEMA_VERSION}, "
            f"recebido {raw.get('schema_version')!r}"
        )

    for required in ("source_id", "symbol", "timeframe", "config", "row_count", "rows"):
        if required not in raw:
            raise ValueError(f"Artefato incompleto: campo ausente {required!r}")

    source_id = raw["source_id"]
    symbol = raw["symbol"]
    timeframe = raw["timeframe"]
    _validate_header_identity(source_id=source_id, symbol=symbol, timeframe=timeframe)

    row_count = raw["row_count"]
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise ValueError(f"row_count deve ser um inteiro não negativo (recebido: {row_count!r})")

    rows_payload = raw["rows"]
    if not isinstance(rows_payload, list):
        raise ValueError("rows deve ser uma lista JSON")
    if any(not isinstance(item, dict) for item in rows_payload):
        raise ValueError("cada item de rows deve ser um objeto JSON")

    config = FeatureConfig.from_dict(raw["config"])
    rows = [FeatureRow.from_dict(item) for item in rows_payload]

    if row_count != len(rows):
        raise ValueError(
            f"row_count declarado ({row_count!r}) não bate com o número "
            f"real de linhas ({len(rows)})"
        )

    _validate_rows_against_header(rows, source_id=source_id, symbol=symbol, timeframe=timeframe)

    return {
        "source_id": source_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "config": config,
        "rows": rows,
    }


def _validate_header_identity(*, source_id: str, symbol: str, timeframe: str) -> None:
    for field_name, value in (
        ("source_id", source_id),
        ("symbol", symbol),
        ("timeframe", timeframe),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} deve ser uma string não vazia")


def _validate_rows_against_header(
    rows: list[FeatureRow],
    *,
    source_id: str,
    symbol: str,
    timeframe: str,
) -> None:
    previous_timestamp: datetime | None = None
    for row in rows:
        if row.schema_version != FEATURE_SCHEMA_VERSION:
            raise ValueError(f"linha com schema_version incompatível: {row.schema_version!r}")
        if (row.source_id, row.symbol, row.timeframe) != (source_id, symbol, timeframe):
            raise ValueError(
                "linha inconsistente com o cabeçalho do artefato: esperado "
                f"{(source_id, symbol, timeframe)!r}, "
                f"encontrado {(row.source_id, row.symbol, row.timeframe)!r}"
            )
        if previous_timestamp is not None and row.timestamp <= previous_timestamp:
            raise ValueError(
                "timestamps das linhas devem ser estritamente crescentes e sem "
                f"duplicatas: {previous_timestamp!r} seguido de {row.timestamp!r}"
            )
        previous_timestamp = row.timestamp
