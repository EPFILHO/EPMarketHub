from types import SimpleNamespace

from core.models import TerminalProfile
from core.mt5_connector import MT5Connector
from core.terminal_states import WorkerConnectionState


def build_initialized_connector(account_login: str = "") -> MT5Connector:
    connector = MT5Connector(
        TerminalProfile(
            id="fake",
            label="Fake",
            account_login=account_login,
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
    assert status.state == WorkerConnectionState.RECONNECTING.value
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
    assert status.state == WorkerConnectionState.WAITING_LOGIN.value
    assert "sem conta logada" in status.message


def test_account_status_authorization_failure_is_not_waiting_login(monkeypatch) -> None:
    fake_mt5 = SimpleNamespace(
        account_info=lambda: None,
        terminal_info=lambda: None,
        last_error=lambda: (-6, "Terminal: Authorization failed"),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)

    status = build_initialized_connector("111").connection_status()

    assert status.ok is False
    assert status.state == WorkerConnectionState.AUTHENTICATION_FAILED.value
    assert "verifique conta, senha e servidor" in status.message


def test_initialize_classifies_authorization_failure(tmp_path, monkeypatch) -> None:
    terminal_exe = tmp_path / "terminal64.exe"
    terminal_exe.write_bytes(b"fake")
    fake_mt5 = SimpleNamespace(
        initialize=lambda **_kwargs: False,
        last_error=lambda: (-6, "Terminal: Authorization failed"),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)
    connector = MT5Connector(
        TerminalProfile(id="auth", label="Auth", terminal_exe=str(terminal_exe))
    )

    status = connector.initialize()

    assert status.ok is False
    assert status.state == WorkerConnectionState.AUTHENTICATION_FAILED.value
    assert "verifique conta, senha e servidor" in status.message


def test_connected_account_must_match_registered_identity(monkeypatch) -> None:
    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(
            login=222,
            server="Sandbox-Server",
            company="Sandbox",
        ),
        terminal_info=lambda: SimpleNamespace(connected=True, path=None),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)

    status = build_initialized_connector("111").connection_status()

    assert status.ok is False
    assert status.state == WorkerConnectionState.ACCOUNT_MISMATCH.value
    assert "222" in status.message
    assert "111" in status.message


def test_broker_disconnection_is_not_reported_as_ipc_failure(monkeypatch) -> None:
    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(
            login=111,
            server="Sandbox-Server",
            company="Sandbox",
        ),
        terminal_info=lambda: SimpleNamespace(connected=False, path=None),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)

    status = build_initialized_connector("111").connection_status()

    assert status.ok is False
    assert status.state == WorkerConnectionState.BROKER_DISCONNECTED.value


def test_connection_to_another_terminal_path_is_rejected(tmp_path, monkeypatch) -> None:
    expected_exe = tmp_path / "expected" / "terminal64.exe"
    fake_mt5 = SimpleNamespace(
        account_info=lambda: SimpleNamespace(
            login=111,
            server="Sandbox-Server",
            company="Sandbox",
        ),
        terminal_info=lambda: SimpleNamespace(
            connected=True,
            path=str(tmp_path / "another-terminal"),
        ),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)
    connector = MT5Connector(
        TerminalProfile(
            id="wrong-terminal",
            label="Wrong terminal",
            account_login="111",
            terminal_exe=str(expected_exe),
        )
    )
    connector.initialized = True

    status = connector.connection_status()

    assert status.ok is False
    assert status.state == WorkerConnectionState.TERMINAL_MISMATCH.value
