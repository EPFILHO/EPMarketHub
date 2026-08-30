# Fundação quantitativa de regimes — fatia 1

## Fronteira EP Market Hub → Fusion Quant

- **EP Market Hub** (este repositório) é **produtor**: observa barras
  fechadas de qualquer mercado (B3, Forex, índices, cripto) e calcula
  features determinísticas de volatilidade/tendência versionadas em
  `market_analytics/`. Não classifica regime, não pontua, não decide nada.
- **Fusion Quant** (repositório separado) é **consumidor**: lê os artefatos
  de features produzidos aqui e os cruza com resultados de backtest para
  treinar/validar classificadores de regime.
- **EP Fusion não faz parte desta fatia** e não é referenciado pelo código
  deste pacote.
- O módulo `market_analytics/` é neutro por mercado: nenhuma constante,
  nome de campo ou lógica assume B3, Forex ou qualquer corretora específica.

## Semântica de volume

Volume nunca é presumido. Toda barra (`Bar`) carrega `volume_quality`
explícito:

- `"exchange"`: volume real reportado pela fonte (ex.: B3).
- `"tick_proxy"`: contagem de ticks usada como aproximação (comum em
  Forex/CFDs no MT5, que não tem volume real centralizado).
- `"missing"`: a fonte não fornece nenhuma medida de volume.

Quando a qualidade é `"missing"`, `volume` é sempre `None` — nunca `0`. Um
zero imputado seria indistinguível de "mercado real sem negociação" e
contaminaria qualquer feature derivada. `volume_relative` só é calculado
quando a barra atual e o histórico usado na média móvel têm a **mesma**
qualidade; `exchange` e `tick_proxy` nunca são misturados na mesma média
porque suas escalas não são comparáveis.

## Identidade da fonte

Toda `Bar` e `FeatureRow` carrega `source_id` (terminal/corretora/feed de
origem), não vazio. O mesmo `symbol` pode existir em fontes diferentes com
preços e horários de fechamento distintos; sem `source_id` essas séries
colidiriam silenciosamente. `compute_feature_rows` recusa uma chamada que
misture mais de uma `source_id`/`symbol`/`timeframe`.

## Configuração reproduzível

`FeatureConfig` (`market_analytics/config.py`) é a única fonte de verdade
para os parâmetros de janela (`atr_period`, `volatility_window`,
`trend_window`, `volume_window`, todos inteiros positivos). O artefato
persiste a configuração usada por completo, para que o Fusion Quant nunca
precise adivinhar com quais parâmetros uma linha foi calculada.

## Warm-up comparável

Cada métrica de janela retorna `None` até a janela completa definida por
`FeatureConfig` estar disponível — nunca uma média parcial encolhida. Isso
mantém o warm-up comparável entre produtor e consumidor: um valor não-`None`
sempre representa exatamente a janela configurada, nunca uma amostra menor.
Detalhes por métrica estão documentados no docstring de módulo de
`market_analytics/features.py`.

## Sem vazamento (leakage)

Toda função de feature em `market_analytics/features.py` recebe
explicitamente o prefixo de dados disponível até a barra corrente. O
pipeline (`market_analytics/pipeline.py`) nunca olha para barras futuras.
Isso é validado por teste de invariância de prefixo: recalcular com um
sufixo de barras a mais não pode alterar os valores já calculados para o
prefixo anterior.

## Persistência

`market_analytics/storage.py` grava um artefato JSON versionado
(`schema_version`) por símbolo/timeframe, com escrita atômica
(arquivo temporário + `fsync` + `replace`). Este formato é adequado para
pesquisa inicial e para o Fusion Quant validar o contrato de dados. Uma
persistência escalável (banco, Parquet, particionamento por data) é
deliberadamente adiada para uma fatia futura — ver `docs/ROADMAP.md`.

## Fora de escopo nesta fatia (fatia 1 — features)

Não há, e não deve haver, nesta fatia: classificador de regime, scoring,
machine learning, banco de dados, UI, QWebChannel, integração com workers
MT5 ou terminal ao vivo. `bars.py`/`config.py`/`features.py`/`pipeline.py`/
`storage.py` continuam um núcleo Python puro (biblioteca padrão), importável
e testável isoladamente de `core/` e `gui/`, sem MT5.

## Fundação de backfill histórico (DEV-002, Portão A)

O mesmo pacote `market_analytics/` também contém, desde o DEV-002, a
fundação de backfill de ticks históricos (`tick_backfill.py`,
`backfill_writer.py`, `backfill_catalog.py`, `backfill_runner.py`). Ao
contrário da fatia 1, esses módulos **não são mais dependentes só da
biblioteca padrão**:

- `backfill_writer.py` é o único módulo que importa `pyarrow` (escrita/
  leitura de Parquet com compressão Zstandard);
- `backfill_catalog.py` usa `sqlite3` da biblioteca padrão para o catálogo
  operacional (jobs, tentativas, estado, contagens, hash, retomada) — nunca
  guarda ticks brutos;
- `tick_backfill.py` e `backfill_runner.py` continuam sem MT5/Qt, mas
  `backfill_runner.py` depende dos dois módulos acima.

`tick_backfill.py`/`backfill_runner.py` reaproveitam deliberadamente
`tick_diagnostics.py` (`TickWindow`, `TickWindowAccumulator`, `TickRecord`,
limites de `chunk_seconds`) em vez de duplicar essas regras já validadas.
Backfill, fluxo ao vivo e diagnóstico de ticks são mutuamente exclusivos no
mesmo worker — ver `docs/KERNEL_PROTOCOL.md`.

O Portão A implementa e testa contratos, escritor, catálogo e comando/
eventos com fontes falsas em diretórios temporários; nenhuma coleta real,
GUI ou integração com `D:\EP\EPMarketHub` acontece nesta fatia — ver
`docs/work_orders/DEV-002.md`.
