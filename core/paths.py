from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "EP Market Hub"
APP_SLUG = "EP_Market_Hub"


@dataclass(frozen=True)
class AppPaths:
    root_dir: Path
    mt5_base_dir: Path
    user_data_dir: Path
    terminals_file: Path
    symbols_file: Path
    logs_dir: Path
    mt5_instances_dir: Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_user_data_dir(dev_mode: bool = True) -> Path:
    """Retorna a pasta de dados do usuário.

    Em modo dev, usa ./user_data para facilitar testes e migrações.
    Em modo instalado, pode usar %LOCALAPPDATA%/EP Market Hub no Windows.
    """

    root = project_root()
    if dev_mode:
        return root / "user_data"

    if sys.platform.startswith("win"):
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
        return Path(base) / APP_NAME

    return Path.home() / f".{APP_SLUG.lower()}"


def build_paths(dev_mode: bool = True) -> AppPaths:
    root = project_root()
    user_data = default_user_data_dir(dev_mode=dev_mode)
    paths = AppPaths(
        root_dir=root,
        mt5_base_dir=root / "MT5",
        user_data_dir=user_data,
        terminals_file=user_data / "terminals.json",
        symbols_file=user_data / "symbols.json",
        logs_dir=user_data / "logs",
        mt5_instances_dir=user_data / "mt5_instances",
    )
    ensure_paths(paths)
    return paths


def ensure_paths(paths: AppPaths) -> None:
    paths.mt5_base_dir.mkdir(parents=True, exist_ok=True)
    paths.user_data_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.mt5_instances_dir.mkdir(parents=True, exist_ok=True)
    if not paths.terminals_file.exists():
        paths.terminals_file.write_text("[]\n", encoding="utf-8")
    if not paths.symbols_file.exists():
        paths.symbols_file.write_text("[]\n", encoding="utf-8")
