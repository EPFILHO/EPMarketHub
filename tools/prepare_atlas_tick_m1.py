"""CLI offline do adaptador de ticks contratuais → M1 (DEV-008B.1B).

Comando:

    python tools/prepare_atlas_tick_m1.py \
        --manifest <tick-m1-adapter-manifest.json> \
        [--dry-run]

Lê um manifesto estrito e versionado (`ep_market_hub.atlas.tick_m1_adapter_manifest.v1`,
ver `market_analytics/manifests/tick_m1_adapter.example.json`), descobre
somente `year=*/month=*/session_date=*/ticks.parquet` sob `input_root`,
constrói um Parquet M1 determinístico por sessão encerrada sob
`derived_m1_root` e gera atomicamente um manifesto de materialização do
Atlas (`ep_market_hub.atlas.materialization_manifest.v1`) sob o mesmo
diretório, pronto para `tools/update_market_atlas.py`. Nunca executa o
materializador Atlas nesta fase. `--dry-run` inventaria e planeja sem gravar
nada em disco. Imprime um único JSON compacto no stdout; qualquer erro
imprime um JSON de erro e retorna código de saída 1. Não conecta ao MT5, não
importa Qt e não toca nada dentro do repositório ou da instalação de teste.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_analytics.tick_atlas_adapter import (  # noqa: E402
    TickAdapterError,
    load_tick_adapter_manifest_file,
    run_tick_atlas_adapter,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path, help="Manifesto do adaptador de ticks (JSON)")
    parser.add_argument("--dry-run", action="store_true", help="Inventaria e planeja sem gravar nada")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        manifest = load_tick_adapter_manifest_file(args.manifest)
        result = run_tick_atlas_adapter(manifest=manifest, dry_run=args.dry_run)
    except TickAdapterError as exc:
        print(
            json.dumps(
                {"schema": "ep_market_hub.atlas.tick_m1_adapter_error.v1", "error": str(exc)},
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
