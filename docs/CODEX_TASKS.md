# Primeira tarefa sugerida para o Codex

## Objetivo

Refatorar a Base 0.4.8 sem alterar comportamento funcional, somente após aprovação explícita da mudança estrutural.

## Passos

1. Ler `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md` e `docs/CURRENT_STATUS.md`.
2. Executar testes e checagens disponíveis.
3. Mapear responsabilidades atuais de `gui/main_window.py` e `web/app.js`.
4. Propor uma divisão incremental em arquivos menores.
5. Implementar a menor primeira etapa de refatoração.
6. Garantir que os testes continuem passando.

## Restrições da primeira tarefa

- Não alterar fluxo de criação/edição/exclusão de terminais.
- Não alterar limite de 3 terminais ativos.
- Não alterar o protocolo dos workers sem teste correspondente.
- Não alterar a interface visual além do necessário para manter compatibilidade.
- Não adicionar novos módulos analíticos nesta primeira etapa.

## Entrega esperada

- Código refatorado em passos pequenos.
- Testes existentes passando.
- Documentação atualizada apenas quando necessário.
- Relatório curto explicando o que mudou e como validar manualmente.
