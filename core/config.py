"""Políticas centrais do kernel do EP Market Hub."""

# Decisão de produto, não uma preferência do usuário. Mude este valor em uma
# versão validada para alterar quantos MT5/workers podem ficar ativos ao mesmo
# tempo. Cadastros de terminais continuam ilimitados.
MAX_ACTIVE_TERMINALS = 3

# Raiz padrão dos dados históricos brutos do backfill (DEV-002). Deliberadamente
# fora do repositório e fora de `D:\EP\EPMarketHub`: é dado reconstruível, nunca
# versionado. Um valor explícito (ex.: um diretório temporário de teste) sempre
# tem precedência sobre este padrão — ver `core.mt5_worker.mt5_worker_main` e
# `core.worker_manager.MT5WorkerManager`.
DEFAULT_MARKET_DATA_ROOT = r"D:\EPData\MarketHub"
