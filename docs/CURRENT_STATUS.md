# Status atual — Kernel 0.4.10

A 0.4.10 fecha o primeiro ciclo de hardening do kernel construído sobre a baseline funcional 0.4.7 e a interface validada da 0.4.9. Não adiciona análises, candles, banco local nem novos módulos de negócio.

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
- Fechamento em lote é apresentado progressivamente e worker parado aparece como **Desconectado**.
- A instalação de teste é atualizada apenas por `scripts/sync_test_copy.ps1`, sem operações Git nem cópia de dados locais.

## Validação

- A 0.4.9 foi validada manualmente no Windows com MT5 reais em 17 de julho de 2026.
- A 0.4.10 passa pela suíte automatizada multiplataforma e pelos testes de regras JavaScript.
- Ciclo de cadastro, exclusão, relançamento simultâneo de três MT5 e fluxos foi validado manualmente em 18 de julho de 2026.
- A reconciliação visual de instância ausente permanece pendente de validação manual antes de integrar a versão.

## Fora do kernel e limitações conhecidas

- Mapeamento de símbolo por terminal ainda não existe.
- Contratos B3 precisam ser atualizados manualmente na lista de aliases.
- A interface web ainda está concentrada em `web/app.js`.
- A ponte PySide/QWebChannel ainda está concentrada em `gui/main_window.py`.
- Processos reais, QWebEngine e a biblioteca `MetaTrader5` não são exercitados pela suíte multiplataforma.
- Numeração alfabética dos MT5 e splashscreen permanecem como evoluções futuras.
- O Dashboard atual é uma bancada visual de três fluxos e poderá mudar quando a camada de plataforma começar a evoluir.
