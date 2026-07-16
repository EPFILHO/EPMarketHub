# Testes manuais atuais

## Preparação

1. Copie `terminal64.exe` para `MT5/terminal64.exe`.
2. Rode `python app.py`.
3. Cadastre pelo menos 3 terminais, por exemplo Forex A, Forex B e B3.
4. Faça login manual em cada MT5 criado.

## Roteiro principal

1. Marque até 3 terminais na tela **Terminais MT5**.
2. Clique em **Abrir selecionados**.
3. Confirme que todos abrem e que as leituras entram em estado conectado.
4. Vá ao Dashboard.
5. Configure 3 fluxos, cada um em um terminal diferente.
6. Clique em **Iniciar 3 fluxos**.
7. Confirme que aparecem 3 PIDs diferentes e que leituras/ticks avançam.
8. Clique em **Parar teste**.
9. Confirme que os cards dos fluxos são limpos imediatamente.
10. Pare a leitura de apenas um terminal e confirme que os outros continuam.
11. Feche o app pelo X e confirme que os MT5 controlados também fecham.

## Ações globais e edição segura

1. Com todas as leituras paradas, confirme que **Parar leituras** está desativado.
2. Inicie a leitura de um terminal e confirme que **Parar leituras** é habilitado.
3. Clique em **Atualizar** e confirme o texto temporário **Atualizando...**, sem abrir ou fechar MT5.
4. Com um terminal aberto ou lendo, confirme que **Editar** está desativado.
5. Feche o MT5, confirme que a leitura está parada e edite somente o apelido.
6. Confirme que o cadastro foi atualizado sem abrir o MT5 e sem alterar o nome da pasta.
7. Ainda com o terminal fechado, altere corretora ou conta e confirme que a pasta é renomeada.

## Portabilidade da instalação

1. Feche o aplicativo e todos os MT5 controlados.
2. Mova ou copie a pasta completa do EP Market Hub, incluindo `user_data/mt5_instances/`.
3. Inicie `app.py` pela nova pasta, mesmo que o terminal do VS Code esteja em outro diretório.
4. Confirme que os cards mostram caminhos sob a nova instalação.
5. Confirme que `user_data/terminals.json` contém caminhos iniciados por `user_data/`, sem letra de unidade.
6. Com o MT5 fechado, altere corretora ou conta e confirme a renomeação na nova instalação.

## Estado dos fluxos ao vivo

1. Inicie um fluxo e confirme que seu botão **Iniciar** fica desativado.
2. Troque o ativo ou terminal no seletor e confirme que o botão passa a **Alterar** e fica habilitado.
3. Aplique a alteração e confirme que o botão volta a **Iniciar** desativado.
4. Atrase temporariamente um fluxo ainda ativo e confirme que o banner informa seu número, terminal, ativo e idade da última leitura como **leitura atrasada**.
5. Confirme que o banner mantém espaço para duas linhas e não desloca os cards quando o detalhe aparece.
6. Feche o MT5 usado por um dos fluxos e volte ao Dashboard.
7. Confirme que o seletor preserva o terminal anterior com a indicação **MT5 fechado**, sem escolher outra corretora automaticamente.
8. Confirme que bid, ask, PID e demais dados antigos são limpos e que o fluxo é contado como parado, não como leitura atrasada.
9. Selecione outro terminal conectado e confirme que **Alterar** é habilitado.

## Capacidade e ações dos terminais

1. Com 3 MT5 abertos, selecione-os e confirme que **Abrir selecionados** permanece desativado.
2. Confirme que **Fechar selecionados** permanece habilitado para os terminais abertos selecionados.
3. Feche um MT5, selecione exatamente um terminal fechado e confirme que **Abrir selecionados** volta a ser habilitado.
4. Confirme que os cards não exibem mais o botão **Snapshot**.
5. No Dashboard, confirme que o snapshot consolidado continua atualizando automaticamente e que **Solicitar atualização agora** continua funcional.

## Exclusão

1. Tente excluir um terminal aberto: o botão deve estar desativado.
2. Feche o MT5 daquele terminal.
3. Clique em **Excluir**.
4. Digite `EXCLUIR`.
5. Confirme que cadastro e pasta local foram removidos.

## Símbolos Cash

Em corretoras que têm `US30` desativado e `US30Cash` ativo, selecione Dow Jones no fluxo. O sistema deve resolver para o alias tradável quando disponível.
