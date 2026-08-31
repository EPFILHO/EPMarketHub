from types import SimpleNamespace

from core.models import TerminalProfile
from core.mt5_connector import MT5Connector
from core.terminal_states import MT5_COMMUNICATION_GUIDANCE, WorkerConnectionState


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


def test_symbol_states_expose_futures_expiration_and_session_liquidity(monkeypatch) -> None:
    row = SimpleNamespace(
        name="WINV26",
        description="IBOVESPA MINI",
        trade_mode=4,
        visible=True,
        select=True,
        bid=177_700.0,
        ask=177_705.0,
        last=177_700.0,
        start_time=1_770_000_000,
        expiration_time=1_782_000_000,
        session_volume=123_456.0,
        session_deals=78_900,
        session_turnover=987_654_321.0,
    )
    fake_mt5 = SimpleNamespace(
        symbols_get=lambda: (row,),
        SYMBOL_TRADE_MODE_DISABLED=0,
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)

    states = build_initialized_connector().list_symbol_states()

    assert states["WINV26"] == {
        "name": "WINV26",
        "description": "IBOVESPA MINI",
        "trade_mode": 4,
        "tradable": True,
        "visible": True,
        "selected": True,
        "has_quote": True,
        "bid": 177_700.0,
        "ask": 177_705.0,
        "last": 177_700.0,
        "start_time": 1_770_000_000,
        "expiration_time": 1_782_000_000,
        "session_volume": 123_456.0,
        "session_deals": 78_900,
        "session_turnover": 987_654_321.0,
    }


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
    assert status.message == MT5_COMMUNICATION_GUIDANCE
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


def test_initialize_ipc_timeout_uses_short_communication_guidance(tmp_path, monkeypatch) -> None:
    terminal_exe = tmp_path / "terminal64.exe"
    terminal_exe.write_bytes(b"fake")
    fake_mt5 = SimpleNamespace(
        initialize=lambda **_kwargs: False,
        last_error=lambda: (-10005, "IPC timeout"),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)
    connector = MT5Connector(
        TerminalProfile(id="ipc-timeout", label="IPC timeout", terminal_exe=str(terminal_exe))
    )

    status = connector.initialize()

    assert status.ok is False
    assert status.state == WorkerConnectionState.RECONNECTING.value
    assert status.message == MT5_COMMUNICATION_GUIDANCE


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


def test_missing_terminal_info_is_a_real_ipc_failure_even_with_account_data(
    monkeypatch,
) -> None:
    fake_mt5 = SimpleNamespace(
        terminal_info=lambda: None,
        account_info=lambda: SimpleNamespace(
            login=111,
            server="Sandbox-Server",
            company="Sandbox",
        ),
        last_error=lambda: (-10005, "IPC timeout"),
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)

    status = build_initialized_connector("111").connection_status()

    assert status.ok is False
    assert status.state == WorkerConnectionState.RECONNECTING.value
    assert status.message == MT5_COMMUNICATION_GUIDANCE


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
