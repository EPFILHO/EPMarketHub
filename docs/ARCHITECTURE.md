# Arquitetura atual

## Visão geral

```text
app.py
  └─ MainWindow / MarketHubBridge
       ├─ TerminalRegistry
       ├─ TerminalManager
       ├─ SymbolRegistry
       ├─ WorkerManager
       └─ QWebChannel → web/app.js

Worker MT5 #1 → terminal64.exe da instância A
Worker MT5 #2 → terminal64.exe da instância B
Worker MT5 #3 → terminal64.exe da instância C
```

O processo principal cuida da interface, registros, abertura/fechamento dos terminais e supervisão dos workers. Ele não deve manter múltiplas conexões diretas com MT5.

Cada worker é um processo separado, inicializa a biblioteca `MetaTrader5` apontando para uma instância específica e mantém aquela conexão viva enquanto a leitura estiver ativa.

## Instâncias MT5

A pasta `MT5/` contém o executável-modelo:

```text
MT5/terminal64.exe
```

Cada cadastro cria uma cópia isolada em:

```text
user_data/mt5_instances/<CORRETORA>-<CONTA>/terminal64.exe
```

A identidade de negócio é `corretora + conta informada`, mas cada terminal também possui um `id` interno estável.

## Comunicação

Workers e processo principal trocam mensagens por filas do `multiprocessing`.

Tipos importantes de mensagens:

- estado do worker;
- snapshot consolidado;
- resposta de fluxo ao vivo;
- erro;
- heartbeat.

O frontend recebe o estado por métodos expostos no QWebChannel e renderiza apenas terminais conectados no Dashboard.

## Símbolos

`SymbolRegistry` mantém ativos lógicos com aliases. A resolução do símbolo ocorre dentro de cada worker usando metadados do MT5. O critério atual prioriza símbolos tradáveis e com cotação válida.
