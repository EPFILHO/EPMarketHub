# Política de dados de futuros

## Princípio

O Market Hub preserva primeiro o fato negociado e só depois produz visões
analíticas. Um contrato contínuo é uma série sintética; nunca substitui o
contrato individual para preço de execução, tick size, slippage ou P&L.

## Camadas

1. **Bruto contratual** — ticks do contrato individual (`WINV26`, `WDOU26`
   etc.), sem rolagem nem ajuste. É a verdade para execução, ranges e
   calibração em pontos.
2. **Referência contínua da fonte** — símbolos fornecidos pela corretora,
   sempre com identidade própria por regra de rolagem e ajuste. Servem para
   recuperar história indisponível dos contratos e para controles; não são
   rotulados como preço executável.
3. **Contínuo derivado** — será produzido a partir da cadeia de contratos
   individuais, com mapa de rolagens versionado. A mesma base bruta poderá
   gerar tanto série proporcional quanto por diferença sem duplicar ticks.

## Matriz B3 observada na Clear

| Símbolo | Rolagem | Ajuste | Identificador estável |
|---|---|---|---|
| `WIN$` | liquidez | proporcional | `win_cont_liq_ratio` |
| `WIN$D` | liquidez | diferença | `win_cont_liq_diff` |
| `WIN@` | vencimento | proporcional | `win_cont_exp_ratio` |
| `WIN@D` | vencimento | diferença | `win_cont_exp_diff` |
| `WDO$` | liquidez | proporcional | `wdo_cont_liq_ratio` |
| `WDO$D` | liquidez | diferença | `wdo_cont_liq_diff` |
| `WDO@` | vencimento | proporcional | `wdo_cont_exp_ratio` |
| `WDO@D` | vencimento | diferença | `wdo_cont_exp_diff` |

Essas identidades estão declaradas em
`market_analytics/futures_series.py`. Duas variantes nunca compartilham o
mesmo `logical_id` ou partição.

## Uso recomendado

- regime, retorno, tendência relativa e comparação entre ativos:
  proporcional;
- ATR, range, stop e alvo em pontos: por diferença ou, preferencialmente,
  contrato individual;
- backtest de execução: contrato individual e rolagem/custos explícitos;
- volume: sempre o volume do contrato individual e sua qualidade declarada,
  nunca volume somado de séries incompatíveis.

Não existe uma série universalmente correta. O produto analítico deve
declarar a unidade da feature: absoluta (pontos) ou relativa
(percentual/normalizada).

## Captura diária do contrato real

`tools/b3_contract_capture_gui.py` conecta ao terminal Clear, lê os contratos
WIN/WDO listados e seleciona o contrato atual de forma conservadora:

1. descarta contrato desabilitado ou cuja expiração informada pela corretora
   já passou;
2. prioriza volume e número de negócios da sessão;
3. sem medidas de liquidez, usa cotação válida e o vencimento mais próximo;
4. exige um nome contratual exato; não resolve um curinga ambíguo.

O contrato selecionado entra em `tracked_contracts.json`. Quando a liquidez
migra, o novo contrato é acrescentado, mas o anterior continua sendo
capturado enquanto permanecer listado, negociável e não expirado. Essa
sobreposição preserva o ciclo até o término e fornece evidência para definir
depois a rolagem derivada. Contrato expirado ou indisponível permanece no
registro com estado explícito; nunca é apagado nem substituído por outro nome.
Se um contrato rastreado sumir do terminal **antes** de sua sessão de
vencimento conhecida ter sido capturada, o registro usa o estado
`missing_before_expiration` (em vez de `unavailable`) e uma issue estruturada
é emitida — essa perda nunca fica silenciosa: aparece em `report["issues"]` e
também como um resultado operacional não-sucedido em `report["results"]`. A
única exceção é o próprio dia de vencimento: se essa sessão exata já estiver
`completed`/`empty` e verificada no catálogo, sumir é o término normal do
ciclo (`expired`), não uma perda — mas em qualquer sessão **antes** do
vencimento real, mesmo já arquivada no catálogo, ainda faltam sessões
futuras obrigatórias e o desaparecimento continua `missing_before_expiration`.

Falha ao selecionar o contrato atual de UM instrumento (WIN ou WDO) — seja
por não haver candidato elegível, seja por a corretora recusar a seleção —
também vira issue estruturada, mas nunca aborta o outro instrumento: cada um
é planejado, tem seu tracker persistido e é capturado de forma independente.

Depois das 19h de São Paulo, captura o próprio dia útil; antes disso, captura
o dia útil anterior. Isso evita perder o contrato antigo no dia seguinte a
uma rolagem. Feriados não são inventados: uma resposta vazia permanece
`empty` e auditável.

Cada contrato usa uma partição diferente, por exemplo:

```text
raw/clear/win_contract_winv26/year=2026/month=08/session_date=2026-08-31/
raw/clear/wdo_contract_wdou26/year=2026/month=08/session_date=2026-08-31/
```

O Parquet carrega metadados de proveniência (`individual_contract`, sem
rolagem, sem ajuste, contrato, mês e expiração observada). O catálogo mantém
retomada, contagem, hash e estado. Ao mudar o contrato, o novo código cria
naturalmente outra partição e o anterior não é sobrescrito.

## Política para o histórico incompleto atual

`WIN$` continua sendo a referência proporcional preferida para regimes. O
histórico de `WDO$` observado na Clear contém vazios relevantes e não pode
ser tratado como série completa. Por decisão do C3 em 31/08/2026:

- o backfill histórico amplo será feito somente para `WIN$`;
- `WDO$`, `WDO@` e `WDO$D` não serão usados para fabricar uma história
  canônica nem unidos para esconder lacunas;
- as amostras já coletadas permanecem preservadas como evidência separada;
- a base canônica de WDO será prospectiva, formada pelos contratos
  individuais capturados diariamente (com sementes em 28 e 31/08/2026);
- `WDO@` e `WDO$D` só poderão voltar como referências de pesquisa sob uma
  ordem futura própria, mantendo identidade de fonte/método.

## Automação aprovada

A tarefa do Windows executa `scripts/run_b3_contract_capture_scheduled.ps1`
às 19h05, de segunda a sexta — sempre depois das 19h00, hora em que a sessão
do dia já pode ser tratada como encerrada (ver seção anterior). A GUI inicia
sozinha, exibe o progresso, toca o aviso final e fecha trinta segundos
depois de um sucesso real, sem issues. Em caso de falha ou de issue crítica
(ex.: `missing_before_expiration`, instrumento sem candidato atual, seleção
recusada pela corretora), o processo termina com exit code diferente de
zero e a janela permanece aberta por 2 minutos para inspeção — o mesmo
prazo usado para uma falha com exceção. O lançador
`.ps1` espera de verdade o processo da GUI (`Start-Process -Wait -PassThru`)
e repassa esse exit code ao Agendador do Windows. A tarefa não inicia o
Strategy Tester nem qualquer IA.

## Limites

- A seleção reflete os metadados observados naquele terminal e registra a
  evidência; não afirma conhecer a regra interna exata da corretora.
- Ajustes e contínuos derivados permanecem etapa posterior. O bruto nunca é
  reescrito para “corrigir” uma rolagem.

## Referências metodológicas

- [QuantConnect — Continuous Futures](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/futures)
- [QuantConnect — US Futures Security Master](https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/quantconnect/us-futures-security-master)
- [Sierra Chart — Continuous Futures Contract Charts](https://www.sierrachart.com/index.php?l=doc/ContinuousFuturesContractCharts.html)
- [CME — Improving Time-Series Momentum Strategies](https://www.cmegroup.com/education/files/improving-time-series-momentum-strategies.pdf)
