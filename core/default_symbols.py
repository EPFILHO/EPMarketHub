from __future__ import annotations

from .models import SymbolDefinition

DEFAULT_SYMBOLS: list[SymbolDefinition] = [
    SymbolDefinition("eurusd", "Euro/Dólar", "Forex", ["EURUSD", "EURUSDm", "EURUSD.pro", "EURUSD*"], ["usd_strength"]),
    SymbolDefinition("gbpusd", "Libra/Dólar", "Forex", ["GBPUSD", "GBPUSDm", "GBPUSD.pro", "GBPUSD*"], ["usd_strength"]),
    SymbolDefinition("usdjpy", "Dólar/Iene", "Forex", ["USDJPY", "USDJPYm", "USDJPY.pro", "USDJPY*"], ["usd_strength"]),
    SymbolDefinition("nasdaq100", "Nasdaq 100", "Índices", ["US100", "US100Cash", "US100.cash", "NAS100", "NAS100Cash", "NAS100.cash", "USTEC", "US100*", "NAS100*", "USTEC*"], ["indices_divergence", "risk_sentiment"]),
    SymbolDefinition("sp500", "S&P 500", "Índices", ["US500", "US500Cash", "US500.cash", "SPX500", "SPX500Cash", "SPX500.cash", "SP500", "US500*", "SPX500*", "SP500*"], ["indices_divergence", "risk_sentiment"]),
    SymbolDefinition("dowjones", "Dow Jones", "Índices", ["US30", "US30Cash", "US30.cash", "DJ30", "DJ30Cash", "DJ30.cash", "WS30", "US30*", "DJ30*", "WS30*"], ["indices_divergence"]),
    SymbolDefinition("gold", "Ouro", "Metais", ["XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.pro", "XAUUSD*", "GOLD*"], ["risk_sentiment", "usd_confirmation"]),
    SymbolDefinition("bitcoin", "Bitcoin", "Cripto", ["BTCUSD", "BTCUSDm", "BTCUSD.pro", "BTCUSD.cash", "BTCUSD*", "BTC*USD*"], ["risk_sentiment"]),
    SymbolDefinition("win", "Índice Futuro (WIN)", "Brasil / B3", ["WIN$", "WIN", "WIN.c", "WIN#"], ["b3", "indices_divergence", "risk_sentiment"]),
    SymbolDefinition("winq26", "Índice Futuro Atual (WINQ26)", "Brasil / B3", ["WINQ26"], ["b3", "indices_divergence", "risk_sentiment"]),
    SymbolDefinition("wdo", "Dólar Futuro (WDO)", "Brasil / B3", ["WDO$", "WDO", "WDO.c", "WDO#"], ["b3", "usd_strength", "risk_sentiment"]),
]
