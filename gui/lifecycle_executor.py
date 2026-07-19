from __future__ import annotations

import json
import logging
import threading
from collections import deque
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
        self._queued_operation_ids: set[str] = set()
        self._tasks: deque[tuple[str, LifecycleTask]] = deque()
        self._thread: threading.Thread | None = None

    def start(self, operation_id: str, task: LifecycleTask) -> bool:
        with self._lock:
            if operation_id in self._queued_operation_ids:
                return False
            self._queued_operation_ids.add(operation_id)
            self._tasks.append((operation_id, task))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._drain,
                    name="EP-MarketHub-Lifecycle",
                    daemon=False,
                )
                self._thread.start()
        return True

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._tasks:
                    self._thread = None
                    return
                operation_id, task = self._tasks.popleft()
            self._run(operation_id, task)

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
                self._queued_operation_ids.discard(operation_id)
        self.finished.emit(json.dumps(payload, ensure_ascii=False))
