# Status atual — Base 0.4.7

## Funcionalidades validadas

- Criação de instâncias controladas a partir de `MT5/terminal64.exe`.
- Cadastro por corretora e número de conta.
- Edição de apelido, corretora e conta, com renomeação da pasta da instância.
- Exclusão de instância apenas com MT5 fechado.
- Limite de 3 terminais ativos simultaneamente.
- Seleção explícita dos terminais que serão abertos.
- Abertura individual e em lote com início automático da leitura.
- Um worker/processo persistente por terminal.
- Dashboard com apenas os terminais conectados.
- Teste simultâneo de até 3 fluxos ao vivo.
- Fechamento dos MT5 controlados ao sair do aplicativo.
- Resolução de aliases como `US30Cash`, `US100Cash`, `US500Cash`, `WINQ26`, `BTCUSD` etc.

## Limitações conhecidas

- Mapeamento de símbolo por terminal ainda não existe.
- Contratos B3 precisam ser atualizados manualmente na lista de aliases.
- A interface web ainda está concentrada em um `app.js` grande.
- A ponte PySide/QWebChannel ainda está concentrada em `gui/main_window.py`.
- Testes automatizados cobrem apenas parte da persistência, nomes de pastas e resolução de símbolos.
- O teste real de MT5 depende de Windows com terminais autenticados.
