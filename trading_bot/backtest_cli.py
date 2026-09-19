"""تشغيل الـ backtest على تاريخ Twelve Data أو على ملفات CSV محلية.

    export TWELVE_DATA_API_KEY=xxxx
    python -m trading_bot.backtest_cli AAPL --bars 2000 --equity 10000
    python -m trading_bot.backtest_cli AEMD --csv-4h data/AEMD_4h.csv --csv-1d data/AEMD_1d.csv \
        --market-cap 30000000 --free-float 9000000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .backtest import BacktestConfig, Backtester
from .config import BotConfig
from .csvio import load_candles_csv, save_candles_csv
from .dataquality import adjust_for_splits
from .models import StockMetrics


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m trading_bot.backtest_cli",
        description="قياس أداء الاستراتيجية على تاريخ حقيقي (walk-forward)",
    )
    p.add_argument("ticker")
    p.add_argument("--csv-4h", help="ملف شموع 4 ساعات بدل جلبها من الشبكة")
    p.add_argument("--csv-1d", help="ملف شموع يومية بدل جلبها من الشبكة")
    p.add_argument("--save-csv", help="مجلد لحفظ البيانات المجلوبة للتكرار لاحقاً")
    p.add_argument("--api-key", default=os.environ.get("TWELVE_DATA_API_KEY"))
    p.add_argument("--bars", type=int, default=1000, help="عدد شموع 4 ساعات")
    p.add_argument("--daily-bars", type=int, default=400)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--spread-pct", type=float, default=2.0, help="فرضية الفرق طلب/عرض %%")
    p.add_argument("--slippage-pct", type=float, default=0.5)
    p.add_argument("--commission-per-share", type=float, default=0.0)
    p.add_argument("--market-cap", type=float, help="الكاب (مطلوب وإلا يرفض صمام الأمان)")
    p.add_argument("--free-float", type=float)
    p.add_argument("--no-split-adjust", action="store_true", help="عطّل تصحيح التجزئة")
    p.add_argument("--trades", action="store_true", help="اعرض جدول الصفقات")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.csv_4h and args.csv_1d:
        c4 = load_candles_csv(args.csv_4h)
        c1 = load_candles_csv(args.csv_1d)
    else:
        from .providers.twelve_data import TwelveDataError, TwelveDataProvider

        try:
            provider = TwelveDataProvider(api_key=args.api_key)
            c4 = provider.get_candles(args.ticker, "4h", args.bars)
            c1 = provider.get_candles(args.ticker, "1day", args.daily_bars)
        except TwelveDataError as exc:
            print(f"خطأ في جلب البيانات: {exc}", file=sys.stderr)
            return 2
        if args.save_csv:
            save_candles_csv(f"{args.save_csv}/{args.ticker}_4h.csv", c4)
            save_candles_csv(f"{args.save_csv}/{args.ticker}_1d.csv", c1)

    notes: List[str] = []
    if not args.no_split_adjust:
        c4, r4 = adjust_for_splits(c4)
        c1, r1 = adjust_for_splits(c1)
        notes += [f"4H: {m}" for m in r4.issues] + [f"1D: {m}" for m in r1.issues]

    metrics = StockMetrics(
        ticker=args.ticker,
        market_cap=args.market_cap,
        free_float=args.free_float,
    )
    result = Backtester(
        BotConfig(account_equity=args.equity, risk_per_trade_pct=args.risk_pct / 100),
        BacktestConfig(
            assumed_spread_pct=args.spread_pct / 100,
            slippage_pct=args.slippage_pct / 100,
            commission_per_share=args.commission_per_share,
        ),
    ).run(args.ticker, c4, c1, metrics)

    if args.json:
        print(json.dumps({
            "ticker": result.ticker,
            "bars_tested": result.bars_tested,
            "trades": len(result.closed),
            "win_rate": round(result.win_rate, 4),
            "expectancy_r": round(result.expectancy_r, 4),
            "profit_factor": None if result.profit_factor == float("inf") else round(result.profit_factor, 4),
            "net_pnl": round(result.net_pnl, 2),
            "return_pct": round(result.return_pct, 4),
            "max_drawdown_pct": round(result.max_drawdown_pct, 4),
            "veto_counts": result.veto_counts,
            "data_notes": notes,
            "warnings": result.warnings,
        }, ensure_ascii=False, indent=2))
        return 0

    print(result.summary())
    for note in notes:
        print(f"  ℹ {note}")
    if args.trades and result.closed:
        print("\n" + "-" * 60)
        print(f"{'دخول':<12}{'خروج':<12}{'سبب':<12}{'R':>8}{'ربح':>12}")
        for t in result.closed:
            from datetime import datetime, timezone
            d_in = datetime.fromtimestamp(t.entry_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            d_out = datetime.fromtimestamp(t.exits[-1].ts, tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"{d_in:<12}{d_out:<12}{t.exit_reason:<12}{t.r_multiple:>+8.2f}{t.pnl:>+12.2f}")
    print("\nتذكير: نتائج تاريخية على فرضيات محددة — ليست وعداً بأداء مستقبلي.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
