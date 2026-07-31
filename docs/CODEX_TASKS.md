# Próxima tarefa sugerida para o Codex após a implementação local da 0.4.12

## Objetivo

Validar manualmente a fundação visual 0.4.12 no runtime real do Windows, preservando a baseline do kernel e publicando a versão somente após aprovação explícita.

## Passos

1. Executar todas as verificações automatizadas no clone e na instalação sincronizada.
2. Executar a seção **Fundação visual 0.4.12** de `docs/MANUAL_TESTS.md` nos temas claro e escuro.
3. Confirmar que o Dashboard apresenta somente contagens e diagnósticos reais.
4. Confirmar renumeração após cadastro, edição e exclusão de uma instância descartável.
5. Repetir os fluxos de Diagnóstico e os encerramentos responsivos com MT5 reais.
6. Registrar divergências visuais ou funcionais sem alterar o kernel como efeito colateral.
7. Somente após aprovação, autorizar push da branch, PR e avanço da `main`.

## Restrições

- Não alterar workers, protocolo, polling, temporizações, filas ou limite simultâneo.
- Não modificar `terminal64.exe`, sessões, registros ou números reais de conta.
- Não introduzir módulos analíticos, notícias, TradingView, IA ou envio de ordens nesta validação.
- Não ampliar a refatoração de `gui/main_window.py` ou dos fluxos QWebChannel antes da aprovação da 0.4.12.

## Entrega esperada

- Tema claro e escuro aprovados no QWebEngine real.
- Dashboard, navegação, Diagnóstico e numeração coerentes com o estado observado.
- Confirmação de que a evolução visual não regrediu conexão, leitura nem shutdown.
- Decisão explícita do proprietário antes de publicação.
