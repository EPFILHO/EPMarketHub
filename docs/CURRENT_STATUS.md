# Status atual — Base 0.4.9

A 0.4.9 continua o hardening da baseline funcional 0.4.7. Ela não adiciona funcionalidades nem altera intencionalmente o fluxo validado com três instâncias MT5.

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

## Hardening 0.4.9

- Ações globais de leitura coerentes com a existência de workers ativos.
- Rollback da pasta de instância quando o cadastro JSON falha.
- Edição permitida somente com MT5 fechado e worker parado.
- Rollback da renomeação e do cadastro para falhas durante a edição de terminal fechado.
- Migração automática de caminhos absolutos legados para caminhos relativos à instalação atual.
- Identificação dos fluxos sem leitura recente e estado coerente da ação **Iniciar/Alterar**.
- Distinção visual entre leitura atrasada e fluxo parado, sem reaproveitar cotação ou terminal de outro fluxo.
- Abertura em lote bloqueada quando a seleção não cabe nas vagas simultâneas de MT5 e workers.
- Snapshot individual removido dos cards; a coleta automática e o painel consolidado continuam ativos.
- Mensagens de falha de edição visíveis no próprio modal.

Esses itens possuem cobertura automatizada com fakes e testes de estado em Node. O bloqueio visual da edição, a migração do JSON local e a renomeação após fechar um MT5 real ainda devem ser confirmados no Windows antes do merge.

## Limitações conhecidas

- Mapeamento de símbolo por terminal ainda não existe.
- Contratos B3 precisam ser atualizados manualmente na lista de aliases.
- A interface web ainda está concentrada em um `app.js` grande.
- A ponte PySide/QWebChannel ainda está concentrada em `gui/main_window.py`.
- Testes automatizados cobrem persistência JSON, nomes de pastas, resolução de símbolos, limites do supervisor de workers e orquestração principal da bridge com fakes.
- Processos reais, QWebEngine e integração com a biblioteca MetaTrader5 continuam fora da suíte automatizada multiplataforma.
- O teste real de MT5 depende de Windows com terminais autenticados.
- Numeração alfabética dos MT5, com renumeração após mudanças, e splashscreen permanecem como evoluções futuras aprovadas conceitualmente.
