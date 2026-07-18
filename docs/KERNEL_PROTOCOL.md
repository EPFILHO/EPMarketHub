# Protocolo do kernel — worker MT5 v1

## Objetivo

Este documento congela a comunicação isolada que sustenta o EP Market Hub. A
camada de plataforma e os aplicativos não acessam `MetaTrader5`, processos ou
filas diretamente; eles solicitam operações ao kernel e consomem estados e
dados normalizados.

## Propriedade dos recursos

- `TerminalManager` cria, localiza, abre minimizado e encerra `terminal64.exe`.
- `MT5WorkerManager` limita e supervisiona um processo Python por terminal.
- `MT5Connector` existe somente dentro do worker e possui exatamente uma
  conexão `MetaTrader5` durante toda a sua vida.
- `MarketHubBridge` coordena processo e worker, mas não executa consultas MT5.
- JavaScript conhece apenas slots QWebChannel, payloads serializados e estados.

Nenhum componente pode alternar uma mesma instância de `MT5Connector` entre
terminais ou compartilhar um worker entre dois executáveis.

## Envelope v1

Todo comando contém:

```json
{"protocol_version": 1, "action": "<ação>", "...": "payload"}
```

Todo evento contém:

```json
{
  "protocol_version": 1,
  "terminal_id": "<id estável>",
  "event": "<tipo>",
  "timestamp": "<ISO-8601>",
  "data": {"pid": 1234}
}
```

Comandos ou eventos sem versão, de versão diferente ou com tipo desconhecido
são descartados e registrados. Alterar nomes ou campos obrigatórios exige nova
versão do protocolo e testes de compatibilidade.

## Comandos aceitos

| Ação | Campos adicionais | Resultado esperado |
|---|---|---|
| `stop` | nenhum | encerra o loop do worker |
| `snapshot` | nenhum | antecipa o próximo snapshot |
| `update_symbols` | `symbols` | substitui o catálogo lógico do worker |
| `reconnect` | nenhum | encerra a conexão atual e força nova tentativa |
| `set_live_stream` | `slot_id`, `symbol` | cria ou altera um fluxo |
| `clear_live_stream` | `slot_id` | remove um fluxo |
| `clear_all_live_streams` | nenhum | remove todos os fluxos daquele worker |

## Eventos aceitos

| Evento | Papel |
|---|---|
| `started` | processo worker criado |
| `status` | transição de conexão |
| `snapshot` | fotografia consolidada do terminal |
| `live_status` | estado de um fluxo |
| `live_tick` | cotação de um fluxo |
| `heartbeat` | prova periódica de vida |
| `terminal_restart_required` | worker vivo detectou seu MT5 fechado |
| `error` | falha não recuperada do worker |
| `stopped` | worker encerrado |

O PID identifica a execução proprietária. O supervisor rejeita eventos de uma
execução anterior antes que alcancem a bridge.

O campo `data.state` usa o vocabulário de `core/terminal_states.py`. Novos
valores desta versão são extensões compatíveis do protocolo v1: o envelope, os
tipos de comando/evento e os campos obrigatórios permanecem inalterados.
Consumidores devem preservar valores desconhecidos como diagnóstico, sem
convertê-los silenciosamente em `connected`.

## Reabertura controlada

Quando o usuário fecha diretamente a janela do MT5 enquanto seu worker continua
ativo:

1. o worker encerra a conexão inválida e emite `terminal_restart_required`;
2. o worker não chama `MetaTrader5.initialize()` enquanto o processo não existir;
3. a bridge pede ao `TerminalManager` a abertura portátil e minimizada;
4. a detecção por caminho completo impede abrir uma segunda cópia;
5. no ciclo de reconexão já existente, o worker conecta novamente ao mesmo MT5.

Uma falha IPC com o processo ainda detectável é apresentada como **MT5 aberto /
Reconectando**. Quando a ausência do processo é confirmada, o estado
passa a `reopening_terminal` e a interface apresenta **Reabrindo MT5 /
Reconectando**. Se a pasta ou o executável tiver desaparecido, a bridge
encerra o worker e exige reconciliação explícita, evitando tentativas e logs
repetidos.

Autenticação recusada, conta divergente, corretora offline e terminal divergente
não emitem `connected`. Erros transitórios continuam sendo tentados pelo ciclo
existente; após tentativas prolongadas, `attention_required` torna explícita a
necessidade de intervenção sem interromper automaticamente o worker.

Fechar o terminal pela interface primeiro encerra o worker. O MT5 só é fechado
depois que a morte do worker é confirmada; se o worker resistir, ambos permanecem
explicitamente em falha para nova tentativa, sem criar um ciclo de reabertura.
Eventos tardios de encerramento ou erro concluem apenas transições de abertura e
jamais apagam `launch_failed` ou `close_failed` já confirmados.

## Entrega e congestionamento

- `live_tick`, `snapshot` e `heartbeat` são renováveis e podem ser descartados
  quando a fila estiver cheia.
- Eventos de ciclo de vida recebem uma tentativa de entrega limitada.
- O supervisor detecta morte do processo mesmo sem evento final.
- Nenhuma espera em fila pode bloquear indefinidamente um worker ou o shutdown.

## Contrato para plataforma e aplicativos

Consumidores podem solicitar estado, símbolos, snapshots, ticks e futuramente
candles. Eles não podem escolher PID, manipular fila, inicializar a biblioteca,
abrir executável nem alterar a política simultânea. Novos consumidores devem
depender deste contrato em vez de reproduzir sua lógica.
