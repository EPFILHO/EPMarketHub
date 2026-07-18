from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _quarantine_invalid_json(path: Path, error: BaseException) -> Path:
    quarantine = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
    try:
        path.replace(quarantine)
    except OSError as quarantine_error:
        logger.exception("JSON inválido não pôde ser preservado em quarentena: %s", path)
        raise OSError(
            f"O JSON {path} está inválido e não pôde ser movido para quarentena."
        ) from quarantine_error
    logger.error(
        "JSON inválido preservado em %s antes de retornar ao padrão: %s",
        quarantine,
        error,
    )
    return quarantine


def load_json(path: Path, default: Any) -> Any:
    """Lê JSON local sem converter falha de acesso em registro vazio.

    Conteúdo vazio, inválido ou com codificação danificada é movido para um
    arquivo de quarentena antes de retornar o padrão. Falhas de acesso são
    propagadas para impedir que uma leitura vazia sobrescreva dados existentes.
    """

    if not path.exists():
        return default
    try:
        content = path.read_text(encoding="utf-8").strip()
    except UnicodeError as exc:
        _quarantine_invalid_json(path, exc)
        return default
    except OSError:
        logger.exception("Não foi possível acessar o JSON local: %s", path)
        raise

    if not content:
        _quarantine_invalid_json(path, ValueError("arquivo vazio"))
        return default
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        _quarantine_invalid_json(path, exc)
        return default


def save_json_atomic(path: Path, data: Any) -> None:
    """Grava JSON por substituição atômica para reduzir risco de arquivo parcial."""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            suffix=".tmp",
        ) as file_handle:
            file_handle.write(text)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
            temporary_path = Path(file_handle.name)
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Arquivo temporário não pôde ser removido: %s", temporary_path)
        raise
