# EP Market Hub — 0.4.12

Aplicativo desktop local para organizar instâncias controladas do MetaTrader 5 e ler dados de mercado por meio da biblioteca Python `MetaTrader5`.

Esta versão está em desenvolvimento sobre a baseline funcional `v0.4.11-baseline`. O kernel, o protocolo worker/bridge, o polling, as temporizações e o limite simultâneo permanecem congelados; a 0.4.12 organiza a interface, introduz temas claro e escuro, coloca os dados de mercado no primeiro plano, separa a saúde das fontes das ferramentas de diagnóstico e adiciona numeração visual derivada aos terminais.

A baseline 0.4.9, o kernel 0.4.10 e a GUI responsiva 0.4.11 foram validados manualmente no Windows com instâncias MT5 reais e conexões simultâneas. A 0.4.11 foi publicada e congelada no commit `2fc4bec`.

## Estado da base

Funciona hoje:

- Criação de instâncias MT5 isoladas em `user_data/mt5_instances/`.
- Uso de uma instalação-modelo local em `MT5/terminal64.exe`.
- Login feito manualmente pelo usuário no próprio MT5.
- Um worker/processo persistente por terminal conectado.
- Limite simultâneo definido somente pela política interna `MAX_ACTIVE_TERMINALS` (atualmente `3`); não é uma preferência do usuário e os cadastros continuam ilimitados.
- Seleção explícita dos terminais que serão abertos.
- Edição e exclusão de cadastros pela interface.
- Dashboard orientado a mercado, consolidando as cotações reais das fontes atualmente conectadas; caches de fontes paradas permanecem apenas no Diagnóstico e a saúde da coleta fica em posição secundária.
- Temas claro e escuro, com preferência local persistida e tema claro como padrão.
- Navegação separada entre Dashboard, Terminais MT5, Ativos e Diagnóstico.
- Numeração visual dos terminais recalculada pela ordem alfabética, sem persistência.
- Teste ao vivo com até 3 fluxos simultâneos.
- Resolução de aliases de símbolos, priorizando símbolos tradáveis e com cotação válida.
- Fechamento dos workers e MT5 controlados ao encerrar o app.
- Máquina de estados do kernel separa integridade local, processo MT5 e conexão do worker, incluindo falhas de autenticação, identidade e encerramento.

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

## Instalação de teste

O desenvolvimento ocorre no clone de trabalho. `D:\EP\EPMarketHub` é somente
a instalação com MT5 e dados reais para validação manual. Não execute operações
Git nessa pasta. O script `scripts/sync_test_copy.ps1` lista e copia apenas os
arquivos alterados permitidos, faz backup preventivo e confirma que executável,
cadastros e símbolos locais mantiveram os mesmos hashes.

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
web/                    Interface HTML/CSS/JS e módulos puros de apresentação carregados no QWebEngineView.
docs/                   Documentação atual para manutenção e Codex.
tests/                  Caracterização automatizada do kernel e da interface de estado.
MT5/                    Pasta da instalação-modelo; recebe terminal64.exe local.
user_data/              Dados locais e instâncias isoladas.
```

## Documentação principal

- `AGENTS.md`: regras para agentes/Codex trabalharem neste repositório.
- `docs/ARCHITECTURE.md`: arquitetura atual.
- `docs/KERNEL.md`: fronteiras, invariantes e modelo de falhas do kernel.
- `docs/KERNEL_PROTOCOL.md`: contrato versionado entre supervisor e workers MT5.
- `docs/BASELINE_AUDIT_0.4.7.md`: auditoria técnica da baseline validada.
- `docs/CURRENT_STATUS.md`: o que funciona e o que ainda falta.
- `docs/MANUAL_TESTS.md`: roteiro atual de testes manuais.
- `docs/ROADMAP.md`: próximos módulos recomendados.
- `docs/CODEX_TASKS.md`: primeira tarefa sugerida para o Codex.
