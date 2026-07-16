from __future__ import annotations

import logging
import multiprocessing as mp
import os
import sys


def configure_rendering_mode() -> None:
    """Ativa renderização por software somente quando solicitada.

    Uso de diagnóstico:
        python app.py --safe-rendering
    """

    if "--safe-rendering" not in sys.argv:
        return
    sys.argv.remove("--safe-rendering")
    current_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    safe_flags = "--disable-gpu --disable-gpu-compositing"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{current_flags} {safe_flags}".strip()
    os.environ.setdefault("QT_QUICK_BACKEND", "software")


configure_rendering_mode()

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.default_symbols import DEFAULT_SYMBOLS  # noqa: E402
from core.paths import build_paths  # noqa: E402
from core.symbol_registry import SymbolRegistry  # noqa: E402
from core.terminal_manager import TerminalManager  # noqa: E402
from core.terminal_registry import TerminalRegistry  # noqa: E402
from core.worker_manager import MT5WorkerManager  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402


def configure_logging(paths) -> None:
    log_file = paths.logs_dir / "market_hub.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    paths = build_paths(dev_mode=True)
    configure_logging(paths)

    app = QApplication(sys.argv)
    terminal_registry = TerminalRegistry(
        paths.terminals_file,
        root_dir=paths.root_dir,
        instances_dir=paths.mt5_instances_dir,
    )
    migrated_terminals = terminal_registry.migrate_paths()
    if migrated_terminals:
        logging.getLogger(__name__).info(
            "Caminhos relativos atualizados em %s cadastro(s) de terminal.",
            migrated_terminals,
        )
    symbol_registry = SymbolRegistry(paths.symbols_file)
    symbol_registry.ensure_defaults(DEFAULT_SYMBOLS)
    terminal_manager = TerminalManager(paths.mt5_instances_dir, paths.mt5_base_dir)
    worker_manager = MT5WorkerManager(refresh_seconds=2.0, live_poll_seconds=0.20, max_workers=3)

    window = MainWindow(
        terminal_registry=terminal_registry,
        symbol_registry=symbol_registry,
        terminal_manager=terminal_manager,
        worker_manager=worker_manager,
        web_dir=paths.root_dir / "web",
    )
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
