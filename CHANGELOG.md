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
- Erros de edição ficam visíveis dentro do modal e em toast acima dos diálogos.
- Testes automatizados para as novas condições de falha sem abrir MT5 real.
