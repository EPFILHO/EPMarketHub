from datetime import UTC, date, datetime

from market_analytics.contract_tracking import empty_tracker, update_contract_tracker
from market_analytics.daily_capture import build_current_contract_plan

NOW = datetime(2026, 8, 31, 22, tzinfo=UTC)
SESSION = date(2026, 8, 31)


def _state(symbol: str, *, volume: float, expiration: datetime, tradable: bool = True) -> tuple[str, dict]:
    return symbol, {
        "tradable": tradable,
        "has_quote": True,
        "session_volume": volume,
        "session_deals": int(volume),
        "expiration_time": int(expiration.timestamp()),
    }


def test_tracker_adds_active_contracts_with_stable_identity() -> None:
    states = dict(
        [
            _state("WINV26", volume=10_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
            _state("WDOU26", volume=20_000, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)

    plan, tracker, issues = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )

    assert [item.selection.spec.symbol for item in plan] == ["WDOU26", "WINV26"]
    assert {row["state"] for row in tracker["contracts"]} == {"active"}
    assert {row["logical_id"] for row in tracker["contracts"]} == {
        "win_contract_winv26",
        "wdo_contract_wdou26",
    }
    assert issues == ()


def test_tracker_keeps_previous_contract_until_expiration_after_liquidity_roll() -> None:
    first_states = dict(
        [
            _state("WINV26", volume=50_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
            _state("WINZ26", volume=10_000, expiration=datetime(2026, 12, 16, tzinfo=UTC)),
            _state("WDOU26", volume=60_000, expiration=datetime(2026, 9, 1, 23, tzinfo=UTC)),
            _state("WDOV26", volume=5_000, expiration=datetime(2026, 10, 1, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(first_states, now_utc=NOW, session_date=SESSION)
    _plan, tracker, _issues = update_contract_tracker(
        empty_tracker(), active, first_states, now_utc=NOW, session_date=SESSION
    )

    later = datetime(2026, 9, 1, 12, tzinfo=UTC)
    rolled_states = dict(
        [
            _state("WINV26", volume=5_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
            _state("WINZ26", volume=80_000, expiration=datetime(2026, 12, 16, tzinfo=UTC)),
            _state("WDOU26", volume=2_000, expiration=datetime(2026, 9, 1, 23, tzinfo=UTC)),
            _state("WDOV26", volume=90_000, expiration=datetime(2026, 10, 1, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(rolled_states, now_utc=later, session_date=date(2026, 9, 1))

    plan, tracker, issues = update_contract_tracker(
        tracker,
        active,
        rolled_states,
        now_utc=later,
        session_date=date(2026, 9, 1),
    )

    assert {item.selection.spec.symbol for item in plan} == {
        "WINV26",
        "WINZ26",
        "WDOU26",
        "WDOV26",
    }
    states_by_symbol = {row["symbol"]: row["state"] for row in tracker["contracts"]}
    assert states_by_symbol == {
        "WINV26": "tracking_until_expiration",
        "WINZ26": "active",
        "WDOU26": "tracking_until_expiration",
        "WDOV26": "active",
    }
    assert issues == ()


def test_tracker_captures_expiring_contract_one_last_time_on_its_expiration_date() -> None:
    states = dict(
        [
            _state("WDOU26", volume=20_000, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)
    _plan, tracker, _issues = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )

    later = datetime(2026, 9, 1, 14, tzinfo=UTC)
    new_states = dict(
        [
            _state("WDOU26", volume=0, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
            _state("WDOV26", volume=50_000, expiration=datetime(2026, 10, 1, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(new_states, now_utc=later, session_date=date(2026, 9, 1))

    plan, tracker, issues = update_contract_tracker(
        tracker,
        active,
        new_states,
        now_utc=later,
        session_date=date(2026, 9, 1),
    )

    assert "WDOU26" in {item.selection.spec.symbol for item in plan}
    assert (
        {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"]
        == "tracking_until_expiration"
    )
    assert issues == ()


def test_tracker_stops_expired_contract_after_its_final_session() -> None:
    states = dict(
        [
            _state("WDOU26", volume=1, expiration=datetime(2026, 8, 31, 22, 15, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)
    _plan, tracker, _issues = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )
    later = datetime(2026, 9, 1, 22, tzinfo=UTC)
    new_states = dict(
        [
            _state("WDOU26", volume=0, expiration=datetime(2026, 8, 31, 22, 15, tzinfo=UTC)),
            _state("WDOV26", volume=50_000, expiration=datetime(2026, 10, 1, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(new_states, now_utc=later, session_date=date(2026, 9, 1))

    plan, tracker, issues = update_contract_tracker(
        tracker,
        active,
        new_states,
        now_utc=later,
        session_date=date(2026, 9, 1),
    )

    assert "WDOU26" not in {item.selection.spec.symbol for item in plan}
    assert {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"] == "expired"
    assert issues == ()


def test_tracker_flags_missing_before_expiration_when_symbol_vanishes_before_its_final_session() -> None:
    """Regressão da auditoria: um contrato ativo cuja expiração conhecida
    ainda cobre a sessão pedida, mas que simplesmente sumiu de
    ``symbol_states`` (corretora não devolveu mais o símbolo), nunca pode
    virar ``unavailable`` em silêncio — é um ``missing_before_expiration``
    com issue estruturada, distinto do encerramento normal.
    """

    states = dict(
        [
            _state("WDOU26", volume=20_000, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)
    _plan, tracker, _issues = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )
    assert {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"] == "active"

    # No dia seguinte -- que é exatamente a sessão de vencimento de WDOU26 --
    # o símbolo desaparece por completo de symbol_states (sem entrada
    # nenhuma), embora WINV26 continue saudável.
    later = datetime(2026, 9, 1, 22, tzinfo=UTC)
    session_2 = date(2026, 9, 1)
    new_states = dict(
        [
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(new_states, now_utc=later, session_date=session_2)

    plan, tracker, issues = update_contract_tracker(
        tracker,
        active,
        new_states,
        now_utc=later,
        session_date=session_2,
    )

    assert "WDOU26" not in {item.selection.spec.symbol for item in plan}
    assert {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"] == (
        "missing_before_expiration"
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue["type"] == "missing_before_expiration"
    assert issue["symbol"] == "WDOU26"
    assert issue["instrument"] == "wdo"
    assert issue["logical_id"] == "wdo_contract_wdou26"
    assert issue["session_date"] == session_2.isoformat()


def test_catalog_confirmed_symbol_never_flags_missing_before_expiration() -> None:
    """Regressão da revisão Codex: reexecutar no MESMO dia depois que a
    sessão pedida já foi capturada com sucesso (completed/empty e
    verificada no catálogo) e o símbolo então sumiu não pode gerar
    ``missing_before_expiration`` -- isso seria um falso alarme sobre uma
    sessão que já está arquivada e auditada. `catalog_confirmed_symbols` é
    a única forma de suprimir essa issue; o resto do comportamento é
    idêntico ao teste acima.

    `session_2` aqui é deliberadamente igual ao dia de vencimento real de
    WDOU26 (2026-09-01, o mesmo usado em `expiration`): só nesse dia exato
    a confirmação do catálogo pode encerrar o acompanhamento. Ver o teste
    de contraste abaixo para o caso "antes do vencimento".
    """

    states = dict(
        [
            _state("WDOU26", volume=20_000, expiration=datetime(2026, 9, 1, 13, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)
    _plan, tracker, _issues = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )

    # Mesmo "later"/mesma sessão do teste anterior, mas desta vez o
    # orquestrador já confirmou (fora deste módulo puro) que a sessão de
    # WDOU26 está completed/empty e verificada no catálogo.
    later = datetime(2026, 9, 1, 22, tzinfo=UTC)
    session_2 = date(2026, 9, 1)
    new_states = dict(
        [
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(new_states, now_utc=later, session_date=session_2)

    plan, tracker, issues = update_contract_tracker(
        tracker,
        active,
        new_states,
        now_utc=later,
        session_date=session_2,
        catalog_confirmed_symbols=frozenset({"WDOU26"}),
    )

    assert "WDOU26" not in {item.selection.spec.symbol for item in plan}
    assert {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"] == "expired"
    assert issues == ()


def test_catalog_confirmed_symbol_before_final_session_still_flags_missing_before_expiration() -> None:
    """Contraste com o teste acima (revisão Codex): se ``session_date`` for
    uma sessão ANTES do vencimento real -- mesmo que ela própria já esteja
    completed/empty e verificada no catálogo -- o desaparecimento continua
    ``missing_before_expiration``. `catalog_confirmed_symbols` só encerra o
    acompanhamento no dia exato de vencimento; antes disso ainda restam
    sessões futuras obrigatórias, e a confirmação de hoje não prova nada
    sobre elas.
    """

    states = dict(
        [
            # Vencimento real em 15/10 -- bem depois da sessão pedida a
            # seguir (01/09).
            _state("WDOU26", volume=20_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(states, now_utc=NOW, session_date=SESSION)
    _plan, tracker, _issues = update_contract_tracker(
        empty_tracker(), active, states, now_utc=NOW, session_date=SESSION
    )

    # A sessão de 2026-09-01 já está (hipoteticamente) completed/empty e
    # verificada no catálogo -- mas o vencimento real de WDOU26 é só em
    # 15/10, então ainda faltam sessões futuras obrigatórias.
    later = datetime(2026, 9, 1, 22, tzinfo=UTC)
    session_2 = date(2026, 9, 1)
    new_states = dict(
        [
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(new_states, now_utc=later, session_date=session_2)

    plan, tracker, issues = update_contract_tracker(
        tracker,
        active,
        new_states,
        now_utc=later,
        session_date=session_2,
        catalog_confirmed_symbols=frozenset({"WDOU26"}),
    )

    assert "WDOU26" not in {item.selection.spec.symbol for item in plan}
    assert {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"] == (
        "missing_before_expiration"
    )
    assert len(issues) == 1
    assert issues[0]["type"] == "missing_before_expiration"
    assert issues[0]["symbol"] == "WDOU26"


def test_tracker_still_reports_plain_unavailable_when_no_session_was_pending() -> None:
    """Contraste com o teste acima: se o símbolo sumido nunca teve uma
    expiração conhecida cobrindo a sessão pedida (ex.: já estava
    ``unavailable``/sem dado de expiração antes), o estado continua
    ``unavailable`` e nenhuma issue é emitida -- não é toda ausência que
    representa uma sessão perdida.
    """

    # A linha de WDOU26 já entra no registro como "unavailable", sem
    # expiration_utc coberta -- nunca chegou a ser "active"/
    # "tracking_until_expiration".
    tracker = empty_tracker()
    tracker["contracts"] = [
        {
            "symbol": "WDOU26",
            "instrument": "wdo",
            "logical_id": "wdo_contract_wdou26",
            "contract_month": "2026-09-01",
            "first_observed_utc": NOW.isoformat(),
            "last_observed_utc": NOW.isoformat(),
            "expiration_utc": None,
            "state": "unavailable",
        }
    ]

    later = datetime(2026, 9, 2, 22, tzinfo=UTC)
    session_2 = date(2026, 9, 2)
    new_states = dict(
        [
            _state("WINV26", volume=30_000, expiration=datetime(2026, 10, 15, tzinfo=UTC)),
        ]
    )
    active = build_current_contract_plan(new_states, now_utc=later, session_date=session_2)

    plan, tracker, issues = update_contract_tracker(
        tracker,
        active,
        new_states,
        now_utc=later,
        session_date=session_2,
    )

    assert "WDOU26" not in {item.selection.spec.symbol for item in plan}
    assert {row["symbol"]: row["state"] for row in tracker["contracts"]}["WDOU26"] == "unavailable"
    assert issues == ()
