# Status atual — 0.4.12 em desenvolvimento

A baseline `v0.4.11-baseline` preserva o kernel 0.4.10 validado e mantém fora da thread gráfica as esperas de encerramento. A 0.4.12 evolui somente a camada de apresentação: organização das telas, temas, navegação, ícones internos e numeração visual derivada. Não adiciona análises, candles, banco local, novos módulos de negócio nem altera o kernel.

## Interface 0.4.12 implementada localmente

- Tema claro como padrão e tema escuro opcional, com preferência local persistida e fallback seguro sem armazenamento.
- Dashboard orientado aos dados de mercado, consolidando somente Bid, Ask e spread efetivamente recebidos de fontes conectadas, com fonte e idade do pacote explícitas; fontes paradas deixam a visão de mercado sem apagar o cache diagnóstico.
- Cabeçalho global reduzido a conectadas, capacidade simultânea e estado da ponte; cadastros, processos e condições detalhadas permanecem nas telas operacionais, sem ocupar um card próprio no Dashboard.
- Gestão de instâncias concentrada em **Terminais MT5**, com os mesmos fluxos de criação, edição, abertura, leitura, fechamento e exclusão.
- Fluxos simultâneos e snapshot consolidados preservados em **Diagnóstico**, com os mesmos IDs e handlers da baseline.
- Numeração visual calculada pela ordem alfabética exibida, recalculada a cada payload e nunca persistida nem usada como identidade.
- Ícones somente na navegação da interface; nenhum executável foi modificado.
- Tema, navegação, ordenação e resumo extraídos para `web/ui_foundation.js`; regras puras dos cards extraídas para `web/terminal_presentation.js`.

## GUI responsiva 0.4.11

- Uma única thread Python dedicada executa o trabalho bloqueante por vez; solicitações de terminais distintos podem ficar enfileiradas sem bloquear globalmente a GUI.
- A bridge e os objetos Qt permanecem na thread principal; progresso e conclusão retornam por sinais enfileirados.
- No fechamento individual, o polling continua para os demais terminais e o alvo usa seu snapshot; somente o shutdown global suspende polling e serve snapshots integrais.
- Fechamentos individual, em lote, de leituras e pelo X mantêm a janela pintando e respondendo.
- Ações conflitantes são recusadas no backend e desabilitadas no frontend até a conclusão.
- IDs de operação impedem encerramento duplo e descartam sinais tardios.
- O estado ocupado é isolado por terminal: fechar uma conta não desabilita botões nem interrompe o polling das outras contas/corretoras.
- Falhas são isoladas por terminal e o shutdown global termina somente após confirmação ou falha explícita.
- Tempos de espera, `terminate()`, `kill()`, protocolo v1, polling e limite simultâneo não mudaram.
- A conexão IPC com o terminal e a sessão da corretora são tratadas separadamente: corretora desconectada mantém worker e IPC ativos, não escala para falha genérica e recupera automaticamente.
- Cada fechamento externo do MT5, enquanto a leitura estiver ativa, solicita nova reabertura minimizada; atividade genérica de reconexão não encerra essa transição nem cria falso diagnóstico de processo duplicado.
- Parar somente a leitura não altera o estado do processo MT5.

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
- A 0.4.11 passou pela suíte automatizada e pela validação manual final com MT5 reais; foi publicada e congelada como `v0.4.11-baseline`.
- Ciclo de cadastro, exclusão, relançamento simultâneo de três MT5 e fluxos foi validado manualmente em 18 de julho de 2026.
- Os fluxos iniciais de reconciliação de instância ausente foram validados manualmente em 18 de julho de 2026.

## Fora do kernel e limitações conhecidas

- Mapeamento de símbolo por terminal ainda não existe.
- Contratos B3 precisam ser atualizados manualmente na lista de aliases.
- A integração QWebChannel e os fluxos interativos ainda estão concentrados em `web/app.js`; fundações visuais e regras puras de apresentação já foram extraídas em pequenas fatias.
- A ponte PySide/QWebChannel ainda está concentrada em `gui/main_window.py`.
- Processos reais, QWebEngine e a biblioteca `MetaTrader5` não são exercitados pela suíte multiplataforma.
- Splashscreen e identificação externa das janelas MT5 permanecem como evoluções futuras.
- O Dashboard agora apresenta as cotações reais disponíveis; sem dados recebidos, apresenta estado vazio. A saúde operacional ficou secundária e a bancada visual de três fluxos foi preservada em **Diagnóstico**.
- DEV-002/DEV-003 implementaram e validaram a fundação de backfill histórico — escritor Parquet, catálogo SQLite v2, retomada, GUI local, descoberta conservadora e captura diária contratual. O C2 persistiu 30.854.930 ticks de agosto/2026; a captura contratual de 31/08 acrescentou 1.684.549 ticks sem issues. O C3 histórico está planejado somente para `WIN$`, desde a evidência observada de 14/07/2025; `WDO$` foi excluído por lacunas e a base de WDO passa a crescer prospectivamente por contrato individual. A automação B3 está configurada para 19h05. A execução ampla do C3 ainda exige aprovação final — ver `docs/work_orders/DEV-003.md` e `docs/FUTURES_DATA_POLICY.md`.
