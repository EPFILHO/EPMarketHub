# Auditoria técnica da baseline 0.4.7

Este documento registra a leitura técnica feita antes das alterações de hardening. A referência funcional é a versão 0.4.7, validada manualmente no Windows com três instâncias MetaTrader 5 conectadas simultaneamente e preservada pela tag `v0.4.7-baseline`.

O documento é uma fotografia técnica e não um roadmap imutável. Mudanças estruturais continuam sujeitas a aprovação antes da implementação.

## Mapa da arquitetura

```text
app.py
  ├─ configura logging e diretórios locais
  ├─ cria registros e gerenciadores
  └─ MainWindow
       ├─ QWebEngineView → web/index.html + web/app.js
       └─ QWebChannel → MarketHubBridge
                         ├─ TerminalRegistry → terminals.json
                         ├─ SymbolRegistry → symbols.json
                         ├─ TerminalManager → terminal64.exe /portable
                         └─ MT5WorkerManager
                              ├─ worker A → MetaTrader5 → instância A
                              ├─ worker B → MetaTrader5 → instância B
                              └─ worker C → MetaTrader5 → instância C
```

O processo principal mantém a interface, a persistência, a supervisão e as filas. Ele não alterna uma única conexão `MetaTrader5` entre contas. Cada conexão pertence a um processo Python persistente e independente.

## Fluxo completo

1. A interface chama um slot de `MarketHubBridge` por `QWebChannel`.
2. No cadastro, `TerminalManager` normaliza `CORRETORA-CONTA`, copia somente `MT5/terminal64.exe` para `user_data/mt5_instances/` e `TerminalRegistry` persiste metadados sem senha.
3. Na abertura, `TerminalManager.launch()` executa o `terminal64.exe` da instância com `/portable`.
4. O login é feito manualmente pelo usuário no próprio MT5; nenhuma senha é solicitada ou armazenada pelo aplicativo.
5. `MT5WorkerManager.start_worker()` cria um processo por terminal usando o contexto `spawn`.
6. O worker inicializa a biblioteca `MetaTrader5` com o executável específico, mantém a conexão e produz estados, heartbeats, snapshots e ticks.
7. Workers e processo principal trocam comandos e eventos por filas de `multiprocessing`.
8. `MarketHubBridge` converte o estado em JSON e emite sinais para o JavaScript.
9. `web/app.js` mantém o estado da tela e atualiza cards, badges, ações, snapshots e fluxos ao vivo.
10. Ao fechar o aplicativo, o timer de polling para, fluxos são limpos, workers encerram e os MT5 controlados recebem fechamento.

## Responsabilidades principais

| Arquivo ou classe | Responsabilidade |
|---|---|
| `app.py` | Entrada, logging, diretórios, registros, gerenciadores e ciclo Qt. |
| `core/paths.py` | Caminhos da instalação-modelo e dos dados locais. |
| `TerminalRegistry` / `core/json_store.py` | Leitura e escrita atômica dos cadastros JSON. |
| `TerminalManager` | Criar, lembrar, localizar, abrir, renomear e fechar instâncias MT5. |
| `MT5WorkerManager` | Limitar, criar, supervisionar e encerrar processos persistentes; rotear filas e estado. |
| `core/mt5_worker.py` | Loop proprietário de uma conexão `MetaTrader5`, snapshots e fluxos ao vivo. |
| `core/mt5_connector.py` | Inicialização da biblioteca, conexão, resolução e leitura de mercado. |
| `core/worker_protocol.py` | Estados e formato interno das mensagens de worker. |
| `SymbolRegistry` / `core/default_symbols.py` | Ativos lógicos, aliases e migrações dos padrões. |
| `MarketHubBridge` | Fachada QWebChannel e orquestração dos casos de uso. |
| `MainWindow` | Hospedagem do WebEngine/WebChannel e shutdown idempotente. |
| `web/index.html` / `web/style.css` | Estrutura e apresentação da interface. |
| `web/app.js` | Estado do frontend, eventos, renderização e chamadas à bridge. |

## Como as conexões simultâneas são mantidas

Cada worker nasce com o perfil completo de um único terminal e chama `MetaTrader5.initialize()` apontando para o `terminal64.exe` daquela instância. O processo permanece vivo e não troca de terminal. Assim, o estado global da biblioteca fica isolado por processo.

O supervisor impede worker duplicado para o mesmo terminal ou executável e limita a três workers ativos. O cadastro não tem esse limite: terminais adicionais permanecem armazenados e fechados. Essa separação é o núcleo da baseline validada.

## Pontos fortes

- Isolamento correto da biblioteca `MetaTrader5` por processo.
- Instâncias portáteis separadas por corretora e conta.
- Ausência deliberada de armazenamento de senha.
- Escrita JSON por substituição atômica.
- Limite simultâneo aplicado no supervisor e na orquestração.
- Fechamento idempotente e tentativa de alcançar processos abertos fora da execução atual.
- Resolução de aliases dentro de cada worker, respeitando diferenças entre corretoras.
- Protocolo simples de filas, adequado ao aplicativo desktop local.

## Dívida técnica e mitigação posterior

| Ponto observado na 0.4.7 | Situação posterior |
|---|---|
| Dados de runtime e executável podiam ser rastreados por engano. | Mitigado na 0.4.8 com `.gitignore` e exemplos seguros. |
| Cobertura automatizada pequena para limites, seleção, aliases e shutdown. | Ampliada na 0.4.8 com fakes multiplataforma. |
| Pasta podia ficar órfã se a criação funcionasse e a persistência falhasse. | Corrigido na 0.4.9 com rollback restrito à área controlada. |
| Caminhos absolutos mantinham referência à instalação anterior depois de mover o projeto. | Corrigido na 0.4.9 com persistência relativa e migração automática. |
| Edição ativa exigia fechar, renomear, relançar e restaurar o worker. | Eliminado na 0.4.9: edição só é aceita com MT5 fechado e worker parado. |
| Ações globais não refletiam sempre o estado dos workers. | Corrigido parcialmente na 0.4.9; faltam testes automatizados do DOM. |
| `MarketHubBridge` concentra persistência, processos, slots e payloads. | Pendente; divisão exige aprovação e caracterização prévia. |
| `web/app.js` concentra estado, renderização, eventos e comunicação. | Pendente; divisão exige aprovação e caracterização prévia. |
| Não há mapeamento de símbolo por terminal nem gestão automática de vencimentos B3. | Pendente no roadmap. |

## Riscos de regressão

- Iniciar dois workers para o mesmo terminal ou executável.
- Ultrapassar três MT5/workers por caminhos individuais, em lote ou pelo Dashboard.
- Fechar ou parar um terminal e afetar os demais.
- Renomear uma pasta enquanto o MT5 ainda usa seus arquivos.
- Salvar cadastro apontando para uma pasta inexistente ou deixar pasta sem cadastro.
- Exibir conexão com base apenas no processo vivo, sem confirmação real do worker.
- Alterar campos, sinais ou payloads usados pelo JavaScript durante uma refatoração.
- Mudar polling, filas ou temporizações e introduzir estados visuais obsoletos.
- Encerrar a janela antes de drenar e terminar workers e terminais.

## Divergências encontradas na auditoria

- A versão de pacote e documentos não era a única fonte de versão; textos visíveis e o título da janela continham `0.4.7` hardcoded.
- O roadmap sugeria refatoração como próximo passo, mas a cobertura ainda não caracterizava todas as transições que essa refatoração poderia afetar.
- A documentação descrevia o comportamento nominal, mas não explicitava compensações para falhas parciais de persistência e restauração.

## Testes automatizados ainda recomendados

- Matriz de transições terminal fechado/aberto versus worker parado/iniciando/conectado/erro.
- Contratos completos dos payloads emitidos por `MarketHubBridge` e consumidos pelo JavaScript.
- Estado das ações globais e individuais em testes de DOM JavaScript.
- Falhas de `launch`, fechamento, renomeação e filas cheias.
- Eventos atrasados ou fora de ordem sem alterar as temporizações atuais.
- Shutdown com worker que exige término forçado.
- Corrupção, permissão negada e recuperação dos arquivos JSON.
- Casos de resolução com metadados incompletos ou símbolos homônimos.

## Testes que dependem de Windows e MT5 real

- Cópia portátil criando a árvore completa e mantendo sessões isoladas.
- Login manual e reconexão com servidores de corretoras diferentes.
- Três processos Python com três conexões `MetaTrader5` simultâneas e PIDs distintos.
- Detecção e fechamento de `terminal64.exe` aberto pelo usuário.
- Bloqueio da edição com MT5 aberto e renomeação segura depois do fechamento.
- Resolução real de símbolos, permissões de negociação, ticks e horários de mercado.
- Encerramento pelo X, inclusive com workers conectando ou aguardando login.
- Comportamento do QWebEngine/WebChannel empacotado e em modo de renderização segura.

## Plano incremental recomendado

1. **0.4.8 — higiene e caracterização básica:** proteger dados locais, ampliar fakes e zerar Ruff. Concluído.
2. **0.4.9 — confiabilidade localizada:** alinhar ações globais, bloquear edição ativa e compensar falhas parciais. Concluído nesta branch.
3. **Caracterização de estados:** cobrir transições e contratos de payload/DOM sem mudar arquitetura.
4. **Primeira extração da bridge:** somente após aprovação, mover um caso de uso sem alterar slots nem payloads.
5. **Primeira extração do JavaScript:** somente após aprovação, separar estado/renderização preservando eventos e aparência.
6. **Símbolos por terminal:** projetar persistência e UI para aprovação antes da implementação.
7. **Candles, histórico e análises:** avançar em módulos independentes depois da estabilidade operacional.

Cada etapa deve permanecer pequena, verificável, reversível e validada no Windows com MT5 real quando tocar processos, sessão, WebEngine ou biblioteca `MetaTrader5`.
