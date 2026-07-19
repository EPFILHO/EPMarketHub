from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]
LifecycleTask = Callable[[ProgressCallback], dict[str, Any]]


class SerializedLifecycleExecutor(QObject):
    """Executa uma operação bloqueante por vez fora da thread gráfica."""

    progress = Signal(str)
    finished = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._active_operation_id: str | None = None
        self._thread: threading.Thread | None = None

    def start(self, operation_id: str, task: LifecycleTask) -> bool:
        with self._lock:
            if self._active_operation_id is not None:
                return False
            self._active_operation_id = operation_id
            self._thread = threading.Thread(
                target=self._run,
                args=(operation_id, task),
                name=f"EP-MarketHub-Lifecycle-{operation_id[:8]}",
                daemon=False,
            )
            self._thread.start()
        return True

    def _run(self, operation_id: str, task: LifecycleTask) -> None:
        def publish_progress(payload: dict[str, Any]) -> None:
            self.progress.emit(
                json.dumps(
                    {"operation_id": operation_id, **payload},
                    ensure_ascii=False,
                )
            )

        try:
            result = task(publish_progress)
            payload = {"operation_id": operation_id, **result}
        except Exception as exc:
            logger.exception("Falha inesperada na operação de ciclo de vida %s", operation_id)
            payload = {
                "operation_id": operation_id,
                "ok": False,
                "message": f"Falha inesperada durante o encerramento: {exc}",
                "data": {},
            }
        finally:
            with self._lock:
                self._active_operation_id = None
                self._thread = None
        self.finished.emit(json.dumps(payload, ensure_ascii=False))
