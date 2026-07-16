# Roadmap sugerido

## Evoluções visuais futuras

- Numerar os MT5 conforme a ordem alfabética exibida, renumerando-os quando o cadastro ou a ordenação mudar.
- Reutilizar o número no badge da interface e, após validação técnica no Windows, no ícone da instância portátil.
- Adicionar splashscreen coerente com a identidade do EP Market Hub durante o carregamento do QWebEngineView.
- Implementar numeração e splash somente após aprovação de escopo e conclusão da baseline 0.4.9.

## Próxima etapa — caracterização de estados antes da 0.5

- Cobrir transições terminal fechado/aberto e worker parado/iniciando/conectado/erro.
- Testar os payloads que determinam a disponibilidade das ações globais e individuais.
- Caracterizar falhas de abertura, fechamento, fila e encerramento antes de mover responsabilidades.
- Manter timers, polling e protocolo atuais durante essa etapa.

## 0.5 — Refatoração sem mudança funcional

- Dividir `gui/main_window.py` em componentes menores.
- Dividir `web/app.js` por responsabilidade.
- Fortalecer testes unitários.
- Registrar estados e erros de worker de forma mais clara.

## 0.6 — Mapeamento de símbolos por terminal

- Permitir escolher manualmente o símbolo real de cada ativo lógico em cada corretora.
- Salvar vínculos por terminal.
- Resolver contratos B3 sem depender de aliases globais.

## 0.7 — Busca de símbolos disponíveis

- Consultar `symbols_get()` no worker.
- Criar busca/pesquisa na interface.
- Mostrar se o símbolo está tradável, visível, com cotação e horário recente.

## 0.8 — Candles multi-timeframe

- Coletar M1, M5, M15, H1, H4 e D1 conforme configuração.
- Preparar dados para análise de tendência.

## 0.9 — Histórico local

- SQLite ou cache local para snapshots/candles.
- Deduplicação e retenção controlada.

## 1.0 — Primeiros painéis analíticos

- Força do dólar.
- Divergência entre índices.
- Risk-on/risk-off.
- Correlações móveis.
- Tendência multi-timeframe.
