# Testes manuais atuais

## Preparação

1. Copie `terminal64.exe` para `MT5/terminal64.exe`.
2. Rode `python app.py`.
3. Consulte `MAX_ACTIVE_TERMINALS` em `core/config.py` (valor de produção atual: 3) e cadastre pelo menos essa quantidade de terminais.
4. Faça login manual em cada MT5 criado.

## Roteiro principal

1. Marque até o limite atual de terminais na tela **Terminais MT5**.
2. Clique em **Abrir selecionados**.
3. Confirme que todos abrem e que as leituras entram em estado conectado.
4. Vá ao Dashboard.
5. Com a política atual em 3, configure 3 fluxos, cada um em um terminal diferente.
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

1. Com a capacidade atual totalmente ocupada, selecione os MT5 abertos e confirme que **Abrir selecionados** permanece desativado.
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

## Fechamento do kernel 0.4.10

1. Abra e conecte a quantidade máxima atual de MT5 e confirme um PID Python distinto por terminal.
2. Pare apenas a leitura do terminal intermediário e confirme que os demais continuam conectados e atualizando.
3. Reinicie a leitura parada e confirme que ela recebe um novo PID sem alterar os outros workers.
4. Feche um MT5 pela interface e confirme **MT5 fechado / Desconectado** sem contaminar os demais cards.
5. Reabra esse MT5 pela interface e confirme a restauração da leitura e dos fluxos anteriormente configurados para ele.
6. Clique repetidamente em **Parar leituras** e depois feche o aplicativo; confirme que não surge erro e que nenhum MT5 controlado permanece aberto.
7. Inicie novamente o aplicativo e confirme que cadastros, caminhos relativos e aliases continuam preservados.
8. Confirme que não existe opção na interface nem arquivo em `user_data` para o usuário alterar o limite simultâneo.
9. Com uma leitura conectada, feche diretamente a janela daquele MT5 pelo **X**.
10. Confirme a sequência coerente **MT5 aberto / Reconectando** enquanto o processo ainda é detectado, depois **Reabrindo MT5 / Reconectando** quando sua ausência for confirmada, sem voltar ao estado anterior durante a reabertura.
11. Confirme que o kernel reabre a mesma instância uma única vez, minimizada, e que os badges voltam a **MT5 aberto / Conectado**.
12. Selecione dois ou três MT5 abertos e clique em **Fechar selecionados**.
13. Confirme que o texto do botão mostra o progresso e que cada card muda para **MT5 fechado / Desconectado** assim que seu próprio fechamento termina, sem esperar o último.

## Máquina de estados do kernel

Use cadastros descartáveis e não armazene credenciais em arquivos ou capturas.

1. Clique em **Abrir MT5** e confirme imediatamente **Abrindo MT5 / Iniciando** antes do primeiro resultado da biblioteca.
2. Com login válido, confirme a transição para **MT5 aberto / Conectado**.
3. Sem login, confirme **MT5 aberto / Aguardando login**.
4. Provoque uma autenticação recusada no próprio MT5 e confirme **MT5 aberto / Falha de autenticação**, com orientação para verificar conta, senha e servidor.
5. Autentique deliberadamente outra conta e confirme **MT5 aberto / Conta divergente**; nenhum fluxo desse terminal deve ser considerado conectado.
6. Restaure a conta correta e confirme recuperação automática para **Conectado**.
7. Interrompa a conexão da corretora mantendo o processo aberto e confirme **MT5 aberto / Corretora desconectada**, sem confundir com falha IPC.
8. Feche o MT5 pelo X e confirme **Reabrindo MT5 / Reconectando**, seguido da abertura minimizada.
9. Clique em **Fechar MT5** e confirme imediatamente **Fechando MT5 / Encerrando**, terminando em **MT5 fechado / Desconectado**.
10. Confirme nos testes automatizados que um worker resistente mantém seu MT5 aberto, produz falha explícita e não dispara um ciclo de fechamento e reabertura.
11. Deixe um worker de teste sem atividade além do limite interno e confirme **Worker sem resposta**; ao retomar um heartbeat válido, confirme a recuperação do estado.
12. Os cenários artificiais de processo duplicado, biblioteca ausente, worker que cai, evento tardio e worker que resiste ao kill permanecem cobertos por fakes e não devem ser provocados em uma sessão real autenticada.

## Reconciliação de instância local

Use apenas um cadastro descartável, com MT5 e worker fechados.

1. Mova sua pasta para fora de `user_data/mt5_instances` e clique em **Atualizar**.
2. Confirme **Instância ausente**, ações de abertura/edição/seleção bloqueadas e o botão **Resolver**.
3. Abra **Resolver**, cancele, devolva a pasta ao caminho original e confirme que **Atualizar** restaura o estado **MT5 fechado**.
4. Mova novamente a pasta e escolha **Recriar instância**; confirme a cópia limpa de `terminal64.exe` e a orientação para login manual.
5. Em outro cadastro descartável ausente, escolha **Remover cadastro** e confirme que ele sai da lista sem qualquer tentativa de apagar ou restaurar pastas externas.
6. Em outro cadastro descartável, abra o modal normal **Excluir**, digite `EXCLUIR`, remova a pasta por fora e então confirme; o cadastro deve ser removido diretamente, sem abrir **Resolver**.
7. Com a pasta ausente, tente salvar pelo modal **Editar**; ele deve fechar e abrir **Resolver**, exibindo a orientação uma única vez e sem remover o cadastro.
8. Remova somente o cadastro e recupere a pasta original da Lixeira para o caminho esperado.
9. Tente cadastrar novamente a mesma corretora e conta e confirme o modal **Pasta existente não cadastrada**.
10. Escolha **Usar pasta existente** e confirme que o cadastro reaparece, os arquivos da pasta permanecem intactos e nenhum MT5 é aberto automaticamente.
11. Repita a adoção deixando o `terminal64.exe` daquela pasta aberto; confirme que a operação é bloqueada até o MT5 ser fechado.
12. Em uma pasta órfã sem `terminal64.exe`, confirme que a adoção repara somente o executável a partir de `MT5/terminal64.exe` e preserva os demais arquivos.

Falhas artificiais de fila cheia/fechada, processo resistente, evento de PID antigo e promoção interrompida de JSON são cobertas por fakes na suíte automatizada. Não devem ser provocadas encerrando processos reais à força durante uma sessão autenticada.

## Sincronização da instalação de teste

Com o aplicativo e todos os MT5 fechados, simule primeiro:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync_test_copy.ps1 -TargetRoot D:\EP\EPMarketHub
```

Revise a lista e aplique somente depois:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync_test_copy.ps1 -TargetRoot D:\EP\EPMarketHub -Apply
```

O script não executa Git no destino, não remove arquivos, recusa caminhos sensíveis, cria backup preventivo dos registros JSON e confirma os hashes deles e do executável antes e depois.

## Símbolos Cash

Em corretoras que têm `US30` desativado e `US30Cash` ativo, selecione Dow Jones no fluxo. O sistema deve resolver para o alias tradável quando disponível.
