# Próxima tarefa sugerida para o Codex — 0.4.12

## Objetivo

Transformar a bancada técnica da interface em uma fundação visual organizada, com temas claro e escuro, Dashboard de saúde, área de Diagnóstico e numeração visual dos MT5, preservando integralmente a baseline `v0.4.11-baseline`.

## Passos

1. Caracterizar os contratos DOM e as regras JavaScript antes de mover elementos.
2. Extrair tema e navegação em módulos pequenos que não dependam do QWebChannel.
3. Converter cores fixas em tokens semânticos para os dois temas.
4. Separar Dashboard, Terminais MT5, Ativos e Diagnóstico sem alterar slots da bridge.
5. Numerar os terminais pela ordem alfabética já exibida, sem persistir o número.
6. Cobrir tema, navegação, numeração e resumo de saúde com testes JavaScript.
7. Executar toda a regressão automatizada e sincronizar somente pelo script protegido.
8. Aguardar validação manual com MT5 reais antes de publicar ou avançar a `main`.

## Restrições

- Não alterar workers, protocolo, polling, temporizações, filas ou limite simultâneo.
- Não mudar fluxos de criação, edição, exclusão, abertura ou shutdown sem defeito reproduzido.
- Não introduzir dados simulados no Dashboard.
- Não implementar notícias, TradingView, análises ou envio de ordens nesta versão.
- Não modificar executáveis nem persistir a numeração visual.
- Não misturar uma refatoração ampla de `gui/main_window.py` ou `web/app.js` com a evolução visual.

## Entrega esperada

- Tema claro e escuro legíveis em todas as telas e modais.
- Dashboard baseado apenas em estados reais.
- Ferramentas técnicas preservadas em Diagnóstico.
- Terminais renumerados corretamente após cadastro, edição e exclusão.
- Regressão automatizada aprovada e roteiro manual específico da 0.4.12 documentado.
