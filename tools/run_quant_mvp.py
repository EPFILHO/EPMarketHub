"""CLI do MVP quantitativo WIN (DEV-006): ticks brutos → barras, features e relatório.

Comando reprodutível único:

    python tools/run_quant_mvp.py \
        --input-root D:\\EPData\\MarketHub\\raw\\clear\\win \
        --output-root D:\\EPData\\MarketHub\\analytics\\win_mvp

Não é uma GUI: imprime progresso curto por sessão e um resumo final no
console. Só processa `raw/clear/win/year=*/month=*/session_date=*/
ticks.parquet` — nenhum outro `source_id`/`logical_id`/símbolo, MT5 ou
terminal ao vivo é tocado por este script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_analytics.config import FeatureConfig  # noqa: E402
from market_analytics.quant_mvp import (  # noqa: E402
    TIMEFRAMES,
    QuantMvpError,
    RunProgress,
    run_quant_mvp,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-root", required=True, type=Path, help="Pasta do logical_id (ex.: raw/clear/win)")
    parser.add_argument("--output-root", required=True, type=Path, help="Pasta de saída dos artefatos analíticos")
    parser.add_argument("--batch-size", type=int, default=200_000, help="Linhas por batch de leitura Parquet")
    parser.add_argument("--atr-period", type=int, default=FeatureConfig().atr_period)
    parser.add_argument("--volatility-window", type=int, default=FeatureConfig().volatility_window)
    parser.add_argument("--trend-window", type=int, default=FeatureConfig().trend_window)
    parser.add_argument("--volume-window", type=int, default=FeatureConfig().volume_window)
    return parser.parse_args(argv)


def _print_progress(event: RunProgress) -> None:
    print(f"[{event.session_date.isoformat()}] {event.status}: {event.detail or event.path}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    feature_config = FeatureConfig(
        atr_period=args.atr_period,
        volatility_window=args.volatility_window,
        trend_window=args.trend_window,
        volume_window=args.volume_window,
    )

    try:
        summary = run_quant_mvp(
            input_root=args.input_root,
            output_root=args.output_root,
            feature_config=feature_config,
            batch_size=args.batch_size,
            progress=_print_progress,
        )
    except QuantMvpError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    totals = summary["totals"]
    print("--- resumo final ---")
    print(f"sessões descobertas: {totals['sessions_discovered']}")
    print(f"sessões processadas: {totals['sessions_processed']}")
    print(f"sessões rejeitadas: {totals['sessions_rejected']}")
    print(f"ticks lidos: {totals['ticks_read']}")
    print(f"ticks válidos: {totals['ticks_valid']}")
    print(f"ticks duplicados removidos: {totals['ticks_duplicated']}")
    for timeframe in TIMEFRAMES:
        print(f"barras {timeframe}: {totals['bars'][timeframe]}")
    if summary["alerts"]:
        print(f"alertas: {len(summary['alerts'])}")
        for alert in summary["alerts"]:
            print(f"  - {alert}")
    print(f"duração: {summary['duration_seconds']:.2f}s")
    print(f"saída: {summary['output_root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
