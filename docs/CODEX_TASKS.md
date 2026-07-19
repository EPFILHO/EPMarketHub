# Próxima tarefa sugerida para o Codex após a implementação local da 0.4.11

## Objetivo

Validar manualmente a GUI responsiva 0.4.11 com MT5 reais e, somente depois da aprovação, publicar a versão. Não iniciar a numeração nem os ícones da 0.4.12 nesta etapa.

## Passos

1. Ler `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md` e `docs/CURRENT_STATUS.md`.
2. Executar testes e checagens disponíveis.
3. Executar a seção **GUI responsiva 0.4.11** de `docs/MANUAL_TESTS.md` com a política de produção atual e MT5 reais.
4. Registrar congelamento, encerramento duplo, processo resistente, estado visual antigo ou divergência entre estado observado e `docs/KERNEL.md`.
5. Somente após aprovação, autorizar push da branch e integração fast-forward na `main`.
6. Abrir uma tarefa separada para a 0.4.12, caso a numeração e os ícones sejam aprovados.

## Restrições da próxima tarefa

- Não alterar fluxo de criação/edição/exclusão de terminais sem um defeito reproduzido.
- Não expor ao usuário configuração do limite simultâneo; qualquer novo valor de `MAX_ACTIVE_TERMINALS` é uma decisão de produto versionada e testada.
- Não alterar o protocolo dos workers sem teste correspondente.
- Não alterar a interface visual além do necessário para manter compatibilidade.
- Não adicionar novos módulos analíticos, numeração ou ícones nesta etapa.

## Entrega esperada

- Roteiro manual da 0.4.11 aprovado ou defeitos reproduzidos e registrados.
- Confirmação de que a janela permaneceu responsiva e que workers/MT5 terminaram realmente.
- Decisão explícita do proprietário antes de qualquer push, merge ou início da 0.4.12.
