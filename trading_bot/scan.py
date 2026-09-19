"""فحص قائمة مراقبة ببيانات Twelve Data حقيقية.

    export TWELVE_DATA_API_KEY=xxxxx
    python -m trading_bot.scan VSME ABCD --equity 10000
    python -m trading_bot.scan --watchlist watchlist.txt --fundamentals fund.json --json

ملف الهيكل المالي (اختياري، ولا غنى عنه في الخطط دون Pro):

    {
      "VSME": {"market_cap": 1450000, "free_float": 980000,
               "has_active_shelf_offering": false}
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .config import BotConfig
from .data_feed import build_snapshot
from .engine import EnhancedTradingBot
from .models import Decision, Signal
from .providers.twelve_data import TwelveDataError, TwelveDataProvider


def _load_json(path: Optional[str]) -> Dict[str, dict]:
    if not path:
        return {}
    return json.loads(Path(path).read_text("utf-8"))


def _load_watchlist(path: Optional[str], inline: List[str]) -> List[str]:
    tickers = [t.strip().upper() for t in inline if t.strip()]
    if path:
        for line in Path(path).read_text("utf-8").splitlines():
            line = line.split("#", 1)[0].strip().upper()
            if line:
                tickers.append(line)
    seen, out = set(), []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def signal_to_dict(signal: Signal) -> dict:
    return {
        "ticker": signal.ticker,
        "decision": signal.decision.value,
        "score": round(signal.score, 1),
        "entry_limit": signal.entry_limit,
        "stop_loss": signal.stop_loss,
        "targets": signal.targets,
        "position_size": signal.position_size,
        "reasons": signal.reasons,
        "vetoes": signal.vetoes,
        "notes": signal.notes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.scan",
        description="فحص أسهم ببيانات Twelve Data عبر البوت المدمج (تنبيهات فقط — لا تنفيذ)",
    )
    parser.add_argument("tickers", nargs="*", help="رموز الأسهم، مثال: VSME ABCD")
    parser.add_argument("--watchlist", help="ملف نصي برمز في كل سطر")
    parser.add_argument("--api-key", default=os.environ.get("TWELVE_DATA_API_KEY"))
    parser.add_argument("--fundamentals", help="ملف JSON بالكاب والفلوت يدوياً")
    parser.add_argument("--equity", type=float, default=10_000.0, help="رأس المال")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="نسبة المخاطرة لكل صفقة %%")
    parser.add_argument("--candles-4h", type=int, default=120)
    parser.add_argument("--candles-1d", type=int, default=120)
    parser.add_argument("--rate-limit", type=int, default=8, help="طلبات/دقيقة (المجاني=8)")
    parser.add_argument("--prepost", action="store_true", help="تضمين ما قبل/بعد الجلسة")
    parser.add_argument("--only-buys", action="store_true", help="اعرض الإشارات المؤكدة فقط")
    parser.add_argument("--json", action="store_true", help="أخرج JSON بدل النص")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    tickers = _load_watchlist(args.watchlist, args.tickers)
    if not tickers:
        print("لا توجد رموز — مرّر رموزاً أو --watchlist", file=sys.stderr)
        return 2

    try:
        provider = TwelveDataProvider(
            api_key=args.api_key,
            max_requests_per_minute=args.rate_limit,
            fundamentals_override=_load_json(args.fundamentals),
            prepost=args.prepost,
        )
    except TwelveDataError as exc:
        print(f"خطأ: {exc}", file=sys.stderr)
        return 2

    bot = EnhancedTradingBot(
        BotConfig(account_equity=args.equity, risk_per_trade_pct=args.risk_pct / 100)
    )

    signals: List[Signal] = []
    for ticker in tickers:
        try:
            snapshot = build_snapshot(
                provider, ticker, candles_4h=args.candles_4h, candles_1d=args.candles_1d
            )
        except TwelveDataError as exc:
            print(f"[data] تخطي {ticker}: {exc}", file=sys.stderr)
            continue
        signals.append(bot.evaluate(snapshot))

    signals.sort(key=lambda s: (s.decision is Decision.BUY_CONFIRMED, s.score), reverse=True)
    if args.only_buys:
        signals = [s for s in signals if s.is_actionable]

    if args.json:
        print(json.dumps(
            {"signals": [signal_to_dict(s) for s in signals], "warnings": provider.warnings},
            ensure_ascii=False,
            indent=2,
        ))
    else:
        for signal in signals:
            print(signal.to_alert())
            print("-" * 64)
        for warning in provider.warnings:
            print(f"⚠ {warning}")
        print("تنبيه: تنبيهات بحثية فقط — لا تنفيذ آلي ولا توصية مالية.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
