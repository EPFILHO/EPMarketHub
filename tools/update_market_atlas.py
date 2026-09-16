"""CLI offline do materializador incremental do Atlas (DEV-008B.1A).

Comando:

    python tools/update_market_atlas.py \
        --manifest <atlas-materialization.json> \
        --output-root <diretorio-fora-do-repositorio> \
        [--dataset <logical_id>] \
        [--dry-run]

Sem `--dataset`, processa todos os datasets declarados no manifesto, em
ordem, cada um com seu próprio lock/estado sob `<output-root>/<logical_id>/`.
`--dry-run` inventaria e planeja sem gravar nada em disco. Imprime um único
JSON compacto no stdout (status, sessões adicionadas/recalculadas, primeira
mudança, geração anterior/nova, issues e hashes por dataset); qualquer erro
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

from market_analytics.atlas_incremental import MaterializationError, run_update  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path, help="Manifesto de materialização (JSON)")
    parser.add_argument("--output-root", required=True, type=Path, help="Pasta de saída, fora do repositório")
    parser.add_argument("--dataset", default=None, help="Restringe a um único logical_id do manifesto")
    parser.add_argument("--dry-run", action="store_true", help="Inventaria e planeja sem gravar nada")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = run_update(
            manifest_path=args.manifest,
            output_root=args.output_root,
            dataset_id=args.dataset,
            dry_run=args.dry_run,
        )
    except MaterializationError as exc:
        print(
            json.dumps(
                {"schema": "ep_market_hub.atlas.materialization_error.v1", "error": str(exc)},
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
