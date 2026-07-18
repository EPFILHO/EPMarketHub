# Próxima tarefa sugerida para o Codex

## Objetivo

Caracterizar as transições de estado da Base 0.4.9 antes de qualquer refatoração estrutural.

## Passos

1. Ler `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md` e `docs/CURRENT_STATUS.md`.
2. Executar testes e checagens disponíveis.
3. Enumerar estados de terminal, worker e conexão que controlam cada ação da interface.
4. Adicionar testes dos payloads e das transições de falha usando fakes, sem abrir MT5.
5. Propor a menor divisão futura de `gui/main_window.py` e `web/app.js`, sem implementá-la.
6. Executar o roteiro manual com três MT5 reais no Windows.

## Restrições da próxima tarefa

- Não alterar fluxo de criação/edição/exclusão de terminais sem um defeito reproduzido.
- Não alterar limite de 3 terminais ativos.
- Não alterar o protocolo dos workers sem teste correspondente.
- Não alterar a interface visual além do necessário para manter compatibilidade.
- Não adicionar novos módulos analíticos nesta primeira etapa.

## Entrega esperada

- Testes de caracterização adicionais passando.
- Matriz curta de estados, ações e resultados esperados.
- Proposta de refatoração submetida à aprovação antes de mudanças estruturais.
