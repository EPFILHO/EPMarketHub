# DEV-008A — auditoria da implementação

## Escopo entregue

- núcleo causal local em `market_analytics/causal_atlas.py`;
- contrato público versionado e adaptador Fusion Quant v1 em
  `market_analytics/atlas_contract.py`;
- documentação em `docs/ATLAS_CONTRACT.md`;
- fixtures inteiramente sintéticas em `tests/test_causal_atlas.py`;
- nenhuma alteração em GUI, Qt, worker, protocolo, MT5 ou dados reais.

O Claude Code preparou a primeira implementação em uma única sessão limitada.
O Codex auditou causalidade, schemas, tipos, generalidade do manifesto e
compatibilidade. Não houve segunda chamada ao Claude.

## Evidência

- 61 testes direcionados aprovados;
- comparação local independente contra o motor estabilizado do Fusion Quant:
  20 sessões, 100 checkpoints e 360 eventos idênticos campo a campo;
- Ruff, `compileall`, testes JavaScript, `node --check` e
  `git diff --check` aprovados;
- suíte Python completa: 956 testes coletados. Houve uma execução integral
  aprovada e, em outra execução, o teste preexistente
  `test_worker_restart_stops_when_instance_disappeared` oscilou; ele e o
  arquivo completo passaram imediatamente quando isolados. A falha ocorre em
  GUI/worker não tocados pela DEV-008A e foi classificada como interferência
  de ordem/stubs da suíte, não regressão do Atlas.

## Correções da auditoria

- validação estrita dos tipos numéricos e booleanos do manifesto;
- envelope `ep_market_hub.atlas.result.v1` com schemas por tabela, manifesto e
  hashes;
- suporte no contrato do Hub às escadas declarativas não padrão, mantendo o
  adaptador Fusion Quant deliberadamente restrito ao contrato congelado;
- correção da descrição de sessão truncada: o núcleo A é de sessão encerrada;
  snapshot intradiário sem `label_*` pertence à DEV-008B.2/C;
- documentação explícita de “local” versus “desconectado” e dos limites de
  calendário do núcleo v1.

## Veredito

DEV-008A pronta para decisão de commit/push. Nenhuma execução real é necessária
nesta fase: a paridade matemática já foi verificada localmente sem modificar
ou versionar os dados da DEV-011D.2.
