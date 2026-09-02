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

## Histórico M1 e features para o Fusion Quant DEV-009C.1 (DEV-007)

`market_analytics/win_m1_collector.py`, `win_m1_inventory.py` e
`win_m1_features.py` produzem, offline e somente leitura, o contexto
histórico M1/M5 do `WIN$` (Clear, terminal de pesquisa) que o Fusion Quant
consome para explicar os 21 backtests mensais do `WIN_copy_2`. Esta entrega
é só o **produtor**: não analisa resultado de backtest, não escolhe
estratégia, não classifica regime — ver `docs/work_orders/DEV-007.md`.

A dependência de `MetaTrader5` fica inteiramente atrás de `RatesProvider`
(`win_m1_collector.py`), um `Protocol` estrutural; `Mt5RatesProvider` é o
adaptador real (importa `MetaTrader5` só dentro dos próprios métodos) usado
pela CLI, nunca pelos testes. Comando único de execução futura (não
executado por esta entrega — nenhum terminal MT5 é aberto):

```powershell
python tools/collect_win_m1_history.py `
  --terminal-path "C:\Users\Famil\Documents\Codex\Fusion\Fusion-Quant\runtime\mt5-clear-research\terminal64.exe" `
  --inventory-path "C:\...\runtime\win_copy2_monthly_retro\inventory_m1.json" `
  --inventory-sha256 59589F49F519742FCA11F4FF7F71566B6400EFDFD9745D8758918F718A203B5C `
  --output-root D:\EPData\MarketHub\analytics\win_m1_history
```

Janela congelada: `2026-01-01 00:00:00` até `2026-08-01 00:00:00` (UTC,
semiaberta), solicitada e validada **mês a mês** por
`fetch_and_validate_month`. O `date_to` enviado ao provider é sempre
**inclusivo** — `23:59:59` do último dia do mês (`month_windows` calcula
`fim_exclusivo - 1s`), nunca `00:00:00` do mês seguinte, porque
`copy_rates_range` do MT5 trata `date_to` como inclusivo (o inventário do
Fusion Quant foi gerado com esse mesmo fim por mês). A validação semântica
de cada barra devolvida continua semiaberta (`[início, fim_exclusivo)`).
Cada mês é recusado (tudo-ou-nada, sem reconciliação parcial) por: retorno
vazio inesperado, linha malformada ou OHLC não finito, timestamp
duplicado/não crescente após ordenar, timestamp fora do mês, ou barra OHLC
inconsistente (`Bar.__post_init__`).

**Fingerprint — algoritmo exato do Fusion Quant.** `win_m1_inventory.py`
monta, para cada barra, uma linha canônica `"|"`-separada — `time`
(`str(int(...))`), `open`/`high`/`low`/`close` (`format(float(...), ".10f")`)
e `tick_volume`/`spread`/`real_volume` (`str(int(...))`) — seguida de um
byte `\n`; o SHA-256 de toda a sequência do mês, em **hexadecimal
maiúsculo**, é o fingerprint. `WinCopy2Inventory`/`load_inventory_file`
conferem primeiro o SHA-256 do arquivo de inventário inteiro (argumento
explícito, nunca hardcoded) antes de qualquer parse, e a comparação por mês
(`collect_validated_history`) sempre usa o fingerprint **integral** — nunca
o prefixo de 16 caracteres, que é só evidência humana.

**Schema do inventário — estrito, sem tolerância especulativa.** O parser
exige exatamente `schema="fusion-quant-mt5-history-inventory-v1"`,
`symbol="WIN$"`, `timeframe="M1"`, `start_month="2026-01"`,
`end_month="2026-07"` e `months` como lista, com um registro por mês
(`requested_month`/`bars`/`bars_fingerprint`/`status`/
`outside_range_count`/`stable`) para exatamente jan..jul/2026, uma vez
cada — nem a mais, nem a menos. Cada registro exige `stable is True` e
`outside_range_count == 0`; campo desconhecido, ausente ou valor fora do
esperado é sempre recusado. `preflight` também confere que o `symbol`
efetivamente solicitado bate com o do inventário carregado, antes de tocar
MT5.

**Preflight antes de qualquer MT5.** `win_m1_features.preflight` (protocolo
congelado quando aplicável, destino, hash/schema do inventário, identidade
do símbolo) roda inteiramente antes de `Mt5RatesProvider` ser construído —
a CLI chama `preflight(..., require_frozen_protocol=True)` e só então entra
no `with Mt5RatesProvider(...)`. Na CLI real, o destino também precisa estar
dentro de `D:\EPData\MarketHub` (`assert_output_within_allowed_root`).
`Mt5RatesProvider.__enter__` confirma, nesta ordem, `initialize()`,
`terminal_info().connected` e `symbol_select(symbol, True)`, desligando o
terminal e recusando claramente antes de qualquer consulta de barras se
qualquer uma falhar.

**Promoção atômica e qualidade de volume.** As barras M1 validadas são
persistidas particionadas por ano/mês
(`m1/year=YYYY/month=MM/bars_m1.parquet`, preservando `tick_volume`,
`spread` e `real_volume` como campos factuais, além de `volume`/
`volume_quality` já decididos, com metadados determinísticos — sem nenhum
campo de horário de execução). A política de volume decide por barra:
`real_volume` vira `"exchange"` quando inteiro não negativo; caso contrário
cai para `"tick_proxy"` usando `tick_volume`. `aggregate_bars`
(`market_analytics/quant_mvp.py`, reaproveitado tanto pelo DEV-006 quanto
pelo DEV-007) preserva essa qualidade no agregado M5 somente quando todas as
M1 do bucket compartilham a mesma qualidade; qualquer mistura (ou uma barra
`"missing"`) produz `volume=None`/`volume_quality="missing"` em vez de somar
grandezas incompatíveis — nunca força `"exchange"` como a versão anterior
fazia. Toda a escrita usa a mesma técnica de promoção transacional
recuperável do DEV-006 (`win_m1_features._promote_output`): diretório
temporário próprio, promovido só após todos os artefatos serem gravados com
sucesso, com backup intermediário que restaura a saída anterior se a
segunda troca falhar.

**Warm-up contínuo e `available_at_utc` sem look-ahead.** Os sete meses
validados são concatenados numa única série M1 cronológica antes de agregar
para M5 (`market_analytics.quant_mvp.aggregate_bars`, reaproveitado sem
reimplementação) e calcular features
(`market_analytics.pipeline.compute_feature_rows`, também reaproveitado): o
warm-up nunca reinicia no primeiro dia de um mês. `timestamp` de cada barra
M5 continua sendo a abertura do bucket; `available_at_utc` (persistido só
no artefato de features, não em `Bar`) é a abertura mais a duração do
timeframe — o encerramento do bucket. Uma barra M5 aberta às 09:05 só fica
disponível às 09:10; um consumidor decidindo às 09:06 só pode usar a M5
aberta às 09:00 (disponível às 09:05).

Artefatos gerados sob a pasta de saída: `m1/year=*/month=*/bars_m1.parquet`,
`bars_features_M5.parquet` (com `available_at_utc` ao lado de
`timestamp_utc`, ambos `timestamp(us, tz=UTC)` nativos do Arrow, e metadados
de proveniência — schema/versão/produtor, fonte, `FeatureConfig` como JSON,
políticas de volume/disponibilidade — também determinísticos),
`coverage_months.csv` (contagem/fingerprint obtidos vs. esperados por mês),
`feature_summary.csv` (mesma forma do DEV-006, só para M5) e
`run_summary.json` (hashes/fingerprints por mês, hash do inventário,
`FeatureConfig` completa, políticas de preço/volume/disponibilidade,
hashes dos próprios artefatos). Determinístico salvo `generated_at_utc`/
`duration_seconds`/`output_root` — nenhum desses três campos voláteis é
embutido nos Parquets em si, só em `run_summary.json` (auditoria Codex:
`generated_at_utc` por mês nos metadados do Parquet M1 quebrava a
byte-identidade entre execuções; removido).
