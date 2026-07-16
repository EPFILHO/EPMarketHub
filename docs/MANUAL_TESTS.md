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

## Ações globais e edição ativa

1. Com todas as leituras paradas, confirme que **Parar leituras** está desativado.
2. Inicie a leitura de um terminal e confirme que **Parar leituras** é habilitado.
3. Clique em **Atualizar** e confirme o texto temporário **Atualizando...**, sem abrir ou fechar MT5.
4. Edite somente o apelido de um terminal aberto e conectado.
5. Confirme que o MT5 e a leitura voltam e que os demais terminais não são afetados.
6. Repita alterando corretora ou conta e confirme que a pasta é renomeada e a leitura volta.

## Exclusão

1. Tente excluir um terminal aberto: o botão deve estar desativado.
2. Feche o MT5 daquele terminal.
3. Clique em **Excluir**.
4. Digite `EXCLUIR`.
5. Confirme que cadastro e pasta local foram removidos.

## Símbolos Cash

Em corretoras que têm `US30` desativado e `US30Cash` ativo, selecione Dow Jones no fluxo. O sistema deve resolver para o alias tradável quando disponível.
