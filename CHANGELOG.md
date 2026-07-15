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
