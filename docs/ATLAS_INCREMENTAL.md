# Materializador incremental do Atlas

`market_analytics/atlas_incremental.py` persiste o Atlas causal produzido por
`build_causal_atlas` a partir de segmentos Parquet M1 declarados. Ele é
offline, genérico por ativo/fonte e não acessa MT5, Qt ou rede.

## Execução

```powershell
python tools/update_market_atlas.py `
  --manifest <manifesto.json> `
  --output-root <diretorio-fora-do-repositorio> `
  [--dataset <logical_id>] `
  [--dry-run]
```

O `output_root` informado na CLI deve ser o mesmo declarado no manifesto.
Essa confirmação dupla evita publicar uma geração no destino errado. Use o
modelo sem dados pessoais em
`market_analytics/manifests/atlas.example.json`.

## Garantias

- segmentos têm identidade, fonte e fronteiras de data explícitas;
- somente sessões anteriores à data da execução são elegíveis nesta fase;
  a sessão corrente, mesmo após o fechamento local, fica para a próxima
  execução diária (intradiário pertence à DEV-008B.2);
- `symbol`, `source_id` e, quando presente, `timeframe=M1` são validados;
- remoção de sessão falha fechado; inserção/correção histórica invalida o
  sufixo causal;
- objetos por sessão são imutáveis e endereçados por SHA-256;
- uma geração só se torna vigente pela troca atômica de `current.json`;
- lock vivo ou ambíguo nunca é roubado; lock comprovadamente órfão é
  recuperado;
- `--dry-run` não cria arquivos;
- JSON persistido é estrito (`NaN`/`Infinity` são recusados).

Cada série lógica vive isolada em `<output_root>/<logical_id>`. Não una uma
série contínua e um contrato individual sob o mesmo `logical_id`.

## Incrementalidade desta versão

`no_change` usa os hashes de manifesto, parâmetros e arquivos e não altera
nenhum artefato. Em append/correção, objetos já existentes não são
reescritos e a nova geração referencia o prefixo anterior.

Por segurança matemática, a versão inicial ainda executa o núcleo causal
sobre toda a série M1 em memória quando a entrada muda. Assim, percentis,
ATR, streaks e eventos continuam exatamente iguais a uma reconstrução limpa.
Estado causal serializado para eliminar também esse recálculo de CPU fica
adiado até haver medição que o justifique; isso não altera o contrato nem os
resultados publicados.

## Recuperação

Se a execução cair antes de `current.json`, a geração anterior continua
vigente. Objetos completos já gravados podem ser reutilizados na repetição.
Uma geração órfã ou um relatório anterior ao ponteiro não é tratado como
verdade: a leitura parte sempre de `current.json` e valida o manifesto e os
hashes dos objetos referenciados.
