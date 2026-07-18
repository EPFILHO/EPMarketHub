# EP Market Hub — Kernel 0.4.10

Aplicativo desktop local para organizar instâncias controladas do MetaTrader 5 e ler dados de mercado por meio da biblioteca Python `MetaTrader5`.

Esta versão fecha o primeiro ciclo de endurecimento do kernel sem introduzir módulos de negócio novos. O ciclo de vida de processos, filas, persistência local e encerramento passa a falhar de forma explícita e recuperável. A parte principal permanece preservada: cada terminal MT5 ativo mantém uma conexão em seu próprio processo Python independente.

A baseline 0.4.9 foi validada manualmente no Windows em 17 de julho de 2026 com instâncias MT5 reais e conexões simultâneas. O hardening 0.4.10 possui validação automatizada; sua rodada manual final com MT5 real está descrita em `docs/MANUAL_TESTS.md`.

## Estado da base

Funciona hoje:

- Criação de instâncias MT5 isoladas em `user_data/mt5_instances/`.
- Uso de uma instalação-modelo local em `MT5/terminal64.exe`.
- Login feito manualmente pelo usuário no próprio MT5.
- Um worker/processo persistente por terminal conectado.
- Limite simultâneo definido somente pela política interna `MAX_ACTIVE_TERMINALS` (atualmente `3`); não é uma preferência do usuário e os cadastros continuam ilimitados.
- Seleção explícita dos terminais que serão abertos.
- Edição e exclusão de cadastros pela interface.
- Dashboard mostrando apenas terminais conectados.
- Teste ao vivo com até 3 fluxos simultâneos.
- Resolução de aliases de símbolos, priorizando símbolos tradáveis e com cotação válida.
- Fechamento dos workers e MT5 controlados ao encerrar o app.

Ainda não existe:

- Mapeamento manual de símbolo por terminal/corretora.
- Busca visual de símbolos disponíveis via `symbols_get()`.
- Gestão automática de vencimentos B3.
- Coleta de candles multi-timeframe.
- Banco SQLite/cache histórico.
- Módulos analíticos de correlação, força relativa ou leitura de cenário.

## Como rodar

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Modo de diagnóstico, caso o Chromium/QWebEngine apresente tela preta:

```bat
python app.py --safe-rendering
```

## Instalação-modelo do MT5

Coloque apenas o executável base em:

```text
MT5/terminal64.exe
```

Ao cadastrar um terminal, o Market Hub copia esse arquivo para uma pasta isolada em:

```text
user_data/mt5_instances/<CORRETORA>-<CONTA>/terminal64.exe
```

O MT5, ao ser aberto em modo portátil, cria os demais arquivos necessários dentro da própria instância.

## Dados locais

Os arquivos `user_data/terminals.json` e `user_data/symbols.json` são registros locais de runtime e não são versionados. Os modelos seguros `user_data/terminals.example.json` e `user_data/symbols.example.json` permanecem no repositório.

Os caminhos das instâncias são persistidos relativamente à pasta de instalação. Ao mover a pasta completa do EP Market Hub, os registros são resolvidos novamente contra o novo local; caminhos absolutos de versões anteriores são migrados na próxima inicialização.

Instâncias reais, logs, sessões do MT5, executáveis, credenciais e dados pessoais são protegidos pelo `.gitignore` e não devem ser adicionados manualmente ao Git.

## Estrutura

```text
app.py                  Entrada do app.
core/                   Regras de negócio, MT5, workers e persistência.
gui/                    Janela PySide6 e ponte Python ↔ JavaScript.
web/                    Interface HTML/CSS/JS carregada no QWebEngineView.
docs/                   Documentação atual para manutenção e Codex.
tests/                  Caracterização automatizada do kernel e da interface de estado.
MT5/                    Pasta da instalação-modelo; recebe terminal64.exe local.
user_data/              Dados locais e instâncias isoladas.
```

## Documentação principal

- `AGENTS.md`: regras para agentes/Codex trabalharem neste repositório.
- `docs/ARCHITECTURE.md`: arquitetura atual.
- `docs/KERNEL.md`: fronteiras, invariantes e modelo de falhas do kernel.
- `docs/BASELINE_AUDIT_0.4.7.md`: auditoria técnica da baseline validada.
- `docs/CURRENT_STATUS.md`: o que funciona e o que ainda falta.
- `docs/MANUAL_TESTS.md`: roteiro atual de testes manuais.
- `docs/ROADMAP.md`: próximos módulos recomendados.
- `docs/CODEX_TASKS.md`: primeira tarefa sugerida para o Codex.
