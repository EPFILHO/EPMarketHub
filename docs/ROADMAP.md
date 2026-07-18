# Roadmap sugerido

## Evoluções visuais futuras

- Numerar os MT5 conforme a ordem alfabética exibida, renumerando-os quando o cadastro ou a ordenação mudar.
- Reutilizar o número no badge da interface e, após validação técnica no Windows, no ícone da instância portátil.
- Adicionar splashscreen coerente com a identidade do EP Market Hub durante o carregamento do QWebEngineView.
- Implementar numeração e splash somente após aprovação de escopo e validação final do kernel 0.4.10.

## 0.4.10 — fechamento do kernel

- Formalizar fronteiras, invariantes, estados e falhas do kernel.
- Centralizar a política interna de capacidade e testá-la com valores 2, 3 e 4.
- Caracterizar criação, morte, filas e encerramento resistente de workers.
- Endurecer persistência atômica e recuperação de JSON inválido.
- Validar manualmente no Windows com MT5 reais antes da integração.

## 0.5 — camada de plataforma sem mudança funcional do kernel

- Apresentar para aprovação a divisão de `gui/main_window.py` e `web/app.js` por responsabilidade.
- Separar consumidores da interface dos contratos descritos em `docs/KERNEL.md`.
- Preservar timers, polling, filas e protocolo até que cada substituição tenha caracterização equivalente.
- Tratar Dashboard e Ativos como módulos substituíveis sobre o kernel estável.

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
