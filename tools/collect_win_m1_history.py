"""CLI offline/read-only — histórico M1 e features do WIN$ (DEV-007).

Produtor da entrega A+B do Fusion Quant DEV-009C.1: solicita `copy_rates_range`
mês a mês a um terminal MT5 de pesquisa explicitamente informado, confere
contagem/fingerprint contra o inventário independente já congelado pelo
Fusion Quant e escreve barras M1 + features M5 num diretório de saída também
explícito. Não é GUI, não integra bridge/worker/kernel, não escreve nada em
`D:\\EP\\EPMarketHub` nem dentro deste repositório.

Esta entrega (Claude/DEV-007) não executa este script: nenhum terminal MT5 é
aberto nesta sessão. É reservado para a execução real futura, após auditoria
do Codex e aprovação explícita do proprietário.

Exemplo de comando de execução futura (ver `docs/work_orders/DEV-007.md`):

    python tools/collect_win_m1_history.py ^
      --terminal-path "C:\\Users\\Famil\\Documents\\Codex\\Fusion\\Fusion-Quant\\runtime\\mt5-clear-research\\terminal64.exe" ^
      --inventory-path "C:\\...\\runtime\\win_copy2_monthly_retro\\inventory_m1.json" ^
      --inventory-sha256 59589F49F519742FCA11F4FF7F71566B6400EFDFD9745D8758918F718A203B5C ^
      --output-root D:\\EPData\\MarketHub\\analytics\\win_m1_history

## Ordem do preflight (auditoria Codex da 1ª entrega)

Toda validação que não depende de MT5 — protocolo congelado (símbolo/janela),
destino dentro de `D:\\EPData\\MarketHub`, sem sobreposição com o
repositório/terminal/inventário, hash e schema estrito do inventário —
acontece ANTES de `Mt5RatesProvider` ser sequer construído. Nenhum terminal é
aberto se qualquer uma dessas checagens falhar.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_analytics.config import FeatureConfig  # noqa: E402
from market_analytics.win_m1_collector import (  # noqa: E402
    FROZEN_WINDOW_END_EXCLUSIVE,
    FROZEN_WINDOW_START,
    Mt5RatesProvider,
)
from market_analytics.win_m1_features import (  # noqa: E402
    ProducerProgress,
    assert_output_within_allowed_root,
    preflight,
    run_m1_history_producer,
)

# Dados reais desta entrega só podem ser gravados sob esta raiz — ver
# AGENTS.md/docs/work_orders/DEV-007.md ("Qualidade e segurança").
ALLOWED_REAL_OUTPUT_ROOT = Path(r"D:\EPData\MarketHub")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta M1 do WIN$ (Clear, terminal de pesquisa) e produz features históricas M5 (DEV-007)."
    )
    parser.add_argument("--terminal-path", required=True, help="Caminho do terminal64.exe de pesquisa.")
    parser.add_argument(
        "--inventory-path", required=True, type=Path, help="Caminho do inventory_m1.json congelado pelo Fusion Quant."
    )
    parser.add_argument(
        "--inventory-sha256",
        required=True,
        help="SHA-256 esperado do arquivo de inventário inteiro (64 caracteres hex).",
    )
    parser.add_argument(
        "--output-root", required=True, type=Path, help="Diretório de saída (dados reais: D:\\EPData\\MarketHub\\...)."
    )
    parser.add_argument("--source-id", default="clear_research", help="Identificador da fonte (default: clear_research).")
    parser.add_argument("--symbol", default="WIN$", help="Símbolo solicitado ao terminal (default: WIN$).")
    parser.add_argument(
        "--start-date", default=FROZEN_WINDOW_START.isoformat(), help="Início da janela (dia 1 de um mês, UTC, inclusive)."
    )
    parser.add_argument(
        "--end-date",
        default=FROZEN_WINDOW_END_EXCLUSIVE.isoformat(),
        help="Fim da janela (dia 1 de um mês, UTC, exclusive).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    start_date = date.fromisoformat(args.start_date)
    end_date_exclusive = date.fromisoformat(args.end_date)

    assert_output_within_allowed_root(args.output_root, ALLOWED_REAL_OUTPUT_ROOT)

    # Preflight completo (protocolo congelado, destino, hash/schema do
    # inventário) ANTES de construir Mt5RatesProvider — nenhum terminal MT5
    # é aberto se qualquer checagem aqui falhar.
    preflight(
        output_root=args.output_root,
        terminal_path=args.terminal_path,
        inventory_path=args.inventory_path,
        inventory_sha256=args.inventory_sha256,
        symbol=args.symbol,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        require_frozen_protocol=True,
    )

    def _on_progress(event: ProducerProgress) -> None:
        print(f"[{event.month}] {event.status}: {event.detail}")

    with Mt5RatesProvider(args.terminal_path, symbol=args.symbol) as provider:
        summary = run_m1_history_producer(
            provider=provider,
            terminal_path=args.terminal_path,
            inventory_path=args.inventory_path,
            inventory_sha256=args.inventory_sha256,
            output_root=args.output_root,
            source_id=args.source_id,
            symbol=args.symbol,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            feature_config=FeatureConfig(),
            progress=_on_progress,
        )

    print(f"OK: {summary['totals']}")
    print(f"Artefatos em: {summary['output_root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
