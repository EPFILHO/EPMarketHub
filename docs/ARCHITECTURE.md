# Arquitetura atual

## Visão geral

```text
app.py
  └─ MainWindow / MarketHubBridge
       ├─ SerializedLifecycleExecutor (thread Python serial para encerramento)
       ├─ core/config.py (política interna de capacidade)
       ├─ core/terminal_states.py (vocabulário e classificação de estados)
       ├─ TerminalRegistry
       ├─ TerminalManager
       ├─ SymbolRegistry
       ├─ WorkerManager
       └─ QWebChannel → web/app.js
                          ├─ ui_foundation.js
                          └─ terminal_presentation.js

Worker MT5 #1 → terminal64.exe da instância A
Worker MT5 #2 → terminal64.exe da instância B
Worker MT5 #3 → terminal64.exe da instância C
```

O processo principal cuida da interface, registros, abertura/fechamento dos terminais e supervisão dos workers. Ele não deve manter múltiplas conexões diretas com MT5.

`QWebEngineView`, `QWebChannel`, `MainWindow`, a bridge e todas as emissões que alteram a tela permanecem na thread principal do Qt. Somente o trabalho bloqueante de encerramento usa uma thread Python dedicada. Os managers protegem suas estruturas compartilhadas com locks curtos, nunca mantidos durante `join()`, `terminate()` ou `kill()`. No fechamento individual, o polling continua para os demais terminais e o alvo usa seu snapshot de transição até a conclusão. Somente o shutdown global suspende polling e serve snapshots integrais. Progresso e conclusão voltam à thread Qt por sinais enfileirados e identificados por operação.

Cada worker é um processo separado, inicializa a biblioteca `MetaTrader5` apontando para uma instância específica e mantém aquela conexão viva enquanto a leitura estiver ativa.

As fronteiras, invariantes, estados e regras de falha dessa camada estão definidos em `docs/KERNEL.md`. A interface consulta o limite em runtime pela bridge, mas o valor de produção nasce somente de `MAX_ACTIVE_TERMINALS`; não existe preferência do usuário para alterá-lo.

## Instâncias MT5

A pasta `MT5/` contém o executável-modelo:

```text
MT5/terminal64.exe
```

Cada cadastro cria uma cópia isolada em:

```text
user_data/mt5_instances/<CORRETORA>-<CONTA>/terminal64.exe
```

`instance_slug` identifica a pasta. `instance_dir` e `terminal_exe` são gravados no JSON como caminhos relativos à instalação e convertidos para caminhos absolutos apenas em memória. Dessa forma, mover a pasta completa do aplicativo não mantém referências à instalação anterior.

A identidade de negócio é `corretora + conta informada`, mas cada terminal também possui um `id` interno estável.

Os registros `user_data/terminals.json` e `user_data/symbols.json`, as instâncias, os logs e a instalação-modelo local são dados de runtime ignorados pelo Git. Somente os arquivos `*.example.json` sem dados reais e o arquivo de instrução da pasta `MT5/` são versionados.

## Comunicação

Workers e processo principal trocam mensagens por filas do `multiprocessing`.
Os envelopes usam a versão definida em `core/worker_protocol.py`; o contrato v1
está documentado em `docs/KERNEL_PROTOCOL.md`.

Tipos importantes de mensagens:

- estado do worker;
- snapshot consolidado;
- resposta de fluxo ao vivo;
- erro;
- heartbeat.

Eventos carregam a identidade do processo. O supervisor descarta mensagens residuais de um PID anterior e eventos tardios de um terminal já marcado para parada, usa entrega não bloqueante para eventos volumosos e uma espera curta e limitada para eventos críticos. A parada começa graciosa e escala para `terminate()` e `kill()` quando necessário; um processo resistente permanece visível como erro. Na 0.4.11 essas mesmas esperas são executadas pelo executor serial de ciclo de vida, sem alterar protocolo ou temporizações.

O frontend recebe o estado por métodos expostos no QWebChannel. `web/app.js` continua responsável pela integração com a bridge e pelos fluxos interativos; tema, navegação, ordenação, numeração, resumo de saúde e consolidação pura de cotações vivem em `ui_foundation.js`, enquanto rótulos, badges e regras puras de ações dos cards vivem em `terminal_presentation.js`. Esses módulos não acessam Qt, processos, filas nem a biblioteca MT5.

O Dashboard apresenta primeiro os dados de mercado já existentes nos caches de snapshots e fluxos ao vivo, filtrados pelos terminais cujo worker está atualmente `connected`. A consolidação usa a chave `terminal + ativo lógico`, mantém o pacote mais recente de cada fonte conectada e expõe sua idade sem fabricar cotações. O cache de uma fonte parada continua disponível para o Diagnóstico, mas deixa imediatamente as métricas e a tabela de mercado. O cabeçalho global informa conectadas e capacidade simultânea com denominadores explícitos; cadastros e estados detalhados ficam em **Terminais MT5** e **Diagnóstico**. Os três fluxos simultâneos e o snapshot consolidado continuam disponíveis na área **Diagnóstico**, sem alteração de handlers ou protocolo.

O estado de um cadastro não é reduzido a um único rótulo. `terminal_states.py`
mantém separadas a integridade da instância, a existência/transição do processo
`terminal64.exe` e a conexão do worker. Assim, por exemplo, **MT5 aberto / Falha
de autenticação** é diferente de **Reabrindo MT5 / Reconectando**.

## Persistência e recuperação

Os registros são gravados em arquivo temporário, sincronizados e promovidos por substituição atômica. Se a promoção falhar, o último JSON válido é preservado. Conteúdo vazio, inválido ou com codificação danificada é renomeado para `*.corrupt-<identificador>` antes de o registro iniciar com o padrão seguro; falhas de acesso não são tratadas como cadastro vazio.

## Símbolos

`SymbolRegistry` mantém ativos lógicos com aliases. A resolução do símbolo ocorre dentro de cada worker usando metadados do MT5. O critério operacional (snapshot, streaming) prioriza símbolos tradáveis e com cotação válida e recusa candidatos não negociáveis. O diagnóstico de ticks usa uma resolução histórica/de dados separada, que aceita um alias exato listado mesmo sem negociação habilitada (ex.: `WIN$` na Clear); aceitar essa fonte para histórico não a torna negociável nem altera a resolução operacional.

## Backfill histórico (DEV-002, Portão A)

Além do snapshot/streaming/diagnóstico, o worker aceita um comando pequeno de backfill (`start_backfill`/`stop_backfill`) que grava ticks brutos de uma sessão (dia civil) em Parquet fora do repositório e fora de `D:\EP\EPMarketHub`, com um catálogo SQLite de estado/retomada — ver `docs/MARKET_ANALYTICS.md` e `docs/work_orders/DEV-002.md`. Ticks brutos nunca atravessam a fila de eventos; o worker escreve por chunk direto no Parquet e só emite resumos pequenos. Backfill, fluxo ao vivo e diagnóstico de ticks são mutuamente exclusivos no mesmo worker, em qualquer ordem. O Portão A implementa e testa essa fundação com fontes falsas, sem MT5 real, coleta ampla nem GUI; portões seguintes exigem nova aprovação do proprietário.
