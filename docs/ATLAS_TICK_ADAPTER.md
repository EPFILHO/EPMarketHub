# Adaptador de ticks contratuais para o Atlas

`market_analytics/tick_atlas_adapter.py` converte sessões encerradas de
`ticks.parquet` em segmentos M1 de um contrato individual e gera um
manifesto consumível pelo materializador do Atlas.

## Fluxo

```powershell
python tools/prepare_atlas_tick_m1.py --manifest <tick-adapter.json> --dry-run
python tools/prepare_atlas_tick_m1.py --manifest <tick-adapter.json>
python tools/update_market_atlas.py `
  --manifest <atlas_materialization_manifest.json-da-geracao> `
  --output-root <atlas_output_root>
```

O primeiro comando apenas inventaria e processa em memória. O segundo cria
os M1 e uma geração do adaptador; ele não executa o Atlas. O terceiro é outro
portão operacional.

## Garantias

- leitura de ticks por batches e mesma política de preço, volume e
  deduplicação do MVP quantitativo;
- identidade bruta validada contra manifesto estrito;
- somente sessões anteriores à data da execução;
- um objeto M1 imutável e endereçado por SHA-256;
- timestamps convertidos do UTC real para o relógio da sessão e novamente
  tipados como UTC, conforme a política `source_wall_clock_no_conversion` do
  Atlas;
- geração contendo `state.json` e manifesto Atlas promovida por um único
  `current.json` atômico;
- remoção ou corrupção de sessão já publicada falha fechado;
- `no_change` preserva bytes e mtimes;
- lock por série impede dois escritores simultâneos.

O adaptador nunca une contratos. Uma troca de vencimento exige outra
identidade ou um mapa de rolagem explícito.
