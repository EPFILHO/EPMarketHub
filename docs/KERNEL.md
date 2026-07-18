# Kernel do EP Market Hub

## Propósito

O kernel mantém instâncias MetaTrader 5 isoladas e controla seu ciclo de vida.
Dashboard, Ativos e futuros módulos analíticos são consumidores do kernel; eles
podem evoluir sem alterar suas invariantes.

## Fronteira

Pertencem ao kernel:

- cadastro local de corretora e conta, sem senha;
- identidade única por `corretora + conta` e `id` interno estável;
- criação de instâncias portáteis em `user_data/mt5_instances/`;
- abertura e fechamento individual ou em lote dos MT5 controlados;
- um processo Python persistente e proprietário por terminal ativo;
- conexão exclusiva do worker com a biblioteca Python `MetaTrader5`;
- filas de comandos e eventos entre supervisor e workers;
- limite simultâneo definido pela política de produto `MAX_ACTIVE_TERMINALS`;
- persistência atômica e recuperação dos registros locais;
- encerramento idempotente de workers e terminais.

Não pertencem ao kernel:

- aparência e navegação da interface;
- quantidade de cards de prova no Dashboard;
- catálogo e apresentação da aba Ativos;
- símbolos por terminal, candles, histórico e análises futuras;
- splashscreen e numeração visual dos MT5.

## Invariantes

1. Cada terminal ativo possui no máximo um worker.
2. Dois workers ativos não podem compartilhar o mesmo `terminal64.exe`.
3. O processo principal nunca alterna uma conexão `MetaTrader5` entre terminais.
4. Cadastros são ilimitados; apenas a ativação simultânea é limitada.
5. O limite simultâneo possui uma única fonte no código e não é configurável pelo usuário.
6. Fechar ou parar um terminal não interrompe os demais.
7. Nenhuma senha ou sessão MT5 é persistida pelo aplicativo.
8. Uma falha de escrita não substitui o último JSON válido por conteúdo parcial.
9. Uma falha parcial de cadastro, edição ou exclusão é compensada ou registrada de forma recuperável.
10. O shutdown pode ser chamado mais de uma vez sem repetir efeitos destrutivos.

## Política do limite simultâneo

`core/config.py` contém `MAX_ACTIVE_TERMINALS`. O valor atual é `3`, cenário
validado com MT5 reais. Alterações futuras são decisões de produto e exigem uma
nova versão e validação; não existe preferência em `user_data` nem controle na
interface.

O supervisor aceita injeção do limite para que os testes cubram valores como
`2`, `3` e `4`. Em produção, `app.py` sempre usa a política central.

## Estados observáveis do worker

| Estado | Processo vivo | Conectado | Significado |
|---|---:|---:|---|
| `stopped` | não | não | leitura não iniciada ou encerrada |
| `starting` | sim | não | processo criado, aguardando primeira conexão |
| `waiting_login` | sim | não | MT5 aberto sem conta autenticada |
| `reopening_terminal` | sim | não | worker detectou o MT5 fechado e aguarda reabertura controlada |
| `reconnecting` | sim | não | conexão perdida ou tentativa em andamento |
| `connected` | sim | sim | biblioteca conectada ao terminal específico |
| `error` | não ou resistente | não | falha inesperada ou encerramento não confirmado |

Eventos de uma execução anterior do mesmo terminal não podem sobrescrever o
estado da execução atual. Eventos volumosos de cotação podem ser descartados se
a interface estiver congestionada; eventos de ciclo de vida recebem tentativa
limitada de entrega, e o supervisor sintetiza erro quando detecta um processo
encerrado sem evento final.

## Modelo de falhas

O fechamento do kernel cobre e testa:

- falha ao criar o processo worker;
- worker que exige `terminate()` ou `kill()`;
- worker que permanece vivo mesmo após encerramento forçado;
- fila de comandos cheia durante uma solicitação;
- fila de eventos congestionada;
- evento residual de um PID anterior;
- evento de ciclo de vida consumido durante uma consulta QWebChannel concorrente;
- falha de abertura ou fechamento de `terminal64.exe`;
- pasta da instância ou `terminal64.exe` removido fora do aplicativo;
- JSON vazio, inválido, com codificação danificada ou inacessível;
- falha ao promover o arquivo temporário da escrita atômica;
- chamada repetida de shutdown.

"Falha segura" significa preservar o último dado válido, não misturar sessões,
não afetar outros terminais e retornar ou registrar um estado explícito. Não
significa esconder a falha nem afirmar sucesso sem confirmar o resultado.

Cada cadastro também expõe a integridade local `ready`, `directory_missing`,
`executable_missing` ou `invalid_path`. Uma instância indisponível não pode ser
aberta, editada nem selecionada em lote. O usuário pode recriar o executável a
partir da instalação-modelo ou remover somente o cadastro. Se uma pasta reaparecer
sem cadastro, o kernel exige adoção explícita, preserva seu conteúdo e bloqueia a
operação enquanto o executável estiver aberto. O kernel não tenta manipular
automaticamente a Lixeira do Windows nem sobrescreve uma pasta órfã.

## Regra de evolução

Depois do fechamento da 0.4.10, mudanças no kernel devem ocorrer somente diante
de defeito reproduzido ou requisito aprovado. Plataforma, interface e módulos
devem depender destes contratos em vez de duplicar regras de processo,
persistência ou limite simultâneo.

O envelope, os comandos, os eventos e a reabertura controlada estão congelados
como protocolo v1 em `docs/KERNEL_PROTOCOL.md`.
