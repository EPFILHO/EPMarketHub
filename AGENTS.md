# AGENTS.md — EP Market Hub

Use estas instruções ao trabalhar neste repositório.

## Verdade atual do projeto

- App desktop local em Python.
- Interface em PySide6 + QWebEngineView.
- Comunicação Python ↔ JavaScript via QWebChannel.
- Leitura de dados por meio da biblioteca Python `MetaTrader5`.
- Cada terminal MT5 ativo usa um processo worker persistente próprio.
- O processo principal não deve alternar conexões MT5 diretamente.
- O limite simultâneo é uma política de produto centralizada em `core/config.py`; o valor atual é 3.
- Cadastros de terminais podem ser vários.
- A instalação-modelo fica em `MT5/terminal64.exe`.
- As instâncias isoladas ficam em `user_data/mt5_instances/`.
- O login é feito manualmente pelo usuário dentro do MT5.
- Senhas e sessões reais não devem ser versionadas.

## Antes de alterar código

1. Leia `README.md`, `docs/ARCHITECTURE.md`, `docs/CURRENT_STATUS.md` e `docs/CODEX_TASKS.md`.
2. Entenda o fluxo completo: cadastro → instância controlada → abertura do MT5 → worker → filas → bridge → dashboard.
3. Preserve a arquitetura de 1 worker/processo por terminal ativo.

## Arquivos locais e dados reais

Não versionar:

- `MT5/terminal64.exe`
- `user_data/mt5_instances/`
- logs reais
- bancos locais
- sessões do MT5
- dados pessoais ou credenciais

## Qualidade mínima

Antes de concluir uma alteração, rode quando possível:

```bat
python -m compileall -q .
python -m pytest
ruff check .
```

Se Node.js estiver disponível:

```bat
node --check web/app.js
```

## Estilo de mudança

- Prefira commits pequenos e focados.
- Não refatore e implemente nova funcionalidade na mesma mudança.
- Preserve comportamento existente salvo quando a tarefa pedir explicitamente alteração.
- Atualize documentação curta e atual; não ressuscite histórico antigo.
