"""Cobertura da orquestração de `tools/b3_contract_capture_gui.py`.

Testa `ContractCaptureRunner`/`determine_exit_code` diretamente, sem
instanciar `CaptureWindow` (evita depender de um display Tk real). Um
`mt5` falso substitui o módulo `MetaTrader5` inteiro via monkeypatch, no
mesmo espírito de `tests/test_mt5_connector.py`; `DATA_ROOT`/`TRACKER_PATH`/
`REPORT_DIR`/`CLEAR_EXE` são redirecionados para `tmp_path`, então nenhum
teste toca `D:\\EPData` nem qualquer terminal real.

Regressão da auditoria do commit `bb8ae48` (correções autorizadas):
- ausência de candidato atual / recusa de seleção para UM instrumento nunca
  aborta o outro instrumento saudável;
- o registro de contratos é persistido antes de uma falha subsequente do
  backfill, mesmo quando essa falha aborta a execução inteira;
- `determine_exit_code` só devolve 0 para um "done" sem issues críticas;
- reexecutar no MESMO dia -- e ser exatamente o dia de vencimento -- depois
  que a sessão já foi capturada e verificada com sucesso nunca gera um
  `missing_before_expiration` falso quando o símbolo some em seguida
  (revisão Codex, 2ª rodada);
- reexecutar ANTES do dia de vencimento, mesmo com a sessão de hoje já
  catalogada, continua gerando `missing_before_expiration` -- a confirmação
  do catálogo só encerra o acompanhamento no dia exato do vencimento
  (contraste da mesma revisão).
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import tools.b3_contract_capture_gui as capture_gui
from market_analytics.contract_tracking import empty_tracker
from market_analytics.pilot_backfill import summarize_results


class FakeMT5:
    """Substitui `MetaTrader5` inteiro para os testes de orquestração."""

    def __init__(self, symbol_rows, *, select_refusals=frozenset(), tick_source=None):
        self._symbol_rows = list(symbol_rows)
        self._select_refusals = set(select_refusals)
        self._tick_source = tick_source or (lambda symbol, start_utc, end_utc: [])
        self.SYMBOL_TRADE_MODE_DISABLED = 0
        self.COPY_TICKS_ALL = 1
        self.selected_symbols: list[str] = []
        self.shutdown_called = False

    def initialize(self, **kwargs):
        return True

    def terminal_info(self):
        return SimpleNamespace(connected=True, build=5000, company="Fake Broker")

    def symbols_get(self):
        return list(self._symbol_rows)

    def symbol_select(self, symbol, enable):
        self.selected_symbols.append(symbol)
        return symbol not in self._select_refusals

    def copy_ticks_range(self, symbol, start_utc, end_utc, flags):
        return self._tick_source(symbol, start_utc, end_utc)

    def last_error(self):
        return (65538, "fake mt5 error")

    def shutdown(self):
        self.shutdown_called = True


def _drain(events: queue.Queue[tuple[str, object]]) -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    while True:
        try:
            items.append(events.get_nowait())
        except queue.Empty:
            return items


def _patch_data_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(capture_gui, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(capture_gui, "TRACKER_PATH", tmp_path / "tracked_contracts.json")
    monkeypatch.setattr(capture_gui, "REPORT_DIR", tmp_path / "reports")
    clear_exe = tmp_path / "terminal64.exe"
    clear_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(capture_gui, "CLEAR_EXE", clear_exe)


def _far_future_timestamp() -> int:
    return int((datetime.now(UTC) + timedelta(days=200)).timestamp())


def _row(
    symbol: str, *, volume: float, deals: int, expiration: datetime | None = None
) -> SimpleNamespace:
    expiration_ts = int(expiration.timestamp()) if expiration is not None else _far_future_timestamp()
    return SimpleNamespace(
        name=symbol,
        trade_mode=4,
        bid=100.0,
        ask=100.5,
        last=100.0,
        expiration_time=expiration_ts,
        session_volume=volume,
        session_deals=deals,
    )


def _freeze_now(monkeypatch, fixed_utc: datetime) -> None:
    """Congela `datetime.now(...)` dentro do módulo da GUI num valor fixo.

    Necessário sempre que um teste precisa que `session_date` seja
    idêntico entre duas execuções -- ou seja exatamente um dia de
    vencimento -- sem depender do relógio real da máquina (o módulo só usa
    `datetime.now(UTC)`, nunca constrói `datetime(...)` diretamente, então
    substituir o nome inteiro é seguro).
    """

    class _FrozenClock:
        def now(self, tz=None):
            return fixed_utc.astimezone(tz) if tz is not None else fixed_utc

    monkeypatch.setattr(capture_gui, "datetime", _FrozenClock())


def _seed_tracker_row(
    *,
    symbol: str,
    instrument: str,
    logical_id: str,
    contract_month: str,
    expiration_utc: str,
    state: str,
    observed_at: str,
) -> None:
    """Grava `TRACKER_PATH` (já redirecionado para `tmp_path`) com uma única
    linha pré-existente, para testar o que acontece quando um contrato já
    rastreado em execuções anteriores some do terminal.
    """

    tracker = empty_tracker()
    tracker["updated_at_utc"] = observed_at
    tracker["contracts"] = [
        {
            "symbol": symbol,
            "instrument": instrument,
            "logical_id": logical_id,
            "contract_month": contract_month,
            "first_observed_utc": observed_at,
            "last_observed_utc": observed_at,
            "expiration_utc": expiration_utc,
            "state": state,
        }
    ]
    capture_gui.atomic_write_json(capture_gui.TRACKER_PATH, tracker)


def test_instrument_without_current_candidate_does_not_block_the_healthy_one(tmp_path, monkeypatch) -> None:
    _patch_data_paths(monkeypatch, tmp_path)
    # Só WDO tem candidato observável nesta execução; WIN não aparece em
    # symbols_get() (equivalente a symbols_get() não devolver nenhum
    # contrato WIN elegível).
    fake_mt5 = FakeMT5([_row("WDOU26", volume=1_000.0, deals=50)])
    monkeypatch.setattr(capture_gui, "mt5", fake_mt5)

    events: queue.Queue = queue.Queue()
    runner = capture_gui.ContractCaptureRunner(events, threading.Event())
    runner.run()

    kinds = [kind for kind, _ in _drain(events)]
    assert "error" not in kinds
    assert "done" in kinds

    no_candidate_issues = [issue for issue in runner.issues if issue["type"] == "no_current_candidate"]
    assert {issue["instrument"] for issue in no_candidate_issues} == {"win"}

    wdo_results = [result for result in runner.results if result["symbol"] == "WDOU26"]
    assert len(wdo_results) == 1
    assert wdo_results[0]["state"] in {"completed", "empty"}

    assert capture_gui.TRACKER_PATH.exists()
    tracker_payload = json.loads(capture_gui.TRACKER_PATH.read_text(encoding="utf-8"))
    tracker_symbols = {row["symbol"] for row in tracker_payload["contracts"]}
    assert tracker_symbols == {"WDOU26"}


def test_symbol_select_refusal_for_current_contract_degrades_instead_of_aborting(tmp_path, monkeypatch) -> None:
    fake_mt5 = FakeMT5(
        [_row("WINV26", volume=500.0, deals=20), _row("WDOU26", volume=1_000.0, deals=50)],
        select_refusals={"WINV26"},
    )
    _patch_data_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(capture_gui, "mt5", fake_mt5)

    events: queue.Queue = queue.Queue()
    runner = capture_gui.ContractCaptureRunner(events, threading.Event())
    runner.run()

    kinds = [kind for kind, _ in _drain(events)]
    assert "error" not in kinds
    assert "done" in kinds

    refused_issues = [issue for issue in runner.issues if issue["type"] == "symbol_select_refused"]
    assert {issue["symbol"] for issue in refused_issues} == {"WINV26"}

    wdo_results = [result for result in runner.results if result["symbol"] == "WDOU26"]
    assert len(wdo_results) == 1
    win_results = [result for result in runner.results if result["symbol"] == "WINV26"]
    assert win_results == []


def test_tracker_is_persisted_before_a_later_session_failure(tmp_path, monkeypatch) -> None:
    def tick_source(symbol, start_utc, end_utc):
        if symbol == "WINV26":
            return None  # mt5.copy_ticks_range devolvendo None -> mt5_error
        return []

    fake_mt5 = FakeMT5(
        [_row("WINV26", volume=500.0, deals=20), _row("WDOU26", volume=1_000.0, deals=50)],
        tick_source=tick_source,
    )
    _patch_data_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(capture_gui, "mt5", fake_mt5)

    events: queue.Queue = queue.Queue()
    runner = capture_gui.ContractCaptureRunner(events, threading.Event())
    runner.run()

    kinds = [kind for kind, _ in _drain(events)]
    assert "done" not in kinds
    assert "error" in kinds

    # WIN falhou o backfill em si (não a seleção nem o planejamento), mas o
    # tracker já tinha sido persistido para os DOIS instrumentos antes de a
    # sessão de WIN sequer começar a baixar ticks.
    assert capture_gui.TRACKER_PATH.exists()
    tracker_payload = json.loads(capture_gui.TRACKER_PATH.read_text(encoding="utf-8"))
    tracker_states = {row["symbol"]: row["state"] for row in tracker_payload["contracts"]}
    assert tracker_states.get("WINV26") == "active"
    assert tracker_states.get("WDOU26") == "active"


# 2026-09-01 22:30 UTC = terça-feira, 19:30 América/São Paulo -- depois do
# corte das 19h, então `session_date` cai no próprio dia (2026-09-01) nas
# duas execuções, já que o relógio fica congelado. WDOU26_FINAL_EXPIRATION é
# o vencimento real de WDOU26 nesse mesmo dia (18:00 BRT), então
# `session_date` coincide exatamente com o dia de vencimento.
FROZEN_NOW = datetime(2026, 9, 1, 22, 30, tzinfo=UTC)
FROZEN_SESSION_DATE = date(2026, 9, 1)
WDOU26_FINAL_EXPIRATION = datetime(2026, 9, 1, 21, 0, tzinfo=UTC)


def test_reexecution_on_the_final_session_after_catalog_confirmation_does_not_flag_missing(
    tmp_path, monkeypatch
) -> None:
    """Regressão da revisão Codex (2ª rodada): reexecutar no MESMO dia --
    que é exatamente o dia de vencimento de WDOU26 -- depois que a sessão
    já foi capturada e verificada com sucesso no catálogo nunca pode gerar
    um `missing_before_expiration` falso quando o símbolo então some, nem
    impedir WIN/WDOV26 de serem reaproveitados via `already_completed`.
    """

    _patch_data_paths(monkeypatch, tmp_path)
    _freeze_now(monkeypatch, FROZEN_NOW)
    _seed_tracker_row(
        symbol="WDOU26",
        instrument="wdo",
        logical_id="wdo_contract_wdou26",
        contract_month="2026-09-01",
        expiration_utc=WDOU26_FINAL_EXPIRATION.isoformat(),
        state="tracking_until_expiration",
        observed_at=FROZEN_NOW.isoformat(),
    )

    # Execução 1: WINV26 e WDOV26 são os contratos ativos hoje; WDOU26 já
    # não é mais o mais líquido, mas ainda está listado no seu próprio dia
    # de vencimento -- capturado uma última vez via tracked_until_expiration.
    fake_mt5_run1 = FakeMT5(
        [
            _row("WINV26", volume=500.0, deals=20),
            _row("WDOV26", volume=2_000.0, deals=80),
            _row("WDOU26", volume=10.0, deals=1, expiration=WDOU26_FINAL_EXPIRATION),
        ]
    )
    monkeypatch.setattr(capture_gui, "mt5", fake_mt5_run1)
    events1: queue.Queue = queue.Queue()
    runner1 = capture_gui.ContractCaptureRunner(events1, threading.Event())
    runner1.run()
    kinds1 = [kind for kind, _ in _drain(events1)]
    assert "error" not in kinds1
    assert "done" in kinds1
    assert runner1.issues == []
    wdou26_results_run1 = [result for result in runner1.results if result["symbol"] == "WDOU26"]
    assert len(wdou26_results_run1) == 1
    assert wdou26_results_run1[0]["state"] in {"completed", "empty"}
    # Confere que o relógio congelado realmente produziu a sessão esperada
    # -- exatamente o dia de vencimento de WDOU26, não um dia qualquer.
    assert wdou26_results_run1[0]["session_date"] == FROZEN_SESSION_DATE.isoformat()

    # Execução 2, MESMO dia (relógio ainda congelado): WIN e WDOV26
    # continuam iguais (devem ser reaproveitados via already_completed);
    # WDOU26 sumiu do terminal -- mas hoje é exatamente o seu dia de
    # vencimento, e essa sessão já está completed/empty e verificada.
    fake_mt5_run2 = FakeMT5(
        [_row("WINV26", volume=500.0, deals=20), _row("WDOV26", volume=2_000.0, deals=80)]
    )
    monkeypatch.setattr(capture_gui, "mt5", fake_mt5_run2)
    events2: queue.Queue = queue.Queue()
    runner2 = capture_gui.ContractCaptureRunner(events2, threading.Event())
    runner2.run()
    drained2 = _drain(events2)
    kinds2 = [kind for kind, _ in drained2]
    assert "error" not in kinds2
    assert "done" in kinds2

    assert runner2.issues == []
    assert all(result["state"] != "missing_before_expiration" for result in runner2.results)

    done_payloads = [payload for kind, payload in drained2 if kind == "done"]
    assert done_payloads[0]["report"]["issues"] == []
    assert capture_gui.determine_exit_code("done", done_payloads[0]) == 0

    win_results = [result for result in runner2.results if result["symbol"] == "WINV26"]
    assert len(win_results) == 1
    assert win_results[0]["reused"] is True
    wdov26_results = [result for result in runner2.results if result["symbol"] == "WDOV26"]
    assert len(wdov26_results) == 1
    assert wdov26_results[0]["reused"] is True

    tracker_payload = json.loads(capture_gui.TRACKER_PATH.read_text(encoding="utf-8"))
    tracker_states = {row["symbol"]: row["state"] for row in tracker_payload["contracts"]}
    assert tracker_states["WINV26"] == "active"
    assert tracker_states["WDOV26"] == "active"
    assert tracker_states["WDOU26"] == "expired"


def test_reexecution_before_the_final_session_still_flags_missing_even_if_catalog_confirmed(
    tmp_path, monkeypatch
) -> None:
    """Contraste com o teste acima (revisão Codex): se hoje NÃO for o dia
    de vencimento real de WDOU26 -- mesmo com a sessão de hoje já
    completed/empty e verificada no catálogo -- o desaparecimento continua
    `missing_before_expiration`, porque ainda restam sessões futuras
    obrigatórias até o vencimento real. A confirmação do catálogo só
    encerra o acompanhamento no dia exato de vencimento, nunca antes.
    """

    _patch_data_paths(monkeypatch, tmp_path)
    _freeze_now(monkeypatch, FROZEN_NOW)
    far_expiration = FROZEN_NOW + timedelta(days=45)  # vencimento real bem depois de hoje

    # Execução 1: WDOU26 é o próprio contrato ativo hoje; a sessão de hoje é
    # capturada e verificada com sucesso, mas o vencimento real está longe.
    fake_mt5_run1 = FakeMT5(
        [
            _row("WINV26", volume=500.0, deals=20),
            _row("WDOU26", volume=1_000.0, deals=50, expiration=far_expiration),
        ]
    )
    monkeypatch.setattr(capture_gui, "mt5", fake_mt5_run1)
    events1: queue.Queue = queue.Queue()
    runner1 = capture_gui.ContractCaptureRunner(events1, threading.Event())
    runner1.run()
    kinds1 = [kind for kind, _ in _drain(events1)]
    assert "error" not in kinds1
    assert "done" in kinds1
    assert runner1.issues == []

    # Execução 2, MESMO dia: WDOU26 sumiu do terminal (ex.: falha
    # transitória do feed) e WDOV26 assume a liquidez -- mas o vencimento
    # real de WDOU26 ainda está a 45 dias; a sessão de hoje já arquivada não
    # prova nada sobre as sessões futuras obrigatórias até lá.
    fake_mt5_run2 = FakeMT5(
        [_row("WINV26", volume=500.0, deals=20), _row("WDOV26", volume=2_000.0, deals=80)]
    )
    monkeypatch.setattr(capture_gui, "mt5", fake_mt5_run2)
    events2: queue.Queue = queue.Queue()
    runner2 = capture_gui.ContractCaptureRunner(events2, threading.Event())
    runner2.run()
    drained2 = _drain(events2)
    kinds2 = [kind for kind, _ in drained2]
    assert "error" not in kinds2
    assert "done" in kinds2

    assert len(runner2.issues) == 1
    assert runner2.issues[0]["type"] == "missing_before_expiration"
    assert runner2.issues[0]["symbol"] == "WDOU26"

    done_payloads = [payload for kind, payload in drained2 if kind == "done"]
    assert done_payloads[0]["report"]["issues"] != []
    assert capture_gui.determine_exit_code("done", done_payloads[0]) == 1

    missing_results = [result for result in runner2.results if result["symbol"] == "WDOU26"]
    assert len(missing_results) == 1
    assert missing_results[0]["state"] == "missing_before_expiration"

    tracker_payload = json.loads(capture_gui.TRACKER_PATH.read_text(encoding="utf-8"))
    tracker_states = {row["symbol"]: row["state"] for row in tracker_payload["contracts"]}
    assert tracker_states["WDOU26"] == "missing_before_expiration"


def test_result_from_issue_marks_missing_before_expiration_as_a_failed_result() -> None:
    issue = {
        "type": "missing_before_expiration",
        "symbol": "WDOU26",
        "instrument": "wdo",
        "logical_id": "wdo_contract_wdou26",
        "session_date": "2026-09-01",
        "expiration_utc": "2026-09-01T13:00:00+00:00",
        "message": "WDOU26 desapareceu do terminal antes de capturar a sessão de vencimento.",
    }

    result = capture_gui.ContractCaptureRunner.result_from_issue(issue)

    assert result["state"] == "missing_before_expiration"
    assert result["symbol"] == "WDOU26"
    assert result["logical_id"] == "wdo_contract_wdou26"
    assert result["tick_count"] == 0

    summary = summarize_results([result])
    assert summary["totals"]["failed"] == 1
    assert summary["totals"]["completed"] == 0
    assert summary["totals"]["empty"] == 0


def test_scheduled_close_delay_matches_the_error_delay_when_there_are_issues() -> None:
    """Regressão da revisão Codex: um "done" com issues críticas precisa do
    mesmo prazo de inspeção de um "error" (2 minutos), não os 30s de um
    sucesso real -- a documentação e o código devem concordar.
    """

    assert capture_gui.scheduled_close_delay_ms(False) == 30_000
    assert capture_gui.scheduled_close_delay_ms(True) == 120_000


def test_determine_exit_code_is_zero_only_for_a_real_success() -> None:
    assert capture_gui.determine_exit_code("done", {"report": {"issues": []}}) == 0
    assert capture_gui.determine_exit_code("done", {"report": {}}) == 0
    assert (
        capture_gui.determine_exit_code(
            "done", {"report": {"issues": [{"type": "no_current_candidate"}]}}
        )
        == 1
    )
    assert capture_gui.determine_exit_code("cancelled", "cancelado pelo usuário") == 1
    assert capture_gui.determine_exit_code("error", {"message": "x", "traceback": "y"}) == 1
