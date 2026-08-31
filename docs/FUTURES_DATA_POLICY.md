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
ser tratado como série completa. Até existir uma fonte melhor:

- `WDO@` é a referência proporcional alternativa para pesquisa de regime;
- `WDO$D` é a referência por liquidez para variáveis em pontos;
- ambos conservam identidade de fonte/método e não são unidos
  silenciosamente;
- os contratos individuais coletados daqui em diante prevalecem sobre as
  referências sintéticas no período coberto.

## Automação aprovada

A tarefa do Windows executa `scripts/run_b3_contract_capture_scheduled.ps1`
às 19h15, de segunda a sexta. A GUI inicia sozinha, exibe o progresso, toca o
aviso final e fecha trinta segundos depois do sucesso. A tarefa não inicia o
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
