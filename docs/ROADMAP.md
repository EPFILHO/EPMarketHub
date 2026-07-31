# Roadmap sugerido

## Evoluções visuais futuras

- A numeração visual pela ordem alfabética e sua reutilização dentro da interface entram na 0.4.12.
- Após validação técnica no Windows, avaliar identificação externa da instância sem modificar `terminal64.exe`.
- Adicionar splashscreen coerente com a identidade do EP Market Hub durante o carregamento do QWebEngineView.
- Implementar splash somente após a validação visual da 0.4.12.

## 0.4.11 — GUI responsiva

- Executar encerramento bloqueante fora da thread gráfica, preservando afinidade dos objetos Qt.
- Serializar operações conflitantes e retornar progresso/conclusão por sinais enfileirados.
- Preservar protocolo v1, polling, temporizações, limites e confirmação real de processos.
- Validar manualmente no Windows com MT5 reais antes de publicar ou avançar a `main`.

## 0.4.12 — fundação visual e organização

- Preservar integralmente o kernel e o protocolo congelados na `v0.4.11-baseline`.
- Oferecer tema claro como padrão e tema escuro opcional, sem depender da bridge.
- Separar Dashboard de saúde, gestão de Terminais MT5, Ativos e Diagnóstico.
- Manter os três fluxos simultâneos e o snapshot como ferramentas de diagnóstico.
- Tratar a numeração como apresentação derivada da ordem alfabética, nunca como ID persistido.
- Adicionar ícones internos e avaliar identificação externa sem modificar executáveis, workers, protocolo ou kernel.
- Extrair apenas módulos frontend pequenos, independentes e caracterizados; a divisão estrutural da bridge e dos consumidores continua reservada para a 0.5.

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
