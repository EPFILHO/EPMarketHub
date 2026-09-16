# DEV-008B.1A — auditoria Codex 1

## Escopo auditado

Implementação offline entregue pelo Claude para materializar o Atlas causal
a partir de segmentos Parquet M1. O Claude encerrou ao atingir o limite da
sessão; a auditoria e as correções abaixo foram feitas localmente, sem nova
chamada, rede, MT5 ou dados de ticks.

## Correções aplicadas

- identificadores de fonte agora aceitam maiúsculas sem relaxar os slugs de
  identidade interna;
- `fsync` do manifesto de geração funciona no Windows (handle gravável);
- JSON lido e persistido é estrito, sem `NaN`/`Infinity`;
- `timeframe`, quando presente no Parquet, precisa ser `M1`;
- inserção de sessão histórica é correção de sufixo, não regressão;
- mudança apenas de proveniência é persistida uma vez e depois volta a
  `no_change`;
- ponteiro, geração e hashes dos objetos vigentes são validados antes do
  atalho `no_change`;
- objeto endereçado por hash nunca é aceito silenciosamente com bytes
  divergentes;
- sessão da data corrente não é materializada nesta fase;
- destino da CLI precisa coincidir com o destino declarado no manifesto.

## Compatibilidade verificada

Foi feita somente inspeção de schema, sem materialização real:

- histórico mensal janeiro/fevereiro: colunas causais exigidas presentes,
  `timestamp_utc` timezone-aware, `timeframe=M1`, `symbol=WIN$`, fonte
  `clear_research`;
- artefato de agosto: mesmas colunas mínimas, `timeframe=M1`, `symbol=WIN$`,
  fonte `clear`.

A transição de fonte permanece explícita no manifesto e auditada pelo
núcleo; os arquivos reais e seus caminhos não foram versionados.

## Resultado dos testes

- suíte dirigida incremental + causal: aprovada;
- suíte completa do repositório: aprovada;
- Ruff: aprovado;
- `compileall`: aprovado;
- `git diff --check`: aprovado (apenas aviso esperado de conversão LF/CRLF
  em documentação no Windows);
- não existe `package.json` neste repositório, portanto não há verificação
  Node aplicável.

## Ressalva arquitetural

A publicação é incremental: `no_change` não escreve e objetos do prefixo
não são regravados. Quando a entrada muda, porém, o núcleo causal ainda
recalcula a série M1 completa em memória. O resultado é deliberadamente
idêntico a um rebuild limpo, mas o item de economia de CPU do plano não foi
implementado. Recomenda-se medir duração e pico de memória na primeira
execução WIN$ janeiro–agosto antes de introduzir estado causal serializado,
que aumentaria bastante a complexidade e o risco de divergência matemática.

## Veredito

Tecnicamente apta para commit e para uma execução real posterior, em portão
separado, desde que a ressalva de CPU seja aceita como otimização orientada
por medição e não como bloqueio de correção.
