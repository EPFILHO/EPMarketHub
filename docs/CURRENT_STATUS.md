# Status atual — 0.4.11

A 0.4.11 preserva o kernel 0.4.10 validado e remove da thread gráfica as esperas de encerramento. Não adiciona análises, candles, banco local, novos módulos de negócio, numeração ou ícones.

## GUI responsiva 0.4.11

- Uma única thread Python dedicada executa o trabalho bloqueante por vez; solicitações de terminais distintos podem ficar enfileiradas sem bloquear globalmente a GUI.
- A bridge e os objetos Qt permanecem na thread principal; progresso e conclusão retornam por sinais enfileirados.
- Polling é suspenso logicamente durante a operação e getters usam snapshots cacheados, sem leitura concorrente dos managers.
- Fechamentos individual, em lote, de leituras e pelo X mantêm a janela pintando e respondendo.
- Ações conflitantes são recusadas no backend e desabilitadas no frontend até a conclusão.
- IDs de operação impedem encerramento duplo e descartam sinais tardios.
- O estado ocupado é isolado por terminal: fechar uma conta não desabilita botões nem interrompe o polling das outras contas/corretoras.
- Falhas são isoladas por terminal e o shutdown global termina somente após confirmação ou falha explícita.
- Tempos de espera, `terminate()`, `kill()`, protocolo v1, polling e limite simultâneo não mudaram.

## Kernel preservado

- Criação de instâncias controladas a partir de `MT5/terminal64.exe`.
- Cadastro único por corretora e conta, sem senha, com quantidade ilimitada de registros.
- Caminhos persistidos relativamente à instalação e instâncias em `user_data/mt5_instances/`.
- Abertura e fechamento individual ou em lote.
- Um processo Python persistente e uma conexão `MetaTrader5` independente por terminal ativo.
- Política interna `MAX_ACTIVE_TERMINALS`, atualmente 3, centralizada no código e não exposta ao usuário.
- Seleção explícita dos terminais ativados e isolamento entre seus ciclos de vida.
- Shutdown idempotente de workers e MT5 controlados.

## Hardening 0.4.10

- Capacidade caracterizada automaticamente com políticas de 2, 3 e 4 terminais.
- Criação de processo, fila cheia/fechada, morte inesperada e encerramento resistente produzem estados explícitos.
- Parada usa sinalização graciosa, depois `terminate()` e `kill()`, sem informar sucesso enquanto o processo continua vivo.
- Eventos residuais de uma execução anterior são rejeitados pelo PID.
- Ações individuais e em lote consideram simultaneamente o estado do MT5 e do worker.
- Escrita JSON é atômica, sincronizada em disco e preserva o arquivo anterior se a promoção falhar.
- JSON vazio, inválido ou com codificação danificada é preservado em quarentena antes da recuperação padrão.
- Fronteiras, invariantes e modelo de falhas estão formalizados em `docs/KERNEL.md`.
- Comandos e eventos usam o protocolo v1 descrito em `docs/KERNEL_PROTOCOL.md`; mensagens incompatíveis são descartadas.
- Fechamento externo do MT5 solicita reabertura controlada, portátil e minimizada ao processo principal.
- A transição é apresentada como **Reabrindo MT5** até conectar ou aguardar login.
- Pasta ou executável removido externamente produz diagnóstico explícito e permite recriar a instância ou remover somente o cadastro.
- Exclusão confirmada de cadastro sem pasta conclui a remoção diretamente; a tela de edição nunca remove cadastro e encaminha a inconsistência ao fluxo **Resolver**.
- Uma pasta existente sem cadastro é tratada como órfã e só pode ser adotada por ação explícita, sem apagar a sessão local nem sobrescrever seus arquivos.
- Perda de comunicação IPC é distinta de ausência de login e atualiza os badges de processo e worker durante a transição.
- Estados são definidos em `core/terminal_states.py` e apresentados em eixos independentes de instância, processo e worker/conexão.
- Conta autenticada diferente da cadastrada e conexão a outro diretório MT5 são rejeitadas antes de fornecer dados ao restante do sistema.
- Falhas de autenticação, corretora offline, configuração permanente, worker sem resposta, processo duplicado e fechamento resistente possuem diagnóstico próprio.
- Processo duplicado bloqueia nova leitura; worker resistente impede fechar seu MT5 e evita relançamento contraditório após uma parada incompleta.
- Eventos tardios do worker não apagam falhas confirmadas de abertura ou fechamento do processo.
- Fechamento em lote é apresentado progressivamente e worker parado aparece como **Desconectado**.
- A instalação de teste é atualizada apenas por `scripts/sync_test_copy.ps1`, sem operações Git nem cópia de dados locais.

## Validação

- A 0.4.9 foi validada manualmente no Windows com MT5 reais em 17 de julho de 2026.
- A 0.4.10 foi validada manualmente no Windows com MT5 reais.
- A 0.4.11 passa pela suíte automatizada multiplataforma e pelos testes de regras JavaScript; a validação manual final com MT5 reais permanece pendente.
- Ciclo de cadastro, exclusão, relançamento simultâneo de três MT5 e fluxos foi validado manualmente em 18 de julho de 2026.
- Os fluxos iniciais de reconciliação de instância ausente foram validados manualmente em 18 de julho de 2026.

## Fora do kernel e limitações conhecidas

- Mapeamento de símbolo por terminal ainda não existe.
- Contratos B3 precisam ser atualizados manualmente na lista de aliases.
- A interface web ainda está concentrada em `web/app.js`.
- A ponte PySide/QWebChannel ainda está concentrada em `gui/main_window.py`.
- Processos reais, QWebEngine e a biblioteca `MetaTrader5` não são exercitados pela suíte multiplataforma.
- Numeração alfabética dos MT5 e splashscreen permanecem como evoluções futuras.
- O Dashboard atual é uma bancada visual de três fluxos e poderá mudar quando a camada de plataforma começar a evoluir.
