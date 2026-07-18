from pathlib import Path

import pytest

from core.json_store import load_json, save_json_atomic
from core.models import TerminalProfile
from core.terminal_registry import TerminalRegistry


def test_invalid_json_is_preserved_in_quarantine(tmp_path: Path) -> None:
    path = tmp_path / "terminals.json"
    invalid = '{"terminal": '
    path.write_text(invalid, encoding="utf-8")

    loaded = load_json(path, [])

    quarantined = list(tmp_path.glob("terminals.json.corrupt-*"))
    assert loaded == []
    assert path.exists() is False
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == invalid


def test_empty_json_is_preserved_in_quarantine(tmp_path: Path) -> None:
    path = tmp_path / "symbols.json"
    path.write_text("", encoding="utf-8")

    assert load_json(path, []) == []
    assert path.exists() is False
    assert len(list(tmp_path.glob("symbols.json.corrupt-*"))) == 1


def test_read_permission_failure_is_not_converted_to_empty_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "terminals.json"
    path.write_text("[]", encoding="utf-8")

    def deny_read(self, *args, **kwargs):
        raise PermissionError("acesso negado")

    monkeypatch.setattr(Path, "read_text", deny_read)

    with pytest.raises(PermissionError, match="acesso negado"):
        load_json(path, [])

    assert path.exists() is True


def test_failed_atomic_replace_preserves_previous_file_and_removes_temp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "terminals.json"
    original = '[{"id": "last-valid"}]\n'
    path.write_text(original, encoding="utf-8")
    original_replace = Path.replace

    def deny_temp_replace(self, target):
        if self.suffix == ".tmp":
            raise PermissionError("promoção negada")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", deny_temp_replace)

    with pytest.raises(PermissionError, match="promoção negada"):
        save_json_atomic(path, [{"id": "new"}])

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_registry_recovers_after_quarantining_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "terminals.json"
    path.write_text("not-json", encoding="utf-8")
    registry = TerminalRegistry(path)

    assert registry.list() == []
    registry.upsert(
        TerminalProfile(
            id="terminal-new",
            label="Conta de teste",
            broker_name="Broker Sandbox",
            account_login="FAKE-RECOVERY",
        )
    )

    assert registry.get("terminal-new") is not None
    assert len(list(tmp_path.glob("terminals.json.corrupt-*"))) == 1
