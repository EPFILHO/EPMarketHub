from core.market_snapshot import resolve_symbol_aliases


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
