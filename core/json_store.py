from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_json(path: Path, default: Any) -> Any:
    """Lê JSON local; em arquivo inválido, registra o erro e retorna o padrão."""

    if not path.exists():
        return default
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return default
        return json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.exception("Não foi possível ler o JSON local: %s", path)
        return default


def save_json_atomic(path: Path, data: Any) -> None:
    """Grava JSON por substituição atômica para reduzir risco de arquivo parcial."""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        suffix=".tmp",
    ) as file_handle:
        file_handle.write(text)
        file_handle.write("\n")
        temporary_name = file_handle.name
    Path(temporary_name).replace(path)
