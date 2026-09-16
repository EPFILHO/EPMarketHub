# Contrato do Atlas causal — v1

## Finalidade

O Atlas transforma barras M1 **já fechadas** em contexto de mercado
determinístico e temporalmente auditável. O núcleo vive em
`market_analytics/causal_atlas.py`, não conecta ao MT5 e não importa Qt nem o
kernel. Adaptadores de coleta e atualização pertencem às fases seguintes da
DEV-008.

“Local e autocontido” significa que a matemática roda no computador e não
depende de IA ou serviço externo. Não significa usar dados antigos: a
DEV-008B alimentará o núcleo com novas barras fechadas.

## Entrada

`build_causal_atlas` recebe:

- uma sequência cronológica de `AtlasBar` M1;
- um `AtlasManifest` com identidade da série, horário normalizado da sessão,
  tipo/ajuste da série e escadas causais.

Cada `Bar.timestamp` é o instante de abertura da barra, normalizado em UTC. A
barra fica disponível somente em `timestamp + 1 minuto`. Fonte, símbolo,
timeframe, OHLC, volume e qualidade do volume são validados pelo contrato
existente de `market_analytics.bars.Bar`.

O núcleo v1 trabalha com uma janela diária cujo encerramento é posterior à
abertura no relógio UTC normalizado. Calendários, feriados, sessões que
atravessam meia-noite e atribuição de `session_date` serão responsabilidade do
adaptador manifesto/calendário da DEV-008B; não serão inferidos pelo núcleo.

## Vocabulário temporal

- `known_at_open`: contexto anterior e gap conhecidos na abertura;
- `known_at_checkpoint`: estado calculado somente quando a barra do checkpoint
  já fechou;
- `known_at_event`: primeiro cruzamento conhecido no fechamento da barra que o
  tocou;
- `end_of_session_label`: desfecho posterior, sempre em coluna `label_*`.

Uma feature nunca usa a própria sessão futura na distribuição que a
classifica. ATR, percentis de range, volatilidade e volume relativo usam
somente sessões anteriores elegíveis.

## Saída pública

`to_hub_contract(result)` produz um envelope JSON estrito:

- `schema=ep_market_hub.atlas.result.v1`;
- versão do contrato/núcleo e política de relógio;
- manifesto efetivo, SHA-256 dos parâmetros e das barras;
- resumo auditável;
- tabelas `session_rows`, `checkpoint_rows`, `event_rows`,
  `outside_core_rows` e `quality_issue_rows`.

Cada linha recebe seu próprio schema versionado. Hashes ficam no envelope para
não se repetirem em todas as linhas.

## Compatibilidade com o Fusion Quant

`to_fusion_quant_v1(result)` adapta explicitamente as quatro tabelas causais
para os schemas estabilizados na Fusion Quant DEV-011D.1/D.2. A adaptação só
aceita as escadas padrão e exige igualdade exata do conjunto de campos; uma
divergência falha fechado.

A auditoria local da DEV-008A comparou campo a campo uma fixture de 20 sessões:
20 linhas de sessão, 100 checkpoints e 360 eventos foram idênticos ao motor do
Fusion Quant. Dados reais não entram na suíte nem no repositório.

## Limites da fase A

- não lê Parquet nem grava artefatos;
- não observa sessão aberta;
- não atualiza incrementalmente;
- não escolhe estratégia;
- não envia ordem;
- não altera worker, protocolo ou GUI.

Uma sessão truncada continua presente, marcada inelegível; seus `label_*`
descrevem apenas o trecho observado e não podem alimentar decisões. O snapshot
intradiário da DEV-008B.2 omitirá `label_*` enquanto a sessão estiver aberta.
