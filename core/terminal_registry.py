from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from .json_store import load_json, save_json_atomic
from .models import TerminalProfile


class TerminalRegistry:
    """Persistência local dos terminais cadastrados.

    Não guarda senha. A identidade de negócio é Corretora + Conta informada,
    enquanto ``id`` permanece estável mesmo quando o usuário corrige os dados.
    """

    def __init__(self, terminals_file: Path):
        self.terminals_file = terminals_file

    @staticmethod
    def _identity_part(value: str) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    @classmethod
    def identity_key(cls, broker_name: str, account_login: str) -> tuple[str, str]:
        return cls._identity_part(broker_name), cls._identity_part(account_login)

    def list(self) -> list[TerminalProfile]:
        rows = load_json(self.terminals_file, [])
        if not isinstance(rows, list):
            return []
        profiles = [TerminalProfile.from_dict(row) for row in rows if isinstance(row, dict)]
        for profile in profiles:
            if not profile.instance_slug and profile.instance_dir:
                profile.instance_slug = Path(profile.instance_dir).name
        return profiles

    def get(self, terminal_id: str) -> TerminalProfile | None:
        return next((t for t in self.list() if t.id == terminal_id), None)

    def find_by_identity(
        self,
        broker_name: str,
        account_login: str,
        exclude_id: str | None = None,
    ) -> TerminalProfile | None:
        target = self.identity_key(broker_name, account_login)
        for profile in self.list():
            if exclude_id and profile.id == exclude_id:
                continue
            if self.identity_key(profile.broker_name, profile.account_login) == target:
                return profile
        return None

    def upsert(self, profile: TerminalProfile) -> TerminalProfile:
        rows = self.list()
        now = datetime.now().isoformat(timespec="seconds")
        profile.updated_at = now
        if not profile.created_at:
            profile.created_at = now

        replaced = False
        for idx, existing in enumerate(rows):
            if existing.id == profile.id:
                rows[idx] = profile
                replaced = True
                break
        if not replaced:
            rows.append(profile)
        self._save(rows)
        return profile

    def remove(self, terminal_id: str) -> bool:
        rows = self.list()
        new_rows = [t for t in rows if t.id != terminal_id]
        self._save(new_rows)
        return len(new_rows) != len(rows)

    def _save(self, rows: Iterable[TerminalProfile]) -> None:
        save_json_atomic(self.terminals_file, [row.to_dict() for row in rows])
