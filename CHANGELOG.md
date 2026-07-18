# Changelog

## 0.1.0

- Primeira base PySide6 + QWebEngineView.
- Cadastro de terminal e conexão inicial via MetaTrader5.

## 0.2.0

- Um processo persistente por terminal.
- Conexões simultâneas com múltiplos MT5.
- Supervisor de workers e reconexão.

## 0.3.0

- Teste visual de fluxos simultâneos.
- Três MT5 exibindo ativos diferentes em tempo real.

## 0.3.1

- Seletor personalizado.
- Estabilização dos cards.
- Primeiro suporte ao contrato futuro B3.

## 0.4.0

- Edição de corretora, conta e apelido.
- Renomeação automática das pastas.
- Fechamento dos MT5 ao encerrar o app.

## 0.4.1

- Instalação-modelo reduzida ao terminal64.exe.

## 0.4.2

- Exclusão de instâncias.
- Nomes de diretórios em caixa alta.
- Limite de três MT5 ativos.

## 0.4.3

- Seleção manual dos terminais.
- Ordem alfabética.
- Melhorias de estabilidade visual.

## 0.4.4

- Dashboard mostra somente conexões ativas.
- Encerramento correto dos testes ao vivo.

## 0.4.5

- Padronização dos comandos de abertura e leitura.
- Inclusão dos testes automatizados e ferramentas de desenvolvimento.

## 0.4.6

- Resolução de símbolos ativos/tradáveis.
- Aliases US30Cash, US100Cash e US500Cash.
- Botões liberados após conexão real.

## 0.4.7

- Documentação limpa para handoff.
- Exclusão bloqueada enquanto o MT5 estiver aberto.

## 0.4.8

- Hardening da baseline 0.4.7, sem novas funcionalidades.
- Proteção de executáveis, instâncias, sessões, logs e registros locais contra versionamento.
- Ampliação dos testes de caracterização para registros, workers, bridge e símbolos usando fakes.
- Correções seguras de estilo e tipagem para conformidade com Ruff.

## 0.4.9

- Continuação do hardening da baseline, sem funcionalidades novas e sem mudança estrutural.
- Botão **Parar leituras** habilitado somente quando existe worker ativo.
- Feedback temporário no botão **Atualizar** durante a sincronização manual do estado.
- Remoção compensatória da pasta recém-criada quando o cadastro não pode ser persistido.
- Edição bloqueada enquanto o MT5 estiver aberto ou seu worker permanecer ativo.
- Rollback da renomeação e do cadastro se uma edição de terminal fechado falhar.
- Caminhos de instâncias persistidos relativamente à instalação atual, com migração dos registros absolutos legados.
- Banner do teste simultâneo identifica nominalmente cada fluxo sem leitura recente.
- Botão individual de fluxo é desativado quando a configuração já está aplicada e muda para **Alterar** após nova seleção.
- Banner diferencia leitura atrasada de fluxo parado e mantém altura mínima para duas linhas.
- Fluxo parado preserva o terminal configurado, sem substituição automática nem cotação antiga na tela.
- Botão **Abrir selecionados** respeita as vagas simultâneas disponíveis para MT5 e workers.
- Remoção do botão redundante **Snapshot** dos cards de terminais; snapshots automáticos e consolidados permanecem disponíveis.
- Erros de edição ficam visíveis dentro do modal e em toast acima dos diálogos.
- Testes automatizados para as novas condições de falha sem abrir MT5 real.
- Baseline 0.4.9 validada manualmente no Windows com MT5 reais e conexões simultâneas em 17 de julho de 2026.

## 0.4.10

- Fechamento do primeiro ciclo de endurecimento do kernel, sem novos módulos de negócio.
- Limite simultâneo centralizado em `MAX_ACTIVE_TERMINALS`, como política interna do produto e sem configuração pelo usuário.
- Caracterização automatizada da capacidade com valores 2, 3 e 4, mantendo o valor de produção atual em 3.
- Encerramento de workers com confirmação real, escalonamento gracioso para `terminate()` e `kill()` e falha explícita quando o processo resiste.
- Filas fechadas, cheias ou congestionadas tratadas sem declarar sucesso falso nem travar o shutdown.
- Eventos residuais de PIDs anteriores descartados antes de alcançar a bridge.
- Falhas de escrita JSON preservam o último arquivo válido; conteúdo vazio ou inválido é movido para quarentena recuperável.
- Abertura, edição, exclusão e ações em lote passam a considerar tanto o processo MT5 quanto o worker ativo.
- Fronteiras, invariantes, estados e modelo de falhas registrados em `docs/KERNEL.md`.
- Protocolo interno v1 formaliza comandos, eventos, versão e propriedade da conexão isolada.
- MT5 fechado diretamente é relançado pelo kernel em modo portátil e minimizado, sem duplicar a instância.
- Estado transitório **Reabrindo MT5** diferencia processo relançado de conexão já restabelecida.
- Instâncias removidas externamente podem ser recriadas a partir da base ou ter somente o cadastro local removido.
- Exclusão já confirmada remove diretamente o cadastro de uma instância ausente, sem repetir a decisão no fluxo de resolução.
- Pastas órfãs recuperadas fora do aplicativo podem ser adotadas explicitamente, preservando seus arquivos e sem sobrescrita automática.
- Pasta ausente interrompe o worker correspondente para evitar tentativas e logs repetidos.
- Falhas IPC são diferenciadas da ausência de login; o processo permanece **MT5 aberto** enquanto o worker apresenta **Reconectando** até confirmar eventual reabertura.
- Máquina de estados centralizada separa integridade da instância, ciclo do processo MT5 e conexão do worker.
- Abertura, fechamento, reabertura e suas falhas possuem estados transitórios explícitos, com pós-condição real do processo.
- Autenticação recusada, conta conectada divergente, corretora offline, terminal divergente e configuração inválida deixam de aparecer como reconexão genérica.
- Supervisor sinaliza worker sem resposta, queda inesperada, falha de criação e resistência ao encerramento sem declarar sucesso falso.
- Mais de um processo do mesmo executável é exposto como anomalia e tentativas transitórias prolongadas passam a exigir atenção.
- Processos duplicados bloqueiam nova leitura até o fechamento, e um worker resistente mantém seu MT5 aberto para impedir reabertura automática contraditória.
- Eventos tardios do worker não apagam falhas de abertura ou fechamento já confirmadas.
- Todos os consumidores da fila passam pelo mesmo despachante, impedindo uma atualização da tela de descartar eventos de relançamento.
- Fechamento dos terminais selecionados atualiza cada card conforme sua operação termina.
- Estado visual de worker parado passa a ser apresentado como **Desconectado**.
- Sincronizador seguro copia somente arquivos de desenvolvimento e protege `.git`, `MT5` e todo o `user_data` da instalação de teste.
- Validação automatizada concluída; validação manual final da 0.4.10 com MT5 real permanece pendente.
