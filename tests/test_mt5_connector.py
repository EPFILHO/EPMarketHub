from types import SimpleNamespace

from core.models import TerminalProfile
from core.mt5_connector import MT5Connector


def build_initialized_connector() -> MT5Connector:
    connector = MT5Connector(
        TerminalProfile(
            id="fake",
            label="Fake",
            terminal_exe="sandbox/terminal64.exe",
        )
    )
    connector.initialized = True
    return connector


def test_ipc_failure_is_not_reported_as_missing_login(monkeypatch) -> None:
    fake_mt5 = SimpleNamespace(
        account_info=lambda: None,
        terminal_info=lambda: None,
        last_error=lambda: (-10001, "IPC send failed"),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)

    status = build_initialized_connector().connection_status()

    assert status.ok is False
    assert "Comunicação com o MT5 foi interrompida" in status.message
    assert "sem conta logada" not in status.message


def test_missing_account_without_ipc_error_requests_manual_login(monkeypatch) -> None:
    fake_mt5 = SimpleNamespace(
        account_info=lambda: None,
        terminal_info=lambda: SimpleNamespace(connected=False),
        last_error=lambda: (0, ""),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)

    status = build_initialized_connector().connection_status()

    assert status.ok is False
    assert "sem conta logada" in status.message
