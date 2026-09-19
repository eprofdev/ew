"""إنتاج الموجز اليومي.

    export TWELVE_DATA_API_KEY=xxxx
    python -m trading_bot.briefing_cli --universe data/run1/candidates.json \
        --out data/run1/briefing.md --top 15

    # بلا شبكة، من الذاكرة المؤقتة
    python -m trading_bot.briefing_cli --universe data/run1/candidates.json --offline
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .briefing import BriefingBuilder
from .config import BotConfig
from .csvio import load_candles_csv
from .dataquality import adjust_for_splits
from .engine import EnhancedTradingBot
from .models import MarketSnapshot, RealTimeData, StockMetrics
from .universe import Universe


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m trading_bot.briefing_cli",
        description="موجز قرار يومي — النظام مُرشِّح والقرار لك",
    )
    p.add_argument("--universe", required=True, help="ملف المرشحين")
    p.add_argument("--cache-dir", default="data/run1/cache")
    p.add_argument("--out", help="مسار حفظ الموجز (markdown)")
    p.add_argument("--api-key", default=os.environ.get("TWELVE_DATA_API_KEY"))
    p.add_argument("--rate-limit", type=int, default=8)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--limit", type=int, help="اكتفِ بأول N رمزاً (توفير رصيد)")
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--offline", action="store_true")
    return p


def snapshot_for(symbol: str, cache: Path, quote: Optional[dict]) -> Optional[MarketSnapshot]:
    path_4h, path_1d = cache / f"{symbol}_4h.csv", cache / f"{symbol}_1d.csv"
    if not path_4h.exists():
        return None
    c4, _ = adjust_for_splits(load_candles_csv(path_4h))
    c1 = []
    if path_1d.exists():
        c1, _ = adjust_for_splits(load_candles_csv(path_1d))

    last = c4[-1] if c4 else None
    price = float(quote.get("close", 0) or 0) if quote else (last.close if last else 0.0)
    previous = float(quote.get("previous_close", 0) or 0) if quote else 0.0
    avg_volume = float(quote.get("average_volume", 0) or 0) if quote else 0.0
    session_volume = float(quote.get("volume", 0) or 0) if quote else (last.volume if last else 0.0)
    if not avg_volume and c1:
        avg_volume = sum(c.volume for c in c1[-20:]) / min(len(c1), 20)

    return MarketSnapshot(
        ticker=symbol,
        metrics=StockMetrics(ticker=symbol, avg_daily_volume=avg_volume),
        realtime=RealTimeData(
            ticker=symbol,
            price=price,
            session_volume=session_volume,
            premarket_change_pct=((price - previous) / previous) if previous > 0 else 0.0,
        ),
        candles_4h=c4,
        candles_1d=c1,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = Universe.load(args.universe).symbols
    if args.limit:
        symbols = symbols[: args.limit]
    if not symbols:
        print("لا رموز في ملف المرشحين", file=sys.stderr)
        return 2

    quotes: Dict[str, dict] = {}
    if not args.offline:
        if not args.api_key:
            print("لا مفتاح API — استخدم --offline", file=sys.stderr)
            return 2
        from .providers.twelve_data import TwelveDataProvider

        provider = TwelveDataProvider(api_key=args.api_key,
                                      max_requests_per_minute=args.rate_limit)
        quotes = provider.get_quotes(symbols)
        print(f"جُلبت اقتباسات {len(quotes)} رمزاً ({provider.credits_used} رصيداً)",
              file=sys.stderr)

    cache = Path(args.cache_dir)
    snapshots = []
    for symbol in symbols:
        quote = quotes.get(symbol)
        if quote and "error" in quote:
            quote = None
        snapshot = snapshot_for(symbol, cache, quote)
        if snapshot is not None:
            snapshots.append(snapshot)

    config = BotConfig(
        account_equity=args.equity,
        risk_per_trade_pct=args.risk_pct / 100,
        require_fundamentals=False,   # الهيكل المالي غير متاح؛ يُذكر كنقطة عمياء
    )
    bot = EnhancedTradingBot(config)
    signals = {s.ticker: bot.evaluate(s) for s in snapshots}
    briefing = BriefingBuilder(config).build(snapshots, signals)
    text = briefing.to_markdown(top=args.top)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"الموجز: {args.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
