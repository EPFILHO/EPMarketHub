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

Desde o schema de catálogo v2, cada tentativa `running` registra a identidade
do processo proprietário (`owner_pid`, instante de criação e terminal). O
supervisor pode assim recuperar automaticamente sessões após uma queda total
sem confundir ausência temporária de heartbeat com morte e sem tomar uma
tentativa pertencente a processo ainda vivo. Catálogos v1 são migrados sem
perda; linhas legadas sem identidade permanecem conservadoramente intocadas.

O Portão A implementa e testa contratos, escritor, catálogo e comando/
eventos com fontes falsas em diretórios temporários; nenhuma coleta real,
GUI ou integração com `D:\EP\EPMarketHub` acontece nesta fatia — ver
`docs/work_orders/DEV-002.md`.

## Contratos futuros e proveniência (DEV-003)

A política atual de futuros está em `docs/FUTURES_DATA_POLICY.md`. A camada
bruta distingue contratos individuais de todas as combinações contínuas de
rolagem (liquidez/vencimento) e ajuste (proporcional/diferença). Essa
proveniência acompanha a solicitação, sua impressão digital e os metadados
do Parquet; uma retomada recusa um artefato cuja semântica não corresponda à
solicitação atual.

`market_analytics/futures_series.py` contém a taxonomia e a seleção pura do
contrato B3 atual. `market_analytics/daily_capture.py` decide apenas a sessão
fechada. O controlador local `tools/b3_contract_capture_gui.py` usa esses
contratos e a fundação de backfill existente; nenhuma IA participa da
transferência dos ticks.

## MVP quantitativo WIN: ticks brutos → barras, features e relatório (DEV-006)

`market_analytics/quant_mvp.py` transforma as sessões já coletadas de
`raw/clear/win/year=*/month=*/session_date=*/ticks.parquet` num conjunto
analítico multi-timeframe. É um processamento local, determinístico e
somente leitura sobre os Parquets brutos — nenhum MT5, terminal ao vivo ou
IA participa. Comando único e reproduzível:

```powershell
python tools/run_quant_mvp.py `
  --input-root D:\EPData\MarketHub\raw\clear\win `
  --output-root D:\EPData\MarketHub\analytics\win_mvp
```

Escopo travado: só `source_id=clear`, `logical_id=win`,
`resolved_symbol=WIN$`; qualquer outro metadado embutido, `session_date`
divergente da partição, schema/versão não suportados ou timestamps fora de
ordem fazem a sessão ser **rejeitada e registrada em `alerts`**, sem abortar
o lote inteiro. Cada sessão é lida por row group/batch (nunca todos os
ticks nem todas as sessões em memória de uma vez).

Política de tick B3 desta fonte:

- preço operacional = `last`, aceitando somente valores finitos e `> 0`;
  não depende de um bit isolado de `flags` (valores compostos 1080/1336 são
  aceitos quando `last`/`volume_real` são válidos);
- volume = soma de `volume_real` finito e não negativo das ticks válidas;
  um `volume_real` inválido contribui como `0`, sem invalidar a tick;
- remove somente duplicatas exatas adjacentes (todos os campos brutos
  iguais ao registro imediatamente anterior), inclusive na fronteira entre
  batches; empates de `time_msc` com conteúdo diferente são válidos;
- nunca preenche minutos sem negócio nem cria candles sintéticos.

M1 é construído diretamente dos ticks (barras ancoradas no relógio UTC,
com a `session_date` de origem anotada à parte, já que `Bar` não carrega
esse campo); M5/M15/M30/H1 são sempre derivados do M1 — nunca de outro
timeframe derivado. O cálculo de features reaproveita integralmente
`Bar`/`FeatureConfig`/`compute_feature_rows`, concatenando as barras de
todas as sessões processadas em ordem cronológica por timeframe (mesma
regra de warm-up e ausência de vazamento documentada acima).

Artefatos gerados sob a pasta de saída: `bars_features_{M1,M5,M15,M30,H1}.parquet`
(com `timestamp_utc` como coluna Arrow `timestamp(us, tz=UTC)`
timezone-aware — não uma string ISO — para que o consumidor não precise
reanalisar texto para obter um tipo de tempo nativo comparável/ordenável),
`session_summary.csv` (uma linha por sessão: OHLC, retorno, amplitude,
contagens de ticks lidos/válidos/duplicados, volume, primeira/última
observação e barras por timeframe — `first_observation_utc`/
`last_observation_utc` são os timestamps exatos do primeiro/último tick
válido, com segundo/milissegundo preservados, nunca o início do minuto da
barra M1 correspondente), `feature_summary.csv` (contagem, ausentes e
percentis p10/p25/p50/p75/p90 por timeframe/feature) e `run_summary.json`
(config de features, política de preço/volume, contagens, alertas,
hashes/contagens de cada Parquet de origem — via
`market_analytics.backfill_writer.inspect_final_file`, sem duplicar a
leitura segura já validada no Portão A — e hashes dos próprios artefatos).

Antes de tocar o disco, `run_quant_mvp` normaliza (`Path.resolve`) e valida
`input_root`/`output_root`/`batch_size`: recusa `batch_size <= 0`, uma
saída igual/sobreposta à entrada em qualquer direção (saída dentro da
entrada, entrada dentro da saída ou os dois iguais) e uma saída igual à
raiz de um volume — nenhum `mkdir` nem leitura acontece antes dessa
checagem.

A escrita é sempre para um diretório temporário da própria execução,
promovido para a pasta de saída somente depois que todos os artefatos
foram gravados com sucesso, através de uma promoção **transacional e
recuperável** (`_promote_output`), não de uma substituição atômica de
diretório de ponta a ponta — o Windows/NTFS não oferece essa operação para
diretórios via biblioteca padrão. Em vez disso: (1) se já existir uma
saída anterior, ela é renomeada (atômico) para um backup único ao lado;
(2) o diretório temporário é renomeado (atômico) para a pasta de saída;
(3) se o passo 2 falhar, o backup do passo 1 é restaurado integralmente
antes de propagar o erro — a saída anterior nunca é perdida nem fica
ausente; (4) o backup só é removido depois que o passo 2 tiver sucesso.
Existe uma janela real entre os passos 1 e 2 em que nada existe sob o nome
final — a garantia é de recuperabilidade determinística, não de
atomicidade de diretório. Uma falha em qualquer etapa anterior à promoção
remove o temporário e nunca toca uma saída existente. Os Parquets brutos
de `input_root` nunca são escritos.

Duas execuções sobre a mesma entrada produzem os mesmos artefatos
analíticos (parquets/CSVs byte-idênticos); só `generated_at_utc` e
`duration_seconds` do `run_summary.json` variam entre execuções. Os
percentis desta amostra são apenas descritivos — fora de escopo nesta
fatia: classificação de regime, machine learning, backtest ou qualquer
integração com o EPFusion (ver `docs/work_orders/DEV-006.md`).
