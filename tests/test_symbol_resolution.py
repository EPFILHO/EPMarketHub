from core.market_snapshot import resolve_historical_symbol_alias, resolve_symbol_aliases


def test_prefers_tradable_cash_symbol_over_disabled_exact_alias():
    available = {"US30", "US30Cash"}
    states = {
        "US30": {"tradable": False, "has_quote": False, "visible": False},
        "US30Cash": {"tradable": True, "has_quote": True, "visible": True},
    }
    assert resolve_symbol_aliases(["US30", "US30Cash", "US30*"], available, states) == "US30Cash"


def test_returns_none_when_all_matching_symbols_are_disabled():
    available = {"US100", "US100Cash"}
    states = {
        "US100": {"tradable": False, "has_quote": False},
        "US100Cash": {"tradable": False, "has_quote": True},
    }
    assert resolve_symbol_aliases(["US100", "US100Cash", "US100*"], available, states) is None


def test_preserves_alias_order_when_both_are_tradable_and_quoted():
    available = {"US500Cash", "US500.cash"}
    states = {
        "US500Cash": {"tradable": True, "has_quote": True},
        "US500.cash": {"tradable": True, "has_quote": True},
    }
    assert resolve_symbol_aliases(["US500Cash", "US500.cash"], available, states) == "US500Cash"


# COR-DEV-002: resolução histórica/de dados, separada da operacional acima.
# Usada somente pelo diagnóstico de ticks; nunca por snapshot ou streaming.


def test_historical_resolver_accepts_listed_non_tradable_exact_alias_with_quote():
    """WIN$ está listado, é exato e possui cotação, mas não é negociável na
    Clear; o resolvedor histórico deve aceitá-lo mesmo assim."""

    available = {"WIN$"}
    assert resolve_historical_symbol_alias(["WIN$"], available) == "WIN$"


def test_historical_resolver_preserves_priority_of_earlier_exact_alias_over_tradable_one():
    """Com WIN$ como primeiro alias exato existente, o resolvedor histórico
    deve escolhê-lo mesmo que um alias posterior seja negociável — a
    prioridade declarada não é reordenada por tradabilidade aqui."""

    available = {"WIN$", "WINV26"}
    assert resolve_historical_symbol_alias(["WIN$", "WINV26"], available) == "WIN$"


def test_operational_resolver_still_returns_none_when_all_candidates_disabled():
    """Regressão: o resolvedor operacional existente não muda de
    comportamento nesta correção e continua recusando candidatos não
    negociáveis, mesmo que o histórico aceitasse o mesmo alias."""

    available = {"WIN$"}
    states = {"WIN$": {"tradable": False, "has_quote": True}}
    assert resolve_symbol_aliases(["WIN$"], available, states) is None


def test_historical_resolver_returns_none_when_alias_is_not_listed():
    assert resolve_historical_symbol_alias(["WIN$"], set()) is None


# Auditoria da primeira entrega da COR-DEV-002: na ausência de correspondência
# exata, um curinga só pode resolver quando aponta para um único símbolo
# distinto; nunca por desempate alfabético entre vários nomes encontrados.


def test_historical_resolver_accepts_unique_wildcard_match():
    available = {"WINV26"}
    assert resolve_historical_symbol_alias(["WIN*"], available) == "WINV26"


def test_historical_resolver_rejects_ambiguous_wildcard_matches():
    available = {"WINV26", "WINZ26"}
    assert resolve_historical_symbol_alias(["WIN*"], available) is None


def test_historical_resolver_exact_alias_wins_over_ambiguous_wildcard():
    available = {"WIN$", "WINV26", "WINZ26"}
    assert resolve_historical_symbol_alias(["WIN$", "WIN*"], available) == "WIN$"
