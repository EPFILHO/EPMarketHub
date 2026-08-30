from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.models import TerminalProfile
from core.mt5_connector import MT5Connector, MT5TicksError

START_UTC = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)
END_UTC = START_UTC + timedelta(minutes=15)


def build_initialized_connector() -> MT5Connector:
    connector = MT5Connector(
        TerminalProfile(id="fake", label="Fake", terminal_exe="sandbox/terminal64.exe")
    )
    connector.initialized = True
    return connector


def test_copy_ticks_chunk_passes_utc_aware_datetimes_unmodified(monkeypatch) -> None:
    """Correção obrigatória: nunca converter para `datetime` naive."""

    captured: dict = {}

    def fake_copy_ticks_range(symbol, date_from, date_to, flags):
        captured["symbol"] = symbol
        captured["date_from"] = date_from
        captured["date_to"] = date_to
        captured["flags"] = flags
        return []

    fake_mt5 = SimpleNamespace(
        copy_ticks_range=fake_copy_ticks_range,
        COPY_TICKS_ALL=999,
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)
    connector = build_initialized_connector()

    connector.copy_ticks_chunk("WIN$", "all", START_UTC, END_UTC)

    assert captured["date_from"] is START_UTC
    assert captured["date_to"] is END_UTC
    assert captured["date_from"].utcoffset() == timedelta(0)
    assert captured["date_to"].utcoffset() == timedelta(0)
    assert captured["flags"] == 999
    assert captured["symbol"] == "WIN$"


def test_copy_ticks_chunk_rejects_naive_datetime() -> None:
    connector = build_initialized_connector()
    with pytest.raises(ValueError, match="timezone-aware"):
        connector.copy_ticks_chunk("WIN$", "all", datetime(2026, 3, 2, 13, 0), END_UTC)


def test_copy_ticks_chunk_maps_canonical_tick_type_to_mt5_constant(monkeypatch) -> None:
    captured: dict = {}
    fake_mt5 = SimpleNamespace(
        copy_ticks_range=lambda symbol, date_from, date_to, flags: (
            captured.setdefault("flags", flags) or []
        ),
        COPY_TICKS_ALL=1,
        COPY_TICKS_INFO=2,
        COPY_TICKS_TRADE=3,
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)
    connector = build_initialized_connector()

    connector.copy_ticks_chunk("WIN$", "trade", START_UTC, END_UTC)

    assert captured["flags"] == 3


def test_copy_ticks_chunk_returns_empty_array_without_error(monkeypatch) -> None:
    fake_mt5 = SimpleNamespace(
        copy_ticks_range=lambda *_args: [],
        COPY_TICKS_ALL=1,
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)
    connector = build_initialized_connector()

    result = connector.copy_ticks_chunk("WIN$", "all", START_UTC, END_UTC)

    assert result == []


def test_copy_ticks_chunk_raises_structured_error_when_api_returns_none(monkeypatch) -> None:
    fake_mt5 = SimpleNamespace(
        copy_ticks_range=lambda *_args: None,
        last_error=lambda: (-2, "Terminal: Invalid params"),
        COPY_TICKS_ALL=1,
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)
    connector = build_initialized_connector()

    with pytest.raises(MT5TicksError) as excinfo:
        connector.copy_ticks_chunk("WIN$", "all", START_UTC, END_UTC)

    assert excinfo.value.reason == "mt5_error"
    assert excinfo.value.code == -2
    # COR-DEV-001 item 5 (regressão): message não deve vir concatenada com o
    # código; ambos ficam disponíveis separadamente na exceção.
    assert excinfo.value.message == "Terminal: Invalid params"


def test_copy_ticks_chunk_normalizes_a_generic_exception_from_the_api(monkeypatch) -> None:
    """COR-DEV-001 item 6 (regressão): uma exceção comum levantada pela
    própria chamada copy_ticks_range vira MT5TicksError, não escapa crua."""

    def raise_runtime_error(*_args):
        raise RuntimeError("falha inesperada de IPC")

    fake_mt5 = SimpleNamespace(
        copy_ticks_range=raise_runtime_error,
        COPY_TICKS_ALL=1,
    )
    monkeypatch.setattr("core.mt5_connector.mt5", fake_mt5)
    connector = build_initialized_connector()

    with pytest.raises(MT5TicksError) as excinfo:
        connector.copy_ticks_chunk("WIN$", "all", START_UTC, END_UTC)

    assert excinfo.value.reason == "mt5_error"
    assert "falha inesperada de IPC" in excinfo.value.message


def test_copy_ticks_chunk_fails_when_not_initialized() -> None:
    connector = MT5Connector(
        TerminalProfile(id="fake", label="Fake", terminal_exe="sandbox/terminal64.exe")
    )

    with pytest.raises(MT5TicksError) as excinfo:
        connector.copy_ticks_chunk("WIN$", "all", START_UTC, END_UTC)

    assert excinfo.value.reason == "terminal_disconnected"
