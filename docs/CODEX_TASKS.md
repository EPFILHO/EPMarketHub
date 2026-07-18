# Próxima tarefa sugerida para o Codex após a 0.4.10

## Objetivo

Validar manualmente o fechamento do kernel 0.4.10 e, somente depois da aprovação, propor a arquitetura da camada de plataforma 0.5 sem implementá-la.

## Passos

1. Ler `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md` e `docs/CURRENT_STATUS.md`.
2. Executar testes e checagens disponíveis.
3. Executar o roteiro de `docs/MANUAL_TESTS.md` com a política de produção atual e MT5 reais.
4. Registrar qualquer divergência entre estado observado, `docs/KERNEL.md` e a interface.
5. Se o kernel for aprovado, apresentar a menor divisão futura de `gui/main_window.py` e `web/app.js`, sem implementá-la.
6. Definir os primeiros contratos entre kernel, plataforma e futuros aplicativos.

## Restrições da próxima tarefa

- Não alterar fluxo de criação/edição/exclusão de terminais sem um defeito reproduzido.
- Não expor ao usuário configuração do limite simultâneo; qualquer novo valor de `MAX_ACTIVE_TERMINALS` é uma decisão de produto versionada e testada.
- Não alterar o protocolo dos workers sem teste correspondente.
- Não alterar a interface visual além do necessário para manter compatibilidade.
- Não adicionar novos módulos analíticos nesta primeira etapa.

## Entrega esperada

- Roteiro manual do kernel 0.4.10 aprovado ou defeitos reproduzidos e registrados.
- Proposta curta de arquitetura da plataforma 0.5.
- Proposta de refatoração submetida à aprovação antes de mudanças estruturais.
