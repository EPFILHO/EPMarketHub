from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .json_store import load_json, save_json_atomic
from .models import TerminalProfile


class TerminalRegistry:
    """Persistência local dos terminais cadastrados.

    Não guarda senha. A identidade de negócio é Corretora + Conta informada,
    enquanto ``id`` permanece estável mesmo quando o usuário corrige os dados.
    """

    def __init__(
        self,
        terminals_file: Path,
        root_dir: Path | None = None,
        instances_dir: Path | None = None,
    ):
        self.terminals_file = terminals_file
        self.root_dir = root_dir.resolve() if root_dir is not None else None
        self.instances_dir = instances_dir.resolve() if instances_dir is not None else None

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
        return self._profiles_from_rows(rows)

    def _profiles_from_rows(self, rows: Iterable[dict]) -> list[TerminalProfile]:
        profiles = [TerminalProfile.from_dict(row) for row in rows if isinstance(row, dict)]
        for profile in profiles:
            if not profile.instance_slug and profile.instance_dir:
                profile.instance_slug = self._path_tail(profile.instance_dir)
            self._bind_installation_paths(profile)
        return profiles

    @staticmethod
    def _path_tail(value: str) -> str:
        return str(value or "").strip().replace("\\", "/").rstrip("/").split("/")[-1]

    def _bind_installation_paths(self, profile: TerminalProfile) -> None:
        """Resolve caminhos persistidos contra a instalação que está em execução."""

        if self.instances_dir is None or not profile.instance_slug:
            return
        slug = self._path_tail(profile.instance_slug)
        if not slug or slug in {".", ".."}:
            return
        profile.instance_slug = slug
        instance_dir = (self.instances_dir / slug).resolve()
        profile.instance_dir = str(instance_dir)
        profile.terminal_exe = str(instance_dir / "terminal64.exe")

    def migrate_paths(self) -> int:
        """Converte caminhos absolutos legados para caminhos relativos à instalação."""

        if self.root_dir is None or self.instances_dir is None:
            return 0
        rows = load_json(self.terminals_file, [])
        if not isinstance(rows, list):
            return 0
        profiles = self._profiles_from_rows(row for row in rows if isinstance(row, dict))
        serialized = [self._serialize(profile) for profile in profiles]
        changed = sum(
            1
            for before, after in zip(
                (row for row in rows if isinstance(row, dict)),
                serialized,
                strict=True,
            )
            if before != after
        )
        if changed:
            save_json_atomic(self.terminals_file, serialized)
        return changed

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
        save_json_atomic(self.terminals_file, [self._serialize(row) for row in rows])

    def _serialize(self, profile: TerminalProfile) -> dict:
        data = profile.to_dict()
        if self.root_dir is None or self.instances_dir is None or not profile.instance_slug:
            return data

        slug = self._path_tail(profile.instance_slug)
        instance_dir = (self.instances_dir / slug).resolve()
        try:
            relative_dir = instance_dir.relative_to(self.root_dir)
        except ValueError as exc:
            raise ValueError(
                "A pasta de instâncias deve permanecer dentro da instalação do EP Market Hub."
            ) from exc
        data["instance_slug"] = slug
        data["instance_dir"] = relative_dir.as_posix()
        data["terminal_exe"] = (relative_dir / "terminal64.exe").as_posix()
        return data
